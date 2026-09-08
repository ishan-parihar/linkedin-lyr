"""Voyager HTTP session probe for LinkedIn — EXPLICIT OPT-IN ONLY.

#2601 (2026-09-08, proven live): replaying browser-minted cookies over plain
HTTP — from ANY egress, home IP or relayed through the fleet — is itself a
revocation trigger. LinkedIn answered a freshly minted, in-browser-validated
jar with ``302`` + ``li_at=delete me`` + ``clearSiteData: storage`` on the very
first HTTP probe, and the revocation propagated into the source browser. The
earlier belief that "importing cookies into an automated browser triggers
rotation (#2329) but plain HTTP is safe" is backwards: the browser context is
the ONLY place these cookies may be used.

Therefore ``probe_session``/``aprobe_session`` make NO network request unless
``LINKEDIN_HTTP_PROBE=1`` is explicitly set, and default to ``unknown``. Every
runtime gate (pre-flight, validators, relogin confirmation, pool failover,
BF injection choice) must treat ``unknown`` as "keep the session, decide
nothing" — liveness is proven by the browser itself (/feed/ loads or the
authwall appears), never by an out-of-band HTTP call.

When the gate IS explicitly enabled: GET the Voyager ``/me`` endpoint with the
cookie + csrf-token (JSESSIONID minus the ``ajax:`` prefix) +
``x-restli-protocol-version: 2.0.0``. Verdicts are honest, not binary (#2593):
a probe that could not ask LinkedIn the question (network error, timeout, 5xx,
rate limit) returns ``unknown``, never ``dead``. Only LinkedIn *answering*
"you are not logged in" from a trusted egress is ``dead`` — and even that
verdict must never feed an automatic invalidation, because the probe itself
may have just revoked the session it measured.

Egress: when BrowseFleet is configured (``BROWSEFLEET_URL`` + token, sourced
from ``~/.linkedin-lyr/bf.env`` by the entrypoints), the probe is relayed
through the fleet's ``/v1/egress/probe`` endpoint. Otherwise the direct
request honors ``PROXY_SERVER`` when set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Literal

from curl_cffi import requests as cffi_req

logger = logging.getLogger(__name__)

_VOYAGER_PROBE = (
    "https://www.linkedin.com/voyager/api/me?decorationId="
    "com.linkedin.voyager.deco.identity.web.ProfileTopCardDecoration-14"
)
# Generic desktop Chrome UA fallback. The session's real UA (from
# source-state.json via config, or LINKEDIN_PROBE_UA) always wins when set:
# probing with a UA the cookies were never minted under is one more signal
# LinkedIn can score, and a 2023-era UA in 2026 is itself suspicious.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Human-readable verdicts callers branch on. ``unknown`` means "the question
# could not be answered" — never treat it as evidence the session died.
ProbeVerdict = Literal["alive", "dead", "unknown", "missing"]

# Statuses that mean LinkedIn answered and the answer was "not logged in".
# 302 to the login flow is the classic rotated-session response; 401/403 are
# the API-level rejections. Everything else (5xx, 429, LinkedIn's 999) is
# LinkedIn or the network failing to answer, not the session dying.
_DEAD_STATUSES = frozenset({301, 302, 401, 403})


def csrf_token(cookies: dict[str, str]) -> str | None:
    """csrf-token = JSESSIONID value minus the ``ajax:`` prefix (#2430)."""
    raw = cookies.get("JSESSIONID") or cookies.get("jsessionid")
    if not raw:
        return None
    return raw.removeprefix("ajax:")


def normalize_cookies(raw: Any) -> dict[str, str]:
    """Coerce every cookie-file shape seen in the wild to a flat name→value dict.

    Accepts the canonical list of ``{"name", "value", ...}`` objects, the
    single-wrap export shape ``{"cookies": [...]}``, the legacy
    ``{"cookies": {name: value}}`` wrap, and a flat ``{name: value}`` dict
    (which is what ``cookies_bc3.json`` and Obscura's FileCookieStorage write).
    Entries without both a name and a non-empty value are dropped — a cookie
    list that parses to nothing must look like "missing", not crash or, worse,
    silently probe with an empty jar.
    """
    if isinstance(raw, dict):
        inner = raw.get("cookies", raw)
        if isinstance(inner, dict):
            return {
                key: str(value)
                for key, value in inner.items()
                if key and value
            }
        raw = inner
    if isinstance(raw, list):
        return {
            cookie["name"]: str(cookie["value"])
            for cookie in raw
            if isinstance(cookie, dict) and cookie.get("name") and cookie.get("value")
        }
    return {}


def _source_state_ua() -> str | None:
    """The UA the session's cookies were minted under, from source-state.json.

    Read directly rather than through get_config(): config loading parses
    sys.argv, which exits the process under pytest or any embedder whose argv
    is not the CLI's. A plain file read has no such blast radius.
    """
    try:
        from linkedin_mcp_server.session_state import source_state_path

        data = json.loads(source_state_path().read_text())
    except Exception:  # noqa: BLE001 - a missing/unreadable state file is fine
        return None
    user_agent = data.get("user_agent") if isinstance(data, dict) else None
    if isinstance(user_agent, str) and user_agent.strip():
        return user_agent.strip()
    return None


def _probe_user_agent() -> str:
    """The UA to probe under: env override, then the mint-time UA, then fallback."""
    env_ua = os.environ.get("LINKEDIN_PROBE_UA", "").strip()
    if env_ua:
        return env_ua
    return _source_state_ua() or _UA


def _bf_egress_config() -> tuple[str | None, str | None]:
    """Return (base_url, token) for the BrowseFleet egress path, or (None, None).

    Reads the environment the entrypoints already sourced from
    ``~/.linkedin-lyr/bf.env`` / ``~/.browsefleet.env``; sources them itself as
    a fallback (``load_proxy_env`` is setdefault-based, so explicit env wins).
    """
    try:
        from linkedin_mcp_server.common_utils import load_proxy_env

        load_proxy_env()
    except Exception:  # noqa: BLE001 - env sourcing is best-effort
        pass
    url = os.environ.get("BROWSEFLEET_URL", "").strip() or None
    token = (
        os.environ.get("BROWSEFLEET_TOKEN", "").strip()
        or os.environ.get("BROWSEFLEET_API_KEY", "").strip()
        or None
    )
    if url and token:
        return url, token
    return None, None


def _probe_via_bf(
    cookies: dict[str, str], csrf: str | None, timeout: float
) -> int | None:
    """Probe through the BrowseFleet egress endpoint. Returns status or None.

    None means the relay itself failed (unreachable, bad payload, timeout) —
    which says nothing about the LinkedIn session and must map to ``unknown``,
    not ``dead``.
    """
    base, token = _bf_egress_config()
    if not base or not token:
        return None
    endpoint = base.rstrip("/") + "/v1/egress/probe"
    payload = {
        "url": _VOYAGER_PROBE,
        "cookies": cookies,
        "csrfToken": csrf,
        "timeoutMs": int(timeout * 1000),
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": token,
            # Cloudflare in front of the fleet 403s the default
            # "Python-urllib/x.y" UA, which made every relayed probe look like
            # a relay failure (unknown) even when the fleet was fine.
            "User-Agent": _probe_user_agent(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        logger.debug("voyager probe via BF: relay http %s", exc.code)
        return None
    except Exception as exc:  # noqa: BLE001 - any relay failure is "no answer"
        logger.debug("voyager probe via BF: relay error %s", exc)
        return None
    status = data.get("status")
    if isinstance(status, int):
        logger.debug(
            "voyager probe via BF: linkedin status=%s clearSiteData=%s",
            status,
            data.get("clearSiteData"),
        )
        return status
    return None


def _direct_probe(cookies: dict[str, str], timeout: float) -> int | None:
    """Probe straight from this host. Returns status, or None on transport error."""
    headers = {
        "User-Agent": _probe_user_agent(),
        "Accept": "application/json",
        "x-restli-protocol-version": "2.0.0",
    }
    token = csrf_token(cookies)
    if token:
        headers["csrf-token"] = token

    proxies: dict[str, str] | None = None
    proxy_server = os.environ.get("PROXY_SERVER", "").strip()
    if proxy_server:
        proxies = {"http": proxy_server, "https": proxy_server}

    try:
        resp = cffi_req.get(
            _VOYAGER_PROBE,
            headers=headers,
            cookies=cookies,
            impersonate="chrome",
            timeout=timeout,
            allow_redirects=False,
            proxies=proxies,
        )
    except Exception as exc:  # noqa: BLE001 - transport failure is "no answer"
        logger.debug("voyager direct probe error: %s", exc)
        return None
    return resp.status_code


def _classify(status: int | None, *, trusted_egress: bool) -> ProbeVerdict:
    """Map a probe status to a verdict.

    200 is alive from any egress. The not-logged-in statuses are ``dead`` only
    from an egress the session trusts (the fleet the browser runs on, or the
    configured proxy); from a bare datacenter host the same statuses can mean
    "this IP is not trusted for this account", which is not a cookie problem,
    so they classify as ``unknown`` there.
    """
    if status is None:
        return "unknown"
    if status == 200:
        return "alive"
    if status in _DEAD_STATUSES:
        return "dead" if trusted_egress else "unknown"
    return "unknown"


def _probe_attempts() -> int:
    try:
        return max(1, int(os.environ.get("LINKEDIN_PROBE_ATTEMPTS", "2")))
    except ValueError:
        return 2


def _http_probe_enabled() -> bool:
    """Whether out-of-band HTTP probing is explicitly opted into.

    Default OFF (#2601): replaying browser-minted cookies over HTTP is itself
    a revocation trigger, so the runtime must never contact LinkedIn outside
    a browser context. Set ``LINKEDIN_HTTP_PROBE=1`` only for deliberate
    diagnostics on jars known to be HTTP-safe (e.g. server-minted sessions).
    """
    return os.environ.get("LINKEDIN_HTTP_PROBE", "").strip() == "1"


def probe_session(
    cookies: Any, timeout: float = 10.0, attempts: int | None = None
) -> ProbeVerdict:
    """Verdict a cookie dict at the Voyager endpoint. Never boots a browser.

    Makes NO network request unless ``LINKEDIN_HTTP_PROBE=1`` (#2601) — an
    out-of-band HTTP replay of browser-minted cookies is itself a revocation
    trigger — and returns ``unknown`` in that default case. Callers must
    treat ``unknown`` as "no evidence either way; keep the session".

    When explicitly enabled, retries ``unknown`` outcomes (default two
    attempts, override with ``LINKEDIN_PROBE_ATTEMPTS``) because a single
    failed TLS handshake to a Cloudflare-fronted endpoint is routine. A
    definitive ``alive``/``dead`` returns immediately; only a probe that
    never got an answer after all attempts returns ``unknown``.
    """
    normalized = normalize_cookies(cookies)
    if not normalized or not normalized.get("li_at"):
        return "missing"

    if not _http_probe_enabled():
        logger.debug("voyager probe skipped (LINKEDIN_HTTP_PROBE unset); verdict unknown")
        return "unknown"

    if attempts is None:
        attempts = _probe_attempts()

    bf_url, _bf_token = _bf_egress_config()
    csrf = csrf_token(normalized)
    verdict: ProbeVerdict = "unknown"
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(min(2.0 * attempt, 5.0))
        if bf_url:
            status = _probe_via_bf(normalized, csrf, timeout)
            trusted = status is not None
            if status is None:
                # Fleet unreachable: fall back to the direct path so a BF
                # outage degrades to a weaker signal instead of no signal.
                status = _direct_probe(normalized, timeout)
                trusted = False
            verdict = _classify(status, trusted_egress=trusted)
        else:
            proxy_configured = bool(os.environ.get("PROXY_SERVER", "").strip())
            status = _direct_probe(normalized, timeout)
            verdict = _classify(status, trusted_egress=proxy_configured)
        if verdict != "unknown":
            return verdict
    return verdict


async def aprobe_session(
    cookies: Any, timeout: float = 10.0, attempts: int | None = None
) -> ProbeVerdict:
    """Async wrapper so event-loop callers don't block on the sync client."""
    return await asyncio.to_thread(probe_session, cookies, timeout, attempts)

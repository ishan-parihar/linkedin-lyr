"""Voyager direct-HTTP session probe for LinkedIn.

The single source of truth for "is this session alive". A direct HTTP probe per
#2430: GET the Voyager profiles endpoint with the cookie + csrf-token
(JSESSIONID minus the ``ajax:`` prefix) + ``x-restli-protocol-version: 2.0.0``.
A dead/rotated session returns a 302 self-loop; a live session returns 200 with
authed JSON.

This module deliberately never boots a browser. Importing stored cookies into
an automated browser is what triggers LinkedIn's server-side rotation (#2329),
so liveness must be decided over plain HTTP or not at all.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from curl_cffi import requests as cffi_req

logger = logging.getLogger(__name__)

_VOYAGER_PROBE = (
    "https://www.linkedin.com/voyager/api/identity/profiles"
    "?q=memberIdentity&memberIdentity=__probe__"
)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Human-readable verdicts callers branch on.
ProbeVerdict = Literal["alive", "dead", "missing"]


def csrf_token(cookies: dict[str, str]) -> str | None:
    """csrf-token = JSESSIONID value minus the ``ajax:`` prefix (#2430)."""
    raw = cookies.get("JSESSIONID") or cookies.get("jsessionid")
    if not raw:
        return None
    return raw.removeprefix("ajax:")


def probe_session(cookies: dict[str, str], timeout: float = 10.0) -> ProbeVerdict:
    """Verdict a cookie dict at the Voyager endpoint. Never boots a browser."""
    if not cookies or not cookies.get("li_at"):
        return "missing"

    token = csrf_token(cookies)
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json",
        "x-restli-protocol-version": "2.0.0",
    }
    if token:
        headers["csrf-token"] = token

    try:
        with cffi_req.Client(impersonate="chrome", timeout=timeout, allow_redirects=False) as client:
            resp = client.get(_VOYAGER_PROBE, headers=headers, cookies=cookies)
    except Exception as exc:  # network blip -> treat as dead, not alive
        logger.debug("voyager probe error: %s", exc)
        return "dead"

    # A live session answers 200 with authed JSON. Anything else (302 self-loop,
    # 401, 403, 500) means the session is not usable for writes.
    if resp.status_code == 200:
        logger.debug("voyager probe: alive (200)")
        return "alive"
    logger.debug(
        "voyager probe: dead (status=%d, location=%s)",
        resp.status_code,
        resp.headers.get("location"),
    )
    return "dead"


async def aprobe_session(cookies: dict[str, str], timeout: float = 10.0) -> ProbeVerdict:
    """Async wrapper so event-loop callers don't block on the sync client."""
    return await asyncio.to_thread(probe_session, cookies, timeout)
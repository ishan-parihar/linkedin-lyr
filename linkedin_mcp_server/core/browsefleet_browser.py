"""
BrowseFleet browser manager — thin CDP client for the remote CloakBrowser pool.

Mirrors :class:`linkedin_mcp_server.core.obscura_browser.ObscuraBrowserManager`
so the rest of the codebase (``drivers/browser.py``, ``scraping/extractor.py``,
``tool_registry.py``) can switch backends by changing ``LINKEDIN_BROWSER_BACKEND=browsefleet``
or setting ``BROWSEFLEET_URL``.

The fleet runs at https://browsefleet.ishanparihar.com (local Docker on
omarchy, tunneled via cloudflared). The VPS makes only HTTP + WebSocket
calls — no local Chromium, no 2 GB shm, no VNC.

Session lifecycle (BrowseFleet HTTP API):
  POST /v1/sessions {stealth, viewport, cookies, profileId, proxyUrl, timeout}
    → {id, websocketUrl, viewerUrl, ...}
  playwright.chromium.connect_over_cdp(websocketUrl) → Browser → Page
  POST /v1/sessions/:id/release  (on close; cookies persist if profileId was set)

Cookie flow:
  - Preferred: ``BROWSEFLEET_PROFILE_ID=linkedin-ishan`` — one manual login via
    ``viewerUrl`` (operatorMode) persists; subsequent creates with the same
    profileId need no cookie replay.
  - Fallback: extract 14 Brave-Origin cookies locally and push them at create
    time as ``[{name,value,domain:".linkedin.com",path:"/"}]``. The fleet also
    injects them via ``page.setCookie`` for redundancy.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_REQUIRED_COOKIES = {"li_at", "bscookie"}
_DEFAULT_USER_DATA_DIR = Path.home() / ".linkedin-lyr" / "profile"
_PRIVATE_FILE_MODE = 0o600


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _browsefleet_config() -> tuple[str, str | None, str | None, int]:
    """Resolve (url, token, profile_id, timeout_ms) from env/config."""
    # Ensure proxy/bf envs are loaded (setdefault, so explicit env wins).
    from linkedin_mcp_server.common_utils import load_proxy_env

    load_proxy_env()

    # Config is authoritative when loaded; env is fallback before config init.
    try:
        from linkedin_mcp_server.config import get_config

        cfg = get_config().browser
        url = (cfg.browsefleet_url or os.environ.get("BROWSEFLEET_URL") or "").strip()
        token = (cfg.browsefleet_token or os.environ.get("BROWSEFLEET_TOKEN") or os.environ.get("BROWSEFLEET_API_KEY") or "").strip() or None
        profile = (cfg.browsefleet_profile_id or os.environ.get("BROWSEFLEET_PROFILE_ID") or "").strip() or None
        timeout = int(cfg.browsefleet_timeout_ms or 30000)
        return url, token, profile, timeout
    except Exception:
        url = os.environ.get("BROWSEFLEET_URL", "").strip()
        token = (os.environ.get("BROWSEFLEET_TOKEN") or os.environ.get("BROWSEFLEET_API_KEY") or "").strip() or None
        profile = os.environ.get("BROWSEFLEET_PROFILE_ID", "").strip() or None
        timeout = int(os.environ.get("BROWSEFLEET_TIMEOUT", "30000").strip() or "30000")
        return url, token, profile, timeout


def _load_portable_cookies() -> dict[str, str]:
    """Load linkedin cookies from portable file, if present."""
    candidates = [
        Path.home() / ".linkedin-lyr" / "cookies.json",
        Path.home() / ".linkedin" / "cookies.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                if "cookies" in data:
                    raw = data["cookies"]
                    if isinstance(raw, dict):
                        return {k: str(v) for k, v in raw.items()}
                    elif isinstance(raw, list):
                        return {c["name"]: str(c["value"]) for c in raw if "name" in c and "value" in c}
                else:
                    # Flat dict
                    return {k: str(v) for k, v in data.items() if isinstance(v, str)}
            elif isinstance(data, list):
                return {c["name"]: str(c["value"]) for c in data if isinstance(c, dict) and "name" in c and "value" in c}
        except Exception as exc:
            logger.warning("Failed to parse cookie file %s: %s", path, exc)
    return {}


def _synthesize_ua() -> str | None:
    """Best-effort UA synthesis from the local Brave install."""
    try:
        from linkedin_mcp_server.browser_import.user_agent import (
            read_engine_version,
            synthesize_user_agent,
        )

        ver = read_engine_version()
        if ver:
            return synthesize_user_agent(ver)
    except Exception:
        pass
    return None


def _extract_browser_cookies() -> dict[str, str]:
    """Try to extract cookies from the local browser (Brave-Origin etc)."""
    try:
        from linkedin_mcp_server.browser_cookie_extractor import extract_linkedin_cookies

        result = extract_linkedin_cookies()
        all_cookies = result.get("all_cookies") or {}
        if all_cookies:
            return {k: str(v) for k, v in all_cookies.items()}
    except Exception as exc:
        logger.debug("Browser cookie extraction failed: %s", exc)
    return {}


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class BrowseFleetBrowserManager:
    """Thin CDP client backed by the BrowseFleet remote pool.

    API surface intentionally mirrors ``ObscuraBrowserManager`` so callers
    (drivers/browser.py, scraping/extractor.py) need only swap the class.
    """

    def __init__(
        self,
        user_data_dir: str | Path = _DEFAULT_USER_DATA_DIR,
        headless: bool = True,
        slow_mo: int = 0,
        viewport: dict[str, int] | None = None,
        user_agent: str | None = None,
        cdp_port: int = 9224,
        stealth: str = "full",
        profile_id: str | None = None,
        **launch_options: Any,
    ):
        self.user_data_dir = str(Path(user_data_dir).expanduser())
        self.headless = headless
        self.slow_mo = slow_mo
        self.viewport = viewport or {"width": 1280, "height": 720}
        self.user_agent = user_agent
        self.launch_options = launch_options
        self._explicit_profile_id = profile_id

        self._cookies: dict[str, str] = {}
        self._is_authenticated = False
        self._storage_dir: Path | None = None
        self._session_id: str | None = None
        self._cdp_endpoint: str | None = None
        self._viewer_url: str | None = None

        self._playwright_obj: Any = None
        self._playwright_browser: Any = None
        self._playwright_context: Any = None
        self._playwright_page: Any = None
        self._close_confirmed = False
        self._stealth = stealth

    # --- context manager ---------------------------------------------------

    async def __aenter__(self) -> BrowseFleetBrowserManager:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self._close_confirmed = await self.close()

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Create a BrowseFleet session and connect via CDP."""
        if self._playwright_browser is not None or self._session_id is not None:
            raise RuntimeError("Browser already started. Call close() first.")

        url, token, profile_id, timeout_ms = _browsefleet_config()
        profile_id = self._explicit_profile_id or profile_id
        if not url:
            raise RuntimeError(
                "BrowseFleet is selected but BROWSEFLEET_URL is not set. "
                "Set BROWSEFLEET_URL=https://browsefleet.ishanparihar.com and "
                "BROWSEFLEET_TOKEN, or switch back with LINKEDIN_BROWSER_BACKEND=obscura."
            )
        url = url.rstrip("/")

        # Resolve cookies: live Brave extraction is preferred (freshest li_at),
        # portable file is fallback. When both exist, prefer the one that probes
        # alive, falling back to extractor's fresher value.
        portable = _load_portable_cookies()
        extracted = _extract_browser_cookies()
        cookies: dict[str, str] = {}
        if extracted and "li_at" in extracted:
            if portable and "li_at" in portable and portable["li_at"] != extracted["li_at"]:
                try:
                    from linkedin_mcp_server.voyager_auth import probe_session

                    ext_alive = probe_session(extracted) == "alive"
                    port_alive = probe_session(portable) == "alive" if portable else False
                    if ext_alive:
                        cookies = extracted
                        logger.info("Using %d cookies from live Brave (probed alive)", len(cookies))
                    elif port_alive:
                        cookies = portable
                        logger.info("Using %d cookies from portable file (extractor dead)", len(cookies))
                    else:
                        cookies = extracted
                        logger.info("Using %d cookies from live Brave (both stale, preferring fresh)", len(cookies))
                except Exception:
                    cookies = extracted
                    logger.info("Using %d cookies extracted from local browser", len(cookies))
            else:
                cookies = extracted
                logger.info("Using %d cookies extracted from local browser", len(cookies))
        elif portable and "li_at" in portable:
            cookies = portable
            logger.info("Using %d cookies from portable file", len(cookies))
        else:
            cookies = extracted or portable
            if cookies:
                logger.info("Using %d cookies (fallback)", len(cookies))
            else:
                cookies = {}
                logger.warning("No cookies found from Brave or portable file")

        self._cookies = cookies
        self._is_authenticated = _REQUIRED_COOKIES.issubset(cookies.keys())

        # Synthesize Brave UA if none was passed explicitly (keeps LinkedIn
        # from challenging on UA mismatch after a fresh import).
        if not self.user_agent:
            try:
                from linkedin_mcp_server.config import get_config

                cfg_ua = get_config().browser.user_agent
                if cfg_ua:
                    self.user_agent = cfg_ua
                else:
                    self.user_agent = _synthesize_ua()
            except Exception:
                self.user_agent = _synthesize_ua()

        # Build CreateSessionRequest
        payload: dict[str, Any] = {
            "stealth": self._stealth,
            "viewport": self.viewport,
            "timeout": timeout_ms,
        }
        if profile_id:
            payload["profileId"] = profile_id
        if cookies:
            payload["cookies"] = [
                {"name": name, "value": value, "domain": ".linkedin.com", "path": "/"}
                for name, value in cookies.items()
            ]
        # Proxy: prefer explicit launch_options proxy, else env/config proxy.
        proxy = (
            self.launch_options.get("proxy")
            or self.launch_options.get("proxy_server")
            or os.environ.get("PROXY_SERVER")
        )
        if proxy:
            # Normalize to proxyUrl string expected by BrowseFleet.
            if isinstance(proxy, dict) and "server" in proxy:
                proxy = proxy["server"]
            payload["proxyUrl"] = str(proxy)

        if self.user_agent:
            payload["userAgent"] = self.user_agent

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if token:
            headers["x-api-key"] = token

        # Create remote session (async via httpx)
        logger.info("Creating BrowseFleet session at %s (profileId=%s, cookies=%d, stealth=%s)", url, profile_id or "-", len(cookies), self._stealth)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                resp = await client.post(f"{url}/v1/sessions", json=payload, headers=headers)
        except Exception as exc:
            raise RuntimeError(f"Failed to reach BrowseFleet at {url}: {exc}") from exc

        if resp.status_code not in (200, 201):
            body = resp.text[:500]
            # Auto-heal: profile lock leftover from a crashed browser. If the
            # BF server has the unlock endpoint (≥v1.2.0), call it once and
            # retry the session creation a single time. The endpoint deletes
            # stale Chromium SingletonLock/SingletonSocket/SingletonCookie
            # files from the profile data dir; it never touches cookies or
            # browsing state. If unlock fails (older server version, network
            # blip), fall through to the original error.
            if profile_id and "already running" in body:
                unlock_url = f"{url}/v1/profiles/{profile_id}/unlock"
                try:
                    logger.warning(
                        "BrowseFleet profile %s locked, attempting auto-unlock via %s",
                        profile_id, unlock_url,
                    )
                    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as unlock_client:
                        unlock_resp = await unlock_client.post(unlock_url, headers=headers)
                    if unlock_resp.status_code in (200, 201, 404):
                        # 404 = endpoint doesn't exist on old server; surface original error.
                        # 200/201 = locks cleared, retry session creation.
                        if unlock_resp.status_code != 404:
                            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as retry_client:
                                resp = await retry_client.post(f"{url}/v1/sessions", json=payload, headers=headers)
                            if resp.status_code not in (200, 201):
                                body = resp.text[:500]
                                raise RuntimeError(
                                    f"BrowseFleet session creation failed ({resp.status_code}) "
                                    f"after profile unlock retry: {body}"
                                )
                        else:
                            raise RuntimeError(
                                f"BrowseFleet session creation failed ({resp.status_code}): {body} "
                                f"(unlock endpoint not available on server; upgrade BF to ≥1.2.0)"
                            )
                    else:
                        raise RuntimeError(
                            f"BrowseFleet session creation failed ({resp.status_code}): {body} "
                            f"(unlock attempt failed: {unlock_resp.status_code})"
                        )
                except RuntimeError:
                    raise
                except Exception as unlock_exc:
                    raise RuntimeError(
                        f"BrowseFleet session creation failed ({resp.status_code}): {body} "
                        f"(unlock attempt errored: {unlock_exc})"
                    ) from unlock_exc
            else:
                raise RuntimeError(f"BrowseFleet session creation failed ({resp.status_code}): {body}")

        try:
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"BrowseFleet returned non-JSON: {resp.text[:300]}") from exc

        self._session_id = data.get("id")
        self._cdp_endpoint = data.get("websocketUrl") or data.get("wsEndpoint")
        self._viewer_url = data.get("viewerUrl")
        if not self._session_id or not self._cdp_endpoint:
            raise RuntimeError(f"BrowseFleet session missing id/websocketUrl: {data}")

        logger.info("BrowseFleet session %s created, CDP %s", self._session_id, self._cdp_endpoint)
        if self._viewer_url:
            logger.info("Viewer: %s", self._viewer_url)

        # Connect Playwright over CDP (remote) — try the fleet-reported URL
        # first, then fall back to a WS derived from BROWSEFLEET_URL so
        # localhost dev works even when the tunnel (and thus the
        # browsefleet.ishanparihar.com WSS) is down (error 1033).
        def _with_token(u: str, t: str | None) -> str:
            if not t or "apiKey=" in u:
                return u
            return f"{u}{'&' if '?' in u else '?'}apiKey={t}"

        def _fallback_cdp(base: str, sid: str) -> str:
            from urllib.parse import urlparse

            parsed = urlparse(base)
            scheme = "wss" if parsed.scheme == "https" else "ws"
            host = parsed.hostname or "localhost"
            # Preserve explicit port (3000 for local, 443 for public wss).
            if parsed.port:
                host = f"{host}:{parsed.port}"
            elif scheme == "wss" and parsed.scheme == "https":
                # browsefleet.ishanparihar.com via 443 — omit default.
                pass
            return f"{scheme}://{host}/cdp/{sid}"

        # Ensure the original URL carries the token.
        self._cdp_endpoint = _with_token(self._cdp_endpoint or "", token)
        cdp_candidates = [self._cdp_endpoint]
        fallback = _with_token(_fallback_cdp(url, self._session_id), token)
        if fallback != self._cdp_endpoint:
            cdp_candidates.append(fallback)
        # Also try ws://localhost:3000 as ultimate local fallback when both fail.
        if url != "http://localhost:3000":
            local_fallback = _with_token(f"ws://localhost:3000/cdp/{self._session_id}", token)
            if local_fallback not in cdp_candidates:
                cdp_candidates.append(local_fallback)

        last_exc: Exception | None = None
        connected = False
        for cdp_url in cdp_candidates:
            try:
                from playwright.async_api import async_playwright

                if self._playwright_obj is None:
                    self._playwright_obj = await async_playwright().start()
                logger.info("Connecting CDP via %s", cdp_url)
                self._playwright_browser = await self._playwright_obj.chromium.connect_over_cdp(cdp_url)
                self._cdp_endpoint = cdp_url
                connected = True
                break
            except Exception as exc:
                last_exc = exc
                logger.warning("CDP connect failed via %s: %s", cdp_url, exc)
                # Tear down partially started playwright before next try
                if self._playwright_browser is not None:
                    try:
                        await self._playwright_browser.close()
                    except Exception:
                        pass
                    self._playwright_browser = None
                continue

        if not connected:
            try:
                await self._release_session(url, token, self._session_id)
            except Exception:
                pass
            self._session_id = None
            self._cdp_endpoint = None
            if self._playwright_obj is not None:
                try:
                    await self._playwright_obj.stop()
                except Exception:
                    pass
                self._playwright_obj = None
            raise RuntimeError(f"Failed to connect to BrowseFleet CDP endpoint {cdp_candidates}: {last_exc}") from last_exc

        contexts = self._playwright_browser.contexts
        if contexts:
            self._playwright_context = contexts[0]
        else:
            self._playwright_context = await self._playwright_browser.new_context(
                viewport=self.viewport,
                user_agent=self.user_agent,
            )

        self._playwright_page = await self._playwright_context.new_page()

        # Redundant cookie injection into the Playwright context (covers cases
        # where the HTTP create path did not propagate).
        if cookies:
            try:
                await self._playwright_context.add_cookies(
                    [{"name": n, "value": v, "domain": ".linkedin.com", "path": "/"} for n, v in cookies.items()]
                )
            except Exception as exc:
                logger.warning("Failed to add cookies to Playwright context: %s", exc)

        self._close_confirmed = False
        logger.info(
            "BrowseFleet browser session ready (session=%s, headless=%s)",
            self._session_id,
            self.headless,
        )

    async def _release_session(self, base_url: str, token: str | None, session_id: str) -> None:
        headers: dict[str, str] = {}
        if token:
            headers["x-api-key"] = token
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                await client.post(f"{base_url.rstrip('/')}/v1/sessions/{session_id}/release", headers=headers)
        except Exception as exc:
            logger.debug("Failed to release BrowseFleet session %s: %s", session_id, exc)

    async def close(self) -> bool:
        """Close the remote session and teardown local CDP bridge."""
        # Snapshot for release after local teardown
        session_id = self._session_id
        base_url, token, _, _ = _browsefleet_config()

        # Close Playwright handles
        try:
            if self._playwright_page:
                try:
                    await self._playwright_page.close()
                except Exception:
                    pass
            if self._playwright_context:
                try:
                    await self._playwright_context.close()
                except Exception:
                    pass
            if self._playwright_browser:
                try:
                    await self._playwright_browser.close()
                except Exception:
                    pass
            if self._playwright_obj:
                try:
                    await self._playwright_obj.stop()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("Error during BrowseFleet Playwright teardown: %s", exc)
        finally:
            self._playwright_page = None
            self._playwright_context = None
            self._playwright_browser = None
            self._playwright_obj = None

        # Release remote session (persists cookies if profileId was set)
        if session_id and base_url:
            await self._release_session(base_url, token, session_id)

        self._session_id = None
        self._cdp_endpoint = None
        self._viewer_url = None
        self._storage_dir = None
        self._close_confirmed = True
        logger.info("BrowseFleet browser session closed (session=%s)", session_id or "-")
        return True

    # --- state -------------------------------------------------------------

    @property
    def close_confirmed(self) -> bool:
        return self._close_confirmed

    @property
    def is_authenticated(self) -> bool:
        return self._is_authenticated

    @is_authenticated.setter
    def is_authenticated(self, value: bool) -> None:
        self._is_authenticated = value

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def viewer_url(self) -> str | None:
        return self._viewer_url

    # --- Page compatibility (mirrors ObscuraBrowserManager) ----------------

    async def goto(self, url: str, **kwargs: Any) -> None:
        logger.info("BrowseFleet navigating to %s", url)
        if self._playwright_page is None:
            raise RuntimeError("Browser not started")
        timeout = kwargs.pop("timeout", 60000)
        if "wait_until" in kwargs:
            kwargs["wait_until"] = kwargs.pop("wait_until")
        try:
            await self._playwright_page.goto(url, timeout=timeout, **kwargs)
        except Exception as exc:
            # LinkedIn returns 999/redirects that Playwright surfaces as
            # net::ERR_HTTP_RESPONSE_CODE_FAILURE; still capture the page.
            msg = str(exc)
            if "ERR_HTTP_RESPONSE_CODE_FAILURE" in msg or "net::ERR_" in msg:
                logger.warning("BrowseFleet goto soft-failed for %s: %s (continuing to capture content)", url, exc)
                # Give the page a moment to settle on the error/login redirect.
                try:
                    await self._playwright_page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                return
            raise

    async def content(self) -> str:
        if self._playwright_page is None:
            raise RuntimeError("Browser not started")
        return await self._playwright_page.content()

    async def title(self) -> str:
        if self._playwright_page is None:
            raise RuntimeError("Browser not started")
        return await self._playwright_page.title()

    async def url(self) -> str:
        if self._playwright_page is None:
            raise RuntimeError("Browser not started")
        return self._playwright_page.url

    async def evaluate(self, script: str, *args: Any) -> Any:
        if self._playwright_page is None:
            raise RuntimeError("Browser not started")
        return await self._playwright_page.evaluate(script, *args)

    async def set_cookie(self, name: str, value: str, domain: str = ".linkedin.com") -> None:
        if self._playwright_context is None:
            raise RuntimeError("Browser not started")
        await self._playwright_context.add_cookies([{"name": name, "value": value, "domain": domain, "path": "/"}])
        self._cookies[name] = value

    async def cookies(self) -> list[dict[str, Any]]:
        if self._playwright_context is None:
            return []
        return await self._playwright_context.cookies()

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        if self._playwright_context is None:
            raise RuntimeError("Browser not started")
        await self._playwright_context.add_cookies(cookies)
        for c in cookies:
            if c.get("name") and c.get("value"):
                self._cookies[c["name"]] = c["value"]

    async def import_cookies(
        self,
        cookie_path: str | Path | None = None,
        *,
        preset_name: str | None = None,
    ) -> bool:
        path = Path(cookie_path) if cookie_path else self._default_cookie_path()
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict) and "cookies" in data:
                raw = data["cookies"]
                if isinstance(raw, dict):
                    items = [{"name": k, "value": v, "domain": ".linkedin.com", "path": "/"} for k, v in raw.items()]
                else:
                    items = [c for c in raw if "linkedin.com" in c.get("domain", "")]
            elif isinstance(data, dict):
                items = [{"name": k, "value": v, "domain": ".linkedin.com", "path": "/"} for k, v in data.items() if isinstance(v, str)]
            elif isinstance(data, list):
                items = [c for c in data if "linkedin.com" in c.get("domain", "")]
            else:
                items = []
            if not any(c.get("name") == "li_at" for c in items):
                return False
            if self._playwright_context:
                await self._playwright_context.add_cookies(items)
            self._cookies = {c["name"]: c["value"] for c in items}
            self._is_authenticated = _REQUIRED_COOKIES.issubset(self._cookies.keys())
            return self._is_authenticated
        except Exception as exc:
            logger.error("Failed to import cookies %s: %s", path, exc)
            return False

    async def export_storage_state(self, storage_state_path: str | Path, *, indexed_db: bool = False) -> bool:
        return await self.export_cookies(storage_state_path)

    def _default_cookie_path(self) -> Path:
        return Path.home() / ".linkedin-lyr" / "cookies.json"

    async def export_cookies(self, cookie_path: str | Path | None = None) -> bool:
        path = Path(cookie_path) if cookie_path else self._default_cookie_path()
        try:
            from linkedin_mcp_server.common_utils import (
                harden_linkedin_tree,
                secure_mkdir,
                secure_write_text,
            )

            cookies = [
                {"name": n, "value": v, "domain": ".linkedin.com", "path": "/"}
                for n, v in self._cookies.items()
            ]
            if self._playwright_context:
                try:
                    # Prefer live context cookies (fresher li_at after rotation)
                    live = await self._playwright_context.cookies()
                    linkedin_live = [c for c in live if "linkedin.com" in c.get("domain", "")]
                    if any(c.get("name") == "li_at" for c in linkedin_live):
                        cookies = linkedin_live
                except Exception:
                    pass
            secure_mkdir(path.parent)
            harden_linkedin_tree(path.parent)
            secure_write_text(path, json.dumps(cookies, indent=2), mode=_PRIVATE_FILE_MODE)
            return True
        except Exception as exc:
            logger.error("Failed to export cookies: %s", exc)
            return False

    def cookie_file_exists(self, cookie_path: str | Path | None = None) -> bool:
        path = Path(cookie_path) if cookie_path else self._default_cookie_path()
        return path.exists()

    # --- Obscura compat shims ----------------------------------------------

    @property
    def page(self) -> BrowseFleetPage:
        return BrowseFleetPage(self)

    @property
    def context(self) -> _BrowseFleetContextProxy:
        return _BrowseFleetContextProxy(self)


# ---------------------------------------------------------------------------
# Page/context proxies (mirror obscura_browser)
# ---------------------------------------------------------------------------

class BrowseFleetPage:
    """Page-like wrapper delegating to the underlying Playwright page."""

    def __init__(self, manager: BrowseFleetBrowserManager):
        self._browser = manager

    @property
    def _playwright_page(self):  # type: ignore[no-redef]
        if self._browser._playwright_page is None:
            raise RuntimeError("BrowseFleet browser not started")
        return self._browser._playwright_page

    @property
    def main_frame(self):
        return self._playwright_page.main_frame

    @property
    def context(self):
        return self._playwright_page.context

    async def goto(self, url: str, **kwargs: Any) -> None:
        await self._browser.goto(url, **kwargs)

    async def content(self) -> str:
        return await self._browser.content()

    async def title(self) -> str:
        return await self._browser.title()

    @property
    def url(self) -> str:  # type: ignore[override]
        return self._browser._playwright_page.url if self._browser._playwright_page else ""  # type: ignore[union-attr]

    async def evaluate(self, script: str, *args: Any) -> Any:
        return await self._browser.evaluate(script, *args)

    def locator(self, selector: str) -> Any:
        return self._playwright_page.locator(selector)

    async def wait_for_selector(self, selector: str, timeout: int = 5000, state: str = "attached") -> Any:
        return await self._playwright_page.wait_for_selector(selector, timeout=timeout, state=state)

    def on(self, event: str, handler: Any) -> None:
        self._playwright_page.on(event, handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        self._playwright_page.remove_listener(event, handler)


class _BrowseFleetContextProxy:
    """Playwright-like BrowserContext proxy."""

    def __init__(self, manager: BrowseFleetBrowserManager):
        self._manager = manager

    async def cookies(self) -> list[dict[str, Any]]:
        return await self._manager.cookies()

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        await self._manager.add_cookies(cookies)

    async def set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        await self._manager.add_cookies(cookies)


# Re-export alias for isinstance checks in drivers that import ObscuraBrowserManager
BrowsfleetBrowserManager = BrowseFleetBrowserManager  # noqa: N816 (typo alias for greps)

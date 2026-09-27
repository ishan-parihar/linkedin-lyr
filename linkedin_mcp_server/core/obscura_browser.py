"""
Obscura-based browser manager for LinkedIn scraping.

Provides lightweight browser management using Obscura CDP server for LinkedIn scraping
with transformative performance improvements over traditional browser automation.
"""

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from linkedin_mcp_server.common_utils import (
    harden_linkedin_tree,
    secure_mkdir,
    secure_write_text,
)
from linkedin_mcp_server.cookie_import import (
    auto_extract_cookies,
    extract_cookies_from_browser,
)
from linkedin_mcp_server.core.obscura_error_handling import (
    handle_obscura_error,
    log_obscura_operation,
    log_obscura_performance,
    ObscuraError,
    ObscuraTimeoutError,
    ObscuraFetchError,
)
from linkedin_mcp_server.core.obscura_binary_manager import (
    ensure_obscura_binary,
)
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

# Obscura binary path
_OBSCURA_PATH = "/tmp/obscura"
# Required LinkedIn cookies
_REQUIRED_COOKIES = {"li_at", "bscookie"}
# Default user data directory
_DEFAULT_USER_DATA_DIR = Path.home() / ".linkedin-lyr" / "profile"
# Default CDP port
_DEFAULT_CDP_PORT = 9224
# Private file mode
_PRIVATE_FILE_MODE = 0o600


def _resolve_external_cdp() -> str | None:
    """Resolve an external Obscura CDP endpoint to attach to (no spawn).

    ``LINKEDIN_OBSCURA_CDP_URL`` — explicit ws:// URL wins.
    ``LINKEDIN_OBSCURA_ATTACH`` — cloakctl profile name (e.g. ``hermes``):
    resolve the profile's *raw* CDP port at runtime via ``cloakctl status
    --json`` (ports are dynamic across keeper restarts; the bridge
    wsEndpoint is NOT Playwright-compatible for new_page). Profile must be
    live or this returns None so the manager falls back to spawning.
    """
    explicit = os.environ.get("LINKEDIN_OBSCURA_CDP_URL", "").strip()
    if explicit:
        return explicit
    profile = os.environ.get("LINKEDIN_OBSCURA_ATTACH", "").strip()
    if not profile:
        return None
    import json as _json
    try:
        out = subprocess.run(
            ["cloakctl", "status", "--json"], capture_output=True, text=True, timeout=15
        )
        data = _json.loads(out.stdout)
        for pr in data.get("profiles", []):
            if pr.get("profile") == profile and pr.get("live") and pr.get("cdpPort"):
                endpoint = f"ws://127.0.0.1:{pr['cdpPort']}"
                logger.info("Attaching to external Obscura (cloakctl profile %s) at %s", profile, endpoint)
                return endpoint
        logger.warning("cloakctl profile %s not live; falling back to spawned browser", profile)
    except Exception as exc:
        logger.warning("cloakctl status resolution failed (%s); falling back to spawned browser", exc)
    return None


class ObscuraBrowserManager:
    """Obscura-based browser manager with Playwright-compatible interface.

    Uses Obscura in CDP server mode with Playwright's connectOverCDP for full
    JavaScript support and Playwright API compatibility.
    """

    def __init__(
        self,
        user_data_dir: str | Path = _DEFAULT_USER_DATA_DIR,
        headless: bool = True,
        slow_mo: int = 0,
        viewport: dict[str, int] | None = None,
        user_agent: str | None = None,
        cdp_port: int = _DEFAULT_CDP_PORT,
        **launch_options: Any,
    ):
        self.user_data_dir = str(Path(user_data_dir).expanduser())
        self.headless = headless
        self.slow_mo = slow_mo
        self.viewport = viewport or {"width": 1280, "height": 720}
        self.user_agent = user_agent
        self.cdp_port = cdp_port
        self.launch_options = launch_options

        # Obscura-specific state
        self._cookies: dict[str, str] = {}
        self._is_authenticated = False
        self._storage_dir: Path | None = None
        self._cdp_endpoint: str | None = None
        self._playwright_browser: Any = None
        self._playwright_context: Any = None
        self._playwright_page: Any = None
        self._playwright_obj: Any = None

        # Compatibility with Playwright interface
        self._close_confirmed = False
        self._external = False
        self._obscura_process = None

    async def __aenter__(self) -> "ObscuraBrowserManager":
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self._close_confirmed = await self.close()


    async def start(self) -> None:
        """Start Obscura browser session in CDP server mode."""
        if self._storage_dir is not None:
            raise RuntimeError("Browser already started. Call close() first.")

        external_ws = _resolve_external_cdp()
        if external_ws:
            return await self._start_attached(external_ws)

        try:
            # Clear only OUR port. NEVER `pkill -9 obscura`: that blast radius
            # kills every Obscura on the host (cloakctl keeper browser, the
            # shared port-9222 fallback) each time this server boots.
            try:
                subprocess.run(
                    ["fuser", "-k", f"{self.cdp_port}/tcp"], check=False, capture_output=True
                )
                logger.info("Cleared port %s for our own Obscura server", self.cdp_port)
            except Exception:
                pass  # Ignore errors from cleanup

            # Ensure Obscura binary is available and up to date
            binary_path = await ensure_obscura_binary()
            logger.info("Using Obscura binary: %s", binary_path)

            # Update global binary path
            global _OBSCURA_PATH
            _OBSCURA_PATH = str(binary_path)

            # Create storage directory for session persistence
            self._storage_dir = Path(self.user_data_dir)
            secure_mkdir(self._storage_dir)
            harden_linkedin_tree(self._storage_dir)

            # Start Obscura CDP server
            self._cdp_endpoint = f"ws://127.0.0.1:{self.cdp_port}"
            cmd = [
                _OBSCURA_PATH,
                "serve",
                "--port",
                str(self.cdp_port),
                "--storage-dir",
                str(self._storage_dir),
                "--stealth",
                "--quiet",
            ]
            # Route the browser's own traffic through the configured proxy
            # (e.g. home residential SOCKS tunnel — see proxy.env). Obscura
            # takes the proxy at the server level, not per-context.
            proxy_server = None
            if isinstance(self.launch_options, dict):
                _p = self.launch_options.get("proxy")
                if isinstance(_p, dict):
                    proxy_server = _p.get("server")
                elif isinstance(_p, str):
                    proxy_server = _p
            if not proxy_server:
                proxy_server = os.environ.get("PROXY_SERVER", "").strip() or None
            if proxy_server:
                cmd += ["--proxy", proxy_server]
                logger.info("Obscura egress routed through proxy %s", proxy_server)

            logger.info("Starting Obscura CDP server on port %s", self.cdp_port)
            self._obscura_process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )

            # Wait for server to start and check if it's running
            await asyncio.sleep(5)

            if self._obscura_process.poll() is not None:
                stdout, stderr = self._obscura_process.communicate()
                error_msg = (
                    f"Obscura CDP server failed to start. stderr: {stderr}, stdout: {stdout}"
                )
                logger.error(error_msg)
                raise RuntimeError(error_msg)

            # Connect Playwright to Obscura CDP server
            self._playwright_obj = await async_playwright().start()
            self._playwright_browser = await self._playwright_obj.chromium.connect_over_cdp(
                self._cdp_endpoint
            )

            # Get or create context
            contexts = self._playwright_browser.contexts
            if contexts:
                self._playwright_context = contexts[0]
            else:
                self._playwright_context = await self._playwright_browser.new_context(
                    viewport=self.viewport, user_agent=self.user_agent
                )

            # Create page
            self._playwright_page = await self._playwright_context.new_page()

            # Load cookies from existing file or extract from browser
            await self._load_cookies()

            # Set cookies in the Playwright context
            if self._cookies:
                await self._playwright_context.add_cookies([
                    {"name": name, "value": value, "domain": ".linkedin.com", "path": "/"}
                    for name, value in self._cookies.items()
                ])

            logger.info(
                "Obscura CDP browser session started (headless=%s, user_data_dir=%s, cdp_port=%s)",
                self.headless,
                self.user_data_dir,
                self.cdp_port,
            )

        except Exception as e:
            logger.error("Failed to start Obscura CDP browser: %s", e)
            if self._obscura_process:
                self._obscura_process.terminate()
            raise

    async def _start_attached(self, cdp_url: str) -> None:
        """Attach to an already-running external Obscura over CDP.

        The external browser (e.g. cloakctl keeper) owns its lifecycle:
        we never kill it, and close() only detaches. The context keeps its
        live cookies (in-browser liveness already proven); we only top up
        from the portable jar if li_at is missing.
        """
        self._external = True
        self._playwright_obj = await async_playwright().start()
        self._playwright_browser = await self._playwright_obj.chromium.connect_over_cdp(cdp_url)
        contexts = self._playwright_browser.contexts
        self._playwright_context = contexts[0] if contexts else await self._playwright_browser.new_context(
            viewport=self.viewport, user_agent=self.user_agent
        )
        if not self._playwright_context.pages:
            self._playwright_page = await self._playwright_context.new_page()
        else:
            self._playwright_page = self._playwright_context.pages[0]
        # Top up only if the attached context lacks a session jar.
        current = {c.get("name") for c in await self._playwright_context.cookies()}
        if "li_at" not in current:
            await self._load_cookies()
            if self._cookies:
                await self._playwright_context.add_cookies([
                    {"name": name, "value": value, "domain": ".linkedin.com", "path": "/"}
                    for name, value in self._cookies.items()
                ])
            logger.info("Attached context had no li_at; topped up %d cookies from portable jar", len(self._cookies))
        else:
            self._cookies = {c["name"]: c["value"] for c in await self._playwright_context.cookies() if "linkedin.com" in (c.get("domain") or "")}
            self._is_authenticated = True
            logger.info("Attached to live context with an existing LinkedIn session (%d cookies)", len(self._cookies))
        logger.info("Obscura external attach established (%s)", cdp_url)

    async def close(self) -> bool:
        """Close Obscura browser session and cleanup resources."""
        try:
            # Close Playwright page and context
            if self._playwright_page:
                await self._playwright_page.close()
            if self._playwright_context:
                await self._playwright_context.close()
            if self._playwright_browser:
                await self._playwright_browser.close()
            if self._playwright_obj:
                await self._playwright_obj.stop()

            # Terminate Obscura CDP server — only if WE spawned it.
            if getattr(self, "_external", False):
                # External attach: detach only. Never kill a browser we
                # don't own (cloakctl keeper, shared fallbacks).
                logger.info("External attach: detaching without killing the browser")
            elif hasattr(self, "_obscura_process") and self._obscura_process:
                self._obscura_process.terminate()
                try:
                    self._obscura_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._obscura_process.kill()
        except Exception as e:
            logger.warning("Error during browser close: %s", e)
        finally:
            self._storage_dir = None
            self._cookies = {}
            self._cdp_endpoint = None
            self._playwright_browser = None
            self._playwright_context = None
            self._playwright_page = None
            self._playwright_obj = None
            self._close_confirmed = True

        logger.info("Obscura CDP browser session closed")
        return True

    @property
    def close_confirmed(self) -> bool:
        """Whether the last close was confirmed."""
        return self._close_confirmed

    async def goto(self, url: str, **kwargs: Any) -> None:
        """Navigate to URL using Playwright."""
        logger.info("Navigating to %s with kwargs: %s", url, kwargs)
        try:
            # Pass through kwargs to Playwright's goto method
            # Add a timeout to prevent hanging
            timeout = kwargs.pop("timeout", 60000)  # Increased timeout to 60s
            logger.info("Starting navigation with timeout=%s", timeout)
            await self._playwright_page.goto(url, timeout=timeout, **kwargs)
            self._current_url = url
            logger.info("Navigation complete, current URL: %s", self._playwright_page.url)
        except Exception as e:
            logger.error("Navigation failed: %s", e)
            raise

    async def content(self) -> str:
        """Get current page content."""
        return await self._playwright_page.content()

    async def title(self) -> str:
        """Get page title."""
        return await self._playwright_page.title()

    async def url(self) -> str:
        """Get current URL."""
        return self._playwright_page.url

    async def evaluate(self, script: str) -> Any:
        """Evaluate JavaScript in page context."""
        return await self._playwright_page.evaluate(script)

    async def set_cookie(self, name: str, value: str, domain: str = ".linkedin.com") -> None:
        """Set cookie in session."""
        await self._playwright_context.add_cookies([
            {"name": name, "value": value, "domain": domain, "path": "/"}
        ])
        self._cookies[name] = value
        logger.debug("Cookie set: %s", name)

    async def cookies(self) -> list[dict[str, Any]]:
        """Get all cookies as Playwright-compatible format."""
        return await self._playwright_context.cookies()

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Add cookies in Playwright format."""
        await self._playwright_context.add_cookies(cookies)
        # Update internal cache
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if name and value:
                self._cookies[name] = value
        logger.info("Added %d cookies", len(cookies))

    async def import_cookies(
        self,
        cookie_path: str | Path | None = None,
        *,
        preset_name: str | None = None,
    ) -> bool:
        """Import cookies from portable JSON file (Playwright-compatible interface)."""
        path = Path(cookie_path) if cookie_path else self._default_cookie_path()

        if not path.exists():
            logger.debug("No cookie file at %s", path)
            return False

        try:
            cookie_data = json.loads(path.read_text())
            if not cookie_data:
                logger.debug("Cookie file is empty")
                return False

            # Filter for LinkedIn cookies
            linkedin_cookies = [
                cookie for cookie in cookie_data if "linkedin.com" in cookie.get("domain", "")
            ]

            # Check for required cookies
            has_li_at = any(c.get("name") == "li_at" for c in linkedin_cookies)
            if not has_li_at:
                logger.warning("No li_at cookie found in %s", path)
                return False

            # Load cookies into Playwright context
            if self._playwright_context:
                await self._playwright_context.add_cookies(linkedin_cookies)

            # Update internal cache
            self._cookies = {c["name"]: c["value"] for c in linkedin_cookies}

            if self._validate_cookies():
                self._is_authenticated = True
                logger.info("Imported %d cookies from %s", len(linkedin_cookies), path)
                return True
            else:
                logger.warning("Imported cookies missing required fields")
                return False

        except Exception as e:
            logger.error("Failed to import cookies from %s: %s", path, e)
            return False

    async def export_storage_state(
        self, storage_state_path: str | Path, *, indexed_db: bool = False
    ) -> bool:
        """Export storage state (cookies) to a file (Playwright-compatible interface)."""
        return await self.export_cookies(storage_state_path)

    @property
    def is_authenticated(self) -> bool:
        """Check if browser is authenticated."""
        return self._is_authenticated

    @is_authenticated.setter
    def is_authenticated(self, value: bool) -> None:
        """Set authentication status."""
        self._is_authenticated = value

    def _default_cookie_path(self) -> Path:
        """Get default cookie file path."""
        return Path.home() / ".linkedin-lyr" / "cookies.json"

    async def _load_cookies(self) -> None:
        """Load cookies from file or browser extraction."""
        # If cookies are already set (e.g., manually set before start), skip loading
        if self._cookies and self._is_authenticated:
            logger.debug("Cookies already set, skipping cookie loading")
            return

        # Try loading from storage directory first (for temp profiles)
        if self._storage_dir:
            storage_cookie_path = Path(self._storage_dir) / "cookies.json"
            if storage_cookie_path.exists():
                try:
                    cookie_data = json.loads(storage_cookie_path.read_text())

                    # Handle different cookie file formats
                    if isinstance(cookie_data, dict):
                        if "cookies" in cookie_data:
                            # Check if cookies is a dict (name: value) or list (cookie objects)
                            if isinstance(cookie_data["cookies"], dict):
                                self._cookies = cookie_data["cookies"]
                            else:
                                self._cookies = {
                                    c["name"]: c["value"] for c in cookie_data["cookies"]
                                }
                        else:
                            # Already in dict format
                            self._cookies = cookie_data
                    elif isinstance(cookie_data, list):
                        self._cookies = {c["name"]: c["value"] for c in cookie_data}
                    else:
                        self._cookies = cookie_data

                    if self._validate_cookies():
                        self._is_authenticated = True
                        # Load into Playwright context if available
                        if self._playwright_context:
                            playwright_cookies = [
                                {
                                    "name": name,
                                    "value": value,
                                    "domain": ".linkedin.com",
                                    "path": "/",
                                }
                                for name, value in self._cookies.items()
                            ]
                            await self._playwright_context.add_cookies(playwright_cookies)
                        logger.info("Loaded cookies from storage dir: %s", storage_cookie_path)
                        return
                except Exception as e:
                    logger.warning("Failed to load cookies from storage dir: %s", e)

        # Try loading from existing cookie file first
        cookie_path = self._default_cookie_path()
        if cookie_path.exists():
            try:
                cookie_data = json.loads(cookie_path.read_text())

                # Handle different cookie file formats
                if isinstance(cookie_data, dict):
                    if "cookies" in cookie_data:
                        # Check if cookies is a dict (name: value) or list (cookie objects)
                        if isinstance(cookie_data["cookies"], dict):
                            self._cookies = cookie_data["cookies"]
                        else:
                            self._cookies = {c["name"]: c["value"] for c in cookie_data["cookies"]}
                    else:
                        # Already in dict format
                        self._cookies = cookie_data
                elif isinstance(cookie_data, list):
                    self._cookies = {c["name"]: c["value"] for c in cookie_data}
                else:
                    self._cookies = cookie_data

                if self._validate_cookies():
                    self._is_authenticated = True
                    # Load into Playwright context if available
                    if self._playwright_context:
                        playwright_cookies = [
                            {"name": name, "value": value, "domain": ".linkedin.com", "path": "/"}
                            for name, value in self._cookies.items()
                        ]
                        await self._playwright_context.add_cookies(playwright_cookies)
                    logger.info("Loaded cookies from file: %s", cookie_path)
                    return
            except Exception as e:
                logger.warning("Failed to load cookies from file: %s", e)

        # Try extracting from browser
        try:
            cookies = auto_extract_cookies()
            if cookies and self._validate_cookies_dict(cookies):
                self._cookies = cookies
                self._is_authenticated = True
                # Load into Playwright context if available
                if self._playwright_context:
                    playwright_cookies = [
                        {"name": name, "value": value, "domain": ".linkedin.com", "path": "/"}
                        for name, value in self._cookies.items()
                    ]
                    await self._playwright_context.add_cookies(playwright_cookies)
                logger.info("Extracted cookies from browser")
            else:
                logger.warning("No valid cookies found in browser")
        except Exception as e:
            logger.warning("Failed to extract cookies from browser: %s", e)

    def _validate_cookies(self) -> bool:
        """Validate loaded cookies."""
        return _REQUIRED_COOKIES.issubset(self._cookies.keys())

    def _validate_cookies_dict(self, cookies: dict[str, str]) -> bool:
        """Validate cookie dictionary."""
        return _REQUIRED_COOKIES.issubset(cookies.keys())

    async def export_cookies(self, cookie_path: str | Path | None = None) -> bool:
        """Export cookies to portable JSON file."""
        path = Path(cookie_path) if cookie_path else self._default_cookie_path()

        try:
            cookies = [
                {"name": name, "value": value, "domain": ".linkedin.com", "path": "/"}
                for name, value in self._cookies.items()
            ]

            secure_mkdir(path.parent)
            harden_linkedin_tree(path.parent)
            secure_write_text(path, json.dumps(cookies, indent=2), mode=_PRIVATE_FILE_MODE)
            logger.info("Exported %d cookies to %s", len(cookies), path)
            return True
        except Exception as e:
            logger.error("Failed to export cookies: %s", e)
            return False

    def cookie_file_exists(self, cookie_path: str | Path | None = None) -> bool:
        """Check if cookie file exists."""
        path = Path(cookie_path) if cookie_path else self._default_cookie_path()
        return path.exists


# Page-like compatibility object
class ObscuraPage:
    """Page-like object for Obscura compatibility with Playwright interface.

    Now wraps Playwright's Page object for full JavaScript support.
    """

    def __init__(self, browser_manager: ObscuraBrowserManager):
        self._browser = browser_manager

    @property
    def _playwright_page(self):
        """Get the underlying Playwright page."""
        return self._browser._playwright_page

    # Expose Playwright page attributes directly
    @property
    def main_frame(self):
        """Get the main frame of the page."""
        return self._playwright_page.main_frame

    @property
    def context(self):
        """Get the browser context."""
        return self._playwright_page.context

    async def goto(self, url: str, **kwargs: Any) -> None:
        """Navigate to URL."""
        await self._playwright_page.goto(url, **kwargs)

    async def content(self) -> str:
        """Get page content."""
        return await self._playwright_page.content()

    async def title(self) -> str:
        """Get page title."""
        return await self._playwright_page.title()

    @property
    def url(self) -> str:
        """Get current URL."""
        return self._playwright_page.url

    async def evaluate(self, script: str, *args: Any) -> Any:
        """Evaluate JavaScript."""
        return await self._playwright_page.evaluate(script, *args)

    def locator(self, selector: str) -> Any:
        """Return a locator object (Playwright Locator)."""
        return self._playwright_page.locator(selector)

    async def wait_for_selector(
        self, selector: str, timeout: int = 5000, state: str = "attached"
    ) -> Any:
        """Wait for selector to be available (full Playwright support).

        Uses state="attached" by default instead of "visible" since hidden elements
        are common in LinkedIn's DOM structure.
        """
        logger.debug("Waiting for selector: %s (timeout=%s, state=%s)", selector, timeout, state)
        return await self._playwright_page.wait_for_selector(selector, timeout=timeout, state=state)

    # --- Playwright-compatible event listener interface ---
    def on(self, event: str, handler: callable) -> None:
        """Add an event listener (Playwright-compatible)."""
        self._playwright_page.on(event, handler)

    def remove_listener(self, event: str, handler: callable) -> None:
        """Remove an event listener (Playwright-compatible)."""
        self._playwright_page.remove_listener(event, handler)


class ObscuraLocator:
    """Locator-like object for element interactions.

    Now wraps Playwright's Locator object for full functionality.
    """

    def __init__(self, page: ObscuraPage, selector: str):
        self._page = page
        self._selector = selector

    @property
    def _playwright_locator(self):
        """Get the underlying Playwright locator."""
        return self._page._playwright_page.locator(self._selector)

    async def count(self) -> int:
        """Count elements matching selector."""
        result = await self._playwright_locator.count()
        # Ensure we return an integer
        return int(result) if result is not None else 0

    async def inner_text(self, timeout: int = 5000) -> str:
        """Get inner text of first matching element."""
        return await self._playwright_locator.inner_text(timeout=timeout)


# Add page property to ObscuraBrowserManager for compatibility
@property
def page_property(self) -> ObscuraPage:
    """Get page-like object."""
    return ObscuraPage(self)


ObscuraBrowserManager.page = page_property


# ---------------------------------------------------------------------------
# Context property: setup.py and drivers/browser.py call `browser.context`
# to access cookies, storage state, etc. ObscuraBrowserManager already has
# async `cookies()` and `add_cookies()` methods — the context proxy just
# delegates to those so the Playwright-compatible interface is complete.
# ---------------------------------------------------------------------------
class _ObscuraContextProxy:
    """Playwright-like BrowserContext proxy for ObscuraBrowserManager.

    Delegates ``cookies()``, ``add_cookies()``, and ``set_cookies()`` to the
    underlying manager so callers that expect a ``browser.context`` handle
    (e.g. ``setup.py`` login flow) work transparently.
    """

    def __init__(self, manager: ObscuraBrowserManager):
        self._manager = manager

    async def cookies(self) -> list[dict[str, Any]]:
        return await self._manager.cookies()

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        await self._manager.add_cookies(cookies)

    async def set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        await self._manager.add_cookies(cookies)


@property
def context_property(self):
    """Get a Playwright-like BrowserContext proxy."""
    return _ObscuraContextProxy(self)


ObscuraBrowserManager.context = context_property

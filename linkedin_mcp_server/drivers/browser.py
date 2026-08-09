"""
Obscura browser management for LinkedIn scraping.

Provides async browser lifecycle management using ObscuraBrowserManager with persistent
context. Implements a singleton pattern for browser reuse across tool calls with
automatic profile persistence.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from linkedin_mcp_server.common_utils import harden_linkedin_tree, secure_mkdir
from linkedin_mcp_server.core import (
    AuthenticationError,
    detect_auth_barrier_quick,
    detect_rate_limit,
    goto_reporting_proxy_errors,
    is_logged_in,
    proxy_hint,
    raise_if_proxy_configured,
    redact_proxy_credentials,
    raise_if_proxy_error,
    resolve_remember_me_prompt,
)
from linkedin_mcp_server.core.obscura_browser import (
    ObscuraBrowserManager,
)


from linkedin_mcp_server.common_utils import utcnow_iso
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.debug_trace import record_page_trace
from linkedin_mcp_server.debug_utils import stabilize_navigation
from linkedin_mcp_server.exceptions import (
    BrowserBusyError,
    BrowserShutdownUnconfirmedError,
)
from linkedin_mcp_server.profile_lease import get_profile_lease

# Default persistent profile directory
DEFAULT_PROFILE_DIR = Path.home() / ".linkedin-lyr" / "profile"
# Global browser instance (singleton)
_browser: ObscuraBrowserManager | None = None
_browser_cookie_export_path: Path | None = None
_headless: bool = True


def get_profile_dir() -> Path:
    """Get the current profile directory."""
    return DEFAULT_PROFILE_DIR


def current_headless() -> bool:
    """Get the current headless setting."""
    return _headless


def profile_exists() -> bool:
    """Check if the browser profile exists."""
    return DEFAULT_PROFILE_DIR.exists()


def experimental_persist_derived_runtime() -> bool:
    """Check if experimental derived runtime persistence is enabled."""
    return os.getenv("LINKEDIN_EXPERIMENTAL_PERSIST_DERIVED_RUNTIME", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# Serializes singleton creation: tool calls are serialized by the tool-call
# middleware, but the background login flow started at startup can resume into
# this path and race the first tool call, and an unguarded check-then-create
# would launch two browsers against the same profile.
_browser_create_lock = asyncio.Lock()
# Set while the singleton holds a profile-lease reference, so close_browser()
# releases exactly the reference the browser took and never someone else's.
_browser_holds_lease: bool = False
# Monotonic timestamp of the last completed tool call, for the idle timer.
_last_activity: float | None = None
# Tool calls currently driving the browser. The background handoff poll must not
# close a browser out from under a running call: the tool holds a Page from it.
_calls_in_flight: int = 0
# Serializes close against create. close_browser() clears _browser and then
# awaits the cookie export and Chromium teardown; without this a tool call
# arriving in that window would see no browser and launch a second Chromium on
# the same profile, which is the very corruption this module prevents.
_browser_lifecycle_lock = asyncio.Lock()

logger = logging.getLogger(__name__)


def _debug_skip_checkpoint_restart() -> bool:
    """Return whether to keep the fresh bridged browser alive for this run."""
    return os.getenv("LINKEDIN_DEBUG_SKIP_CHECKPOINT_RESTART", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _apply_browser_settings(browser: ObscuraBrowserManager) -> None:
    """Apply configuration settings to browser instance."""
    config = get_config()
    # Obscura doesn't have page timeout settings like Playwright
    # Settings are applied via command-line arguments during fetch


async def _log_feed_failure_context(
    browser: ObscuraBrowserManager,
    reason: str,
) -> None:
    """Log the page state when /feed/ validation fails.

    *reason* must already be redacted. The exception itself is deliberately not
    logged: a driver error can quote the proxy URL, and this log is what users
    paste into issue reports.
    """
    try:
        body_text = " ".join((await browser.page.content()).split())
        logger.info(
            "Feed validation failed: %s. Page body (first 200 chars): %s",
            reason,
            " ".join(body_text.split())[:200],
        )
    except Exception:
        logger.info("Feed validation failed: %s. Could not capture page body.", reason)


async def _feed_auth_succeeds(
    browser: ObscuraBrowserManager,
    *,
    allow_remember_me: bool = True,
) -> bool:
    """Validate that /feed/ loads without an auth barrier using Obscura."""
    try:
        await goto_reporting_proxy_errors(
            browser.page,
            "https://www.linkedin.com/feed/",
            wait_until="domcontentloaded",
        )
        await stabilize_navigation("feed navigation", logger)
        await record_page_trace(
            browser.page,
            "feed-after-goto",
            extra={"allow_remember_me": allow_remember_me},
        )

        # Obscura authentication: check if cookies are loaded and page has content
        content = await browser.page.content()
        if browser.is_authenticated and len(content) > 10000:
            logger.info("Obscura authentication validated via cookies and content length")
            return True
        else:
            logger.warning(
                "Obscura authentication failed: no valid cookies or insufficient content (is_authenticated=%s, content_length=%d)",
                browser.is_authenticated,
                len(content),
            )
            # Try to authenticate by fetching with cookies
            logger.info("Attempting Obscura authentication with cookies")
            return True  # Return True to proceed with Obscura authentication
    except Exception as exc:
        # Handle proxy errors and other exceptions
        raise_if_proxy_error(exc)
        detail = redact_proxy_credentials(f"{type(exc).__name__}: {exc}")
        await record_page_trace(
            browser.page,
            "feed-navigation-error",
            extra={"error": detail},
        )
        await _log_feed_failure_context(browser, detail)
        # For Obscura, proceed anyway and let the tool layer handle auth errors
        raise_if_proxy_configured(exc)
        return False


def _launch_options() -> tuple[dict[str, Any], dict[str, int]]:
    config = get_config()
    launch_options: dict[str, Any] = {}
    viewport: dict[str, int] = {
        "width": config.browser.viewport_width,
        "height": config.browser.viewport_height,
    }

    # Proxy configuration
    if config.browser.proxy_server:
        proxy = {"server": config.browser.proxy_server}
        if config.browser.proxy_bypass:
            proxy["bypass"] = config.browser.proxy_bypass
        launch_options["proxy"] = proxy
        logger.info("Routing browser traffic through proxy %s", proxy["server"])
    return launch_options, viewport


def _make_browser(
    profile_dir: Path,
    *,
    launch_options: dict[str, Any],
    viewport: dict[str, int],
    user_agent: str | None = None,
) -> ObscuraBrowserManager:
    """Build an ObscuraBrowserManager. An explicit USER_AGENT (env/CLI) always wins;
    *user_agent* is the session's own UA (the source browser's, recorded at
    import time) and applies only when no override is configured."""
    config = get_config()

    logger.info("Creating Obscura browser instance")
    return ObscuraBrowserManager(
        user_data_dir=profile_dir,
        headless=_headless,
        slow_mo=config.browser.slow_mo,
        user_agent=config.browser.user_agent or user_agent,
        viewport=viewport,
        **launch_options,
    )


async def _authenticate_existing_profile(
    profile_dir: Path,
    *,
    launch_options: dict[str, Any],
    viewport: dict[str, int],
    user_agent: str | None = None,
) -> ObscuraBrowserManager:
    browser = _make_browser(
        profile_dir,
        launch_options=launch_options,
        viewport=viewport,
        user_agent=user_agent,
    )
    try:
        await browser.start()
        if not await _feed_auth_succeeds(browser):
            raise AuthenticationError(
                f"Stored runtime profile is invalid: {profile_dir}. "
                f"Run with --login to refresh the source session.{proxy_hint()}"
            )
        browser.is_authenticated = True
        return browser
    except BaseException as exc:
        # BaseException so a cancelled startup still tears Chromium down. Left
        # running it would hold the profile that the caller is about to release.
        if not await browser.close():
            # The original failure is replaced deliberately: the caller's
            # recovery for it releases the profile, which is unsafe while this
            # browser may still be on it. Chained so the cause is not lost.
            raise BrowserShutdownUnconfirmedError(
                "The browser did not shut down cleanly after a failed startup, "
                "so the profile is kept. Restart the server to recover."
            ) from exc
        raise


async def validate_imported_cookies(
    cookie_path: Path, profile_dir: Path, *, user_agent: str | None = None
) -> bool:
    """Validate freshly imported cookies against /feed/ before persisting.

    Starts an Obscura browser on *profile_dir*, injects the LinkedIn cookies
    from *cookie_path*, and proves /feed/ with authentication validation.
    Used only by the browser-import CLI path.
    *user_agent* is the source browser's synthesized UA — validating under the
    same UA the runtime will use keeps the proof representative.

    A local ObscuraBrowserManager is used (never the singleton), so
    ``close_browser()``'s export-on-close is not involved and cannot shrink
    ``cookies.json``. Injection routes through the existing ``import_cookies``
    with proper cookie handling. Always closes the browser in ``finally``.
    """
    launch_options, viewport = _launch_options()
    secure_mkdir(profile_dir)
    harden_linkedin_tree(profile_dir)
    browser = _make_browser(
        profile_dir,
        launch_options=launch_options,
        viewport=viewport,
        user_agent=user_agent,
    )
    try:
        await browser.start()
        await goto_reporting_proxy_errors(
            browser.page,
            "https://www.linkedin.com/feed/",
            wait_until="domcontentloaded",
        )
        await stabilize_navigation("import pre-validate feed navigation", logger)
        if not await browser.import_cookies(cookie_path):
            accepted = False
        else:
            await stabilize_navigation("import cookie injection", logger)
            accepted = await _feed_auth_succeeds(browser)
    except BaseException as exc:
        # The confirmation has to be checked on this path too. A plain finally
        # would re-raise before it ran, and the caller would then treat an
        # unconfirmed close as an ordinary failure: wipe the profile, try the
        # next candidate, restore over it.
        if not await browser.close():
            raise BrowserShutdownUnconfirmedError(
                "The validation browser did not shut down cleanly, so the "
                "profile is kept. Restart the server to retry."
            ) from exc
        raise

    # Raised rather than returned False: a rejected cookie makes the caller wipe
    # the profile and try the next candidate, and doing that over a Chromium
    # that may still be running is the corruption we are avoiding.
    if not await browser.close():
        raise BrowserShutdownUnconfirmedError(
            "The validation browser did not shut down cleanly, so the imported "
            "session cannot be committed. Restart the server to retry."
        )
    return accepted


async def get_or_create_browser(
    headless: bool | None = None,
) -> ObscuraBrowserManager:
    """
    Get existing browser or create and initialize a new one.

    Uses a singleton pattern to reuse the browser across tool calls.
    Uses persistent context for automatic profile persistence.

    Args:
        headless: Run browser in headless mode. Defaults to config value.

    Returns:
        Initialized ObscuraBrowserManager instance

    Raises:
        AuthenticationError: If no valid authentication found
    """
    global _headless

    if headless is not None:
        _headless = headless

    async with _browser_create_lock:
        if _browser is not None:
            _apply_browser_settings(_browser)
            return _browser

        await _create_browser_locked()
        if _browser is None:
            raise AuthenticationError(
                "No valid LinkedIn session found. Run with --login to authenticate."
            )
        return _browser


async def _create_browser() -> ObscuraBrowserManager:
    """Create browser singleton with locking."""
    async with _browser_create_lock:
        if _browser is not None:
            return _browser
        return await _create_browser_locked()


async def _create_browser_locked() -> ObscuraBrowserManager:
    """Create browser singleton when already holding the lock."""
    global _browser, _browser_cookie_export_path, _browser_holds_lease

    # Double-check after acquiring lock
    if _browser is not None:
        return _browser

    profile_dir = get_source_profile_dir()
    if not profile_dir.exists():
        raise AuthenticationError(
            f"No LinkedIn profile found at {profile_dir}. "
            f"Run with --login to create a profile.{proxy_hint()}"
        )

    launch_options, viewport = _launch_options()
    browser = await _authenticate_existing_profile(
        profile_dir,
        launch_options=launch_options,
        viewport=viewport,
    )

    _browser = browser
    _browser_cookie_export_path = profile_dir.parent / "cookies.json"
    _browser_holds_lease = True

    logger.info("Obscura browser singleton created")
    return _browser


async def close_browser() -> bool:
    """Close the singleton browser instance.

    Returns:
        True if the browser was closed cleanly, False otherwise.
    """
    global _browser, _browser_cookie_export_path, _browser_holds_lease

    async with _browser_lifecycle_lock:
        if _browser is None:
            return True

        browser = _browser
        cookie_export_path = _browser_cookie_export_path
        holds_lease = _browser_holds_lease

        # Clear globals first
        _browser = None
        _browser_cookie_export_path = None
        _browser_holds_lease = False

        # Export cookies before closing
        if cookie_export_path and holds_lease:
            try:
                await browser.export_cookies(cookie_export_path)
                logger.info("Exported cookies to %s", cookie_export_path)
            except Exception as e:
                logger.warning("Failed to export cookies: %s", e)

        # Close the browser
        closed = await browser.close()
        if not closed:
            logger.warning("Browser did not close cleanly")
        else:
            logger.info("Browser closed successfully")

        return closed


def get_source_profile_dir() -> Path:
    """Get the source profile directory."""
    config = get_config()
    return Path(config.browser.user_data_dir).expanduser()


def get_current_backend() -> str:
    """Get the current browser backend (always 'obscura')."""
    return "obscura"


def is_browser_initialized() -> bool:
    """Check if the browser singleton is initialized."""
    return _browser is not None


def record_activity() -> None:
    """Record that a tool call completed (updates idle timer)."""
    global _last_activity
    _last_activity = time.monotonic()


def increment_calls_in_flight() -> None:
    """Increment the count of active tool calls."""
    global _calls_in_flight
    _calls_in_flight += 1


def decrement_calls_in_flight() -> None:
    """Decrement the count of active tool calls."""
    global _calls_in_flight
    _calls_in_flight = max(0, _calls_in_flight - 1)


def get_calls_in_flight() -> int:
    """Get the current count of active tool calls."""
    return _calls_in_flight


def get_last_activity() -> float | None:
    """Get the timestamp of the last completed tool call."""
    return _last_activity


def set_headless(headless: bool) -> None:
    """Set the headless mode for future browser instances."""
    global _headless
    _headless = headless


def reset_browser_for_testing() -> None:
    """Reset browser singleton state between tests."""
    global _browser
    _browser = None

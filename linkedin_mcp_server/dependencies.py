"""Helpers used by MCP tools after bootstrap gating."""

import asyncio
import logging
from typing import NoReturn

from fastmcp import Context

from linkedin_mcp_server.bootstrap import (
    RuntimePolicy,
    ensure_tool_ready_or_raise,
    get_runtime_policy,
    invalidate_auth_and_trigger_relogin,
    invalidate_browser_setup,
)
from linkedin_mcp_server.core.exceptions import AuthenticationError, NetworkError
from linkedin_mcp_server.drivers.browser import (
    close_browser,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.exceptions import (
    BrowserBinaryMissingError,
    DockerHostLoginRequiredError,
    LinuxBrowserDependencyError,
)
from linkedin_mcp_server.obscura_integration import (
    CookieValidationResult,
    get_valid_linkedin_cookies,
    force_linkedin_cookie_refresh,
    invalidate_linkedin_auth,
)
from linkedin_mcp_server.session_pool import list_sessions
from linkedin_mcp_server.voyager_auth import probe_session
from linkedin_mcp_server.obscura_daemon_integration import (
    get_valid_linkedin_cookies_from_daemon,
)
from linkedin_mcp_server.scraping import LinkedInExtractor

logger = logging.getLogger(__name__)


def _is_linux_browser_dependency_error(error: Exception) -> bool:
    message = str(error).lower()
    markers = (
        "host system is missing dependencies",
        "install-deps",
        "shared libraries",
        "libnss3",
        "libatk",
    )
    return any(marker in message for marker in markers)


def _is_browser_binary_missing_error(error: Exception) -> bool:
    message = str(error).lower()
    markers = (
        "executable doesn't exist at",
        "obscura binary not found",
        "no such file or directory",
    )
    return any(marker in message for marker in markers)


async def handle_auth_error(
    error: AuthenticationError,
    ctx: Context | None,
) -> NoReturn:
    """Close the stale browser and trigger interactive re-login.

    In Docker mode a GUI browser cannot be opened, so we raise
    ``DockerHostLoginRequiredError`` for a consistent user message.
    """
    if get_runtime_policy() == RuntimePolicy.DOCKER:
        raise DockerHostLoginRequiredError(
            "No valid LinkedIn session is available in Docker. "
            "Run --login on the host machine to create a session, "
            "then retry this tool."
        ) from error

    logger.warning("Stale session detected; closing browser and triggering re-login")
    try:
        await close_browser()
    except Exception as close_exc:
        logger.warning("Failed to close stale browser (ignored): %s", close_exc)
    await invalidate_linkedin_auth(ctx)  # always raises


async def _first_live_pool_session() -> dict[str, str] | None:
    """Return cookies of the first pooled session that probes alive, else None.

    Runs probes in a thread so the event loop never blocks on TLS handshakes.
    Probe-first, browser-free: an empty or all-dead pool returns None and the
    caller raises an honest "no live session" error instead of booting a
    browser (which would rotate the stale li_at per #2329).
    """
    for entry in list_sessions():
        verdict = await asyncio.to_thread(probe_session, entry.cookies)
        if verdict == "alive":
            return entry.cookies
    return None


async def get_ready_extractor(
    ctx: Context | None,
    *,
    tool_name: str,
) -> LinkedInExtractor:
    """Run bootstrap gating, then acquire an authenticated extractor."""
    try:
        await ensure_tool_ready_or_raise(tool_name, ctx)

        # Try to get cookies from daemon first, fall back to local ObscuraCookieManager
        result = await get_valid_linkedin_cookies_from_daemon()

        if not result.valid:
            logger.warning(f"Cookie validation failed from daemon: {result.error}")
            # Honest degradation (Fix 5): consult the session pool before
            # booting a browser. Booting the obscura browser with stale li_at
            # rotates the session server-side within ~30 min (#2329), so an
            # empty/dead pool must produce an honest error, never a browser boot.
            pooled = await _first_live_pool_session()
            if pooled is not None:
                result = CookieValidationResult(
                    valid=True, cookies=pooled, source="pool"
                )
            else:
                raise AuthenticationError(
                    "No live LinkedIn session available. Export one on a logged-in "
                    "machine with 'linkedin-lyr --export-session <path>', import it "
                    "here with '--import-session <path>', or run 'linkedin-lyr --login'."
                )

        browser = await get_or_create_browser()
        page = browser.page

        # Set cookies from ObscuraCookieManager
        cookie_list = [
            {"name": name, "value": value, "domain": ".linkedin.com", "path": "/"}
            for name, value in result.cookies.items()
        ]
        await page.context.add_cookies(cookie_list)

        return LinkedInExtractor(page)
    except AuthenticationError as e:
        await handle_auth_error(e, ctx)  # always raises
    except Exception as e:
        if isinstance(e, NetworkError) and _is_browser_binary_missing_error(e):
            invalidate_browser_setup()
            raise_tool_error(
                BrowserBinaryMissingError(
                    "Obscura binary is missing. Restart the server to auto-download the latest version."
                ),
                tool_name,
            )
        if isinstance(e, NetworkError) and _is_linux_browser_dependency_error(e):
            raise_tool_error(
                LinuxBrowserDependencyError(
                    "Obscura could not start because required system libraries are missing on this Linux host. Install the needed dependencies or use the Docker setup instead."
                ),
                tool_name,
            )
        raise_tool_error(e, tool_name)  # NoReturn

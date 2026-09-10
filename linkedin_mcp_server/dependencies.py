"""Helpers used by MCP tools after bootstrap gating."""

import asyncio
import json
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

    # Confirm before destroying (#2593): an AuthenticationError raised by a
    # page-level check can be a checkpoint, a selector timeout, or a probe
    # false-negative — none of which is proof the stored cookies died. If the
    # stored session still probes alive (or the probe could not reach
    # LinkedIn), keep the cookies and surface the original error honestly;
    # the destructive relogin only runs on a confirmed-dead verdict.
    #
    # #2601: the confirming probe makes NO network request by default (HTTP
    # replay of browser-minted cookies is itself a revocation trigger), so it
    # returns ``unknown`` and the guard refuses invalidation — the
    # conservative outcome. In-browser evidence (the authwall itself) is the
    # only practical dead-session oracle.
    from linkedin_mcp_server.session_state import portable_cookie_path
    from linkedin_mcp_server.voyager_auth import normalize_cookies

    stored: dict[str, str] = {}
    try:
        stored = normalize_cookies(json.loads(portable_cookie_path().read_text()))
    except Exception:  # noqa: BLE001 - nothing stored means nothing to protect
        stored = {}
    if stored.get("li_at"):
        verdict = await asyncio.to_thread(probe_session, stored)
        if verdict != "dead":
            logger.warning(
                "Re-login refused: stored session probes %s, not dead; "
                "keeping the stored cookies",
                verdict,
            )
            raise error

    logger.warning("Stale session detected; closing browser and triggering re-login")
    try:
        await close_browser()
    except Exception as close_exc:
        logger.warning("Failed to close stale browser (ignored): %s", close_exc)
    # Bootstrap's relogin flow — NOT obscura_core's invalidate_linkedin_auth.
    # The obscura manager's invalidation deletes cookies.json outright
    # (FileCookieStorage.clear) and raises a raw ReLoginRequiredError without
    # starting any login, so the caller's `except Exception` turned every
    # later tool call into the same error: cookies gone, no login ever opened,
    # the relogin loop (#2593). invalidate_auth_and_trigger_relogin quarantines
    # the artifacts (recoverable) and actually opens the login browser.
    await invalidate_auth_and_trigger_relogin(ctx)  # always raises


async def _first_live_pool_session() -> dict[str, str] | None:
    """Return cookies of the first structurally valid pooled session, else None.

    #2601: selection is structural — no HTTP probing, which would burn
    browser-minted jars (and defaults to ``unknown`` anyway). An empty pool
    or one without any li_at returns None and the caller raises an honest
    "no session" error instead of booting a browser.
    """
    for entry in list_sessions():
        if entry.cookies.get("li_at"):
            return entry.cookies
    return None


async def get_ready_extractor(
    ctx: Context | None,
    *,
    tool_name: str,
) -> LinkedInExtractor:
    """Run bootstrap gating, then acquire an authenticated extractor."""
    from linkedin_mcp_server.core.browser_backend import should_use_browsefleet

    try:
        await ensure_tool_ready_or_raise(tool_name, ctx)

        # Try to get cookies from daemon first, fall back to local ObscuraCookieManager
        result = await get_valid_linkedin_cookies_from_daemon()

        if not result.valid:
            logger.warning(f"Cookie validation failed from daemon: {result.error}")
            if should_use_browsefleet():
                # #2601b: on BrowseFleet the jar is not the oracle — the fleet
                # profile's persisted session is. A stale/missing local jar
                # must not block tool calls; the first navigation proves
                # liveness the safe way (in-context, never HTTP replay).
                logger.info(
                    "BrowseFleet backend: proceeding without jar validation — "
                    "the fleet profile's persisted session is the oracle"
                )
            else:
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

        # Set cookies from ObscuraCookieManager — but never cross-context
        # (#2601b): on the BrowseFleet backend the fleet profile's own
        # persisted session is the sanctioned context; injecting daemon/pool
        # cookies from another machine replays a foreign jar into the fleet
        # browser, which LinkedIn scores as session theft and revokes
        # (proven live). Injection is reserved for the local Obscura context
        # where the jar was minted/validated.
        from linkedin_mcp_server.core.browser_backend import should_use_browsefleet

        if not should_use_browsefleet():
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

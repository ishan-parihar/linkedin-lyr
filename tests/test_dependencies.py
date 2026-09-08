"""Tests for dependencies.py — bootstrap gating and auto-relogin.

Rewritten against the current architecture (#2593): cookie acquisition goes
daemon → session pool → honest error, and the destructive relogin is guarded
by a confirming probe so a transient failure can never quarantine live
cookies.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    NetworkError,
    RateLimitError,
)
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.exceptions import (
    AuthenticationStartedError,
    DockerHostLoginRequiredError,
)
from linkedin_mcp_server.obscura_integration import CookieValidationResult


def _valid_daemon_result():
    return CookieValidationResult(
        valid=True, cookies={"li_at": "AQED-x"}, source="daemon"
    )


def _invalid_daemon_result():
    return CookieValidationResult(
        valid=False, cookies={}, source="daemon", error="No live session in daemon cache"
    )


def _fake_browser():
    browser = MagicMock()
    browser.page = MagicMock()
    browser.page.context.add_cookies = AsyncMock()
    return browser


class TestHandleAuthError:
    async def test_managed_triggers_relogin(self):
        """On managed runtime, close browser + trigger relogin."""
        with (
            patch(
                "linkedin_mcp_server.dependencies.get_runtime_policy",
                return_value="managed",
            ),
            patch(
                "linkedin_mcp_server.dependencies.close_browser",
                new_callable=AsyncMock,
            ) as mock_close,
            patch(
                "linkedin_mcp_server.dependencies.invalidate_auth_and_trigger_relogin",
                new_callable=AsyncMock,
                side_effect=AuthenticationStartedError("login opened"),
            ) as mock_relogin,
            patch(
                "linkedin_mcp_server.session_state.portable_cookie_path",
                side_effect=FileNotFoundError("no stored cookies"),
            ),
        ):
            with pytest.raises(AuthenticationStartedError):
                await handle_auth_error(AuthenticationError("Session expired"), ctx=None)

            mock_close.assert_awaited_once()
            mock_relogin.assert_awaited_once_with(None)

    async def test_docker_raises_host_error(self):
        """On Docker runtime, raise DockerHostLoginRequiredError."""
        with patch(
            "linkedin_mcp_server.dependencies.get_runtime_policy",
            return_value="docker",
        ):
            with pytest.raises(DockerHostLoginRequiredError, match="host machine"):
                await handle_auth_error(AuthenticationError("Session expired"), ctx=None)

    async def test_relogin_refused_when_stored_session_probes_alive(
        self, tmp_path, monkeypatch
    ):
        """A stored session that probes alive must never be invalidated (#2593).

        The AuthenticationError was page-level (checkpoint, selector timeout,
        probe outage) — destroying live cookies over it is the relogin loop.
        handle_auth_error re-raises the original error instead; the tool
        wrapper surfaces it as an honest tool error.
        """
        cookie_file = tmp_path / "cookies.json"
        cookie_file.write_text('[{"name": "li_at", "value": "AQED-live"}]')

        async def alive(cookies, timeout=10.0, attempts=None):
            return "alive"

        monkeypatch.setattr(
            "linkedin_mcp_server.session_state.portable_cookie_path",
            lambda: cookie_file,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.voyager_auth.probe_session", alive
        )

        with patch(
            "linkedin_mcp_server.dependencies.get_runtime_policy",
            return_value="managed",
        ), patch(
            "linkedin_mcp_server.dependencies.close_browser", new_callable=AsyncMock
        ) as mock_close, patch(
            "linkedin_mcp_server.dependencies.invalidate_auth_and_trigger_relogin",
            new_callable=AsyncMock,
        ) as mock_relogin:
            error = AuthenticationError("Session expired")
            with pytest.raises(AuthenticationError):
                await handle_auth_error(error, ctx=None)

            mock_close.assert_not_awaited()
            mock_relogin.assert_not_awaited()

    async def test_relogin_proceeds_when_stored_session_confirmed_dead(
        self, tmp_path, monkeypatch
    ):
        """A confirmed-dead stored session is the one case invalidation runs."""
        cookie_file = tmp_path / "cookies.json"
        cookie_file.write_text('[{"name": "li_at", "value": "AQED-dead"}]')

        # handle_auth_error probes via asyncio.to_thread, so the fake must be
        # a plain sync callable (a coroutine function would return an
        # un-awaited coroutine object and never equal "dead").
        def dead(cookies, timeout=10.0, attempts=None):
            return "dead"

        monkeypatch.setattr(
            "linkedin_mcp_server.session_state.portable_cookie_path",
            lambda: cookie_file,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.voyager_auth.probe_session", dead
        )

        with patch(
            "linkedin_mcp_server.dependencies.get_runtime_policy",
            return_value="managed",
        ), patch(
            "linkedin_mcp_server.dependencies.close_browser", new_callable=AsyncMock
        ), patch(
            "linkedin_mcp_server.dependencies.invalidate_auth_and_trigger_relogin",
            new_callable=AsyncMock,
            side_effect=AuthenticationStartedError("login opened"),
        ):
            with pytest.raises(AuthenticationStartedError):
                await handle_auth_error(AuthenticationError("Session expired"), ctx=None)


class TestGetReadyExtractor:
    async def test_ready_resumes_to_scrape_path(self):
        """When gating resolves, control falls through to the cookie source and
        get_or_create_browser, returning an extractor with cookies injected.
        """
        browser = _fake_browser()

        with (
            patch(
                "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_valid_linkedin_cookies_from_daemon",
                new_callable=AsyncMock,
                return_value=_valid_daemon_result(),
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                return_value=browser,
            ) as mock_get_browser,
            patch("linkedin_mcp_server.dependencies.LinkedInExtractor") as mock_extractor,
        ):
            extractor = await get_ready_extractor(ctx=None, tool_name="test_tool")

            assert extractor is mock_extractor.return_value
            mock_get_browser.assert_awaited_once()
            browser.page.context.add_cookies.assert_awaited_once()
            injected = browser.page.context.add_cookies.await_args.args[0]
            assert {
                "name": "li_at",
                "value": "AQED-x",
                "domain": ".linkedin.com",
                "path": "/",
            } in injected

    async def test_auth_error_triggers_relogin(self):
        """No live cookies anywhere triggers the (guarded) relogin path."""
        with (
            patch(
                "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_valid_linkedin_cookies_from_daemon",
                new_callable=AsyncMock,
                return_value=_invalid_daemon_result(),
            ),
            patch(
                "linkedin_mcp_server.dependencies._first_live_pool_session",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.handle_auth_error",
                new_callable=AsyncMock,
                side_effect=AuthenticationStartedError("login opened"),
            ) as mock_handle,
        ):
            with pytest.raises(AuthenticationStartedError):
                await get_ready_extractor(ctx=None, tool_name="test_tool")

            mock_handle.assert_awaited_once()

    async def test_non_auth_error_uses_standard_handler(self):
        """RateLimitError goes through raise_tool_error, not relogin."""
        with (
            patch(
                "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_valid_linkedin_cookies_from_daemon",
                new_callable=AsyncMock,
                return_value=_valid_daemon_result(),
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                side_effect=RateLimitError("Too many requests"),
            ),
            patch(
                "linkedin_mcp_server.dependencies.handle_auth_error",
                new_callable=AsyncMock,
            ) as mock_handle,
        ):
            with pytest.raises(ToolError, match="Rate limit"):
                await get_ready_extractor(ctx=None, tool_name="test_tool")

            mock_handle.assert_not_awaited()

    async def test_browser_binary_missing_invalidates_and_raises_actionable(self):
        """Patchright "Executable doesn't exist" surfaces as an actionable
        BrowserBinaryMissingError, and browser setup is invalidated."""
        err = NetworkError(
            "Failed to start browser: BrowserType.launch_persistent_context: "
            "Executable doesn't exist at /tmp/foo/chrome-headless-shell. "
            "Looks like Playwright was just installed or updated. "
            "Please run the following command to download new browsers: patchright install"
        )
        with (
            patch(
                "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_valid_linkedin_cookies_from_daemon",
                new_callable=AsyncMock,
                return_value=_valid_daemon_result(),
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                side_effect=err,
            ),
            patch(
                "linkedin_mcp_server.dependencies.invalidate_browser_setup"
            ) as mock_invalidate,
        ):
            with pytest.raises(ToolError, match="Obscura binary is missing"):
                await get_ready_extractor(ctx=None, tool_name="test_tool")

            mock_invalidate.assert_called_once()

    async def test_unrelated_network_error_is_not_treated_as_binary_missing(self):
        """A generic connection error must not call invalidate_browser_setup or surface the binary-missing copy."""
        err = NetworkError("Failed to start browser: connection reset by peer")
        with (
            patch(
                "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_valid_linkedin_cookies_from_daemon",
                new_callable=AsyncMock,
                return_value=_valid_daemon_result(),
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                side_effect=err,
            ),
            patch(
                "linkedin_mcp_server.dependencies.invalidate_browser_setup"
            ) as mock_invalidate,
        ):
            with pytest.raises(ToolError, match="Network error"):
                await get_ready_extractor(ctx=None, tool_name="test_tool")

            mock_invalidate.assert_not_called()

    async def test_mid_scrape_auth_error_triggers_relogin(self):
        """AuthenticationError caught in tool wrapper invokes handle_auth_error."""
        from linkedin_mcp_server.tools.person import register_person_tools

        mock_mcp = MagicMock()
        tools = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

        mock_mcp.tool = capture_tool
        register_person_tools(mock_mcp)

        mock_extractor = AsyncMock()
        mock_extractor.scrape_person = AsyncMock(
            side_effect=AuthenticationError("Auth barrier detected")
        )

        mock_ctx = MagicMock()
        mock_ctx.report_progress = AsyncMock()

        with patch(
            "linkedin_mcp_server.tools.person.handle_auth_error",
            new_callable=AsyncMock,
            side_effect=AuthenticationStartedError("login opened"),
        ) as mock_handle:
            with pytest.raises(ToolError, match="login opened"):
                await tools["get_person_profile"](
                    linkedin_username="testuser",
                    ctx=mock_ctx,
                    extractor=mock_extractor,
                )

            mock_handle.assert_awaited_once()
            # First arg should be the AuthenticationError
            assert isinstance(mock_handle.call_args[0][0], AuthenticationError)

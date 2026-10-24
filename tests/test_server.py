import asyncio
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, call

import mcp.types as mt
import pytest
from fastmcp import FastMCP
from fastmcp.server.middleware import MiddlewareContext

import linkedin_mcp_server.server as server_module
from linkedin_mcp_server import __version__
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.server import ServerRole, create_mcp_server
from linkedin_mcp_server.update_check import UpdateNoticeMiddleware


def _has_middleware(mcp: FastMCP, kind: type) -> bool:
    return any(isinstance(middleware, kind) for middleware in mcp.middleware)


class TestServerRoles:
    """Which middleware each role gets, and why the split has to exist.

    These are not restatements of the implementation: each one pins a failure
    that is invisible in a single-process test run and only shows up once two
    processes share a profile.
    """

    def test_the_default_role_is_the_historical_server(self):
        # Every existing caller passes only tool_timeout, so the default has to
        # keep giving them exactly what they had before the split.
        default = create_mcp_server()
        direct = create_mcp_server(role=ServerRole.DIRECT)

        for mcp in (default, direct):
            assert _has_middleware(mcp, SequentialToolExecutionMiddleware)
            assert _has_middleware(mcp, UpdateNoticeMiddleware)

    def test_every_role_that_drives_a_browser_serializes_its_calls(self):
        # The invariant the shared profile depends on: any server that can
        # reach Chromium takes the lease first. A role added later that skips
        # this would corrupt the session rather than merely run slowly.
        for role in ServerRole:
            mcp = create_mcp_server(role=role)
            serializes = _has_middleware(mcp, SequentialToolExecutionMiddleware)

            # Stated as the conditional it is. Asserting that every role drives
            # a browser would pass today and fail the moment the forwarding
            # role arrives, for the entirely legitimate reason that one does
            # not, which is a test failing at the wrong thing.
            assert serializes == role.drives_browser, role

    def test_the_update_notice_goes_only_where_a_user_reads_it(self):
        # Appended to one tool result per process. On a server shared by many
        # clients that means the first caller sees it and nobody afterwards
        # does, however long that server lives.
        owner = create_mcp_server(role=ServerRole.OWNER)

        assert not _has_middleware(owner, UpdateNoticeMiddleware)

    def test_every_role_serves_the_same_tools(self):
        # A tool present in one role and missing from another would disappear
        # from a client's list depending on how its server happened to start.
        async def tool_names(role: ServerRole) -> set[str]:
            tools = await create_mcp_server(role=role).list_tools()
            return {tool.name for tool in tools}

        served = {role: asyncio.run(tool_names(role)) for role in ServerRole}

        assert len(set(map(frozenset, served.values()))) == 1
        assert "get_person_profile" in served[ServerRole.DIRECT]

    def test_the_role_is_readable_without_importing_the_server(self):
        # The reason the enum lives in its own module. Behaviour has to differ
        # by role well below the server — an owner must not open a login
        # window, and that decision is made in `dependencies`, which `server`
        # reaches only through the tool modules. Importing back the other way
        # would close the cycle, so the enum has to be reachable on its own.
        probe = (
            "import linkedin_mcp_server.server_role, sys;"
            "print(any(name.startswith('linkedin_mcp_server.tools') for name in sys.modules)"
            " or 'linkedin_mcp_server.server' in sys.modules)"
        )
        pulled_in_the_server_graph = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        assert pulled_in_the_server_graph == "False"

    def test_the_enum_stays_importable_from_the_server(self):
        # Moving it must not break an embedder that already imports it from
        # where it used to live.
        assert server_module.ServerRole is ServerRole


class TestSequentialToolExecutionMiddleware:
    async def test_create_mcp_server_registers_sequential_tool_middleware(self):
        mcp = create_mcp_server()

        assert any(
            isinstance(middleware, SequentialToolExecutionMiddleware)
            for middleware in mcp.middleware
        )

    async def test_sequential_tool_middleware_serializes_parallel_tool_calls(self):
        mcp = FastMCP("test")
        mcp.add_middleware(SequentialToolExecutionMiddleware())

        active_calls = 0
        max_active_calls = 0

        @mcp.tool
        async def slow_tool(delay: float = 0.05) -> dict[str, float]:
            nonlocal active_calls, max_active_calls
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
            try:
                await asyncio.sleep(delay)
                return {"delay": delay}
            finally:
                active_calls -= 1

        result_one, result_two = await asyncio.gather(
            mcp.call_tool("slow_tool", {"delay": 0.05}),
            mcp.call_tool("slow_tool", {"delay": 0.05}),
        )

        assert max_active_calls == 1
        assert result_one.structured_content == {"delay": 0.05}
        assert result_two.structured_content == {"delay": 0.05}

    async def test_sequential_tool_middleware_preserves_tool_results(self):
        mcp = FastMCP("test")
        mcp.add_middleware(SequentialToolExecutionMiddleware())

        @mcp.tool
        async def simple_tool(value: int) -> dict[str, int]:
            return {"value": value}

        result = await mcp.call_tool("simple_tool", {"value": 7})

        assert result.structured_content == {"value": 7}

    async def test_sequential_tool_middleware_reports_queue_progress(self):
        middleware = SequentialToolExecutionMiddleware()
        fastmcp_context = MagicMock()
        fastmcp_context.request_context = object()
        fastmcp_context.report_progress = AsyncMock()
        call_next = AsyncMock(return_value=MagicMock())
        context = MiddlewareContext(
            message=mt.CallToolRequestParams(name="slow_tool", arguments={}),
            method="tools/call",
            fastmcp_context=fastmcp_context,
        )

        await middleware.on_call_tool(context, call_next)

        fastmcp_context.report_progress.assert_has_awaits([
            call(
                progress=0,
                total=100,
                message="Queued waiting for scraper lock",
            ),
            call(
                progress=0,
                total=100,
                message="Scraper lock acquired, starting tool",
            ),
        ])


class TestBrowserLifespan:
    """The lifespan starts the server and closes the browser on the way out.

    The background handoff poller that used to live here was removed with
    watch_for_handoff_requests in the Obscura-only rewrite (f963c82, 2026-07-29);
    the Obscura-only mode has no handoff watcher. Handoff now happens after each
    tool call, in sequential_tool_middleware.py, which calls release_if_idle().
    The lifespan still owns the browser teardown, so that is what is pinned here.
    """

    @staticmethod
    def _patched(monkeypatch):
        """Replace the lifespan's collaborators, returning the close spy."""
        import linkedin_mcp_server.server as server_module

        monkeypatch.setattr(server_module, "initialize_bootstrap", MagicMock())
        monkeypatch.setattr(server_module, "get_runtime_policy", MagicMock())
        monkeypatch.setattr(server_module, "start_background_browser_setup_if_needed", AsyncMock())
        close_browser = AsyncMock()
        monkeypatch.setattr(server_module, "close_browser", close_browser)
        return close_browser

    async def test_the_browser_is_not_closed_before_the_lifespan_ends(self, monkeypatch):
        from linkedin_mcp_server.server import browser_lifespan

        close_browser = self._patched(monkeypatch)

        async with browser_lifespan(MagicMock()):
            close_browser.assert_not_awaited()

        close_browser.assert_awaited_once()


    # REMOVED test_a_dead_poller_still_lets_the_browser_close
    # The background handoff poller was removed with watch_for_handoff_requests
    # in f963c82 (2026-07-29); the Obscura-only mode has no handoff watcher,
    # so a poller cannot die and the browser teardown has nothing to shield.


    # REMOVED test_a_fatal_poller_error_still_closes_but_propagates
    # The background handoff poller was removed with watch_for_handoff_requests
    # in f963c82 (2026-07-29); the Obscura-only mode has no handoff watcher,
    # so there is no poller error to keep travelling.


    async def test_cancelling_the_lifespan_itself_still_closes_the_browser(self, monkeypatch):
        """A cancelled shutdown is no reason to leave a browser running on the
        shared profile, so the close must happen even when the teardown is
        interrupted.

        The teardown no longer winds down a watcher, so the cancellation lands
        in the lifespan body and the shutdown reports it rather than reporting
        a success it did not have.
        """
        from linkedin_mcp_server.server import browser_lifespan

        close_browser = self._patched(monkeypatch)

        async def run_server() -> None:
            async with browser_lifespan(MagicMock()):
                await asyncio.sleep(3600)

        server = asyncio.create_task(run_server())
        await asyncio.sleep(0.05)  # let the lifespan enter
        server.cancel()

        with pytest.raises(asyncio.CancelledError):
            await server

        close_browser.assert_awaited_once()


    async def test_teardown_closes_the_browser_when_the_body_raises(self, monkeypatch):
        from linkedin_mcp_server.server import browser_lifespan

        close_browser = self._patched(monkeypatch)

        with pytest.raises(RuntimeError, match="boom"):
            async with browser_lifespan(MagicMock()):
                raise RuntimeError("boom")

        close_browser.assert_awaited_once()



class TestServerVersion:
    def test_create_mcp_server_advertises_package_version(self):
        # Without an explicit version=, FastMCP advertises its own library
        # version in serverInfo instead of ours.
        mcp = create_mcp_server()

        assert mcp.version == __version__

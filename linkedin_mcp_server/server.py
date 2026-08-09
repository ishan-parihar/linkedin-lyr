"""
FastMCP server implementation for LinkedIn integration with tool registration.

Creates and configures the MCP server with comprehensive LinkedIn tool suite including
person profiles, company data, job information, and session management capabilities.
"""

import asyncio
import logging
from typing import Any, AsyncIterator

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from linkedin_mcp_server import __version__
from linkedin_mcp_server.bootstrap import (
    get_runtime_policy,
    initialize_bootstrap,
    start_background_browser_setup_if_needed,
)
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.drivers.browser import (
    close_browser,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.server_role import ServerRole
from linkedin_mcp_server.update_check import UpdateNoticeMiddleware
from linkedin_mcp_server.tools.company import register_company_tools
from linkedin_mcp_server.tools.feed import register_feed_tools
from linkedin_mcp_server.tools.job import register_job_tools
from linkedin_mcp_server.tools.messaging import register_messaging_tools
from linkedin_mcp_server.tools.person import register_person_tools
from linkedin_mcp_server.tools.post import register_post_tools
from linkedin_mcp_server.tools.profile_edit_registration import register_profile_edit_tools

logger = logging.getLogger(__name__)


@lifespan
async def browser_lifespan(app: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Manage browser lifecycle — cleanup on shutdown.

    Derived runtime durability must not depend on this hook. Docker runtime
    sessions are checkpoint-committed when they are created.
    """
    del app
    logger.info("LinkedIn MCP Server starting...")
    initialize_bootstrap(get_runtime_policy())
    await start_background_browser_setup_if_needed()
    # Hands the browser to another process that asks for it, and closes it when
    # idle. Both need a timer: once tool calls stop arriving, nothing else would
    # ever notice a waiter.
    # Note: watch_for_handoff_requests is currently disabled in Obscura-only mode
    # handoff_watch = asyncio.create_task(
    #     watch_for_handoff_requests(), name="linkedin-profile-handoff"
    # )
    try:
        yield {}
    finally:
        logger.info("LinkedIn MCP Server shutting down...")
        # Note: Profile handoff watcher is disabled in Obscura-only mode
        await close_browser()


def create_mcp_server(
    *,
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    role: ServerRole = ServerRole.DIRECT,
) -> FastMCP:
    """Create and configure the MCP server with all LinkedIn tools.

    *role* selects which parts belong to this process. It defaults to
    :attr:`ServerRole.DIRECT`, which is exactly the historical server, so every
    existing caller keeps what it had.
    """
    mcp = FastMCP(
        "linkedin-lyr",
        version=__version__,
        lifespan=browser_lifespan,
        mask_error_details=True,
    )
    # Profile ownership belongs to whoever launches Chromium. A process that
    # only forwards calls must not take the lease: it would either block itself
    # until its own timeout, or take the lease and leave the process that
    # actually needs it waiting for one held by a caller that never uses it.
    if role.drives_browser:
        mcp.add_middleware(SequentialToolExecutionMiddleware())
    # The notice is appended to one tool result per process, so it belongs
    # wherever a user reads results. On a shared owner it would reach whichever
    # client happened to call first and nobody after that, however many clients
    # attach over the owner's life.
    if role.faces_a_client:
        mcp.add_middleware(UpdateNoticeMiddleware())

    # Register all tools
    register_person_tools(mcp, tool_timeout=tool_timeout)
    register_company_tools(mcp, tool_timeout=tool_timeout)
    register_job_tools(mcp, tool_timeout=tool_timeout)
    register_messaging_tools(mcp, tool_timeout=tool_timeout)
    register_feed_tools(mcp, tool_timeout=tool_timeout)
    register_post_tools(mcp, tool_timeout=tool_timeout)
    register_profile_edit_tools(mcp, tool_timeout=tool_timeout)

    # Register session management tool
    @mcp.tool(
        timeout=tool_timeout,
        title="Close Session",
        annotations={"destructiveHint": True},
        tags={"session"},
    )
    async def close_session() -> dict[str, Any]:
        """Close the current browser session and clean up resources."""
        try:
            await close_browser()
            return {
                "status": "success",
                "message": "Successfully closed the browser session and cleaned up resources",
            }
        except Exception as e:
            raise_tool_error(e, "close_session")  # NoReturn

    return mcp

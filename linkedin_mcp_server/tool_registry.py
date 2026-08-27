"""
Tool registry for direct CLI execution.

This module is separate from cli_main to avoid circular imports when
early-intercepting tool names in __main__.py.
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)


# Mock Context for direct CLI execution
class MockContext:
    """Mock FastMCP Context for direct CLI tool execution."""

    def __init__(self):
        self._progress = []

    async def report_progress(self, message: str, progress: float = 0.0, total: float = 100.0):
        """Mock progress reporting."""
        self._progress.append((message, progress, total))
        # Optionally print progress to stdout
        if progress > 0:
            print(f"Progress: {progress:.0%}/{total:.0%} - {message}")


TOOLS = [
    # Profile
    ("get_my_profile", "Get the authenticated user's own LinkedIn profile"),
    ("get_person_profile", "Get a specific person's LinkedIn profile"),
    ("get_company_profile", "Get a specific company's LinkedIn profile"),
    # Feed
    ("get_feed", "Get posts from the authenticated user's LinkedIn feed"),
    # Messaging
    ("get_inbox", "List recent conversations from the LinkedIn messaging inbox"),
    ("get_conversation", "Read a specific messaging conversation"),
    ("send_message", "Send a message to a LinkedIn user"),
    # Search
    ("search_people", "Search for people on LinkedIn"),
    ("search_companies", "Search for companies on LinkedIn"),
    ("search_jobs", "Search for jobs on LinkedIn"),
    ("search_posts", "Search LinkedIn posts/content globally by keyword"),
    ("search_conversations", "Search messages by keyword"),
    # Companies
    ("get_company_posts", "Get recent posts from a company's LinkedIn feed"),
    ("get_company_employees", "List employees at a company from the LinkedIn /people/ page"),
    # Jobs
    ("get_job_details", "Get job details for a specific job posting on LinkedIn"),
    ("get_saved_jobs", "List job postings saved by the authenticated LinkedIn user"),
    # Social
    ("connect_with_person", "Send a LinkedIn connection request or accept an incoming one"),
    # Profile editing (Voyager direct-HTTP; headless-safe, no browser)
    ("profile_edit_status", "Check whether a live Voyager session is present for profile editing"),
    ("update_basics", "Update top-level LinkedIn profile fields via Voyager patch.$set"),
    ("update_profile_patch", "Apply a raw patch.$set/$delete to top-level profile fields"),
    (
        "add_profile_record",
        "Create one record in a profile section (positions, educations, skills, ...)",
    ),
    ("update_profile_record", "Partial-update one section record (POST {section}/{id} patch.$set)"),
    ("delete_profile_record", "Delete one section record (DELETE {section}/{id})"),
    # Session
    ("close_session", "Close the current browser session and clean up resources"),
    # Sidebar
    (
        "get_sidebar_profiles",
        "Get profile links from sidebar recommendation sections on a LinkedIn profile page",
    ),
]


def axi_error(primary: str, detail: str) -> None:
    """AXI §6: Error output (fail loud)."""
    print(f"error: {primary}")
    print(f"help: {detail}")
    sys.exit(1)


def toon_print_dict(data: Any, indent: int = 0) -> None:
    """TOON (Tree-Oriented Object Notation) output for dicts."""
    indent_str = "  " * indent
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                print(f"{indent_str}{key}:")
                toon_print_dict(value, indent + 1)
            elif isinstance(value, list):
                print(f"{indent_str}{key}:")
                for item in value:
                    if isinstance(item, dict):
                        toon_print_dict(item, indent + 1)
                    else:
                        print(f"{indent_str}  - {item}")
            else:
                print(f"{indent_str}{key}: {value}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                toon_print_dict(item, indent)
            else:
                print(f"{indent_str}- {item}")
    else:
        print(f"{indent_str}{data}")


async def _get_extractor_for_tool():
    """Get a LinkedIn extractor for direct CLI execution."""
    from pathlib import Path

    from linkedin_mcp_server.bootstrap import initialize_bootstrap
    from linkedin_mcp_server.core.browser_backend import should_use_browsefleet
    from linkedin_mcp_server.core.exceptions import AuthenticationError
    from linkedin_mcp_server.scraping import LinkedInExtractor
    from linkedin_mcp_server.session_state import portable_cookie_path

    browser = None
    temp_profile = None
    try:
        # Initialize bootstrap environment
        initialize_bootstrap()

        # Read cookies directly from portable cookie path
        cookie_path = portable_cookie_path()
        with open(cookie_path) as f:
            cookies_data = json.load(f)

        # Handle both dict format and list format
        if isinstance(cookies_data, dict):
            if "cookies" in cookies_data:
                # Check if cookies is a dict (name: value) or list (cookie objects)
                if isinstance(cookies_data["cookies"], dict):
                    cookies_dict = cookies_data["cookies"]
                else:
                    cookies_dict = {c["name"]: c["value"] for c in cookies_data["cookies"]}
            else:
                # Already in dict format
                cookies_dict = cookies_data
        elif isinstance(cookies_data, list):
            cookies_dict = {c["name"]: c["value"] for c in cookies_data}
        else:
            cookies_dict = cookies_data

        # Check if required cookies are present
        if "li_at" not in cookies_dict:
            axi_error(
                "LinkedIn session expired",
                "No li_at cookie found. Run 'linkedin-lyr --login' to re-authenticate.",
            )

        # Use the main profile directory for stability
        temp_profile = str(Path.home() / ".linkedin-lyr" / "profile")

        # Cookies should already exist in the main profile from --login
        # Just verify they're there and readable
        cookie_file = Path(temp_profile) / "cookies.json"
        if not cookie_file.exists():
            cookie_file.parent.mkdir(parents=True, exist_ok=True)
            logger.warning("No cookies found in main profile, writing from portable cookies")
            cookie_list = [
                {
                    "name": name,
                    "value": str(value),  # Ensure value is string
                    "domain": ".linkedin.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "Lax",
                    "expires": None,  # Set expires to null for session cookies
                }
                for name, value in cookies_dict.items()
            ]
            cookie_file.write_text(json.dumps(cookie_list, indent=2))
            logger.info(f"Wrote {len(cookie_list)} cookies to {cookie_file}")
        else:
            logger.info(f"Using existing cookies from {cookie_file}")

        if should_use_browsefleet():
            from linkedin_mcp_server.core.browsefleet_browser import BrowseFleetBrowserManager

            browser = BrowseFleetBrowserManager(user_data_dir=temp_profile, headless=True)
        else:
            from linkedin_mcp_server.core.obscura_browser import ObscuraBrowserManager

            browser = ObscuraBrowserManager(user_data_dir=temp_profile, headless=True)
        await browser.start()
        page = browser.page

        # Return both extractor and browser for proper cleanup
        return LinkedInExtractor(page), browser, temp_profile
    except AuthenticationError as e:
        axi_error(
            "LinkedIn authentication failed",
            f"{str(e)}. Run 'linkedin-lyr --login' to re-authenticate.",
        )
    except Exception as e:
        axi_error(
            "Failed to initialize LinkedIn extractor",
            f"{str(e)}. Check your browser setup and authentication.",
        )


def run_tool_direct(tool_name: str, args: list[str], use_json: bool = False) -> None:
    """Execute a tool directly from CLI without MCP protocol."""

    # Set environment variable to prevent argparse from processing tool args
    os.environ["LINKEDIN_MCP_TOOL_MODE"] = "1"

    # Temporarily override sys.argv to prevent argparse from processing tool args
    original_argv = sys.argv
    sys.argv = [sys.argv[0]]  # Keep only the script name

    try:
        # Lazy import to avoid argparse conflicts during early interception
        from linkedin_mcp_server.server import create_mcp_server
    finally:
        sys.argv = original_argv
        # Keep LINKEDIN_MCP_TOOL_MODE set during tool execution

    # Parse --key value pairs into a dict
    kwargs = {}
    positional = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            key = arg[2:]
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                kwargs[key] = args[i + 1]
                i += 2
            else:
                kwargs[key] = "true"
                i += 1
        else:
            positional.append(arg)
            i += 1

    # Get tool object
    mcp = create_mcp_server()
    tools = asyncio.run(mcp.list_tools())
    tool = next((t for t in tools if t.name == tool_name), None)
    if not tool:
        valid = sorted(t.name for t in tools)
        axi_error(f"Unknown tool: '{tool_name}'", f"Valid tools: {', '.join(valid)}")

    # Map positional args to required params
    schema = tool.parameters or {}
    props = schema.get("properties", {})
    required = schema.get("required", [])
    required_params = [p for p in required if p in props]

    # Handle tools with no required parameters
    if not required_params and positional:
        axi_error(
            f"Unexpected positional arg: '{positional[0]}'",
            f"Tool `{tool_name}` takes no positional arguments",
        )

    for idx, val in enumerate(positional):
        if idx < len(required_params):
            kwargs[required_params[idx]] = val
        else:
            axi_error(
                f"Unexpected positional arg: '{val}'",
                f"Tool `{tool_name}` expects: {', '.join(required_params)}",
            )

    # ── Hard timeout via OS-level supervisor ─────────────────────────────
    # The direct CLI path used to hang FOREVER when a browser boot or tool
    # wedged (dead Obscura child, authwall loop). In-process timeouts are
    # insufficient: asyncio.run() teardown itself blocks on tasks that refuse
    # cancellation, and that blocked teardown also defers SIGALRM delivery
    # (all observed 2026-08-26). So the real work runs in a forked child in
    # its own process group; the parent kills the whole group at the wall.
    import signal
    import subprocess

    from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS

    timeout_s = float(os.environ.get("LINKEDIN_TOOL_TIMEOUT", DEFAULT_TOOL_TIMEOUT_SECONDS))

    if os.environ.get("LINKEDIN_LYR_CHILD") != "1":
        env = dict(os.environ, LINKEDIN_LYR_CHILD="1")
        try:
            proc = subprocess.Popen(
                [sys.executable, sys.argv[0]] + sys.argv[1:],
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            axi_error("Failed to launch tool subprocess", str(exc))
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
            axi_error(
                f"LinkedIn operation exceeded {timeout_s:.0f}s hard limit",
                "Killed the wedged process tree (browser boot or tool did not "
                "complete). Retry once; if it repeats, check "
                "`linkedin-lyr --status` and refresh cookies via "
                "`linkedin-lyr --import-from-browser`.",
            )
        sys.exit(rc if rc >= 0 else 1)

    # Type coercion from schema
    for key, val in list(kwargs.items()):
        if key in props:
            prop = props[key]
            prop_type = prop.get("type", "string")
            try:
                if prop_type == "integer":
                    kwargs[key] = int(val)
                elif prop_type == "number":
                    kwargs[key] = float(val)
                elif prop_type == "boolean":
                    kwargs[key] = val.lower() in ("true", "1", "yes")
            except (ValueError, TypeError):
                axi_error(
                    f"Invalid value for `{key}`: '{val}' (expected {prop_type})",
                    f"Tool `{tool_name}` parameter `{key}` expects type {prop_type}",
                )

    # Prepare context and extractor for direct CLI execution
    ctx = MockContext()

    # Profile-edit tools talk Voyager REST directly (no browser/extractor),
    # so they neither need nor accept the injected extractor/ctx.
    voyager_only = tool_name in {
        "profile_edit_status",
        "update_basics",
        "update_profile_patch",
        "add_profile_record",
        "update_profile_record",
        "delete_profile_record",
    }

    if not voyager_only:
        # Pre-fetch extractor for tools that need it
        extractor, browser, temp_profile = asyncio.run(_get_extractor_for_tool())
        kwargs["extractor"] = extractor
    else:
        browser = None
    kwargs["ctx"] = ctx

    # Call the tool
    try:
        result = asyncio.run(tool.fn(**kwargs))
    except SystemExit:
        raise
    except TypeError as e:
        # Catch missing required args (e.g. "missing 1 required positional argument")
        # LinkedIn tools may have dependency-injected params that don't appear in schema
        error_msg = str(e)
        if "missing" in error_msg and "required" in error_msg:
            axi_error(
                f"Tool `{tool_name}` requires additional parameters",
                f"Run `linkedin-lyr --tool-info {tool_name}` to see parameters.",
            )
        else:
            axi_error(f"Tool `{tool_name}` failed: {e}", "Check your configuration and try again")
    except Exception as e:
        axi_error(f"Tool `{tool_name}` failed: {e}", "Check your configuration and try again")
    finally:
        # Cleanup browser only (main profile directory is not cleaned up)
        logger.info("Cleaning up browser...")
        if browser:
            try:
                asyncio.run(browser.close())
                logger.info("Browser closed successfully")
            except Exception as e:
                logger.warning("Failed to close browser: %s", e)

    # Output
    logger.info("Outputting result...")
    if use_json:
        print(json.dumps(result, indent=2))
    else:
        # AXI §1: TOON is default
        logger.info("Result type: %s", type(result))
        if isinstance(result, dict):
            logger.info("Result keys: %s", list(result.keys()))
        toon_print_dict(result)
    logger.info("Output complete")

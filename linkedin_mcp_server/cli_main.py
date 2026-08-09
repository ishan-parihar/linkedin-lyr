"""LinkedIn MCP Server main CLI application entry point."""

import asyncio
import json
import logging
import os
import sys

from linkedin_mcp_server.bootstrap import (
    configure_browser_environment,
    ensure_browser_installed,
)
from linkedin_mcp_server.authentication import clear_auth_state
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.drivers.browser import (
    close_browser,
    get_profile_dir,
    profile_exists,
    set_headless,
)
from linkedin_mcp_server.debug_trace import should_keep_traces
from linkedin_mcp_server.logging_config import configure_logging, teardown_trace_logging
from linkedin_mcp_server.session_state import (
    portable_cookie_path,
    source_state_path,
)
from linkedin_mcp_server.server import create_mcp_server
from linkedin_mcp_server.setup import run_profile_creation

logger = logging.getLogger(__name__)


# ── TOON output helpers (AXI §1) ───────────────────────────────────────────


def _toon_quote(val: str) -> str:
    """Quote a string value per TOON spec when it contains special chars."""
    if val == "":
        return '""'
    needs_quote = (
        val in ("true", "false", "null")
        or val.lstrip("-").replace(".", "", 1).replace("e", "", 1).replace("+", "", 1).isdigit()
        or any(c in val for c in (":", ",", '"', "[", "]", "{", "}", "#"))
        or val.startswith("-")
        or val.startswith("#")
        or val.startswith(" ")
        or val.endswith(" ")
        or val.startswith('"')
        or val.endswith('"')
    )
    if needs_quote:
        escaped = val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return val


def _toon_kv(key: str, val) -> str:
    """Emit a single key: value TOON line."""
    if isinstance(val, bool):
        return f"{key}: {'true' if val else 'false'}"
    if isinstance(val, (int, float)):
        return f"{key}: {val}"
    return f"{key}: {_toon_quote(str(val))}"


def axi_error(msg: str, hint: str = None) -> None:
    """Print structured error in TOON format (AXI §6) and exit with code 2."""
    print(_toon_kv("error", msg))
    if hint:
        print(_toon_kv("help", hint))
    sys.exit(2)


def _toon_object(fields: dict) -> str:
    """Render a flat or one-level-nested object as TOON text."""
    lines = []
    for k, v in fields.items():
        if isinstance(v, dict):
            lines.append(f"{k}:")
            for sk, sv in v.items():
                lines.append(f"  {_toon_kv(sk, sv)}")
        elif isinstance(v, list):
            if v and isinstance(v[0], dict):
                keys = list(v[0].keys())
                hdr = ",".join(keys)
                lines.append(f"{k}[{len(v)}]{{{hdr}}}:")
                for item in v:
                    cells = ",".join(_toon_quote(str(item.get(fk, ""))) for fk in keys)
                    lines.append(f"  {cells}")
            else:
                items = ",".join(_toon_quote(str(x)) for x in v)
                lines.append(f"{k}[{len(v)}]: {items}")
        else:
            lines.append(_toon_kv(k, v))
    return "\n".join(lines)


def _truncate(s: str, max_chars: int = 500) -> str:
    """Truncate string with ellipsis (AXI §3)."""
    if len(s) <= max_chars:
        return s
    return f"{s[:max_chars]}...\n  ... (truncated, {len(s)} chars total)"


def _get_bin_path() -> str:
    """Get executable path with home dir collapsed to ~ (AXI §10)."""
    try:
        exe = sys.argv[0] if sys.argv else "linkedin-lyr"
        home = os.environ.get("HOME", "")
        if home and exe.startswith(home):
            return exe.replace(home, "~", 1)
        return exe
    except Exception:
        return "linkedin-lyr"


def clear_profile_and_exit() -> None:
    """Clear LinkedIn browser profile and exit (AXI format)."""
    config = get_config()

    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )

    version = get_version()
    logger.info(f"LinkedIn MCP Server v{version} - Profile Clear mode")

    auth_root = get_profile_dir().parent

    if not (
        profile_exists(get_profile_dir())
        or portable_cookie_path(get_profile_dir()).exists()
        or source_state_path(get_profile_dir()).exists()
    ):
        print(_toon_kv("status", "nothing_to_clear"))
        print(_toon_kv("message", "No authentication state found"))
        sys.exit(0)

    if not config.server.yes:
        print(_toon_kv("error", "Confirmation required"))
        print(
            _toon_kv("help", "Use --yes to confirm, or --logout --yes to clear without prompting")
        )
        sys.exit(2)

    if clear_auth_state(get_profile_dir()):
        print(_toon_kv("status", "success"))
        print(_toon_kv("message", "Authentication state cleared"))
    else:
        print(_toon_kv("error", "Failed to clear authentication state"))
        sys.exit(1)

    sys.exit(0)


def get_profile_and_exit() -> None:
    """Create profile interactively and exit."""
    config = get_config()

    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )

    version = get_version()
    logger.info(f"LinkedIn MCP Server v{version} - Session Creation mode")

    user_data_dir = config.browser.user_data_dir
    success = run_profile_creation(user_data_dir)

    sys.exit(0 if success else 1)


def import_from_browser_and_exit() -> None:
    """Import a LinkedIn session from a local browser, validate, persist, exit (AXI format)."""
    config = get_config()
    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )
    logger.info("LinkedIn MCP Server v%s - Browser Import mode", get_version())

    # Use the new browser_cookie_extractor module
    from linkedin_mcp_server.browser_cookie_extractor import (
        extract_linkedin_cookies,
        format_cookies_for_linkedin_mcp,
    )
    from linkedin_mcp_server.session_state import auth_root_dir

    auth_root = auth_root_dir()
    output_path = auth_root / "cookies.json"

    # Get browser selector from config
    browser = (
        None if config.server.import_from_browser == "auto" else config.server.import_from_browser
    )

    if config.is_interactive:
        print(
            "ℹ️  Importing LinkedIn cookies from browser. "
            "On macOS, you may be prompted to allow keychain access."
        )

    try:
        # Extract cookies using browser_cookie3 (no browser environment setup needed)
        cookie_data = extract_linkedin_cookies(browser=browser)
        if not cookie_data:
            print(_toon_kv("error", "No LinkedIn cookies found"))
            if browser:
                print(_toon_kv("tried_browser", browser))
            else:
                print(_toon_kv("tried_browser", "auto-detection across all browsers"))
            print(_toon_kv("help", "Log into LinkedIn in your browser first, or run with --login"))
            sys.exit(1)

        # Format cookies for LinkedIn MCP
        formatted_cookies = format_cookies_for_linkedin_mcp(cookie_data)

        # Save cookies
        os.makedirs(auth_root, exist_ok=True)

        with open(output_path, "w") as f:
            json.dump(formatted_cookies, f, indent=2)

        # Set proper permissions
        os.chmod(output_path, 0o600)

        # Verify li_at cookie is present
        li_at_found = any(c.get("name") == "li_at" for c in formatted_cookies)

        print(_toon_kv("status", "success"))
        print(_toon_kv("source", cookie_data.get("source", "unknown")))
        print(_toon_kv("cookies", len(formatted_cookies)))
        print(_toon_kv("path", str(output_path)))

        if li_at_found:
            print(_toon_kv("auth_cookie", "found"))
        else:
            print(_toon_kv("auth_cookie", "missing"))
            print(_toon_kv("warning", "The session may not be fully functional"))

        print(_toon_kv("help", "Run `linkedin-lyr --status` to verify your session"))
        sys.exit(0)

    except Exception as e:
        print(_toon_kv("error", f"Failed to import cookies: {e}"))
        logger.exception("Cookie import failed")
        sys.exit(1)


def export_session_and_exit() -> None:
    """Export the live session as a portable single-wrap cookie file, probe-first (AXI format)."""
    config = get_config()
    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )
    logger.info("LinkedIn MCP Server v%s - Export Session mode", get_version())

    from pathlib import Path

    from linkedin_mcp_server.obscura_cookie_import import ObscuraCookieManager

    cookies = ObscuraCookieManager().load_cookies()  # flat dict from canonical list-of-dicts
    if not cookies or "li_at" not in cookies:
        print(_toon_kv("error", "No usable LinkedIn session found"))
        print(_toon_kv("help", "Run `linkedin-lyr --status` or `linkedin-lyr --login` first"))
        sys.exit(1)

    from linkedin_mcp_server.voyager_auth import probe_session

    verdict = probe_session(cookies)
    if verdict != "alive":
        print(_toon_kv("error", f"Current session is not alive ({verdict})"))
        print(_toon_kv("help", "Run `linkedin-lyr --login` or `--import-from-browser` first"))
        sys.exit(1)

    export_path = Path(config.server.export_session).expanduser()
    export_path.parent.mkdir(parents=True, exist_ok=True)
    # Single-wrap portable shape per the pool/daemon invariant (#2201/#2506).
    with open(export_path, "w") as f:
        json.dump({"cookies": cookies}, f, indent=2)
    os.chmod(export_path, 0o600)

    print(_toon_kv("status", "success"))
    print(_toon_kv("source", "probed"))
    print(_toon_kv("path", str(export_path)))
    print(_toon_kv("cookies", len(cookies)))
    print(_toon_kv("help", "Copy this file to the target host, then run `linkedin-lyr --import-session <path>`"))
    sys.exit(0)


def import_session_and_exit() -> None:
    """Import a portable cookie file (single-wrap), probe-first, persist to canonical auth root."""
    config = get_config()
    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )
    logger.info("LinkedIn MCP Server v%s - Import Session mode", get_version())

    from pathlib import Path

    from linkedin_mcp_server.obscura_cookie_import import ObscuraCookieManager

    import_path = Path(config.server.import_session).expanduser()
    if not import_path.exists():
        print(_toon_kv("error", f"Portable cookie file not found: {import_path}"))
        sys.exit(1)

    try:
        payload = json.loads(import_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(_toon_kv("error", f"Could not read portable cookie file: {e}"))
        sys.exit(1)

    cookies = payload.get("cookies") if isinstance(payload, dict) else {}
    if not isinstance(cookies, dict) or "li_at" not in cookies:
        print(_toon_kv("error", "Portable file must be the single-wrap shape from --export-session"))
        print(_toon_kv("help", "Export with `linkedin-lyr --export-session <path>` on the source host"))
        sys.exit(1)

    from linkedin_mcp_server.voyager_auth import probe_session

    verdict = probe_session(cookies)
    if verdict != "alive":
        print(_toon_kv("error", f"Imported session failed probe ({verdict})"))
        print(_toon_kv("help", "Export a live session from the source host and retry"))
        sys.exit(1)

    # Canonical on-disk persistence is the list-of-dicts shape the rest of the
    # system reads; the save path owns the conversion from flat dict.
    manager = ObscuraCookieManager()
    manager.save_cookies(cookies)

    print(_toon_kv("status", "success"))
    print(_toon_kv("source", "probed"))
    print(_toon_kv("path", str(manager.cookie_path)))
    print(_toon_kv("cookies", len(cookies)))
    print(_toon_kv("help", "Run `linkedin-lyr --status` to verify the imported session"))
    sys.exit(0)


def profile_info_and_exit() -> None:
    """Check profile validity and display info, then exit (AXI format)."""
    config = get_config()

    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )

    version = get_version()
    logger.info(f"LinkedIn MCP Server v{version} - Session Info mode")

    from linkedin_mcp_server.session_state import auth_root_dir

    auth_root = auth_root_dir()
    cookies_path = auth_root / "cookies.json"

    # Simple cookie file check first
    if not cookies_path.exists():
        print(_toon_kv("session", "not_found"))
        print(_toon_kv("path", str(cookies_path)))
        print(
            _toon_kv(
                "help",
                "Run `linkedin-lyr --import-from-browser` to import cookies from your browser",
            )
        )
        sys.exit(0)

    # Check cookie file contents
    try:
        with open(cookies_path) as f:
            cookies = json.load(f)

        # Handle both dict format (cookie name as key) and list format (dict with "name" key)
        if isinstance(cookies, dict):
            li_at_found = "li_at" in cookies and cookies["li_at"]
        elif isinstance(cookies, list):
            li_at_found = any(c.get("name") == "li_at" for c in cookies)
        else:
            li_at_found = False

        print(_toon_kv("session", "valid" if li_at_found else "invalid"))
        print(
            _toon_kv("cookies", len(cookies) if isinstance(cookies, list) else len(cookies.keys()))
        )
        print(_toon_kv("path", str(cookies_path)))

        if li_at_found:
            print(_toon_kv("auth_cookie", "found"))
        else:
            print(_toon_kv("auth_cookie", "missing"))
            print(
                _toon_kv("help", "Run `linkedin-lyr --import-from-browser` to refresh your session")
            )

        sys.exit(0)
    except Exception as e:
        print(_toon_kv("error", f"Could not read session: {e}"))
        sys.exit(1)


def get_version() -> str:
    """Get version from installed metadata with a source fallback."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        for package_name in (
            "linkedin-lyr",
            "linkedin-mcp-server",
        ):
            try:
                return version(package_name)
            except PackageNotFoundError:
                continue
    except Exception:
        pass

    try:
        import os
        import tomllib

        pyproject_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pyproject.toml")
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
            return data["project"]["version"]
    except Exception:
        return "unknown"


# ── Tool registry (auto-discovered from MCP server) ────────────────────────


def _discover_tools() -> list[tuple[str, str]]:
    """Dynamically list all registered MCP tools with their docstrings."""
    try:
        mcp = create_mcp_server()
        tools_obj = asyncio.run(mcp.list_tools())
        return sorted(
            [(t.name, (t.description or "").split("\n")[0].strip()) for t in tools_obj],
            key=lambda x: x[0],
        )
    except Exception:
        # Fallback: static list if discovery fails
        return [
            ("get_person_profile", "Get LinkedIn person profile information"),
            ("get_company_profile", "Get LinkedIn company profile information"),
            ("get_feed", "Get LinkedIn feed posts"),
            ("search_people", "Search for LinkedIn people"),
            ("search_companies", "Search for LinkedIn companies"),
            ("search_jobs", "Search for LinkedIn jobs"),
            ("send_message", "Send a LinkedIn message"),
            ("get_post", "Get a LinkedIn post details"),
            ("comment_on_post", "Comment on a LinkedIn post"),
            ("like_post", "Like a LinkedIn post"),
        ]


_TOOLS_CACHE = None


def _get_tools() -> list[tuple[str, str]]:
    global _TOOLS_CACHE
    if _TOOLS_CACHE is None:
        _TOOLS_CACHE = _discover_tools()
    return _TOOLS_CACHE


# ── AXI §8: Content-first home view ───────────────────────────────────────


def show_home_view() -> None:
    """Show live state when no args provided (AXI §8)."""
    bin_path = _get_bin_path()
    version = get_version()

    # Check session state
    has_session = False
    try:
        from linkedin_mcp_server.session_state import auth_root_dir

        auth_root = auth_root_dir()
        cookies_path = auth_root / "cookies.json"
        has_session = cookies_path.exists()
    except Exception as e:
        # Log warning but don't fail — session check is best-effort
        print(f"warning: Session check failed: {e}", file=sys.stderr)

    # AXI §10: Tool identity header
    print(f"bin: {bin_path}")
    print(f"version: {version}")
    print("description: LinkedIn MCP server — profiles, posts, messaging, and job search")
    print()

    # Live session state
    print("session:")
    if has_session:
        print("  status: valid")
    else:
        print("  status: not_configured")
        print("  help: Run `linkedin-lyr --import-from-browser` to import cookies")
    print()

    # Tool listing in TOON format (AXI §2: minimal schema)
    tools = _get_tools()
    print(f"tools[{len(tools)}]{{name,description}}:")
    for name, desc in tools:
        print(f"  {name},{_truncate(desc, 80)}")
    print()

    # AXI §9: Contextual disclosure
    print("help[4]:")
    print("  Run `linkedin-lyr --tool-info <name>` for detailed parameters")
    print("  Run `linkedin-lyr --list-tools` to see all tools")
    print("  Run `linkedin-lyr --import-from-browser` to import browser cookies")
    print("  Run `linkedin-lyr` to start the MCP server")


def list_tools_and_exit() -> None:
    """List all available MCP tools and exit (AXI §8 content-first)."""
    tools = _get_tools()
    print(f"tools[{len(tools)}]{{name,description}}:")
    for name, desc in tools:
        print(f"  {name},{desc}")
    print()
    print("help[2]:")
    print("  Run `linkedin-lyr --tool-info <name>` for details")
    print("  Run `linkedin-lyr` to start the MCP server")
    sys.exit(0)


def tool_info_and_exit(tool_name: str) -> None:
    """Show detailed info for a specific tool in TOON format."""
    try:
        mcp = create_mcp_server()
        tools_obj = asyncio.run(mcp.list_tools())
        tool = next((t for t in tools_obj if t.name == tool_name), None)
        if tool:
            fields = {"name": tool.name, "description": tool.description or ""}
            schema = getattr(tool, "inputSchema", None) or getattr(tool, "parameters", None) or {}
            if isinstance(schema, dict):
                props = schema.get("properties", {})
                required = set(schema.get("required", []))
                if props:
                    params = []
                    for pname, pdef in props.items():
                        params.append({
                            "name": pname,
                            "type": pdef.get("type", "any"),
                            "required": "true" if pname in required else "false",
                            "description": _truncate(pdef.get("description", ""), 80),
                        })
                    fields["params"] = params
            print(_toon_object(fields))
            print(
                _toon_kv(
                    "help", f"Run `linkedin-lyr` to start the MCP server and call `{tool.name}`"
                )
            )
        else:
            valid = sorted([t.name for t in tools_obj])
            axi_error(f"Unknown tool: '{tool_name}'", f"Valid tools: {', '.join(valid)}")
    except Exception as e:
        axi_error(f"Failed to load tool info: {e}")
    sys.exit(0)


# ── AXI §7: Session integrations ─────────────────────────────────────────────


def install_session_hook_and_exit() -> None:
    """Install session hooks for Claude Code/Codex (AXI §7)."""
    bin_path = _get_bin_path()
    home_dir = os.path.expanduser("~")

    hooks_installed = []

    # Claude Code: ~/.claude/settings.json
    claude_settings = os.path.join(home_dir, ".claude", "settings.json")
    try:
        if os.path.exists(claude_settings):
            with open(claude_settings) as f:
                settings = json.load(f)
        else:
            settings = {}
        hooks = settings.get("hooks", {})
        session_start = hooks.get("SessionStart", [])
        already = any(
            h.get("command") == f"{bin_path} --status" for h in session_start if isinstance(h, dict)
        )
        if not already:
            session_start.append({"command": f"{bin_path} --status"})
            hooks["SessionStart"] = session_start
            settings["hooks"] = hooks
            os.makedirs(os.path.dirname(claude_settings), exist_ok=True)
            with open(claude_settings, "w") as f:
                json.dump(settings, f, indent=2)
            hooks_installed.append("Claude Code")
    except Exception as e:
        print(f"Claude Code hook: {e}", file=sys.stderr)

    # Codex: ~/.codex/hooks.json
    codex_hooks = os.path.join(home_dir, ".codex", "hooks.json")
    try:
        if os.path.exists(codex_hooks):
            with open(codex_hooks) as f:
                hooks = json.load(f)
        else:
            hooks = {}
        session_start = hooks.get("SessionStart", [])
        already = any(
            h.get("command") == f"{bin_path} --status" for h in session_start if isinstance(h, dict)
        )
        if not already:
            session_start.append({"command": f"{bin_path} --status"})
            hooks["SessionStart"] = session_start
            os.makedirs(os.path.dirname(codex_hooks), exist_ok=True)
            with open(codex_hooks, "w") as f:
                json.dump(hooks, f, indent=2)
            hooks_installed.append("Codex")
    except Exception as e:
        print(f"Codex hook: {e}", file=sys.stderr)

    if hooks_installed:
        print(
            _toon_object({
                "status": "success",
                "message": f"Installed hooks for: {', '.join(hooks_installed)}",
            })
        )
    else:
        print(
            _toon_object({
                "status": "info",
                "message": "Hooks already installed or no supported editors found",
            })
        )
    sys.exit(0)


def install_agent_skill_and_exit() -> None:
    """Create installable agent skill from home view (AXI §7)."""
    from pathlib import Path

    skill_dir = Path.home() / ".claude" / "skills" / "linkedin-mcp"
    skill_dir.mkdir(parents=True, exist_ok=True)

    skill_content = """name: LinkedIn MCP Server
description: LinkedIn automation with profile scraping, job search, messaging, and feed analysis
triggers:
  - "linkedin profile"
  - "linkedin search"
  - "linkedin jobs"
  - "linkedin message"
  - "linkedin automation"
  - "professional networking"
  - "job search"

## Overview
LinkedIn MCP Server provides comprehensive LinkedIn automation:
- Profile scraping (people and companies)
- Job search and application tracking
- Direct messaging
- Feed analysis and post interactions
- Company intelligence

## Quick Start
```bash
# Show home view with live state
linkedin-lyr

# Import browser cookies
linkedin-lyr --import-from-browser

# Check session status
linkedin-lyr --status

# List available tools
linkedin-lyr --list-tools

# Start MCP server
linkedin-lyr
```

## MCP Tools
- `get_person_profile` - Get person profile information
- `get_company_profile` - Get company profile information
- `get_feed` - Get LinkedIn feed posts
- `search_people` - Search for LinkedIn people
- `search_companies` - Search for LinkedIn companies
- `search_jobs` - Search for LinkedIn jobs
- `send_message` - Send a LinkedIn message
- `get_post` - Get post details
- `comment_on_post` - Comment on a post
- `like_post` - Like a post

## Session Integration
Install session hooks for ambient context:
```bash
linkedin-lyr --install-hook
```

This shows LinkedIn session state on every agent session start.
"""

    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(skill_content)

    print("status: success")
    print(f"skill_path: {skill_file}")
    print("help: Agent skill installed - will load automatically on LinkedIn-related tasks")
    sys.exit(0)


def main() -> None:
    """Main application entry point."""
    config = get_config()

    # Configure logging
    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )

    version = get_version()

    # Handle AXI flags first (these exit)
    if config.server.list_tools:
        list_tools_and_exit()

    if config.server.tool_info:
        tool_info_and_exit(config.server.tool_info)

    if config.server.install_hook:
        install_session_hook_and_exit()

    if config.server.install_skill:
        install_agent_skill_and_exit()

    # Print banner in interactive mode
    if config.is_interactive:
        print(f"🔗 LinkedIn MCP Server v{version} 🔗")
        print("=" * 40)

    logger.info(f"LinkedIn MCP Server v{version}")

    try:
        # Configure browser environment only for modes that need it
        # --import-from-browser and --status don't need it (they use browser_cookie3)
        # --login and normal server startup do need it
        if config.server.login or not (
            config.server.import_from_browser
            or config.server.export_session
            or config.server.import_session
            or config.server.status
        ):
            configure_browser_environment()

        # Set headless mode from config
        set_headless(config.browser.headless)

        # Handle --logout flag
        if config.server.logout:
            clear_profile_and_exit()

        # Ensure browser is installed for CLI modes that launch it.
        # Normal server startup uses async background setup instead. --login is
        # headed and needs full chromium; --status and --import-from-browser don't
        # need browser anymore (they use browser_cookie3 directly).
        if config.server.login:
            ensure_browser_installed(full=config.server.login)

        # Handle --import-from-browser flag
        if config.server.import_from_browser:
            import_from_browser_and_exit()

        # Handle --export-session / --import-session flags
        if config.server.export_session:
            export_session_and_exit()
        if config.server.import_session:
            import_session_and_exit()

        # Handle --login flag
        if config.server.login:
            get_profile_and_exit()

        # Handle --status flag
        if config.server.status:
            profile_info_and_exit()

        logger.debug(f"Server configuration: {config}")

        # Phase 1: Server Runtime
        try:
            transport = config.server.transport

            # Create and run the MCP server
            mcp = create_mcp_server(tool_timeout=config.server.tool_timeout_seconds)

            if transport == "streamable-http":
                # Validate Host and Origin. Without this a website the user
                # merely visits can point a hostname at this server's address
                # and have the user's own browser drive tools with the
                # logged-in LinkedIn session. The request comes from inside, so
                # a firewall does not help. The MCP specification requires this
                # for local HTTP servers, and it is off unless asked for.
                #
                # Both checks are needed, and the Host one carries most of the
                # weight. A rebinding attack sends its own domain as *both*
                # Host and Origin, so those agree and origin validation alone
                # lets it through; what gives it away is that the Host is not a
                # name this server answers to. Requests carrying no Origin at
                # all stay allowed, which is every non-browser client.
                #
                # True rather than "auto": "auto" only validates when the
                # accepted connection landed on a loopback address, so a server
                # bound to 0.0.0.0 and reached over its LAN address checked
                # nothing at all, which is the exposed case where it matters
                # most. Measured before this: an attacker Host and Origin over
                # the LAN address were served, while the same request to
                # 127.0.0.1 was refused.
                #
                # Strict accepts localhost and the address the connection
                # arrived on, which covers the documented flows. It does not
                # accept a DNS name such as a machine name or a public name in
                # front of a proxy, so those need the proxy to rewrite the
                # upstream Host, or the name listed explicitly. The README says
                # so next to the exposed-bind example, because a 421 nobody can
                # explain is how a guard like this ends up switched off.
                #
                # Deliberately no host wildcard: it would accept any Host and
                # reopen the same hole from the other side.
                mcp.run(
                    transport=transport,
                    host=config.server.host,
                    port=config.server.port,
                    path=config.server.path,
                    host_origin_protection=True,
                )
            else:
                mcp.run(transport=transport)

        except KeyboardInterrupt:
            exit_gracefully(0)

        except Exception as e:
            logger.exception(f"Server runtime error: {e}")
            if config.is_interactive:
                print(f"\n❌ Server error: {e}")
            exit_gracefully(1)
    finally:
        teardown_trace_logging(keep_traces=should_keep_traces())


def exit_gracefully(exit_code: int = 0) -> None:
    """Exit the application gracefully with browser cleanup."""
    try:
        asyncio.run(close_browser())
    except Exception:
        pass  # Best effort cleanup
    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        exit_gracefully(0)
    except Exception as e:
        logger.exception(
            f"Error running MCP server: {e}",
            extra={"exception_type": type(e).__name__, "exception_message": str(e)},
        )
        exit_gracefully(1)

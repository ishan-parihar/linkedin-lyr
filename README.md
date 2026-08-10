# LinkedIn MCP Server

<!-- T2I HERO SPEC — Subject: a LinkedIn data engine — a professional profile card feeding profiles, companies, jobs, and messages through your own browser session into an MCP server for agents. Composition: profile card → browser-session pipeline → agent. Palette: linkedin blue #0a66c2 → dark slate → clean white cards. Style: dark flat vector, connection-node motif, no text. 16:9. -->

**Give AI assistants like Claude access to LinkedIn profiles, companies, jobs, and messaging through your own browser session.**

[![PyPI](https://img.shields.io/pypi/v/linkedin-lyr?color=blue)](https://pypi.org/project/linkedin-lyr/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

> **Disclaimer:** This is an independent, community project. It is not affiliated with, authorized by, endorsed by, or sponsored by LinkedIn Corporation or Microsoft. "LinkedIn" is a registered trademark of LinkedIn Corporation and is used here only descriptively to identify the third-party service this software interoperates with.

---

## What It Does

This MCP server and CLI lets AI assistants read LinkedIn data through your own logged-in browser session. Import cookies from your browser, configure the MCP server, and give Claude access to:

- **Profiles**: Get person profiles with sections like posts, experience, skills
- **Companies**: Extract company information and search by keywords  
- **Jobs**: Search job postings with filters and get detailed job information
- **Messaging**: List conversations and send messages to LinkedIn users
- **Feed**: Get recent posts from your home feed and search by keywords

## How It Works

1. **Import cookies** from your browser (Chrome, Brave, Firefox, Edge, and more)
2. **Start the MCP server** using the `linkedin-lyr` command
3. **Configure Claude Desktop** to connect to the MCP server
4. **Use LinkedIn data** through Claude with natural language queries

**No browser automation required** — the system uses your existing browser session directly.

## Quick Start

### 1. Install

```bash
pip install linkedin-lyr
```

### 2. Import Your LinkedIn Session

```bash
# Auto-detect browser
linkedin-lyr --import-from-browser auto

# Or specify a browser
linkedin-lyr --import-from-browser brave
linkedin-lyr --import-from-browser chrome
```

**Output:**
```
✅ Successfully imported LinkedIn cookies
   Source: brave[Default](in-process)
   Cookies: 14
   Path: /home/user/.linkedin/cookies.json
   ✅ Authentication cookie (li_at) found
```

### 3. Verify Session

```bash
linkedin-lyr --status
```

**Output:**
```
Session: active
Cookies: 14
Path: /home/user/.linkedin/cookies.json
✅ Authentication cookie (li_at) found
```

### 4. Configure Claude Desktop

Add to your Claude Desktop configuration file:

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`  
**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`  
**Linux**: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "linkedin-lyr",
      "args": []
    }
  }
}
```

Restart Claude Desktop and you're ready to use LinkedIn data through Claude.

---

## What Makes It Different

**Playwright-Free Cookie Import**
- Direct browser cookie extraction without Playwright dependency
- Supports Chrome, Brave, Firefox, Edge, Chromium, Opera, Vivaldi, Arc, and more
- Handles encrypted cookies and multiple browser profiles

**AXI-Compliant CLI**
- Token-efficient TOON output format for optimal AI agent interaction
- Structured errors with actionable suggestions
- Content-first design — shows session status by default

**Multi-Browser Support**
- Auto-detection across all installed browsers
- Support for 10+ browser types including Brave Origin Beta and Zen Browser
- Reliable cookie extraction with proper error handling

**Agent Integration**
- Session hooks for Claude Code, Codex, and other AI agents
- Ambient context injection for seamless agent startup
- Installable agent skill for automatic discovery

---

## Installation Methods

### Global Installation (Recommended)

```bash
pip install linkedin-lyr
```

This installs both `linkedin-lyr` (CLI) and `linkedin-lyr` (MCP server).

### Using pipx

```bash
pipx install linkedin-lyr
```

### Using uvx (No Installation)

```bash
uvx linkedin-lyr@latest --status
uvx linkedin-lyr@latest --import-from-browser brave
```

### One-Line Install Script

```bash
curl -sSL https://raw.githubusercontent.com/ishan-parihar/linkedin-lyr/main/install.sh | bash
```

---

## CLI Commands

### Session Management

```bash
linkedin-lyr --status                           # Check session status
linkedin-lyr --import-from-browser [browser]   # Import cookies from browser
linkedin-lyr --logout                          # Clear LinkedIn session
```

### Browser Options

Supported browsers for cookie import:
- `auto` (auto-detect all browsers)
- `chrome` (Google Chrome)
- `edge` (Microsoft Edge)
- `firefox` (Mozilla Firefox)
- `brave` (Brave Browser)
- `brave-origin` (Brave Origin Beta)
- `chromium` (Chromium)
- `opera` (Opera)
- `vivaldi` (Vivaldi)
- `zen` (Zen Browser)

### MCP Server

```bash
linkedin-lyr                                    # Start MCP server with defaults
linkedin-lyr --transport streamable-http --host 127.0.0.1 --port 8080
```

---

## MCP Tools Available

| Tool | Description | Status |
|------|-------------|--------|
| `get_person_profile` | Get profile info with section selection | working |
| `get_my_profile` | Get your own LinkedIn profile | working |
| `get_company_profile` | Extract company information | working |
| `search_companies` | Search for companies by keywords | working |
| `search_jobs` | Search for jobs with filters | working |
| `get_job_details` | Get detailed job posting information | working |
| `get_feed` | Get recent posts from home feed | working |
| `search_posts` | Search posts by keyword | working |
| `get_inbox` | List messaging conversations | working |
| `send_message` | Send messages to LinkedIn users | limited |
| `close_session` | Close browser session | working |

---

## Limitations

### No posting
`linkedin-lyr` has **no post tool** — it reads profiles/companies/jobs/feed/posts and sends DMs, but does not create posts or edit the profile.

### `send_message` (DM) is blocked on headless/automated hosts
The DM pipeline itself works — browser boots via obscura, cookies inject, the composer resolves against a real 1st-degree connection in a `confirm_send=False` dry run. But live sends are not reliably verifiable:

- **Session rotation:** LinkedIn rotates any `li_at` that appears in an automated browser within ~30 min (server issues `li_at=delete me` + 302). After rotation the same cookie redirect-loops on `/feed/`, `/mynetwork/`, and even `/in/<profile>/`.
- **Anti-bot wall:** the Playwright/patchright browser fingerprint is flagged, so even valid cookies get re-rejected on browser-touching tools (the `validation_interval` revalidation loop).
- **Escape hatches:** `LINKEDIN_SKIP_BROWSER_VALIDATION=1` bypasses browser re-validation; direct-drive the extractor (`get_or_create_browser()` + cookies from `get_valid_linkedin_cookies_from_daemon()`) to bypass the bootstrap login gate.
- **Self-DM is blocked at the platform level** (no Message button on your own profile).

### Cookie shape mismatch
`--status` reads a **flat** cookie dict from `~/.linkedin-lyr/cookies.json`; the extractor reads a **Playwright-format list** from `~/.linkedin-lyr/profile/cookies.json`. The two must be maintained in their own formats — writing one shape to the wrong file makes the auth gate misreport.

---

## Configuration

### Environment Variables

```bash
LINKEDIN_USER_DATA_DIR=~/.linkedin/profile    # Browser profile directory
LINKEDIN_LOG_LEVEL=WARNING                         # Logging level
LINKEDIN_HEADLESS=true                              # Run browser headless
```

### Advanced MCP Configuration

Alternative MCP server configurations:

**Using pipx:**
```json
{
  "mcpServers": {
    "linkedin": {
      "command": "pipx",
      "args": ["run", "linkedin-lyr"],
      "env": { "UV_HTTP_TIMEOUT": "300" }
    }
  }
}
```

**Using uvx:**
```json
{
  "mcpServers": {
    "linkedin": {
      "command": "uvx",
      "args": ["linkedin-lyr@latest"],
      "env": { "UV_HTTP_TIMEOUT": "300" }
    }
  }
}
```

---

## Important Safety Notes

### Account Safety

This tool controls a real browser session; it doesn't exploit undocumented APIs or bypass authentication. LinkedIn's User Agreement prohibits automated access, and accounts using automated tools can be restricted or banned. Use at your own risk.

### Rate Limiting

Tool calls run sequentially through a queue. You are responsible for the volume of automation you run; use it sparingly and prompt your agents responsibly.

### Proxy Usage

Most users should not use a proxy. LinkedIn's own guidance for reducing security challenges is to avoid VPNs or proxies, as they score the addresses a session signs in from. A home connection you have used for years is a trust signal; a commercial exit node is not.

---

## Troubleshooting

### Session Issues

```bash
# Check session status
linkedin-lyr --status

# Clear and re-import
linkedin-lyr --logout
linkedin-lyr --import-from-browser brave
```

### MCP Connection Issues

```bash
# Start with debug logging
linkedin-lyr --log-level DEBUG

# Check Claude Desktop configuration
# Verify the path to linkedin-lyr is correct
```

### Cookie Import Issues

```bash
# Try auto-detection
linkedin-lyr --import-from-browser auto

# Check browser installation
linkedin-lyr --import-from-browser chrome
linkedin-lyr --import-from-browser firefox
```

---

## Development

### Local Setup

```bash
git clone https://github.com/ishan-parihar/linkedin-lyr
cd linkedin-mcp-server
uv sync
uv sync --group dev
uv run pre-commit install
```

### Running Tests

```bash
uv run pytest
uv run pytest --cov
```

### Code Quality

```bash
uv run ruff check .
uv run ruff format .
uv run ty check
```

---

## Acknowledgments

Built with [FastMCP](https://gofastmcp.com/) and browser-cookie3.

## Related CLI Tools

This project is part of a family of agent-friendly CLI tools for social platforms:

| Tool | CLI | Repo |
|------|-----|------|
| Instagram | `instagram-lyr` | [ishan-parihar/instagram-lyr](https://github.com/ishan-parihar/instagram-lyr) |
| Reddit | `reddit-lyr` | [ishan-parihar/reddit-lyr](https://github.com/ishan-parihar/reddit-lyr) |
| LinkedIn | `linkedin-lyr` | [ishan-parihar/linkedin-lyr](https://github.com/ishan-parihar/linkedin-lyr) |
| Twitter/X | `twitter-lyr` | [ishan-parihar/twitter-lyr](https://github.com/ishan-parihar/twitter-lyr) |
| Discord | `discord` | [ishan-parihar/discord-cli](https://github.com/ishan-parihar/discord-cli) |
| Telegram | `tg` | [ishan-parihar/tg-cli](https://github.com/ishan-parihar/tg-cli) |

## License

MIT License - see [LICENSE](LICENSE) file for details.

Use in accordance with [LinkedIn's User Agreement](https://www.linkedin.com/legal/user-agreement). Automated access may violate LinkedIn's terms and can lead to account restrictions. This tool is for personal use only and comes with no warranty of any kind.
# MCP Configuration Guide

This guide explains how to wire up the LinkedIn MCP server with various AI agents and platforms.

## Claude Desktop (Recommended)

### Installation Steps

1. **Install linkedin-lyr globally:**
   ```bash
   pip install linkedin-lyr
   # OR
   pipx install linkedin-lyr
   ```

2. **Configure Claude Desktop:**
   
   Locate your Claude Desktop configuration file:
   - **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
   - **Linux**: `~/.config/Claude/claude_desktop_config.json`

3. **Add the MCP server configuration:**
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

4. **Restart Claude Desktop** to activate the server.

5. **Import LinkedIn cookies:**
   ```bash
   linkedin-lyr --import-from-browser brave
   ```

6. **Verify session status:**
   ```bash
   linkedin-lyr --status
   ```

### Alternative Configurations

#### Using pipx
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

#### Using uvx (No Installation)
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

#### Using Direct Path
```json
{
  "mcpServers": {
    "linkedin": {
      "command": "/home/user/.local/bin/linkedin-lyr",
      "args": []
    }
  }
}
```

## Other MCP Clients

### Cline (VS Code Extension)

Add to your Cline settings:
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

### Continue.dev

Configure in your MCP settings:
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

### Generic MCP Client

For any MCP-compatible client, use the standard configuration:
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

## Troubleshooting

### PATH Issues

If you get "command not found" errors:

1. **Check your PATH:**
   ```bash
   which linkedin-lyr
   ```

2. **Ensure pipx is in PATH:**
   ```bash
   pipx ensurepath
   ```

3. **Use full path in configuration:**
   ```json
   {
     "mcpServers": {
       "linkedin": {
         "command": "/home/user/.local/bin/linkedin-lyr",
         "args": []
       }
     }
   }
   ```

### Session Issues

If the MCP server can't find cookies:

1. **Check session status:**
   ```bash
   linkedin-lyr --status
   ```

2. **Re-import cookies:**
   ```bash
   linkedin-lyr --import-from-browser auto
   ```

3. **Check cookie file location:**
   ```bash
   ls -la ~/.linkedin/cookies.json
   ```

### Permission Issues

If you get permission errors:

1. **Check file permissions:**
   ```bash
   ls -la ~/.linkedin/cookies.json
   ```

2. **Fix permissions:**
   ```bash
   chmod 600 ~/.linkedin/cookies.json
   ```

## Environment Variables

You can configure the MCP server using environment variables:

```bash
# Browser profile directory
LINKEDIN_USER_DATA_DIR=~/.linkedin/profile

# Logging level
LINKEDIN_LOG_LEVEL=WARNING

# Headless mode
LINKEDIN_HEADLESS=true

# Obscura features
LINKEDIN_OBSURA_CONNECTION_POOLING=true
LINKEDIN_OBSURA_CACHING=true
LINKEDIN_OBSURA_ADVANCED_PARSING=true
```

Add these to your MCP configuration:
```json
{
  "mcpServers": {
    "linkedin": {
      "command": "linkedin-lyr",
      "args": [],
      "env": {
        "LINKEDIN_LOG_LEVEL": "DEBUG",
        "LINKEDIN_HEADLESS": "true"
      }
    }
  }
}
```

## Verification

After configuration, verify the MCP connection:

1. **Check CLI functionality:**
   ```bash
   linkedin-lyr --status
   ```

2. **Test MCP server startup:**
   ```bash
   linkedin-lyr
   ```

3. **Verify in Claude Desktop:**
   - Open Claude Desktop
   - Check the MCP tools are available
   - Try using a LinkedIn tool

## Advanced Configuration

### Custom Server Transport

For HTTP transport instead of stdio:
```json
{
  "mcpServers": {
    "linkedin": {
      "command": "linkedin-lyr",
      "args": ["--transport", "streamable-http", "--host", "127.0.0.1", "--port", "8080"]
    }
  }
}
```

### Debug Mode

Enable debug logging for troubleshooting:
```json
{
  "mcpServers": {
    "linkedin": {
      "command": "linkedin-lyr",
      "args": ["--log-level", "DEBUG"]
    }
  }
}
```

## Cookie Import Options

The MCP server supports multiple browsers for cookie import:

```bash
# Auto-detect
linkedin-lyr --import-from-browser auto

# Specific browsers
linkedin-lyr --import-from-browser chrome
linkedin-lyr --import-from-browser brave
linkedin-lyr --import-from-browser brave-origin
linkedin-lyr --import-from-browser edge
linkedin-lyr --import-from-browser firefox
linkedin-lyr --import-from-browser chromium
linkedin-lyr --import-from-browser opera
linkedin-lyr --import-from-browser vivaldi
linkedin-lyr --import-from-browser zen
```

## Support

For issues or questions:
- GitHub Issues: https://github.com/ishan-parihar/linkedin-lyr/issues
- Documentation: https://github.com/ishan-parihar/linkedin-lyr#readme
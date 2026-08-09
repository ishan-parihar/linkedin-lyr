---
name: linkedin-lyr
description: LinkedIn automation with profiles, companies, jobs, messaging, and feed access. Use this skill whenever the user requests LinkedIn operations, professional networking, job searching, or any LinkedIn-related tasks. Triggers on phrases like "linkedin profile", "linkedin search", "linkedin jobs", "linkedin message", "linkedin automation", "job search", "professional networking", "company research", or any request for LinkedIn functionality.
---

# LinkedIn-lyr Skill

This skill enables AI agents to interact with LinkedIn using the linkedin-lyr CLI tool. It provides comprehensive LinkedIn automation including profiles, companies, jobs, messaging, and feed access.

## Prerequisites

- linkedin-lyr CLI must be installed globally on the system
- The CLI must be accessible in the system PATH
- Browser cookies are required for authenticated operations

## Command Structure

### Available Commands

```bash
# Session management
linkedin-lyr status                    # Check session status
linkedin-lyr import                   # Import browser cookies
linkedin-lyr import brave             # Import from specific browser
linkedin-lyr logout                  # Clear session
linkedin-lyr browsers                 # List supported browsers
linkedin-lyr mcp                      # Start MCP server
linkedin-lyr setup-session           # Install session hooks
```

## MCP Tool Usage

The linkedin-lyr CLI serves as an MCP server with the following tools:

### Profile Tools
- `get_person_profile` - Get LinkedIn profile information
- `get_my_profile` - Get your own LinkedIn profile
- `connect_with_person` - Send connection requests

### Company Tools
- `get_company_profile` - Get company information
- `search_companies` - Search for companies
- `get_company_posts` - Get company posts
- `get_company_employees` - List company employees

### Job Tools
- `search_jobs` - Search for jobs
- `get_job_details` - Get job posting details
- `get_saved_jobs` - List saved jobs

### Messaging Tools
- `get_inbox` - List messaging conversations
- `get_conversation` - Read specific conversation
- `send_message` - Send messages

### Feed Tools
- `get_feed` - Get home feed posts
- `search_posts` - Search posts by keyword

## MCP Configuration

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "uvx",
      "args": ["linkedin-lyr"]
    }
  }
}
```

## Authentication

Import LinkedIn authentication cookies from your browser:

```bash
# Auto-detect browser
linkedin-lyr import

# Specific browser
linkedin-lyr import brave
linkedin-lyr import chrome
linkedin-lyr import firefox
```

## Session Integration

Install session hooks for ambient context:

```bash
linkedin-lyr setup-session
```

This shows LinkedIn session state on every agent session start.

## Workflow

### Step 1: Analyze User Request
- Determine the type of LinkedIn operation needed (profile, company, job, message, feed)
- Identify if authentication is required
- Select appropriate command based on user intent

### Step 2: Generate Command
- Use the command structure above to build the appropriate CLI command
- Add relevant flags for sections, limits, and output format
- Include any required parameters (usernames, query terms, etc.)

### Step 3: Execute and Validate
- Run the command using shell execution
- Check for successful completion
- Parse the output in the appropriate format
- Report the result to the user

### Step 4: Handle Errors
- If authentication fails: guide user to run `linkedin-lyr import`
- If command not found: suggest installing linkedin-lyr
- If rate limited: suggest waiting before retrying
- If invalid parameters: provide correct usage examples

## Error Handling

### Common Issues and Solutions

1. **Command not found**
   - Error: "linkedin-lyr: command not found"
   - Solution: Install linkedin-lyr globally or add to PATH

2. **Authentication errors**
   - Error: "Authentication required"
   - Solution: Run `linkedin-lyr import` to import browser cookies

3. **Session validation failed**
   - Error: "Session validation failed"
   - Solution: Re-import cookies and ensure you're logged into LinkedIn

4. **No supported browsers found**
   - Error: "No supported browsers found"
   - Solution: Install a supported browser (Brave, Chrome, Firefox, Edge)

5. **Rate limiting**
   - Error: "Rate limit exceeded"
   - Solution: Wait before retrying the operation

## Integration Example

### User Request: "Get Bill Gates' LinkedIn profile"

**Skill Processing:**
1. Identify as a profile operation
2. Generate command: MCP tool call to `get_person_profile` with username "williamhgates"
3. Execute and parse results
4. Report profile to user

### User Request: "Search for software engineer jobs"

**Skill Processing:**
1. Identify as a job search operation
2. Generate command: MCP tool call to `search_jobs` with query "software engineer"
3. Execute and format results
4. Report job listings to user

### User Request: "Send a LinkedIn message"

**Skill Processing:**
1. Identify as a write operation requiring authentication
2. Generate command: MCP tool call to `send_message` with recipient and message
3. Execute and report results

## Best Practices

1. **Always check authentication requirements** before executing write operations
2. **Use appropriate sections** when getting profiles (posts, experience, skills, etc.)
3. **Handle rate limiting gracefully** by suggesting delays between operations
4. **Provide context in results** when returning content data (author, timestamp, metrics)
5. **Use reasonable limits** when searching to avoid overwhelming results
6. **Handle errors with actionable suggestions** for resolution
7. **Verify usernames** before messaging to ensure they exist
8. **Use Brave browser** for best bot-detection resistance when importing cookies
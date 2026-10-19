# LinkedIn Profile-Edit: Investigation Findings

Status: **Investigation complete — awaiting decision before build.**

## What the user asked for
Implement LinkedIn profile editing (about, skills, education, projects, publications,
courses, languages) for AI agents via MCP/CLI.

## Grounded facts

### Read surface (confirmed)
- Voyager API base: `https://www.linkedin.com/voyager/api/identity/profiles/{publicId}/...`
  — confirmed via `nsandman/linkedin-api` source and the iron-mind production scraper writeup (#2492).
- Auth recipe (iron-mind, #2492): `li_at` cookie + `JSESSIONID`-derived `csrf-token` (quotes
  stripped) + `x-restli-protocol-version: 2.0.0`. The linkedin-lyr cookie store already carries
  `li_at`, `JSESSIONID`, `bcookie`, `bscookie` (#2500) — so direct-HTTP auth is available.
- **Direct HTTP (curl_cffi, already a linkedin-lyr dependency per obscura_cookie_import.py:130)
  is the only viable path**: the browser rotates/kills the session on the VPS within ~30min
  of automated use (memory #2329 — identical anti-bot outcome as facebook/twitter). Any
  profile-edit flow must NOT touch the browser.

### Write surface (NOT grounded — open constraint)
- Microsoft Learn docs (#2491) cover only the **official** `/v2/` API (api.linkedin.com/v2),
  which is heavily restricted and does **not** cover the Voyager web-client edit surface that
  this tool must mirror.
- `nsandman/linkedin-api` — the canonical community Voyager library — **does not implement
  profile writes at all** (its `add_connection` write is commented `# TODO doesn't work`,
  #2492). No community lib carries the Voyager profile-edit POST shapes.
- LinkedIn's web-client edit endpoints (`profileupdates`, per-section collections) live in the
  lazy JS bundles, NOT in main.js. Extracting them requires the same ondemand-bundle scrape
  that remains an **unfinished wall** for twitter DM (compartments 56-60, #2329) — LinkedIn
  bundles are 10x larger and more aggressively obfuscated than twitter's.

### Tool wiring (confirmed)
- `server.py` registers tool-group modules via `register_*_tools(mcp, tool_timeout=...)` (#2498).
- Tool idiom: `get_ready_extractor(ctx, tool_name=...)` gates on `ensure_tool_ready_or_raise`;
  browser-backed tools fail here when no browser-backed auth is viable (#2313/#2329).
- The read tools (`get_person_profile`, `get_my_profile`, search) are browser-scrape; a
  write client would bypass that layer entirely (direct-HTTP Voyager), analogous to the
  `GraphApiClient` pattern used for facebook (#2208/#2210).

## The gap
The facebook Graph API write layer shipped because Postiz (a self-hosted reference) exposed
the exact endpoint shapes and I could live-verify every mutation (#2471). For LinkedIn there
is **no self-hosted reference and no public write-endpoint spec**. The only ground-truth source
is the LinkedIn web-client bundle, and reaching that bundle under authed cookies is the same
open scrape that blocked twitter DM.

## Decision needed (mirrors #2217 twitter-DM options)
- **A) Build the scraper.** Run the linkedin-lyr own tool / a Playwright session with the live
  cookies to fetch the edit bundles and extract the `profileupdates` op IDs + request bodies.
  High effort, same anti-bot class as twitter DM / facebook messenger browsers (#2329).
- **B) Honest-error guard (recommended).** Register the profile-edit MCP tools (`update_about`,
  `add_skill`, `add_education`, `add_project`, `add_publication`, `add_course`,
  `add_language`) as an honest-error surface: when invoked, they surface a structured error
  `linkedin_profile_write_unavailable — profile editing is not implemented on the Voyager web
  surface without a real endpoint spec; see LINKEDIN_PROFILE_EDIT_INVESTIGATION.md`. Shipped
  immediately, no fabrication. Same pattern as the twitter DM `query_id_error` guard (#2290).
- **C) Implement with hand-written payloads** from rest.li conventions — explicitly
  discouraged: the rest.li `patch`/`$set` bodies differ per section and per LinkedIn
  version; unverified payloads risk silent partial-writes or session-flagging, indistinguishable
  from the original facebook `updatestatus.php` failure (#2204).

No build has started. No commits.

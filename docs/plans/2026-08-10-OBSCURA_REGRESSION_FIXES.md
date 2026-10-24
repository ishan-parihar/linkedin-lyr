# linkedin-lyr Obscura Regression Fixes — Implementation Plan

## Root cause (verified in recon)

The "works natively through obscura but ai-agents messed it up later" regression:

1. **Amplifier (commit gate default-off):** a Devin-era commit made browser-based cookie validation the *default* by
   introducing `LINKEDIN_SKIP_BROWSER_VALIDATION` as an **opt-out** gate. When unset, `LinkedInCookieValidator.validate()`
   boots the obscura browser, loads stored `li_at`, and hits `/feed/` — which per #2329 triggers LinkedIn's server-side
   session rotation within ~30 min. Every import cycle amplifies the very rotation it's supposed to detect.
2. **Presence-only daemon path:** `get_valid_cookies_from_daemon` (per #2327/#2469 recall) can hand the extractor
   cookies that only look alive (required key present) but 302-loop — the dead-session signature.
3. **No liveness check before browser use:** structured write/read surfaces load whatever `get_valid_cookies`
   returns into a browser with no direct-HTTP probe.

The correct native pattern already exists in `obscura_cookie_import.py`: `cffi_req.Client(impersonate="chrome")`
direct-HTTP (TLS-coherent, no browser boot). The fixes reuse it and make the browser path unreachable-by-default.

## Fixes (ladder from the audit, each with a regression guardrail test)

### Fix 1 — Stop the amplifier (highest leverage)
- New locally-importable module `linkedin_mcp_server/voyager_auth.py` (NO `obscura_core` dep):
  - `probe_session(cookies) -> ProbeVerdict` — direct-HTTP Voyager GET
    `voyager/api/identity/profiles?q=memberIdentity`… with cookie + `csrf-token` (JSESSIONID minus `ajax:`) +
    `x-restli-protocol-version: 2.0.0`. Verdict is `"alive" | "dead" | "missing"` (200 = alive, 302/redirect-loop
    = dead, missing keys = missing). Network blips return `"dead"` (never optimistically alive).
  - `aprobe_session(cookies)` async wrapper (`asyncio.to_thread`) so event-loop callers don't block on TLS.
- `LinkedInCookieValidator.validate()` **first runs the direct-HTTP probe** and never boots a browser. There is
  **no env flag** gating the probe — probe-first is unconditional (#2564). Missing required key → False; probe
  verdict != alive → False. The browser boot path was fully removed from the validator.
- Guardrail test `tests/test_voyager_auth.py` (10 cases): with no env set, the validator NEVER calls
  `get_or_create_browser`; a 302 probe verdict makes `validate()` return False; probes monkeypatched at the
  `voyager_auth.aprobe_session` source binding (import-inside-body resolves at call time, not module level).

### Fix 3 — Session pool + liveness failover
- New module `linkedin_mcp_server/session_pool.py` (NO `obscura_core` dep):
  - `list_sessions(source_profile_dir=None) -> list[PoolEntry]` — enumerates every pooled session file
    (name-sorted for deterministic failover), skipping unreadable ones.
  - `first_live_session(source_profile_dir=None, *, timeout=10.0) -> PoolEntry | None` — probes each pooled
    session via `voyager_auth.aprobe_session`, returns the first alive entry, else None. Never boots a browser.
  - `PoolEntry(name, path, cookies)` with `.cookies` as the flat cookie dict.
- Guardrail test `tests/test_session_pool.py` (9 cases): dead primary file + live secondary → returns the live
  one; all-dead → None; single-wrap invariant maintained on save; no browser boot.

### Fix 4 — Fresh-supply chain (user-run on a logged-in machine → VPS)
- CLI: `linkedin-lyr --export-session <path>` writes the portable cookie bundle (single-wrap list-of-dicts per
  #2201). `linkedin-lyr --import-session <path>` validates shape + URI-decodes and writes into
  `portable_cookie_path()`. Both modes are probe-first (no browser), excluded from the browser-env path.
- Surfaces: `ServerConfig.export_session`/`import_session` fields (`config/schema.py`), argparse flags
  (`config/loaders.py`), dispatch in `cli_main.main()` mirroring the `--import-from-browser` precedent.
- Guardrail test `tests/test_session_export_import.py` (5 cases): export→import round-trip preserves the
  single-wrap invariant and round-trips `li_at`; import gates on the probe-first validator.

### Fix 5 — Honest degradation (empty pool never boots a browser)
- `dependencies.get_ready_extractor()` (the single runtime resolution chain for every tool): after a dead daemon
  verdict, consult the session pool via `_first_live_pool_session()` (probe-first, thread-pooled). Alive pool →
  boot the browser with pooled cookies; dead/empty pool → raise `AuthenticationError` with the
  "No live LinkedIn session" message routing into `handle_auth_error` (re-login flow), never a browser boot.
- Guardrail test `tests/test_honest_degradation.py` (2 cases): dead/empty pool → honest error raised AND
  `get_or_create_browser` never awaited; alive pool → browser boots with pooled `li_at`.

### Fix 2 — Voyager direct-HTTP for structured reads (deferred, minimal)
- Reads stay on the extractor for now; only the *liveness probe* becomes direct-HTTP. Full read migration is
  explicitly out of scope this pass (large extractor surface, JS-heavy feed/sidebar per #2331). Noted, not built.

## Files touched
- `linkedin_mcp_server/voyager_auth.py` (new), `linkedin_mcp_server/session_pool.py` (new)
- `linkedin_mcp_server/obscura_integration.py` (validator probe-first, browser boot removed)
- `linkedin_mcp_server/dependencies.py` (Fix 5 pool gate in `get_ready_extractor`)
- `linkedin_mcp_server/cli_main.py` + `config/schema.py` + `config/loaders.py` (export/import-session flags)
- `README.md` (document export/import + probe-first behavior)
- `tests/test_voyager_auth.py`, `tests/test_session_pool.py`, `tests/test_session_export_import.py`,
  `tests/test_honest_degradation.py` (new)

## Verification
- New guardrail suites green: `tests/test_voyager_auth.py` (10), `tests/test_session_pool.py` (9),
  `tests/test_session_export_import.py` (5), `tests/test_honest_degradation.py` (2) — 26 new green tests.
- The remaining failure surface in the full suite is **pre-existing at HEAD** (91 failed + 32 errors, including
  `test_dependencies.py`'s 6 cases targeting the never-defined `ensure_authenticated` symbol) and verified via
  stash+baseline comparison to be unaffected by this arc.
- Full suite collect + commit + push (repo hygiene per standing rule).

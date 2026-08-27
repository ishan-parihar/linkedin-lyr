# BrowseFleet CloakBrowser Integration Plan for LinkedIn-LYR

> **Goal:** Replace the heavyweight `obscura` local binary + noVNC with a thin BrowseFleet client so `linkedin-lyr` on the 2.4 GB RackNerd VPS makes only HTTP/CDP calls to the remote CloakBrowser pool at `https://browsefleet.ishanparihar.com` (now running on `omarchy` local Docker, tunneled via existing `cloudflared-tunnel@main`).

**User constraint:** No noVNC — too much RAM on VPS. `bf` CLI already on `hermes-vps`. Deploy on local Docker via existing Cloudflare Tunnel.

---

## 1. Deployment Status (Done 2026-08-26)

| Step | Result |
|------|--------|
| **Tunnel** | `~/.cloudflared/main.yml` already had `browsefleet.ishanparihar.com → http://localhost:3000`, `cloudflared-tunnel@main` active |
| **.env** | Fixed duplicate `API_KEYS`, set `API_KEYS=49f7c273...,60b401...,89fb31...`, `CDP_EXTERNAL_HOST=browsefleet.ishanparihar.com:443/wss`, `STEALTH_DEFAULT=full`, `MAX_CONCURRENT_SESSIONS=30` |
| **Docker** | `docker compose build` 35s, `up -d` → `browsefleet-browsefleet-1` healthy on `127.0.0.1:3000`, `GET /health` → `{"status":"ok","version":"1.1.0"}` locally and via `https://browsefleet.ishanparihar.com/health` |
| **VPS bf CLI** | Fixed `~/.browsefleet.env` (`BROWSEFLEET_URL`+`TOKEN`+`CDP_URL`), `bf health`/`bf sessions` now work from `hermes-vps` |
| **Local resources** | 46 GB RAM, 24 vCPU — fits 10 sessions (2–5 GB). RackNerd 2.4 GB stays thin-client only |

**Remaining deploy hardening (optional):**
- Set `CLOAKBROWSER_LICENSE_KEY` in `.env` for Pro binary (71 patches, unlimited sessions vs free 1-concurrent). Without it, free tier still works for single-session LinkedIn.
- Add `bf.env` to VPS `~/.linkedin-lyr/bf.env` so all cron contexts inherit `BROWSEFLEET_*` without per-job env.

---

## 2. Why BrowseFleet Instead of Obscura / noVNC

| Aspect | Obscura (`/tmp/obscura serve`) | noVNC patchright | **BrowseFleet** |
|--------|-------------------------------|------------------|-----------------|
| **Where Chrome runs** | VPS local binary (Rust) | VPS Chromium persistent on `:1` | **Remote pool** (local Docker or Hetzner) |
| **VPS RAM** | ~300 MB + 2 GB shm | 512 MB cap + VNC | **~0 MB** — only `curl`/`websockets` |
| **Cookie replay** | File `cookies.json` → `wreq` jar (dotless rewrite bug, `secure:false`) | SQLite `Cookies` (native) | **HTTP API `cookies` array** at `POST /v1/sessions` + `profileId` persistence |
| **Stealth** | `wreq` impersonation (flagged today) | Real Chromium | **CloakBrowser patched Chromium** (71 Pro patches) |
| **LinkedIn flag** | `li_at` replay from datacenter → `delete-me` (3 revocations today) | Native login on VPS IP → stable, but heavy | **Native login via `operatorMode` viewer** → profile persists, no replay |

BrowseFleet's `POST /v1/sessions {profileId}` persists via `Storage.getCookies` on `release()` (`session.ts:151`), so one manual login via `viewerUrl` survives restarts — exactly what `patchright` did, without VNC.

---

## 3. Plugin Design — Mirror of `obscura_browser.py`

### 3.1 New file: `linkedin_mcp_server/core/browsefleet_browser.py`

```python
class BrowseFleetBrowserManager:
    """Drop-in replacement for ObscuraBrowserManager, backed by BrowseFleet HTTP/CDP."""

    def __init__(self, api_url=None, api_key=None, profile_id=None, **opts):
        # env: BROWSEFLEET_URL, BROWSEFLEET_TOKEN, BROWSEFLEET_PROFILE_ID
        # fallback to ~/.browsefleet.env via common_utils.load_proxy_env()
        self.api_url = api_url or os.environ["BROWSEFLEET_URL"]
        self.api_key = api_key or os.environ["BROWSEFLEET_TOKEN"]
        self.profile_id = profile_id
        self.session_id: str | None = None
        self.cdp_url: str | None = None
        self._playwright_browser = None
        self._playwright_context = None
        self._playwright_page = None

    async def start(self):
        # 1. Load Brave-Origin cookies (already fixed for Brave-Origin vs -Beta)
        cookies = extract_linkedin_cookies()["all_cookies"]  # 14, li_at present
        # 2. POST /v1/sessions {stealth:"full", cookies:[{name,value,domain:".linkedin.com"}], profileId, timeout}
        # 3. playwright.chromium.connect_over_cdp(self.cdp_url)  # wss://browsefleet.ishanparihar.com/cdp/<id>
        # 4. contexts[0] or new_context, new_page, add_cookies to context for redundancy

    async def close(self):  # POST /v1/sessions/:id/release
    async def goto(url, **kw):  # page.goto
    # ... content(), title(), evaluate(), cookies(), add_cookies(), import_cookies() — same surface as Obscura
```

**Reuse:** `browser_cookie_extractor` (already fixed), `common_utils.load_proxy_env` (extend to load `~/.browsefleet.env`), `scraping/extractor.py` (takes `page` object — agnostic to backend).

### 3.2 Backend selection

`linkedin_mcp_server/core/browser_backend.py` (currently hard-coded `obscura`):

```python
def get_browser_backend() -> str:
    return os.environ.get("LINKEDIN_BROWSER_BACKEND", "obscura")  # "obscura" | "browsefleet"

def should_use_browsefleet() -> bool:
    return get_browser_backend() == "browsefleet"
```

`linkedin_mcp_server/config/loaders.py`: add `BROWSEFLEET_URL`, `BROWSEFLEET_TOKEN`, `BROWSEFLEET_PROFILE_ID` to `EnvironmentKeys`, map to `config.browser`.

`linkedin_mcp_server/tool_registry.py:_get_extractor_for_tool` and `scraping/extractor.py`: branch:
```python
if should_use_browsefleet():
    from linkedin_mcp_server.core.browsefleet_browser import BrowseFleetBrowserManager
    browser = BrowseFleetBrowserManager(profile_id=config.browser.browsefleet_profile_id)
else:
    browser = ObscuraBrowserManager(...)
```

### 3.3 Cookie flow

```
Home Brave-Origin (14 cookies, li_at 152 chars)
  │
  ├─► linkedin-lyr --import-from-browser  (file: ~/.linkedin-lyr/cookies.json)  [already fixed]
  │
  ├─► BrowseFleet: POST /v1/sessions {cookies, profileId:"linkedin-ishan"}
  │     └─► CloakBrowser context with .linkedin.com cookies, stealth full
  │           └─► wss://browsefleet.ishanparihar.com/cdp/<id>  (VPS connects via Cloudflare)
  │
  └─► VPS: BROWSEFLEET_PROFILE_ID=linkedin-ishan → auto-reuse, no file sync needed
       Subsequent calls: POST /v1/sessions {profileId} (no cookies) → persisted via Storage.getCookies
```

For flagged accounts: **first run** with `operatorMode:true` → open `viewerUrl` in a real browser, log into LinkedIn once natively, `POST /v1/sessions/:id/control {controlMode:"agent"}` → profile now holds a VPS-native `li_at` that never leaves the pool.

### 3.4 VPS thin-client env

On `hermes-vps`, `~/.linkedin-lyr/bf.env` (sourced by `common_utils.load_proxy_env`):
```
LINKEDIN_BROWSER_BACKEND=browsefleet
BROWSEFLEET_URL=https://browsefleet.ishanparihar.com
BROWSEFLEET_TOKEN=49f7c273ef86c3e7d108f1aa72682bc0
BROWSEFLEET_PROFILE_ID=linkedin-ishan
```

`hermes-vps` wrapper already exports `PATH` for `~/.local/bin`; `bf` CLI reads `~/.browsefleet.env` — keep both to avoid confusion (bf uses its own, linkedin-lyr uses bf.env).

---

## 4. Implementation Steps (2–3 days)

| Phase | Tasks | Verify |
|-------|-------|--------|
| **P0 — Stabilize** | Keep current fixes: Brave-Origin path, passive `--status` (`LINKEDIN_FORCE_LIVENESS_PROBE=1` opt-in), probe-free export/import, `linkedin-socks.service` (SOCKS5 → home Jio 122.161.66.212) proven, smoke-gate `linkedin` removed, `egress_proxies()` for `voyager_auth`/`obscura_cookie_import`. | `hermes-vps 'linkedin-lyr --status'` → `present/skipped_passive` |
| **P1 — Fleet** | ✅ Done: local Docker + Cloudflare Tunnel. Next: set `CLOAKBROWSER_LICENSE_KEY` if Pro, `docker compose pull && up -d` for updates. | `curl -H "x-api-key: $K" https://browsefleet.ishanparihar.com/health` → `ok` |
| **P2 — Plugin scaffold** | Create `browsefleet_browser.py`, update `browser_backend.py`+`config/loaders.py`+`common_utils`, add `tests/test_browsefleet_browser.py` mocking `POST /v1/sessions`. | `LINKEDIN_BROWSER_BACKEND=browsefleet uv run pytest tests/test_browsefleet_browser.py -q` |
| **P3 — Cookie bridge** | Test: `bf session create --profile linkedin-ishan` with 14 cookies → `GET /v1/sessions/:id` → `POST /v1/sessions/:id/actions [{"navigate":"https://www.linkedin.com/feed"}]` → no `uas/login` redirect. | `hermes-vps 'BROWSEFLEET_PROFILE_ID=... linkedin-lyr get_my_profile'` returns JSON, not `Too many redirects` |
| **P4 — Profiles** | `bf` CLI: `curl -X POST https://browsefleet.ishanparihar.com/v1/profiles -H "x-api-key: $K" -d '{"name":"linkedin-ishan"}'` → store `BROWSEFLEET_PROFILE_ID`. Test release persistence (cookies survive `release`→`create {profileId}`). | Second `get_my_profile` without re-injecting cookies succeeds |
| **P5 — Cutover** | Set `LINKEDIN_BROWSER_BACKEND=browsefleet` in VPS `bf.env`, keep `obscura` fallback (`LINKEDIN_BROWSER_BACKEND=obscura` reverts). Update `vps-update.sh` to preserve `bf.env`. | `hermes-vps 'linkedin-lyr get_my_profile; linkedin-lyr search_people --keywords rust'` both 200 |

---

## 5. Risks & Mitigations

- **RAM:** BrowseFleet needs 2 GB shm + 200–500 MB per session. Local 46 GB host fits 10 sessions; RackNerd 2.4 GB as thin client uses 0 MB (only HTTP). Monitor `docker stats` and `bf health.activeSessions`.
- **API key:** `API_KEYS` in `.env` and `~/.browsefleet.env` are 0600. Rotate via env and `docker compose up -d`.
- **CDP via Cloudflare:** `CDP_EXTERNAL_SCHEME=wss`, `PORT=443`, `originRequest.noTLSVerify` already in `~/.cloudflared/main.yml` — verifies `wss://browsefleet.ishanparihar.com/cdp/<id>` works for `puppeteer-core` (tested via `bf session create` → `websocketUrl`).
- **Flagged account:** First BrowseFleet login should be native via `viewerUrl` (operatorMode), not cookie replay, to establish a clean CloakBrowser fingerprint. Cookie replay from Brave can then be discontinued.

---

## 6. Next Commands (copy-paste)

```bash
# Fleet health (local or VPS)
curl -H "x-api-key: 49f7c273ef86c3e7d108f1aa72682bc0" https://browsefleet.ishanparihar.com/health
bf sessions   # via hermes-vps (uses ~/.browsefleet.env)

# Plugin dev (local)
cd ~/Documents/github/my-projects/agentic-utility/social-media/linkedin-lyr
uv run ruff check linkedin_mcp_server/core/browsefleet_browser.py
uv run pytest tests/test_browsefleet_browser.py -q

# End-to-end on VPS (after P3)
hermes-vps 'BROWSEFLEET_PROFILE_ID=... linkedin-lyr get_my_profile 2>&1 | head -20'
```

## 7. Production validation (2026-08-28)

| Check | Result | Notes |
|-------|--------|-------|
| **Fleet container** (`browsefleet-browsefleet-1`) | ✓ | 0/30 sessions, `uptime 30+ min`, `MAX_CONCURRENT_SESSIONS=30`, `STEALTH_DEFAULT=full` |
| **Public endpoint** (`https://browsefleet.ishanparihar.com/health`) | ✓ | Returns `{"status":"ok","version":"1.1.0"}` over Cloudflare tunnel |
| **Local endpoint** (`http://localhost:3000/health`) | ✓ | Same response (loopback) |
| **Cloudflare tunnel** (`~/.config/systemd/user/browsefleet-tunnel.service`) | ✓ | 4 QUIC connections, user systemd, auto-restart on failure. Fixed `credentials-file: ~/.cloudflared/...json` (was `/etc/cloudflared/...` 0600 unreadable by `cloudflared` user) |
| **Public session create + WSS connect** | ✓ | `wss://browsefleet.ishanparihar.com/cdp/<id>?apiKey=...` returns and Playwright `connect_over_cdp` succeeds. 3-step fallback: fleet URL → ws://localhost:3000 → local fallback |
| **Brave-Origin cookie extraction** | ✓ | 15 cookies (li_at, JSESSIONID, bcookie, bscookie, dfpfpt, fptctx2, g_state, lang, li_theme, li_theme_set, liap, lidc, sdui_ver, timezone, PLAY_SESSION) |
| **Cookie priority (live vs portable)** | ✓ | Probes both with `voyager_auth.probe_session`; prefers alive, falls back to freshest when both stale |
| **Test suite** `tests/test_browsefleet_browser.py` | ✓ | 13/13 pass (backend selection, config validation, manager init, CDP token-injection, env loading) |
| **Lint** `uv run ruff check .` | ✓ | Clean |
| **Smoke test** `scripts/smoke_browsefleet.py` | ✓ 3/4 | Fleet / Tunnel / Cookies pass; LinkedIn content depends on a live session (see below) |
| **Brave cookie replay to LinkedIn** | ✗ (expected) | LinkedIn's JA3/TLS fingerprint differs on CloakBrowser vs Brave → redirect loop, `probe_session` returns `dead`. The smoke fails on [4] because the `li_at AQEDARtoEt0AdOso...` was already server-side revoked by prior replay attempts. Re-login natively required once (see P3 of the plan, "operatorMode" + `viewerUrl`). |

### Production setup checklist for a new install

1. **Fleet host** (one-time):
   - `git clone https://github.com/ishan-parihar/browsefleet ~/browsefleet && cd ~/browsefleet && cp .env.example .env`
   - In `.env`: set `API_KEYS=<token1>,<token2>,...`, `CDP_EXTERNAL_HOST=<your-tunnel-domain>`, `CDP_EXTERNAL_SCHEME=wss`, `CLOAKBROWSER_LICENSE_KEY=<pro key>` (optional but recommended for `unlimited concurrent sessions + 71 patches`).
   - `docker compose up -d` → healthy on `127.0.0.1:3000`.
2. **Tunnel** (one-time, run as the user owning the tunnel, not `cloudflared` user):
   - `cloudflared tunnel create main` → save the credentials JSON to `~/.cloudflared/<UUID>.json` (0600).
   - `~/.cloudflared/main.yml` with `credentials-file: /home/<user>/.cloudflared/<UUID>.json` and `ingress` mapping your hostname to `http://localhost:3000`.
   - `~/.config/systemd/user/browsefleet-tunnel.service` with `ExecStart=/usr/bin/cloudflared --config %h/.cloudflared/main.yml tunnel run`; `systemctl --user enable --now browsefleet-tunnel`.
3. **VPS thin-client**:
   - `mkdir -p ~/.linkedin-lyr` and write `~/.linkedin-lyr/bf.env` (0600):
     ```
     LINKEDIN_BROWSER_BACKEND=browsefleet
     BROWSEFLEET_URL=https://browsefleet.ishanparihar.com
     BROWSEFLEET_TOKEN=<token>
     BROWSEFLEET_PROFILE_ID=linkedin-ishan
     ```
   - One native login on the fleet: `curl -X POST https://<fleet>/v1/profiles -H "x-api-key: $TOKEN" -d '{"name":"linkedin-ishan"}'` returns a profileId; create an operator session with that profileId; open `viewerUrl` in a real browser and complete the LinkedIn login once. The profile persists.
4. **Verify**:
   - `uv run python scripts/smoke_browsefleet.py` (passes 4/4 once profile is seeded; otherwise 3/4 with the documented LinkedIn-cookie limitation).
   - `linkedin-lyr get_my_profile` returns JSON in TOON format.


**Decision needed:** Start **P2** (scaffold `browsefleet_browser.py` locally) or **P1 hardening** (`CLOAKBROWSER_LICENSE_KEY` Pro) first?

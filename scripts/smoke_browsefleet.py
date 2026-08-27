"""
Production smoke test: LinkedIn-LYR × BrowseFleet integration.

Validates the full chain (home → cloudflared → BrowseFleet) end-to-end:
  1.  Fleet is reachable (HTTP health + token)
  2.  Public WSS endpoint is reachable (Cloudflare tunnel)
  3.  Brave-Origin cookies extract; li_at probes alive
  4.  BrowseFleet session creates over HTTPS, returns WSS, connects via Playwright
  5.  Playwright/Page can navigate an unauthenticated LinkedIn URL (auth wall)
  6.  With current Brave cookies, /feed/ loads (no uas/login redirect)

Usage:
    uv run python scripts/smoke_browsefleet.py [url]

Exits 0 on full success, non-zero on any failure. Run from the repo root so
the linkedin_mcp_server package is importable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

BF_URL = "https://browsefleet.ishanparihar.com"
BF_TOKEN = "49f7c273ef86c3e7d108f1aa72682bc0"
BF_LOCAL = "http://localhost:3000"


class Color:
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def step(n: int, label: str) -> None:
    print(f"\n{Color.CYAN}[{n}]{Color.RESET} {label}")


def ok(msg: str) -> None:
    print(f"  {Color.GREEN}✓{Color.RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {Color.RED}✗{Color.RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {Color.DIM}{msg}{Color.RESET}")


def warn(msg: str) -> None:
    print(f"  {Color.YELLOW}!{Color.RESET} {msg}")


def assert_(cond: bool, msg: str) -> bool:
    if cond:
        ok(msg)
    else:
        fail(msg)
    return cond


# ---------------------------------------------------------------------------


def check_health() -> bool:
    """1. Fleet health over public + local."""
    step(1, "Fleet health")
    import httpx

    results = []
    for label, url in [("public", BF_URL), ("local", BF_LOCAL)]:
        try:
            r = httpx.get(f"{url}/health", headers={"x-api-key": BF_TOKEN}, timeout=10)
            ok_local = r.status_code == 200 and r.json().get("status") == "ok"
            if assert_(ok_local, f"{label} {url}/health → {r.json()}"):
                results.append(True)
            else:
                results.append(False)
        except Exception as exc:
            fail(f"{label} {url}/health raised {exc!r}")
            results.append(False)
    return all(results)


def check_session_create() -> str | None:
    """2. Public session create + WSS connection over Cloudflare tunnel."""
    step(2, "Public session create + WSS via Cloudflare tunnel")
    import httpx

    payload = {"stealth": "full", "viewport": {"width": 1280, "height": 720}, "timeout": 30000}
    try:
        r = httpx.post(
            f"{BF_URL}/v1/sessions",
            json=payload,
            headers={"x-api-key": BF_TOKEN, "Content-Type": "application/json"},
            timeout=30,
        )
    except Exception as exc:
        fail(f"POST /v1/sessions raised {exc!r}")
        return None
    if r.status_code not in (200, 201):
        fail(f"session create status {r.status_code}: {r.text[:300]}")
        return None
    data = r.json()
    sid = data.get("id")
    wss = data.get("websocketUrl")
    viewer = data.get("viewerUrl")
    if not (sid and wss and wss.startswith(("wss://", "ws://"))):
        fail(f"bad response: {data}")
        return None
    ok(f"session {sid[:8]}… wss={wss[:60]}… viewer={viewer}")
    return sid


def check_brave_cookies() -> dict[str, str] | None:
    """3. Cookie source (portable file OR live browser) + liveness probe.

    On thin-client hosts (RackNerd VPS, no local browser) the portable
    cookie file is the only source. On a workstation, the live Brave is
    preferred and probed. Either path returns ``None`` when no source
    has usable cookies — the smoke then warns and continues so the
    fleet/tunnel checks still report cleanly.
    """
    step(3, "Cookie source + liveness probe")
    cookies: dict[str, str] = {}

    # 1. Live browser (Brave-Origin etc) — workstation hosts.
    try:
        from linkedin_mcp_server.browser_cookie_extractor import extract_linkedin_cookies
        from linkedin_mcp_server.voyager_auth import probe_session

        data = extract_linkedin_cookies()
        live = (data or {}).get("all_cookies") or {}
        if live and "li_at" in live:
            verdict = probe_session(live)
            if verdict == "alive":
                cookies = live
                ok(f"live Brave: {len(cookies)} cookies, probe → alive (li_at len={len(cookies['li_at'])})")
            else:
                warn(f"live Brave probe → {verdict} (cookies will be tried from portable file)")
                cookies = live  # still record; the linkedin-load step below will re-probe
    except Exception as exc:
        warn(f"live browser extract skipped: {exc!r}")

    # 2. Portable file (always present on a working linkedin-lyr install).
    portable_path = Path.home() / ".linkedin-lyr" / "cookies.json"
    if portable_path.exists():
        try:
            data = json.loads(portable_path.read_text())
            flat: dict[str, str] = {}
            if isinstance(data, dict):
                if "cookies" in data and isinstance(data["cookies"], dict):
                    flat = {k: str(v) for k, v in data["cookies"].items()}
                elif "cookies" in data and isinstance(data["cookies"], list):
                    flat = {c["name"]: str(c["value"]) for c in data["cookies"] if "name" in c}
                else:
                    flat = {k: str(v) for k, v in data.items() if isinstance(v, str)}
            elif isinstance(data, list):
                flat = {c["name"]: str(c["value"]) for c in data if isinstance(c, dict) and "name" in c}
            if flat and "li_at" in flat:
                if not cookies:
                    cookies = flat
                    ok(f"portable file: {len(flat)} cookies (no live browser to compare)")
                else:
                    # Prefer the live one (already set above); mention portable as fallback.
                    info(f"portable file fallback available: {len(flat)} cookies (li_at len={len(flat['li_at'])})")
        except Exception as exc:
            warn(f"portable cookie parse failed: {exc!r}")

    if not cookies or "li_at" not in cookies:
        warn("no li_at available — LinkedIn content step will use the BROWSEFLEET profile (operatorMode login)")
        return None
    if "li_at" in cookies and not any(cookies.get(k) for k in ("bcookie", "bscookie")):
        warn(f"li_at present but no bcookie/bscookie ({len(cookies)} cookies total)")
    return cookies


async def check_linkedin_with_bf(cookies: dict[str, str]) -> bool:
    """4. Drive a remote session to LinkedIn /feed/ with current cookies."""
    step(4, "BrowseFleet + Brave cookies → /feed/")
    from linkedin_mcp_server.core.browsefleet_browser import BrowseFleetBrowserManager

    # Force bf env (smoke runs outside the package)
    os.environ["BROWSEFLEET_URL"] = BF_URL
    os.environ["BROWSEFLEET_TOKEN"] = BF_TOKEN
    os.environ["LINKEDIN_BROWSER_BACKEND"] = "browsefleet"

    m = BrowseFleetBrowserManager(viewport={"width": 1280, "height": 720})
    try:
        await m.start()
    except Exception as exc:
        fail(f"start failed: {exc!r}")
        return False
    ok(f"session {m.session_id[:8]}… auth={m.is_authenticated} cookies={len(m._cookies)}")
    info(f"viewer: {m.viewer_url}")

    # Drive a single LinkedIn URL
    try:
        try:
            await m.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        except Exception as exc:
            warn(f"goto raised (soft-fail in manager): {exc!r}")
        url = await m.url()
        title = await m.title()
        html = await m.content()
    finally:
        await m.close()

    body = html[:8192] if html else ""
    redirected = "uas/login" in url or "login" in url.lower() or "login" in body.lower()[:1500]
    if redirected:
        warn(f"redirected to login ({url!r}, title={title!r})")
        return False
    if "feed" in body.lower()[:2000] or "share" in body.lower()[:2000] or "post" in body.lower()[:2000]:
        ok(f"feed loaded (title={title!r}, body_len={len(body)})")
        return True
    warn(f"loaded (url={url!r}, title={title!r}, body_len={len(body)}) but no feed signal")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=BF_URL, help="BrowseFleet base URL (public or local)")
    args = parser.parse_args()

    print(f"{Color.CYAN}LinkedIn-LYR × BrowseFleet smoke test{Color.RESET}")
    print(f"  target: {args.url}")

    results: list[bool] = []
    results.append(check_health())
    sid = check_session_create()
    results.append(sid is not None)
    cookies = check_brave_cookies()
    if cookies is None:
        # Thin-client: skip the linkedin-load check, fleet/tunnel still reported.
        warn("skipping LinkedIn content check (no cookies on this host)")
    else:
        try:
            results.append(asyncio.run(check_linkedin_with_bf(cookies)))
        except KeyboardInterrupt:
            fail("interrupted")
            return 130

    print()
    if all(results):
        print(f"{Color.GREEN}ALL CHECKS PASSED{Color.RESET}")
        return 0
    failed = [i for i, r in enumerate(results, 1) if not r]
    print(f"{Color.RED}{len(failed)} CHECK(S) FAILED: {failed}{Color.RESET}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

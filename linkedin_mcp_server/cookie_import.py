"""
Multi-browser cookie import for LinkedIn MCP Server.

Primary authentication method: Import cookies from user's browser session.
Supports ALL major browsers: Brave, Chrome, Edge, Firefox, Zen, Helium,
Chromium, Opera, Arc, Vivaldi, LibreWolf, Waterfox, and more.

This bypasses LinkedIn's aggressive bot detection that blocks automated browsers.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from linkedin_mcp_server.session_state import auth_root_dir

logger = logging.getLogger(__name__)

# ─── Cookie requirements ───────────────────────────────────────────────────
LINKEDIN_COOKIES = {"li_at", "jsessionid", "bcookie", "bscookie"}
REQUIRED_COOKIES = {"li_at"}

# ─── Browser engine types ──────────────────────────────────────────────────
BrowserEngine = Literal["chromium", "firefox", "safari"]


@dataclass
class BrowserProfile:
    """Describes a browser's cookie DB path, executable paths, and engine."""

    name: str
    engine: BrowserEngine
    cookie_db_paths: list[str]  # relative to browser profile dir
    profile_dir_paths: list[str]  # relative to home/config dirs
    executable_paths: list[str]
    cdp_flag: str = "--remote-debugging-port={port}"
    cdp_process_pattern: str = ""  # regex-like substring for process detection
    description: str = ""
    # For Firefox: uses cookies.sqlite, not Chrome-format Cookies db
    cookie_db_name: str = "Cookies"


# ─── Browser registry ──────────────────────────────────────────────────────
# Ordered by priority (most recommended first)
BROWSER_REGISTRY: dict[str, BrowserProfile] = {
    "brave": BrowserProfile(
        name="Brave",
        engine="chromium",
        description="Recommended — best bot-detection resistance",
        cookie_db_paths=["Default/Cookies", "Profile */Cookies"],
        profile_dir_paths=[
            ".config/BraveSoftware/Brave-Browser",
            ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",
            "AppData/Local/BraveSoftware/Brave-Browser/User Data",  # Windows
            "Library/Application Support/BraveSoftware/Brave-Browser",  # macOS
        ],
        executable_paths=[
            "/opt/brave-bin/brave",
            "/usr/bin/brave-browser",
            "/usr/bin/brave",
            "/snap/bin/brave",
            "/usr/lib/brave/brave",
            "C:/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe",
            "C:/Program Files (x86)/BraveSoftware/Brave-Browser/Application/brave.exe",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ],
        cdp_process_pattern="brave.*remote-debugging",
    ),
    "brave-origin": BrowserProfile(
        name="Brave-Origin",
        engine="chromium",
        description="Brave-based fork (Omarchy hosts browser at Brave-Origin)",
        cookie_db_paths=["Default/Cookies", "Profile */Cookies"],
        profile_dir_paths=[
            ".config/BraveSoftware/Brave-Origin",
        ],
        executable_paths=[
            "/usr/bin/brave",
            "/usr/bin/brave-browser",
        ],
        cdp_process_pattern="brave.*remote-debugging",
    ),
    "zen": BrowserProfile(
        name="Zen Browser",
        engine="firefox",
        description="Firefox-based, privacy-focused",
        cookie_db_paths=["Default/cookies.sqlite"],
        profile_dir_paths=[
            ".zen",
            ".config/zen",
            "AppData/Roaming/Zen",
            "Library/Application Support/Zen",
        ],
        executable_paths=[
            "/opt/zen-browser-bin/zen-bin",
            "/opt/zen-browser-bin/zen",
            "/usr/bin/zen-browser",
            "/opt/zen/zen",
            "/usr/bin/zen",
            "/usr/local/bin/zen",
            "C:/Program Files/Zen Browser/zen.exe",
            "/Applications/Zen Browser.app/Contents/MacOS/zen",
        ],
        cdp_process_pattern="",  # Firefox doesn't support CDP
        cookie_db_name="cookies.sqlite",
    ),
    "chrome": BrowserProfile(
        name="Google Chrome",
        engine="chromium",
        description="Most widely used",
        cookie_db_paths=["Default/Cookies", "Profile */Cookies"],
        profile_dir_paths=[
            ".config/google-chrome",
            ".var/app/com.google.Chrome/config/google-chrome",
            "AppData/Local/Google/Chrome/User Data",
            "Library/Application Support/Google/Chrome",
        ],
        executable_paths=[
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chrome",
            "/snap/bin/chromium",
            "/opt/google/chrome/chrome",
            "C:/Program Files/Google/Chrome/Application/chrome.exe",
            "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ],
        cdp_process_pattern="chrome.*remote-debugging",
    ),
    "edge": BrowserProfile(
        name="Microsoft Edge",
        engine="chromium",
        description="Built into Windows, Chromium-based",
        cookie_db_paths=["Default/Cookies", "Profile */Cookies"],
        profile_dir_paths=[
            ".config/microsoft-edge",
            "AppData/Local/Microsoft/Edge/User Data",
            "Library/Application Support/Microsoft Edge",
        ],
        executable_paths=[
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/usr/bin/edge",
            "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
            "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ],
        cdp_process_pattern="msedge.*remote-debugging",
    ),
    "firefox": BrowserProfile(
        name="Mozilla Firefox",
        engine="firefox",
        description="Open-source, non-Chromium",
        cookie_db_paths=["cookies.sqlite"],
        profile_dir_paths=[
            ".mozilla/firefox",
            ".var/app/org.mozilla.firefox/.mozilla/firefox",
            "AppData/Roaming/Mozilla/Firefox/Profiles",
            "Library/Application Support/Firefox/Profiles",
        ],
        executable_paths=[
            "/usr/bin/firefox",
            "/usr/bin/firefox-esr",
            "/snap/bin/firefox",
            "/opt/firefox/firefox",
            "C:/Program Files/Mozilla Firefox/firefox.exe",
            "C:/Program Files (x86)/Mozilla Firefox/firefox.exe",
            "/Applications/Firefox.app/Contents/MacOS/firefox",
        ],
        cdp_process_pattern="",
        cookie_db_name="cookies.sqlite",
    ),
    "librewolf": BrowserProfile(
        name="LibreWolf",
        engine="firefox",
        description="Hardened Firefox fork",
        cookie_db_paths=["cookies.sqlite"],
        profile_dir_paths=[
            ".librewolf",
            ".var/app/io.gitlab.librewolf-community/.librewolf",
        ],
        executable_paths=[
            "/usr/bin/librewolf",
            "/usr/local/bin/librewolf",
            "/opt/librewolf/librewolf",
        ],
        cdp_process_pattern="",
        cookie_db_name="cookies.sqlite",
    ),
    "waterfox": BrowserProfile(
        name="Waterfox",
        engine="firefox",
        description="Privacy-focused Firefox fork",
        cookie_db_paths=["cookies.sqlite"],
        profile_dir_paths=[
            ".waterfox",
            "AppData/Roaming/Waterfox/Profiles",
            "Library/Application Support/Waterfox/Profiles",
        ],
        executable_paths=[
            "/usr/bin/waterfox",
            "/opt/waterfox/waterfox",
            "C:/Program Files/Waterfox/waterfox.exe",
            "/Applications/Waterfox.app/Contents/MacOS/waterfox",
        ],
        cdp_process_pattern="",
        cookie_db_name="cookies.sqlite",
    ),
    "helium": BrowserProfile(
        name="Helium",
        engine="chromium",
        description="Lightweight Chromium-based browser",
        cookie_db_paths=["Default/Cookies", "Profile */Cookies"],
        profile_dir_paths=[
            ".config/net.imput.helium",
            ".config/Helium",
            ".var/app/io.helium.browser/config/Helium",
            "AppData/Local/Helium/User Data",
            "Library/Application Support/Helium",
        ],
        executable_paths=[
            "/opt/helium-browser-bin/helium",
            "/opt/helium-browser-bin/helium-wrapper",
            "/usr/bin/helium-browser",
            "/usr/bin/helium",
            "/opt/helium/helium",
            "/usr/local/bin/helium",
        ],
        cdp_process_pattern="helium.*remote-debugging",
    ),
    "chromium": BrowserProfile(
        name="Chromium",
        engine="chromium",
        description="Open-source Chromium",
        cookie_db_paths=["Default/Cookies", "Profile */Cookies"],
        profile_dir_paths=[
            ".config/chromium",
            ".var/app/org.chromium.Chromium/config/chromium",
            "AppData/Local/Chromium/User Data",
            "Library/Application Support/Chromium",
        ],
        executable_paths=[
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
            "/usr/lib/chromium/chromium",
            "C:/Program Files/Chromium/chrome.exe",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ],
        cdp_process_pattern="chromium.*remote-debugging",
    ),
    "opera": BrowserProfile(
        name="Opera",
        engine="chromium",
        description="Feature-rich Chromium browser",
        cookie_db_paths=["Default/Cookies", "Profile */Cookies"],
        profile_dir_paths=[
            ".config/opera",
            "AppData/Roaming/Opera Software/Opera Stable",
            "Library/Application Support/com.operasoftware.Opera",
        ],
        executable_paths=[
            "/usr/bin/opera",
            "/usr/bin/opera-stable",
            "/snap/bin/opera",
            "C:/Program Files/Opera/launcher.exe",
            "C:/Program Files (x86)/Opera/launcher.exe",
            "/Applications/Opera.app/Contents/MacOS/Opera",
        ],
        cdp_process_pattern="opera.*remote-debugging",
    ),
    "vivaldi": BrowserProfile(
        name="Vivaldi",
        engine="chromium",
        description="Customizable Chromium browser",
        cookie_db_paths=["Default/Cookies", "Profile */Cookies"],
        profile_dir_paths=[
            ".config/vivaldi",
            "AppData/Local/Vivaldi/User Data",
            "Library/Application Support/Vivaldi",
        ],
        executable_paths=[
            "/usr/bin/vivaldi",
            "/usr/bin/vivaldi-stable",
            "/opt/vivaldi/vivaldi",
            "C:/Program Files/Vivaldi/Application/vivaldi.exe",
            "C:/Program Files (x86)/Vivaldi/Application/vivaldi.exe",
            "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
        ],
        cdp_process_pattern="vivaldi.*remote-debugging",
    ),
    "arc": BrowserProfile(
        name="Arc Browser",
        engine="chromium",
        description="Modern Chromium-based browser (macOS)",
        cookie_db_paths=["Default/Cookies", "Profile */Cookies"],
        profile_dir_paths=[
            "Library/Application Support/Arc/User Data",
        ],
        executable_paths=[
            "/Applications/Arc.app/Contents/MacOS/Arc",
        ],
        cdp_process_pattern="Arc.*remote-debugging",
    ),
}


# ─── Platform helpers ──────────────────────────────────────────────────────
def _current_platform() -> Literal["linux", "darwin", "win32"]:
    return sys.platform


def _expand_browser_path(path: str) -> Path | None:
    """Resolve a browser executable path, handling platform-specific expansion."""
    p = Path(path).expanduser()
    if p.exists():
        return p
    return None


def _expand_profile_path(path: str) -> Path | None:
    """Resolve a browser profile directory path."""
    plat = _current_platform()
    if plat == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif plat == "darwin":
        base = Path.home()
    else:
        base = Path.home()

    candidate = base / path
    if candidate.exists():
        return candidate
    return None


# ─── Browser detection ─────────────────────────────────────────────────────
def detect_installed_browsers() -> list[tuple[str, BrowserProfile]]:
    """Detect which browsers from the registry are installed on this system.

    Returns list of (browser_id, profile) tuples, ordered by registry priority.
    """
    installed = []
    for browser_id, profile in BROWSER_REGISTRY.items():
        exe = find_browser_executable(profile)
        if exe is not None:
            installed.append((browser_id, profile))
    return installed


def find_browser_executable(profile: BrowserProfile) -> Path | None:
    """Find the first existing executable for a browser profile."""
    for path in profile.executable_paths:
        exe = _expand_browser_path(path)
        if exe is not None:
            return exe
    return None


def find_browser_profile_dir(profile: BrowserProfile) -> Path | None:
    """Find the first existing profile directory for a browser."""
    for path in profile.profile_dir_paths:
        p = _expand_profile_path(path)
        if p is not None:
            return p
    return None


def find_browser_cookie_db(browser_id: str) -> Path | None:
    """Find the cookie database for a specific browser."""
    prof = BROWSER_REGISTRY.get(browser_id)
    if prof is None:
        return None

    profile_dir = find_browser_profile_dir(prof)
    if profile_dir is None:
        return None

    # For Firefox-based browsers, find the default profile first
    if prof.engine == "firefox":
        # Firefox-style browsers use profiles.ini + cookies.sqlite in profile folders
        if browser_id in ("firefox", "librewolf", "waterfox", "zen"):
            return _find_firefox_cookie_db(profile_dir)
        else:
            # Zen and others might have a simpler structure
            for db_path in prof.cookie_db_paths:
                candidate = profile_dir / db_path
                if candidate.exists():
                    return candidate
            # Try searching subdirectories
            for subdir in profile_dir.iterdir():
                if subdir.is_dir():
                    for db_path in prof.cookie_db_paths:
                        candidate = subdir / db_path
                        if candidate.exists():
                            return candidate
            return None

    # Chromium-based: search profile dirs for Cookies file
    for db_path in prof.cookie_db_paths:
        candidate = profile_dir / db_path
        if candidate.exists():
            return candidate

    # Try with wildcard for Profile N
    if "Profile *" in prof.cookie_db_paths:
        for item in sorted(profile_dir.iterdir()):
            if item.is_dir() and item.name.startswith("Profile "):
                candidate = item / "Cookies"
                if candidate.exists():
                    return candidate

    return None


def _find_firefox_cookie_db(profiles_dir: Path) -> Path | None:
    """Find cookies.sqlite in a Firefox-style profile directory."""
    # Direct cookies.sqlite in the profiles dir
    direct = profiles_dir / "cookies.sqlite"
    if direct.exists():
        return direct

    # Search for profiles.ini to find the default profile
    ini_path = profiles_dir / "profiles.ini"
    if ini_path.exists():
        default_profile = _parse_firefox_profiles_ini(ini_path, profiles_dir)
        if default_profile:
            cookie_db = default_profile / "cookies.sqlite"
            if cookie_db.exists():
                return cookie_db

    # Search all subdirectories for cookies.sqlite
    for subdir in profiles_dir.iterdir():
        if subdir.is_dir():
            cookie_db = subdir / "cookies.sqlite"
            if cookie_db.exists():
                return cookie_db

    return None


def _parse_firefox_profiles_ini(ini_path: Path, base_dir: Path) -> Path | None:
    """Parse profiles.ini to find the default Firefox profile directory."""
    try:
        import configparser

        config = configparser.ConfigParser()
        config.read(str(ini_path))

        # Find the default profile
        for section in config.sections():
            if config.has_option(section, "Default") and config.getboolean(section, "Default"):
                profile_path = config.get(section, "Path")
                is_relative = config.get(section, "IsRelative", fallback="1") == "1"
                if is_relative:
                    return base_dir / profile_path
                return Path(profile_path)

        # If no default, try Profile0
        for section in config.sections():
            if section.startswith("Profile"):
                profile_path = config.get(section, "Path")
                is_relative = config.get(section, "IsRelative", fallback="1") == "1"
                if is_relative:
                    return base_dir / profile_path
                return Path(profile_path)
    except Exception:
        pass
    return None


# ─── Cookie extraction ─────────────────────────────────────────────────────
def _copy_db_to_temp(db_path: Path) -> Path:
    """Copy a locked SQLite DB to a temp file for safe reading."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=db_path.suffix)
    tmp_path = Path(tmp.name)
    tmp.close()
    shutil.copy2(db_path, tmp_path)
    return tmp_path


def extract_chromium_cookies(db_path: Path) -> dict[str, str]:
    """Extract LinkedIn cookies from a Chromium-style Cookies SQLite database.

    Chromium cookies may have encrypted values on Linux (using DPAPI on Windows,
    or libsecret on Linux). For simplicity, we extract plaintext values only.
    """
    cookies: dict[str, str] = {}
    tmp_path = _copy_db_to_temp(db_path)
    try:
        conn = sqlite3.connect(str(tmp_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name, value, encrypted_value FROM cookies WHERE host_key LIKE '%linkedin.com%'"
        )
        for name, value, encrypted_value in cursor.fetchall():
            if name in LINKEDIN_COOKIES:
                if value:
                    cookies[name] = value
                elif encrypted_value:
                    logger.debug("Skipping encrypted cookie: %s", name)
        conn.close()
    finally:
        tmp_path.unlink(missing_ok=True)
    return cookies


def extract_firefox_cookies(db_path: Path) -> dict[str, str]:
    """Extract LinkedIn cookies from Firefox cookies.sqlite.

    Firefox cookie schemas vary by version/branch:
    - Modern Firefox (127+): uses 'baseDomain' column
    - Zen, Floorp, older Firefox: uses 'host' column
    We detect which column exists at runtime.
    """
    cookies: dict[str, str] = {}
    tmp_path = _copy_db_to_temp(db_path)
    try:
        conn = sqlite3.connect(str(tmp_path))
        cursor = conn.cursor()

        # Detect which host-matching column exists in this DB
        cursor.execute("PRAGMA table_info(moz_cookies)")
        col_names = {row[1] for row in cursor.fetchall()}

        if "baseDomain" in col_names:
            host_col = "baseDomain"
        elif "host" in col_names:
            host_col = "host"
        else:
            logger.error("No host column found in Firefox moz_cookies table")
            conn.close()
            return cookies

        cursor.execute(
            f"SELECT name, value FROM moz_cookies WHERE {host_col} LIKE '%linkedin.com%'"
        )
        for name, value in cursor.fetchall():
            if name in LINKEDIN_COOKIES and value:
                cookies[name] = value
        conn.close()
    finally:
        tmp_path.unlink(missing_ok=True)
    return cookies


def extract_cookies_from_browser(browser_id: str) -> dict[str, str] | None:
    """Extract LinkedIn cookies from a specific browser.

    Returns None if browser not found or no LinkedIn cookies found.
    """
    profile = BROWSER_REGISTRY.get(browser_id)
    if profile is None:
        logger.error("Unknown browser: %s", browser_id)
        return None

    cookie_db = find_browser_cookie_db(browser_id)
    if cookie_db is None:
        logger.error("Cookie database not found for %s", browser_id)
        return None

    logger.info("Extracting cookies from %s: %s", browser_id, cookie_db)

    if profile.engine == "chromium":
        cookies = extract_chromium_cookies(cookie_db)
    elif profile.engine == "firefox":
        cookies = extract_firefox_cookies(cookie_db)
    else:
        logger.error("Unsupported browser engine: %s", profile.engine)
        return None

    if not cookies:
        logger.warning("No LinkedIn cookies found in %s", browser_id)
        return None

    # Check for required cookies
    missing = REQUIRED_COOKIES - set(cookies.keys())
    if missing:
        logger.warning("Missing required cookies: %s", missing)
        return None

    logger.info("Successfully extracted cookies from %s: %s", browser_id, list(cookies.keys()))
    return cookies


def auto_extract_cookies() -> dict[str, str] | None:
    """Auto-detect and extract cookies from available browsers.

    Tries browsers in priority order until LinkedIn cookies are found.
    """
    installed = detect_installed_browsers()
    if not installed:
        logger.error("No supported browsers found")
        return None

    logger.info("Found installed browsers: %s", [b for b, _ in installed])

    for browser_id, _ in installed:
        cookies = extract_cookies_from_browser(browser_id)
        if cookies:
            return cookies

    logger.error("No LinkedIn cookies found in any browser")
    return None


def save_cookies(cookies: dict[str, str], output_path: Path) -> None:
    """Save cookies to a JSON file in LinkedIn MCP server format."""
    cookie_list = []
    for name, value in cookies.items():
        cookie_list.append({
            "name": name,
            "value": value,
            "domain": ".linkedin.com",
            "path": "/",
            "expires": -1,  # Session cookie
            "httpOnly": name in ["li_at", "jsessionid", "bscookie"],
            "secure": True,
            "sameSite": "None",
        })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(cookie_list, indent=2))
    logger.info("Saved cookies to %s", output_path)


def import_cookies_for_linkedin(browser_id: str | None = None) -> Path | None:
    """Import LinkedIn cookies from browser and save for MCP server.

    Args:
        browser_id: Specific browser to use, or None for auto-detection

    Returns:
        Path to saved cookies file, or None if import failed
    """
    if browser_id:
        cookies = extract_cookies_from_browser(browser_id)
    else:
        cookies = auto_extract_cookies()

    if not cookies:
        return None

    output_path = auth_root_dir() / "cookies.json"
    save_cookies(cookies, output_path)
    return output_path


if __name__ == "__main__":
    # Test cookie extraction
    import sys

    if len(sys.argv) > 1:
        browser_id = sys.argv[1]
        print(f"Extracting cookies from {browser_id}...")
        cookies = extract_cookies_from_browser(browser_id)
    else:
        print("Auto-detecting browser...")
        cookies = auto_extract_cookies()

    if cookies:
        print(f"Extracted cookies: {list(cookies.keys())}")
        output_path = import_cookies_for_linkedin()
        if output_path:
            print(f"Saved to: {output_path}")
    else:
        print("Failed to extract cookies")
        sys.exit(1)

"""
Browser cookie extraction for LinkedIn MCP Server using browser_cookie3.

Supports extraction from Chromium-based and Firefox-based browsers including:
- Chrome, Edge, Brave, Arc, Chromium (Chromium-based)
- Firefox, Zen Browser, LibreWolf, Waterfox (Firefox-based)
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# LinkedIn domains to match
_LINKEDIN_DOMAINS = {"linkedin.com", ".linkedin.com", "www.linkedin.com", ".www.linkedin.com"}


def _is_linkedin_domain(domain: str) -> bool:
    """Check if domain is LinkedIn-related."""
    return domain in _LINKEDIN_DOMAINS or domain.endswith(".linkedin.com")


# Base directories for Chromium-based browsers
_CHROMIUM_BASE_DIRS: dict[str, str] = {
    "chrome": os.path.join("Google", "Chrome"),
    "arc": os.path.join("Arc", "User Data"),
    "edge": "Microsoft Edge",
    "brave": os.path.join("BraveSoftware", "Brave-Browser"),
    "brave-origin": os.path.join("BraveSoftware", "Brave-Origin"),
    "chromium": "Chromium",
    "opera": "Opera",
    "vivaldi": "Vivaldi",
    "zen": os.path.join("Zen", "Zen"),  # Zen Browser (Chromium-based)
}

# Firefox-based browsers
_FIREFOX_BASE_DIRS: dict[str, str] = {
    "firefox": "Firefox",
    "zen-browser": "Zen-Browser",  # Zen Browser (Firefox-based)
    "zen": "Zen-Browser",  # Zen Browser (Firefox-based) - alternate name
    "librewolf": "LibreWolf",
    "waterfox": "Waterfox",
}

# Default browser order for cookie extraction
_DEFAULT_BROWSER_ORDER = [
    "chrome",
    "edge",
    "firefox",
    "brave",
    "brave-origin",
    "chromium",
    "opera",
    "vivaldi",
    "zen",
    "zen-browser",
]


def _get_browser_order() -> list[str]:
    """Return browser extraction order, respecting LINKEDIN_BROWSER env var."""
    env = os.environ.get("LINKEDIN_BROWSER", "").strip().lower()
    if not env:
        return _DEFAULT_BROWSER_ORDER
    if env not in _CHROMIUM_BASE_DIRS and env not in _FIREFOX_BASE_DIRS:
        logger.warning("LINKEDIN_BROWSER='%s' is invalid, using default order", env)
        return _DEFAULT_BROWSER_ORDER
    return [env] + [b for b in _DEFAULT_BROWSER_ORDER if b != env]


def _iter_chrome_cookie_files(browser_name: str) -> list[str]:
    """Return cookie file paths for all Chrome profiles."""
    base_dir = _CHROMIUM_BASE_DIRS.get(browser_name)
    if base_dir is None:
        return []

    if sys.platform == "darwin":
        root = os.path.join(os.path.expanduser("~"), "Library", "Application Support", base_dir)
    elif sys.platform == "win32":
        if browser_name == "edge":
            root = os.path.join(
                os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data"
            )
        elif browser_name == "brave-origin":
            root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "BraveSoftware", "Brave-Origin")
        else:
            root = os.path.join(os.environ.get("LOCALAPPDATA", ""), base_dir)
    else:
        if browser_name == "edge":
            root = os.path.join(os.path.expanduser("~"), ".config", "microsoft-edge")
        elif browser_name == "brave-origin":
            root = os.path.join(os.path.expanduser("~"), ".config", "BraveSoftware", "Brave-Origin")
        else:
            root = os.path.join(os.path.expanduser("~"), ".config", base_dir)

    if not os.path.isdir(root):
        return []

    # Auto-discover: Default first, then Profile N sorted
    paths: list[str] = []
    default_cookies = os.path.join(root, "Default", "Cookies")
    if os.path.exists(default_cookies):
        paths.append(default_cookies)

    import glob

    profile_dirs = sorted(glob.glob(os.path.join(root, "Profile *")))
    for profile_dir in profile_dirs:
        cookie_file = os.path.join(profile_dir, "Cookies")
        if os.path.exists(cookie_file):
            paths.append(cookie_file)

    return paths


def _get_firefox_cookie_path(browser_name: str) -> str | None:
    """Return cookie database path for Firefox-based browsers."""
    base_dir = _FIREFOX_BASE_DIRS.get(browser_name)
    if base_dir is None:
        return None

    if sys.platform == "darwin":
        root = os.path.join(os.path.expanduser("~"), "Library", "Application Support", base_dir)
    elif sys.platform == "win32":
        root = os.path.join(os.environ.get("APPDATA", ""), base_dir)
    else:
        root = os.path.join(os.path.expanduser("~"), base_dir)

    if not os.path.isdir(root):
        return None

    # Find the default profile
    import glob

    profiles = sorted(glob.glob(os.path.join(root, "*/")))

    # Look for default-release or similar
    for profile in profiles:
        cookies_file = os.path.join(profile, "cookies.sqlite")
        if os.path.exists(cookies_file):
            return cookies_file

    return None


def extract_cookies_from_file(cookie_file_path: str) -> dict[str, Any] | None:
    """Extract LinkedIn cookies from a direct cookie file path.

    Supports JSON cookie files in LinkedIn MCP format.
    For SQLite databases, use browser_cookie3's built-in decryption instead.

    Args:
        cookie_file_path: Path to cookie file (cookies.json)

    Returns:
        Dictionary with cookies data or None if extraction failed
    """
    cookie_path = Path(cookie_file_path)
    if not cookie_path.exists():
        logger.error(f"Cookie file not found: {cookie_file_path}")
        return None

    # Only support JSON format for direct file import
    # For SQLite databases, use browser extraction with cookie_file parameter
    if cookie_path.suffix == ".json":
        try:
            with open(cookie_path) as f:
                cookies_data = json.load(f)

            # If it's already in LinkedIn MCP format, return it
            if isinstance(cookies_data, list) and all(isinstance(c, dict) for c in cookies_data):
                all_cookies = {
                    c.get("name"): c.get("value") for c in cookies_data if c.get("value")
                }
                return {"all_cookies": all_cookies, "source": f"file:{cookie_file_path}"}
        except Exception as e:
            logger.error(f"Failed to read JSON cookie file: {e}")
            return None

    logger.error(f"Unsupported cookie file format for direct import: {cookie_path.suffix}")
    logger.error("For SQLite databases, use browser extraction instead")
    return None


def _extract_cookies_from_jar(jar: Any, source: str = "unknown") -> dict[str, Any] | None:
    """Extract LinkedIn cookies from a cookie jar."""
    result: dict[str, str] = {}
    all_cookies: dict[str, str] = {}
    linkedin_cookie_count = 0

    for cookie in jar:
        domain = cookie.domain or ""
        if _is_linkedin_domain(domain):
            linkedin_cookie_count += 1
            if cookie.name and cookie.value:
                all_cookies[cookie.name] = cookie.value
                # Store specific important cookies
                if cookie.name in ["li_at", "bscookie", "bcookie"]:
                    result[cookie.name] = cookie.value

    # Accept any LinkedIn cookies, not just specific ones
    if all_cookies:
        cookies = {"all_cookies": all_cookies, "source": source}
        # Add important cookies if present
        if result:
            cookies.update(result)
        logger.info("Extracted %d total LinkedIn cookies from %s", len(all_cookies), source)
        return cookies

    logger.debug(
        "Cookie jar %s did not contain any LinkedIn cookies",
        source,
    )
    return None


def _extract_in_process() -> tuple[dict[str, Any] | None, list[str]]:
    """Extract cookies in the main process (required on macOS for Keychain access)."""
    try:
        import browser_cookie3
    except ImportError:
        logger.debug("browser_cookie3 not installed, skipping in-process extraction")
        return None, ["browser-cookie3 not installed"]

    browser_fns = {
        "chrome": browser_cookie3.chrome,
        "edge": browser_cookie3.edge,
        "firefox": browser_cookie3.firefox,
        "brave": browser_cookie3.brave,
        "brave-origin": browser_cookie3.brave,  # Brave Origin
        "chromium": browser_cookie3.chromium,
        "opera": browser_cookie3.opera,
        "vivaldi": browser_cookie3.vivaldi,
        "zen": browser_cookie3.firefox,  # Zen Browser (Firefox-based)
        "zen-browser": browser_cookie3.firefox,  # Zen Browser (Firefox-based)
    }

    attempts: list[str] = []
    diagnostics: list[str] = []

    for name in _get_browser_order():
        fn = browser_fns.get(name)
        if not fn:
            continue

        if name in _CHROMIUM_BASE_DIRS:
            # Chromium-based: iterate all profiles
            cookie_files = _iter_chrome_cookie_files(name)
            if not cookie_files:
                # No profile dirs found — try the default (no cookie_file arg)
                try:
                    jar = fn()
                except Exception as e:
                    logger.debug("%s in-process extraction failed: %s", name, e)
                    attempts.append("%s=%s" % (name, type(e).__name__))
                    diagnostics.append("%s: %s" % (name, e))
                    continue
                cookies = _extract_cookies_from_jar(jar, source="%s(in-process)" % name)
                if cookies:
                    logger.info("Found cookies in %s (in-process, default)", name)
                    return cookies, diagnostics
                attempts.append("%s=no-cookies" % name)
                continue

            for cookie_file in cookie_files:
                profile_name = os.path.basename(os.path.dirname(cookie_file))
                try:
                    jar = fn(cookie_file=cookie_file)
                except Exception as e:
                    logger.debug("%s[%s] in-process extraction failed: %s", name, profile_name, e)
                    attempts.append("%s[%s]=%s" % (name, profile_name, type(e).__name__))
                    diagnostics.append("%s[%s]: %s" % (name, profile_name, e))
                    continue
                cookies = _extract_cookies_from_jar(
                    jar, source="%s[%s](in-process)" % (name, profile_name)
                )
                if cookies:
                    logger.info("Found cookies in %s profile '%s' (in-process)", name, profile_name)
                    return cookies, diagnostics
                attempts.append("%s[%s]=no-cookies" % (name, profile_name))
        else:
            # Firefox-based: use default behavior
            try:
                jar = fn()
            except Exception as e:
                logger.debug("%s in-process extraction failed: %s", name, e)
                attempts.append("%s=%s" % (name, type(e).__name__))
                diagnostics.append("%s: %s" % (name, e))
                continue
            cookies = _extract_cookies_from_jar(jar, source="%s(in-process)" % name)
            if cookies:
                logger.info("Found cookies in %s (in-process)", name)
                return cookies, diagnostics
            attempts.append("%s=no-cookies" % name)

    if attempts:
        logger.debug("In-process extraction attempts: %s", ", ".join(attempts))
    return None, diagnostics


def _extract_via_subprocess() -> tuple[dict[str, Any] | None, list[str]]:
    """Extract cookies via subprocess (fallback if in-process fails)."""
    extract_script = """
import glob, json, os, sys
try:
    import browser_cookie3
except ImportError:
    print(json.dumps({"error": "browser-cookie3 not installed"}))
    sys.exit(1)

CHROMIUM_BASE_DIRS = {
    "chrome": os.path.join("Google", "Chrome"),
    "arc": os.path.join("Arc", "User Data"),
    "edge": "Microsoft Edge",
    "brave": os.path.join("BraveSoftware", "Brave-Browser"),
    "brave-origin": os.path.join("BraveSoftware", "Brave-Origin"),
    "chromium": "Chromium",
    "opera": "Opera",
    "vivaldi": "Vivaldi",
    "zen": os.path.join("Zen", "Zen"),  # Zen Browser (Chromium-based)
}

FIREFOX_BASE_DIRS = {
    "firefox": "Firefox",
    "zen-browser": "Zen-Browser",  # Zen Browser (Firefox-based)
    "zen": "Zen-Browser",  # Zen Browser (Firefox-based) - alternate name
    "librewolf": "LibreWolf",
    "waterfox": "Waterfox",
}

LINKEDIN_DOMAINS = {"linkedin.com", ".linkedin.com", "www.linkedin.com", ".www.linkedin.com"}

def is_linkedin_domain(domain):
    return domain in LINKEDIN_DOMAINS or domain.endswith(".linkedin.com")

def iter_cookie_files(browser_name):
    # Handle Firefox-based browsers
    if browser_name in FIREFOX_BASE_DIRS:
        base_dir = FIREFOX_BASE_DIRS.get(browser_name)
        if base_dir is None:
            return []
        if sys.platform == "darwin":
            root = os.path.join(os.path.expanduser("~"), "Library", "Application Support", base_dir)
        elif sys.platform == "win32":
            root = os.path.join(os.environ.get("APPDATA", ""), base_dir)
        else:
            root = os.path.join(os.path.expanduser("~"), base_dir)
        if not os.path.isdir(root):
            return []
        # Find the default profile
        profiles = sorted(glob.glob(os.path.join(root, "*/")))
        for profile in profiles:
            cookies_file = os.path.join(profile, "cookies.sqlite")
            if os.path.exists(cookies_file):
                return [cookies_file]
        return []
    
    # Handle Chromium-based browsers
    base_dir = CHROMIUM_BASE_DIRS.get(browser_name)
    if base_dir is None:
        return []
    if sys.platform == "darwin":
        root = os.path.join(os.path.expanduser("~"), "Library", "Application Support", base_dir)
    elif sys.platform == "win32":
        if browser_name == "edge":
            root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data")
        elif browser_name == "brave-origin":
            root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "BraveSoftware", "Brave-Origin")
        else:
            root = os.path.join(os.environ.get("LOCALAPPDATA", ""), base_dir)
    else:
        if browser_name == "edge":
            root = os.path.join(os.path.expanduser("~"), ".config", "microsoft-edge")
        elif browser_name == "brave-origin":
            root = os.path.join(os.path.expanduser("~"), ".config", "BraveSoftware", "Brave-Origin")
        else:
            root = os.path.join(os.path.expanduser("~"), ".config", base_dir)
    if not os.path.isdir(root):
        return []
    paths = []
    d = os.path.join(root, "Default", "Cookies")
    if os.path.exists(d):
        paths.append(d)
    for pd in sorted(glob.glob(os.path.join(root, "Profile *"))):
        cf = os.path.join(pd, "Cookies")
        if os.path.exists(cf):
            paths.append(cf)
    return paths

def extract_from_jar(jar, name, profile=""):
    result = {}
    all_cookies = {}
    for cookie in jar:
        domain = cookie.domain or ""
        if is_linkedin_domain(domain):
            if cookie.name in ["li_at", "bscookie"]:
                result[cookie.name] = cookie.value
            if cookie.name and cookie.value:
                all_cookies[cookie.name] = cookie.value
    # Accept any LinkedIn cookies, not just specific ones
    if all_cookies:
        result["all_cookies"] = all_cookies
        result["browser"] = name
        if profile:
            result["profile"] = profile
        return result
    return None

DEFAULT_ORDER = ["chrome", "edge", "firefox", "brave", "brave-origin", "chromium", "opera", "vivaldi", "zen", "zen-browser"]
env_browser = os.environ.get("LINKEDIN_BROWSER", "").strip().lower()
if env_browser in set(DEFAULT_ORDER):
    browser_order = [env_browser] + [b for b in DEFAULT_ORDER if b != env_browser]
else:
    browser_order = DEFAULT_ORDER

browser_fns = {
    "chrome": browser_cookie3.chrome,
    "edge": browser_cookie3.edge,
    "firefox": browser_cookie3.firefox,
    "brave": browser_cookie3.brave,
    "brave-origin": browser_cookie3.brave,  # Brave Origin
    "chromium": browser_cookie3.chromium,
    "opera": browser_cookie3.opera,
    "vivaldi": browser_cookie3.vivaldi,
    "zen": browser_cookie3.firefox,  # Zen Browser (Firefox-based)
    "zen-browser": browser_cookie3.firefox,  # Zen Browser (Firefox-based)
}
attempts = []

for name in browser_order:
    fn = browser_fns.get(name)
    if not fn:
        continue
    if name in CHROMIUM_BASE_DIRS:
        cookie_files = iter_cookie_files(name)
        if not cookie_files:
            try:
                jar = fn()
            except Exception as exc:
                attempts.append(f"{name}={type(exc).__name__}: {exc}")
                continue
            r = extract_from_jar(jar, name)
            if r:
                print(json.dumps(r))
                sys.exit(0)
            attempts.append(f"{name}=no-cookies")
            continue
        for cf in cookie_files:
            pname = os.path.basename(os.path.dirname(cf))
            try:
                jar = fn(cookie_file=cf)
            except Exception as exc:
                attempts.append(f"{name}[{pname}]={type(exc).__name__}: {exc}")
                continue
            r = extract_from_jar(jar, name, pname)
            if r:
                print(json.dumps(r))
                sys.exit(0)
            attempts.append(f"{name}[{pname}=no-cookies")
    else:
        # Firefox-based browsers
        cookie_files = iter_cookie_files(name)
        if not cookie_files:
            try:
                jar = fn()
            except Exception as exc:
                attempts.append(f"{name}={type(exc).__name__}: {exc}")
                continue
            r = extract_from_jar(jar, name)
            if r:
                print(json.dumps(r))
                sys.exit(0)
            attempts.append(f"{name}=no-cookies")
            continue
        for cf in cookie_files:
            pname = os.path.basename(os.path.dirname(cf))
            try:
                jar = fn(cookie_file=cf)
            except Exception as exc:
                attempts.append(f"{name}[{pname}]={type(exc).__name__}: {exc}")
                continue
            r = extract_from_jar(jar, name, pname)
            if r:
                print(json.dumps(r))
                sys.exit(0)
            attempts.append(f"{name}[{pname}=no-cookies")

print(json.dumps({
    "error": "No LinkedIn cookies found in any browser. Make sure you are logged into linkedin.com.",
    "attempts": attempts,
}))
sys.exit(1)
"""

    diagnostics: list[str] = []

    def _run_extract_command(
        cmd: list[str],
        timeout: int,
        label: str,
    ) -> tuple[dict[str, Any] | None, bool]:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.debug("Cookie extraction %s timed out", label)
            return None, False
        except FileNotFoundError as exc:
            logger.debug("Cookie extraction %s launcher missing: %s", label, exc)
            return None, False

        output = result.stdout.strip()
        stderr = result.stderr.strip()
        if stderr:
            logger.debug("Cookie extraction stderr from %s: %s", label, stderr[:300])
        if not output:
            logger.debug("Cookie extraction from %s produced no stdout", label)
            return None, True

        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            logger.debug("Cookie extraction from %s produced invalid JSON: %s", label, exc)
            return None, True

        if "error" in data:
            logger.debug("Cookie extraction from %s returned error: %s", label, data["error"])
            diagnostics.append("%s: %s" % (label, data["error"]))
            return None, True

        return data, True

    # Try in-process first (required for macOS Keychain access)
    cookies, diags = _extract_in_process()
    if cookies:
        return cookies, diags

    # Fallback to subprocess extraction
    data, success = _run_extract_command(
        [sys.executable, "-c", extract_script], timeout=30, label="subprocess"
    )

    if data and success:
        return data, diagnostics

    return None, diagnostics


def extract_linkedin_cookies(browser: str = None, cookie_file: str = None) -> dict[str, Any] | None:
    """
    Extract LinkedIn cookies from browsers using browser_cookie3 or direct file.

    Args:
        browser: Specific browser to extract from (auto-detect if None)
        cookie_file: Direct path to cookie file (takes precedence over browser)

    Returns:
        Dictionary with cookies data or None if extraction failed
    """
    # If direct file path provided, use that
    if cookie_file:
        return extract_cookies_from_file(cookie_file)

    # Set browser preference if specified
    if browser:
        os.environ["LINKEDIN_BROWSER"] = browser

    # Otherwise use browser extraction
    cookies, diagnostics = _extract_in_process()
    if cookies:
        return cookies

    # Fallback to subprocess
    cookies, diagnostics = _extract_via_subprocess()
    if cookies:
        return cookies

    logger.warning("Failed to extract LinkedIn cookies from any browser")
    if diagnostics:
        logger.warning("Diagnostics: %s", ", ".join(diagnostics))
    return None


def format_cookies_for_linkedin_mcp(cookie_data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Format extracted cookies for LinkedIn MCP format.

    Args:
        cookie_data: Raw cookie data from browser_cookie3

    Returns:
        List of cookie dictionaries in LinkedIn MCP format
    """
    all_cookies = cookie_data.get("all_cookies", {})
    formatted_cookies = []

    from datetime import datetime, timedelta

    current_time = datetime.now()

    for name, value in all_cookies.items():
        if not value:
            continue

        # LinkedIn MCP cookie format
        cookie_dict = {
            "name": name,
            "value": value,
            "domain": ".linkedin.com",
            "path": "/",
            "expires": int((current_time + timedelta(days=30)).timestamp()),
            "secure": True,
            "httpOnly": name in ["li_at", "bscookie"],
            "sameSite": "None",
        }
        formatted_cookies.append(cookie_dict)

    return formatted_cookies

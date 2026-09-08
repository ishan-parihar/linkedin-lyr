"""
Obscura-compatible cookie import and validation system.

This module provides Playwright-free cookie import and validation using Obscura
for LinkedIn authentication. It bypasses the need for Playwright entirely.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

from curl_cffi import requests as cffi_req

from linkedin_mcp_server.cookie_import import (
    extract_cookies_from_browser,
    auto_extract_cookies,
)
from linkedin_mcp_server.session_state import auth_root_dir

logger = logging.getLogger(__name__)

# Required LinkedIn cookies for Obscura
_REQUIRED_COOKIES = {"li_at", "bscookie"}
_LINKEDIN_DOMAIN = ".linkedin.com"
_TEST_URL = "https://www.linkedin.com/feed/"


class ObscuraCookieManager:
    """Manage cookies for Obscura-based LinkedIn scraping."""

    def __init__(self, auth_root: Path | None = None):
        self.auth_root = auth_root or auth_root_dir()
        self.cookie_path = self.auth_root / "cookies.json"
        self._cookies: dict[str, str] = {}

    def load_cookies(self) -> dict[str, str]:
        """Load cookies from storage."""
        if not self.cookie_path.exists():
            return {}

        try:
            with open(self.cookie_path) as f:
                cookie_list = json.load(f)

            cookies = {}
            for cookie in cookie_list:
                name = cookie.get("name")
                value = cookie.get("value")
                if name and value:
                    cookies[name] = value

            self._cookies = cookies
            return cookies
        except Exception as e:
            logger.error("Failed to load cookies: %s", e)
            return {}

    def save_cookies(self, cookies: dict[str, str]) -> None:
        """Save cookies to storage in Obscura-compatible format."""
        cookie_list = []
        for name, value in cookies.items():
            cookie_list.append({
                "name": name,
                "value": value,
                "domain": _LINKEDIN_DOMAIN,
                "path": "/",
                "expires": -1,  # Session cookie
                "httpOnly": name in ["li_at", "jsessionid", "bscookie"],
                "secure": True,
                "sameSite": "None",
            })

        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cookie_path, "w") as f:
            json.dump(cookie_list, f, indent=2)

        self._cookies = cookies
        logger.info("Saved %d cookies to %s", len(cookies), self.cookie_path)

    def import_from_browser(self, browser_id: str | None = None) -> dict[str, str] | None:
        """Import cookies from browser."""
        if browser_id:
            cookies = extract_cookies_from_browser(browser_id)
        else:
            cookies = auto_extract_cookies()

        if not cookies:
            logger.error("No cookies extracted from browser")
            return None

        # Validate required cookies
        missing = _REQUIRED_COOKIES - set(cookies.keys())
        if missing:
            logger.error("Missing required cookies: %s", missing)
            return None

        self.save_cookies(cookies)
        return cookies

    def validate_cookies(self) -> bool:
        """Validate cookies structurally by default; HTTP check is explicit opt-in.

        #2601: replaying browser-minted cookies over plain HTTP is itself a
        revocation trigger (LinkedIn answers with 302 + ``li_at=delete me``),
        so the network test runs ONLY when ``LINKEDIN_HTTP_PROBE=1``. The
        default is the structural check (required cookies present), which is
        what every caller that merely gates a code path needs.
        """
        if not self._cookies:
            self._cookies = self.load_cookies()

        if not self._cookies:
            logger.error("No cookies to validate")
            return False

        # Check required cookies
        missing = _REQUIRED_COOKIES - set(self._cookies.keys())
        if missing:
            logger.error("Missing required cookies: %s", missing)
            return False

        if os.environ.get("LINKEDIN_HTTP_PROBE", "").strip() != "1":
            logger.info("Cookie validation passed (structural; HTTP probe disabled by default)")
            return True

        try:
            # Make a test request to LinkedIn feed
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }

            cookies = self._format_cookies_for_http()

            # curl_cffi impersonates a real Chrome TLS fingerprint; raw httpx
            # (Python TLS) replaying a browser-minted li_at is a bot-detection tell.
            # Reusing the same bundle the session was minted under keeps the probe
            # fingerprint-coherent with the rest of the stack.
            with cffi_req.Client(impersonate="chrome", timeout=10.0) as client:
                response = client.get(
                    _TEST_URL, headers=headers, cookies=cookies, follow_redirects=True
                )

            # Check if response indicates successful authentication
            # LinkedIn redirects to login page if not authenticated
            is_authenticated = (
                response.status_code == 200
                and "login" not in response.url.lower()
                and "checkpoint" not in response.url.lower()
            )

            if is_authenticated:
                logger.info("Cookie validation successful")
                return True
            else:
                logger.error(
                    "Cookie validation failed: status=%d, url=%s",
                    response.status_code,
                    response.url,
                )
                return False

        except Exception as e:
            logger.error("Cookie validation error: %s", e)
            return False

    def _format_cookies_for_http(self) -> dict[str, str]:
        """Format cookies for the HTTP client."""
        return self._cookies

    def get_obscura_cookie_args(self) -> list[str]:
        """Get cookie arguments for Obscura CLI."""
        if not self._cookies:
            self._cookies = self.load_cookies()

        args = []
        for name, value in self._cookies.items():
            args.extend(["--cookie", f"{name}={value}"])

        return args

    def clear_cookies(self) -> None:
        """Clear stored cookies."""
        if self.cookie_path.exists():
            self.cookie_path.unlink()
        self._cookies = {}
        logger.info("Cleared cookies")


class ObscuraSessionValidator:
    """Validate Obscura sessions without Playwright."""

    def __init__(self, cookie_manager: ObscuraCookieManager):
        self.cookie_manager = cookie_manager

    def validate_session(self) -> dict[str, Any]:
        """Validate the current session and return status."""
        cookies = self.cookie_manager.load_cookies()

        if not cookies:
            return {"valid": False, "reason": "no_cookies", "message": "No cookies found"}

        # Check required cookies
        missing = _REQUIRED_COOKIES - set(cookies.keys())
        if missing:
            return {
                "valid": False,
                "reason": "missing_cookies",
                "message": f"Missing required cookies: {missing}",
                "missing": list(missing),
            }

        # #2601: structural validation only — no HTTP replay of these
        # cookies (it is itself a revocation trigger). "valid" here means
        # "the jar is present and complete", not "LinkedIn confirmed it".
        is_valid = self.cookie_manager.validate_cookies()

        if is_valid:
            return {
                "valid": True,
                "reason": "cookies_present",
                "message": "Session jar present and structurally valid (liveness is proven in-browser)",
                "cookies": len(cookies),
            }
        else:
            return {
                "valid": False,
                "reason": "missing_cookies",
                "message": "Cookie jar is missing required authentication cookies",
            }


def import_and_validate_browser_session(
    browser_id: str | None = None, auth_root: Path | None = None
) -> dict[str, Any]:
    """Import and validate a browser session in one step."""
    manager = ObscuraCookieManager(auth_root)
    validator = ObscuraSessionValidator(manager)

    # Import cookies
    cookies = manager.import_from_browser(browser_id)
    if not cookies:
        return {
            "success": False,
            "reason": "import_failed",
            "message": "Failed to import cookies from browser",
        }

    # Validate session
    validation = validator.validate_session()

    return {
        "success": validation["valid"],
        "reason": validation.get("reason"),
        "message": validation.get("message"),
        "cookies_imported": len(cookies),
        "validation": validation,
    }


if __name__ == "__main__":
    # Test cookie import and validation
    import sys

    manager = ObscuraCookieManager()

    if len(sys.argv) > 1 and sys.argv[1] == "import":
        browser = sys.argv[2] if len(sys.argv) > 2 else None
        result = import_and_validate_browser_session(browser)
        print(json.dumps(result, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "validate":
        validator = ObscuraSessionValidator(manager)
        result = validator.validate_session()
        print(json.dumps(result, indent=2))
    else:
        # Show status
        cookies = manager.load_cookies()
        print(f"Cookies loaded: {len(cookies)}")
        if cookies:
            validator = ObscuraSessionValidator(manager)
            result = validator.validate_session()
            print(f"Validation: {json.dumps(result, indent=2)}")

"""
Enhanced cookie management and session handling for Obscura.

Provides improved cookie management, session persistence, and optimization
specifically designed for Obscura's lightweight architecture.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional
import hashlib
from collections import defaultdict

from linkedin_mcp_server.cookie_import import (
    auto_extract_cookies,
    extract_cookies_from_browser,
)
from linkedin_mcp_server.core.obscura_error_handling import (
    ObscuraCookieError,
    handle_obscura_error,
)

logger = logging.getLogger(__name__)

# Required LinkedIn cookies for authentication
REQUIRED_COOKIES = {"li_at", "bscookie"}
# Optional but recommended cookies
RECOMMENDED_COOKIES = {"li_rm", "JSESSIONID", "lidc", "lang", "timezone"}


class ObscuraCookieManager:
    """Enhanced cookie manager for Obscura with optimization features."""

    def __init__(self, cookie_file: Optional[Path] = None):
        self.cookie_file = cookie_file or Path.home() / ".linkedin-lyr" / "cookies.json"
        self._cookies: dict[str, str] = {}
        self._cookie_metadata: dict = {}
        self._last_validation: Optional[float] = None
        self._validation_interval: float = 300.0  # 5 minutes

        logger.info("Obscura cookie manager initialized with cookie file: %s", self.cookie_file)

    async def load_cookies(self) -> dict[str, str]:
        """Load cookies from file or browser extraction."""
        # Try file first
        if self.cookie_file.exists():
            try:
                file_cookies = await self._load_from_file()
                if file_cookies and self._validate_cookies(file_cookies):
                    self._cookies = file_cookies
                    self._update_metadata("loaded_from_file")
                    logger.info("Loaded cookies from file: %s", self.cookie_file)
                    return self._cookies
            except Exception as e:
                logger.warning("Failed to load cookies from file: %s", e)

        # Try browser extraction
        try:
            browser_cookies = auto_extract_cookies()
            if browser_cookies and self._validate_cookies(browser_cookies):
                self._cookies = browser_cookies
                self._update_metadata("extracted_from_browser")
                await self.save_cookies()  # Save for future use
                logger.info("Extracted cookies from browser")
                return self._cookies
        except Exception as e:
            logger.warning("Failed to extract cookies from browser: %s", e)

        raise ObscuraCookieError("No valid cookies available from file or browser")

    async def _load_from_file(self) -> dict[str, str]:
        """Load cookies from JSON file."""
        cookie_data = json.loads(self.cookie_file.read_text())
        return {cookie["name"]: cookie["value"] for cookie in cookie_data}

    async def save_cookies(self) -> bool:
        """Save current cookies to file."""
        try:
            cookies_list = [
                {"name": name, "value": value, "domain": ".linkedin.com", "path": "/"}
                for name, value in self._cookies.items()
            ]

            self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
            self.cookie_file.write_text(json.dumps(cookies_list, indent=2))

            self._update_metadata("saved_to_file")
            logger.info("Saved %d cookies to file: %s", len(cookies_list), self.cookie_file)
            try:
                from linkedin_mcp_server.auth_audit import log_auth_event

                log_auth_event(
                    "jar_persist", detail=f"obscura store save ({len(cookies_list)} cookies)"
                )
            except Exception:
                pass
            return True

        except Exception as e:
            logger.error("Failed to save cookies to file: %s", e)
            return False

    def _validate_cookies(self, cookies: dict[str, str]) -> bool:
        """Validate that cookies contain required LinkedIn auth tokens."""
        has_required = REQUIRED_COOKIES.issubset(cookies.keys())
        has_recommended = RECOMMENDED_COOKIES.issubset(cookies.keys())

        if not has_required:
            logger.warning("Cookies missing required: %s", REQUIRED_COOKIES - cookies.keys())

        if not has_recommended:
            logger.info("Cookies missing recommended: %s", RECOMMENDED_COOKIES - cookies.keys())

        return has_required

    def _update_metadata(self, source: str) -> None:
        """Update cookie metadata."""
        self._cookie_metadata = {
            "source": source,
            "updated_at": time.time(),
            "cookie_count": len(self._cookies),
            "has_required": self._validate_cookies(self._cookies),
        }
        self._last_validation = time.time()

    async def validate_session(self) -> bool:
        """Validate current session by checking cookie freshness."""
        if not self._cookies:
            return False

        # Check if validation is needed
        if (
            self._last_validation
            and (time.time() - self._last_validation) < self._validation_interval
        ):
            return True

        # Perform validation
        is_valid = self._validate_cookies(self._cookies)
        self._last_validation = time.time()

        if not is_valid:
            logger.warning("Cookie validation failed, attempting refresh")
            try:
                await self.load_cookies()
                return True
            except ObscuraCookieError:
                return False

        return is_valid

    def get_cookie_string(self) -> str:
        """Format cookies as HTTP header string."""
        return "; ".join([f"{k}={v}" for k, v in self._cookies.items()])

    def get_cookies(self) -> dict[str, str]:
        """Get current cookies with validation."""
        if not self._cookies:
            raise ObscuraCookieError("No cookies loaded")

        if not self._validate_cookies(self._cookies):
            raise ObscuraCookieError("Loaded cookies are invalid")

        return self._cookies

    def set_cookie(self, name: str, value: str) -> None:
        """Set a single cookie."""
        self._cookies[name] = value
        logger.debug("Set cookie: %s", name)

    def remove_cookie(self, name: str) -> None:
        """Remove a cookie."""
        if name in self._cookies:
            del self._cookies[name]
            logger.debug("Removed cookie: %s", name)

    async def refresh_cookies(self) -> bool:
        """Force refresh cookies from browser."""
        try:
            browser_cookies = auto_extract_cookies()
            if browser_cookies and self._validate_cookies(browser_cookies):
                self._cookies = browser_cookies
                self._update_metadata("refreshed_from_browser")
                await self.save_cookies()
                logger.info("Successfully refreshed cookies from browser")
                return True
        except Exception as e:
            logger.error("Failed to refresh cookies: %s", e)

        return False

    def get_metadata(self) -> dict[str, Any]:
        """Get cookie metadata."""
        return {
            **self._cookie_metadata,
            "cookie_count": len(self._cookies),
            "last_validation": self._last_validation,
            "cookie_file": str(self.cookie_file),
        }


class ObscuraSessionManager:
    """Manage Obscura sessions with persistence and optimization."""

    def __init__(self, session_dir: Optional[Path] = None):
        self.session_dir = session_dir or Path.home() / ".linkedin-lyr" / "sessions"
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self._active_sessions: dict = {}
        self._session_stats: dict = {}

        logger.info("Obscura session manager initialized with session dir: %s", self.session_dir)

    def create_session(self, session_id: str) -> Path:
        """Create a new session directory."""
        session_path = self.session_dir / session_id
        session_path.mkdir(parents=True, exist_ok=True)

        self._active_sessions[session_id] = {
            "created_at": time.time(),
            "last_used": time.time(),
            "path": str(session_path),
            "request_count": 0,
        }

        logger.info("Created session: %s at %s", session_id, session_path)
        return session_path

    def get_session(self, session_id: str) -> Optional[Path]:
        """Get an existing session directory."""
        session_path = self.session_dir / session_id
        if session_path.exists():
            self._active_sessions[session_id] = self._active_sessions.get(session_id, {})
            self._active_sessions[session_id]["last_used"] = time.time()
            return session_path
        return None

    def update_session_activity(self, session_id: str) -> None:
        """Update session activity timestamp."""
        if session_id in self._active_sessions:
            self._active_sessions[session_id]["last_used"] = time.time()
            self._active_sessions[session_id]["request_count"] += 1

    async def cleanup_expired_sessions(self, max_age: float = 3600.0) -> int:
        """Clean up sessions older than max_age seconds."""
        now = time.time()
        expired_sessions = []

        for session_id, metadata in self._active_sessions.items():
            if now - metadata["last_used"] > max_age:
                expired_sessions.append(session_id)

        for session_id in expired_sessions:
            await self.remove_session(session_id)

        if expired_sessions:
            logger.info("Cleaned up %d expired sessions", len(expired_sessions))

        return len(expired_sessions)

    async def remove_session(self, session_id: str) -> bool:
        """Remove a session and its directory."""
        session_path = self.session_dir / session_id

        if session_id in self._active_sessions:
            del self._active_sessions[session_id]

        if session_path.exists():
            try:
                import shutil

                shutil.rmtree(session_path, ignore_errors=True)
                logger.info("Removed session: %s", session_id)
                return True
            except Exception as e:
                logger.error("Failed to remove session directory %s: %s", session_path, e)
                return False

        return False

    def get_session_stats(self) -> dict[str, Any]:
        """Get session statistics."""
        return {
            "active_sessions": len(self._active_sessions),
            "total_requests": sum(
                s.get("request_count", 0) for s in self._active_sessions.values()
            ),
            "session_dir": str(self.session_dir),
        }


class CookieOptimizer:
    """Optimize cookie usage for Obscura operations."""

    def __init__(self):
        self._cookie_usage_stats: dict = defaultdict(int)
        self._cookie_performance: dict = {}

    def record_cookie_usage(self, cookie_name: str, operation: str) -> None:
        """Record cookie usage for optimization."""
        self._cookie_usage_stats[cookie_name] += 1

    def get_optimal_cookie_set(self, all_cookies: dict[str, str]) -> dict[str, str]:
        """Get optimal cookie set based on usage patterns."""
        # Always include required cookies
        optimal_cookies = {
            name: value for name, value in all_cookies.items() if name in REQUIRED_COOKIES
        }

        # Add recommended cookies if they exist
        for name in RECOMMENDED_COOKIES:
            if name in all_cookies:
                optimal_cookies[name] = all_cookies[name]

        # Add other frequently used cookies
        for cookie_name, usage_count in sorted(
            self._cookie_usage_stats.items(), key=lambda x: x[1], reverse=True
        ):
            if cookie_name not in optimal_cookies and usage_count > 5:
                if cookie_name in all_cookies:
                    optimal_cookies[cookie_name] = all_cookies[cookie_name]

        logger.info(
            "Optimized cookie set: %d cookies from %d total", len(optimal_cookies), len(all_cookies)
        )

        return optimal_cookies

    def get_usage_stats(self) -> dict[str, Any]:
        """Get cookie usage statistics."""
        return {
            "total_usage": sum(self._cookie_usage_stats.values()),
            "unique_cookies": len(self._cookie_usage_stats),
            "top_cookies": sorted(
                self._cookie_usage_stats.items(), key=lambda x: x[1], reverse=True
            )[:10],
        }


# Global instances
_cookie_manager: ObscuraCookieManager | None = None
_session_manager: ObscuraSessionManager | None = None
_cookie_optimizer: CookieOptimizer | None = None


def get_cookie_manager() -> ObscuraCookieManager:
    """Get the global cookie manager instance."""
    global _cookie_manager

    if _cookie_manager is None:
        _cookie_manager = ObscuraCookieManager()

    return _cookie_manager


def get_session_manager() -> ObscuraSessionManager:
    """Get the global session manager instance."""
    global _session_manager

    if _session_manager is None:
        _session_manager = ObscuraSessionManager()

    return _session_manager


def get_cookie_optimizer() -> CookieOptimizer:
    """Get the global cookie optimizer instance."""
    global _cookie_optimizer

    if _cookie_optimizer is None:
        _cookie_optimizer = CookieOptimizer()

    return _cookie_optimizer


async def initialize_cookie_management(cookie_file: Optional[Path] = None) -> None:
    """Initialize cookie management with specific settings."""
    global _cookie_manager, _session_manager

    _cookie_manager = ObscuraCookieManager(cookie_file)
    _session_manager = ObscuraSessionManager()

    logger.info("Initialized enhanced cookie management")

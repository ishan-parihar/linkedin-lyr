"""
LinkedIn-specific ObscuraCookieManager integration.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from obscura_core import (
    ObscuraCookieManager,
    FileCookieStorage,
    LinkedInCookieExtractor,
    CookieValidationResult,
)
from obscura_core.cookie_manager.exceptions import CookieStorageError

from linkedin_mcp_server.common_utils import utcnow_iso
from linkedin_mcp_server.session_state import portable_cookie_path

logger = logging.getLogger(__name__)

# Required cookies for LinkedIn
LINKEDIN_REQUIRED_COOKIES = ["li_at"]


class _QuarantiningFileCookieStorage(FileCookieStorage):
    """FileCookieStorage that never destroys the cookie file (#2593).

    obscura_core's ObscuraCookieManager calls ``storage.clear()`` on
    invalidation; the base class unlinks cookies.json outright, so one
    false-negative validation left the runtime with no session at all — the
    destruction step of the relogin loop. Here "clear" quarantines the file
    beside itself under the session_state quarantine naming (recoverable by
    hand, swept by ``clear_auth_state``), and a failed quarantine refuses to
    destroy anything.

    ``load`` also accepts the canonical list-of-dicts shape the rest of the
    system writes, which the base class silently reads as None and treated as
    "no session", triggering the re-extract/invalidate cascade on a perfectly
    good file.
    """

    async def load(self) -> Optional[dict[str, str]]:
        if not self.file_path.exists():
            return None
        try:
            from linkedin_mcp_server.voyager_auth import normalize_cookies

            data = json.loads(self.file_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise CookieStorageError(
                f"Failed to load cookies from {self.file_path}: {exc}"
            ) from exc
        return normalize_cookies(data)

    async def clear(self) -> None:
        if not self.file_path.exists():
            return
        stamp = utcnow_iso().replace(":", "-")
        backup = self.file_path.parent / f"invalid-state-{stamp}-cookies.json"
        try:
            self.file_path.replace(backup)
        except OSError as exc:
            logger.error(
                "Could not quarantine cookies to %s: %s; leaving the file in place",
                backup,
                exc,
            )
            return
        logger.warning("Quarantined cookies to %s (not deleted)", backup)


class LinkedInCookieValidator:
    """Validates LinkedIn cookies by making an API call."""

    def __init__(self):
        self._extractor = None

    async def validate(self, cookies: dict[str, str]) -> bool:
        """Verdict a cookie dict. Probe-first over direct HTTP (#2430).

        Bringing stored cookies into a headless browser is what triggers
        LinkedIn's server-side rotation (#2329), so liveness is decided with a
        plain HTTP probe and the automated browser is never booted here.

        #2601 correction: the plain-HTTP belief was backwards — replaying
        browser-minted cookies over HTTP is itself a revocation trigger, so
        the probe makes NO network request by default and returns
        ``unknown``. ``unknown`` validates the session: no evidence of death
        means no invalidation. True in-browser liveness is proven by the
        /feed/ navigation itself.
        """
        try:
            # Fast fail: check that required cookies are present
            required = ["li_at"]
            for cookie in required:
                if cookie not in cookies or not cookies[cookie]:
                    logger.debug(f"Required cookie missing: {cookie}")
                    return False

            from linkedin_mcp_server.voyager_auth import aprobe_session

            verdict = await aprobe_session(cookies)
            if verdict == "dead":
                logger.debug("Voyager probe rejected session: dead")
                return False
            if verdict == "unknown":
                logger.warning(
                    "Voyager probe could not reach LinkedIn (unknown); "
                    "treating the session as valid rather than invalidating it"
                )
            return True
        except Exception as e:
            logger.debug(f"LinkedIn cookie validation failed: {e}")
            return False


class LinkedInObscuraManager:
    """LinkedIn-specific wrapper around ObscuraCookieManager."""

    def __init__(self):
        self._manager: Optional[ObscuraCookieManager] = None
        self._validator = LinkedInCookieValidator()

    def _get_storage(self) -> FileCookieStorage:
        """Get file-based cookie storage (non-destructive, format-tolerant)."""
        return _QuarantiningFileCookieStorage(portable_cookie_path())

    def _get_extractor(self) -> LinkedInCookieExtractor:
        """Get browser cookie extractor (prefers Chrome/Arc)."""
        return LinkedInCookieExtractor(
            preferred_browsers=["chrome", "arc", "brave", "firefox", "edge"]
        )

    def _get_manager(self) -> ObscuraCookieManager:
        """Get or create the ObscuraCookieManager instance."""
        if self._manager is None:
            self._manager = ObscuraCookieManager(
                storage=self._get_storage(),
                extractor=self._get_extractor(),
                validator=self._validator.validate,
                required_cookies=LINKEDIN_REQUIRED_COOKIES,
                validation_interval=300,  # 5 minutes
                max_re_extraction_attempts=3,
                re_extraction_cooldown=60,
            )
        return self._manager

    async def get_valid_cookies(self, force_refresh: bool = False) -> CookieValidationResult:
        """Get valid cookies, performing validation and re-extraction as needed."""
        manager = self._get_manager()
        return await manager.get_cookies(force_refresh=force_refresh)

    async def force_re_extraction(self) -> CookieValidationResult:
        """Force re-extraction from browser (call after user logs in)."""
        manager = self._get_manager()
        return await manager.force_re_extraction()

    async def invalidate_and_trigger_relogin(self) -> None:
        """Invalidate auth and trigger re-login flow."""
        manager = self._get_manager()
        await manager.invalidate_and_trigger_relogin()

    def is_cache_valid(self) -> bool:
        """Check if cached cookies are within validation interval."""
        manager = self._get_manager()
        return manager.is_cache_valid()


# Global instance
_linkedin_obscura_manager: Optional[LinkedInObscuraManager] = None


def get_linkedin_obscura_manager() -> LinkedInObscuraManager:
    """Get the global LinkedIn Obscura manager instance."""
    global _linkedin_obscura_manager
    if _linkedin_obscura_manager is None:
        _linkedin_obscura_manager = LinkedInObscuraManager()
    return _linkedin_obscura_manager


async def get_valid_linkedin_cookies(force_refresh: bool = False) -> CookieValidationResult:
    """Get valid LinkedIn cookies using ObscuraCookieManager."""
    manager = get_linkedin_obscura_manager()
    return await manager.get_valid_cookies(force_refresh)


async def force_linkedin_cookie_refresh() -> CookieValidationResult:
    """Force re-extraction of LinkedIn cookies from browser."""
    manager = get_linkedin_obscura_manager()
    return await manager.force_re_extraction()


async def invalidate_linkedin_auth() -> None:
    """Invalidate LinkedIn auth and trigger re-login."""
    manager = get_linkedin_obscura_manager()
    await manager.invalidate_and_trigger_relogin()

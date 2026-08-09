"""Direct-HTTP LinkedIn Voyager profile-edit client.

Cookie+CSRF REST-li client against LinkedIn's Voyager web surface. This is the
only write architecture that survives LinkedIn's anti-bot rotation on headless
boxes: raw HTTP (curl_cffi TLS-fingerprint impersonation) succeeds where a
Playwright-driven browser gets its session rotated within ~30 min.

Payload shapes mirror the official LinkedIn v2 Profile Edit API
(people/... base, `patch.$set`/`patch.$delete`, localized+preferredLocale text,
`{month, year}` dates, rawText descriptions). The section resource segments are
canonical: positions, educations, certifications, publications, skills,
volunteering-experiences. Every write is a stable REST+CSRF surface — there are
no rotating GraphQL op IDs (unlike twitter DM).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from curl_cffi import requests as cffi_req

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
    NetworkError,
    RateLimitError,
)
from linkedin_mcp_server.session_state import portable_cookie_path

logger = logging.getLogger(__name__)

VOYAGER_PROFILE_BASE = "https://www.linkedin.com/voyager/api/identity/profiles"
VOYAGER_TIMEOUT_SECONDS = 15.0
DEFAULT_HEADERS = {
    "Accept": "application/vnd.linkedin.normalized+json 2.1, application/json",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Canonical section resource segments under {base}/{profile_id}.
SECTIONS: dict[str, str] = {
    "positions": "positions",
    "educations": "educations",
    "certifications": "certifications",
    "publications": "publications",
    "skills": "skills",
    "volunteering-experiences": "volunteeringExperiences",
    "projects": "projects",
}

REQUIRED_COOKIES = {"li_at"}
RECORD_HEADER = "x-linkedin-id"


def localized_text(value: str) -> dict[str, Any]:
    """Wrap a plain string as a localized field value (official v2 shape)."""
    return {
        "localized": {"en_US": value},
        "preferredLocale": {"country": "US", "language": "en"},
    }


def raw_text(value: str) -> dict[str, Any]:
    """Wrap a long text field (descriptions, summaries) as localized rawText."""
    return {
        "localized": {"en_US": {"rawText": value}},
        "preferredLocale": {"country": "US", "language": "en"},
    }


def month_year(year: int, month: int | None = None) -> dict[str, int]:
    """Official `{month, year}` date shape (not ISO strings)."""
    date: dict[str, int] = {"year": year}
    if month is not None:
        date["month"] = month
    return date


def _csrf_from_jsessionid(cookies: dict[str, str]) -> str | None:
    jsessionid = cookies.get("JSESSIONID") or cookies.get("jsessionid")
    if not jsessionid:
        return None
    # Voyager drops the opaque "ajax:" scheme prefix from the CSRF token.
    return jsessionid.removeprefix("ajax:")


class VoyagerProfileEditClient:
    """Direct-HTTP cookie+CSRF client for LinkedIn Voyager profile writes."""

    def __init__(
        self,
        cookies: dict[str, str] | None = None,
        csrf_token: str | None = None,
        base_url: str = VOYAGER_PROFILE_BASE,
        timeout: float = VOYAGER_TIMEOUT_SECONDS,
        dry_run: bool = False,
    ) -> None:
        self.cookies = cookies if cookies is not None else self._load_cookies()
        self.csrf_token = csrf_token or _csrf_from(self.cookies)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.dry_run = dry_run
        self._require_session()

    # ------------------------------------------------------------------ auth

    def _load_cookies(self) -> dict[str, str]:
        from linkedin_mcp_server.obscura_cookie_import import ObscuraCookieManager

        manager = ObscuraCookieManager()
        path = portable_cookie_path()
        if not path.exists():
            raise AuthenticationError(
                "No LinkedIn cookie file found",
                "Run 'linkedin-lyr --login' or supply cookies.",  # ponytail: local short doc
            )
        cookies = manager.load_cookies()
        if not cookies:
            raise AuthenticationError(
                "LinkedIn cookie file is empty",
                "Run 'linkedin-lyr --login' to re-authenticate.",
            )
        return cookies

    def _require_session(self) -> None:
        if not self.cookies:
            raise AuthenticationError(
                "LinkedIn cookie file is empty",
                "Run 'linkedin-lyr --login' to re-authenticate.",
            )

    def _require_li_at(self) -> None:
        if "li_at" not in self.cookies:
            raise AuthenticationError(
                "LinkedIn session cookie li_at missing",
                (
                    "The Voyager profile-edit surface is auth-gated. Supply a live "
                    "li_at (login from a real browser) before writing."
                ),
            )

    def authentication_status(self) -> dict[str, Any]:
        """Read-only auth/state report; never touches the browser."""
        present = sorted(self.cookies)
        signed_in = all(c in self.cookies for c in ("li_at", "bscookie"))
        return {
            "authenticated": bool(signed_in),
            "has_li_at": "li_at" in self.cookies,
            "csrf_token_possible": bool(self.csrf_token),
            "cookie_names": present,
            "profile_id": self._profile_id(),
        }

    # ------------------------------------------------------------------ wire

    def _profile_id(self) -> str:
        return "me"

    def _headers(self) -> dict[str, str]:
        headers = dict(DEFAULT_HEADERS)
        if self.csrf_token:
            headers["csrf-token"] = self.csrf_token
        headers["x-restli-protocol-version"] = "2.0.0"
        return headers

    def _url(self, tail: str) -> str:
        return f"{self.base_url}/{self._profile_id()}/{tail.lstrip('/')}"

    def _send(
        self,
        method: str,
        tail: str,
        body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = self._url(tail)
        headers = self._headers()
        if extra_headers:
            headers = {**headers, **extra_headers}
        prepared = {
            "method": method,
            "url": url,
            "headers": headers,
            "body": body,
        }
        if self.dry_run:
            return {"status": "dry_run", "auth_ok": True, **prepared}
        return self._execute(method, url, body, headers)

    def _execute(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            with cffi_req.Session(impersonate="chrome", timeout=self.timeout) as client:
                response = client.request(
                    method,
                    url,
                    headers=headers,
                    json=body,
                    allow_redirects=False,  # a 302 here is a dead-session signal
                )
        except AuthenticationError:
            raise
        except Exception as exc:
            raise NetworkError(f"Voyager request failed: {exc}") from exc
        return self._classify(response)

    def _classify(self, response: Any) -> dict[str, Any]:
        status = response.status_code
        if status in (200, 201, 204):
            try:
                payload = response.json()
            except Exception:
                payload = None
            return {
                "status": status,
                "ok": True,
                **(payload if isinstance(payload, dict) else {"raw": payload}),
            }
        if status in (301, 302, 401, 403):
            raise AuthenticationError(
                f"LinkedIn rejected the Voyager request (HTTP {status})",
                "The stored session is dead or rotated. A fresh li_at from a real "
                "browser login is required.",
            )
        if status == 429:
            raise RateLimitError(f"LinkedIn rate-limited Voyager request (HTTP {status})")
        raise LinkedInScraperException(
            f"Voyager returned HTTP {status} for a profile-edit request"
        )

    # ----------------------------------------------------------------- writes

    def update_profile(
        self,
        set_fields: dict[str, Any] | None = None,
        delete_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update basics (headline, summary, geo...) via patch.$set on the base."""
        patch: dict[str, Any] = {}
        if set_fields:
            patch["$set"] = set_fields
        if delete_keys:
            patch["$delete"] = delete_keys or []
        if not patch:
            raise LinkedInScraperException("Nothing to patch: pass set_fields or delete_keys")
        self._require_li_at()
        return self._send("POST", "", {"patch": patch})

    def create_record(
        self,
        section: str,
        fields: dict[str, Any],
        x_linkedin_id: str | None = None,
    ) -> dict[str, Any]:
        """Create one record in a section (POST to the section collection).

        x_linkedin_id carries the entity id for the create (per the official
        Profile Edit API the create verb submits this header).
        """
        tail = SECTIONS[section]
        self._require_li_at()
        extra = {"x-linkedin-id": x_linkedin_id} if x_linkedin_id else None
        return self._send("POST", tail, fields, extra_headers=extra)

    def update_record(
        self,
        section: str,
        record_id: str,
        set_fields: dict[str, Any] | None = None,
        delete_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """Partial-update one record: POST {section}/{id} with patch.$set."""
        patch: dict[str, Any] = {}
        if set_fields:
            patch["$set"] = set_fields
        if delete_keys:
            patch["$delete"] = delete_keys or []
        if not patch:
            raise LinkedInScraperException("Nothing to update: pass set_fields or delete_keys")
        self._require_li_at()
        tail = f"{SECTIONS[section]}/{record_id}"
        return self._send("POST", tail, {"patch": patch})

    def delete_record(self, section: str, record_id: str) -> dict[str, Any]:
        """Delete one record from a section (DELETE {section}/{id})."""
        self._require_li_at()
        tail = f"{SECTIONS[section]}/{record_id}"
        return self._send("DELETE", tail)


def _csrf_from(cookies: dict[str, str]) -> str | None:
    # Voyager's csrf-token header is the JSESSIONID value minus its opaque
    # "ajax:" scheme prefix (#2430). Prefer an explicit cookie when present.
    from_cookie = cookies.get("csrf-token") or cookies.get("csrfToken")
    if from_cookie:
        return from_cookie.removeprefix("ajax:")
    return _csrf_from_jsessionid(cookies)
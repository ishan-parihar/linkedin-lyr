"""Profile-edit tools backed by the VoyagerProfileEditClient (direct HTTP).

These read the flat cookie file directly (no browser, no daemon) — the only
live LinkedIn surface that survives on headless boxes (#2344).
"""

from __future__ import annotations

from typing import Any

from ..core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
    ProfileNotFoundError,
)
from ..voyager_profile import SECTIONS, VoyagerProfileEditClient


def _client(public_id: str | None, dry_run: bool) -> VoyagerProfileEditClient:
    client = VoyagerProfileEditClient(public_id=public_id, dry_run=dry_run)
    if not client.auth_ok:
        raise AuthenticationError(
            "No usable LinkedIn session: the stored cookies are dead or absent "
            "(voyager writes need a live li_at). Refresh cookies from a real "
            "browser login, then retry."
        )
    return client


def update_basics(
    headline: str | None = None,
    summary: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    location_name: str | None = None,
    profession: str | None = None,
    public_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Update top-level profile fields via Voyager patch.$set.

    Each provided field is wrapped in the official
    {localized: {en_US: ...}, preferredLocale} shape; other basics fields can
    be written raw by passing a raw $set dict through update_profile_patch.
    """
    fields: dict[str, Any] = {}
    for key, value in [
        ("headline", headline),
        ("firstName", first_name),
        ("lastName", last_name),
        ("locationName", location_name),
        ("industryName", profession),
    ]:
        if value is not None:
            fields[key] = {"localized": {"en_US": value}}
    if summary is not None:
        # Official Profile Edit API documents description fields via rawText.
        fields["summary"] = {"localized": {"en_US": {"rawText": summary}}}
    if not fields:
        raise LinkedInScraperException("No fields to update: pass at least one basic field")
    client = _client(public_id, dry_run)
    return client.update_profile(set_fields=fields)


def update_profile_patch(
    patch: dict[str, Any],
    public_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a raw patch.$set/$delete for basics fields.

    passthrough for callers who pre-shape the payload (e.g. dates as
    {year, month}, geoLocation objects); the client only adds transport and
    auth, it never fabricates field shapes it cannot verify.
    """
    client = _client(public_id, dry_run)
    return client.update_profile(set_fields=patch.get("$set"), delete_keys=patch.get("$delete"))


def add_profile_record(
    section: str,
    fields: dict[str, Any],
    x_linkedin_id: str | None = None,
    public_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create one record in a profile section (positions/educations/skills/...)."""
    _check_section(section)
    client = _client(public_id, dry_run)
    return client.create_record(section, fields, x_linkedin_id=x_linkedin_id)


def update_profile_record(
    section: str,
    record_id: str,
    patch: dict[str, Any],
    public_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Partial-update one section record (POST {section}/{id} patch.$set)."""
    _check_section(section)
    client = _client(public_id, dry_run)
    return client.update_record(
        section,
        record_id,
        set_fields=patch.get("$set"),
        delete_keys=patch.get("$delete"),
    )


def delete_profile_record(
    section: str,
    record_id: str,
    public_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete one section record (DELETE {section}/{id})."""
    _check_section(section)
    client = _client(public_id, dry_run)
    return client.delete_record(section, record_id)


def profile_edit_status(public_id: str | None = None) -> dict[str, Any]:
    """Read-only: is a live Voyager session present? No network, no browser."""
    client = VoyagerProfileEditClient(public_id=public_id)
    return {
        "session": "valid" if client.auth_ok else "missing_or_invalid",
        "public_id": client._profile_id(),
        "hint": (
            ""
            if client.auth_ok
            else "Refresh cookies from a real browser login; voyager writes need a live li_at."
        ),
    }


def _check_section(section: str) -> None:
    if section not in SECTIONS:
        raise LinkedInScraperException(
            f"Unknown section {section!r}; expected one of {sorted(SECTIONS)}"
        )


# Keep AXI err profiles: tools above raise the same structured classes the
# read tools use. Re-export for importers that pattern-match on them.
AuthorizationError = AuthenticationError  # tasteful alias for tool consumers
NotFoundError = ProfileNotFoundError
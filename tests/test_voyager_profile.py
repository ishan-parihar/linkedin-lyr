"""Tests for VoyagerProfileEditClient -- payload shapes grounded in the
official Profile Edit API (#2437), all dry-run (no network)."""

import sys
from unittest.mock import MagicMock

sys.path.insert(0, ".")

import pytest

from linkedin_mcp_server.core.exceptions import AuthenticationError, RateLimitError
from linkedin_mcp_server.voyager_profile import (
    SECTIONS,
    VoyagerProfileEditClient,
    _csrf_from_jsessionid,
    localized_text,
    month_year,
    raw_text,
)


def _client(**kw):
    return VoyagerProfileEditClient(
        cookies={"li_at": "AQEDfake", "bscookie": "mock", "JSESSIONID": "ajax:6836980583432298106"},
        **kw,
    )


class TestLocalized:
    def test_plain_text_wrapper(self):
        assert localized_text("H") == {
            "localized": {"en_US": "H"},
            "preferredLocale": {"country": "US", "language": "en"},
        }

    def test_raw_text_wrapper(self):
        assert raw_text("long") == {
            "localized": {"en_US": {"rawText": "long"}},
            "preferredLocale": {"country": "US", "language": "en"},
        }

    def test_month_year(self):
        assert month_year(2020) == {"year": 2020}
        assert month_year(2020, 6) == {"year": 2020, "month": 6}


class TestCsrf:
    def test_strips_ajax_prefix(self):
        assert _csrf_from_jsessionid({"JSESSIONID": "ajax:683"}) == "683"

    def test_no_prefix(self):
        assert _csrf_from_jsessionid({"JSESSIONID": "683"}) == "683"

    def test_missing(self):
        assert _csrf_from_jsessionid({}) is None


class TestAuthGate:
    def test_empty_cookies_raises(self):
        with pytest.raises(AuthenticationError):
            VoyagerProfileEditClient(cookies={})

    def test_li_at_gate_on_write(self):
        c = VoyagerProfileEditClient(cookies={"jcookie": "x"}, dry_run=True)
        with pytest.raises(AuthenticationError):
            c.update_record("educations", "e1", set_fields={"schoolName": "X"})

    def test_status_reports_missing_li_at(self):
        c = VoyagerProfileEditClient(cookies={"bcookie": "x"})
        st = c.authentication_status()
        assert st["authenticated"] is False
        assert st["has_li_at"] is False

    def test_status_authenticated(self):
        c = _client()
        st = c.authentication_status()
        assert st["authenticated"] is True
        assert st["csrf_token_possible"] is True
        assert st["profile_id"] == "me"


class TestDryRunShapes:
    def test_update_profile_sets_patch(self):
        req = _client(dry_run=True).update_profile(set_fields={"headline": "H"})
        assert req["status"] == "dry_run"
        assert req["body"] == {"patch": {"$set": {"headline": "H"}}}
        assert "csrf-token" in req["headers"]

    def test_update_profile_delete(self):
        req = _client(dry_run=True).update_profile(delete_keys=["headline"])
        assert req["body"] == {"patch": {"$delete": ["headline"]}}

    def test_create_record(self):
        c = _client(dry_run=True)
        req = c.create_record("educations", {"schoolName": "X"})
        assert req["method"] == "POST"
        assert req["url"].endswith("/me/educations")

    def test_create_record_extra_header(self):
        c = _client(dry_run=True)
        req = c.create_record("skills", {"text": "Go"}, "skill-123")
        assert req["headers"]["x-linkedin-id"] == "skill-123"

    def test_update_record_path(self):
        c = _client(dry_run=True)
        req = c.update_record("educations", "e1", set_fields={"schoolName": "Y"})
        assert req["url"].endswith("/me/educations/e1")
        assert req["body"] == {"patch": {"$set": {"schoolName": "Y"}}}
        assert req["method"] == "POST"

    def test_delete_record(self):
        c = _client(dry_run=True)
        req = c.delete_record("certifications", "c1")
        assert req["method"] == "DELETE"
        assert req["url"].endswith("/me/certifications/c1")

    def test_restli_header(self):
        c = _client(dry_run=True)
        req = c.update_profile(set_fields={"headline": "H"})
        assert req["headers"]["x-restli-protocol-version"] == "2.0.0"

    def test_url_no_double_slash(self):
        c = _client(dry_run=True, base_url="https://www.linkedin.com/voyager/api/identity/profiles/")
        req = c.delete_record("skills", "s1")
        assert "/profiles/me/" in req["url"]
        assert "//me" not in req["url"]


class TestSections:
    def test_official_segments(self):
        assert SECTIONS["educations"] == "educations"
        assert SECTIONS["skills"] == "skills"
        assert SECTIONS["volunteering-experiences"] == "volunteeringExperiences"


class TestClassify:
    def test_ok(self):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"newHeadline": "ok"}
        out = _client()._classify(r)
        assert out["ok"] is True
        assert out["newHeadline"] == "ok"

    def test_redirect_raises_auth(self):
        r = MagicMock(status_code=302)
        with pytest.raises(AuthenticationError):
            _client()._classify(r)

    def test_rate_limit(self):
        r = MagicMock(status_code=429)
        with pytest.raises(RateLimitError):
            _client()._classify(r)

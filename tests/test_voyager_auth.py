"""Guardrail tests for the honest Voyager session probe (Fix 1, #2593).

These pin the invariants from OBSCURA_REGRESSION_FIXES.md Fix 1 plus the
#2593 red-team audit:

- cookie liveness is decided over direct HTTP (voyager_auth.probe_session);
  the validator never boots an automated browser,
- only LinkedIn *answering* "not logged in" from a trusted egress is "dead":
  a probe that could not ask LinkedIn the question (network error, 5xx, rate
  limit, authwall from an untrusted IP) returns "unknown", and callers must
  not invalidate cookies over it,
- every cookie-file shape on disk parses to the same flat dict.
"""

from __future__ import annotations

import inspect
import json
import urllib.error
from types import SimpleNamespace

import pytest

import linkedin_mcp_server.voyager_auth as va
from linkedin_mcp_server.voyager_auth import normalize_cookies, probe_session


class _FakeResponse:
    def __init__(self, status_code: int, location: str | None = None):
        self.status_code = status_code
        self.headers = {"location": location} if location else {}


@pytest.fixture(autouse=True)
def _isolate_probe_env(monkeypatch):
    """No fleet, no proxy, no real user files: every test chooses its egress."""
    for var in (
        "BROWSEFLEET_URL",
        "BROWSEFLEET_TOKEN",
        "BROWSEFLEET_API_KEY",
        "PROXY_SERVER",
        "LINKEDIN_PROBE_ATTEMPTS",
        "LINKEDIN_PROBE_UA",
        "LINKEDIN_FORCE_BROWSER_VALIDATION",
        "LINKEDIN_SKIP_BROWSER_VALIDATION",
    ):
        monkeypatch.delenv(var, raising=False)
    # A dev machine or VPS can have a real bf.env and source-state.json; the
    # probe must derive its egress and UA from the test's choices only.
    monkeypatch.setattr(
        "linkedin_mcp_server.common_utils.load_proxy_env", lambda: None
    )
    monkeypatch.setattr(va, "_source_state_ua", lambda: None)
    monkeypatch.setattr(va.time, "sleep", lambda _s: None)


def _patch_direct(monkeypatch, outcomes):
    """Swap cffi_req.get for a canned sequence of statuses / exceptions."""

    def fake_get(url, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)

    monkeypatch.setattr(va, "cffi_req", SimpleNamespace(get=fake_get))


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def test_probe_session_missing_when_no_li_at():
    assert probe_session({}) == "missing"
    assert probe_session({"li_at": ""}) == "missing"


def test_probe_session_alive_on_200(monkeypatch):
    _patch_direct(monkeypatch, [200])
    assert probe_session({"li_at": "x", "JSESSIONID": "ajax:abc"}) == "alive"


def test_probe_session_dead_on_302_from_trusted_egress(monkeypatch):
    # PROXY_SERVER makes the direct probe trusted: the session is meant to be
    # used from this egress, so its "not logged in" answer is definitive.
    monkeypatch.setenv("PROXY_SERVER", "socks5://127.0.0.1:1080")
    _patch_direct(monkeypatch, [302])
    assert probe_session({"li_at": "x"}) == "dead"


def test_probe_session_unknown_on_302_from_untrusted_egress(monkeypatch):
    # Bare datacenter host: LinkedIn's authwall there can mean "this IP is not
    # trusted for this account" — that is not evidence the cookies died.
    _patch_direct(monkeypatch, [302])
    assert probe_session({"li_at": "x"}) == "unknown"


def test_probe_session_unknown_on_network_error(monkeypatch):
    _patch_direct(monkeypatch, [RuntimeError("connection refused")])
    assert probe_session({"li_at": "x"}) == "unknown"


def test_probe_session_unknown_on_rate_limit_and_5xx(monkeypatch):
    for status in (429, 500, 503, 999):
        _patch_direct(monkeypatch, [status])
        assert probe_session({"li_at": "x"}) == "unknown"


def test_probe_session_retries_unknown_then_alive(monkeypatch):
    _patch_direct(monkeypatch, [RuntimeError("blip"), 200])
    assert probe_session({"li_at": "x"}) == "alive"


def test_probe_session_unknown_after_all_attempts_fail(monkeypatch):
    _patch_direct(monkeypatch, [RuntimeError("blip"), RuntimeError("blip")])
    assert probe_session({"li_at": "x"}) == "unknown"


def test_probe_session_attempts_env_disables_retry(monkeypatch):
    monkeypatch.setenv("LINKEDIN_PROBE_ATTEMPTS", "1")
    outcomes = [RuntimeError("blip"), RuntimeError("should not fire")]

    def fake_get(url, **kwargs):
        raise outcomes.pop(0)

    monkeypatch.setattr(va, "cffi_req", SimpleNamespace(get=fake_get))
    assert probe_session({"li_at": "x"}) == "unknown"
    assert outcomes  # the second canned outcome was never consumed


# ---------------------------------------------------------------------------
# BrowseFleet egress
# ---------------------------------------------------------------------------


class _FakeURLOpener:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def test_probe_bf_verdicts_are_trusted(monkeypatch):
    monkeypatch.setattr(
        va, "_bf_egress_config", lambda: ("http://bf.test", "tok")
    )

    def _refuse_direct(cookies, timeout):
        raise AssertionError("a BF verdict must not fall through to direct")

    monkeypatch.setattr(va, "_direct_probe", _refuse_direct)

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _FakeURLOpener({"status": 200, "ok": True}),
    )
    assert probe_session({"li_at": "x"}) == "alive"

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _FakeURLOpener({"status": 302}),
    )
    assert probe_session({"li_at": "x"}) == "dead"

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _FakeURLOpener({"status": 429}),
    )
    assert probe_session({"li_at": "x"}) == "unknown"


def test_probe_bf_relay_down_falls_back_to_direct(monkeypatch):
    monkeypatch.setattr(
        va, "_bf_egress_config", lambda: ("http://bf.test", "tok")
    )
    direct_calls = []

    def fake_direct(cookies, timeout):
        direct_calls.append(1)
        return 200

    monkeypatch.setattr(va, "_direct_probe", fake_direct)

    def boom(req, timeout=None):
        raise urllib.error.URLError("relay down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert probe_session({"li_at": "x"}) == "alive"
    assert direct_calls


# ---------------------------------------------------------------------------
# Cookie-file normalization (every shape on disk)
# ---------------------------------------------------------------------------


def test_normalize_cookies_canonical_list():
    raw = [{"name": "li_at", "value": "A"}, {"name": "JSESSIONID", "value": "ajax:b"}]
    assert normalize_cookies(raw) == {"li_at": "A", "JSESSIONID": "ajax:b"}


def test_normalize_cookies_single_wrap_list():
    assert normalize_cookies({"cookies": [{"name": "li_at", "value": "A"}]}) == {
        "li_at": "A"
    }


def test_normalize_cookies_wrapped_dict():
    assert normalize_cookies({"cookies": {"li_at": "A"}}) == {"li_at": "A"}


def test_normalize_cookies_flat_dict():
    assert normalize_cookies({"li_at": "A", "bcookie": "B"}) == {
        "li_at": "A",
        "bcookie": "B",
    }


def test_normalize_cookies_drops_valueless_entries():
    raw = [
        {"name": "li_at"},
        {"nope": 1},
        {"name": "", "value": "x"},
        {"name": "li_at", "value": ""},
    ]
    assert normalize_cookies(raw) == {}


# ---------------------------------------------------------------------------
# csrf-token derives from JSESSIONID minus the ajax: prefix (#2430)
# ---------------------------------------------------------------------------


def test_csrf_token_strips_ajax_prefix():
    from linkedin_mcp_server.voyager_auth import csrf_token

    assert csrf_token({"JSESSIONID": "ajax:abc123"}) == "abc123"
    assert csrf_token({"jsessionid": "ajax:xyz"}) == "xyz"
    assert csrf_token({}) is None
    assert csrf_token({"JSESSIONID": "no-prefix"}) == "no-prefix"


# ---------------------------------------------------------------------------
# Validator: probe-driven verdicts, never a browser boot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validator_alive_probe_passes(monkeypatch):
    async def alive(cookies, timeout=10.0, attempts=None):
        return "alive"

    monkeypatch.setattr("linkedin_mcp_server.voyager_auth.aprobe_session", alive)
    from linkedin_mcp_server.obscura_integration import LinkedInCookieValidator

    validator = LinkedInCookieValidator()
    assert await validator.validate({"li_at": "x"}) is True


@pytest.mark.asyncio
async def test_validator_dead_probe_fails(monkeypatch):
    async def dead(cookies, timeout=10.0, attempts=None):
        return "dead"

    monkeypatch.setattr("linkedin_mcp_server.voyager_auth.aprobe_session", dead)
    from linkedin_mcp_server.obscura_integration import LinkedInCookieValidator

    validator = LinkedInCookieValidator()
    assert await validator.validate({"li_at": "x"}) is False


@pytest.mark.asyncio
async def test_validator_unknown_probe_does_not_invalidate(monkeypatch):
    """An unreachable probe is not evidence of a dead session (#2593).

    Returning False here sent live sessions into the invalidation loop: the
    obscura manager re-extracts, fails on a headless host, and clears storage.
    """

    async def unknown(cookies, timeout=10.0, attempts=None):
        return "unknown"

    monkeypatch.setattr("linkedin_mcp_server.voyager_auth.aprobe_session", unknown)
    from linkedin_mcp_server.obscura_integration import LinkedInCookieValidator

    validator = LinkedInCookieValidator()
    assert await validator.validate({"li_at": "x"}) is True


@pytest.mark.asyncio
async def test_validator_missing_required_cookie_fails_without_probe(monkeypatch):
    async def should_not_run(cookies, timeout=10.0, attempts=None):
        raise AssertionError("probe should not run for missing required cookie")

    monkeypatch.setattr("linkedin_mcp_server.voyager_auth.aprobe_session", should_not_run)
    from linkedin_mcp_server.obscura_integration import LinkedInCookieValidator

    validator = LinkedInCookieValidator()
    assert await validator.validate({}) is False


def test_validator_source_never_boots_browser():
    """The validator body must not reference any browser-boot symbol.

    Uses the precise amplifier symbols (not the English word "browser") so the
    docstring that explains the gate doesn't trip the wire.
    """
    from linkedin_mcp_server.obscura_integration import LinkedInCookieValidator

    source = inspect.getsource(LinkedInCookieValidator.validate)
    banned = (
        "get_or_create_browser",
        "ObscuraBrowserManager",
        "force_linkedin_cookie_refresh",
        "invalidate_linkedin_auth",
        "get_ready_extractor",
    )
    for symbol in banned:
        assert symbol not in source, (
            f"validator must not reference {symbol}; probe-first per Fix 1"
        )


def test_voyager_auth_imports_no_obscura_core():
    """voyager_auth must import cleanly with no VPS-only obscura_core at module level."""
    assert not hasattr(va, "obscura_core")
    source = inspect.getsource(va)
    assert "import obscura_core" not in source
    assert "get_or_create_browser" not in source
    assert "ObscuraBrowserManager" not in source

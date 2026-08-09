"""Guardrail tests for the probe-first LinkedIn session validator (Fix 1).

These pin the invariant from OBSCURA_REGRESSION_FIXES.md Fix 1: cookie
liveness is decided over direct HTTP (voyager_auth.probe_session), and the
validator never boots an automated browser. A future change that re-introduces
browser boot into LinkedinCookieValidator.validate() MUST fail here.
"""

from __future__ import annotations

import inspect

import pytest


# ---------------------------------------------------------------------------
# probe_session verdicts
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int, location: str | None = None):
        self.status_code = status_code
        self.headers = {"location": location} if location else {}


class _FakeClient:
    def __init__(self, responses):
        self._responses = responses

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, cookies=None):
        return self._responses.pop(0)


def test_probe_session_missing_when_no_li_at(monkeypatch):
    from linkedin_mcp_server.voyager_auth import probe_session

    verdict = probe_session({})
    assert verdict == "missing"
    verdict = probe_session({"li_at": ""})
    assert verdict == "missing"


def _patch_client(monkeypatch, response_or_builder):
    """Swap the cffi_req.Client callable in voyager_auth's namespace."""
    import types

    import linkedin_mcp_server.voyager_auth as va

    fake = types.SimpleNamespace(Client=response_or_builder)
    monkeypatch.setattr(va, "cffi_req", fake)


def test_probe_session_alive_on_200(monkeypatch):
    from linkedin_mcp_server.voyager_auth import probe_session

    _patch_client(monkeypatch, lambda **kw: _FakeClient([_FakeResponse(200)]))
    assert probe_session({"li_at": "x", "JSESSIONID": "ajax:abc"}) == "alive"


def test_probe_session_dead_on_302_self_loop(monkeypatch):
    from linkedin_mcp_server.voyager_auth import probe_session

    _patch_client(monkeypatch, lambda **kw: _FakeClient([_FakeResponse(302, location="/login")]))
    assert probe_session({"li_at": "x"}) == "dead"


def test_probe_session_dead_on_network_error(monkeypatch):
    from linkedin_mcp_server.voyager_auth import probe_session

    def boom(**kw):
        raise RuntimeError("connection refused")

    _patch_client(monkeypatch, boom)
    assert probe_session({"li_at": "x"}) == "dead"


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

@pytest.fixture(autouse=True)
def _clear_any_browser_env(monkeypatch):
    """Guardrail tests must run with no validation env set."""
    monkeypatch.delenv("LINKEDIN_FORCE_BROWSER_VALIDATION", raising=False)
    monkeypatch.delenv("LINKEDIN_SKIP_BROWSER_VALIDATION", raising=False)


@pytest.mark.asyncio
async def test_validator_alive_probe_passes(monkeypatch):
    class _FakeAprobe:
        def __init__(self, verdict):
            self._verdict = verdict
            self.called = False

        async def __call__(self, cookies, timeout=10.0):
            self.called = True
            return self._verdict

    fake = _FakeAprobe("alive")
    monkeypatch.setattr("linkedin_mcp_server.voyager_auth.aprobe_session", fake)
    from linkedin_mcp_server.obscura_integration import LinkedInCookieValidator

    validator = LinkedInCookieValidator()
    assert await validator.validate({"li_at": "x"}) is True
    assert fake.called


@pytest.mark.asyncio
async def test_validator_dead_probe_fails(monkeypatch):
    async def dead_probe(cookies, timeout=10.0):
        return "dead"

    monkeypatch.setattr(
        "linkedin_mcp_server.voyager_auth.aprobe_session", dead_probe
    )
    from linkedin_mcp_server.obscura_integration import LinkedInCookieValidator

    validator = LinkedInCookieValidator()
    assert await validator.validate({"li_at": "x"}) is False


@pytest.mark.asyncio
async def test_validator_missing_required_cookie_fails_without_probe(monkeypatch):
    async def should_not_run(cookies, timeout=10.0):
        raise AssertionError("probe should not run for missing required cookie")

    monkeypatch.setattr(
        "linkedin_mcp_server.voyager_auth.aprobe_session", should_not_run
    )
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
    import linkedin_mcp_server.voyager_auth as va

    assert not hasattr(va, "obscura_core")
    source = inspect.getsource(va)
    assert "import obscura_core" not in source
    assert "get_or_create_browser" not in source
    assert "ObscuraBrowserManager" not in source
"""Guardrail tests for the Fix 5 honest-degradation wiring.

These pin the Fix 5 invariants from OBSCURA_REGRESSION_FIXES.md:
- when the daemon reports an invalid session AND the pool has no live session,
  get_ready_extractor raises an honest error and NEVER boots the browser
  (booting the obscura browser with stale li_at rotates the session server-side
  within ~30 min per #2329),
- when the daemon reports invalid but the pool has a live session, the pooled
  cookies win and the browser boots with them (alive/dead/empty semantics per
  #2572),
- handle_auth_error is reached on the empty/dead verdict (re-login flow, not a
  browser boot).
"""

from __future__ import annotations

import pytest

_POOL_COOKIES = {"li_at": "AQED-pool-live-token"}


class _DeadDaemonResult:
    valid = False
    cookies = {}
    error = "probe failed: all connection attempts failed"


class _FakeBrowserPage:
    def __init__(self):
        self.added_cookies = None
        self.context = _FakeBrowserContext(self)

    def __enter__(self):
        return self


class _FakeBrowserContext:
    def __init__(self, page):
        self._page = page

    async def add_cookies(self, cookie_list):
        self._page.added_cookies = cookie_list


class _FakeBrowser:
    def __init__(self):
        self.page = _FakeBrowserPage()


class _FakeExtractor:
    def __init__(self, page):
        self.page = page


def _patch_ready_deps(monkeypatch, *, pooled):
    """Patch the module-level bindings get_ready_extractor resolves at call time."""
    from linkedin_mcp_server import dependencies

    async def _noop_ready(*_a, **_k):
        return None

    async def _pool():
        return _POOL_COOKIES if pooled else None

    # ensure_tool_ready_or_raise is imported into dependencies at module level.
    monkeypatch.setattr(dependencies, "ensure_tool_ready_or_raise", _noop_ready)
    # pool verdict: no sessions -> None; one alive session -> its cookies.
    monkeypatch.setattr(dependencies, "_first_live_pool_session", _pool)
    # daemon always reports dead so the pool verdict is what decides the boot.
    async def _dead_daemon():
        return _DeadDaemonResult()

    monkeypatch.setattr(dependencies, "get_valid_linkedin_cookies_from_daemon", _dead_daemon)


@pytest.mark.asyncio
async def test_dead_or_empty_pool_never_boots_browser(monkeypatch):
    from linkedin_mcp_server import dependencies
    from linkedin_mcp_server.core.exceptions import AuthenticationError
    from linkedin_mcp_server.obscura_integration import CookieValidationResult

    _patch_ready_deps(monkeypatch, pooled=False)

    booted = []

    async def _should_never_boot():
        booted.append("booted")

    monkeypatch.setattr(dependencies, "get_or_create_browser", _should_never_boot)

    called_handle = []

    async def _handle_auth(error: AuthenticationError, ctx):
        called_handle.append(error)
        raise error

    monkeypatch.setattr(dependencies, "handle_auth_error", _handle_auth)

    with pytest.raises(AuthenticationError, match="No live LinkedIn session"):
        await dependencies.get_ready_extractor(None, tool_name="get_person_profile")

    # The regression amplifier is dead: an empty/dead pool verdict must not
    # boot the browser (per #2329/#2572), and the honest-degradation error
    # routes through the re-login handler, not a silent fall-through.
    assert booted == []
    assert len(called_handle) == 1
    assert "No live LinkedIn session" in str(called_handle[0])

    assert CookieValidationResult is not None


@pytest.mark.asyncio
async def test_alive_pool_session_boots_with_pooled_cookies(monkeypatch):
    from linkedin_mcp_server import dependencies

    _patch_ready_deps(monkeypatch, pooled=True)

    browser = _FakeBrowser()

    async def _boot():
        return browser

    monkeypatch.setattr(dependencies, "get_or_create_browser", _boot)
    monkeypatch.setattr(
        dependencies, "LinkedInExtractor", lambda page: _FakeExtractor(page)
    )

    extractor = await dependencies.get_ready_extractor(None, tool_name="get_person_profile")

    assert isinstance(extractor, _FakeExtractor)
    added = browser.page.added_cookies
    assert added is not None
    by_name = {c["name"]: c["value"] for c in added}
    assert by_name.get("li_at") == _POOL_COOKIES["li_at"]
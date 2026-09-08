"""Guardrail tests for the session pool (#2553).

Each test locks one invariant of the pool so a future refactor — which is
exactly how the original browser-boot amplifier got re-introduced — fails
loudly instead of silently rotating the ``li_at``:
  * sessions persist in the single-wrap Obscura shape (#2201),
  * add refuses sessions without ``li_at``,
  * first_live_session probes over direct HTTP and never boots a browser,
  * prune_dead_sessions never deletes the last surviving session.
"""

from __future__ import annotations

import json

import pytest

from linkedin_mcp_server import session_pool
from linkedin_mcp_server.session_pool import (
    add_session,
    first_live_session,
    list_sessions,
    prune_dead_sessions,
    sessions_dir,
)


@pytest.fixture
def pool_dir(tmp_path, monkeypatch):
    """Point the pool at a temp auth root so tests never touch real cookies."""
    from linkedin_mcp_server import session_state

    monkeypatch.setattr(
        session_state,
        "get_source_profile_dir",
        lambda: tmp_path / "profile",
    )
    sessions_dir(tmp_path / "profile")
    return tmp_path / "profile"


def _cookies(li_at: str = "AQED-live-token", **extra) -> dict[str, str]:
    return {"li_at": li_at, "csrf-token": "ajax:12345", "bscookie": "v=2", **extra}


class TestPoolShape:
    def test_add_stores_single_wrap(self, pool_dir):
        path = add_session("alpha", _cookies())
        blob = json.loads(path.read_text())
        assert list(blob.keys()) == ["cookies"]
        assert blob["cookies"]["li_at"] == "AQED-live-token"

    def test_add_refuses_missing_li_at(self, pool_dir):
        with pytest.raises(ValueError, match="no li_at"):
            add_session("stale", {"bscookie": "v=2"})

    def test_add_refuses_unsafe_name(self, pool_dir):
        with pytest.raises(ValueError, match="invalid session name"):
            add_session("../escape", _cookies())

    def test_sessions_are_sortable_and_listable(self, pool_dir):
        add_session("zz", _cookies("AQED-z"))
        add_session("aa", _cookies("AQED-a"))
        names = [e.name for e in list_sessions(pool_dir)]
        assert names == ["aa", "zz"]


class TestStructuralSelection:
    """#2601: failover selection is structural; no HTTP probing in the hot path."""

    async def test_first_live_returns_first_structurally_valid(self, pool_dir):
        add_session("dead-one", _cookies("AQED-dead"))
        add_session("alive-two", _cookies("AQED-alive"))
        # No probe patching: selection must not depend on any HTTP verdict.
        entry = await first_live_session(pool_dir)
        assert entry.name == "alive-two"  # deterministic name-sorted order

    async def test_first_live_none_when_no_li_at_anywhere(self, pool_dir):
        # add_session refuses jars without li_at, so build the file directly.
        path = session_pool._session_path("broken", pool_dir)
        path.write_text(json.dumps({"cookies": {"bscookie": "v=2"}}))
        assert await first_live_session(pool_dir) is None

    async def test_prune_preserves_last_survivor(self, pool_dir, monkeypatch):
        add_session("dead-one", _cookies("AQED-dead"))
        add_session("alive-two", _cookies("AQED-alive"))

        async def fake_probe(cookies, timeout=10.0):
            return "dead" if "dead" in cookies["li_at"] else "alive"

        monkeypatch.setattr(session_pool, "aprobe_session", fake_probe)
        alive, pruned = await prune_dead_sessions(pool_dir)
        assert [e.name for e in alive] == ["alive-two"]
        assert pruned == ["dead-one"]

    async def test_prune_does_not_wipe_all_dead(self, pool_dir, monkeypatch):
        add_session("dead-one", _cookies("AQED-dead"))

        async def dead(cookies, timeout=10.0):
            return "dead"

        monkeypatch.setattr(session_pool, "aprobe_session", dead)
        alive, pruned = await prune_dead_sessions(pool_dir)
        assert alive == []
        assert pruned == ["dead-one"]
        # honest degradation: the file stays on disk for diagnosis
        assert len(list_sessions(pool_dir)) == 1


class TestNoBrowserInPath:
    async def test_pool_module_never_boots_browser(self, pool_dir, monkeypatch):
        """The pool must resolve entirely via direct-HTTP probe."""
        import inspect

        add_session("only", _cookies())

        async def fake_probe(cookies, timeout=10.0):
            return "alive"

        monkeypatch.setattr(session_pool, "aprobe_session", fake_probe)
        assert await first_live_session(pool_dir) is not None
        # Tripwire (#2553): if a future refactor routes the pool through the
        # automated browser, these boot symbols reappear in the module source
        # and this import-free scan fails.
        source = inspect.getsource(session_pool)
        for banned in ("get_or_create_browser", "ObscuraBrowserManager", "playwright"):
            assert banned not in source
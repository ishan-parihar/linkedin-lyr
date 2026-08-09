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
from linkedin_mcp_server.session_state import portable_cookie_path


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


class TestProbeDriven:
    async def test_first_live_picks_alive_over_dead(self, pool_dir, monkeypatch):
        add_session("dead-one", _cookies("AQED-dead"))
        add_session("alive-two", _cookies("AQED-alive"))
        results = {"dead": "dead", "alive": "alive"}

        async def fake_probe(cookies, timeout=10.0):
            return results[cookies["li_at"].split("-")[-1]]

        monkeypatch.setattr(session_pool, "aprobe_session", fake_probe)
        entry = await first_live_session(pool_dir)
        assert entry.name == "alive-two"

    async def test_first_live_none_when_all_dead(self, pool_dir, monkeypatch):
        add_session("only", _cookies("AQED-dead"))

        async def dead(cookies, timeout=10.0):
            return "dead"

        monkeypatch.setattr(session_pool, "aprobe_session", dead)
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
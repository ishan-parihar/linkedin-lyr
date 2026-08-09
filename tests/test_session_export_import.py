"""Guardrail tests for linkedin-lyr --export-session / --import-session (Fix 4).

These pin the Fix 4 invariants from OBSCURA_REGRESSION_FIXES.md:
- export writes the single-wrap portable shape {"cookies": {flat}} (the only
  shape the pool/daemon FileCookieStorage validation single-unwraps, #2201/#2506),
- import accepts that shape, probes first (probe-first per #2555/#2564) and
  only then persists via ObscuraCookieManager.save_cookies,
- import rejects anything that is not the single-wrap shape with li_at.
"""

from __future__ import annotations

import json

import pytest


class _FakeCookieManager:
    """Records what was saved and returns canned flat cookies for load."""

    def __init__(self, loaded: dict[str, str]):
        self._loaded = loaded
        self.saved: dict[str, str] | None = None
        self.cookie_path = "/fake/auth/cookies.json"

    def load_cookies(self) -> dict[str, str]:
        return self._loaded

    def save_cookies(self, cookies: dict[str, str]) -> None:
        self.saved = cookies


def _patch_export_deps(monkeypatch, config, cookies_loaded):
    """Patch the config + module-level bindings the handlers resolve at call time."""
    manager = _FakeCookieManager(cookies_loaded)
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_config", lambda: config)
    monkeypatch.setattr("linkedin_mcp_server.cli_main.configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_version", lambda: "4.0.0")
    # import-inside-body: patch at the source modules, not cli_main.
    monkeypatch.setattr(
        "linkedin_mcp_server.obscura_cookie_import.ObscuraCookieManager",
        lambda _auth_root=None: manager,
    )
    return manager


def test_export_writes_single_wrap_and_probes(
    monkeypatch, capsys, tmp_path
):
    from linkedin_mcp_server.config import AppConfig
    from linkedin_mcp_server.cli_main import export_session_and_exit

    export_path = tmp_path / "portable.json"
    config = AppConfig()
    config.server.export_session = str(export_path)

    probes = []

    def _probe(cookies):
        probes.append(cookies)
        return "alive"

    manager = _patch_export_deps(monkeypatch, config, {"li_at": "abc", "JSESSIONID": "x"})
    monkeypatch.setattr("linkedin_mcp_server.voyager_auth.probe_session", _probe)

    with pytest.raises(SystemExit) as exc:
        export_session_and_exit()
    assert exc.value.code == 0

    payload = json.loads(export_path.read_text())
    assert set(payload.keys()) == {"cookies"}
    assert payload["cookies"] == {"li_at": "abc", "JSESSIONID": "x"}
    assert probes == [{"li_at": "abc", "JSESSIONID": "x"}]

    out = capsys.readouterr().out
    assert 'status\t"success"' in out or "success" in out


def test_export_refuses_dead_session(monkeypatch, tmp_path):
    from linkedin_mcp_server.config import AppConfig
    from linkedin_mcp_server.cli_main import export_session_and_exit

    config = AppConfig()
    config.server.export_session = str(tmp_path / "portable.json")

    manager = _patch_export_deps(monkeypatch, config, {"li_at": "abc"})
    monkeypatch.setattr(
        "linkedin_mcp_server.voyager_auth.probe_session", lambda cookies: "dead"
    )

    with pytest.raises(SystemExit) as exc:
        export_session_and_exit()
    assert exc.value.code == 1
    assert not (tmp_path / "portable.json").exists()


def test_import_round_trips_probed_cookies(monkeypatch, capsys, tmp_path):
    from linkedin_mcp_server.config import AppConfig
    from linkedin_mcp_server.cli_main import import_session_and_exit

    portable = tmp_path / "portable.json"
    portable.write_text(json.dumps({"cookies": {"li_at": "abc", "bscookie": "y"}}))

    config = AppConfig()
    config.server.import_session = str(portable)

    probes = []

    def _probe(cookies):
        probes.append(cookies)
        return "alive"

    manager = _patch_export_deps(monkeypatch, config, {})
    monkeypatch.setattr("linkedin_mcp_server.voyager_auth.probe_session", _probe)

    with pytest.raises(SystemExit) as exc:
        import_session_and_exit()
    assert exc.value.code == 0

    assert probes == [{"li_at": "abc", "bscookie": "y"}]
    assert manager.saved == {"li_at": "abc", "bscookie": "y"}

    out = capsys.readouterr().out
    assert "success" in out


def test_import_rejects_non_single_wrap(monkeypatch, tmp_path):
    from linkedin_mcp_server.config import AppConfig
    from linkedin_mcp_server.cli_main import import_session_and_exit

    config = AppConfig()
    portable = tmp_path / "portable.json"
    portable.write_text(json.dumps({"li_at": "abc"}))  # not wrapped

    config.server.import_session = str(portable)

    probe_called = []

    def _probe(cookies):
        probe_called.append(cookies)
        return "alive"

    manager = _patch_export_deps(monkeypatch, config, {})
    monkeypatch.setattr("linkedin_mcp_server.voyager_auth.probe_session", _probe)

    with pytest.raises(SystemExit) as exc:
        import_session_and_exit()
    assert exc.value.code == 1
    assert manager.saved is None
    assert probe_called == []


def test_import_rejects_probe_dead(monkeypatch, tmp_path):
    from linkedin_mcp_server.config import AppConfig
    from linkedin_mcp_server.cli_main import import_session_and_exit

    config = AppConfig()
    portable = tmp_path / "portable.json"
    portable.write_text(json.dumps({"cookies": {"li_at": "stale"}}))
    config.server.import_session = str(portable)

    manager = _patch_export_deps(monkeypatch, config, {})
    monkeypatch.setattr(
        "linkedin_mcp_server.voyager_auth.probe_session", lambda cookies: "dead"
    )

    with pytest.raises(SystemExit) as exc:
        import_session_and_exit()
    assert exc.value.code == 1
    assert manager.saved is None
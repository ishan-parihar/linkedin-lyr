"""Tests for the persistent auth audit ledger (linkedin_mcp_server.auth_audit)."""

import json

import pytest

from linkedin_mcp_server import auth_audit


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    """Point the audit ledger at a temp auth root with a sample jar."""
    monkeypatch.setattr(
        auth_audit, "audit_log_path", lambda: tmp_path / "auth-audit.jsonl"
    )
    monkeypatch.setattr(
        auth_audit,
        "li_at_fingerprint",
        lambda: {"li_at_tail": "abcd1234", "jar_cookies": 16},
    )
    return tmp_path / "auth-audit.jsonl"


def test_tool_call_appends_jsonl(ledger):
    auth_audit.log_tool_call("get_inbox", ok=True, duration_s=1.5)
    auth_audit.log_tool_call(
        "search_conversations", ok=False, duration_s=12.0, error="TimeoutError: boom"
    )
    lines = ledger.read_text().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(l) for l in lines)
    assert first["event"] == "tool_call"
    assert first["tool"] == "get_inbox"
    assert first["ok"] is True
    assert first["li_at_tail"] == "abcd1234"
    assert second["ok"] is False
    assert "TimeoutError" in second["error"]


def test_auth_event_invalidation_signals(ledger):
    auth_audit.log_auth_event("redirect_storm", detail="messaging too many redirects", wait_time=3600)
    auth_audit.log_auth_event("probe_alive")
    summary = auth_audit.summarize(limit=10)
    assert summary["records"] == 2
    assert len(summary["invalidations"]) == 1
    assert summary["invalidations"][0]["signal"] == "redirect_storm"
    assert summary["invalidations"][0]["wait_time"] == 3600


def test_ledger_never_raises_on_unwritable_path(monkeypatch, tmp_path):
    def boom():
        raise OSError("read-only filesystem")

    monkeypatch.setattr(auth_audit, "audit_log_path", boom)
    # Must not raise despite the unwritable ledger path.
    auth_audit.log_tool_call("get_inbox", ok=True, duration_s=0.1)
    auth_audit.log_auth_event("session_expired")


def test_ledger_permissions_are_600(ledger):
    import os

    auth_audit.log_tool_call("get_inbox", ok=True, duration_s=0.1)
    assert ledger.exists()
    assert (ledger.stat().st_mode & 0o777) == 0o600


def test_summarize_counts_tool_frequency(ledger):
    for _ in range(3):
        auth_audit.log_tool_call("get_inbox", ok=True, duration_s=0.1)
    auth_audit.log_tool_call("send_message", ok=False, duration_s=1.0, error="x")
    summary = auth_audit.summarize()
    assert summary["tools"]["get_inbox"]["calls"] == 3
    assert summary["tools"]["send_message"]["errors"] == 1

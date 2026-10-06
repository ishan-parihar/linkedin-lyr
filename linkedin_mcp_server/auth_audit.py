"""Persistent, append-only auth audit trail for linkedin-lyr.

Answers the debugging question the per-run traces cannot: "which tool call
used or invalidated the cookie, and when did the issue start?"

The per-run trace system (``trace-runs/run-*/``) is ephemeral by design —
kept only when a run errors, one directory per MCP invocation. When li_at is
rotated server-side, correlating the invalidation with the calls that
preceded it means hand-sorting hundreds of directories. This module writes
a single append-only ledger instead:

    ~/.linkedin-lyr/auth-audit.jsonl   (one JSON object per line)

Event types:

* ``tool_call``   — every MCP tool invocation: name, outcome, duration,
  error class. Emitted from the tool middleware, so direct-CLI tool calls
  and MCP-server calls are both covered.
* ``auth_event``  — every auth-invalidation or session-health signal:
  auth_failed, session_expired, rate_limit, redirect_storm (the 3600s
  server-side automation flag), jar_persist (a cookie jar was written),
  cooldown_armed, probe_alive, probe_dead.
* Each record carries a non-secret ``li_at`` fingerprint (last 8 chars)
  and the jar's cookie count when readable, so an invalidation event can
  be matched against the jar that was in flight.

Cookie VALUES are never written. Only fingerprints and counts.

All writes are best-effort: an audit failure must never break the tool
call it is auditing. The file is chmod 600 and appends are flock-serialised
so concurrent MCP servers do not interleave partial lines.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from linkedin_mcp_server.session_state import (
    auth_root_dir,
    portable_cookie_path as cookies_path,
)

logger = logging.getLogger(__name__)

# Rotate the ledger at 5 MiB; keep exactly one previous generation.
_MAX_BYTES = 5 * 1024 * 1024

# Events that count as an auth-invalidation signal for summary purposes.
_INVALIDATION_EVENTS = frozenset(
    {"auth_failed", "session_expired", "redirect_storm", "probe_dead"}
)


def audit_log_path() -> Path:
    """Path of the persistent auth audit ledger."""
    return auth_root_dir() / "auth-audit.jsonl"


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            prev = path.with_suffix(path.suffix + ".1")
            if prev.exists():
                prev.unlink()
            path.rename(prev)
    except OSError:
        logger.debug("Auth audit rotation failed", exc_info=True)


def _append(record: dict[str, Any]) -> None:
    """Append one record, best-effort. Never raises into the caller."""
    try:
        path = audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        # os.open mode is masked by umask; enforce 600 explicitly.
        os.chmod(path, 0o600)
    except Exception:
        logger.debug("Auth audit append failed", exc_info=True)


def _base_record(event: str) -> dict[str, Any]:
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
    }


def li_at_fingerprint() -> dict[str, Any]:
    """Non-secret fingerprint of the current root jar: li_at tail + count.

    Best-effort: returns an empty-marker dict when the jar is unreadable
    so callers never need to guard the call.
    """
    try:
        data = json.loads(cookies_path().read_text())
        if isinstance(data, dict):
            li_at = data.get("li_at") or ""
            count = len(data)
        elif isinstance(data, list):
            names = {c.get("name"): c.get("value") for c in data if isinstance(c, dict)}
            li_at = names.get("li_at") or ""
            count = len(data)
        else:
            li_at, count = "", 0
        return {
            "li_at_tail": (li_at[-8:] if li_at else None),
            "jar_cookies": count,
        }
    except Exception:
        return {"li_at_tail": None, "jar_cookies": 0}


def log_tool_call(
    tool_name: str,
    *,
    ok: bool,
    duration_s: float,
    error: str | None = None,
    arg_keys: list[str] | None = None,
) -> None:
    """Record one MCP tool invocation (call or direct-CLI tool run)."""
    record = _base_record("tool_call")
    record.update(
        {
            "tool": tool_name,
            "ok": ok,
            "duration_s": round(duration_s, 3),
        }
    )
    if arg_keys:
        # Keys only, never values: tool args can contain message drafts and
        # profile handles, and the ledger must stay shareable.
        record["arg_keys"] = arg_keys[:10]
    if error:
        record["error"] = str(error)[:300]
    record.update(li_at_fingerprint())
    _append(record)


def log_auth_event(
    event: str,
    detail: str | None = None,
    *,
    context: str | None = None,
    wait_time: int | None = None,
) -> None:
    """Record an auth-invalidation or session-health signal.

    ``event`` is a free-form tag; the canonical ones are listed in the
    module docstring and mirrored in ``_INVALIDATION_EVENTS``.
    """
    record = _base_record("auth_event")
    record["signal"] = event
    if context:
        record["context"] = context
    if wait_time is not None:
        record["wait_time"] = wait_time
    if detail:
        record["detail"] = str(detail)[:300]
    record.update(li_at_fingerprint())
    _append(record)


def read_audit(limit: int = 200) -> list[dict[str, Any]]:
    """Read the most recent ``limit`` records, oldest first."""
    path = audit_log_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def summarize(limit: int = 200) -> dict[str, Any]:
    """Summarise recent activity: tool frequency, errors, invalidations."""
    records = read_audit(limit)
    tools: dict[str, dict[str, Any]] = {}
    invalidations: list[dict[str, Any]] = []
    for r in records:
        if r.get("event") == "tool_call":
            name = r.get("tool", "?")
            slot = tools.setdefault(name, {"calls": 0, "errors": 0, "last": None})
            slot["calls"] += 1
            if not r.get("ok"):
                slot["errors"] += 1
                slot["last"] = r.get("error", "")
            slot.setdefault("last_ts", r.get("ts"))
        elif r.get("event") == "auth_event" and r.get("signal") in _INVALIDATION_EVENTS:
            invalidations.append(
                {
                    "ts": r.get("ts"),
                    "signal": r.get("signal"),
                    "context": r.get("context"),
                    "wait_time": r.get("wait_time"),
                    "detail": r.get("detail"),
                }
            )
    return {
        "records": len(records),
        "window_start": records[0].get("ts") if records else None,
        "tools": tools,
        "invalidations": invalidations,
        "audit_path": str(audit_log_path()),
    }

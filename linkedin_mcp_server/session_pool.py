"""Multi-session cookie pool with probe-first liveness and failover.

The failure mode this module exists to prevent (#2329, #2554): a single
on-disk ``li_at`` goes stale server-side after a session rotation, the legacy
validator loads it into an automated browser, and the browser boot *is* the
thing that triggers the next rotation. The pool fixes both halves:

* **Probe-first liveness** — every session's verdict comes from the direct-HTTP
  Voyager probe in ``voyager_auth`` (#2430). A stored session only counts as
  healthy when HTTPS says so; the automated browser is never booted to decide.
* **Failover** — many sessions (exported from real logged-in browsers per step 4
  of the fix ladder) live side-by-side here. ``first_live_session`` walks them
  in order and returns the first one that probes alive, pruning the dead.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import NamedTuple

from linkedin_mcp_server.session_state import auth_root_dir
from linkedin_mcp_server.voyager_auth import ProbeVerdict, aprobe_session

logger = logging.getLogger(__name__)

# Sessions are stored one file per entry under this directory, each in the
# single-wrap Obscura flat shape ({"cookies": {flat}}) so every consumer that
# already reads portable_cookie_path reads these verbatim (#2201 invariant).
_SESSIONS_DIR = "sessions"

_FLAT_KEY = "cookies"


class PoolEntry(NamedTuple):
    name: str
    path: Path
    cookies: dict[str, str]


class ProbeOutcome(NamedTuple):
    name: str
    verdict: ProbeVerdict


def sessions_dir(source_profile_dir: Path | None = None) -> Path:
    """Directory holding one single-wrap cookies file per pooled session."""
    d = auth_root_dir(source_profile_dir) / _SESSIONS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_path(name: str, source_profile_dir: Path | None = None) -> Path:
    # name is a plain stem; it only escapes the sessions dir if it contains
    # separators, which we refuse on the way in (see add_session).
    return sessions_dir(source_profile_dir) / f"{name}.json"


def _read_wrapped(path: Path) -> dict[str, str] | None:
    """Read a single-wrap Obscura file, returning the flat cookie dict."""
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    flat = blob.get(_FLAT_KEY) if isinstance(blob, dict) else None
    if not isinstance(flat, dict):
        return None
    # The single-wrap invariant (#2201): the nested value must itself hold
    # name->value, not another {"cookies": ...} wrapper.
    if _FLAT_KEY in flat:
        inner = flat.get(_FLAT_KEY)
        if isinstance(inner, dict):
            return dict(inner)
    return {str(k): str(v) for k, v in flat.items()}


def list_sessions(source_profile_dir: Path | None = None) -> list[PoolEntry]:
    """Enumerate every pooled session file, skipping unreadable ones.

    Order is stable (name-sorted) so failover is predictable and
    deterministic across restarts.
    """
    d = sessions_dir(source_profile_dir)
    entries: list[PoolEntry] = []
    for path in sorted(d.glob("*.json")):
        cookies = _read_wrapped(path)
        if cookies is None:
            continue
        entries.append(PoolEntry(name=path.stem, path=path, cookies=cookies))
    return entries


def add_session(
    name: str,
    cookies: dict[str, str],
    source_profile_dir: Path | None = None,
) -> Path:
    """Persist one session in the pool, normalized to the single-wrap shape.

    Writes happen via a temp file + atomic rename so a crash mid-write never
    leaves a half-written session that failover would probe as dead.
    """
    name = str(name).strip()
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError(f"invalid session name: {name!r}")
    if not cookies.get("li_at"):
        raise ValueError(f"session {name!r} has no li_at; refusing to pool it")

    path = _session_path(name, source_profile_dir)
    blob = {_FLAT_KEY: json.loads(json.dumps(cookies))}

    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(blob, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def remove_session(name: str, source_profile_dir: Path | None = None) -> bool:
    path = _session_path(name, source_profile_dir)
    if path.exists():
        path.unlink()
        return True
    return False


async def probe_sessions(
    source_profile_dir: Path | None = None,
    *,
    timeout: float = 10.0,
) -> list[ProbeOutcome]:
    """Probe every pooled session over direct HTTP. Never boots a browser.

    A session that fails to probe (missing cookie, network blip, rotated
    session) is left on disk — failover prunes it only after another live
    session has been confirmed, so a transient network error can't nuke the
    last good cookie (see prune_dead_sessions).
    """
    outcomes: list[ProbeOutcome] = []
    for entry in list_sessions(source_profile_dir):
        verdict = await aprobe_session(entry.cookies, timeout=timeout)
        outcomes.append(ProbeOutcome(name=entry.name, verdict=verdict))
    return outcomes


async def prune_dead_sessions(
    source_profile_dir: Path | None = None,
    *,
    timeout: float = 10.0,
) -> tuple[list[PoolEntry], list[str]]:
    """Remove sessions that probe dead, *only if a live one remains*.

    Returns ``(live_entries, pruned_names)``. The guard "only prune when
    something else is alive" is the honest-degradation rule from the fix
    ladder: when the entire pool is dead the files stay for diagnosis and
    re-login, not for failover. Pruning is idempotent.
    """
    live: list[PoolEntry] = []
    dead_names: list[str] = []
    for entry in list_sessions(source_profile_dir):
        verdict = await aprobe_session(entry.cookies, timeout=timeout)
        if verdict == "alive":
            live.append(entry)
        elif verdict == "dead":
            dead_names.append(entry.name)
    if not live:
        logger.warning(
            "pool has no live session (%d dead); leaving files for diagnosis",
            len(dead_names),
        )
        return [], dead_names
    for name in dead_names:
        remove_session(name, source_profile_dir)
    return live, dead_names


async def first_live_session(
    source_profile_dir: Path | None = None,
    *,
    timeout: float = 10.0,
) -> PoolEntry | None:
    """Return the first pooled session that probes alive, or None.

    This is the failover entry point Fix 2 wires the runtime resolution chain
    to. Deterministic order (name-sorted via list_sessions) keeps failover
    stable across restarts. Never boots a browser.
    """
    for entry in list_sessions(source_profile_dir):
        verdict = await aprobe_session(entry.cookies, timeout=timeout)
        if verdict == "alive":
            return entry
    return None
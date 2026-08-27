"""Small shared helpers used across diagnostics and session-state modules."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def _load_env_file(path: Path) -> None:
    """Source *path* into the environment via setdefault (explicit env wins)."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        # Allow leading 'export ' as used by bf CLI's .browsefleet.env
        if line.startswith("export "):
            line = line[7:].strip()
            if "=" not in line:
                continue
        key, _, value = line.partition("=")
        key = key.strip()
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))
        # Back-compat alias: some installs use BROWSEFLEET_API_KEY
        if key == "BROWSEFLEET_API_KEY":
            os.environ.setdefault("BROWSEFLEET_TOKEN", value.strip().strip('"').strip("'"))


def load_proxy_env() -> None:
    """Source ``~/.linkedin-lyr/proxy.env`` and BrowseFleet envs (setdefault).

    Deployments behind a different egress than the machine's default (e.g. a
    VPS routing LinkedIn through a home residential SOCKS tunnel) declare it
    once in that file; every entrypoint — console script, ``python -m``, MCP
    server mode — inherits it without per-consumer configuration. Explicit
    environment always wins.

    Also sources BrowseFleet thin-client envs so a single ``bf.env`` or
    ``~/.browsefleet.env`` is enough for the VPS to speak to
    ``https://browsefleet.ishanparihar.com`` without per-job exports:
      - ``~/.linkedin-lyr/bf.env``  (preferred, LINKEDIN_* + BROWSEFLEET_*)
      - ``~/.browsefleet.env``        (bf CLI install, BROWSEFLEET_URL/TOKEN)
      - ``~/.linkedin-lyr/proxy.env`` (SOCS egress)
    Order is bf.env → browsefleet.env → proxy.env so an explicit
    LINKEDIN_BROWSER_BACKEND=browsefleet in bf.env is not shadowed.
    """
    _load_env_file(Path.home() / ".linkedin-lyr" / "bf.env")
    _load_env_file(Path.home() / ".browsefleet.env")
    _load_env_file(Path.home() / ".linkedin-lyr" / "proxy.env")


_PRIVATE_DIR_MODE = 0o700


def slugify_fragment(value: str) -> str:
    """Return a lowercase URL/file-safe fragment."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def utcnow_iso() -> str:
    """Return the current UTC timestamp in a compact ISO-8601 form."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def secure_mkdir(path: Path, mode: int = 0o700) -> None:
    """Create a directory tree with restrictive permissions.

    Unlike ``Path.mkdir(parents=True, mode=...)``, this applies *mode* to
    every newly created directory in the chain, not just the leaf.
    """
    if path.exists() and not path.is_dir():
        raise NotADirectoryError(f"Path exists and is not a directory: {path}")

    missing: list[Path] = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for part in reversed(missing):
        part.mkdir(mode=mode, exist_ok=True)


def harden_linkedin_tree(path: Path) -> None:
    """Ensure dirs from *path* up to ``.linkedin`` are owner-only (``0o700``).

    Complements :func:`secure_mkdir` by hardening pre-existing directories that
    may have been created with default umask permissions. No-op on Windows or
    when *path* is not inside a ``.linkedin`` directory.
    """
    if os.name == "nt":
        return
    d = path if path.is_dir() else path.parent
    # Bail out early when the path is not inside a .linkedin tree.
    if not any(p.name == ".linkedin" for p in (d, *d.parents)):
        return
    for p in (d, *d.parents):
        if p.is_dir() and stat.S_IMODE(p.stat().st_mode) != _PRIVATE_DIR_MODE:
            p.chmod(_PRIVATE_DIR_MODE)
        if p.name == ".linkedin":
            return


def is_still_at(fd: int, path: Path) -> bool:
    """Whether *fd* is still the file that *path* names.

    Compared by device and inode rather than by counting links. A count catches
    an unlink and misses a rename: the inode keeps its one link while the name
    comes to mean a different file, so the count still reads as one. A path that
    has since vanished counts as changed.

    Only meaningful once whatever the caller wanted to establish is in hand.
    Asked earlier, the answer can stop being true immediately afterwards.
    """
    held = os.fstat(fd)
    try:
        current = path.stat()
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


def secure_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    """Atomically write *content* to *path* with owner-only permissions.

    Uses a temp file + ``os.replace`` in the same directory so the write is
    atomic on the same filesystem and avoids TOCTOU permission races.
    """
    secure_mkdir(path.parent)
    fd_int, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd_int, "w") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise

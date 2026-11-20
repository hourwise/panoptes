"""Radiance profile files: a read-only look inside an engine's ``profiles_dir``.

Nothing here writes, renames, copies or applies anything — the dashboard only
shows what is already on disk so a human can compare profiles and see which one
the engine is running. Two rules keep that honest:

* a name must match ``NAME_RE`` (a plain ``*.yaml``/``*.yml`` file name) and the
  fully resolved path must still sit inside the directory, so ``..``, an
  embedded ``/`` and a symlink out of the directory all fail the same way;
* a file is read at most ``MAX_BYTES`` (64 KB), so a pathological file cannot be
  dragged into a page, and a diff is capped at ``MAX_DIFF_LINES``.

Everything is parameterised by the directory, so the tests can point it at a
fixture directory instead of the live ``~/Applications/Radiance``. A file or
directory that is missing yields nothing rather than invented data.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

# A profile is one plain, visible file in the directory: never a path, never a
# dot-name (hidden editor leftovers are not profiles), never anything but
# *.yaml/*.yml. This also decides which files show up in a listing, so a
# listing can never contain a name ``read_profile`` would refuse.
NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*\.ya?ml$")

MAX_BYTES = 64 * 1024  # one file is read at most this far
MAX_DIFF_LINES = 200  # a longer diff is cut, with a marker line
SHA_LENGTH = 12


class ProfileError(Exception):
    """Nothing servable behind this request; the route answers 404."""


def _base(dir_path: str | Path) -> Path:
    base = Path(dir_path)
    if not base.is_dir():
        raise ProfileError(f"{dir_path} is not a directory")
    return base


def _served_file(base: Path, candidate: Path) -> Path | None:
    """``candidate`` resolved, when it is a real file inside the resolved ``base``."""
    root = base.resolve()
    resolved = candidate.resolve()
    return resolved if root in resolved.parents and resolved.is_file() else None


def _read(path: Path) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(MAX_BYTES)
    except OSError as exc:
        raise ProfileError(f"{path.name}: {exc}") from exc


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _text_of(path: Path | None) -> str | None:
    """The file's text, or None when there is no readable file at ``path``."""
    if path is None:
        return None
    try:
        return _text(_read(path))
    except ProfileError:
        return None


def _modified(path: Path) -> str:
    """mtime as an ISO 8601 timestamp in UTC."""
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def _diff(active_text: str | None, text: str, active_name: str, name: str) -> str:
    """Unified diff capped at MAX_DIFF_LINES lines; "" when there is nothing to diff."""
    if active_text is None:
        return ""
    lines = list(
        difflib.unified_diff(
            active_text.splitlines(), text.splitlines(), fromfile=active_name, tofile=name, lineterm=""
        )
    )
    if len(lines) <= MAX_DIFF_LINES:
        return "\n".join(lines)
    keep = MAX_DIFF_LINES - 1
    return "\n".join([*lines[:keep], f"... {len(lines) - keep} more diff line(s) truncated"])


def list_profiles(dir_path: str | Path, active: str | None = None) -> list[dict]:
    """One row per profile file, the active one first, then by name.

    ``active`` is the file name the engine is running (or None). A row carries
    ``name``, ``size`` (bytes on disk), ``modified`` (ISO 8601 UTC),
    ``sha256_12`` (first 12 hex of the content hash), ``active`` and
    ``diff_vs_active`` — "" for the active file, and for every file when there
    is no readable active profile. Files this reader cannot serve (other
    extension, symlink out of the directory, unreadable) are left out; a
    directory that is gone or unreadable raises ProfileError instead of
    inventing an answer.
    """
    base = _base(dir_path)
    active_name = active if isinstance(active, str) and NAME_RE.match(active) else None
    active_text = _text_of(_served_file(base, base / active_name) if active_name else None)
    try:
        entries = list(base.iterdir())
    except OSError as exc:  # a directory that went unreadable holds nothing
        raise ProfileError(f"{dir_path}: {exc}") from exc

    rows: list[dict] = []
    for entry in entries:
        if not NAME_RE.match(entry.name):
            continue
        served = _served_file(base, entry)
        if served is None:
            continue
        try:
            data = _read(served)
            size = served.stat().st_size
            modified = _modified(served)
        except (OSError, ProfileError):  # vanished or unreadable since the check
            continue
        rows.append(
            {
                "name": entry.name,
                "size": size,
                "modified": modified,
                "sha256_12": hashlib.sha256(data).hexdigest()[:SHA_LENGTH],
                "active": entry.name == active_name,
                "diff_vs_active": ""
                if entry.name == active_name
                else _diff(active_text, _text(data), active_name or "", entry.name),
            }
        )
    rows.sort(key=lambda row: (not row["active"], row["name"]))
    return rows


def read_profile(dir_path: str | Path, name: str) -> str:
    """The profile's text (at most MAX_BYTES, undecodable bytes replaced)."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ProfileError(f"invalid profile name {name!r}")
    base = _base(dir_path)
    served = _served_file(base, base / name)
    if served is None:
        raise ProfileError(f"profile {name!r} is not a file in {dir_path}")
    return _text(_read(served))

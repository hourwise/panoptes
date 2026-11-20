"""Panoptes configuration: which inference engines to watch.

``load_config`` reads a TOML file (stdlib ``tomllib``) and returns a plain
dict ``{"engines": [...], "warnings": [...]}``. Search order for the file is
the ``path`` argument, then ``$PANOPTES_CONFIG``, then ``config/panoptes.toml``
next to the repo root. A missing file yields the built-in default below, so a
fresh checkout still shows the local Radiance box.

A top-level ``[[harnesses]]`` table names the agent harnesses whose live
activity the dashboard shows (see :mod:`wsd.harness`); entries are validated
the same way — broken ones are skipped with a warning.

An engine entry may also carry the profile keys ``profiles_dir`` (absolute
directory), ``active_profile`` (a *.yaml/*.yml name inside it) and
``journal_dir`` (absolute directory holding the profile-switch journal, which
need not exist yet; unset it defaults to ``config-journal`` under the records
dir). They are machine-specific, so the dashboard must work when
either is missing; a bad value costs the key and adds a warning, never a crash.
Invalid engine entries are skipped with a warning — a broken config degrades
the dashboard, it never crashes it.

A top-level ``[records] dir`` names the supervisor's records directory (task
packets, reviews, ``perf/tasks.jsonl``). It is machine-specific like the
engine paths: absent or unusable, ``records_dir`` is None and the job
history simply stays empty.

A top-level ``[guardian] state_file`` points at the state file the future
Panoptes Guardian service writes (``.pm/milestones/GUARDIAN.md`` §9). That
service may well not exist yet, so the file must not exist for the config to be
good: only the shape of the path is checked, and a bad value falls back to the
default (``$XDG_STATE_HOME`` or ``~/.local/state``) with a warning.
"""

import copy
import os
import re
import tomllib
from pathlib import Path
from urllib.parse import urlparse

from .guardian_state import default_state_path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "panoptes.toml"
ENV_VAR = "PANOPTES_CONFIG"

ENGINE_TYPES = ("radiance", "openai-compatible", "llama.cpp")
HARNESS_TYPES = ("omp",)

DEFAULT_CONFIG = {
    "engines": [
        {
            "id": "radiance-r9700",
            "type": "radiance",
            "base_url": "http://127.0.0.1:8000",
            "label": "Radiance \u00b7 R9700",
        }
    ]
}


# A Radiance profile file name: a plain *.yaml/*.yml name, never a path.
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.ya?ml$")

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _is_loopback_host(host: str) -> bool:
    host = host.lower()
    if host in _LOOPBACK_HOSTS:
        return True
    # every 127.0.0.0/8 address is loopback
    parts = host.split(".")
    return len(parts) == 4 and parts[0] == "127" and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts[1:])


def _valid_profiles_dir(value, warnings: list, where: str) -> str | None:
    """Absolute existing directory, or None (with a warning) when unusable."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        warnings.append(f"{where}: profiles_dir {value!r} is not a path string, dropped")
        return None
    path = Path(value)
    if not path.is_absolute():
        warnings.append(f"{where}: profiles_dir {value!r} is not absolute, dropped")
        return None
    if not path.is_dir():
        warnings.append(f"{where}: profiles_dir {value!r} is not a directory, dropped")
        return None
    return str(path)


def _valid_active_profile(value, profiles_dir: str, warnings: list, where: str) -> str | None:
    """A *.yaml/*.yml name that exists in ``profiles_dir``, else None + warning."""
    if value is None:
        return None
    if not isinstance(value, str) or not PROFILE_NAME_RE.match(value):
        warnings.append(f"{where}: active_profile {value!r} is not a *.yaml/*.yml file name, dropped")
        return None
    if not (Path(profiles_dir) / value).is_file():
        warnings.append(f"{where}: active_profile {value!r} is not a file in {profiles_dir}, dropped")
        return None
    return value


def _valid_journal_dir(value, warnings: list, where: str) -> str | None:
    """Absolute directory for the switch journal, or None (with a warning).

    It need not exist — the pipeline creates it on the first switch — so only
    the shape is checked; a file sitting there is not a directory.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        warnings.append(f"{where}: journal_dir {value!r} is not a path string, dropped")
        return None
    path = Path(value)
    if not path.is_absolute():
        warnings.append(f"{where}: journal_dir {value!r} is not absolute, dropped")
        return None
    if path.exists() and not path.is_dir():
        warnings.append(f"{where}: journal_dir {value!r} is not a directory, dropped")
        return None
    return str(path)


def _valid_engine(entry, warnings: list) -> dict | None:
    """Cleaned engine dict, or None (with a warning) when the entry is invalid."""
    if not isinstance(entry, dict):
        warnings.append(f"engine entry {entry!r} is not a table")
        return None
    engine_id = entry.get("id")
    if not isinstance(engine_id, str) or not engine_id.strip():
        warnings.append(f"engine entry {entry!r} has no id")
        return None
    where = f"engine {engine_id!r}"
    engine_type = entry.get("type")
    if engine_type not in ENGINE_TYPES:
        warnings.append(f"{where}: unknown type {engine_type!r} (known: {', '.join(ENGINE_TYPES)})")
        return None
    base_url = entry.get("base_url")
    if not isinstance(base_url, str):
        warnings.append(f"{where}: base_url must be a string")
        return None
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        warnings.append(f"{where}: base_url scheme {parsed.scheme!r} is not http(s)")
        return None
    if not _is_loopback_host(parsed.hostname or ""):
        warnings.append(f"{where}: base_url host {parsed.hostname!r} is not loopback")
        return None
    label = entry.get("label")
    cleaned = {
        "id": engine_id,
        "type": engine_type,
        "base_url": base_url,
        "label": label if isinstance(label, str) and label.strip() else engine_id,
    }
    # Optional, machine-specific: this engine's profile directory, the profile
    # it is running now, and where switches get journalled. Bad values cost the
    # keys and a warning, never the engine.
    profiles_dir = _valid_profiles_dir(entry.get("profiles_dir"), warnings, where)
    if profiles_dir is not None:
        cleaned["profiles_dir"] = profiles_dir
        active = _valid_active_profile(entry.get("active_profile"), profiles_dir, warnings, where)
        if active is not None:
            cleaned["active_profile"] = active
        journal_dir = _valid_journal_dir(entry.get("journal_dir"), warnings, where)
        if journal_dir is not None:
            cleaned["journal_dir"] = journal_dir
    else:
        for key in ("active_profile", "journal_dir"):
            if key in entry:
                warnings.append(f"{where}: {key} without a usable profiles_dir, dropped")
    return cleaned


def _valid_harness(entry, warnings: list) -> dict | None:
    """Cleaned harness dict, or None (with a warning) when the entry is invalid.

    ``sessions_dir`` is what the adapter reads: without an existing absolute
    directory the harness has nothing to show, so the entry is skipped.
    ``lock_file`` is optional and needs no existing file: it comes and goes
    with each task; a bad value costs the key and a warning.
    """
    if not isinstance(entry, dict):
        warnings.append(f"harness entry {entry!r} is not a table")
        return None
    harness_id = entry.get("id")
    if not isinstance(harness_id, str) or not harness_id.strip():
        warnings.append(f"harness entry {entry!r} has no id")
        return None
    where = f"harness {harness_id!r}"
    harness_type = entry.get("type")
    if harness_type not in HARNESS_TYPES:
        warnings.append(f"{where}: unknown type {harness_type!r} (known: {', '.join(HARNESS_TYPES)})")
        return None
    sessions_dir = entry.get("sessions_dir")
    if (
        not isinstance(sessions_dir, str)
        or not sessions_dir.strip()
        or not Path(sessions_dir).is_absolute()
        or not Path(sessions_dir).is_dir()
    ):
        warnings.append(f"{where}: sessions_dir {sessions_dir!r} is not an existing absolute directory, skipped")
        return None
    label = entry.get("label")
    cleaned = {
        "id": harness_id,
        "type": harness_type,
        "sessions_dir": str(Path(sessions_dir)),
        "label": label if isinstance(label, str) and label.strip() else harness_id,
    }
    lock_file = entry.get("lock_file")
    if lock_file is not None:
        if not isinstance(lock_file, str) or not lock_file.strip() or not Path(lock_file).is_absolute():
            warnings.append(f"{where}: lock_file {lock_file!r} is not an absolute path, dropped")
        else:
            cleaned["lock_file"] = lock_file
    return cleaned


def _parse_records(raw: dict, candidate: Path, warnings: list) -> str | None:
    """The validated [records] dir, or None (with a warning) when unusable."""
    records_raw = raw.get("records")
    if records_raw is None:
        return None
    if not isinstance(records_raw, dict):
        warnings.append(f"{candidate}: records is {type(records_raw).__name__}, expected a table")
        return None
    value = records_raw.get("dir")
    if not isinstance(value, str) or not value.strip():
        warnings.append(f"{candidate}: records.dir {value!r} is not a path string, dropped")
        return None
    if not Path(value).is_absolute():
        warnings.append(f"{candidate}: records.dir {value!r} is not absolute, dropped")
        return None
    return value


def _parse_guardian(raw: dict | None, candidate: Path, warnings: list) -> str:
    """The validated ``[guardian] state_file``, or the default path when unset.

    Machine-specific like the records dir, and like ``journal_dir`` it need not
    exist: the Guardian service writes it, and until it does the chip honestly
    says "not installed". Only the shape is checked; a directory sitting there
    is not a file. A bad value costs the setting and adds a warning.
    """
    section = None if raw is None else raw.get("guardian")
    if section is None:
        return default_state_path()
    if not isinstance(section, dict):
        warnings.append(
            f"{candidate}: guardian is {type(section).__name__}, expected a table; "
            "the default state file is used"
        )
        return default_state_path()
    value = section.get("state_file")
    # a [guardian] table without a usable state_file says nothing, and saying
    # nothing is how a typo'd key stays invisible: warn like records.dir does
    if not isinstance(value, str) or not value.strip():
        warnings.append(
            f"{candidate}: guardian.state_file {value!r} is not a path string; "
            "the default state file is used"
        )
        return default_state_path()
    if not Path(value).is_absolute():
        warnings.append(
            f"{candidate}: guardian.state_file {value!r} is not absolute; "
            "the default state file is used"
        )
        return default_state_path()
    if Path(value).is_dir():
        warnings.append(
            f"{candidate}: guardian.state_file {value!r} is a directory; "
            "the default state file is used"
        )
        return default_state_path()
    return value


def load_config(path=None) -> dict:
    """Load and validate the engine config; never raises on bad input."""
    warnings: list[str] = []
    candidate = path or os.environ.get(ENV_VAR) or DEFAULT_CONFIG_PATH
    candidate = Path(candidate)
    raw = None
    if candidate.is_file():
        try:
            with open(candidate, "rb") as fh:
                raw = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            warnings.append(f"{candidate}: {exc}; using defaults")
    elif str(path or os.environ.get(ENV_VAR) or ""):
        # an explicitly configured path that is missing is worth mentioning;
        # the repo default simply not existing is the normal fresh-checkout case
        warnings.append(f"{candidate}: file not found; using defaults")
    if raw is None:
        return {
            "engines": copy.deepcopy(DEFAULT_CONFIG["engines"]),
            "harnesses": [],
            "warnings": warnings,
            "records_dir": None,
            "guardian_state_file": default_state_path(),
        }

    harnesses: list[dict] = []
    harnesses_raw = raw.get("harnesses")
    if harnesses_raw is not None and not isinstance(harnesses_raw, list):
        warnings.append(f"{candidate}: harnesses is {type(harnesses_raw).__name__}, expected a list")
        harnesses_raw = None
    seen_harness: set[str] = set()
    for entry in harnesses_raw or []:
        cleaned = _valid_harness(entry, warnings)
        if cleaned is None:
            continue
        if cleaned["id"] in seen_harness:
            warnings.append(f"harness {cleaned['id']!r}: duplicate id, skipped")
            continue
        seen_harness.add(cleaned["id"])
        harnesses.append(cleaned)

    engines_raw = raw.get("engines")
    if engines_raw is None:
        warnings.append(f"{candidate}: no [[engines]] section; using defaults")
        return {
            "engines": copy.deepcopy(DEFAULT_CONFIG["engines"]),
            "harnesses": harnesses,
            "warnings": warnings,
            "records_dir": _parse_records(raw, candidate, warnings),
            "guardian_state_file": _parse_guardian(raw, candidate, warnings),
        }
    if not isinstance(engines_raw, list):
        warnings.append(f"{candidate}: engines is {type(engines_raw).__name__}, expected a list")
        engines_raw = []

    records_dir = _parse_records(raw, candidate, warnings)
    guardian_state_file = _parse_guardian(raw, candidate, warnings)
    engines: list[dict] = []
    seen: set[str] = set()
    for entry in engines_raw:
        cleaned = _valid_engine(entry, warnings)
        if cleaned is None:
            continue
        if cleaned["id"] in seen:
            warnings.append(f"engine {cleaned['id']!r}: duplicate id, skipped")
            continue
        seen.add(cleaned["id"])
        engines.append(cleaned)
    return {"engines": engines, "harnesses": harnesses, "warnings": warnings,
            "records_dir": records_dir, "guardian_state_file": guardian_state_file}

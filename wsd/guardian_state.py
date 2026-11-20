"""The Panoptes Guardian state file, read for the header chip (read-only).

The Guardian milestone contract (§9) agrees one file between the future
Guardian service and this dashboard. The dashboard only ever reads it, and never
creates the directory or the file: while the service does not exist the chip
honestly says "not installed".

:func:`read_state` takes the path, the current time and an injectable pid check,
so a test needs neither a real process nor a real clock. Everything it reports
comes from the file; a field that is missing, wrong-typed or unknown costs its
own display and adds a warning — a value is never invented and a colour is
never claimed.

The seven display rules, in order:

1. file missing          -> ``off``     "Guardian · not installed"
2. unreadable / no JSON / not an object -> ``warn`` "Guardian · unreadable"
3. unknown ``schema``    -> ``off``     "Guardian · unsupported version"
4. no usable ``written_at``, a file older than :data:`STALE_AFTER_S` seconds,
   or a dead ``service.pid`` -> ``warn`` "Guardian · not responding"
   (a stale state never keeps its last colour)
5. ``estop.latched``     -> ``hot``     "E-STOP · <scope>"
6. otherwise by ``state``: GREEN ``ok``, AMBER/ORANGE ``warn``, RED ``hot``,
   UNKNOWN ``unknown``; `` (sim)`` is appended while ``service.mode`` is
   ``simulate``
7. ``detail`` lists the ``top`` rules and the open incident id, leaving out
   whatever the file does not say.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_V1 = "panoptes.guardian.state.v1"

#: The state file is stale once it is *older than* this many seconds. The
#: boundary itself is fresh on purpose: the service writes every 2 s, so
#: ``age == 10.0`` is five samples' worth of jitter and still counts as alive,
#: while 10.1 s is already "not responding".
STALE_AFTER_S = 10.0

#: Where the file lives when the config does not say otherwise, under
#: ``$XDG_STATE_HOME`` or ``~/.local/state``.
STATE_SUBPATH = ("panoptes", "guardian", "state.json")

LEVELS = ("off", "ok", "warn", "hot", "unknown")

#: Guardian state -> chip level. Anything the service reports that is not in
#: here gets ``unknown``, never the colour it might once have had.
STATE_LEVELS = {"GREEN": "ok", "AMBER": "warn", "ORANGE": "warn", "RED": "hot", "UNKNOWN": "unknown"}

#: The longest e-stop scope the chip shows. ``estop.scope`` is a worker id from
#: the Guardian configuration, or ``all``: the product knows no worker names, so
#: any non-empty string is shown exactly as the service reported it, cut at this
#: many characters so a long id cannot push the chip out of the header. The label
#: reaches the browser as plain text (``textContent``), never as HTML.
SCOPE_MAX = 32

SEP = " · "


def default_state_path(environ=None) -> str:
    """``<state home>/panoptes/guardian/state.json``.

    ``$XDG_STATE_HOME`` wins; without it (or with an empty one) ``~/.local/state``
    is used. Never touches the file system.
    """
    env = os.environ if environ is None else environ
    base = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return str(Path(base) / Path(*STATE_SUBPATH))


def pid_is_alive(pid) -> bool:
    """``os.kill(pid, 0)``: ``PermissionError`` means alive, ``ProcessLookupError`` dead."""
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False  # the contract says an integer pid; anything else proves nothing
    if pid <= 0:  # 0 and negatives address process groups, not a process
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):  # a pid outside the range is no process either
        return False
    return True


def _display(level: str, label: str, detail: str = "") -> dict:
    return {"level": level, "label": label, "detail": detail}


def _result(installed: bool, display: dict, state: dict | None, warnings: list) -> dict:
    return {"installed": installed, "display": display, "state": state, "warnings": warnings}


def _parse_stamp(value) -> datetime | None:
    """An ISO 8601 stamp as an aware UTC datetime, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:  # the contract says UTC; a naive stamp is read as UTC
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _num_text(value) -> str | None:
    """A number as short text, or None when it is not a usable number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return f"{value:g}"


def _rule_text(entry) -> str | None:
    """One ``top`` entry as ``rule value unit (threshold)``, minus whatever is absent."""
    if not isinstance(entry, dict):
        return None
    rule = entry.get("rule")
    if not isinstance(rule, str) or not rule.strip():
        return None
    parts = [rule.strip()]
    value = _num_text(entry.get("value"))
    if value is not None:
        parts.append(value)
    unit = entry.get("unit")
    if isinstance(unit, str) and unit.strip():
        parts.append(unit.strip())
    threshold = _num_text(entry.get("threshold"))
    if threshold is not None:
        parts.append(f"({threshold})")
    return " ".join(parts)


def _detail(doc: dict) -> str:
    """The tooltip text: the worst rules and the open incident, nothing else."""
    parts = []
    top = doc.get("top")
    if isinstance(top, list):
        parts.extend(text for text in (_rule_text(entry) for entry in top) if text)
    incident = doc.get("open_incident")
    if isinstance(incident, dict):
        ident = incident.get("id")
        if isinstance(ident, str) and ident.strip():
            parts.append(f"incident {ident.strip()}")
    return "; ".join(parts)


def read_state(path=None, now: datetime | None = None, pid_alive=pid_is_alive) -> dict:
    """Read ``path`` and derive what the header chip may honestly show.

    ``now`` defaults to the current UTC time; ``pid_alive`` is injectable so a
    test needs no real process. The return is
    ``{"installed", "display": {"level", "label", "detail"}, "state", "warnings"}``.
    Read-only: a missing path or directory stays missing.
    """
    warnings: list[str] = []
    target = Path(str(path)) if path else Path(default_state_path())
    if not target.is_file():
        return _result(
            False,
            _display("off", f"Guardian{SEP}not installed"),
            None,
            [f"no Guardian state file at {target}"],
        )
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.append(f"state file {str(target)!r} could not be read: {exc}")
        return _result(True, _display("warn", f"Guardian{SEP}unreadable"), None, warnings)
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        warnings.append(f"state file {str(target)!r} is not JSON: {exc}")
        return _result(True, _display("warn", f"Guardian{SEP}unreadable"), None, warnings)
    if not isinstance(doc, dict):
        warnings.append(f"state file {str(target)!r} holds {type(doc).__name__}, expected an object")
        return _result(True, _display("warn", f"Guardian{SEP}unreadable"), None, warnings)

    # 3. a schema this dashboard does not know: never guess its meaning.
    schema = doc.get("schema")
    if schema != SCHEMA_V1:
        warnings.append(f"schema {schema!r} is not {SCHEMA_V1!r}")
        return _result(True, _display("off", f"Guardian{SEP}unsupported version"), doc, warnings)

    service = doc.get("service") if isinstance(doc.get("service"), dict) else {}
    if not isinstance(doc.get("service"), dict):
        warnings.append("service is missing or not an object")

    # 4. fresh, or not responding: a stale colour is never shown.
    written = _parse_stamp(doc.get("written_at"))
    stale = written is None
    if written is None:
        warnings.append(f"written_at {doc.get('written_at')!r} is missing or not an ISO 8601 stamp")
    else:
        reference = now if now is not None else datetime.now(timezone.utc)
        age = (reference - written).total_seconds()
        if age > STALE_AFTER_S:
            warnings.append(f"the state was written {age:.1f} s ago, older than {STALE_AFTER_S:g} s")
            stale = True
    alive = pid_alive(service.get("pid"))
    if not alive:
        warnings.append(f"service pid {service.get('pid')!r} is not running")
    if stale or not alive:
        return _result(True, _display("warn", f"Guardian{SEP}not responding"), doc, warnings)

    detail = _detail(doc)

    # 5. a latched e-stop outranks every colour.
    estop = doc.get("estop") if isinstance(doc.get("estop"), dict) else {}
    if not isinstance(doc.get("estop"), dict):
        warnings.append("estop is missing or not an object")
    if estop.get("latched") is True:
        scope = estop.get("scope")
        if isinstance(scope, str) and scope.strip():
            label = f"E-STOP{SEP}{scope.strip()[:SCOPE_MAX]}"
        else:
            warnings.append(f"estop.scope {scope!r} is not a non-empty string "
                            "(a worker id from the Guardian configuration, or \"all\")")
            label = "E-STOP"
        return _result(True, _display("hot", label, detail), doc, warnings)

    # 6. the state itself.
    state = doc.get("state")
    known = state.upper() if isinstance(state, str) and state.strip() else None
    if known is None:
        warnings.append(f"state {state!r} is missing")
        level, name = "unknown", "UNKNOWN"
    elif known in STATE_LEVELS:
        level, name = STATE_LEVELS[known], known
    else:
        warnings.append(f"state {state!r} is not one of {', '.join(STATE_LEVELS)}")
        level, name = "unknown", known
    mode = service.get("mode")
    if mode not in ("simulate", "live"):
        warnings.append(f"service.mode {mode!r} is not simulate or live")
    label = f"Guardian{SEP}{name}" + (" (sim)" if mode == "simulate" else "")
    return _result(True, _display(level, label, detail), doc, warnings)

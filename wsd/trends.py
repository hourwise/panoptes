"""Lab trends: how every accepted task went, in acceptance order, with the
profile switches that happened in between (read-only).

Two sources, both never written here: the job history (:func:`wsd.jobs.load_jobs`,
so every metric arrives with the provenance class and scope :mod:`wsd.jobs` gave
it — nothing is recomputed, re-united or invented) and each engine's
profile-switch journal (:func:`wsd.controls.load_history`).

One point per accepted task that carries an ``accepted_at``, oldest first: a
task without one cannot be placed on the time axis, so it is left out and says
so in ``warnings`` rather than being guessed to the front or the back.

Only ``applied`` switches become markers. A refused switch never touched the
engine and a rolled-back one put everything back, so neither is something that
"happened" to a task; a switch still in flight (``outcome: running``) or one
whose rollback failed (the engine is not on the profile it asked for) is left
out for the same reason. ``at`` is the moment the attempt finished, so a point
counts as *after* the change from the moment the engine was serving it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import controls, jobs

#: The point key -> the job metric it reads. The names are the ones
#: ``wsd.jobs._METRICS`` already uses (``peak_context`` and ``compactions`` for
#: the ``last_session_*`` record keys); the point copies that metric dict as it
#: is, so ``value``, ``unit``, ``provenance`` and ``scope`` stay honest.
METRICS: tuple[str, ...] = (
    "worker_seconds",
    "total_energy_wh",
    "effective_worker_tps",
    "peak_context",
    "compactions",
)

#: Outcomes that changed what the engine runs: everything else in the journal
#: (refused, rolled_back, rollback_failed, running) describes no real change.
APPLIED = "applied"

#: Outcomes that say the task did NOT end accepted. Everything else in
#: ``perf/tasks.jsonl`` is the supervisor's record of an accepted task — a
#: review whose first line names no verdict reads ``unknown`` in
#: :mod:`wsd.jobs`, and such a task was still accepted — so those count.
NOT_ACCEPTED = ("rejected", "stopped")


def _stamp(value: object) -> datetime | None:
    """The instant an ISO stamp names, or None when it is not one.

    A stamp with no offset is read as UTC: the records and the journal both
    write ``+00:00``, and ordering a hand-made naive stamp as UTC keeps every
    comparison in this module aware-vs-aware (which naive would make raise).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _point(job: dict) -> dict:
    """One accepted task as a trend point: its own metrics, unaltered."""
    point = {
        "id": job["id"],
        "title": job["title"],
        "accepted_at": job["accepted_at"],
        "first_time_accept": job["first_time_accept"],
    }
    for name in METRICS:
        point[name] = job["metrics"][name]
    # rounds is a plain job figure, not a metric: no provenance to carry.
    point["rounds"] = job["rounds"]
    return point


def _marker(engine_id: str, entry: dict) -> dict | None:
    """A finished, applied switch as a marker, or None when it cannot be placed."""
    to = entry.get("to")
    name = to.get("name") if isinstance(to, dict) else None
    if _stamp(entry.get("finished")) is None or not isinstance(name, str) or not name:
        return None
    return {
        "engine": entry.get("engine") if isinstance(entry.get("engine"), str) else engine_id,
        "at": entry["finished"],
        "action": entry.get("action"),
        "to": name,
    }


def load_markers(journal_dirs: Mapping[str, str] | None, warnings: list[str]) -> list[dict]:
    """Every applied switch of every engine, oldest first.

    ``journal_dirs`` maps an engine id to the directory holding its journal (the
    same place ``GET /api/engines/<id>/history`` reads). A journal that does not
    exist simply has nothing to say; an entry that cannot be placed on the time
    axis — no finished stamp, no profile name — is counted in ``warnings``.
    """
    stamped: list[tuple[datetime, str, str, dict]] = []
    for engine_id in sorted(journal_dirs or {}):
        journal_dir = journal_dirs[engine_id]
        if not journal_dir:
            continue
        entries = controls.load_history(journal_dir, engine_id)
        for entry in entries:
            if entry.get("outcome") != APPLIED:
                continue
            marker = _marker(engine_id, entry)
            if marker is None:
                warnings.append(
                    f"engine {engine_id}: switch entry {entry.get('id')!r} is applied but has "
                    "no finished time or profile name, skipped"
                )
                continue
            stamped.append((_stamp(marker["at"]), marker["engine"], marker["to"], marker))
    stamped.sort(key=lambda item: (item[0], item[1], item[2]))
    return [marker for _, _, _, marker in stamped]


def load_trends(records_dir: str | None, journal_dirs: Mapping[str, str] | None = None) -> dict:
    """``{"points": [...], "markers": [...], "warnings": [...]}`` for the Lab trends card.

    Points are the accepted jobs that carry an ``accepted_at``, oldest first
    (equal stamps fall back to the task id, so the order is stable). Every
    metric is the job's own dict. Markers come from :func:`load_markers`.
    Without a records dir there is nothing to plot: both lists are empty and
    ``warnings`` says why, exactly as ``/api/lab/variants`` does.
    """
    warnings: list[str] = []
    if not records_dir:
        warnings.append("no [records] dir in the config; no task trends to show")
        return {"points": [], "markers": [], "warnings": warnings}
    if not Path(records_dir).is_dir():
        warnings.append(f"records dir {records_dir!r} not found; no task trends to show")
        return {"points": [], "markers": [], "warnings": warnings}
    dated: list[tuple[datetime, str, dict]] = []
    for job in jobs.load_jobs(records_dir):
        if job["outcome"] in NOT_ACCEPTED:
            warnings.append(f"{job['id']}: outcome is {job['outcome']}, not accepted; "
                            "left out of the trends")
            continue
        stamp = _stamp(job["accepted_at"])
        if stamp is None:
            shown = job["accepted_at"] or "missing"
            warnings.append(f"{job['id']}: accepted_at {shown}; left out of the trends")
            continue
        dated.append((stamp, job["id"], _point(job)))
    dated.sort(key=lambda item: (item[0], item[1]))
    return {
        "points": [point for _, _, point in dated],
        "markers": load_markers(journal_dirs, warnings),
        "warnings": warnings,
    }

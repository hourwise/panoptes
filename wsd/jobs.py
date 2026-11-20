"""Read-only job history built from the supervisor's stored task records.

Sources (never written): ``<records_dir>/perf/tasks.jsonl`` (one JSON object
per accepted task, the LAST line per task id wins), ``<records_dir>/tasks/
<id>.md`` (the packet: ``# T-xxx (Mn ...): Title``) and ``<records_dir>/
reviews/<id>.md`` (the review: first line ``# T-xxx review — ACCEPTED ...``).

Data honesty (PLAN.md): every metric carries a provenance class and a scope.
Values are never invented or coerced — a raw ``"~20000`` stays ``value: None``
with the string kept under ``raw``. Schema-1 backfill lines are ``partial``,
and so is every time-dependent metric of a record whose ``worker_seconds`` are
partial (a sampler that kept running through review inflates the energy too).
``last_session_*`` figures only describe the final harness session: with
rounds > 1 they are ``partial``, not task totals. ``prefix_hit`` and
``draft_accept`` are engine-lifetime counters, never job figures.
"""

import json
import re
from datetime import datetime
from pathlib import Path

REVIEW_EXCERPT_CHARS = 600

#: metric name -> (unit, provenance, scope, source record keys in order).
#: The provenance is the class of a complete value; backfill rows downgrade
#: everything, non-numeric raw values downgrade themselves.
_METRICS: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    ("worker_seconds", "s", "measured", "job", ("worker_seconds",)),
    ("gpu_energy_wh", "Wh", "measured", "job", ("gpu_energy_wh",)),
    ("cpu_energy_wh", "Wh", "measured", "job", ("cpu_energy_wh",)),
    ("total_energy_wh", "Wh", "measured", "job", ("total_energy_wh",)),
    ("energy_cost_gbp", "GBP", "calculated", "job", ("energy_cost_gbp",)),
    ("output_tokens", "tokens", "measured", "job", ("tokens_output_total",)),
    ("reasoning_tokens", "tokens", "measured", "job", ("tokens_reasoning_total",)),
    ("effective_worker_tps", "tok/s", "calculated", "job", ("effective_worker_tps",)),
    ("peak_context", "tokens", "measured", "job", ("last_session_peak_ctx", "peak_ctx")),
    ("compactions", "count", "measured", "job", ("last_session_compactions", "compactions_last_session")),
    # OMP records no retry event; these are the error signals it does write
    # (failed tool results, aborted requests). Counted over every session of
    # the task, so rounds > 1 does not make them partial, and they do not
    # grow with wall time. A record without the keys stays unavailable.
    ("failed_tool_calls", "count", "measured", "job", ("failed_tool_calls",)),
    ("aborted_requests", "count", "measured", "job", ("aborted_requests",)),
    ("python_tests", "tests", "measured", "job", ("python_tests",)),
    # Sampled from engine counters at review time: engine lifetime, not this job.
    ("prefix_hit", "ratio", "measured", "engine-lifetime", ("prefix_hit",)),
    ("draft_accept", "ratio", "measured", "engine-lifetime", ("draft_accept",)),
)

#: Metrics whose value grows with wall time: when ``worker_seconds`` is
#: partial the sampler window is wrong, so every time-dependent figure of
#: that record shares the doubt (e.g. a sampler left running through review).
_TIME_DEPENDENT = frozenset({
    "worker_seconds", "gpu_energy_wh", "cpu_energy_wh", "total_energy_wh",
    "energy_cost_gbp", "effective_worker_tps",
})

#: Record keys that describe only the last harness session.
_SESSION_KEYS = frozenset({"last_session_peak_ctx", "last_session_compactions"})
#: Metrics that can be sourced from those keys.
_LAST_SESSION_METRICS = frozenset({"peak_context", "compactions"})


_PACKET_RE = re.compile(r"^#\s*\S+(?:\s*\((?P<phase>[^)]*)\))?\s*:\s*(?P<title>.*)$")
_MILESTONE_RE = re.compile(r"\b(M\d+)\b")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _metric(record: dict, unit: str, provenance: str, scope: str, keys: tuple[str, ...],
            partial: bool, partial_keys: frozenset[str] = frozenset()) -> dict:
    """One metric cell; never coerces, never invents."""
    raw = None
    matched: str | None = None
    for key in keys:
        if record.get(key) is not None:
            raw = record[key]
            matched = key
            break
    if raw is None:
        return {"value": None, "unit": unit, "provenance": "unavailable", "scope": scope}
    if matched in partial_keys:
        partial = True
    if not _is_number(raw):
        return {"value": None, "unit": unit, "provenance": "partial", "scope": scope, "raw": raw}
    return {
        "value": raw,
        "unit": unit,
        "provenance": "partial" if partial else provenance,
        "scope": scope,
    }


def _outcome(review_header: str | None, record: dict) -> str:
    """Outcome from the review's first line; fallbacks when there is no review."""
    if review_header is not None:
        line = review_header.upper()
        if "ACCEPTED" in line:
            return "accepted"
        if "STOPPED" in line:
            return "stopped"
        if "REJECTED" in line or "SENT BACK" in line:
            return "rejected"
        return "unknown"
    return "partial" if record.get("partial") is True else "unknown"


def _read_first_lines(path: Path, count: int, limit: int) -> str | None:
    """First ``count`` lines of a text file, or None when it cannot be read."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    return "\n".join(text.splitlines()[:count])[:limit]


def _packet(base: Path, job_id: str) -> tuple[str | None, str | None]:
    """(title, milestone) from the packet's first line; (None, None) when absent."""
    first = _read_first_lines(base / "tasks" / f"{job_id}.md", 1, 2000)
    if first is None:
        return None, None
    match = _PACKET_RE.match(first)
    if match is None:
        return None, None
    milestone = None
    phase = match.group("phase")
    if phase:
        found = _MILESTONE_RE.search(phase)
        milestone = found.group(1) if found else None
    return match.group("title").strip() or None, milestone


def _accepted_at(record: dict) -> float | None:
    value = record.get("accepted_at")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _build_job(base: Path, record: dict) -> dict:
    job_id = record["task"]
    backfill = record.get("partial") is True
    review = _read_first_lines(base / "reviews" / f"{job_id}.md", 1, 1000)
    excerpt = None
    if review is not None:
        try:
            excerpt = (base / "reviews" / f"{job_id}.md").read_text(errors="replace")[:REVIEW_EXCERPT_CHARS]
        except OSError:
            excerpt = None
    outcome = _outcome(review, record)
    title, milestone = _packet(base, job_id)
    first_time_accept = record.get("first_time_accept")
    if not isinstance(first_time_accept, bool):
        rounds = record.get("rounds")
        if isinstance(rounds, int) and not isinstance(rounds, bool):
            first_time_accept = outcome == "accepted" and rounds == 1
        else:
            first_time_accept = None
    seconds_partial = backfill or record.get("worker_seconds_partial") is True
    rounds_value = record.get("rounds")
    # last_session_* figures describe only the final harness session; they are
    # task totals solely when the task ran exactly one round. An unknown round
    # count cannot certify them either.
    last_session_partial = not (_is_number(rounds_value) and rounds_value == 1)
    metrics = {}
    for name, unit, provenance, scope, keys in _METRICS:
        partial = backfill or (seconds_partial and name in _TIME_DEPENDENT)
        partial_keys = _SESSION_KEYS if name in _LAST_SESSION_METRICS and last_session_partial else frozenset()
        metrics[name] = _metric(record, unit, provenance, scope, keys, partial, partial_keys)
    # the record's own acceptance stamp, verbatim: the trends' x axis is built
    # from it, so it is passed through exactly as written (None when absent).
    accepted_at = record.get("accepted_at") if isinstance(record.get("accepted_at"), str) else None
    return {
        "id": job_id,
        "title": title,
        "milestone": milestone,
        "outcome": outcome,
        "rounds": record.get("rounds") if _is_number(record.get("rounds")) else None,
        "first_time_accept": first_time_accept,
        "accepted_at": accepted_at,
        "review_excerpt": excerpt,
        "metrics": metrics,
    }


def load_jobs(records_dir: str | None) -> list[dict]:
    """Every recorded job, newest first; [] when there are no records.

    A task id may appear several times in ``tasks.jsonl``; the last line
    wins. Unparsable lines are skipped — a broken record costs itself.
    """
    if not records_dir:
        return []
    base = Path(records_dir)
    tasks_path = base / "perf" / "tasks.jsonl"
    try:
        raw_lines = tasks_path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    latest: dict[str, tuple[dict, int]] = {}
    for index, line in enumerate(raw_lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or not isinstance(record.get("task"), str) or not record["task"]:
            continue
        latest[record["task"]] = (record, index)

    def order(item: tuple[dict, int]) -> tuple[int, float, int]:
        record, index = item
        stamp = _accepted_at(record)
        if stamp is None:
            return (1, 0.0, -index)  # undated (early backfill): after the dated ones
        return (0, -stamp, -index)

    return [_build_job(base, record) for record, _ in sorted(latest.values(), key=order)]


def job_summary(jobs: list[dict]) -> dict:
    """Totals and averages over trustworthy values, plus acceptance figures.

    A metric's declared class feeds the totals — ``measured`` sensors and
    ``calculated`` figures (energy cost, effective tps) computed from
    non-partial inputs; the summary entry repeats that provenance. Downgraded
    ``partial`` and ``unavailable`` values never count, and the count per
    metric says how many jobs contributed. ``accepted_tasks_per_worker_hour``
    uses only jobs whose worker_seconds are non-partial (measured).
    """
    metrics: dict[str, dict] = {}
    for name, unit, provenance, _scope, _keys in _METRICS:
        values = [
            job["metrics"][name]["value"]
            for job in jobs
            if job["metrics"][name]["provenance"] in ("measured", "calculated")
            and job["metrics"][name]["value"] is not None
        ]
        total = sum(values) if values else None
        metrics[name] = {
            "unit": unit,
            "provenance": provenance,
            "count": len(values),
            "total": total,
            # 4dp: GBP and tps figures lose their meaning at 2dp (0.0388 -> 0.04)
            "average": round(total / len(values), 4) if values else None,
        }
    accepted = [job for job in jobs if job["outcome"] == "accepted"]
    fta_known = [job["first_time_accept"] for job in jobs if isinstance(job["first_time_accept"], bool)]
    seconds = [
        job["metrics"]["worker_seconds"]["value"]
        for job in jobs
        if job["metrics"]["worker_seconds"]["provenance"] == "measured"
    ]
    hours = sum(seconds) / 3600.0 if seconds else 0.0
    accepted_measured = sum(
        1 for job in jobs
        if job["outcome"] == "accepted" and job["metrics"]["worker_seconds"]["provenance"] == "measured"
    )
    return {
        "jobs": len(jobs),
        "accepted": len(accepted),
        "metrics": metrics,
        "first_time_accept": {
            "count": sum(1 for value in fta_known if value),
            "of": len(fta_known),
            "rate": round(sum(1 for value in fta_known if value) / len(fta_known), 4) if fta_known else None,
        },
        "worker_hours_measured": round(hours, 3),
        "accepted_tasks_per_worker_hour": round(accepted_measured / hours, 3) if hours else None,
    }

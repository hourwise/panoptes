"""API route registrations. Import this module to register all routes."""

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from . import controls, guardian_state, lab, profiles, server, trends
from .collectors.gpu import read_gpus
from .collectors.system import CpuSampler, read_system
from .collectors.storage import DiskSampler, read_storage
from .config import load_config
from .harness import HARNESSES, WARNINGS as HARNESS_WARNINGS
from .inference import ENGINES
from .jobs import job_summary, load_jobs
from .reports import MODES, PRIVATE, SHAREABLE, job_report_md, jobs_report_md, lab_report_md
from .lifecycle.control import ControlClient

_sampler = CpuSampler()
_storage_sampler = DiskSampler("/")
# engine id -> (profile directory, active profile name or None). Read straight
# from the config: the adapters in wsd/inference only carry the connection
# fields. Tests replace this map.
ENGINE_PROFILES: dict[str, tuple[str, str | None]] = {
    spec["id"]: (spec["profiles_dir"], spec.get("active_profile"))
    for spec in load_config()["engines"]
    if "profiles_dir" in spec
}

# engine id -> configured type, for the routes that need a control schema.
# Tests replace this map too.
ENGINE_TYPES: dict[str, str] = {spec["id"]: spec["type"] for spec in load_config()["engines"]}

# The Guardian service's state file (M7 §9), straight from the config: the
# configured ``[guardian] state_file`` or the XDG default when the section is
# absent. The dashboard only ever reads this file and never creates it.
# Tests replace this.
GUARDIAN_STATE_FILE: str | None = load_config().get("guardian_state_file")

# The supervisor's records directory (task packets, reviews, perf/tasks.jsonl)
# straight from the config; None on a machine without one. Tests replace this.
RECORDS_DIR: str | None = load_config().get("records_dir")


@server.route("/api/jobs")
def jobs() -> dict:
    """The stored job history: one entry per accepted task, newest first."""
    warnings: list[str] = []
    if RECORDS_DIR is None:
        warnings.append("no [records] dir in the config; job history is empty")
    elif not Path(RECORDS_DIR).is_dir():
        warnings.append(f"records dir {RECORDS_DIR!r} not found; job history is empty")
    jobs_list = load_jobs(RECORDS_DIR)
    return {"jobs": jobs_list, "summary": job_summary(jobs_list), "warnings": warnings}

# The two non-JSON answers under /api/jobs/: a downloadable Markdown report.
# ``<id>/report.md`` is the Shareable one (the original route); ``<id>/report-<mode>.md``
# names the mode, and a mode that does not exist is a 400, not a missing page.
_MD_SUFFIX = ".md"

#: A report is built from stored records on every request; 200 ids is already a
#: wide screen selection and keeps one request from loading the whole history.
MAX_REPORT_IDS = 200


def _markdown(text: str, filename: str) -> server.Response:
    """A report as an attachment, the same for every report route."""
    return server.Response(
        text,
        "text/markdown; charset=utf-8",
        {"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _report_mode(value: str) -> str:
    """``shareable`` or ``private``; anything else is the caller's mistake (400)."""
    if value not in MODES:
        raise server.RouteError(400, f"unknown report mode {value!r}; expected one of: "
                                     + ", ".join(MODES))
    return value


def _strip_md(name: str) -> str:
    """Drop the ``.md`` a browser or a hand-typed URL may add to a last segment."""
    return name[:-3] if name.endswith(".md") else name


def _stored_jobs() -> list[dict]:
    """The job history for a report; 404 when this machine has no records."""
    if RECORDS_DIR is None or not Path(RECORDS_DIR).is_dir():
        raise server.RouteError(404, "no job records on this machine")
    return load_jobs(RECORDS_DIR)


def _stamp() -> str:
    """The generation time, to the second, in UTC ISO 8601."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@server.route_prefix("/api/jobs/")
def job_detail(tail: str) -> object:
    """``<id>`` as JSON, ``<id>/report.md`` (Shareable) or
    ``<id>/report-<mode>.md`` (either mode) as a Markdown download.

    Unknown id, bad path or missing records is a 404, answered as JSON; an
    unknown report mode is the caller's mistake, answered as a 400.
    """
    if tail.endswith(_MD_SUFFIX):
        report, sep, word = _strip_md(tail.rsplit("/", 1)[-1]).partition("-")
        if report != "report":
            raise server.RouteError(404, f"unknown route /api/jobs/{tail}")
        # the id itself is checked by job_report: a deeper path is no job
        text, filename = job_report(tail[: -len(_MD_SUFFIX)].rsplit("/", 1)[0],
                                    _report_mode(word if sep else SHAREABLE))
        return _markdown(text, filename)
    if "/" in tail or not tail:
        raise server.RouteError(404, f"unknown route /api/jobs/{tail}")
    for job in _stored_jobs():
        if job["id"] == tail:
            return job
    raise server.RouteError(404, f"no job {tail!r}")


def job_report(tail: str, mode: str = SHAREABLE) -> tuple[str, str]:
    """Markdown report for one stored job; (text, download filename).

    Same lookups and 404s as :func:`job_detail`; the generation itself is
    read-only (wsd.reports works from the stored record alone). The default
    mode stays ``shareable``, so the older route answers exactly as before.
    """
    if "/" in tail or not tail:
        raise server.RouteError(404, f"unknown route /api/jobs/{tail}/report.md")
    mode = _report_mode(mode)
    for job in _stored_jobs():
        if job["id"] == tail:
            suffix = "report-private" if mode == PRIVATE else "report"
            return (job_report_md(job, _stamp(), RECORDS_DIR, mode=mode),
                    f"panoptes-{tail}-{suffix}.md")
    raise server.RouteError(404, f"no job {tail!r}")


@server.route_prefix("/api/reports/jobs/")
def jobs_report(tail: str) -> server.Response:
    """``<mode>/<id>,<id>,….md``: one report over the jobs the caller names.

    The ids are validated against the stored history, printed in stored order
    (newest first) and deduplicated: a selection is a set, not a list. An empty
    list, an unknown id or no records is a 404; more than
    :data:`MAX_REPORT_IDS` ids is a 400 — a request that wide is a mistake.
    """
    parts = tail.split("/")
    if len(parts) != 2 or not all(parts):
        raise server.RouteError(404, f"unknown route /api/reports/jobs/{tail}")
    mode = _report_mode(parts[0])
    ids = [item for item in _strip_md(parts[1]).split(",") if item]
    if not ids:
        raise server.RouteError(404, "no job ids given")
    if len(ids) > MAX_REPORT_IDS:
        raise server.RouteError(400, f"too many job ids: {len(ids)}, the cap is {MAX_REPORT_IDS}")
    known = {job["id"]: job for job in _stored_jobs()}
    missing = [name for name in ids if name not in known]
    if missing:
        raise server.RouteError(404, "no job " + ", ".join(missing))
    chosen = [known[name] for name in known if name in set(ids)]
    return _markdown(jobs_report_md(chosen, _stamp(), RECORDS_DIR, mode=mode),
                     f"panoptes-jobs-{mode}.md")


@server.route_prefix("/api/reports/lab/")
def lab_report(tail: str) -> server.Response:
    """``<mode>/<baseline>/<modified>.md``: one Lab comparison as a report."""
    parts = tail.split("/")
    if len(parts) != 3 or not all(parts):
        raise server.RouteError(404, f"unknown route /api/reports/lab/{tail}")
    mode = _report_mode(parts[0])
    baseline, modified = _strip_md(parts[1]), _strip_md(parts[2])
    runs, _warnings = _lab_runs()
    known = [entry["variant"] for entry in lab.variants(runs)]
    unknown = [name for name in (baseline, modified) if name not in known]
    if unknown:
        raise server.RouteError(404, "no bench records for " + ", ".join(unknown)
                                + ("; known variants: " + ", ".join(known) if known else ""))
    return _markdown(lab_report_md(lab.compare(runs, baseline, modified), _stamp(),
                                   RECORDS_DIR, mode=mode),
                     f"panoptes-lab-{mode}.md")


@server.route("/api/system")
def system() -> dict:
    return read_system("/", _sampler)


@server.route("/api/gpus")
def gpus() -> dict:
    return {"gpus": read_gpus("/")}


@server.route("/api/storage")
def storage() -> dict:
    return read_storage("/", _storage_sampler)


@server.route("/api/inference")
def inference() -> dict:
    return {"engines": [adapter.snapshot() for adapter in ENGINES.values()]}


@server.route("/api/harnesses")
def harnesses() -> dict:
    """Live worker activity per configured agent harness (read-only)."""
    return {
        "harnesses": [adapter.snapshot() for adapter in HARNESSES.values()],
        "warnings": list(HARNESS_WARNINGS),
    }


@server.route("/api/engines")
def engines() -> dict:
    """One row per configured engine, from the very adapters /api/inference serves.

    Deliberately the same objects, so a row can never disagree with the tab:
    ``type``, ``model`` and ``capabilities`` come from the shared snapshot, the
    rest from the adapter. ``has_profiles`` says whether a profile directory is
    configured, which decides whether the profile routes answer for the engine.
    """
    rows = []
    for adapter in ENGINES.values():
        snapshot = adapter.snapshot()
        rows.append(
            {
                "id": adapter.engine_id,
                "type": snapshot.get("type"),
                "label": adapter.label,
                "base_url": adapter.base_url,
                "status": snapshot.get("status"),
                "model": (snapshot.get("model") or {}).get("id"),
                "capabilities": snapshot.get("capabilities") or {},
                "has_profiles": adapter.engine_id in ENGINE_PROFILES,
            }
        )
    return {"engines": rows}


@server.route_prefix("/api/engines/")
def engine_profiles(tail: str) -> object:
    """``<id>/controls``, ``<id>/profiles``, ``<id>/profiles/<name>``,
    ``<id>/profiles/<name>/preview``, ``<id>/history`` and ``<id>/switch``.

    The first four read and judge, never apply; ``history`` and ``switch``
    report the profile-switch journal written by
    ``POST <id>/profiles/<name>/apply`` (newest entry first, and the switch in
    flight, step by step).

    ``tail`` is the raw path after the prefix and is never unquoted, so a
    percent-escape just fails the profile name check instead of reaching the
    file system. Unknown engine, engine without ``profiles_dir``, unknown
    sub-path and unservable name are all a 404 with a reason. A preview of a
    file that does not parse answers 200 with ``valid: false``, not a 500.
    """
    engine_id, _, rest = tail.partition("/")
    preview_of = None
    if rest == "controls":
        pass
    elif rest in ("profiles", "history", "switch"):
        pass
    elif rest.startswith("profiles/"):
        name = rest[len("profiles/") :]
        if name.endswith("/preview"):
            preview_of = name[: -len("/preview")]
        elif "/" not in name:
            preview_of = None
        else:
            raise server.RouteError(404, f"unknown route /api/engines/{tail}")
    else:
        raise server.RouteError(404, f"unknown route /api/engines/{tail}")
    if engine_id not in ENGINES:
        raise server.RouteError(404, f"no engine {engine_id!r}")
    if engine_id not in ENGINE_PROFILES:
        raise server.RouteError(404, f"engine {engine_id!r} has no profiles_dir")
    dir_path, active = ENGINE_PROFILES[engine_id]
    try:
        if rest == "controls":
            schema, _, _ = _control_schema(engine_id, dir_path, active)
            return {"engine": engine_id, "controls": [c.to_dict() for c in schema.controls]}
        if rest == "profiles":
            return {
                "engine": engine_id,
                "profiles_dir": dir_path,
                "active_profile": active,
                "profiles": profiles.list_profiles(dir_path, active),
            }
        if rest == "history":
            return _history_payload(engine_id)
        if rest == "switch":
            return _switch_payload(engine_id)
        name = rest[len("profiles/") :]
        if preview_of is not None:
            return _profile_preview(engine_id, dir_path, active, preview_of)
        return {"engine": engine_id, "name": name, "text": profiles.read_profile(dir_path, name)}
    except profiles.ProfileError as exc:
        raise server.RouteError(404, str(exc)) from exc


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _control_schema(engine_id: str, dir_path: str, active: str | None):
    """The engine's control schema built against its active profile.

    ``(schema, active text or None, error or None)``: ``error`` names what
    is wrong with the active profile (missing, unreadable, not the expected
    YAML subset). The schema is still answerable without a usable active
    profile — only its model paths then carry the ``<name>`` placeholder.
    """
    if ENGINE_TYPES.get(engine_id) != "radiance":
        raise server.RouteError(
            404, f"engine {engine_id!r} has no control schema (type {ENGINE_TYPES.get(engine_id)!r})"
        )
    if not active:
        return controls.build_schema(None), None, None
    try:
        text = profiles.read_profile(dir_path, active)
    except profiles.ProfileError as exc:
        return controls.build_schema(None), None, f"active profile {active!r} cannot be read: {exc}"
    try:
        return controls.build_schema(controls.parse_profile(text)), text, None
    except controls.ProfileParseError as exc:
        return controls.build_schema(None), None, f"active profile {active!r} does not parse: {exc}"


def _profile_preview(engine_id: str, dir_path: str, active: str | None, name: str) -> dict:
    """Judge one profile file against the active one. Never writes, applies
    or restarts: this is the confirm-screen data for T-040's apply."""
    schema, active_text, active_error = _control_schema(engine_id, dir_path, active)
    result = {
        "engine": engine_id,
        "name": name,
        "active_profile": active,
        "valid": False,
        "errors": [],
        "warnings": [],
        "changes": [],
        "active_sha256": None,
        "candidate_sha256": None,
    }
    if active_error:
        result["errors"] = [active_error]
        return result
    candidate_text = profiles.read_profile(dir_path, name)  # unservable name -> 404
    result["candidate_sha256"] = _sha256(candidate_text)
    result["active_sha256"] = _sha256(active_text)
    try:
        candidate = controls.parse_profile(candidate_text)
    except controls.ProfileParseError as exc:
        result["errors"] = [f"profile {name!r}: {exc}"]
        return result
    active_tree = controls.parse_profile(active_text)
    result.update(controls.validate(schema, active_tree, candidate))
    result["changes"] = controls.preview(schema, active_tree, candidate)
    return result


_control = ControlClient()


@server.route("/api/control/status")
def control_status() -> dict:
    return {"status": _control.status(), "worker_active": _control.worker_active()}


@server.route("/api/control/logs")
def control_logs() -> dict:
    out = _control.logs()
    if isinstance(out, dict):  # {"ok": false, "error": ...} from the runner layer
        return out
    return {"lines": out.split("\n") if out else []}


def _lifecycle(op: str, body: object) -> object:
    """Explicit confirmation in the body is mandatory before the helper runs."""
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return {"ok": False, "error": 'confirmation required: body must be {"confirm": true}'}
    return _control.run(op)


@server.post_route("/api/control/start")
def control_start(body: dict) -> object:
    return _lifecycle("start", body)


@server.post_route("/api/control/stop")
def control_stop(body: dict) -> object:
    return _lifecycle("stop", body)


@server.post_route("/api/control/restart")
def control_restart(body: dict) -> object:
    return _lifecycle("restart", body)


# ---------------------------------------------------------------------
# Panoptes Lab: benchmark variant comparison, read-only
# and how the accepted tasks went over time
# ---------------------------------------------------------------------


def _lab_runs() -> tuple[list[dict], list[str]]:
    """The bench records, plus the reason there are none when that is the case."""
    warnings: list[str] = []
    if RECORDS_DIR is None:
        warnings.append("no [records] dir in the config; no lab runs to compare")
        return [], warnings
    if not Path(RECORDS_DIR).is_dir():
        warnings.append(f"records dir {RECORDS_DIR!r} not found; no lab runs to compare")
        return [], warnings
    return lab.load_runs(RECORDS_DIR, warnings), warnings


@server.route("/api/lab/variants")
def lab_variants() -> dict:
    """The bench variants worth picking two of, with how many runs each has."""
    runs, warnings = _lab_runs()
    return {"variants": lab.variants(runs), "runs": len(runs), "warnings": warnings}


@server.route_prefix("/api/lab/compare/")
def lab_compare(tail: str) -> object:
    """``/api/lab/compare/<baseline>/<modified>``: one variant against another.

    The two variants are path segments, not a query string: ``wsd/server.py``
    routes on ``urlparse(self.path).path``, so a ``?...`` never reaches any
    handler, and that file is off-limits here.
    """
    baseline, _, modified = tail.partition("/")
    if not baseline or not modified:
        raise server.RouteError(400, "usage: /api/lab/compare/<baseline>/<modified>")
    runs, warnings = _lab_runs()
    known = [entry["variant"] for entry in lab.variants(runs)]
    missing = [name for name in (baseline, modified) if name not in known]
    if missing:
        raise server.RouteError(400, "no bench records for " + ", ".join(missing)
                                + ("; known variants: " + ", ".join(known) if known else ""))
    return {**lab.compare(runs, baseline, modified), "warnings": warnings}


@server.route("/api/lab/trends")
def lab_trends() -> dict:
    """How every accepted task went, with the applied profile switches marked."""
    return trends.load_trends(RECORDS_DIR, _journal_dirs())


def _journal_dirs() -> dict[str, str]:
    """``engine id -> switch journal`` for every engine that has profiles.

    The very directory :func:`_history_payload` reads, through :func:`_journal_dir`.
    Deliberately not the directory the switch *hooks* carry: under
    ``PANOPTES_CONTROL_DRYRUN=1`` the hooks are pointed at a sandbox journal so a
    dry run can never write the real one, while the trends are the real history —
    and reading it is read-only whatever the mode.
    """
    journals: dict[str, str] = {}
    for engine_id in ENGINE_PROFILES:
        journal_dir = _journal_dir(engine_id)
        if journal_dir:
            journals[engine_id] = journal_dir
    return journals


# ---------------------------------------------------------------------
# Profile switching: apply a profile, restore one from the journal
# ---------------------------------------------------------------------

# engine id -> journal directory, for the engines that name one explicitly in
# the config; the rest get :data:`JOURNAL_DIR_NAME` under the records dir.
# Tests replace this map.
ENGINE_JOURNAL_DIRS: dict[str, str] = {
    spec["id"]: spec["journal_dir"]
    for spec in load_config()["engines"]
    if "journal_dir" in spec
}

# Fallback journal directory name under the records dir, and the names of the
# environment variables overriding how long a switch waits for the restarted
# engine to serve the new profile (a cold model load is slow) and, on its own
# budget, how long a rollback waits for the profile it writes back.
JOURNAL_DIR_NAME = "config-journal"
READY_TIMEOUT_ENV = "PANOPTES_READY_TIMEOUT_S"
ROLLBACK_TIMEOUT_ENV = "PANOPTES_ROLLBACK_TIMEOUT_S"

_CONFIRM_REQUIRED = 'confirmation required: body must be {"confirm": true}'


class PostRoutes(dict):
    """``server.POST_ROUTES`` with prefix support added.

    ``do_POST`` looks a handler up by exact path, which cannot express
    ``/api/engines/<id>/profiles/<name>/apply``; that file is off-limits here,
    so the map itself grows the fallback the GET side already has: exact
    registrations first, then the longest prefix registered with
    :func:`post_route_prefix`, called with the remainder of the path plus the
    parsed body. Replacing the module global is what makes ``do_POST`` ask this
    object, and :func:`wsd.server.post_route` keeps working because it writes
    through that same global.
    """

    def __init__(self, base: dict | None = None) -> None:
        super().__init__(base or {})
        self.prefixes: dict[str, object] = {}

    def get(self, key: str, default=None):
        handler = super().get(key)
        if handler is not None:
            return handler
        for prefix in sorted(self.prefixes, key=len, reverse=True):
            if key.startswith(prefix) and len(key) > len(prefix):
                prefix_handler = self.prefixes[prefix]
                tail = key[len(prefix) :]
                return lambda body: prefix_handler(tail, body)
        return default


POST_PREFIX_ROUTES = PostRoutes(server.POST_ROUTES)
server.POST_ROUTES = POST_PREFIX_ROUTES


def post_route_prefix(prefix: str):
    """Register ``func(remainder, body)`` for every POST path under ``prefix``."""

    def decorator(func):
        POST_PREFIX_ROUTES.prefixes[prefix] = func
        return func

    return decorator


def _journal_dir(engine_id: str) -> str | None:
    """Where this engine's switches are journalled: the configured
    ``journal_dir``, else one under the records dir — None on a machine that
    has neither, where switching is simply unavailable."""
    configured = ENGINE_JOURNAL_DIRS.get(engine_id)
    if configured:
        return configured
    return None if RECORDS_DIR is None else str(Path(RECORDS_DIR) / JOURNAL_DIR_NAME)


def _timeout_from_env(name: str, default: float) -> float:
    """Seconds asked for by ``name``: a positive float, otherwise ``default``."""
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _ready_timeout() -> float:
    """Seconds to wait for a restarted engine to serve the new profile."""
    return _timeout_from_env(READY_TIMEOUT_ENV, controls.apply.READY_TIMEOUT_S)


def _rollback_timeout() -> float:
    """Seconds to wait for the profile a rollback puts back: a separate knob,
    so shortening the forward budget never shortens recovery."""
    return _timeout_from_env(ROLLBACK_TIMEOUT_ENV, controls.apply.ROLLBACK_TIMEOUT_S)


def _switch_hooks(engine_id: str):
    """Hooks for this engine's switch pipeline, or None when it cannot switch
    profiles at all (unknown engine, no ``profiles_dir``, not Radiance, and no
    journal directory to write to)."""
    if engine_id not in ENGINE_PROFILES or ENGINE_TYPES.get(engine_id) != "radiance":
        return None
    journal_dir = _journal_dir(engine_id)
    if journal_dir is None:
        return None
    dir_path, active = ENGINE_PROFILES[engine_id]
    adapter = ENGINES.get(engine_id)
    return controls.radiance_hooks(
        engine_id,
        getattr(adapter, "base_url", ""),
        dir_path,
        active,
        journal_dir,
        control=_control,
    )


def _refusal(message: str) -> dict:
    """A request this route will not attempt. ``do_POST`` answers 200 or 500
    only — a handler raising is a 500 — so a refusal is a payload, exactly like
    :func:`_lifecycle` makes its unconfirmed lifecycle calls one."""
    return {"ok": False, "error": message}


def _switch_reply(entry: dict) -> dict:
    """The journal entry a switch wrote, with ``ok`` saying whether the engine
    ended up running what was asked: refused and rolled back are not ok."""
    return {"ok": entry["outcome"] == "applied", **entry}


@post_route_prefix("/api/engines/")
def engine_switch(tail: str, body: dict) -> object:
    """``<id>/profiles/<name>/apply`` and ``<id>/history/<entry>/restore``.

    Both run the switch pipeline synchronously and answer with the journal
    entry they wrote (``outcome`` plus every step). Anything that cannot even
    be attempted is a refusal that touches nothing: no explicit confirmation
    (the same rule :func:`_lifecycle` applies to the container helpers), an
    engine that cannot switch profiles, a name that is not a profile file, an
    unknown history entry, or an unknown path.
    """
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return _refusal(_CONFIRM_REQUIRED)
    engine_id, _, rest = tail.partition("/")
    hooks = _switch_hooks(engine_id)
    if hooks is None:
        return _refusal(f"engine {engine_id!r} cannot switch profiles")
    if rest.startswith("profiles/") and rest.endswith("/apply"):
        return _apply_profile(hooks, engine_id, rest[len("profiles/") : -len("/apply")])
    if rest.startswith("history/") and rest.endswith("/restore"):
        return _restore_entry(hooks, engine_id, rest[len("history/") : -len("/restore")])
    return _refusal(f"unknown route /api/engines/{tail}")


def _apply_profile(hooks, engine_id: str, name: str) -> dict:
    """Switch the engine to the profile ``name`` in its own profile directory."""
    dir_path, _ = ENGINE_PROFILES[engine_id]
    try:
        profiles.read_profile(dir_path, name)  # the name/size checks, on the spot
    except profiles.ProfileError as exc:
        return _refusal(str(exc))
    return _switch_reply(
        controls.apply_profile(
            hooks, engine_id, Path(dir_path) / name, name,
            action="apply", ready_timeout=_ready_timeout(), rollback_timeout=_rollback_timeout(),
        )
    )


def _restore_entry(hooks, engine_id: str, entry_id: str) -> dict:
    """Switch back to the profile a journalled entry backed up before changing."""
    previous = next(
        (entry for entry in controls.load_history(hooks.journal_dir, engine_id)
         if entry.get("id") == entry_id),
        None,
    )
    if previous is None:
        return _refusal(f"no history entry {entry_id!r} for engine {engine_id!r}")
    backup = Path(str(previous.get("backup") or "")).name
    candidate = hooks.journal_dir / controls.apply.BACKUP_DIR_NAME / backup
    if not backup or not candidate.is_file():
        return _refusal(f"history entry {entry_id!r} has no backup file to restore")
    name = str((previous.get("from") or {}).get("name") or backup)
    return _switch_reply(
        controls.apply_profile(
            hooks, engine_id, candidate, name,
            action="restore", ready_timeout=_ready_timeout(), rollback_timeout=_rollback_timeout(), restore_of=entry_id,
        )
    )


def _history_payload(engine_id: str) -> dict:
    """The journal, newest first; empty with a reason when there is nowhere to
    journal. Reading never creates the journal directory."""
    hooks = _switch_hooks(engine_id)
    if hooks is None:
        return {"engine": engine_id, "entries": [],
                "warnings": [f"engine {engine_id!r} has no switch journal"]}
    entries = controls.load_history(hooks.journal_dir, engine_id)
    return {"engine": engine_id, "entries": list(reversed(entries))}


def _switch_payload(engine_id: str) -> dict:
    """The switch in flight (step by step) or ``running: false``."""
    hooks = _switch_hooks(engine_id)
    if hooks is None:
        return {"engine": engine_id, "running": False, "step": None, "lock": None,
                "entry": None, "warnings": [f"engine {engine_id!r} has no switch journal"]}
    return controls.switch_state(hooks.journal_dir, engine_id, hooks.switch_lock)


# ---------------------------------------------------------------------
# Panoptes Guardian: the header chip's read-only hook (GUARDIAN.md §9)
# ---------------------------------------------------------------------


@server.route("/api/guardian/state")
def guardian_status() -> dict:
    """What the Guardian service's state file says, plus what the chip may show.

    Read-only, and it creates nothing: the file and its directory belong to the
    (later) Guardian service, so while it does not exist the answer is
    ``installed: false`` with an ``off`` chip. ``display`` is the honest
    headline (``level``, ``label``, ``detail``), ``state`` the parsed file —
    None when there was nothing to parse — and ``warnings`` every field that
    could not be used.
    """
    return guardian_state.read_state(GUARDIAN_STATE_FILE)

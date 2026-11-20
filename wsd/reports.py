"""Markdown reports, built from the stored records only (T-027, T-047).

Every report here comes from records already on disk — job entries from
:mod:`wsd.jobs`, bench entries from :mod:`wsd.lab` — never from a live engine,
harness or worker. GitHub-flavoured Markdown; data honesty per PLAN.md: values
keep their stored provenance class and scope, a missing figure is an em dash
(``—``) and never a 0, and a difference inside the run-to-run spread is never
called a win.

Two modes, printed in the header of every report and in its file name:

``shareable`` (the default everywhere)
    home paths become ``[path redacted]`` or ``[…]/<file name>``, machine names
    become ``[host redacted]`` and secret assignments are blanked.
``private``
    paths and machine names are kept — full technical detail — while secret
    assignments are STILL blanked, so private never means "has credentials".

URL paths (``/api/health``), relative paths and plain words survive verbatim.
Every replacement is counted and the count is published in Data notes, so
redaction is never silent. Stored values are printed exactly as kept — never
rounded, never coerced; a non-numeric raw value (``"~20000"``) shows as stored.
"""

import hashlib
import math
import re
import socket
from pathlib import Path
from statistics import median

from . import PRODUCT, __version__

#: What an absent value is shown as.
DASH = "—"

#: What a side that never measured is shown as (the Lab screen's wording).
NO_DATA = "no data"

#: The two report modes; ``shareable`` is the default everywhere.
SHAREABLE = "shareable"
PRIVATE = "private"
MODES = (SHAREABLE, PRIVATE)
MODE_LABEL = {SHAREABLE: "Shareable", PRIVATE: "Private"}

#: The layout version of every report this module writes.
SCHEMA_VERSION = "2"

#: The provenance classes and their meaning, rendered as a legend in every
#: report so a reader never has to guess what "partial" promised.
PROVENANCE_LEGEND = (
    ("measured", "sampled or counted during the job by a sensor or counter"),
    ("calculated", "computed from other stored values"),
    ("estimated", "a judgement figure, not a reading"),
    ("partial", "an incomplete measurement, downgraded from its original class"),
    ("unavailable", "nothing was stored for this figure"),
)

#: What each scope covers, in the same legend position in every report.
SCOPE_LEGEND = (
    ("job", "one accepted task, end to end, across all its rounds"),
    ("engine-lifetime", "an engine counter read at review time; never this job alone"),
    ("run", "one benchmark run of one task by one variant"),
)

PARTIAL_NOTE = (
    "Partial values are incomplete measurements (for example a sampler that started "
    "late or kept running); they are not comparable with measured values."
)

#: A difference the run-to-run spread covers: the Lab screen's own words, so
#: the report and the screen can never disagree about what was shown.
NOISE_NOTE = "not distinguishable from run-to-run variation"

# A home-directory path: ``/home/<user>/``, ``/Users/<user>/`` or ``/root/``
# followed by path characters up to whitespace or markup; trailing sentence
# punctuation stays outside the match. The lookbehind keeps a path that is
# really a URL path (http://host/home/x) out of scope.
_HOME_PATH = re.compile(
    r"(?<![\w.~+\-])/(?:home|Users|root)/(?:[^\s\"'<>|)\]]*[^\s\"'<>|)\].,;:])?"
)
_SECRET = re.compile(
    r"(?<![\w-])((?:[A-Za-z0-9]+[_-])*(?:token|secret|password|passwd|passphrase|api[_ -]?key)"
    r"(?:[_-][A-Za-z0-9]+)?)([\"']?\s*[:=]\s*)([\"']?)([^\s\"',;]+)",
    re.IGNORECASE,
)
# A dotted name on this network: ``printer.local``, ``box.lan``. The last
# label decides — ``.local``, ``.lan``, ``.home`` and friends name a machine
# here, so a shareable report never carries one, while ``example.com`` and
# ``127.0.0.1`` say nothing about this machine and stay untouched.
_DOTTED_NAME = re.compile(r"(?<![\w.-])(?:[\w-]+\.){1,}[A-Za-z0-9_-]+(?![\w-])")
_LAN_SUFFIXES = ("local", "lan", "home", "internal", "intranet", "localdomain")


def _redact_path(match: "re.Match[str]") -> str:
    """One home path -> ``[path redacted]``, keeping the file name if it has one."""
    last = match.group(0).rsplit("/", 1)[-1]
    return f"[…]/{last}" if "." in last.strip(".") else "[path redacted]"


def _redact_lan_name(match: "re.Match[str]") -> str:
    """A LAN host name -> ``[host redacted]``; any other dotted name is left be."""
    name = match.group(0)
    return "[host redacted]" if name.rsplit(".", 1)[-1].lower() in _LAN_SUFFIXES else name

def _host_names() -> list[str]:
    """This machine's names, longest first: the host name and its short form.

    ``localhost`` and anything shorter than three characters are excluded —
    redacting them would blank ordinary prose and loopback URLs, which say
    nothing about which machine produced the report.
    """
    try:
        name = socket.gethostname() or ""
    except OSError:
        return []
    names = [candidate.strip() for candidate in (name, name.split(".")[0])]
    keep = [n for n in names if len(n) >= 3 and n.lower() != "localhost"]
    return sorted(dict.fromkeys(keep), key=len, reverse=True)


def redact(text: str, mode: str = SHAREABLE) -> tuple[str, int]:
    """Blank secrets everywhere; in ``shareable`` also paths and machine names.

    Returns the rewritten text and the number of replacements, so a report can
    publish what it hid. Private keeps every path and machine name: the secret
    pattern is the one thing it never lifts.
    """
    count = 0
    if mode != PRIVATE:
        text, hits = _HOME_PATH.subn(_redact_path, text)
        count += hits
        for host in _host_names():
            text, hits = re.subn(
                rf"(?<![\w.-]){re.escape(host)}(?![\w.-])", "[host redacted]", text, flags=re.IGNORECASE
            )
            count += hits
        count += sum(1 for match in _DOTTED_NAME.finditer(text)
                     if match.group(0).rsplit(".", 1)[-1].lower() in _LAN_SUFFIXES)
        text = _DOTTED_NAME.sub(_redact_lan_name, text)
    text, hits = _SECRET.subn(r"\1\2\3«redacted»", text)
    return text, count + hits


def _value(metric: dict) -> str:
    """A metric's value exactly as stored; raw kept for non-numerics."""
    if metric.get("value") is not None:
        return str(metric["value"])
    if metric.get("raw") is not None:
        return str(metric["raw"])
    return DASH


def _cell(value: object) -> str:
    """Free text for one table cell: no pipes, no line breaks."""
    if value is None:
        return DASH
    return str(value).replace("|", "\\|").replace("\n", " ")


def _phrase(metric: dict) -> str:
    """``316.11 Wh (partial)`` / ``unavailable`` — provenance in words."""
    if metric["provenance"] == "unavailable" and metric.get("value") is None:
        return "unavailable"
    unit = metric.get("unit") or ""
    return f"{_value(metric)} {unit} ({metric['provenance']})".strip()


def _metric_table(metrics: dict) -> list[str]:
    lines = [
        "| Metric | Value | Unit | Provenance | Scope |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, metric in metrics.items():
        unit = metric.get("unit") or DASH
        lines.append(
            f"| {_cell(name)} | {_cell(_value(metric))} | {_cell(unit)} "
            f"| {_cell(metric['provenance'])} | {_cell(metric['scope'])} |"
        )
    return lines


def _yes_no(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return DASH


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _header(title: str, mode: str, generated_at: str, schema_version: str) -> list[str]:
    """The metadata block every report opens with, under its title."""
    return [
        f"# {title}",
        "",
        f"- report_schema_version: {schema_version}",
        f"- mode: {MODE_LABEL.get(mode, mode)} (`{mode}`)",
        f"- generated: {generated_at}",
        f"- {PRODUCT} version: {__version__}",
        "",
    ]


def _assemble(title: str, body: list[str], mode: str, generated_at: str, schema_version: str) -> str:
    """Header + body, redacted as a whole, with the count published last.

    The count comes from the redaction itself, so it is written after the body
    has been rewritten. The header only ever carries the caller's own values
    (title, mode, stamp, version), never a stored figure, so redacting it too
    cannot hide a fact or invent one.
    """
    text, redactions = redact("\n".join(_header(title, mode, generated_at, schema_version) + body), mode)
    return "\n".join([text, "", f"Redactions: {redactions}", ""])


def _data_notes(sources: list[str]) -> list[str]:
    """The legend section every report ends with, before the redaction count."""
    out = ["## Data notes", ""]
    out += [f"- `{name}` — {meaning}" for name, meaning in PROVENANCE_LEGEND]
    out += ["", "Scopes:"]
    out += [f"- `{name}` — {meaning}" for name, meaning in SCOPE_LEGEND]
    out += ["", PARTIAL_NOTE, ""]
    out += sources
    return out


def _sha256_file(path: Path) -> str:
    """The digest of a source file, or an em dash when it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return DASH


def _source_lines(records_dir: str | None, jsonl: str, records: list[str], extra: list[str]) -> list[str]:
    """Which records the report was built from: path, file digest, per-record refs.

    Paths are relative to the records directory, so a report names its sources
    without ever publishing where that directory lives.
    """
    digest = _sha256_file(Path(records_dir) / jsonl) if records_dir else DASH
    lines = [f"- `{jsonl}` (sha256 `{digest}`) — records: "
             + (", ".join(f"`{r}`" for r in records) if records else DASH)]
    if extra:
        lines.append("- " + ", ".join(f"`{ref}`" for ref in extra))
    lines.append("- All paths are relative to the records directory.")
    return lines


def _fmt_num(value: object) -> str:
    """At most two decimals, trailing zeros dropped, never ``-0`` and never ``0.00``.

    The same rule as ``web/lab-format.js``, including rounding halves up: the
    Lab report and the Lab screen print the same digits for the same record.
    """
    if not _is_number(value):
        return NO_DATA
    rounded = math.floor(float(value) * 100 + 0.5) / 100
    text = f"{rounded:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _fmt_side(stats: dict, unit: str) -> str:
    """One side of a comparison: mean, n and the min–max range; "no data" if none."""
    if not stats or not stats.get("n") or stats.get("mean") is None:
        return NO_DATA
    if unit == "ratio":
        return (f"{_fmt_num(stats['mean'] * 100)} % (n={stats['n']}, "
                f"{_fmt_num(stats['min'] * 100)}–{_fmt_num(stats['max'] * 100)} %)")
    unit_text = f" {unit}" if unit and unit != "count" else ""
    return (f"{_fmt_num(stats['mean'])}{unit_text} (n={stats['n']}, "
            f"{_fmt_num(stats['min'])}–{_fmt_num(stats['max'])})")


def _fmt_diff(difference: dict, unit: str) -> str:
    """modified − baseline, sign always explicit, percent of the baseline in brackets."""
    if not difference or difference.get("absolute") is None:
        return DASH
    absolute = difference["absolute"]
    shown = absolute * 100 if unit == "ratio" else absolute
    text = f"{'+' if absolute > 0 else ''}{_fmt_num(shown)}{' pp' if unit == 'ratio' else ''}"
    percent = difference.get("percent")
    if percent is None:
        return text
    return f"{text} ({'+' if percent > 0 else ''}{_fmt_num(percent)} %)"


#: The record files a report can be built from, relative to the records dir.
TASKS_JSONL = "perf/tasks.jsonl"
BENCH_JSONL = "perf/bench.jsonl"


def job_report_md(job: dict, generated_at: str, records_dir: str | None = None,
                  schema_version: str = SCHEMA_VERSION, mode: str = SHAREABLE) -> str:
    """The full GitHub-flavoured Markdown report for one stored job.

    ``job`` is a :func:`wsd.jobs.load_jobs` entry; ``generated_at`` is the
    caller's timestamp string; ``records_dir`` only feeds the source digest.
    ``mode`` decides whether paths and machine names are redacted.
    """
    job_id = job["id"]
    metrics = job["metrics"]
    job_metrics = {n: m for n, m in metrics.items() if m["scope"] == "job"}
    engine_metrics = {n: m for n, m in metrics.items() if m["scope"] != "job"}
    seconds = metrics.get("worker_seconds", {})
    energy = metrics.get("total_energy_wh", {})
    rounds = job.get("rounds")

    body: list[str] = [
        f"Job {job_id} finished **{job.get('outcome') or DASH}** after "
        f"{rounds if rounds is not None else DASH} round(s), first-time acceptance: "
        f"{_yes_no(job.get('first_time_accept'))}. Worker time {_phrase(seconds)}; "
        f"total energy {_phrase(energy)}.",
        "",
        "## Job",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| id | {_cell(job_id)} |",
        f"| title | {_cell(job.get('title'))} |",
        f"| milestone | {_cell(job.get('milestone'))} |",
        f"| outcome | {_cell(job.get('outcome'))} |",
        f"| rounds | {_cell(job.get('rounds'))} |",
        f"| first-time acceptance | {_yes_no(job.get('first_time_accept'))} |",
        "",
        "## Measurements (this job)",
        "",
    ]
    body += _metric_table(job_metrics)
    body += ["", "## Engine context at review time (not per-job performance)", ""]
    body += _metric_table(engine_metrics)
    body += ["", "## Review", ""]
    body += _review_block(job.get("review_excerpt"))
    body += ["", *_data_notes(_source_lines(records_dir, TASKS_JSONL, [str(job_id)],
                                            [f"reviews/{job_id}.md", f"tasks/{job_id}.md"]))]
    return _assemble(f"{PRODUCT} job report — {job_id}: {_cell(job.get('title'))}",
                     body, mode, generated_at, schema_version)


def _review_block(excerpt: str | None) -> list[str]:
    """One stored review as a block quote, or the honest absence of one."""
    if not excerpt:
        return ["No review is stored for this job (unavailable)."]
    return [f"> {line}" for line in excerpt.splitlines()] or [">"]


def _exact(value: object) -> str:
    """A stored number, printed exactly as kept: never rounded, never coerced."""
    return str(value)


def _derived(value: float) -> str:
    """A total or a median: enough digits to be exact, without float noise."""
    return f"{value:.10g}"


def _rounded(value: float, digits: int) -> str:
    """A derived rate: the digits that mean something (a rate to 3 dp, a Wh figure to 2)."""
    return _derived(round(value, digits))


#: The numeric columns of the jobs report, in table order: header, metric,
#: stored value -> column value, column value -> text (stored / derived), and
#: whether the column adds up at all (a rate does not).
JOBS_COLUMNS: tuple[tuple[str, str, object, object, object, bool], ...] = (
    # header, metric, stored value -> column value, column value -> text (one job),
    # column value -> text (a total or a median), whether the column adds up.
    ("worker min", "worker_seconds", lambda v: v / 60.0, lambda v: f"{v:.1f}", lambda v: f"{v:.1f}", True),
    ("Wh", "total_energy_wh", lambda v: v, _exact, _derived, True),
    ("effective tok/s", "effective_worker_tps", lambda v: v, _exact, _derived, False),
    ("peak context", "peak_context", lambda v: v, _exact, _derived, False),
    ("compactions", "compactions", lambda v: v, _exact, _derived, True),
)


def _metric_of(job: dict, name: str) -> dict:
    return (job.get("metrics") or {}).get(name) or {"value": None, "provenance": "unavailable"}


def _trusted(job: dict, name: str) -> int | float | None:
    """A column's value when it can be totalled: measured, or calculated from
    measured inputs. ``partial`` and ``unavailable`` never count."""
    metric = _metric_of(job, name)
    value = metric.get("value")
    if metric.get("provenance") in ("measured", "calculated") and _is_number(value):
        return value
    return None


def _measured(job: dict, name: str) -> int | float | None:
    """A column's value only when it is a plain measurement (never partial)."""
    metric = _metric_of(job, name)
    value = metric.get("value")
    if metric.get("provenance") == "measured" and _is_number(value):
        return value
    return None


def _column_cell(job: dict, column: tuple) -> str:
    """One job's cell: the value as stored, plus its provenance word unless measured."""
    _label, name, conv, show, _agg_show, _additive = column
    metric = _metric_of(job, name)
    value = metric.get("value")
    if value is None:
        raw = metric.get("raw")
        if raw is None:
            return DASH
        text, provenance = str(raw), metric.get("provenance") or "partial"
    else:
        text, provenance = show(conv(value)), metric.get("provenance")
    return text if provenance == "measured" else f"{text} ({provenance})"


def _jobs_table(jobs: list[dict]) -> list[str]:
    lines = [
        "| id | title | outcome | rounds | first time | worker min | Wh | effective tok/s "
        "| peak context | compactions |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for job in jobs:
        cells = " ".join(f"| {_column_cell(job, column)}" for column in JOBS_COLUMNS)
        lines.append(
            f"| {_cell(job.get('id'))} | {_cell(job.get('title'))} "
            f"| {_cell(job.get('outcome') or 'unknown')} | {_cell(job.get('rounds'))} "
            f"| {_yes_no(job.get('first_time_accept'))} {cells} |"
        )
    return lines


def _aggregate_table(jobs: list[dict]) -> list[str]:
    lines = ["| Column | Counted | Total | Median |", "| --- | ---: | ---: | ---: |"]
    for column in JOBS_COLUMNS:
        label, name, conv, _show, agg_show, additive = column
        values = [conv(value) for value in (_trusted(job, name) for job in jobs) if value is not None]
        counted = f"{len(values)} of {len(jobs)}"
        if not values:
            lines.append(f"| {label} | {counted} | {DASH} | {DASH} |")
            continue
        total = agg_show(sum(values)) if additive else "not additive"
        lines.append(f"| {label} | {counted} | {total} | {agg_show(median(values))} |")
    return lines


def _jobs_summary(jobs: list[dict]) -> list[str]:
    if not jobs:
        return ["No jobs are in scope, so there is nothing to total or average."]
    accepted = [job for job in jobs if job.get("outcome") == "accepted"]
    first_time = [job for job in accepted if job.get("first_time_accept") is True]
    lines = [f"{len(jobs)} job(s) in scope: {len(accepted)} accepted, "
             f"{len(first_time)} accepted first time."]
    measured = [job for job in jobs
                if _measured(job, "worker_seconds") is not None
                and _measured(job, "total_energy_wh") is not None]
    if not measured:
        lines.append("Accepted tasks per worker-hour and Wh per accepted task are unavailable: "
                     f"no job in this set of {len(jobs)} has both measured worker seconds and "
                     "measured energy.")
        return lines
    counted_accepted = [job for job in measured if job.get("outcome") == "accepted"]
    hours = sum(_measured(job, "worker_seconds") for job in measured) / 3600.0
    per_hour = _rounded(len(counted_accepted) / hours, 3) if hours else DASH
    if counted_accepted:
        wh = sum(_measured(job, "total_energy_wh") for job in counted_accepted)
        wh_per_task = _rounded(wh / len(counted_accepted), 2)
    else:
        wh_per_task = DASH
    lines.append(
        f"Accepted tasks per worker-hour: {per_hour}; Wh per accepted task: {wh_per_task}. "
        f"Both are computed over the {len(measured)} of {len(jobs)} job(s) whose worker seconds "
        f"and energy are both measured ({len(counted_accepted)} of them accepted); partial and "
        "unavailable figures never count."
    )
    return lines


def _review_sections(jobs: list[dict]) -> list[str]:
    lines = ["## Review excerpts", ""]
    for job in jobs:
        lines.append(f"### {_cell(job.get('id'))} — {_cell(job.get('title'))}")
        lines.append("")
        lines += _review_block(job.get("review_excerpt"))
        lines.append("")
    return lines


def _id_order(value: str) -> list:
    """Sort key that lines up T-002 before T-010 before T-100."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def _scope_phrase(ids: list[str]) -> str:
    """The id list in a title: every id in id order when there are few enough to
    read, a first … last range plus a count when there are too many."""
    if not ids:
        return DASH
    ordered = sorted(ids, key=_id_order)
    if len(ordered) <= 8:
        return ", ".join(ordered)
    return f"{ordered[0]} … {ordered[-1]} ({len(ordered)} jobs)"


def jobs_report_md(jobs: list[dict], generated_at: str, records_dir: str | None = None,
                   schema_version: str = SCHEMA_VERSION, mode: str = SHAREABLE) -> str:
    """A report over a set of stored jobs: summary, table, totals, reviews.

    ``jobs`` are :func:`wsd.jobs.load_jobs` entries, in the order they should
    be printed; every figure comes from those records and nothing else.
    """
    ids = [str(job.get("id")) for job in jobs]
    title = f"{PRODUCT} jobs report — {len(jobs)} job(s): {_scope_phrase(ids)}"
    refs = [ref for job in jobs for ref in (f"reviews/{job.get('id')}.md", f"tasks/{job.get('id')}.md")]
    body = [
        "## Summary",
        "",
        *_jobs_summary(jobs),
        "",
        "## Jobs",
        "",
        "An em dash is an unavailable value — never a 0. `(partial)` and `(calculated)` name "
        "the provenance of a figure; a bare number is measured.",
        "",
        *_jobs_table(jobs),
        "",
        "## Totals and medians",
        "",
        "Totals and medians count only trustworthy values — measured ones, and calculated ones "
        "computed from them. Partial and unavailable values never count, and `Counted` says how "
        "many jobs each column came from.",
        "",
        *_aggregate_table(jobs),
        "",
        *_review_sections(jobs),
        *_data_notes(_source_lines(records_dir, TASKS_JSONL, ids, refs)),
    ]
    return _assemble(title, body, mode, generated_at, schema_version)


def _beyond_noise(comparison: dict) -> list[dict]:
    """The measurements whose difference is bigger than either side's spread."""
    return [row for row in comparison.get("measurements") or []
            if row["difference"].get("absolute") is not None and not row["within_noise"]]


def _run_count(runs: dict, key: str) -> int:
    """How many runs one side has; anything unparsable counts as none."""
    value = runs.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _better_cell(row: dict) -> str:
    """Who is ahead — never said when the run-to-run spread covers the difference.

    The direction rules are the Lab screen's own: colour came only from
    ``better``, and a within-noise row stayed neutral and said why.
    """
    if row.get("difference", {}).get("absolute") is None:
        return NO_DATA
    if row["within_noise"]:
        return NOISE_NOTE
    if row["better"] in ("modified", "baseline"):
        return row["better"]
    return "no difference"


def _lab_table(comparison: dict) -> list[str]:
    lines = [
        "| Measurement | Unit | Baseline (mean, range, n) | Modified (mean, range, n) "
        "| Difference | Better | Within noise |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in comparison.get("measurements") or []:
        unit = row.get("unit") or ""
        difference = row.get("difference") or {}
        noise = "n/a" if difference.get("absolute") is None else ("yes" if row["within_noise"] else "no")
        lines.append(
            f"| {_cell(row.get('label'))} | {_cell(row.get('unit'))} "
            f"| {_cell(_fmt_side(row.get('baseline'), unit))} "
            f"| {_cell(_fmt_side(row.get('modified'), unit))} "
            f"| {_cell(_fmt_diff(difference, unit))} "
            f"| {_cell(_better_cell(row))} | {noise} |"
        )
    return lines


def _task_table(comparison: dict) -> list[str]:
    """Which tasks each side actually ran; a task one side skipped is an em dash."""
    compared = comparison.get("tasks_compared") or []
    only_base = comparison.get("tasks_only_in_baseline") or []
    only_mod = comparison.get("tasks_only_in_modified") or []
    lines = ["| Task | Baseline | Modified |", "| --- | ---: | ---: |"]
    in_base = set(compared) | set(only_base)
    in_modified = set(compared) | set(only_mod)
    for task in sorted(in_base | in_modified):
        lines.append(
            f"| {_cell(task)} | {'ran' if task in in_base else DASH} "
            f"| {'ran' if task in in_modified else DASH} |"
        )
    return lines


def lab_report_md(comparison: dict, generated_at: str, records_dir: str | None = None,
                  schema_version: str = SCHEMA_VERSION, mode: str = SHAREABLE) -> str:
    """A report over one Lab comparison, from :func:`wsd.lab.compare` output.

    The rows are the very measurements ``compare`` returned, in its order: the
    report cannot disagree with the Lab screen about which numbers were shown.
    """
    baseline = comparison.get("baseline") or DASH
    modified = comparison.get("modified") or DASH
    runs = comparison.get("runs") or {}
    measurements = comparison.get("measurements") or []
    compared = comparison.get("tasks_compared") or []
    only_base = comparison.get("tasks_only_in_baseline") or []
    only_mod = comparison.get("tasks_only_in_modified") or []
    beyond = _beyond_noise(comparison)

    scored = [row for row in measurements if row["difference"].get("absolute") is not None]
    summary = [
        f"`{baseline}` is the baseline and `{modified}` the modified variant: "
        f"{_run_count(runs, 'baseline')} baseline run(s) against "
        f"{_run_count(runs, 'modified')} modified run(s).",
        f"{len(compared)} task(s) compared"
        + (f": {', '.join(compared)}." if compared else ", none: no task was run by both variants."),
    ]
    if only_base:
        summary.append(f"Ran only in the baseline, so not compared: {', '.join(only_base)}.")
    if only_mod:
        summary.append(f"Ran only in the modified variant, so not compared: {', '.join(only_mod)}.")
    if beyond:
        summary.append(
            f"{len(beyond)} of {len(scored)} comparable measurement(s) differ by more than the "
            f"run-to-run spread; every other difference is {NOISE_NOTE}."
        )
    elif scored:
        summary.append(
            f"No measurement of the {len(scored)} comparable one(s) differs by more than the "
            "run-to-run spread: nothing here separates the two variants."
        )
    else:
        summary.append("Neither variant reported a value for any measurement, so nothing here "
                       "separates them.")
    body = [
        "## Summary",
        "",
        *summary,
        "",
        "## Measurements",
        "",
        "Difference is always modified minus baseline (percent of the baseline mean, omitted when "
        "that mean is 0). A side that never measured this figure reads "
        f"`{NO_DATA}`, never 0, and a winner is never claimed from a difference the run-to-run "
        f"spread covers — those read `{NOISE_NOTE}`.",
        "",
        *_lab_table(comparison),
        "",
        "## Tasks",
        "",
        "Which tasks each variant actually ran; a task only one side ran is listed but never compared.",
        "",
        *_task_table(comparison),
        "",
        *_data_notes(_source_lines(records_dir, BENCH_JSONL, [str(baseline), str(modified)],
                                   [f"{_run_count(runs, 'baseline') + _run_count(runs, 'modified')} "
                                    f"bench records over {len(compared)} compared task(s)"])),
    ]
    return _assemble(f"{PRODUCT} Lab report — {baseline} vs {modified}",
                     body, mode, generated_at, schema_version)


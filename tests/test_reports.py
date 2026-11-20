"""One-job Markdown report: content, provenance separation, redaction, route.

The fixture records dir is the same one tests/test_jobs.py uses (T-013 full,
T-016 sampler-past-review, T-091 without review, T-092 without packet/review).
"""

import hashlib
import http.client
import json
import socket
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import wsd.routes
from wsd import __version__, lab
from wsd.jobs import load_jobs
from wsd.reports import (JOBS_COLUMNS, PRIVATE, SHAREABLE, job_report_md,
                         jobs_report_md, lab_report_md)
from wsd.server import RouteError, make_server

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jobs" / "records"
STAMP = "2026-11-19T09:00:00+00:00"


def job(job_id: str) -> dict:
    return next(j for j in load_jobs(str(FIXTURES)) if j["id"] == job_id)


def summary_line(report: str) -> str:
    """The prose line under the metadata header: the whole job in one sentence."""
    return next(line for line in report.splitlines() if line.startswith("Job "))


def line_with(report: str, needle: str) -> str:
    return next(line for line in report.splitlines() if needle in line)


def section(report: str, heading: str) -> str:
    start = report.index(heading) + len(heading)
    rest = report[start:]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


class ReportContentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.full = job_report_md(job("T-013"), STAMP)
        cls.partial = job_report_md(job("T-016"), STAMP)
        cls.no_review = job_report_md(job("T-091"), STAMP)

    def test_title_and_summary(self):
        first = self.full.splitlines()[0]
        self.assertEqual(first, "# Panoptes job report — T-013: Honest throughput figures and no stale browser files")
        summary = summary_line(self.full)
        self.assertIn("**accepted**", summary)
        self.assertIn("1 round(s)", summary)
        self.assertIn("first-time acceptance: yes", summary)
        self.assertIn("1905 s (measured)", summary)
        self.assertIn("155.28 Wh (measured)", summary)

    def test_job_table_fields(self):
        table = section(self.full, "## Job")
        for row in ("| id | T-013 |", "| milestone | M2 |", "| outcome | accepted |",
                    "| rounds | 1 |", "| first-time acceptance | yes |"):
            self.assertIn(row, table)

    def test_measurements_table_job_scope_only(self):
        table = section(self.full, "## Measurements (this job)")
        self.assertIn("| Metric | Value | Unit | Provenance | Scope |", table)
        self.assertIn("| worker_seconds | 1905 | s | measured | job |", table)
        self.assertIn("| energy_cost_gbp | 0.0388 | GBP | calculated | job |", table)
        self.assertNotIn("prefix_hit", table)
        self.assertNotIn("draft_accept", table)

    def test_engine_context_separated_with_honest_heading(self):
        self.assertIn("## Engine context at review time (not per-job performance)", self.full)
        table = section(self.full, "## Engine context at review time")
        self.assertIn("| prefix_hit | 0.872 | ratio | measured | engine-lifetime |", table)
        self.assertIn("engine-lifetime", table)

    def test_review_blockquoted(self):
        review = section(self.full, "## Review")
        lines = [line for line in review.splitlines() if line.strip()]
        self.assertTrue(lines[0].startswith("> # T-013 review"))
        self.assertTrue(all(line.startswith(">") for line in lines))

    def test_partial_job_keeps_value_downgrades_class(self):
        table = section(self.partial, "## Measurements (this job)")
        self.assertIn("| total_energy_wh | 396.5 | Wh | partial | job |", table)
        self.assertIn("| worker_seconds | 1112 | s | partial | job |", table)
        self.assertIn("396.5 Wh (partial)", summary_line(self.partial))
        self.assertIn("first-time acceptance: no", self.partial)

    def test_missing_review_shows_unavailable_not_invented(self):
        review = section(self.no_review, "## Review")
        self.assertIn("No review is stored for this job (unavailable).", review)

    def test_unavailable_shown_as_dash(self):
        report = job_report_md(job("T-092"), STAMP)
        table = section(report, "## Measurements (this job)")
        self.assertIn("| worker_seconds | — | s | unavailable | job |", table)
        self.assertIn("title | — |", section(report, "## Job"))

    def test_data_notes_and_sources(self):
        notes = section(self.full, "## Data notes")
        for name, _meaning in (
            ("measured", ""), ("calculated", ""), ("estimated", ""), ("partial", ""), ("unavailable", ""),
        ):
            self.assertIn(f"`{name}`", notes)
        self.assertIn(
            "Partial values are incomplete measurements (for example a sampler that started "
            "late or kept running); they are not comparable with measured values.", notes,
        )
        self.assertIn("- `perf/tasks.jsonl` (sha256 `", notes)
        self.assertIn("— records: `T-013`", notes)
        self.assertIn("`reviews/T-013.md`, `tasks/T-013.md`", notes)

    def test_metadata_header_carries_mode_and_schema(self):
        lines = self.full.splitlines()
        self.assertEqual(lines[1], "")
        self.assertEqual(lines[2], "- report_schema_version: 2")
        self.assertEqual(lines[3], "- mode: Shareable (`shareable`)")
        self.assertEqual(lines[4], f"- generated: {STAMP}")
        self.assertEqual(lines[5], f"- Panoptes version: {__version__}")
        self.assertIn("- mode: Private (`private`)",
                      job_report_md(job("T-013"), STAMP, mode="private"))
        other = job_report_md(job("T-013"), STAMP, schema_version="7")
        self.assertIn("- report_schema_version: 7", other)
        self.assertNotIn("Generated by Panoptes", self.full)
        self.assertTrue(self.full.rstrip().endswith("Redactions: 0"))


class RedactionTest(unittest.TestCase):
    def test_home_paths_and_secrets_redacted(self):
        base = job("T-013")
        dirty = dict(base, title="Fix /home/alice/Projects/App/src/a.py parsing", review_excerpt=(
            "# T-013 review — ACCEPTED\n\n"
            "Leaked /home/alice/.config/tokens: X-WSD-Token: sup3rs3cretvalue and\n"
            "password = hunter2 in the log excerpt. output_tokens: 137881 stayed.\n"
        ))
        report = job_report_md(dirty, STAMP)
        self.assertNotIn("/home/", report)
        self.assertNotIn("alice", report)
        self.assertNotIn("sup3rs3cretvalue", report)
        self.assertNotIn("hunter2", report)
        self.assertIn("«redacted»", report)
        # an ordinary json key that merely contains "token" is not a secret
        self.assertIn("output_tokens: 137881", report)
        # the home path is gone, the file name is kept
        self.assertIn("Fix […]/a.py parsing", report)
        self.assertNotIn("Projects/App/src/a.py", report)

    def test_url_paths_are_untouched(self):
        dirty = dict(job("T-013"), review_excerpt=(
            "# review\n\n"
            "readiness via /api/health; assets at /static/app.js; "
            "served on http://127.0.0.1:8765/api/health ok\n"
        ))
        report = job_report_md(dirty, STAMP)
        self.assertIn("readiness via /api/health", report)
        self.assertIn("assets at /static/app.js", report)
        self.assertIn("http://127.0.0.1:8765/api/health", report)
        self.assertIn("Redactions: 0", report)

    def test_home_path_keeps_the_file_name(self):
        dirty = dict(job("T-013"), review_excerpt=(
            "# review\n\nLoaded /home/alice/Applications/Radiance/flashnext.yaml fine.\n"
        ))
        report = job_report_md(dirty, STAMP)
        self.assertIn("Loaded […]/flashnext.yaml fine.", report)
        self.assertNotIn("alice", report)
        self.assertIn("Redactions: 1", report)

    def test_redaction_count_is_published(self):
        self.assertIn("Redactions: 0", job_report_md(job("T-013"), STAMP))
        dirty = dict(job("T-013"), review_excerpt=(
            "# review\n\nconfig /root/radiance/flashnext.yaml and /Users/alex/app/config.json"
            " plus api_key = sk-abc123 in the log.\n"
        ))
        # three replacements: two home paths (file names kept) and one secret
        report = job_report_md(dirty, STAMP)
        self.assertIn("config […]/flashnext.yaml and […]/config.json "
                      "plus api_key = «redacted» in the log.", report)
        self.assertIn("Redactions: 3", report)

    def test_stored_review_text_is_verbatim(self):
        for job_id in ("T-001", "T-002", "T-013", "T-016", "T-090"):
            entry = job(job_id)
            excerpt = entry.get("review_excerpt") or ""
            for line in (l for l in excerpt.splitlines() if l.strip()):
                with self.subTest(job_id=job_id, line=line):
                    self.assertIn(f"> {line}", job_report_md(entry, STAMP))

    def test_report_never_contains_absolute_home(self):
        for job_id in ("T-001", "T-002", "T-013", "T-016", "T-090", "T-091", "T-092"):
            with self.subTest(job_id=job_id):
                self.assertNotIn("/home/", job_report_md(job(job_id), STAMP))


class ModeRedactionTest(unittest.TestCase):
    """What each mode hides and what it keeps, on the same dirty review."""

    DIRTY = ("# T-013 review — ACCEPTED\n\n"
             "Ran on panther-box and printed to printer.local.\n"
             "See /home/alice/Projects/App/src/a.py and api_key = sk-abc123 here.\n")

    def setUp(self):
        self.host = mock.patch("socket.gethostname", return_value="panther-box")
        self.host.start()
        self.addCleanup(self.host.stop)
        self.dirty = dict(job("T-013"), review_excerpt=self.DIRTY)

    def test_shareable_hides_paths_hosts_and_secrets(self):
        report = job_report_md(self.dirty, STAMP, str(FIXTURES))
        self.assertIn("- mode: Shareable (`shareable`)", report)
        self.assertNotIn("/home/", report)
        self.assertNotIn("alice", report)
        self.assertNotIn("panther-box", report)
        self.assertNotIn("printer.local", report)
        self.assertNotIn("sk-abc123", report)
        self.assertIn("Ran on [host redacted] and printed to [host redacted].", report)
        self.assertIn("See […]/a.py and api_key = «redacted» here.", report)
        # one home path, two machine names, one secret
        self.assertIn("Redactions: 4", report)

    def test_private_keeps_paths_and_hosts_but_still_hides_the_secret(self):
        report = job_report_md(self.dirty, STAMP, str(FIXTURES), mode=PRIVATE)
        self.assertIn("- mode: Private (`private`)", report)
        self.assertIn("/home/alice/Projects/App/src/a.py", report)
        self.assertIn("Ran on panther-box and printed to printer.local.", report)
        self.assertNotIn("[host redacted]", report)
        self.assertNotIn("sk-abc123", report)
        self.assertIn("api_key = «redacted»", report)
        # private lifts the path and host redactions, never the secret one
        self.assertIn("Redactions: 1", report)

    def test_shareable_is_the_default_for_every_report(self):
        jobs = load_jobs(str(FIXTURES))
        for report in (job_report_md(self.dirty, STAMP), jobs_report_md([self.dirty], STAMP)):
            self.assertIn("- mode: Shareable (`shareable`)", report)
            self.assertNotIn("/home/", report)
            self.assertNotIn("panther-box", report)

    def test_private_jobs_report_keeps_the_paths_a_review_names(self):
        text = jobs_report_md([self.dirty, job("T-091")], STAMP, str(FIXTURES), mode=PRIVATE)
        self.assertIn("/home/alice/Projects/App/src/a.py", text)
        self.assertNotIn("sk-abc123", text)
        self.assertIn("- mode: Private (`private`)", text)


class JobsReportTest(unittest.TestCase):
    """The set-of-jobs report: summary, per-job table, totals, reviews."""

    @classmethod
    def setUpClass(cls):
        cls.jobs = load_jobs(str(FIXTURES))
        cls.report = jobs_report_md(cls.jobs, STAMP, str(FIXTURES))

    def row(self, job_id):
        return line_with(self.report, f"| {job_id} |")

    def test_header_and_summary(self):
        self.assertEqual(
            self.report.splitlines()[0],
            "# Panoptes jobs report — 7 job(s): T-001, T-002, T-013, "
            "T-016, T-090, T-091, T-092")
        self.assertIn("- report_schema_version: 2", self.report)
        self.assertIn("- mode: Shareable (`shareable`)", self.report)
        self.assertIn("- generated: " + STAMP, self.report)
        self.assertIn("7 job(s) in scope: 4 accepted, 1 accepted first time.", self.report)

    def test_rate_and_peak_columns_can_never_be_summed(self):
        # a sum of peaks or of rates is not a quantity: the flag lives on the
        # column definition, so no future edit can start totalling them again
        for label, _name, _conv, _show, _agg_show, additive in JOBS_COLUMNS:
            if "tok/s" in label or "peak" in label:
                self.assertFalse(additive, f"{label} must never be totalled")
            else:
                self.assertTrue(additive, f"{label} is a sum of quantities")
        table = section(self.report, "## Totals and medians")
        self.assertIn("not additive", line_with(table, "| peak context |"))
        self.assertIn("not additive", line_with(table, "| effective tok/s |"))

    def test_title_lists_every_id_in_id_order_when_there_are_eight_or_fewer(self):
        jobs = [{"id": f"T-{n:03d}"} for n in (3, 11, 2, 10, 1, 100, 4)]
        title = jobs_report_md(jobs, STAMP).splitlines()[0]
        self.assertEqual(title,
                         "# Panoptes jobs report — 7 job(s): T-001, T-002, T-003, "
                         "T-004, T-010, T-011, T-100")

    def test_title_gives_a_range_and_count_when_there_are_more_than_eight_ids(self):
        jobs = [{"id": f"T-{n:03d}"} for n in range(9, 0, -1)]
        title = jobs_report_md(jobs, STAMP).splitlines()[0]
        self.assertEqual(title,
                         "# Panoptes jobs report — 9 job(s): T-001 … T-009 (9 jobs)")

    def test_throughput_covers_measured_jobs_only_and_says_so(self):
        line = line_with(self.report, "Accepted tasks per worker-hour")
        self.assertIn("Wh per accepted task: 155.28", line)
        self.assertIn("computed over the 1 of 7 job(s) whose worker seconds", line)
        self.assertIn("partial and unavailable figures never count", line)
        # 1905 s is the only measured pair: 1 accepted / 0.529166.. worker-hours
        self.assertIn("Accepted tasks per worker-hour: 1.89;", line)

    def test_per_job_table_marks_provenance_and_never_zero(self):
        self.assertIn("| id | title | outcome | rounds | first time | worker min | Wh "
                      "| effective tok/s | peak context | compactions |", self.report)
        self.assertEqual(
            self.row("T-013"),
            "| T-013 | Honest throughput figures and no stale browser files | accepted | 1 "
            "| yes | 31.8 | 155.28 | 31.8 (calculated) | 49338 | 5 |")
        self.assertIn("18.5 (partial)", self.row("T-016"))
        self.assertIn("396.5 (partial)", self.row("T-016"))
        # nothing stored is an em dash, never a 0
        self.assertEqual(self.row("T-092"),
                         "| T-092 | — | partial | 1 | no | — | — | — | — | — |")
        self.assertNotIn("| 0 |", self.row("T-092"))

    def test_totals_and_medians_say_how_many_jobs_they_came_from(self):
        table = section(self.report, "## Totals and medians")
        self.assertIn("| Column | Counted | Total | Median |", table)
        self.assertIn("| worker min | 2 of 7 |", table)
        self.assertIn("| Wh | 1 of 7 | 155.28 | 155.28 |", table)
        # a rate is not a sum: adding tok/s across jobs would invent a figure
        self.assertIn("| effective tok/s | 1 of 7 | not additive | 31.8 |", table)
        # partial values never join a total
        self.assertNotIn("3 of 7", table)
        self.assertIn("| peak context | 1 of 7 | not additive | 49338 |", table)

    def test_review_excerpt_under_a_heading_per_job(self):
        self.assertIn("### T-013 — Honest throughput figures and no stale browser files",
                      self.report)
        self.assertIn("> # T-013 review", section(self.report, "### T-013"))
        self.assertIn("No review is stored for this job (unavailable).",
                      section(self.report, "### T-091"))

    def test_source_records_carry_the_file_digest(self):
        digest = hashlib.sha256((FIXTURES / "perf" / "tasks.jsonl").read_bytes()).hexdigest()
        notes = section(self.report, "## Data notes")
        self.assertIn(f"- `perf/tasks.jsonl` (sha256 `{digest}`) — records: `T-016`", notes)
        self.assertIn("`reviews/T-013.md`, `tasks/T-013.md`", notes)
        self.assertIn("All paths are relative to the records directory.", notes)
        self.assertNotIn(str(FIXTURES), notes)

    def test_empty_selection_says_there_is_nothing_to_total(self):
        empty = jobs_report_md([], STAMP, str(FIXTURES))
        self.assertIn("No jobs are in scope, so there is nothing to total or average.", empty)
        self.assertIn("| Column | Counted | Total | Median |", empty)
        self.assertIn("| Wh | 0 of 0 | — | — |", empty)


BENCH_LINES = [
    {"task": "T-004", "variant": "old", "engine": {"available": True, "active_decode_tps": 10,
                                                   "prefill_new_tps": 100}},
    {"task": "T-004", "variant": "old", "engine": {"available": True, "active_decode_tps": 12,
                                                   "prefill_new_tps": 102}},
    {"task": "T-004", "variant": "new", "engine": {"available": True, "active_decode_tps": 14,
                                                   "prefill_new_tps": 101.5}},
    {"task": "T-004", "variant": "new", "engine": {"available": True, "active_decode_tps": 16,
                                                   "prefill_new_tps": 103.5}},
    {"task": "T-007", "variant": "new", "engine": {"available": True, "active_decode_tps": 20}},
]


class LabReportTest(unittest.TestCase):
    """The comparison report: the screen's rows, the screen's caution."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        records = Path(cls.tmp.name)
        (records / "perf").mkdir()
        lines = BENCH_LINES
        (records / "perf" / "bench.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        warnings: list[str] = []
        cls.records = str(records)
        cls.comparison = lab.compare(lab.load_runs(cls.records, warnings), "old", "new")
        cls.report = lab_report_md(cls.comparison, STAMP, cls.records)

    def test_summary_names_variants_runs_and_noise(self):
        summary = section(self.report, "## Summary")
        self.assertIn("`old` is the baseline and `new` the modified variant: "
                      "2 baseline run(s) against 3 modified run(s).", summary)
        self.assertIn("1 task(s) compared: T-004.", summary)
        self.assertIn("Ran only in the modified variant, so not compared: T-007.", summary)
        self.assertIn("1 of 2 comparable measurement(s) differ by more than the "
                      "run-to-run spread", summary)

    def test_rows_are_the_comparison_the_screen_shows(self):
        table = section(self.report, "## Measurements")
        self.assertEqual(
            line_with(table, "| active generation tok/s |"),
            "| active generation tok/s | tok/s | 11 tok/s (n=2, 10–12) "
            "| 15 tok/s (n=2, 14–16) | +4 (+36.36 %) | modified | no |")
        self.assertEqual(len([l for l in table.splitlines() if l.startswith("| ")]) - 2,
                         len(self.comparison["measurements"]))

    def test_a_within_noise_difference_never_names_a_winner(self):
        row = line_with(self.report, "| prefill new tok/s |")
        self.assertIn("not distinguishable from run-to-run variation", row)
        self.assertTrue(row.endswith("| yes |"))
        self.assertNotIn("| modified |", row)
        self.assertNotIn("| baseline |", row)

    def test_a_side_that_never_measured_is_no_data_not_zero(self):
        self.assertEqual(line_with(self.report, "| output tokens |"),
                         "| output tokens | tokens | no data | no data | — | no data | n/a |")

    def test_task_list_shows_which_side_ran_what(self):
        table = section(self.report, "## Tasks")
        self.assertIn("| T-004 | ran | ran |", table)
        self.assertIn("| T-007 | — | ran |", table)

    def test_bench_digest_and_mode_are_published(self):
        digest = hashlib.sha256(
            (Path(self.records) / "perf" / "bench.jsonl").read_bytes()).hexdigest()
        notes = section(self.report, "## Data notes")
        self.assertIn(f"- `perf/bench.jsonl` (sha256 `{digest}`) — records: `old`, `new`", notes)
        self.assertIn("- mode: Shareable (`shareable`)", self.report)
        private = lab_report_md(self.comparison, STAMP, self.records, mode=PRIVATE)
        self.assertIn("- mode: Private (`private`)", private)

    def test_no_bench_records_yet_is_said_not_invented(self):
        empty = lab.compare([], "old", "new")
        report = lab_report_md(empty, STAMP, self.records)
        self.assertIn("0 baseline run(s) against 0 modified run(s).", report)
        self.assertIn("no task was run by both variants", report)
        self.assertIn("Neither variant reported a value for any measurement", report)


class JobReportFunctionTest(unittest.TestCase):
    def setUp(self):
        self.original = wsd.routes.RECORDS_DIR
        wsd.routes.RECORDS_DIR = str(FIXTURES)

    def tearDown(self):
        wsd.routes.RECORDS_DIR = self.original

    def test_job_report_text_and_filename(self):
        text, filename = wsd.routes.job_report("T-013")
        self.assertTrue(text.startswith("# Panoptes job report — T-013:"))
        self.assertEqual(filename, "panoptes-T-013-report.md")

    def test_unknown_id_is_404(self):
        with self.assertRaises(RouteError) as ctx:
            wsd.routes.job_report("T-999")
        self.assertEqual(ctx.exception.status, 404)

    def test_bad_path_is_404(self):
        for tail in ("", "T-013/extra"):
            with self.subTest(tail=tail), self.assertRaises(RouteError) as ctx:
                wsd.routes.job_report(tail)
            self.assertEqual(ctx.exception.status, 404)

    def test_no_records_dir_is_404(self):
        wsd.routes.RECORDS_DIR = None
        with self.assertRaises(RouteError) as ctx:
            wsd.routes.job_report("T-013")
        self.assertEqual(ctx.exception.status, 404)


class ReportHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = wsd.routes.RECORDS_DIR
        wsd.routes.RECORDS_DIR = str(FIXTURES)
        cls.server = make_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        wsd.routes.RECORDS_DIR = cls.original

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, dict(resp.headers), body

    def head(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("HEAD", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, dict(resp.headers), body

    def test_report_download_headers_and_body(self):
        status, headers, body = self.get("/api/jobs/T-013/report.md")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/markdown; charset=utf-8")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="panoptes-T-013-report.md"')
        text = body.decode("utf-8")
        self.assertIn("- report_schema_version: 2", text)
        self.assertTrue(text.startswith("# Panoptes job report — T-013:"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(int(headers["Content-Length"]), len(body))

    def test_report_head_keeps_headers_without_body(self):
        status, headers, body = self.head("/api/jobs/T-013/report.md")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/markdown; charset=utf-8")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="panoptes-T-013-report.md"')
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body, b"")

    def test_report_path_without_a_usable_id_is_json_404(self):
        for path in ("/api/jobs/report.md", "/api/jobs/T-013/extra/report.md"):
            with self.subTest(path=path):
                status, headers, body = self.get(path)
                self.assertEqual(status, 404)
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertIn("error", json.loads(body))

    def test_unknown_job_is_json_404(self):
        status, headers, body = self.get("/api/jobs/T-999/report.md")
        self.assertEqual(status, 404)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("error", json.loads(body))

    def test_other_job_routes_still_json(self):
        status, headers, _body = self.get("/api/jobs/T-013")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        status, _headers, _body = self.get("/api/jobs/T-013/extra")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()

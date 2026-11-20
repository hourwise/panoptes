"""Lab trends (T-045): accepted tasks in acceptance order, applied switches marked.

Fixtures live in a temp dir: ``perf/tasks.jsonl`` + ``tasks/<id>.md`` +
``reviews/<id>.md`` for the points, a switch journal for the markers. They carry
the holes the real records have — a task with no ``accepted_at``, one the review
sent back, a review whose first line names no verdict, a partial backfill row, a
journal entry that never finished — because those are what the card has to be
honest about. Nothing here reads the machine's real records or journal.
"""

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import wsd.server
from wsd import jobs, routes, trends

ENGINE = "radiance-r9700"


def _record(task, **fields):
    return {"task": task, "milestone": "M1", "rounds": 1, **fields}


def _write_records(root: Path, records, reviews=()):
    """A records dir in the shape ``wsd.jobs`` reads."""
    (root / "perf").mkdir(parents=True, exist_ok=True)
    (root / "tasks").mkdir(exist_ok=True)
    (root / "reviews").mkdir(exist_ok=True)
    with (root / "perf" / "tasks.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    for record in records:
        (root / "tasks" / f"{record['task']}.md").write_text(
            f"# {record['task']} (M1): {record['task']} title\n\nGoal.\n", encoding="utf-8")
    for job_id, header in reviews:
        (root / "reviews" / f"{job_id}.md").write_text(header + "\n\nBody.\n", encoding="utf-8")


def _entry(entry_id, engine, action, to, outcome, finished=None):
    entry = {
        "id": entry_id, "engine": engine, "action": action,
        "started": "2026-11-19T09:00:00+00:00", "from": {"name": "flashnext.yaml", "sha256": "a"},
        "to": {"name": to, "sha256": "b"}, "backup": "/tmp/backup.yaml",
        "outcome": outcome, "errors": [], "steps": [], "dry_run": False,
    }
    if finished:
        entry["finished"] = finished
    return entry


def _write_journal(root: Path, engine, lines):
    """One journal file; every item of ``lines`` is one JSONL line, so a switch
    that was written twice (running, then finished) is written twice here."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{engine}.jsonl").open("w", encoding="utf-8") as handle:
        for entry in lines:
            handle.write(json.dumps(entry) + "\n")


class TrendsTest(unittest.TestCase):
    """One fixture dir: six tasks and one journal."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.records = self.root / "records"
        self.journal = self.root / "journal"
        _write_records(self.records, [
            _record("T-001", accepted_at="2026-11-19T10:00:00+0000", first_time_accept=True,
                    worker_seconds=600, total_energy_wh=12.5, effective_worker_tps=30.0,
                    last_session_peak_ctx=20000, last_session_compactions=1),
            # second round, and the sampler kept running: every time-dependent figure partial
            _record("T-002", accepted_at="2026-11-19T12:00:00+0000", first_time_accept=False,
                    rounds=2, worker_seconds=900, worker_seconds_partial=True,
                    last_session_peak_ctx=40000, last_session_compactions=2),
            # its accepted stamp sits between the two, so order is not id order
            _record("T-003", accepted_at="2026-11-19T11:00:00+0000", first_time_accept=True,
                    worker_seconds=300),
            _record("T-004", first_time_accept=True, worker_seconds=100),      # no accepted_at
            _record("T-005", accepted_at="2026-11-19T13:00:00+0000", worker_seconds=100),
            _record("T-006", accepted_at="yesterday", worker_seconds=100),     # unreadable stamp
        ], reviews=[
            ("T-001", "# T-001 review — ACCEPTED first time (2026-11-19)"),
            ("T-002", "# T-002 review — ACCEPTED after round 2"),
            ("T-003", "# T-003 review — small change, docs and a test"),        # no verdict word
            ("T-004", "# T-004 review — ACCEPTED"),
            ("T-005", "# T-005 review — SENT BACK: the packet was wrong"),
            ("T-006", "# T-006 review — ACCEPTED"),
        ])
        _write_journal(self.journal, ENGINE, [
            _entry("s3", ENGINE, "apply", "flashnext-131k.yaml", "applied",
                   finished="2026-11-19T11:30:00+00:00"),
            _entry("s4", ENGINE, "apply", "flashnext-131k.yaml", "refused",
                   finished="2026-11-19T10:30:00+00:00"),
            _entry("s5", ENGINE, "apply", "flashnext-131k.yaml", "rolled_back",
                   finished="2026-11-19T11:45:00+00:00"),
            _entry("s6", ENGINE, "restore", "flashnext.yaml", "applied",
                   finished="2026-11-19T12:30:00+00:00"),
            _entry("s7", ENGINE, "apply", "flashnext-131k.yaml", "applied"),
            _entry("s8", ENGINE, "apply", "flashnext-64k.yaml", "running"),
            _entry("s8", ENGINE, "apply", "flashnext-64k.yaml", "applied",
                   finished="2026-11-19T12:45:00+00:00"),
        ])
        self.dirs = {ENGINE: str(self.journal)}

    def load(self, records=None, journal_dirs=None):
        return trends.load_trends(self.records if records is None else records,
                                  self.dirs if journal_dirs is None else journal_dirs)

    def points(self, payload):
        return {point["id"]: point for point in payload["points"]}

    # ------------------------------------------------------------------ points

    def test_points_are_in_acceptance_order(self):
        payload = self.load(journal_dirs={})
        self.assertEqual([point["id"] for point in payload["points"]], ["T-001", "T-003", "T-002"])
        self.assertEqual([point["accepted_at"] for point in payload["points"]],
                         ["2026-11-19T10:00:00+0000", "2026-11-19T11:00:00+0000",
                          "2026-11-19T12:00:00+0000"])

    def test_a_task_without_an_accepted_at_is_left_out_and_warned(self):
        payload = self.load(journal_dirs={})
        self.assertNotIn("T-004", self.points(payload))
        self.assertIn("T-004: accepted_at missing; left out of the trends", payload["warnings"])

    def test_an_unreadable_accepted_at_is_left_out_and_warned(self):
        payload = self.load(journal_dirs={})
        self.assertIn("T-006: accepted_at yesterday; left out of the trends", payload["warnings"])

    def test_a_task_the_review_sent_back_is_left_out_and_warned(self):
        payload = self.load(journal_dirs={})
        self.assertNotIn("T-005", self.points(payload))
        self.assertIn("T-005: outcome is rejected, not accepted; left out of the trends",
                      payload["warnings"])

    def test_a_review_without_a_verdict_word_is_still_an_accepted_task(self):
        """``perf/tasks.jsonl`` is the supervisor's record of accepted work, so a
        review header naming no verdict (``unknown``) must not drop the newest
        tasks off the chart."""
        payload = self.load(journal_dirs={})
        self.assertIn("T-003", self.points(payload))
        by_id = {job["id"]: job for job in jobs.load_jobs(str(self.records))}
        self.assertEqual(by_id["T-003"]["outcome"], "unknown")

    def test_equal_stamps_fall_back_to_the_task_id(self):
        tie = self.root / "tie"
        _write_records(tie, [
            _record("T-002", accepted_at="2026-11-19T10:00:00+0000"),
            _record("T-001", accepted_at="2026-11-19T10:00:00+0000"),
        ])
        payload = self.load(records=tie, journal_dirs={})
        self.assertEqual([point["id"] for point in payload["points"]], ["T-001", "T-002"])

    def test_a_naive_stamp_orders_as_utc(self):
        mixed = self.root / "mixed"
        _write_records(mixed, [
            _record("T-002", accepted_at="2026-11-19T12:00:00"),
            _record("T-001", accepted_at="2026-11-19T11:00:00+0000"),
        ])
        payload = self.load(records=mixed, journal_dirs={})
        self.assertEqual([point["id"] for point in payload["points"]], ["T-001", "T-002"])

    # --------------------------------------------------------- metric honesty

    def test_metrics_are_the_job_cells_verbatim(self):
        """Nothing here recomputes, re-unites or re-classes a figure."""
        by_id = {job["id"]: job for job in jobs.load_jobs(str(self.records))}
        points = self.points(self.load(journal_dirs={}))
        for job_id in ("T-001", "T-002", "T-003"):
            for name in trends.METRICS:
                self.assertEqual(points[job_id][name], by_id[job_id]["metrics"][name],
                                 f"{job_id}.{name}")

    def test_partial_and_missing_values_survive_as_they_are(self):
        point = self.points(self.load(journal_dirs={}))["T-002"]
        self.assertEqual(point["worker_seconds"],
                         {"value": 900, "unit": "s", "provenance": "partial", "scope": "job"})
        self.assertEqual(point["total_energy_wh"],
                         {"value": None, "unit": "Wh", "provenance": "unavailable", "scope": "job"})
        # a last_session_* figure of a two-round task is a session figure, not a total
        self.assertEqual(point["peak_context"],
                         {"value": 40000, "unit": "tokens", "provenance": "partial", "scope": "job"})
        self.assertIsNone(point["effective_worker_tps"]["value"])

    def test_the_point_carries_the_answer_not_only_the_numbers(self):
        points = self.points(self.load(journal_dirs={}))
        self.assertIs(points["T-001"]["first_time_accept"], True)
        self.assertIs(points["T-002"]["first_time_accept"], False)
        self.assertEqual(points["T-002"]["rounds"], 2)
        self.assertEqual(points["T-001"]["title"], "T-001 title")

    def test_the_point_keys_are_the_names_the_charts_use(self):
        self.assertEqual(sorted(self.load(journal_dirs={})["points"][0]),
                         sorted(["id", "title", "accepted_at", "first_time_accept", "rounds",
                                 *trends.METRICS]))

    # ---------------------------------------------------------------- markers

    def test_markers_are_the_applied_switches_oldest_first(self):
        payload = self.load()
        self.assertEqual([(marker["at"], marker["to"]) for marker in payload["markers"]],
                         [("2026-11-19T11:30:00+00:00", "flashnext-131k.yaml"),
                          ("2026-11-19T12:30:00+00:00", "flashnext.yaml"),
                          ("2026-11-19T12:45:00+00:00", "flashnext-64k.yaml")])
        self.assertEqual([marker["action"] for marker in payload["markers"]],
                         ["apply", "restore", "apply"])
        self.assertEqual({marker["engine"] for marker in payload["markers"]}, {ENGINE})

    def test_refused_and_rolled_back_switches_are_not_markers(self):
        stamps = [marker["at"] for marker in self.load()["markers"]]
        self.assertNotIn("2026-11-19T10:30:00+00:00", stamps)
        self.assertNotIn("2026-11-19T11:45:00+00:00", stamps)

    def test_a_switch_that_never_finished_is_warned_about(self):
        journal_warnings = [w for w in self.load()["warnings"] if w.startswith("engine")]
        self.assertEqual(len(journal_warnings), 1, journal_warnings)
        self.assertIn("s7", journal_warnings[0])
        self.assertIn("skipped", journal_warnings[0])

    def test_two_switch_engines_are_merged_by_time(self):
        other = self.root / "journal-2"
        _write_journal(other, "other-engine",
                       [_entry("o1", "other-engine", "apply", "long.yaml", "applied",
                               finished="2026-11-19T09:30:00+00:00")])
        payload = self.load(journal_dirs={ENGINE: str(self.journal), "other-engine": str(other)})
        self.assertEqual([(marker["engine"], marker["at"]) for marker in payload["markers"]],
                         [("other-engine", "2026-11-19T09:30:00+00:00"),
                          (ENGINE, "2026-11-19T11:30:00+00:00"),
                          (ENGINE, "2026-11-19T12:30:00+00:00"),
                          (ENGINE, "2026-11-19T12:45:00+00:00")])

    def test_a_journal_that_is_not_there_says_nothing(self):
        payload = self.load(journal_dirs={ENGINE: str(self.root / "nowhere")})
        self.assertEqual(payload["markers"], [])
        self.assertEqual([w for w in payload["warnings"] if w.startswith("engine")], [])

    def test_no_journal_dirs_means_no_markers(self):
        self.assertEqual(trends.load_trends(self.records, None)["markers"], [])
        self.assertEqual(trends.load_trends(self.records, {ENGINE: ""})["markers"], [])

    # ------------------------------------------------------------- no records

    def test_no_records_dir_says_so(self):
        for missing in (None, ""):
            with self.subTest(records_dir=missing):
                payload = trends.load_trends(missing, self.dirs)
                self.assertEqual(payload["points"], [])
                self.assertEqual(payload["markers"], [])
                self.assertIn("no task trends to show", " ".join(payload["warnings"]))

    def test_a_records_dir_that_is_gone_says_so(self):
        payload = trends.load_trends(str(self.root / "gone"), self.dirs)
        self.assertEqual(payload["points"], [])
        self.assertIn("not found", " ".join(payload["warnings"]))

    def test_records_without_tasks_are_empty_points_not_an_error(self):
        empty = self.root / "empty"
        empty.mkdir()
        payload = self.load(records=empty)
        self.assertEqual(payload["points"], [])
        self.assertEqual(len(payload["markers"]), 3)
        self.assertEqual([w for w in payload["warnings"] if w.startswith("engine") and "s7" in w],
                         payload["warnings"], payload["warnings"])


class TrendsRouteTest(unittest.TestCase):
    """``GET /api/lab/trends`` — the same payload, wired to the config."""

    def setUp(self):
        for name in ("RECORDS_DIR", "ENGINE_PROFILES", "ENGINE_JOURNAL_DIRS"):
            self.addCleanup(setattr, routes, name, getattr(routes, name))
        routes.ENGINE_PROFILES = {}
        routes.ENGINE_JOURNAL_DIRS = {}

    def fixture(self, tmp: str) -> Path:
        root = Path(tmp)
        _write_records(root, [_record("T-001", accepted_at="2026-11-19T10:00:00+0000",
                                      worker_seconds=600)],
                       [("T-001", "# T-001 review — ACCEPTED first time")])
        routes.RECORDS_DIR = str(root)
        return root

    def test_route_is_registered(self):
        self.assertIs(wsd.server.ROUTES["/api/lab/trends"], routes.lab_trends)

    def test_route_returns_the_trend_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.fixture(tmp)
            journal = root / "journal"
            _write_journal(journal, ENGINE,
                           [_entry("s1", ENGINE, "apply", "flashnext-131k.yaml", "applied",
                                   finished="2026-11-19T11:00:00+00:00")])
            with mock.patch.object(routes, "_journal_dirs", return_value={ENGINE: str(journal)}):
                payload = routes.lab_trends()
        self.assertEqual([point["id"] for point in payload["points"]], ["T-001"])
        self.assertEqual([marker["to"] for marker in payload["markers"]], ["flashnext-131k.yaml"])
        self.assertEqual(payload["warnings"], [])

    def test_journal_dirs_are_where_the_history_route_reads_them(self):
        """Configured ``journal_dir`` wins; the rest fall under the records dir."""
        routes.ENGINE_PROFILES = {ENGINE: ("/tmp/profiles", "a.yaml"), "plain": ("/tmp/p", None)}
        routes.ENGINE_JOURNAL_DIRS = {ENGINE: "/tmp/journals"}
        routes.RECORDS_DIR = "/tmp/records"
        self.assertEqual(routes._journal_dirs(),
                         {ENGINE: "/tmp/journals", "plain": "/tmp/records/config-journal"})

    def test_an_engine_without_profiles_has_no_journal(self):
        self.assertEqual(routes._journal_dirs(), {})

    def test_the_route_is_served_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.fixture(tmp)
            httpd = wsd.server.make_server("127.0.0.1", 0)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(httpd.server_close)
            self.addCleanup(thread.join, 5)
            self.addCleanup(httpd.shutdown)
            request = urllib.request.Request(
                f"http://127.0.0.1:{httpd.server_address[1]}/api/lab/trends",
                headers={"X-WSD-Token": wsd.server.TOKEN})
            with urllib.request.urlopen(request, timeout=5) as got:
                self.assertEqual(got.headers["Content-Type"], "application/json")
                body = json.loads(got.read())
        self.assertEqual([point["id"] for point in body["points"]], ["T-001"])
        self.assertEqual(body["markers"], [])


if __name__ == "__main__":
    unittest.main()

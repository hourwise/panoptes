"""Job history: load_jobs, provenance/scope labels and job_summary.

The fixture records dir mixes a real schema-1 backfill line (odd types), a
superseded duplicate id, a full sampler record, a stopped partial job, a
multi-round job whose sampler ran past review (T-016), a job without review,
and one without packet or review at all.
"""

import unittest
from pathlib import Path

from wsd import routes
from wsd.jobs import REVIEW_EXCERPT_CHARS, job_summary, load_jobs
from wsd.server import RouteError

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jobs" / "records"


def by_id(jobs):
    return {job["id"]: job for job in jobs}


class LoadJobsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jobs = load_jobs(str(FIXTURES))
        cls.job = by_id(cls.jobs)

    def test_newest_first_and_deduplicated(self):
        # dated records first (newest accepted_at), then undated backfill
        self.assertEqual(
            [job["id"] for job in self.jobs],
            ["T-016", "T-090", "T-013", "T-092", "T-002", "T-091", "T-001"],
        )

    def test_duplicate_id_last_line_wins(self):
        t002 = self.job["T-002"]
        self.assertEqual(t002["rounds"], 2)  # superseded copy said 9
        self.assertEqual(t002["metrics"]["worker_seconds"]["value"], 289)  # superseded said 1

    def test_full_record_fields(self):
        t013 = self.job["T-013"]
        self.assertEqual(t013["outcome"], "accepted")
        self.assertEqual(t013["milestone"], "M2")
        self.assertEqual(t013["title"], "Honest throughput figures and no stale browser files")
        self.assertEqual(t013["rounds"], 1)
        self.assertIs(t013["first_time_accept"], True)  # derived: accepted with rounds == 1
        self.assertTrue(t013["review_excerpt"].startswith("# T-013 review"))
        self.assertLessEqual(len(t013["review_excerpt"]), REVIEW_EXCERPT_CHARS)

    def test_provenance_classes(self):
        m = self.job["T-013"]["metrics"]
        self.assertEqual(m["worker_seconds"], {"value": 1905, "unit": "s", "provenance": "measured", "scope": "job"})
        self.assertEqual(m["gpu_energy_wh"]["provenance"], "measured")
        self.assertEqual(m["cpu_energy_wh"]["provenance"], "measured")
        self.assertEqual(m["total_energy_wh"]["value"], 155.28)
        self.assertEqual(m["energy_cost_gbp"]["provenance"], "calculated")
        self.assertEqual(m["effective_worker_tps"]["provenance"], "calculated")
        self.assertEqual(m["output_tokens"]["value"], 60578)
        self.assertEqual(m["reasoning_tokens"]["value"], 38264)
        self.assertEqual(m["peak_context"]["value"], 49338)
        self.assertEqual(m["python_tests"]["value"], 134)

    def test_engine_lifetime_scope_never_job(self):
        for job in self.jobs:
            for name in ("prefix_hit", "draft_accept"):
                self.assertEqual(job["metrics"][name]["scope"], "engine-lifetime", f"{job['id']}/{name}")
        m = self.job["T-013"]["metrics"]
        self.assertEqual(m["prefix_hit"]["value"], 0.872)
        # every other metric of a full record is job-scoped
        job_scoped = {n for n, cell in m.items() if cell["scope"] == "job"}
        self.assertNotIn("prefix_hit", job_scoped)
        self.assertNotIn("draft_accept", job_scoped)

    def test_backfill_values_are_partial_not_coerced(self):
        m = self.job["T-001"]["metrics"]
        self.assertEqual(
            m["peak_context"],
            {"value": None, "unit": "tokens", "provenance": "partial", "scope": "job", "raw": "~20000"},
        )
        self.assertEqual(m["worker_seconds"]["provenance"], "partial")
        self.assertEqual(m["worker_seconds"]["value"], 190)  # numeric partial keeps its value
        self.assertEqual(m["python_tests"]["provenance"], "partial")

    def test_partial_seconds_downgrade_every_time_metric(self):
        # T-016-like: the sampler kept running through supervisor review, so
        # no time-dependent figure of that record may claim "measured".
        m = self.job["T-016"]["metrics"]
        for name in ("worker_seconds", "gpu_energy_wh", "cpu_energy_wh", "total_energy_wh",
                     "energy_cost_gbp", "effective_worker_tps"):
            self.assertEqual(m[name]["provenance"], "partial", name)
        self.assertEqual(m["gpu_energy_wh"]["value"], 316.4)  # value kept, class downgraded
        # not time-dependent: unaffected by the partial seconds
        self.assertEqual(m["python_tests"]["provenance"], "measured")
        self.assertEqual(m["output_tokens"]["provenance"], "measured")
        self.assertEqual(m["reasoning_tokens"]["provenance"], "measured")

    def test_worker_seconds_partial_flag(self):
        m = self.job["T-090"]["metrics"]
        self.assertEqual(m["worker_seconds"]["provenance"], "partial")
        self.assertEqual(m["worker_seconds"]["value"], 500)
        self.assertEqual(m["gpu_energy_wh"]["provenance"], "partial")  # time-dependent too
        self.assertEqual(m["cpu_energy_wh"], {"value": None, "unit": "Wh", "provenance": "unavailable", "scope": "job"})

    def test_error_counts_are_measured_or_absent(self):
        # OMP's error signals — failed tool calls and aborted requests. They
        # are summed over a task's sessions, so neither rounds > 1 nor partial
        # seconds may downgrade them, and an absent key is never a zero.
        m = self.job["T-013"]["metrics"]
        self.assertEqual(
            m["failed_tool_calls"],
            {"value": 3, "unit": "count", "provenance": "measured", "scope": "job"})
        self.assertEqual(
            m["aborted_requests"],
            {"value": 1, "unit": "count", "provenance": "measured", "scope": "job"})
        # a checked zero stays zero (T-090: rounds 2, seconds partial)
        m = self.job["T-090"]["metrics"]
        for name in ("failed_tool_calls", "aborted_requests"):
            self.assertEqual(
                m[name],
                {"value": 0, "unit": "count", "provenance": "measured", "scope": "job"}, name)
        # no keys in the record: unavailable, never an invented zero
        for job_id in ("T-016", "T-091", "T-092", "T-002", "T-001"):
            for name in ("failed_tool_calls", "aborted_requests"):
                self.assertEqual(
                    self.job[job_id]["metrics"][name],
                    {"value": None, "unit": "count", "provenance": "unavailable",
                     "scope": "job"}, f"{job_id}/{name}")
        # nothing in the payload calls either count a retry
        self.assertNotIn("retries", self.job["T-013"]["metrics"])

    def test_last_session_metrics_are_only_measured_for_single_round(self):
        # last_session_* describes the final harness session only: with
        # rounds > 1 it is not a task total (T-016: peak 24,070 from round 3).
        t016 = self.job["T-016"]["metrics"]
        self.assertEqual(
            t016["peak_context"],
            {"value": 24070, "unit": "tokens", "provenance": "partial", "scope": "job"},
        )
        self.assertEqual(t016["compactions"], {"value": 2, "unit": "count", "provenance": "partial", "scope": "job"})
        # T-013-like: rounds == 1, the last session is the whole task
        t013 = self.job["T-013"]["metrics"]
        self.assertEqual(t013["peak_context"]["provenance"], "measured")
        self.assertEqual(t013["compactions"]["provenance"], "measured")

    def test_outcomes_from_review_first_line(self):
        self.assertEqual(self.job["T-090"]["outcome"], "stopped")
        self.assertEqual(self.job["T-013"]["outcome"], "accepted")
        self.assertEqual(self.job["T-001"]["outcome"], "accepted")  # ACCEPTED despite send-backs

    def test_missing_review_and_packet(self):
        t091 = self.job["T-091"]
        self.assertEqual(t091["outcome"], "unknown")
        self.assertIsNone(t091["review_excerpt"])
        self.assertIsNone(t091["title"])
        self.assertIsNone(t091["milestone"])
        self.assertIs(t091["first_time_accept"], False)  # derived: rounds == 2
        # no review, partial record -> partial outcome; first_time_accept derived False
        self.assertEqual(self.job["T-092"]["outcome"], "partial")
        self.assertIsNone(self.job["T-092"]["review_excerpt"])

    def test_missing_metric_is_unavailable(self):
        m = self.job["T-092"]["metrics"]
        self.assertEqual(m["worker_seconds"], {"value": None, "unit": "s", "provenance": "unavailable", "scope": "job"})
        self.assertEqual(m["prefix_hit"]["provenance"], "unavailable")

    def test_absent_records_dir(self):
        self.assertEqual(load_jobs("/nonexistent/records/dir"), [])
        self.assertEqual(load_jobs(None), [])
        self.assertEqual(load_jobs(""), [])


class JobSummaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = job_summary(load_jobs(str(FIXTURES)))

    def test_counts_and_totals_over_measured_only(self):
        metrics = self.summary["metrics"]
        self.assertEqual(metrics["worker_seconds"]["provenance"], "measured")
        self.assertEqual(metrics["worker_seconds"]["count"], 2)  # T-013, T-091; backfill/partial excluded
        self.assertEqual(metrics["worker_seconds"]["total"], 1905 + 300)
        self.assertAlmostEqual(metrics["worker_seconds"]["average"], (1905 + 300) / 2, places=6)
        self.assertEqual(metrics["gpu_energy_wh"]["count"], 1)  # only T-013; T-090/T-016 have partial seconds
        self.assertAlmostEqual(metrics["gpu_energy_wh"]["total"], 113.34, places=6)
        self.assertEqual(metrics["python_tests"]["count"], 4)  # T-013, T-090, T-091, T-016
        self.assertEqual(metrics["python_tests"]["total"], 134 + 12 + 5 + 200)

    def test_calculated_metrics_use_non_partial_inputs(self):
        metrics = self.summary["metrics"]
        self.assertEqual(metrics["energy_cost_gbp"]["provenance"], "calculated")
        self.assertEqual(metrics["energy_cost_gbp"]["count"], 1)  # T-013; T-016 downgraded to partial
        self.assertAlmostEqual(metrics["energy_cost_gbp"]["total"], 0.0388, places=6)
        self.assertAlmostEqual(metrics["energy_cost_gbp"]["average"], 0.0388, places=6)
        self.assertEqual(metrics["effective_worker_tps"]["provenance"], "calculated")
        self.assertEqual(metrics["effective_worker_tps"]["count"], 1)
        self.assertEqual(metrics["effective_worker_tps"]["total"], 31.8)

    def test_first_time_accept_rate(self):
        fta = self.summary["first_time_accept"]
        self.assertEqual(fta["of"], 7)
        self.assertEqual(fta["count"], 1)  # only T-013
        self.assertAlmostEqual(fta["rate"], 1 / 7, places=4)

    def test_error_signal_totals(self):
        metrics = self.summary["metrics"]
        # only T-013 (3/1) and T-090 (0/0) carry the keys
        self.assertEqual(metrics["failed_tool_calls"],
                         {"unit": "count", "provenance": "measured",
                          "count": 2, "total": 3, "average": 1.5})
        self.assertEqual(metrics["aborted_requests"],
                         {"unit": "count", "provenance": "measured",
                          "count": 2, "total": 1, "average": 0.5})

    def test_accepted_tasks_per_worker_hour(self):
        # only non-partial seconds count: T-013 (accepted) + T-091 (unknown) = 2205 s
        self.assertAlmostEqual(self.summary["worker_hours_measured"], 2205 / 3600, places=3)
        self.assertAlmostEqual(self.summary["accepted_tasks_per_worker_hour"], 1 / (2205 / 3600), places=3)

    def test_empty_jobs(self):
        summary = job_summary([])
        self.assertEqual(summary["jobs"], 0)
        self.assertIsNone(summary["metrics"]["worker_seconds"]["total"])
        self.assertIsNone(summary["first_time_accept"]["rate"])
        self.assertIsNone(summary["accepted_tasks_per_worker_hour"])
        self.assertEqual(summary["worker_hours_measured"], 0.0)


class JobsRouteTest(unittest.TestCase):
    def test_jobs_route_payload(self):
        original = routes.RECORDS_DIR
        routes.RECORDS_DIR = str(FIXTURES)
        try:
            payload = routes.jobs()
        finally:
            routes.RECORDS_DIR = original
        self.assertEqual(payload["warnings"], [])
        self.assertEqual(payload["jobs"][0]["id"], "T-016")
        self.assertEqual(payload["summary"]["jobs"], 7)

    def test_jobs_route_without_records_warns(self):
        original = routes.RECORDS_DIR
        routes.RECORDS_DIR = None
        try:
            payload = routes.jobs()
        finally:
            routes.RECORDS_DIR = original
        self.assertEqual(payload["jobs"], [])
        self.assertEqual(payload["summary"]["jobs"], 0)
        self.assertIn("job history is empty", " ".join(payload["warnings"]))

    def test_jobs_route_missing_dir_warns(self):
        original = routes.RECORDS_DIR
        routes.RECORDS_DIR = "/nonexistent/records/dir"
        try:
            payload = routes.jobs()
        finally:
            routes.RECORDS_DIR = original
        self.assertEqual(payload["jobs"], [])
        self.assertIn("not found", " ".join(payload["warnings"]))

    def test_job_detail_found(self):
        original = routes.RECORDS_DIR
        routes.RECORDS_DIR = str(FIXTURES)
        try:
            self.assertEqual(routes.job_detail("T-013")["title"], "Honest throughput figures and no stale browser files")
            with self.assertRaises(RouteError) as ctx:
                routes.job_detail("T-999")
            self.assertEqual(ctx.exception.status, 404)
            with self.assertRaises(RouteError):
                routes.job_detail("T-013/extra")
            with self.assertRaises(RouteError):
                routes.job_detail("")
        finally:
            routes.RECORDS_DIR = original

    def test_job_detail_without_records_is_404(self):
        original = routes.RECORDS_DIR
        routes.RECORDS_DIR = None
        try:
            with self.assertRaises(RouteError) as ctx:
                routes.job_detail("T-013")
            self.assertEqual(ctx.exception.status, 404)
        finally:
            routes.RECORDS_DIR = original


if __name__ == "__main__":
    unittest.main()

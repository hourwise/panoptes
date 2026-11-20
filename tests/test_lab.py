"""Panoptes Lab: bench records, variant comparison and the two read-only routes.

The ``records`` fixture is two variants over the same two tasks, two runs each,
plus a third task only ``base`` ever ran, and it hides the honest holes the
engine has to cope with: one run with an empty ``energy`` table and an
``engine`` block marked ``available: false`` because its engine restarted
mid-run, one run whose ``hidden`` summary carries a note instead of a pass
rate, a gate verdict that is ``false``, and a run with a null ``ttft_mean_s``.
``single`` has one run per side, ``empty`` is a zero-byte file and
``malformed`` mixes two usable records with four unusable lines.
"""

import unittest
from pathlib import Path

import wsd.server
from wsd import lab, routes
from wsd.server import RouteError

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lab"
RECORDS = FIXTURES / "records"
MEASUREMENT_IDS = ["active_decode_tps", "effective_request_tps", "prefill_new_tps",
                   "ttft_mean_s", "queue_mean_s", "wall_seconds", "peak_ram_gb",
                   "peak_vram_gb", "total_energy_wh", "gate_pass_rate",
                   "hidden_pass_rate", "output_tokens", "compactions",
                   "peak_context"]


def wall_runs(values_by_variant: dict[str, list[int]]) -> list[dict]:
    """Minimal records for one task, only wall seconds, for the noise rule."""
    return [{"task": "T-001", "variant": variant, "run": index + 1, "wall_seconds": seconds}
            for variant, values in values_by_variant.items() for index, seconds in enumerate(values)]


class LoadRunsTest(unittest.TestCase):
    def test_every_record_in_file_order(self):
        runs = lab.load_runs(str(RECORDS))
        self.assertEqual(len(runs), 9)
        self.assertEqual([(run["task"], run["variant"], run["run"]) for run in runs],
                         [("T-001", "base", 1), ("T-001", "base", 2), ("T-002", "base", 1),
                          ("T-002", "base", 2), ("T-003", "base", 1), ("T-001", "mod", 1),
                          ("T-001", "mod", 2), ("T-002", "mod", 1), ("T-002", "mod", 2)])
        self.assertTrue(all(run["schema"] == 1 for run in runs))

    def test_empty_file(self):
        warnings: list[str] = []
        self.assertEqual(lab.load_runs(str(FIXTURES / "empty"), warnings), [])
        self.assertEqual(warnings, [])

    def test_missing_file_or_dir_or_config(self):
        self.assertEqual(lab.load_runs(str(FIXTURES / "nofile")), [])
        self.assertEqual(lab.load_runs("/nonexistent/records/dir"), [])
        self.assertEqual(lab.load_runs(None), [])
        self.assertEqual(lab.load_runs(""), [])

    def test_malformed_lines_are_skipped_and_counted(self):
        warnings: list[str] = []
        runs = lab.load_runs(str(FIXTURES / "malformed"), warnings)
        self.assertEqual([(run["task"], run["variant"]) for run in runs], [("T-002", "a"), ("T-001", "b")])
        self.assertEqual(warnings,
                         ["bench.jsonl line 2: not JSON, skipped",
                          "bench.jsonl line 3: is list, not an object, skipped",
                          "bench.jsonl line 4: no variant, skipped",
                          "bench.jsonl line 5: no task id, skipped"])

    def test_warnings_are_optional(self):
        self.assertEqual(len(lab.load_runs(str(FIXTURES / "malformed"))), 2)


class VariantsTest(unittest.TestCase):
    def test_variant_names_with_run_counts(self):
        self.assertEqual(lab.variants(lab.load_runs(str(RECORDS))),
                         [{"variant": "base", "runs": 5}, {"variant": "mod", "runs": 4}])

    def test_no_runs_no_variants(self):
        self.assertEqual(lab.variants([]), [])


class CompareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = lab.load_runs(str(RECORDS))
        cls.theirs = lab.compare(cls.runs, "base", "mod")
        cls.rows = {row["id"]: row for row in cls.theirs["measurements"]}

    def test_only_shared_tasks_are_compared(self):
        self.assertEqual(self.theirs["baseline"], "base")
        self.assertEqual(self.theirs["modified"], "mod")
        self.assertEqual(self.theirs["tasks_compared"], ["T-001", "T-002"])
        self.assertEqual(self.theirs["tasks_only_in_baseline"], ["T-003"])
        self.assertEqual(self.theirs["tasks_only_in_modified"], [])
        # the runs of the unmatched task still count as runs, just not as a comparison
        self.assertEqual(self.theirs["runs"], {"baseline": 5, "modified": 4})

    def test_every_measurement_is_reported_in_order(self):
        self.assertEqual([row["id"] for row in self.theirs["measurements"]], MEASUREMENT_IDS)
        self.assertEqual([(row["label"], row["unit"]) for row in self.theirs["measurements"]],
                         [("active generation tok/s", "tok/s"),
                          ("effective request tok/s", "tok/s"),
                          ("prefill new tok/s", "tok/s"), ("first-token latency", "s"),
                          ("queue waiting", "s"), ("wall seconds", "s"),
                          ("peak RAM", "GB"), ("peak VRAM", "GB"),
                          ("total energy", "Wh"), ("gate pass rate", "ratio"),
                          ("hidden-test pass rate", "ratio"), ("output tokens", "tokens"),
                          ("compactions", "count"), ("peak context", "tokens")])

    def test_gate_pass_rate_higher_is_better(self):
        row = self.rows["gate_pass_rate"]
        self.assertEqual(row["direction"], "higher")
        self.assertEqual(row["baseline"], {"n": 4, "mean": 0.75, "min": 0.0, "max": 1.0})
        self.assertEqual(row["modified"], {"n": 4, "mean": 1.0, "min": 1.0, "max": 1.0})
        self.assertEqual(row["difference"], {"absolute": 0.25, "percent": 33.33})
        self.assertEqual(row["better"], "modified")
        # 0.75 -> 1.0 across runs that swing the whole range: noise, not a win
        self.assertTrue(row["within_noise"])

    def test_runs_without_a_hidden_pass_rate_are_skipped(self):
        row = self.rows["hidden_pass_rate"]
        # base T-002 r2 has {"files": 0, "tests": 0, "note": ...} — no rate to mean
        self.assertEqual(row["baseline"]["n"], 3)
        self.assertAlmostEqual(row["baseline"]["mean"], 0.8333333333, places=6)
        self.assertEqual(row["modified"]["n"], 4)
        self.assertEqual(row["modified"]["mean"], 0.6)
        self.assertAlmostEqual(row["difference"]["absolute"], -0.2333333333, places=6)
        self.assertEqual(row["difference"]["percent"], -28.0)
        self.assertEqual(row["better"], "baseline")
        self.assertFalse(row["within_noise"])

    def test_wall_seconds_lower_is_better(self):
        row = self.rows["wall_seconds"]
        self.assertEqual(row["direction"], "lower")
        self.assertEqual(row["baseline"], {"n": 4, "mean": 110.0, "min": 100, "max": 120})
        self.assertEqual(row["modified"], {"n": 4, "mean": 89.0, "min": 86, "max": 92})
        self.assertEqual(row["difference"], {"absolute": -21.0, "percent": -19.09})
        self.assertEqual(row["better"], "modified")
        # 21 s off a mean, where the widest run-to-run spread is 20 s
        self.assertFalse(row["within_noise"])

    def test_run_without_energy_is_skipped_not_zero(self):
        row = self.rows["total_energy_wh"]
        self.assertEqual(row["baseline"]["n"], 3)
        self.assertAlmostEqual(row["baseline"]["mean"], 13.3333333333, places=6)
        self.assertEqual((row["baseline"]["min"], row["baseline"]["max"]), (12.0, 15.0))
        self.assertEqual(row["modified"]["mean"], 12.25)
        self.assertAlmostEqual(row["difference"]["percent"], -8.13, delta=0.02)
        self.assertEqual(row["better"], "modified")
        self.assertTrue(row["within_noise"])

    def test_more_output_tokens_is_worse(self):
        row = self.rows["output_tokens"]
        self.assertEqual(row["baseline"]["mean"], 1500.0)
        self.assertEqual(row["modified"]["mean"], 1535.0)
        self.assertEqual(row["difference"], {"absolute": 35.0, "percent": 2.33})
        self.assertEqual(row["better"], "baseline")
        self.assertTrue(row["within_noise"])

    def test_compactions_and_peak_context(self):
        self.assertEqual(self.rows["compactions"]["difference"], {"absolute": -1.5, "percent": -60.0})
        self.assertEqual(self.rows["compactions"]["better"], "modified")
        peak = self.rows["peak_context"]
        self.assertEqual((peak["baseline"]["mean"], peak["modified"]["mean"]), (40250.0, 36750.0))
        self.assertAlmostEqual(peak["difference"]["percent"], -8.7, delta=0.05)
        # 3500 tokens off, both variants swinging 4000: overlapping ranges
        self.assertTrue(peak["within_noise"])

    def test_active_generation_tok_s_skips_unavailable_engine(self):
        row = self.rows["active_decode_tps"]
        self.assertEqual(row["label"], "active generation tok/s")
        self.assertEqual(row["unit"], "tok/s")
        self.assertEqual(row["direction"], "higher")
        self.assertEqual(row["baseline"]["n"], 3)  # the available:false run is skipped
        self.assertEqual(row["baseline"]["mean"], 30.0)
        self.assertEqual(row["baseline"]["min"], 24.0)
        self.assertEqual(row["baseline"]["max"], 36.0)
        self.assertEqual(row["modified"]["n"], 4)
        self.assertEqual(row["modified"]["mean"], 39.75)
        self.assertEqual(row["difference"], {"absolute": 9.75, "percent": 32.5})
        self.assertEqual(row["better"], "modified")
        # 9.75 tok/s on runs that swing 12 tok/s themselves
        self.assertTrue(row["within_noise"])

    def test_effective_request_tok_s(self):
        row = self.rows["effective_request_tps"]
        self.assertEqual(row["label"], "effective request tok/s")
        self.assertEqual(row["direction"], "higher")
        self.assertEqual(row["baseline"]["mean"], 2.5)
        self.assertAlmostEqual(row["modified"]["mean"], 3.0, places=6)
        self.assertAlmostEqual(row["difference"]["absolute"], 0.5, places=6)
        self.assertEqual(row["difference"]["percent"], 20.0)
        self.assertEqual(row["better"], "modified")
        self.assertTrue(row["within_noise"])

    def test_prefill_new_tok_s(self):
        row = self.rows["prefill_new_tps"]
        self.assertEqual(row["label"], "prefill new tok/s")
        self.assertEqual(row["direction"], "higher")
        self.assertAlmostEqual(row["baseline"]["mean"], 1083.333333, places=6)
        self.assertEqual(row["modified"]["mean"], 1450.0)
        self.assertAlmostEqual(row["difference"]["absolute"], 366.666667, places=6)
        self.assertEqual(row["difference"]["percent"], 33.85)
        self.assertEqual(row["better"], "modified")
        self.assertFalse(row["within_noise"])

    def test_first_token_latency_lower_is_better(self):
        row = self.rows["ttft_mean_s"]
        self.assertEqual(row["label"], "first-token latency")
        self.assertEqual(row["unit"], "s")
        self.assertEqual(row["direction"], "lower")
        self.assertEqual(row["baseline"]["n"], 2)  # the null-field run is skipped
        self.assertAlmostEqual(row["baseline"]["mean"], 1.05, places=6)
        self.assertAlmostEqual(row["modified"]["mean"], 0.725, places=6)
        self.assertAlmostEqual(row["difference"]["absolute"], -0.325, places=6)
        self.assertEqual(row["difference"]["percent"], -30.95)
        self.assertEqual(row["better"], "modified")
        self.assertFalse(row["within_noise"])

    def test_queue_waiting(self):
        row = self.rows["queue_mean_s"]
        self.assertEqual(row["label"], "queue waiting")
        self.assertEqual(row["unit"], "s")
        self.assertEqual(row["direction"], "lower")
        self.assertAlmostEqual(row["baseline"]["mean"], 1.2, places=6)
        self.assertAlmostEqual(row["modified"]["mean"], 0.675, places=6)
        self.assertAlmostEqual(row["difference"]["absolute"], -0.525, places=6)
        self.assertEqual(row["difference"]["percent"], -43.75)
        self.assertEqual(row["better"], "modified")
        self.assertTrue(row["within_noise"])

    def test_peak_ram_bytes_converted_to_gb(self):
        row = self.rows["peak_ram_gb"]
        self.assertEqual(row["label"], "peak RAM")
        self.assertEqual(row["unit"], "GB")
        self.assertEqual(row["direction"], "lower")
        self.assertEqual(row["baseline"]["n"], 3)  # the empty energy block is skipped
        self.assertEqual(row["baseline"]["mean"], 24.0)  # 24/23/25 GiB, in bytes
        self.assertEqual((row["baseline"]["min"], row["baseline"]["max"]), (23.0, 25.0))
        self.assertEqual(row["modified"]["mean"], 21.5)
        self.assertEqual(row["difference"], {"absolute": -2.5, "percent": -10.42})
        self.assertEqual(row["better"], "modified")
        self.assertTrue(row["within_noise"])

    def test_peak_vram_bytes_converted_to_gb(self):
        row = self.rows["peak_vram_gb"]
        self.assertEqual(row["label"], "peak VRAM")
        self.assertEqual(row["unit"], "GB")
        self.assertEqual(row["direction"], "lower")
        self.assertEqual(row["baseline"]["mean"], 17.0)  # 16/17/18 GiB, in bytes
        self.assertEqual(row["modified"]["mean"], 13.75)
        self.assertEqual(row["difference"], {"absolute": -3.25, "percent": -19.12})
        self.assertEqual(row["better"], "modified")
        self.assertFalse(row["within_noise"])

    def test_every_row_is_scoped_to_one_run(self):
        for row in self.theirs["measurements"]:
            self.assertEqual(row["scope"], "run")

    def test_reversed_arguments(self):
        reversed_ = lab.compare(self.runs, "mod", "base")
        self.assertEqual(reversed_["tasks_only_in_modified"], ["T-003"])
        self.assertEqual(reversed_["tasks_only_in_baseline"], [])
        self.assertEqual(reversed_["runs"], {"baseline": 4, "modified": 5})
        gate = {row["id"]: row for row in reversed_["measurements"]}["gate_pass_rate"]
        self.assertEqual(gate["difference"], {"absolute": -0.25, "percent": -25.0})
        self.assertEqual(gate["better"], "baseline")

    def test_single_run_per_side_is_never_outside_the_noise(self):
        comparison = lab.compare(lab.load_runs(str(FIXTURES / "single")), "solo", "pair")
        rows = {row["id"]: row for row in comparison["measurements"]}
        self.assertEqual(comparison["tasks_compared"], ["T-001"])
        self.assertTrue(all(row["within_noise"] for row in comparison["measurements"]))
        self.assertEqual({row["baseline"]["n"] for row in comparison["measurements"]}, {1})
        self.assertEqual(rows["gate_pass_rate"]["better"], "baseline")
        self.assertEqual(rows["wall_seconds"]["better"], "baseline")
        # both variants sampled exactly 15 Wh: a difference of nothing is a tie
        self.assertEqual(rows["total_energy_wh"]["difference"], {"absolute": 0.0, "percent": 0.0})
        self.assertEqual(rows["total_energy_wh"]["better"], "tie")

    def test_nothing_measured_on_either_side_is_a_tie_with_no_numbers(self):
        comparison = lab.compare([{"task": "T-001", "variant": "a", "run": 1},
                                  {"task": "T-001", "variant": "b", "run": 1}], "a", "b")
        for row in comparison["measurements"]:
            self.assertEqual(row["baseline"], {"n": 0, "mean": None, "min": None, "max": None})
            self.assertEqual(row["difference"], {"absolute": None, "percent": None})
            self.assertEqual(row["better"], "tie")
            self.assertTrue(row["within_noise"])

    def test_a_difference_exactly_the_size_of_the_spread_is_not_noise(self):
        def wall(variants):
            rows = lab.compare(wall_runs(variants), "a", "b")["measurements"]
            return next(row for row in rows if row["id"] == "wall_seconds")

        equal = wall({"a": [10, 14], "b": [14, 18]})
        self.assertEqual(equal["difference"]["absolute"], 4.0)
        self.assertFalse(equal["within_noise"])
        smaller = wall({"a": [10, 14], "b": [14, 17]})
        self.assertEqual(smaller["difference"]["absolute"], 3.5)
        self.assertTrue(smaller["within_noise"])


class LabRoutesTest(unittest.TestCase):
    def setUp(self):
        original = routes.RECORDS_DIR
        self.addCleanup(setattr, routes, "RECORDS_DIR", original)

    def test_variants_payload(self):
        routes.RECORDS_DIR = str(RECORDS)
        payload = routes.lab_variants()
        self.assertEqual(payload["variants"], [{"variant": "base", "runs": 5}, {"variant": "mod", "runs": 4}])
        self.assertEqual(payload["runs"], 9)
        self.assertEqual(payload["warnings"], [])

    def test_compare_payload(self):
        routes.RECORDS_DIR = str(RECORDS)
        payload = routes.lab_compare("base/mod")
        self.assertEqual(payload["baseline"], "base")
        self.assertEqual(payload["modified"], "mod")
        self.assertEqual(payload["tasks_compared"], ["T-001", "T-002"])
        self.assertEqual([row["id"] for row in payload["measurements"]], MEASUREMENT_IDS)
        self.assertEqual(payload["warnings"], [])

    def test_unknown_variant_is_400(self):
        routes.RECORDS_DIR = str(RECORDS)
        with self.assertRaises(RouteError) as ctx:
            routes.lab_compare("base/nope")
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("nope", str(ctx.exception))
        self.assertIn("base, mod", str(ctx.exception))
        with self.assertRaises(RouteError):
            routes.lab_compare("nope/mod")

    def test_both_variants_are_required(self):
        routes.RECORDS_DIR = str(RECORDS)
        for tail in ("", "base", "base/"):
            with self.subTest(tail=tail), self.assertRaises(RouteError) as ctx:
                routes.lab_compare(tail)
            self.assertEqual(ctx.exception.status, 400)
            self.assertIn("/api/lab/compare/<baseline>/<modified>", str(ctx.exception))

    def test_variants_warn_when_there_is_no_records_dir(self):
        routes.RECORDS_DIR = None
        payload = routes.lab_variants()
        self.assertEqual(payload["variants"], [])
        self.assertIn("no lab runs to compare", " ".join(payload["warnings"]))

    def test_variants_warn_when_the_records_dir_is_gone(self):
        routes.RECORDS_DIR = "/nonexistent/records/dir"
        payload = routes.lab_variants()
        self.assertEqual(payload["variants"], [])
        self.assertIn("not found", " ".join(payload["warnings"]))

    def test_compare_without_records_is_400(self):
        routes.RECORDS_DIR = None
        with self.assertRaises(RouteError) as ctx:
            routes.lab_compare("base/mod")
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("no bench records", str(ctx.exception))

    def test_routes_are_registered(self):
        self.assertIn("/api/lab/variants", wsd.server.ROUTES)
        self.assertIn("/api/lab/compare/", wsd.server.PREFIX_ROUTES)


if __name__ == "__main__":
    unittest.main()

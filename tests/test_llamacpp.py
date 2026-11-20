"""llama.cpp adapter tests: fixture maths, states, redaction, measured rates.

Token semantics asserted here were confirmed live on the llama-server at
127.0.0.1:8001 (T-019 step 4): sending the same 310-token prompt twice moved
``llamacpp:prompt_tokens_total`` by +310 on the first call and +1 on the
second (response timings ``prompt_n: 1, cache_n: 309``) while
``llamacpp:prompt_tokens_cached_total`` moved +0 then +309. Processed and
cached are therefore disjoint and prompt_total = processed + cached is the
engine-visible prompt volume.
"""

import json
import unittest
import urllib.error
from pathlib import Path

from wsd import routes  # noqa: F401  (registers ROUTES)
from wsd.config import ENGINE_TYPES
from wsd.inference import build_engines
from wsd.inference.llamacpp import LlamaCppAdapter

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llamacpp"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


HEALTH = fixture("health_before.txt")
PROPS = fixture("props.json")
SLOTS = fixture("slots_before.json")
METRICS = fixture("metrics_before.txt")

MINIMAL_PROPS = json.dumps(
    {
        "build_info": "b1-test",
        "model_alias": "tiny",
        "model_path": "/srv/models/tinyllama-q4.gguf",
        "total_slots": 2,
        "default_generation_settings": {"n_ctx": 2048},
    }
)

METRICS_A = (
    "llamacpp:prompt_tokens_total 1000\n"
    "llamacpp:prompt_tokens_cached_total 500\n"
    "llamacpp:prompt_seconds_total 1\n"
    "llamacpp:tokens_predicted_total 200\n"
    "llamacpp:tokens_predicted_seconds_total 4\n"
    "llamacpp:spec_decode_num_draft_tokens_total 0\n"
    "llamacpp:spec_decode_num_accepted_tokens_total 0\n"
    "llamacpp:spec_decode_num_drafts_total 0\n"
    "llamacpp:requests_processing 1\n"
    "llamacpp:requests_deferred 2\n"
)

METRICS_B = METRICS_A.replace("prompt_tokens_total 1000", "prompt_tokens_total 1500").replace(
    "tokens_predicted_total 200", "tokens_predicted_total 400"
)


def fake_fetch(overrides=None, failures=None):
    data = {"/health": HEALTH, "/props": PROPS, "/slots": SLOTS, "/metrics": METRICS}
    data.update(overrides or {})
    failures = failures or {}

    def fetch(path):
        if path in failures:
            raise failures[path]
        return data[path]

    return fetch


def adapter(fetch=None, clock=None):
    return LlamaCppAdapter(
        "lc",
        "L",
        "http://127.0.0.1:8001",
        fetch=fetch or fake_fetch(),
        clock=clock or iter([100.0]).__next__,
    )


class FixtureSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.snap = adapter().snapshot()

    def test_ready_state_and_identity(self):
        self.assertEqual(self.snap["status"], "ready")
        self.assertEqual(self.snap["type"], "llama.cpp")
        self.assertEqual(self.snap["version"], "b1-edec462")
        self.assertEqual(self.snap["model"]["id"], "gpt-oss-20b-MXFP4")
        self.assertEqual(self.snap["model"]["state"], "resident")
        self.assertEqual(self.snap["model"]["max_model_len"], 32768)

    def test_model_file_is_basename_only(self):
        self.assertEqual(self.snap["model"]["file"], "gpt-oss-20b-MXFP4.gguf")

    def test_no_absolute_paths_leak_into_the_snapshot(self):
        blob = json.dumps(self.snap)
        self.assertNotIn("/mnt", blob)
        self.assertNotIn("model_path", blob)
        self.assertNotIn("/home", blob)

    def test_prompt_total_is_processed_plus_cached(self):
        # metrics_before.txt: 39897 processed + 15408 cached (disjoint, live-proved).
        self.assertEqual(self.snap["tokens"]["prompt_total"], 39897 + 15408)
        self.assertEqual(self.snap["tokens"]["generation_total"], 48106)
        self.assertEqual(self.snap["tokens"]["cached_total"], 15408)
        self.assertIsNone(self.snap["tokens"]["prefill_total"])
        self.assertIsNone(self.snap["tokens"]["decode_total"])

    def test_cache_share(self):
        self.assertAlmostEqual(
            self.snap["cache"]["prefix_hit_rate"], 15408 / (39897 + 15408)
        )

    def test_lifetime_levels(self):
        life = self.snap["throughput_levels"]["lifetime"]
        self.assertAlmostEqual(life["active_decode_tps"], 48106 / 600.983)
        self.assertAlmostEqual(life["prefill_new_tps"], 39897 / 10.5044)
        self.assertEqual(life["prompt_tokens"], 39897 + 15408)
        self.assertEqual(life["new_prompt_tokens"], 39897)
        self.assertEqual(life["cached_prompt_tokens"], 15408)
        self.assertIsNone(life["queue_share"])
        self.assertIsNone(life["effective_request_tps"])
        self.assertIsNone(life["requests"])
        self.assertIsNone(self.snap["throughput_levels"]["window"])

    def test_queue_from_gauges_and_props(self):
        queue = self.snap["queue"]
        self.assertEqual(queue["running"], 0)
        self.assertEqual(queue["waiting"], 0)
        self.assertEqual(queue["max_num_seqs"], 1)
        self.assertIsNone(queue["in_flight"])
        self.assertIsNone(queue["queue_cap"])
        self.assertIsNone(queue["preemptions"])

    def test_deferred_requests_warn(self):
        snap = adapter(fetch=fake_fetch(overrides={"/metrics": METRICS_A})).snapshot()
        self.assertEqual(snap["queue"]["waiting"], 2)
        self.assertIn("2 request(s) deferred (all slots busy)", snap["warnings"])

    def test_requests_from_idle_slot_fixture(self):
        (row,) = self.snap["requests"]
        self.assertEqual(row["id"], 43344)
        self.assertEqual(row["slot"], 0)
        self.assertEqual(row["state"], "idle")
        self.assertEqual(row["prompt"], 8852)
        self.assertEqual(row["output"], 0)
        self.assertEqual(row["ctx"], 8852)
        self.assertAlmostEqual(row["ctx_pct"], 8852 / 32768 * 100, places=1)

    def test_requests_row_next_token_as_dict(self):
        slots = json.dumps(
            [
                {
                    "id": 1,
                    "id_task": 7,
                    "is_processing": True,
                    "n_ctx": 100,
                    "n_prompt_tokens": 60,
                    "n_prompt_tokens_processed": 40,
                    "n_prompt_tokens_cache": 20,
                    "next_token": {"n_decoded": 5},
                }
            ]
        )
        snap = adapter(fetch=fake_fetch(overrides={"/slots": slots})).snapshot()
        (row,) = snap["requests"]
        self.assertEqual(row["state"], "processing")
        self.assertEqual(row["processed"], 40)
        self.assertEqual(row["cached"], 20)
        self.assertEqual(row["output"], 5)
        self.assertEqual(row["ctx"], 65)
        self.assertEqual(row["ctx_pct"], 65.0)

    def test_requests_idle_empty_slot_ctx_pct_zero(self):
        slots = json.dumps(
            [
                {
                    "id": 2,
                    "id_task": -2,
                    "is_processing": False,
                    "n_ctx": 4096,
                    "n_prompt_tokens": 0,
                    "n_prompt_tokens_processed": 0,
                    "n_prompt_tokens_cache": 0,
                    "next_token": {"n_decoded": 0},
                }
            ]
        )
        snap = adapter(fetch=fake_fetch(overrides={"/slots": slots})).snapshot()
        (row,) = snap["requests"]
        self.assertEqual(row["state"], "idle")
        self.assertEqual(row["ctx"], 0)
        self.assertEqual(row["ctx_pct"], 0.0)

    def test_speculative_none_without_drafts(self):
        self.assertIsNone(self.snap["speculative"]["draft_accept"])
        self.assertFalse(self.snap["capabilities"]["speculative"])

    def test_speculative_acceptance_with_drafts(self):
        metrics = METRICS_A + (
            "llamacpp:spec_decode_num_draft_tokens_total 100\n"
            "llamacpp:spec_decode_num_accepted_tokens_total 40\n"
            "llamacpp:spec_decode_num_drafts_total 10\n"
        )
        snap = adapter(fetch=fake_fetch(overrides={"/metrics": metrics})).snapshot()
        self.assertAlmostEqual(snap["speculative"]["draft_accept"], 0.4)
        self.assertTrue(snap["capabilities"]["speculative"])

    def test_static_capabilities(self):
        caps = self.snap["capabilities"]
        self.assertTrue(caps["per_request"])
        self.assertTrue(caps["throughput_levels"])
        self.assertTrue(caps["prefix_cache"])
        for key in ("latency_histograms", "experts", "transfers", "lifecycle", "controls"):
            self.assertFalse(caps[key], key)


class StateTest(unittest.TestCase):
    def test_health_503_is_loading(self):
        fetch = fake_fetch(failures={"/health": urllib.error.HTTPError("u", 503, "x", None, None)})
        snap = adapter(fetch=fetch).snapshot()
        self.assertEqual(snap["status"], "loading")
        self.assertIsNone(snap["model"]["state"])

    def test_health_loading_body_is_loading(self):
        snap = adapter(fetch=fake_fetch(overrides={"/health": '{"status":"loading"}'})).snapshot()
        self.assertEqual(snap["status"], "loading")

    def test_plain_ok_health_body_is_ready(self):
        snap = adapter(fetch=fake_fetch(overrides={"/health": "ok"})).snapshot()
        self.assertEqual(snap["status"], "ready")

    def test_down_when_unreachable(self):
        def refused(path):
            raise ConnectionRefusedError("connection refused")

        snap = adapter(fetch=refused).snapshot()
        self.assertEqual(snap["status"], "down")
        self.assertIsNone(snap["model"]["id"])
        self.assertIsNone(snap["tokens"]["prompt_total"])
        self.assertIsNone(snap["throughput"]["decode_tps"])
        self.assertEqual(snap["requests"], [])
        self.assertEqual(len(snap["warnings"]), 4)

    def test_broken_endpoint_reports_error(self):
        snap = adapter(fetch=fake_fetch(overrides={"/props": "not json"})).snapshot()
        self.assertEqual(snap["status"], "error")
        self.assertIn("/props failed", " ".join(snap["warnings"]))

    def test_unknown_health_status_is_error(self):
        snap = adapter(fetch=fake_fetch(overrides={"/health": "how are you"})).snapshot()
        self.assertEqual(snap["status"], "error")


class MeasuredRateTest(unittest.TestCase):
    def two_snapshots(self, metrics2, times):
        calls = {"n": 0}

        def fetch(path):
            if path == "/metrics":
                calls["n"] += 1
                return METRICS_A if calls["n"] == 1 else metrics2
            if path == "/props":
                return MINIMAL_PROPS
            if path == "/slots":
                return "[]"
            return HEALTH

        a = LlamaCppAdapter("lc", "L", fetch=fetch, clock=iter(times).__next__)
        a.snapshot()
        return a, a.snapshot()

    def test_counter_deltas_over_two_snapshots(self):
        _, snap = self.two_snapshots(METRICS_B, [100.0, 102.0])
        thr = snap["throughput"]
        self.assertAlmostEqual(thr["decode_tps"], (400 - 200) / 2.0)
        self.assertAlmostEqual(thr["prefill_tps"], (1500 - 1000) / 2.0)
        self.assertAlmostEqual(thr["last_decode_tps"], 100.0)
        self.assertIsNone(thr["decode_tps_gauge"])  # lifetime avg, not a gauge

    def test_short_window_rejected_and_baseline_kept(self):
        a, snap = self.two_snapshots(METRICS_B, [100.0, 100.2])
        self.assertIsNone(snap["throughput"]["decode_tps"])
        self.assertEqual(a._prev["t"], 100.0)  # baseline not clobbered

    def test_idle_window_yields_none_and_keeps_last(self):
        _, snap = self.two_snapshots(METRICS_A, [100.0, 102.0])
        self.assertIsNone(snap["throughput"]["decode_tps"])
        self.assertIsNone(snap["throughput"]["last_decode_tps"])

    def test_counter_decrease_resets_and_clears_last(self):
        def fetch(path):
            if path == "/metrics":
                return METRICS_B
            if path == "/props":
                return MINIMAL_PROPS
            if path == "/slots":
                return "[]"
            return HEALTH

        a = LlamaCppAdapter("lc", "L", fetch=fetch, clock=iter([100.0, 102.0, 104.0]).__next__)
        a.snapshot()  # seeds prev with METRICS_B counters
        a._prev = {"t": 102.0, "prefill_total": 99999.0, "decode_total": 99999.0}
        snap = a.snapshot()  # counters dropped -> restart, void everything
        thr = snap["throughput"]
        self.assertIsNone(thr["decode_tps"])
        self.assertIsNone(thr["prefill_tps"])
        self.assertIsNone(thr["last_decode_tps"])
        self.assertIsNone(thr["last_prefill_tps"])


class DivisionGuardTest(unittest.TestCase):
    def test_zero_denominators_give_none(self):
        metrics = (
            "llamacpp:prompt_tokens_total 0\n"
            "llamacpp:prompt_tokens_cached_total 0\n"
            "llamacpp:prompt_seconds_total 0\n"
            "llamacpp:tokens_predicted_total 0\n"
            "llamacpp:tokens_predicted_seconds_total 0\n"
        )
        snap = adapter(fetch=fake_fetch(overrides={"/metrics": metrics})).snapshot()
        self.assertIsNone(snap["cache"]["prefix_hit_rate"])
        life = snap["throughput_levels"]["lifetime"]
        self.assertIsNone(life["active_decode_tps"])
        self.assertIsNone(life["prefill_new_tps"])
        self.assertIsNone(life["cache_share"])

    def test_missing_metrics_give_none(self):
        snap = adapter(fetch=fake_fetch(overrides={"/metrics": ""})).snapshot()
        self.assertIsNone(snap["tokens"]["prompt_total"])
        self.assertIsNone(snap["cache"]["prefix_hit_rate"])
        self.assertIsNone(snap["speculative"]["draft_accept"])


class RegistryTest(unittest.TestCase):
    def test_llama_cpp_is_a_known_engine_type(self):
        self.assertIn("llama.cpp", ENGINE_TYPES)

    def test_build_engines_instantiates_the_adapter(self):
        engines, warnings = build_engines(
            {
                "engines": [
                    {"id": "lc", "type": "llama.cpp", "label": "Local", "base_url": "http://127.0.0.1:9"}
                ],
                "warnings": [],
            }
        )
        self.assertEqual(warnings, [])
        self.assertIsInstance(engines["lc"], LlamaCppAdapter)


if __name__ == "__main__":
    unittest.main()

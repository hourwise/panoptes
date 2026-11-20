"""Engine registry and the OpenAI-compatible adapter (fixtures in tests/fixtures/engines)."""

import json
import unittest
import urllib.error
from pathlib import Path

from wsd.inference import TYPES, build_engines
from wsd.inference.base import CAPABILITIES, LATENCY_STATS, SCHEMA
from wsd.inference.openai_compat import OpenAICompatAdapter
from wsd.inference.radiance import RadianceAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "engines"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def vllm_fetch(path: str) -> str:
    return {
        "/v1/models": fixture("vllm_models.json"),
        "/health": "",  # vLLM answers 200 with an empty body
        "/metrics": fixture("vllm_metrics.txt"),
    }[path]


def plain_fetch(path: str) -> str:
    """llama.cpp-style server: models but no /metrics endpoint."""
    if path == "/metrics":
        raise urllib.error.HTTPError("http://x/metrics", 404, "Not Found", None, None)
    if path == "/health":
        return ""
    return fixture("plain_models.json")


def down_fetch(path: str) -> str:
    raise ConnectionRefusedError("connection refused")


def spec(engine_id, engine_type):
    return {"id": engine_id, "type": engine_type, "base_url": "http://127.0.0.1:8000", "label": engine_id}


class BuildEnginesTest(unittest.TestCase):
    def test_builds_both_types_from_config(self):
        engines, warnings = build_engines({"engines": [spec("rad", "radiance"), spec("ocr", "openai-compatible")]})
        self.assertEqual(warnings, [])
        self.assertEqual(list(engines), ["rad", "ocr"])
        self.assertIsInstance(engines["rad"], RadianceAdapter)
        self.assertIsInstance(engines["ocr"], OpenAICompatAdapter)
        self.assertEqual(engines["rad"].base_url, "http://127.0.0.1:8000")

    def test_label_defaults_to_id(self):
        engines, _ = build_engines({"engines": [{"id": "a", "type": "radiance", "base_url": "http://127.0.0.1:1"}]})
        self.assertEqual(engines["a"].label, "a")

    def test_unknown_type_skipped_with_warning(self):
        engines, warnings = build_engines({"engines": [spec("ok", "radiance"), spec("no", "ollama")]})
        self.assertEqual(list(engines), ["ok"])
        self.assertIn("ollama", warnings[0])

    def test_duplicate_id_skipped_with_warning(self):
        engines, warnings = build_engines({"engines": [spec("dup", "radiance"), spec("dup", "openai-compatible")]})
        self.assertEqual(list(engines), ["dup"])
        self.assertIn("duplicate", warnings[0])

    def test_types_registry_matches_known_types(self):
        from wsd.config import ENGINE_TYPES

        self.assertEqual(sorted(TYPES), sorted(ENGINE_TYPES))

    def test_default_config_builds_all_adapters(self):
        from wsd.inference import ENGINES, WARNINGS, build_engines
        from wsd.inference.base import EngineAdapter

        self.assertEqual(WARNINGS, [])
        self.assertIn("radiance-r9700", ENGINES)
        self.assertIsInstance(ENGINES["radiance-r9700"], RadianceAdapter)
        for engine_id, adapter in ENGINES.items():
            self.assertIsInstance(adapter, EngineAdapter, engine_id)
        self.assertEqual(list(build_engines()[0]), list(ENGINES))


class CommonSchemaTest(unittest.TestCase):
    """Every adapter's snapshot carries the same documented top-level keys."""

    EXPECTED = {
        "id", "type", "label", "status", "version", "model", "queue", "throughput",
        "throughput_levels", "latency", "tokens", "cache", "requests", "speculative",
        "transfers", "experts", "warnings", "capabilities",
        "uptime_s", "startup_s",
    }

    def keys(self, snapshot):
        return set(snapshot)

    def test_openai_compat_schema(self):
        s = OpenAICompatAdapter("v", "V", "http://127.0.0.1:8000", fetch=vllm_fetch).snapshot()
        self.assertEqual(self.keys(s), self.EXPECTED)
        self.assertEqual(list(s["capabilities"]), list(CAPABILITIES))
        json.dumps(s)

    def test_radiance_schema(self):
        infl = Path(__file__).parent / "fixtures" / "inference"
        data = {
            "/health": (infl / "health.json").read_text(),
            "/v1/models": (infl / "models.json").read_text(),
            "/stats": (infl / "stats.json").read_text(),
            "/metrics": (infl / "metrics.txt").read_text(),
        }
        s = RadianceAdapter(engine_id="r", label="R", fetch=data.__getitem__).snapshot()
        self.assertEqual(self.keys(s), self.EXPECTED | {"engine"})
        self.assertEqual(s["capabilities"], {k: k not in ("lifecycle", "controls") for k in CAPABILITIES})
        self.assertEqual((s["id"], s["type"], s["label"]), ("r", "radiance", "R"))
        self.assertEqual(s["engine"], "radiance")


class OpenAICompatVllmTest(unittest.TestCase):
    def setUp(self):
        self.s = OpenAICompatAdapter("vllm", "Local vLLM", "http://127.0.0.1:8000", fetch=vllm_fetch).snapshot()

    def test_status_and_model(self):
        self.assertEqual(self.s["status"], "ready")
        self.assertEqual(self.s["model"]["id"], "Meta-Llama-3.1-8B-Instruct")
        self.assertEqual(self.s["model"]["max_model_len"], 131072)
        self.assertEqual(self.s["model"]["state"], "resident")

    def test_queue_and_tokens(self):
        self.assertEqual(self.s["queue"]["running"], 2)
        self.assertEqual(self.s["queue"]["waiting"], 1)
        self.assertEqual(self.s["tokens"]["prompt_total"], 500000)
        self.assertEqual(self.s["tokens"]["generation_total"], 123456)

    def test_latency_histograms_use_the_schema_keys(self):
        self.assertEqual(list(self.s["latency"]), list(SCHEMA["latency"]))
        ttft = self.s["latency"]["ttft"]
        self.assertEqual(ttft["count"], 50)
        self.assertAlmostEqual(ttft["sum"], 25.5)
        self.assertEqual(set(ttft), set(LATENCY_STATS))
        self.assertIsNotNone(self.s["latency"]["e2e"])
        self.assertIsNone(self.s["latency"]["queue"])  # the fixture's count is 0
        self.assertIsNone(self.s["latency"]["itl"])  # the fixture reports no itl

    def test_prefix_cache_rate(self):
        self.assertAlmostEqual(self.s["cache"]["prefix_hit_rate"], 0.9)

    def test_capabilities(self):
        caps = self.s["capabilities"]
        self.assertTrue(caps["latency_histograms"])
        self.assertTrue(caps["prefix_cache"])
        self.assertFalse(caps["per_request"])
        self.assertFalse(caps["speculative"])
        self.assertFalse(caps["experts"])
        self.assertFalse(caps["transfers"])
        self.assertFalse(caps["throughput_levels"])
        self.assertFalse(caps["lifecycle"])
        self.assertFalse(caps["controls"])

    def test_no_warnings_on_healthy_engine(self):
        self.assertEqual(self.s["warnings"], [])


class OpenAICompatNoMetricsTest(unittest.TestCase):
    def setUp(self):
        self.s = OpenAICompatAdapter("plain", "", "http://127.0.0.1:8080", fetch=plain_fetch).snapshot()

    def test_still_ready_with_model(self):
        self.assertEqual(self.s["status"], "ready")
        self.assertEqual(self.s["model"]["id"], "gemma-3-12b-it")

    def test_metric_sections_are_all_none(self):
        self.assertEqual(self.s["latency"], {key: None for key in SCHEMA["latency"]})
        self.assertIsNone(self.s["queue"]["running"])
        self.assertIsNone(self.s["cache"]["prefix_hit_rate"])

    def test_capabilities_all_false(self):
        self.assertFalse(any(self.s["capabilities"].values()))

    def test_label_falls_back_to_id(self):
        self.assertEqual(self.s["label"], "plain")

    def test_warns_about_missing_metrics(self):
        self.assertIn("/metrics", "\n".join(self.s["warnings"]))


class OpenAICompatDownTest(unittest.TestCase):
    def test_down_status_and_valid_schema(self):
        s = OpenAICompatAdapter("gone", "Gone", "http://127.0.0.1:9", fetch=down_fetch).snapshot()
        self.assertEqual(s["status"], "down")
        self.assertIsNone(s["model"]["id"])
        self.assertEqual(len(s["warnings"]), 3)  # all three endpoints unreachable
        json.dumps(s)


class OpenAICompatLoadingTest(unittest.TestCase):
    def test_no_models_yet_is_loading(self):
        def fetch(path):
            if path == "/v1/models":
                return json.dumps({"object": "list", "data": []})
            return ""

        s = OpenAICompatAdapter("boot", "", "http://127.0.0.1:8000", fetch=fetch).snapshot()
        self.assertEqual(s["status"], "loading")
        self.assertIsNone(s["model"]["state"])


class OpenAICompatItlNameTest(unittest.TestCase):
    """itl comes from inter_token_latency_seconds, else the older per-token name."""

    @staticmethod
    def _hist(name: str, mean: float) -> str:
        return (
            f'{name}_bucket{{le="0.1"}} 10\n'
            f'{name}_bucket{{le="+Inf"}} 10\n'
            f"{name}_sum {mean * 10}\n"
            f"{name}_count 10\n"
        )

    def snapshot(self, metrics: str) -> dict:
        def fetch(path):
            if path == "/metrics":
                return metrics
            if path == "/health":
                return ""
            return fixture("vllm_models.json")

        return OpenAICompatAdapter("v", "", "http://127.0.0.1:8000", fetch=fetch).snapshot()

    def test_inter_token_latency_wins_when_both_are_reported(self):
        s = self.snapshot(
            self._hist("vllm:inter_token_latency_seconds", 0.02)
            + self._hist("vllm:time_per_output_token_seconds", 0.05)
        )
        self.assertAlmostEqual(s["latency"]["itl"]["mean"], 0.02)

    def test_time_per_output_token_is_the_fallback(self):
        s = self.snapshot(self._hist("vllm:time_per_output_token_seconds", 0.05))
        self.assertAlmostEqual(s["latency"]["itl"]["mean"], 0.05)

    def test_absent_itl_leaves_the_key_none(self):
        s = self.snapshot(self._hist("vllm:time_to_first_token_seconds", 0.4))
        self.assertIsNone(s["latency"]["itl"])
        self.assertIsNotNone(s["latency"]["ttft"])
        self.assertTrue(s["capabilities"]["latency_histograms"])


if __name__ == "__main__":
    unittest.main()

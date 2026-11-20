"""One snapshot schema for every engine adapter.

The T-017 review caught ``OpenAICompatAdapter`` returning its latency section
under vLLM's raw histogram names (``time_to_first_token``,
``e2e_request_latency``, ...) while ``RadianceAdapter`` and the UI use
``ttft`` / ``e2e`` / ``queue`` / ``itl``. With the generic engine selected the
Latency card's capability read ``true`` and the card rendered nothing, because
the common schema in ``base.py`` had never fixed the key names.

``base.SCHEMA`` fixes them; this test holds every adapter to it in each state an
engine can be in — metrics present, no ``/metrics`` endpoint, engine down.
"""

import json
import unittest
import urllib.error
from pathlib import Path

from wsd.inference.base import LATENCY_STATS, SCHEMA
from wsd.inference.llamacpp import LlamaCppAdapter
from wsd.inference.openai_compat import OpenAICompatAdapter
from wsd.inference.radiance import RadianceAdapter

ENGINE_FIXTURES = Path(__file__).parent / "fixtures" / "engines"
INFERENCE_FIXTURES = Path(__file__).parent / "fixtures" / "inference"
LLAMACPP_FIXTURES = Path(__file__).parent / "fixtures" / "llamacpp"


def vllm_fetch(path: str) -> str:
    return {
        "/v1/models": (ENGINE_FIXTURES / "vllm_models.json").read_text(),
        "/health": "",  # vLLM answers 200 with an empty body
        "/metrics": (ENGINE_FIXTURES / "vllm_metrics.txt").read_text(),
    }[path]


def plain_fetch(path: str) -> str:
    """llama.cpp-style server: serves models, has no /metrics endpoint."""
    if path == "/metrics":
        raise urllib.error.HTTPError("http://x/metrics", 404, "Not Found", None, None)
    if path == "/health":
        return ""
    return (ENGINE_FIXTURES / "plain_models.json").read_text()


def radiance_fetch(path: str) -> str:
    return (INFERENCE_FIXTURES / _RADIANCE_FILES[path]).read_text()


_RADIANCE_FILES = {
    "/health": "health.json",
    "/v1/models": "models.json",
    "/stats": "stats.json",
    "/metrics": "metrics.txt",
}

_LLAMACPP_FILES = {
    "/health": "health_before.txt",
    "/props": "props.json",
    "/slots": "slots_before.json",
    "/metrics": "metrics_before.txt",
}


def llamacpp_fetch(path: str) -> str:
    return (LLAMACPP_FIXTURES / _LLAMACPP_FILES[path]).read_text()


def llamacpp_loading_fetch(path: str) -> str:
    """llama-server during model load: /health answers 503, the rest serve."""
    if path == "/health":
        raise urllib.error.HTTPError(
            "http://127.0.0.1:8001/health", 503, "Service Unavailable", None, None
        )
    return llamacpp_fetch(path)


def down_fetch(path: str) -> str:
    raise ConnectionRefusedError("connection refused")


class SchemaContract:
    """Mixin: the snapshot under test must match base.SCHEMA exactly."""

    adapter_class = None
    fetch = staticmethod(down_fetch)

    def setUp(self):
        adapter = self.adapter_class("contract", "Contract", "http://127.0.0.1:8000", fetch=self.fetch)
        self.snapshot = adapter.snapshot()

    def test_every_section_has_exactly_the_schema_keys(self):
        for section, keys in SCHEMA.items():
            with self.subTest(section=section):
                self.assertIn(section, self.snapshot)
                value = self.snapshot[section]
                self.assertIsInstance(value, dict, f"{section} is not a dict")
                self.assertEqual(set(value), set(keys))
                self.assertEqual(list(value), list(keys))

    def test_latency_values_are_histograms_or_none(self):
        for key, hist in self.snapshot["latency"].items():
            with self.subTest(latency=key):
                if hist is not None:
                    self.assertEqual(set(hist), set(LATENCY_STATS))
                    self.assertIsInstance(hist["count"], (int, float))

    def test_sections_are_json_serialisable(self):
        for section in SCHEMA:
            json.dumps(self.snapshot[section])


class RadianceMetricsContract(SchemaContract, unittest.TestCase):
    adapter_class = RadianceAdapter
    fetch = staticmethod(radiance_fetch)

    def test_latency_is_filled_under_the_schema_names(self):
        lat = self.snapshot["latency"]
        self.assertIsNotNone(lat["ttft"])
        self.assertIsNotNone(lat["itl"])

    def test_counters_are_filled(self):
        self.assertIsNotNone(self.snapshot["tokens"]["prompt_total"])


class RadianceDownContract(SchemaContract, unittest.TestCase):
    adapter_class = RadianceAdapter
    fetch = staticmethod(down_fetch)

    def test_down_snapshot_is_all_none_but_complete(self):
        self.assertEqual(self.snapshot["status"], "down")
        for section, keys in SCHEMA.items():
            with self.subTest(section=section):
                self.assertEqual(self.snapshot[section], {key: None for key in keys})


class OpenAICompatVllmContract(SchemaContract, unittest.TestCase):
    adapter_class = OpenAICompatAdapter
    fetch = staticmethod(vllm_fetch)

    def test_latency_is_filled_under_the_schema_names(self):
        lat = self.snapshot["latency"]
        self.assertIsNotNone(lat["ttft"])
        self.assertIsNotNone(lat["e2e"])
        self.assertIsNone(lat["queue"])  # the fixture's queue histogram has count 0

    def test_the_engine_names_are_gone(self):
        for raw in ("time_to_first_token", "e2e_request_latency", "time_per_output_token"):
            self.assertNotIn(raw, self.snapshot["latency"])

    def test_counters_are_filled(self):
        self.assertEqual(self.snapshot["tokens"]["prompt_total"], 500000)
        self.assertEqual(self.snapshot["queue"]["running"], 2)


class OpenAICompatNoMetricsContract(SchemaContract, unittest.TestCase):
    adapter_class = OpenAICompatAdapter
    fetch = staticmethod(plain_fetch)

    def test_ready_engine_still_carries_every_key(self):
        self.assertEqual(self.snapshot["status"], "ready")
        self.assertFalse(self.snapshot["capabilities"]["latency_histograms"])
        for section, keys in SCHEMA.items():
            with self.subTest(section=section):
                self.assertEqual(self.snapshot[section], {key: None for key in keys})


class OpenAICompatDownContract(SchemaContract, unittest.TestCase):
    adapter_class = OpenAICompatAdapter
    fetch = staticmethod(down_fetch)

    def test_down_snapshot_is_all_none_but_complete(self):
        self.assertEqual(self.snapshot["status"], "down")
        for section, keys in SCHEMA.items():
            with self.subTest(section=section):
                self.assertEqual(self.snapshot[section], {key: None for key in keys})


class LlamaCppMetricsContract(SchemaContract, unittest.TestCase):
    adapter_class = LlamaCppAdapter
    fetch = staticmethod(llamacpp_fetch)

    def test_counters_are_filled(self):
        self.assertIsNotNone(self.snapshot["tokens"]["prompt_total"])
        self.assertIsNotNone(self.snapshot["cache"]["prefix_hit_rate"])

    def test_latency_is_all_none_without_histograms(self):
        for key, hist in self.snapshot["latency"].items():
            with self.subTest(latency=key):
                self.assertIsNone(hist)

    def test_model_file_is_a_bare_basename(self):
        self.assertNotIn("/", self.snapshot["model"]["file"])


class LlamaCppLoadingContract(SchemaContract, unittest.TestCase):
    adapter_class = LlamaCppAdapter
    fetch = staticmethod(llamacpp_loading_fetch)

    def test_health_503_means_loading_not_error(self):
        self.assertEqual(self.snapshot["status"], "loading")
        self.assertEqual(self.snapshot["warnings"], [])


class LlamaCppDownContract(SchemaContract, unittest.TestCase):
    adapter_class = LlamaCppAdapter

    def test_down_snapshot_keeps_every_section_with_none_values(self):
        self.assertEqual(self.snapshot["status"], "down")
        for section, keys in SCHEMA.items():
            with self.subTest(section=section):
                self.assertEqual(self.snapshot[section], {key: None for key in keys})


if __name__ == "__main__":
    unittest.main()

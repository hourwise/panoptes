"""Generic OpenAI-compatible engine adapter (vLLM and friends).

Read-only GETs: ``/v1/models`` decides reachability and the model id,
``/health`` is used when present (vLLM answers 200 with an empty body),
``/metrics`` is optional Prometheus text parsed with
:mod:`wsd.inference.prometheus`. A server without ``/metrics`` still yields a
status and a model; the capability flags report exactly which sections the
last snapshot's ``/metrics`` could fill.
"""

import json
import time
import urllib.error
import urllib.request

from . import prometheus
from .base import EngineAdapter, empty_capabilities, empty_section

_TIMEOUT_S = 2.0

# Rolling token rates have no /metrics equivalent, so this adapter leaves every
# throughput key None; the other shared sections come from ``base.SCHEMA``.
_THROUGHPUT_KEYS = (
    "decode_tps_gauge",
    "prefill_tps_gauge",
    "decode_tps",
    "prefill_tps",
    "last_decode_tps",
    "last_prefill_tps",
    "last_at",
)

# vLLM's histogram families, named after the measurement the UI shows. Radiance
# publishes the same vllm:* names, so both adapters fill the same latency keys.
_LATENCY_HISTOGRAMS = {
    "ttft": "vllm:time_to_first_token_seconds",
    "e2e": "vllm:e2e_request_latency_seconds",
    "queue": "vllm:request_queue_time_seconds",
}
# Inter-token latency, newest name first: engines that predate the rename only
# expose time_per_output_token_seconds.
_ITL_METRICS = ("vllm:inter_token_latency_seconds", "vllm:time_per_output_token_seconds")


def _looks_down(exc: BaseException) -> bool:
    """True for connection refused / timeouts, False for HTTP or data errors."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, OSError)
    return isinstance(exc, OSError)


def _counter_total(samples, name):
    entries = samples.get(name)
    if not entries:
        return None
    return sum(value for _labels, value in entries)


class _Failed:
    __slots__ = ("down",)

    def __init__(self, down: bool):
        self.down = down


class OpenAICompatAdapter(EngineAdapter):
    """Polls any OpenAI-compatible server; rich data only if it serves /metrics."""

    def __init__(
        self,
        engine_id: str = "",
        label: str = "",
        base_url: str = "http://127.0.0.1:8000",
        fetch=None,
    ):
        super().__init__(engine_id, label, base_url)
        self._fetch = fetch if fetch is not None else self._urlopen

    def _urlopen(self, path: str) -> str:
        with urllib.request.urlopen(self.base_url + path, timeout=_TIMEOUT_S) as resp:
            return resp.read().decode("utf-8")

    def _get(self, path: str, warnings: list):
        try:
            return self._fetch(path)
        except Exception as exc:
            down = _looks_down(exc)
            warnings.append(f"{path} {'unreachable' if down else 'failed'} ({type(exc).__name__})")
            return _Failed(down)

    def snapshot(self) -> dict:
        warnings: list[str] = []
        models_raw = self._get("/v1/models", warnings)
        health_raw = self._get("/health", warnings)
        metrics_raw = self._get("/metrics", warnings)

        models = self._json(models_raw)
        health = self._json(health_raw)
        metrics_text = metrics_raw if isinstance(metrics_raw, str) else None
        samples = prometheus.parse(metrics_text) if metrics_text else {}

        model_entry = self._first_model(models)
        model_id = (model_entry or {}).get("id")
        max_len = (model_entry or {}).get("max_model_len")
        if isinstance(models_raw, _Failed):
            status = "down" if models_raw.down else "error"
        elif health is not None and health.get("status") in ("loading", "starting"):
            status = "loading"
        elif model_id is None:
            status = "loading"
        else:
            status = "ready"

        queue = empty_section("queue")
        queue["running"] = _counter_total(samples, "vllm:num_requests_running")
        queue["waiting"] = _counter_total(samples, "vllm:num_requests_waiting")

        tokens = empty_section("tokens")
        tokens["prompt_total"] = _counter_total(samples, "vllm:prompt_tokens_total")
        tokens["generation_total"] = _counter_total(samples, "vllm:generation_tokens_total")

        histograms = self._latency_histograms(samples)
        cache = empty_section("cache")
        hits = _counter_total(samples, "vllm:prefix_cache_hits_total")
        queries = _counter_total(samples, "vllm:prefix_cache_queries_total")
        if hits is not None and queries:  # queries None or 0 -> no rate yet
            cache["prefix_hit_rate"] = hits / queries
        prefix_cache = hits is not None and queries is not None

        capabilities = empty_capabilities()
        capabilities["latency_histograms"] = any(hist is not None for hist in histograms.values())
        capabilities["prefix_cache"] = prefix_cache

        return {
            "id": self.engine_id,
            "type": "openai-compatible",
            "label": self.label,
            "status": status,
            "version": None,
            "uptime_s": None,
            "startup_s": None,
            "model": {
                "id": model_id,
                "state": "resident" if status == "ready" else None,
                "max_model_len": max_len,
                "reasoning_efforts": None,
                "vram_bytes": None,
                "load_seconds": None,
            },
            "queue": queue,
            "throughput": {key: None for key in _THROUGHPUT_KEYS},
            "throughput_levels": None,
            "latency": histograms,
            "tokens": tokens,
            "cache": cache,
            "requests": None,
            "speculative": None,
            "transfers": None,
            "experts": None,
            "warnings": warnings,
            "capabilities": capabilities,
        }

    @staticmethod
    def _json(raw):
        if not isinstance(raw, str):
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None  # e.g. vLLM's /health answers 200 with an empty body
        return data if isinstance(data, dict) else None

    @staticmethod
    def _first_model(models):
        if not isinstance(models, dict):
            return None
        data = models.get("data") or []
        return data[0] if data else None

    @staticmethod
    def _latency_histograms(samples) -> dict:
        """Exactly the SCHEMA latency keys; None where the engine reports nothing.

        ``prometheus.histogram`` returns None for a histogram family that is
        absent and for one with zero observations, so an unreported measurement
        and an unused one both leave the key None. ``itl`` tries the renamed
        metric first and falls back to the older per-output-token one.
        """
        out = empty_section("latency")
        for key, metric in _LATENCY_HISTOGRAMS.items():
            out[key] = prometheus.histogram(samples, metric)
        for metric in _ITL_METRICS:
            hist = prometheus.histogram(samples, metric)
            if hist is not None:
                out["itl"] = hist
                break
        return out

"""llama.cpp ``llama-server`` adapter.

Polls four read-only endpoints: ``/health`` (200 = ready, 503 = model still
loading), ``/props`` (build info, model, slot count), ``/slots`` (one row per
parallel slot) and ``/metrics`` (Prometheus text). This build (b1-edec462)
exposes no latency histograms, no request counts and no uptime metric, so
``latency``, the per-request timings and the lifetime ``requests`` figure
stay None-filled; ``/props`` also gives no load duration, so ``model.state``
is simply resident whenever ``/health`` says ok.

Token semantics, confirmed on the live server (T-019 step 4):
``llamacpp:prompt_tokens_total`` counts *processed* prompt tokens only — its
HELP line says "excluding cached tokens" and sending the same prompt twice
moved ``llamacpp:prompt_tokens_cached_total`` by the full prompt on the
second call while ``prompt_tokens_total`` barely moved. The engine-visible
prompt volume is therefore processed + cached, which is what
``throughput_levels.*.prompt_tokens`` reports (matching the Radiance
adapter, whose ``vllm:prompt_tokens_total`` includes cached tokens) and what
the cache-share denominator uses.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import prometheus
from .base import EngineAdapter, empty_capabilities, empty_section

ENDPOINTS = ("/health", "/props", "/slots", "/metrics")
_TIMEOUT_S = 2.0
# Deltas measured over less than this is noise (a 2 s poll straddles one step).
_MIN_RATE_DT_S = 0.5

_M = "llamacpp:"
_PROMPT = _M + "prompt_tokens_total"            # processed prompt tokens, cache excluded
_PROMPT_S = _M + "prompt_seconds_total"
_CACHED = _M + "prompt_tokens_cached_total"
_PREDICTED = _M + "tokens_predicted_total"
_PREDICTED_S = _M + "tokens_predicted_seconds_total"
_PROCESSING = _M + "requests_processing"        # gauges, not counters
_DEFERRED = _M + "requests_deferred"
_DRAFT_TOKENS = _M + "spec_decode_num_draft_tokens_total"
_ACCEPTED_TOKENS = _M + "spec_decode_num_accepted_tokens_total"
_DRAFTS = _M + "spec_decode_num_drafts_total"

_PREFILL_NOTE = "lifetime average since server start; cache hits cost no time here"


def _looks_down(exc: BaseException) -> bool:
    """True for connection refused / timeouts, False for HTTP or data errors."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, OSError)
    return isinstance(exc, OSError)


def _counter(samples, name):
    entries = samples.get(name)
    if not entries:
        return None
    return sum(value for _labels, value in entries)


def _div(num, den):
    """num / den; None on missing data or a zero denominator."""
    if num is None or den is None or den == 0:
        return None
    return num / den


def _add(a, b):
    if a is None or b is None:
        return None
    return a + b


def _basename(path):
    """BASENAME only — a report must never leak the model's absolute path."""
    if not isinstance(path, str) or not path:
        return None
    return os.path.basename(path.rstrip("/")) or None


class _Failed:
    __slots__ = ("down",)

    def __init__(self, down: bool):
        self.down = down


class _Loading:
    """``/health`` answered 503: the server is up, the model is not."""

    __slots__ = ()


class LlamaCppAdapter(EngineAdapter):
    """Polls a llama.cpp llama-server's read-only endpoints for one snapshot."""

    def __init__(
        self,
        engine_id: str = "",
        label: str = "",
        base_url: str = "http://127.0.0.1:8001",
        fetch=None,
        clock=time.monotonic,
        wall=time.time,
    ):
        super().__init__(engine_id, label, base_url)
        self._fetch = fetch if fetch is not None else self._urlopen
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._prev = None
        self._last = {"decode_tps": None, "prefill_tps": None, "at": None}

    def _urlopen(self, path: str) -> str:
        with urllib.request.urlopen(self.base_url + path, timeout=_TIMEOUT_S) as resp:
            return resp.read().decode("utf-8")

    def _get(self, path: str, warnings: list):
        try:
            text = self._fetch(path)
            if path == "/metrics":
                return text
            if path == "/health":
                try:
                    return json.loads(text)
                except ValueError:
                    return text  # older builds answer plain "ok"
            return json.loads(text)
        except urllib.error.HTTPError as exc:
            if path == "/health" and exc.code == 503:
                return _Loading()
            warnings.append(f"{path} failed (HTTP {exc.code})")
            return _Failed(False)
        except Exception as exc:
            down = _looks_down(exc)
            warnings.append(f"{path} {'unreachable' if down else 'failed'} ({type(exc).__name__})")
            return _Failed(down)

    def snapshot(self) -> dict:
        warnings: list[str] = []
        got = {path: self._get(path, warnings) for path in ENDPOINTS}
        failed = {path: res for path, res in got.items() if isinstance(res, _Failed)}
        health = got["/health"] if "/health" not in failed else None
        props = got["/props"] if isinstance(got["/props"], dict) else None
        slots = got["/slots"] if isinstance(got["/slots"], list) else None
        metrics_text = got["/metrics"] if isinstance(got["/metrics"], str) else None
        samples = prometheus.parse(metrics_text) if metrics_text else {}

        health_state = self._health_state(health)
        if any(res.down for res in failed.values()):
            status = "down"
        elif isinstance(health, _Loading) or health_state == "loading":
            status = "loading"
        elif failed or health_state != "ok":
            status = "error"
        else:
            status = "ready"

        prompt = _counter(samples, _PROMPT)
        cached = _counter(samples, _CACHED)
        predicted = _counter(samples, _PREDICTED)
        prompt_total_all = _add(prompt, cached)

        now = self._clock()
        wall_now = self._wall()
        counters = {"prefill_total": prompt, "decode_total": predicted}
        with self._lock:
            prev = self._prev
            rates = self._measured_rates(prev, counters, now)
            if rates["reset"]:
                self._last = {"decode_tps": None, "prefill_tps": None, "at": None}
            for key in ("decode_tps", "prefill_tps"):
                if rates[key] is not None:
                    self._last[key] = rates[key]
                    self._last["at"] = wall_now
            # llama.cpp has no rolling-rate gauge: llamacpp:*_tokens_seconds is
            # a lifetime average, reported in throughput_levels instead, so
            # the *_gauge keys stay None (a gauge that never decays would
            # freeze the "last" dials on an idle engine).
            throughput = {
                "decode_tps_gauge": None,
                "prefill_tps_gauge": None,
                "decode_tps": rates["decode_tps"],
                "prefill_tps": rates["prefill_tps"],
                "last_decode_tps": self._last["decode_tps"],
                "last_prefill_tps": self._last["prefill_tps"],
                "last_at": self._last["at"],
            }
            # A rejected short window keeps the older baseline, so the tokens
            # it carried are diffed by the next accepted sample, not lost.
            if prev is None or now - prev["t"] >= _MIN_RATE_DT_S:
                self._prev = {"t": now, **counters}

        running = _counter(samples, _PROCESSING)
        waiting = _counter(samples, _DEFERRED)
        queue = empty_section("queue")
        queue["running"] = running
        queue["waiting"] = waiting
        queue["max_num_seqs"] = (props or {}).get("total_slots")
        if waiting:
            warnings.append(f"{waiting:g} request(s) deferred (all slots busy)")

        cache = empty_section("cache")
        cache["prefix_hit_rate"] = _div(cached, prompt_total_all)

        drafts = _counter(samples, _DRAFTS)
        speculative = {
            "draft_accept": _div(_counter(samples, _ACCEPTED_TOKENS), _counter(samples, _DRAFT_TOKENS))
            if drafts
            else None,
            "per_position": None,   # llama.cpp counts acceptance totals only
            "adaptive_depth": None,
        }

        capabilities = empty_capabilities()
        capabilities["per_request"] = True
        capabilities["throughput_levels"] = True
        capabilities["prefix_cache"] = True
        capabilities["speculative"] = bool(drafts)

        max_len = ((props or {}).get("default_generation_settings") or {}).get("n_ctx")

        return {
            "id": self.engine_id,
            "type": "llama.cpp",
            "label": self.label,
            "status": status,
            "version": (props or {}).get("build_info"),
            "uptime_s": None,     # no uptime metric in this build
            "startup_s": None,
            "model": {
                "id": (props or {}).get("model_alias"),
                "state": "resident" if status == "ready" else None,
                "max_model_len": max_len,
                "reasoning_efforts": None,
                "vram_bytes": None,
                "load_seconds": None,
                "file": _basename((props or {}).get("model_path")),
            },
            "queue": queue,
            "throughput": throughput,
            "throughput_levels": {
                # "lifetime" only: llama.cpp has no completed-request
                # histogram sums, so a 15-minute window would diff counters
                # the UI has no baseline semantics for.
                "lifetime": {
                    "active_decode_tps": _div(predicted, _counter(samples, _PREDICTED_S)),
                    "effective_request_tps": None,  # no per-request e2e time
                    "queue_share": None,            # not measurable here
                    "prompt_tokens": prompt_total_all,
                    "cached_prompt_tokens": cached,
                    "new_prompt_tokens": prompt,    # processed == new (see above)
                    "cache_share": _div(cached, prompt_total_all),
                    "prefill_new_tps": _div(prompt, _counter(samples, _PROMPT_S)),
                    "requests": None,               # no request counter
                },
                "window": None,
                "prefill_note": _PREFILL_NOTE,
            },
            "latency": empty_section("latency"),
            "tokens": {
                "prompt_total": prompt_total_all,
                "generation_total": predicted,
                "prefill_total": None,   # one prompt path; == prompt_total
                "decode_total": None,    # one decode path; == generation_total
                "cached_total": cached,
            },
            "cache": cache,
            "requests": self._requests(slots),
            "speculative": speculative,
            "transfers": None,
            "experts": None,
            "warnings": warnings,
            "capabilities": capabilities,
        }

    @staticmethod
    def _health_state(health) -> str:
        """Normalise /health to "ok" | "loading" | "unknown"."""
        if isinstance(health, _Loading):
            return "loading"
        if isinstance(health, dict):
            value = health.get("status")
        elif isinstance(health, str):
            value = health.strip()  # older llama-server builds answer plain "ok"
        else:
            return "unknown"
        if value == "ok":
            return "ok"
        if value in ("loading", "starting"):
            return "loading"
        return "unknown"

    @staticmethod
    def _requests(slots):
        """One UI row per llama.cpp slot; ``ctx`` is prompt + decoded tokens."""
        rows = []
        for slot in slots or []:
            if not isinstance(slot, dict):
                continue
            raw_next = slot.get("next_token")
            # llama-server reports next_token as a one-element list (per
            # sequence) in this build and a bare object in others.
            next_token = raw_next[0] if isinstance(raw_next, list) and raw_next else (
                raw_next if isinstance(raw_next, dict) else {}
            )
            prompt = slot.get("n_prompt_tokens")
            decoded = next_token.get("n_decoded")
            ctx = _add(prompt, decoded)
            n_ctx = slot.get("n_ctx")
            rows.append(
                {
                    "id": slot.get("id_task"),
                    "slot": slot.get("id"),
                    "state": "processing" if slot.get("is_processing") else "idle",
                    "prompt": prompt,
                    "cached": slot.get("n_prompt_tokens_cache"),
                    "processed": slot.get("n_prompt_tokens_processed"),
                    "output": decoded,
                    "ctx": ctx,
                    "ctx_pct": round(ctx / n_ctx * 100, 1) if ctx is not None and n_ctx else None,
                }
            )
        return rows

    @staticmethod
    def _measured_rates(prev, counters, now):
        """Token rates from counter deltas between consecutive snapshots.

        Same contract as the Radiance adapter (T-013): a delta over a window
        shorter than ``_MIN_RATE_DT_S`` is noise, a zero delta means the phase
        was idle, and a negative delta means the server restarted, voiding
        every stored sample.
        """
        rates = {"decode_tps": None, "prefill_tps": None, "reset": False}
        if prev is None:
            return rates
        dt = now - prev["t"]
        if dt < _MIN_RATE_DT_S:
            return rates
        for key, metric in (("decode_tps", "decode_total"), ("prefill_tps", "prefill_total")):
            cur = counters.get(metric)
            old = prev.get(metric)
            if cur is None or old is None:
                continue
            if cur < old:
                rates["reset"] = True
                return rates
            if cur > old:
                rates[key] = (cur - old) / dt
        return rates

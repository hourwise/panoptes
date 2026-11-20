"""Radiance inference engine adapter.

Read-only GETs against the Radiance server (``/health``, ``/v1/models``,
``/stats``, ``/metrics``); nothing in here can make the engine generate text.
One adapter instance keeps the previous ``/stats`` snapshot behind a lock so
byte and token counters can become rates and counter increases can raise warnings.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque

from . import prometheus
from .base import CAPABILITIES, EngineAdapter

ENDPOINTS = ("/health", "/v1/models", "/stats", "/metrics")
_TIMEOUT_S = 2.0

_QUEUE_KEYS = ("running", "waiting", "in_flight", "queue_cap", "max_num_seqs", "preemptions")
_THROUGHPUT_KEYS = ("decode_tps", "prefill_tps")  # rolling gauges: they ramp and decay
# Deltas measured over less than this is noise (a 2 s poll straddles one step).
_MIN_RATE_DT_S = 0.5
# The "window" throughput level diffs against a baseline this old (~15 minutes).
_WINDOW_S = 900.0
_ENGINE_KEYS = ("version", "uptime_s", "startup_s")
_CACHE_KEYS = (
    "prefix_hit_rate",
    "linear_hit_rate",
    "prefix_evictions",
    "kv_util",
    "kv_tokens_used",
    "kv_tokens_max",
)

_LATENCY_HISTOGRAMS = {
    "ttft": "vllm:time_to_first_token_seconds",
    "e2e": "vllm:e2e_request_latency_seconds",
    "queue": "vllm:request_queue_time_seconds",
    "itl": "vllm:inter_token_latency_seconds",
}

_TOKEN_COUNTERS = {
    "prompt_total": "vllm:prompt_tokens_total",
    "generation_total": "vllm:generation_tokens_total",
    "prefill_total": "radiance:prefill_tokens_total",
    "decode_total": "radiance:decode_tokens_total",
    "cached_total": "radiance:attn_cached_tokens_total",
}

# Everything the three throughput levels need. The vllm:* histogram _sum and
# _count families only advance when a request finishes, so window deltas cover
# completed requests only.
_LEVEL_COUNTERS = {
    "gen_tokens": "vllm:request_generation_tokens_sum",
    "decode_s": "vllm:request_decode_time_seconds_sum",
    "e2e_s": "vllm:e2e_request_latency_seconds_sum",
    "queue_s": "vllm:request_queue_time_seconds_sum",
    "requests": "vllm:request_generation_tokens_count",
    "prompt": "vllm:prompt_tokens_total",
    "cached": "radiance:attn_cached_tokens_total",
    "prefill": "radiance:prefill_tokens_total",
    "step_s": "radiance:engine_step_seconds_total",
    "step_decode_s": "radiance:engine_step_seconds_decode_total",
}

_WORKER_NOTE = (
    "Effective worker speed (tokens over the whole task, including tool runs and "
    "compaction) is computed per task by the supervisor; see the Agents tab."
)
_PREFILL_NOTE = "approximate: prefill_new_tps includes mixed prefill+decode steps"


def _looks_down(exc: BaseException) -> bool:
    """True for connection refused / timeouts, False for HTTP or data errors."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, OSError)
    return isinstance(exc, OSError)


def _pick(src, keys):
    if not isinstance(src, dict):
        return {k: None for k in keys}
    return {k: src.get(k) for k in keys}


def _counter(samples, name):
    entries = samples.get(name)
    if not entries:
        return None
    return sum(value for _labels, value in entries)


def _first_model(models):
    if not isinstance(models, dict):
        return None
    data = models.get("data") or []
    return data[0] if data else None


def _div(num, den):
    """num / den; None on missing data or a zero denominator."""
    if num is None or den is None or den == 0:
        return None
    return num / den


def _sub(a, b):
    if a is None or b is None:
        return None
    return a - b


def _levels(counters):
    """Ratio maths over one set of absolute (lifetime) or delta (window) values."""
    prompt, cached = counters["prompt"], counters["cached"]
    new_prompt = _sub(prompt, cached)
    if new_prompt is not None and new_prompt < 0:
        new_prompt = 0.0
    return {
        "active_decode_tps": _div(counters["gen_tokens"], counters["decode_s"]),
        "effective_request_tps": _div(counters["gen_tokens"], counters["e2e_s"]),
        "queue_share": _div(counters["queue_s"], counters["e2e_s"]),
        "prompt_tokens": prompt,
        "cached_prompt_tokens": cached,
        "new_prompt_tokens": new_prompt,
        "cache_share": _div(cached, prompt),
        "prefill_new_tps": _div(
            counters["prefill"], _sub(counters["step_s"], counters["step_decode_s"])
        ),
        "requests": counters["requests"],
    }


class _Failed:
    __slots__ = ("down",)

    def __init__(self, down: bool):
        self.down = down


class RadianceAdapter(EngineAdapter):
    """Polls Radiance's read-only endpoints and builds one panel snapshot."""

    def __init__(
        self,
        engine_id: str = "radiance",
        label: str = "",
        base_url: str = "http://127.0.0.1:8000",
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
        self._tl_hist = deque()

    def _urlopen(self, path: str) -> str:
        with urllib.request.urlopen(self.base_url + path, timeout=_TIMEOUT_S) as resp:
            return resp.read().decode("utf-8")

    def _get(self, path: str, warnings: list):
        try:
            text = self._fetch(path)
            if path == "/metrics":
                return text
            return json.loads(text)
        except Exception as exc:
            down = _looks_down(exc)
            warnings.append(f"{path} {'unreachable' if down else 'failed'} ({type(exc).__name__})")
            return _Failed(down)

    def snapshot(self) -> dict:
        warnings: list[str] = []
        got = {path: self._get(path, warnings) for path in ENDPOINTS}
        failed = {path: res for path, res in got.items() if isinstance(res, _Failed)}
        health = got["/health"] if "/health" not in failed else None
        models = got["/v1/models"] if "/v1/models" not in failed else None
        stats = got["/stats"] if "/stats" not in failed else None
        metrics_text = got["/metrics"] if "/metrics" not in failed else None
        samples = prometheus.parse(metrics_text) if metrics_text else {}

        model = _first_model(models)
        state = self._model_state(health, model)
        if failed:
            status = "down" if any(res.down for res in failed.values()) else "error"
        elif state == "resident":
            status = "ready"
        else:
            status = "loading"

        now = self._clock()
        wall_now = self._wall()
        gauges = _pick(stats, _THROUGHPUT_KEYS)
        counters = {
            "admission": _counter(samples, "radiance:http_admission_rejected_total"),
            "disconnects": _counter(samples, "radiance:client_disconnect_total"),
            "decode_total": _counter(samples, "radiance:decode_tokens_total"),
            "prefill_total": _counter(samples, "radiance:prefill_tokens_total"),
        }
        with self._lock:
            prev = self._prev
            rates = self._measured_rates(prev, counters, now)
            if rates["reset"]:
                self._last = {"decode_tps": None, "prefill_tps": None, "at": None}
            for key in ("decode_tps", "prefill_tps"):
                if rates[key] is not None:
                    self._last[key] = rates[key]
                    self._last["at"] = wall_now
            throughput = {
                "decode_tps_gauge": gauges["decode_tps"],
                "prefill_tps_gauge": gauges["prefill_tps"],
                "decode_tps": rates["decode_tps"],
                "prefill_tps": rates["prefill_tps"],
                "last_decode_tps": self._last["decode_tps"],
                "last_prefill_tps": self._last["prefill_tps"],
                "last_at": self._last["at"],
            }
            levels = self._throughput_levels(samples, now)
            # A rejected short window keeps the older baseline, so the tokens it
            # carried are diffed by the next accepted sample instead of lost.
            if prev is None or now - prev["t"] >= _MIN_RATE_DT_S:
                if isinstance(stats, dict):
                    self._prev = {
                        "t": now,
                        "stats": stats,
                        **counters,
                    }

        max_len = (model or {}).get("max_model_len")
        requests = []
        for req in (_pick(stats, ("requests",))["requests"] or []):
            req = dict(req)
            ctx = req.get("ctx")
            req["ctx_pct"] = round(ctx / max_len * 100, 1) if ctx is not None and max_len else None
            requests.append(req)

        link = _pick(stats, ("link",))["link"] or {}

        def rate(key):
            cur = link.get(key)
            if prev is None or cur is None:
                return None
            old = (prev["stats"].get("link") or {}).get(key)
            dt = now - prev["t"]
            if old is None or dt <= 0:
                return None
            return (cur - old) / dt

        transfers = {
            "h2d_bps": rate("h2d_bytes"),
            "d2h_bps": rate("d2h_bytes"),
            "ssd_read_bps": rate("ssd_bytes"),
            "h2d_bytes": link.get("h2d_bytes"),
            "d2h_bytes": link.get("d2h_bytes"),
            "ssd_bytes": link.get("ssd_bytes"),
        }

        draft_positions = _pick(stats, ("draft_positions",))["draft_positions"]
        per_position = None
        if isinstance(draft_positions, list):
            per_position = [
                {"position": i, "rate": pos.get("rate")} for i, pos in enumerate(draft_positions)
            ]

        experts_raw = _pick(stats, ("experts",))["experts"] or {}
        tier = experts_raw.get("tier")
        tier_counts = None
        if isinstance(tier, list):
            tier_counts = {}
            for value in tier:
                tier_counts[value] = tier_counts.get(value, 0) + 1

        vram_by_card = (model or {}).get("vram_bytes_by_card") or {}
        cache = _pick(stats, _CACHE_KEYS)

        warnings = self._counter_warnings(warnings, stats, samples, prev)
        kv_util = cache["kv_util"]
        if kv_util is not None and kv_util > 0.9:
            warnings.append(f"KV cache utilisation {kv_util:.0%} above 90%")

        return {
            "id": self.engine_id,
            "type": "radiance",
            "label": self.label,
            "engine": "radiance",
            "status": status,
            "capabilities": {key: key not in ("lifecycle", "controls") for key in CAPABILITIES},
            **_pick(stats, _ENGINE_KEYS),
            "model": {
                "id": (model or {}).get("id"),
                "state": state,
                "max_model_len": max_len,
                "reasoning_efforts": ((model or {}).get("capabilities") or {}).get(
                    "reasoning_efforts"
                ),
                "vram_bytes": sum(vram_by_card.values()) if vram_by_card else (model or {}).get(
                    "vram_bytes_per_card"
                ),
                "load_seconds": (model or {}).get("load_seconds"),
            },
            "queue": _pick(stats, _QUEUE_KEYS),
            "throughput": throughput,
            "throughput_levels": levels,
            "latency": {
                key: prometheus.histogram(samples, metric)
                for key, metric in _LATENCY_HISTOGRAMS.items()
            },
            "tokens": {key: _counter(samples, metric) for key, metric in _TOKEN_COUNTERS.items()},
            "cache": cache,
            "speculative": {
                "draft_accept": _pick(stats, ("draft_accept",))["draft_accept"],
                "per_position": per_position,
                "adaptive_depth": _counter(samples, "radiance:draft_depth_adaptive"),
            },
            "requests": requests,
            "transfers": transfers,
            "experts": {
                "n_layers": experts_raw.get("n_layers"),
                "n_experts": experts_raw.get("n_experts"),
                "tier_counts": tier_counts,
            },
            "warnings": warnings,
        }

    @staticmethod
    def _model_state(health, model):
        mid = (model or {}).get("id")
        if isinstance(health, dict):
            states = health.get("models") or {}
            if mid in states:
                return states[mid]
            if mid is None and states:
                return next(iter(states.values()))
        return (model or {}).get("state")

    @staticmethod
    def _measured_rates(prev, counters, now):
        """Token rates from counter deltas between consecutive snapshots.

        The ``/stats`` gauges ramp up inside a request and decay to 0 once it is
        done, so only the exact counters (``radiance:decode_tokens_total`` and
        ``radiance:prefill_tokens_total``) say what a phase really achieved. A
        delta over a window shorter than ``_MIN_RATE_DT_S`` is noise, a zero
        delta means that phase was idle, and a negative delta means the engine
        restarted, which voids every stored sample.
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

    def _throughput_levels(self, samples, now):
        """Three labelled throughput levels, as totals and as a rolling window.

        ``lifetime`` uses the since-start totals straight from ``/metrics``.
        ``window`` diffs the current counters against a baseline kept in a
        short history: the first snapshot while the adapter is younger than
        ``_WINDOW_S``, afterwards the stored sample nearest 15 minutes old.
        The histogram counters only move when a request completes, so the
        window covers completed requests. If any counter went backwards since
        the last poll the engine restarted and every baseline is void.
        """
        cur = {key: _counter(samples, metric) for key, metric in _LEVEL_COUNTERS.items()}
        if all(value is not None for value in cur.values()):
            hist = self._tl_hist
            if hist and any(cur[key] < hist[-1][1][key] for key in cur):
                hist.clear()
            hist.append((now, cur))
            # slide the baseline: keep it the newest sample >= _WINDOW_S old,
            # or the very first one while the adapter is still younger
            while len(hist) > 1 and now - hist[1][0] >= _WINDOW_S:
                hist.popleft()
        base = self._tl_hist[0] if self._tl_hist else None
        if base is None:
            window = _levels({key: None for key in _LEVEL_COUNTERS})
            window["window_seconds"] = None
        else:
            window = _levels({key: _sub(cur[key], base[1][key]) for key in cur})
            window["window_seconds"] = now - base[0]
        return {
            "lifetime": _levels(cur),
            "window": window,
            "worker_note": _WORKER_NOTE,
            "prefill_note": _PREFILL_NOTE,
        }

    @staticmethod
    def _counter_warnings(warnings, stats, samples, prev):
        if prev is None or not isinstance(stats, dict):
            return warnings
        increases = [
            (
                "preemptions",
                stats.get("preemptions"),
                prev["stats"].get("preemptions"),
            ),
            (
                "admission rejections",
                _counter(samples, "radiance:http_admission_rejected_total"),
                prev.get("admission"),
            ),
            (
                "client disconnects",
                _counter(samples, "radiance:client_disconnect_total"),
                prev.get("disconnects"),
            ),
        ]
        for label, cur, old in increases:
            if cur is not None and old is not None and cur > old:
                warnings.append(f"{label} increased by {cur - old:g} since last snapshot")
        return warnings

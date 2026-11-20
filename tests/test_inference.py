"""Radiance inference adapter and Prometheus parser tests (fixtures + fake fetch)."""

import json
import math
import unittest
from pathlib import Path

from wsd import routes  # noqa: F401  (registers ROUTES)
from wsd import server
from wsd.inference import ENGINES
from wsd.inference import prometheus
from wsd.inference.radiance import RadianceAdapter

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "inference"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


HEALTH = fixture("health.json")
MODELS = fixture("models.json")
STATS = fixture("stats.json")
METRICS = fixture("metrics.txt")


def fake_fetch(overrides=None, failures=None):
    data = {"/health": HEALTH, "/v1/models": MODELS, "/stats": STATS, "/metrics": METRICS}
    data.update(overrides or {})
    failures = failures or {}

    def fetch(path):
        if path in failures:
            raise failures[path]
        return data[path]

    return fetch


class ParseTest(unittest.TestCase):
    def test_skips_comments_and_blank_lines(self):
        samples = prometheus.parse("# HELP x a metric\n# TYPE x gauge\n\n  x 1\n")
        self.assertEqual(samples, {"x": [({}, 1.0)]})

    def test_label_value_with_comma_space_and_escapes(self):
        samples = prometheus.parse(
            'x{a="hello, world",b="2",c="say \\"hi\\""} 5\n'
        )
        labels, value = samples["x"][0]
        self.assertEqual(labels, {"a": "hello, world", "b": "2", "c": 'say "hi"'})
        self.assertEqual(value, 5.0)

    def test_special_values_and_timestamp(self):
        samples = prometheus.parse("a NaN\nb +Inf\nc -Inf\nd 3 1600000000\n")
        self.assertTrue(math.isnan(samples["a"][0][1]))
        self.assertEqual(samples["b"][0][1], math.inf)
        self.assertEqual(samples["c"][0][1], -math.inf)
        self.assertEqual(samples["d"][0][1], 3.0)

    def test_repeated_name_multiple_series(self):
        samples = prometheus.parse('m{e="0"} 1\nm{e="1"} 2\n')
        self.assertEqual([v for _l, v in samples["m"]], [1.0, 2.0])
        self.assertEqual([l["e"] for l, _v in samples["m"]], ["0", "1"])

    def test_real_metrics_fixture_parses(self):
        samples = prometheus.parse(METRICS)
        self.assertEqual(samples["vllm:prompt_tokens_total"][0][1], 14144751.0)
        self.assertIn("vllm:time_to_first_token_seconds_bucket", samples)
        # info-style metric with a JSON-ish label value must not break the parse
        self.assertIn("vllm:cache_config_info", samples)


class HistogramTest(unittest.TestCase):
    TEXT = (
        "h_bucket{le=\"0.1\"} 10\n"
        "h_bucket{le=\"0.2\"} 30\n"
        "h_bucket{le=\"+Inf\"} 40\n"
        "h_sum 8\n"
        "h_count 40\n"
        "z_bucket{le=\"+Inf\"} 0\n"
        "z_sum 0\n"
        "z_count 0\n"
    )

    def setUp(self):
        self.samples = prometheus.parse(self.TEXT)

    def test_count_sum_mean(self):
        h = prometheus.histogram(self.samples, "h")
        self.assertEqual(h["count"], 40)
        self.assertEqual(h["sum"], 8)
        self.assertAlmostEqual(h["mean"], 0.2)

    def test_percentiles_interpolate_in_bucket(self):
        h = prometheus.histogram(self.samples, "h")
        # p50 target 20 falls in (0.1, 0.2] between cums 10..30 -> 0.1 + 0.1*10/20
        self.assertAlmostEqual(h["p50"], 0.15)
        # p95 target 38 lands in the +Inf bucket -> previous bound
        self.assertAlmostEqual(h["p95"], 0.2)

    def test_zero_count_returns_none(self):
        self.assertIsNone(prometheus.histogram(self.samples, "z"))

    def test_missing_metric_returns_none(self):
        self.assertIsNone(prometheus.histogram(self.samples, "nope"))

    def test_real_ttft_histogram(self):
        samples = prometheus.parse(METRICS)
        h = prometheus.histogram(samples, "vllm:time_to_first_token_seconds")
        self.assertEqual(h["count"], 481)
        self.assertIsNotNone(h["p50"])
        self.assertLessEqual(h["p50"], h["p95"])


def adapter(**kwargs):
    return RadianceAdapter(fetch=fake_fetch(**kwargs), clock=iter([100.0, 102.0]).__next__)


class AdapterStatusTest(unittest.TestCase):
    def test_ready_state_from_fixtures(self):
        s = adapter().snapshot()
        self.assertEqual(s["engine"], "radiance")
        self.assertEqual(s["status"], "ready")
        self.assertEqual(s["version"], "2.1.0")
        self.assertEqual(s["uptime_s"], 9827)
        self.assertEqual(s["startup_s"], 29)
        self.assertEqual(s["model"]["id"], "flashnext")
        self.assertEqual(s["model"]["state"], "resident")
        self.assertEqual(s["model"]["max_model_len"], 65536)
        self.assertEqual(
            s["model"]["reasoning_efforts"], ["none", "minimal", "low", "medium", "xhigh"]
        )
        self.assertEqual(s["model"]["vram_bytes"], 30472845544)
        self.assertIsNotNone(s["model"]["load_seconds"])

    def test_queue_keeps_real_zeros(self):
        q = adapter().snapshot()["queue"]
        self.assertEqual(q["running"], 0)
        self.assertEqual(q["waiting"], 0)
        self.assertEqual(q["in_flight"], 0)
        self.assertEqual(q["queue_cap"], 8)
        self.assertEqual(q["max_num_seqs"], 1)
        self.assertEqual(q["preemptions"], 0)
        t = adapter().snapshot()["throughput"]
        self.assertAlmostEqual(t["decode_tps_gauge"], 88.28217315673828)
        self.assertAlmostEqual(t["prefill_tps_gauge"], 64.23725891113281)
        self.assertIsNone(t["decode_tps"])  # no earlier sample to diff against
        self.assertIsNone(t["last_decode_tps"])
        self.assertIsNone(t["last_at"])

    def test_tokens_cache_speculative_experts(self):
        s = adapter().snapshot()
        self.assertEqual(s["tokens"]["prompt_total"], 14144751.0)
        self.assertEqual(s["tokens"]["generation_total"], 372771.0)
        self.assertEqual(s["tokens"]["prefill_total"], 2599215.0)
        self.assertEqual(s["tokens"]["decode_total"], 372290.0)
        self.assertEqual(s["tokens"]["cached_total"], 12349260.0)
        self.assertAlmostEqual(s["cache"]["prefix_hit_rate"], 0.8730630874633789)
        self.assertAlmostEqual(s["cache"]["linear_hit_rate"], 0.8162417411804199)
        self.assertEqual(s["cache"]["prefix_evictions"], 340904.0)
        self.assertEqual(s["cache"]["kv_tokens_max"], 65536)
        self.assertAlmostEqual(s["speculative"]["draft_accept"], 0.6302933096885681)
        self.assertEqual([p["position"] for p in s["speculative"]["per_position"]], [0, 1, 2, 3, 4])
        self.assertAlmostEqual(s["speculative"]["per_position"][0]["rate"], 0.7864185049813792)
        self.assertEqual(s["speculative"]["adaptive_depth"], 0.0)
        e = s["experts"]
        self.assertEqual(e["n_layers"], 49)
        self.assertEqual(e["n_experts"], 512)
        self.assertEqual(sum(e["tier_counts"].values()), 49 * 512)  # 25088 experts scored

    def test_latency_histograms_present(self):
        lat = adapter().snapshot()["latency"]
        self.assertEqual(sorted(lat), ["e2e", "itl", "queue", "ttft"])
        self.assertEqual(lat["ttft"]["count"], 481)
        self.assertIsNotNone(lat["e2e"]["p95"])

    def test_ctx_pct(self):
        s = adapter().snapshot()
        self.assertEqual(s["requests"][0]["ctx"], 12272)
        self.assertEqual(s["requests"][0]["ctx_pct"], 18.7)

    def test_loading_when_not_resident(self):
        health = json.dumps({"status": "ok", "models": {"flashnext": "loading"}})
        s = adapter(overrides={"/health": health}).snapshot()
        self.assertEqual(s["status"], "loading")

    def test_down_on_connection_refused(self):
        s = adapter(failures={"/health": ConnectionRefusedError()}).snapshot()
        self.assertEqual(s["status"], "down")
        self.assertTrue(any("/health" in w for w in s["warnings"]))

    def test_down_on_timeout(self):
        s = adapter(failures={"/health": TimeoutError()}).snapshot()
        self.assertEqual(s["status"], "down")

    def test_error_on_unexpected_exception(self):
        s = adapter(failures={"/health": RuntimeError("boom")}).snapshot()
        self.assertEqual(s["status"], "error")

    def test_one_endpoint_failing_keeps_others(self):
        s = adapter(failures={"/metrics": RuntimeError("boom")}).snapshot()
        self.assertEqual(s["status"], "error")
        self.assertEqual(s["model"]["id"], "flashnext")
        self.assertEqual(s["queue"]["queue_cap"], 8)
        self.assertIsNone(s["tokens"]["prompt_total"])
        self.assertEqual(s["latency"], {"ttft": None, "e2e": None, "queue": None, "itl": None})
        self.assertIn("/metrics", " ".join(s["warnings"]))

    def test_stats_failing_keeps_others(self):
        s = adapter(failures={"/stats": RuntimeError("boom")}).snapshot()
        self.assertEqual(s["model"]["id"], "flashnext")
        self.assertEqual(s["tokens"]["prompt_total"], 14144751.0)
        self.assertIsNone(s["throughput"]["decode_tps"])
        self.assertIsNone(s["transfers"]["h2d_bps"])
        self.assertIn("/stats", " ".join(s["warnings"]))

    def test_snapshot_is_json_serialisable(self):
        json.dumps(adapter().snapshot())


class AdapterRatesWarningsTest(unittest.TestCase):
    def stats2(self, **link):
        stats = json.loads(STATS)
        stats["link"].update(link)
        return stats

    METRICS_BUMPED = METRICS.replace(
        'radiance:http_admission_rejected_total{model_name="flashnext",engine="0"} 0',
        'radiance:http_admission_rejected_total{model_name="flashnext",engine="0"} 1',
    ).replace(
        'radiance:client_disconnect_total{model_name="flashnext",engine="0"} 2',
        'radiance:client_disconnect_total{model_name="flashnext",engine="0"} 5',
    )

    def two_call_fetch(self, stats2, metrics2=None):
        calls = {"n": 0}

        def fetch(path):
            if path == "/stats":
                calls["n"] += 1
                return STATS if calls["n"] == 1 else json.dumps(stats2)
            if path == "/metrics":
                return METRICS if calls["n"] <= 1 or metrics2 is None else metrics2
            return {"/health": HEALTH, "/v1/models": MODELS}[path]

        return fetch

    def test_no_warnings_on_first_snapshot(self):
        self.assertEqual(adapter().snapshot()["warnings"], [])

    def test_rates_between_snapshots(self):
        stats2 = self.stats2(h2d_bytes=62708481180672 + 4000, d2h_bytes=8367921809664 + 2000)
        a = RadianceAdapter(
            fetch=self.two_call_fetch(stats2), clock=iter([100.0, 102.0]).__next__
        )
        first = a.snapshot()
        self.assertIsNone(first["transfers"]["h2d_bps"])
        self.assertIsNone(first["transfers"]["d2h_bps"])
        self.assertIsNone(first["transfers"]["ssd_read_bps"])
        self.assertEqual(first["transfers"]["h2d_bytes"], 62708481180672)
        second = a.snapshot()
        self.assertAlmostEqual(second["transfers"]["h2d_bps"], 4000 / 2.0)
        self.assertAlmostEqual(second["transfers"]["d2h_bps"], 2000 / 2.0)
        self.assertEqual(second["transfers"]["ssd_read_bps"], 0.0)
        self.assertEqual(second["transfers"]["h2d_bytes"], 62708481180672 + 4000)

    def test_warnings_on_counter_increases(self):
        stats2 = json.loads(STATS)
        stats2["preemptions"] = 3
        stats2["kv_util"] = 0.95
        a = RadianceAdapter(
            fetch=self.two_call_fetch(stats2, metrics2=self.METRICS_BUMPED),
            clock=iter([100.0, 102.0]).__next__,
        )
        self.assertEqual(a.snapshot()["warnings"], [])
        joined = "\n".join(a.snapshot()["warnings"])
        self.assertIn("preemptions increased by 3", joined)
        self.assertIn("admission rejections increased by 1", joined)
        self.assertIn("client disconnects increased by 3", joined)
        self.assertIn("KV cache utilisation 95%", joined)


class ThroughputRatesTest(unittest.TestCase):
    """Rates come from token-counter deltas; the /stats gauges ramp and decay."""

    DECODE_LINE = 'radiance:decode_tokens_total{model_name="flashnext",engine="0"} 372290'
    PREFILL_LINE = 'radiance:prefill_tokens_total{model_name="flashnext",engine="0"} 2599215'

    def metrics(self, decode=None, prefill=None):
        text = METRICS
        for line, value in ((self.DECODE_LINE, decode), (self.PREFILL_LINE, prefill)):
            if value is not None:
                text = text.replace(line, line.rsplit(" ", 1)[0] + f" {value}")
        return text

    def poll(self, metric_texts, times, wall_start=1700000000.0):
        """Poll one adapter len(times) times and return each throughput block."""
        calls = {"n": 0}

        def fetch(path):
            if path == "/metrics":
                text = metric_texts[calls["n"]]
                calls["n"] += 1
                return text
            return {"/health": HEALTH, "/v1/models": MODELS, "/stats": STATS}[path]

        adapter_ = RadianceAdapter(
            fetch=fetch,
            clock=iter(times).__next__,
            wall=iter([wall_start + t for t in times]).__next__,
        )
        return [adapter_.snapshot()["throughput"] for _ in times]

    def test_first_snapshot_has_no_measured_rate(self):
        t = self.poll([METRICS], [100.0])[0]
        self.assertIsNone(t["decode_tps"])
        self.assertIsNone(t["prefill_tps"])
        self.assertIsNone(t["last_decode_tps"])
        self.assertIsNone(t["last_prefill_tps"])
        self.assertIsNone(t["last_at"])
        self.assertAlmostEqual(t["decode_tps_gauge"], 88.28217315673828)

    def test_measured_rate_from_counter_delta(self):
        moved = self.metrics(decode=372490, prefill=2603215)
        _first, second = self.poll([METRICS, moved], [100.0, 102.0])
        self.assertAlmostEqual(second["decode_tps"], 100.0)  # 200 tokens over 2 s
        self.assertAlmostEqual(second["prefill_tps"], 2000.0)  # 4000 tokens over 2 s
        self.assertAlmostEqual(second["last_decode_tps"], 100.0)
        self.assertAlmostEqual(second["last_prefill_tps"], 2000.0)
        self.assertEqual(second["last_at"], 1700000102.0)
        # the engine's own gauges are still published, untouched, under their own keys
        self.assertAlmostEqual(second["decode_tps_gauge"], 88.28217315673828)
        self.assertAlmostEqual(second["prefill_tps_gauge"], 64.23725891113281)

    def test_idle_poll_reports_none_but_keeps_the_last_rate(self):
        moved = self.metrics(decode=372490, prefill=2603215)
        snaps = self.poll([METRICS, moved, moved, moved], [100.0, 102.0, 104.0, 106.0])
        self.assertAlmostEqual(snaps[1]["decode_tps"], 100.0)
        for idle in (snaps[2], snaps[3]):
            self.assertIsNone(idle["decode_tps"])  # counters did not move
            self.assertIsNone(idle["prefill_tps"])
            self.assertAlmostEqual(idle["last_decode_tps"], 100.0)
            self.assertAlmostEqual(idle["last_prefill_tps"], 2000.0)
            self.assertEqual(idle["last_at"], 1700000102.0)  # not re-stamped while idle

    def test_short_window_is_noise_and_keeps_the_baseline(self):
        texts = [METRICS, self.metrics(decode=372300), self.metrics(decode=372490)]
        snaps = self.poll(texts, [100.0, 100.2, 102.0])
        self.assertIsNone(snaps[1]["decode_tps"])  # 0.2 s window is noise
        self.assertIsNone(snaps[1]["last_decode_tps"])
        self.assertAlmostEqual(snaps[2]["decode_tps"], 100.0)  # 200 tokens since t=100.0

    def test_counter_reset_clears_history(self):
        bumped = self.metrics(decode=372490, prefill=2603215)
        restarted = self.metrics(decode=500, prefill=1400)
        after = self.metrics(decode=700, prefill=1800)
        snaps = self.poll([METRICS, bumped, restarted, after], [100.0, 102.0, 104.0, 106.0])
        self.assertAlmostEqual(snaps[1]["last_decode_tps"], 100.0)
        self.assertIsNone(snaps[2]["decode_tps"])  # counters went backwards
        self.assertIsNone(snaps[2]["last_decode_tps"])  # rates from the old process are void
        self.assertIsNone(snaps[2]["last_prefill_tps"])
        self.assertIsNone(snaps[2]["last_at"])
        self.assertAlmostEqual(snaps[3]["decode_tps"], 100.0)  # measured off the new baseline
        self.assertAlmostEqual(snaps[3]["prefill_tps"], 200.0)

    def test_missing_counters_report_none_not_the_gauge(self):
        def fetch(path):
            if path == "/metrics":
                raise RuntimeError("boom")
            return {"/health": HEALTH, "/v1/models": MODELS, "/stats": STATS}[path]

        a = RadianceAdapter(fetch=fetch, clock=iter([100.0, 102.0]).__next__)
        a.snapshot()
        t = a.snapshot()["throughput"]
        self.assertIsNone(t["decode_tps"])
        self.assertIsNone(t["last_decode_tps"])
        self.assertAlmostEqual(t["decode_tps_gauge"], 88.28217315673828)


class ThroughputLevelsTest(unittest.TestCase):
    """Lifetime totals and a ~15 min rolling window over the histogram counters."""

    # values in the order self.metrics() writes them out
    NAMES = (
        "vllm:request_generation_tokens_sum",
        "vllm:request_decode_time_seconds_sum",
        "vllm:e2e_request_latency_seconds_sum",
        "vllm:request_queue_time_seconds_sum",
        "vllm:request_generation_tokens_count",
        "vllm:prompt_tokens_total",
        "radiance:attn_cached_tokens_total",
        "radiance:prefill_tokens_total",
        "radiance:engine_step_seconds_total",
        "radiance:engine_step_seconds_decode_total",
    )

    def metrics(self, *values):
        return "".join(f"{name} {v}\n" for name, v in zip(self.NAMES, values))

    def poll(self, metric_texts, times):
        """Poll one adapter len(times) times and return each throughput_levels block."""
        calls = {"n": 0}

        def fetch(path):
            if path == "/metrics":
                text = metric_texts[calls["n"]]
                calls["n"] += 1
                return text
            return {"/health": HEALTH, "/v1/models": MODELS, "/stats": STATS}[path]

        adapter_ = RadianceAdapter(fetch=fetch, clock=iter(times).__next__)
        return [adapter_.snapshot()["throughput_levels"] for _ in times]

    def test_lifetime_maths_from_fixture(self):
        lv = RadianceAdapter(fetch=fake_fetch(), clock=iter([100.0]).__next__).snapshot()[
            "throughput_levels"
        ]
        life = lv["lifetime"]
        self.assertAlmostEqual(life["active_decode_tps"], 74.08041565)  # 372771 tok / 5031.98 s
        self.assertAlmostEqual(life["effective_request_tps"], 42.74738494)  # / 8720.32 s e2e
        self.assertAlmostEqual(life["queue_share"], 0.19104183)  # 1665.95 s queued
        self.assertEqual(life["prompt_tokens"], 14144751.0)
        self.assertEqual(life["cached_prompt_tokens"], 12349260.0)
        self.assertEqual(life["new_prompt_tokens"], 1795491.0)
        self.assertAlmostEqual(life["cache_share"], 0.87306309)
        self.assertAlmostEqual(life["prefill_new_tps"], 1509.0001161)  # / (6757.81 - 5035.34) s
        self.assertEqual(life["requests"], 481.0)
        self.assertIn("Agents tab", lv["worker_note"])
        self.assertIn("approximate", lv["prefill_note"])

    def test_first_snapshot_window_is_empty(self):
        win = self.poll([METRICS], [100.0])[0]["window"]
        self.assertEqual(win["window_seconds"], 0.0)
        self.assertIsNone(win["active_decode_tps"])
        self.assertIsNone(win["effective_request_tps"])
        self.assertIsNone(win["cache_share"])
        self.assertEqual(win["requests"], 0.0)

    def test_window_maths_from_counter_pair(self):
        first = self.metrics(1000, 100, 200, 40, 10, 5000, 4000, 900, 300, 250)
        second = self.metrics(1400, 106, 210, 46, 14, 5500, 4400, 1000, 304, 252)
        win = self.poll([first, second], [100.0, 200.0])[1]["window"]
        self.assertEqual(win["window_seconds"], 100.0)
        self.assertAlmostEqual(win["active_decode_tps"], 400 / 6)
        self.assertAlmostEqual(win["effective_request_tps"], 40.0)
        self.assertAlmostEqual(win["queue_share"], 0.6)
        self.assertEqual(win["prompt_tokens"], 500.0)
        self.assertEqual(win["cached_prompt_tokens"], 400.0)
        self.assertEqual(win["new_prompt_tokens"], 100.0)
        self.assertAlmostEqual(win["cache_share"], 0.8)
        self.assertAlmostEqual(win["prefill_new_tps"], 100 / 2)  # mixed steps included
        self.assertEqual(win["requests"], 4.0)

    def test_zero_divisions_report_none(self):
        life = self.poll([self.metrics(0, 0, 0, 0, 0, 0, 0, 5, 7, 7)], [100.0])[0]["lifetime"]
        self.assertIsNone(life["active_decode_tps"])
        self.assertIsNone(life["effective_request_tps"])
        self.assertIsNone(life["queue_share"])
        self.assertIsNone(life["cache_share"])
        self.assertIsNone(life["prefill_new_tps"])  # step seconds cancel: no pure-prefill time
        self.assertEqual(life["prompt_tokens"], 0.0)
        self.assertEqual(life["new_prompt_tokens"], 0.0)
        self.assertEqual(life["requests"], 0.0)

    def test_missing_metrics_report_none(self):
        def fetch(path):
            if path == "/metrics":
                raise RuntimeError("boom")
            return {"/health": HEALTH, "/v1/models": MODELS, "/stats": STATS}[path]

        lv = RadianceAdapter(fetch=fetch, clock=iter([100.0, 102.0]).__next__).snapshot()[
            "throughput_levels"
        ]
        self.assertIsNone(lv["lifetime"]["active_decode_tps"])
        self.assertIsNone(lv["lifetime"]["requests"])
        self.assertIsNone(lv["window"]["window_seconds"])

    def test_counter_reset_restarts_the_baselines(self):
        before = self.metrics(1000, 100, 200, 40, 10, 5000, 4000, 900, 300, 250)
        restarted = self.metrics(500, 50, 100, 20, 5, 2500, 2000, 450, 150, 125)
        after = self.metrics(700, 54, 108, 24, 7, 2700, 2200, 500, 155, 130)
        snaps = self.poll([before, restarted, after], [100.0, 200.0, 300.0])
        win = snaps[1]["window"]  # counters went backwards: window is void
        self.assertEqual(win["window_seconds"], 0.0)
        self.assertIsNone(win["active_decode_tps"])
        self.assertEqual(win["requests"], 0.0)
        win = snaps[2]["window"]  # measured off the new baseline
        self.assertEqual(win["window_seconds"], 100.0)
        self.assertAlmostEqual(win["active_decode_tps"], 200 / 4)
        self.assertEqual(win["requests"], 2.0)
        self.assertAlmostEqual(snaps[2]["lifetime"]["active_decode_tps"], 700 / 54)

    def test_window_baseline_rollover(self):
        """Constant engine rates: gen 75 tok/s, decode 1 s/s, e2e 2 s/s, queue 0.4 s/s."""

        def metrics_at(t):
            return self.metrics(75 * t, t, 2 * t, 0.4 * t, t, 100 * t, 80 * t, 10 * t, 2 * t, 1.5 * t)

        snaps = self.poll(
            [metrics_at(t) for t in (0.0, 600.0, 1200.0, 1800.0)], [0.0, 600.0, 1200.0, 1800.0]
        )
        # young adapter: window grows against the very first snapshot
        self.assertEqual(snaps[1]["window"]["window_seconds"], 600.0)
        self.assertAlmostEqual(snaps[1]["window"]["active_decode_tps"], 75.0)
        self.assertEqual(snaps[2]["window"]["window_seconds"], 1200.0)
        # past 15 min the baseline slides to the sample nearest 15 min ago
        self.assertEqual(snaps[3]["window"]["window_seconds"], 1200.0)  # dropped t=0, base t=600
        self.assertAlmostEqual(snaps[3]["window"]["active_decode_tps"], 75.0)
        self.assertEqual(snaps[3]["window"]["requests"], 1200.0)
        self.assertAlmostEqual(snaps[3]["window"]["effective_request_tps"], 37.5)
        self.assertAlmostEqual(snaps[3]["window"]["queue_share"], 0.2)
        self.assertAlmostEqual(snaps[3]["window"]["cache_share"], 0.8)
        self.assertAlmostEqual(snaps[3]["window"]["prefill_new_tps"], 20.0)


class RouteTest(unittest.TestCase):
    def test_inference_route_lists_engines(self):
        class Dummy:
            def snapshot(self):
                return {"engine": "dummy", "status": "ready"}

        original = dict(ENGINES)
        ENGINES.clear()
        ENGINES["dummy"] = Dummy()
        try:
            self.assertIs(server.ROUTES["/api/inference"], routes.inference)
            self.assertEqual(routes.inference(), {"engines": [{"engine": "dummy", "status": "ready"}]})
        finally:
            ENGINES.clear()
            ENGINES.update(original)


if __name__ == "__main__":
    unittest.main()

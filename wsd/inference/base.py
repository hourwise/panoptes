"""Common interface for inference engine adapters.

Every adapter exposes ``snapshot()`` returning one plain-JSON dict with this
schema. Keys whose data an engine cannot provide are present but ``None`` (or
a dict of ``None``s for the grouped sections); ``capabilities`` says which
sections the engine could *ever* fill, so the UI can hide a section instead of
showing an empty one.

    id                  config id, e.g. "radiance-r9700"
    version             engine version, None when unreported
    uptime_s            seconds the engine has been up, None when unreported
    startup_s           engine startup duration, None when unreported
    label               display label from the config
    status              "ready" | "loading" | "down" | "error"
    version             engine version, None when unreported
    model               {id, state, max_model_len, reasoning_efforts,
                         vram_bytes, load_seconds}
    queue               {running, waiting, in_flight, queue_cap, max_num_seqs,
                         preemptions}
    throughput          rolling token rates {decode_tps, prefill_tps, gauges,
                         last_* values}
    throughput_levels   lifetime/window ratio tables (Radiance only for now)
    latency             {ttft, e2e, queue, itl} — each one histogram
                        {count, sum, mean, p50, p95} or None
    tokens              {prompt_total, generation_total, prefill_total,
                         decode_total, cached_total}
    cache               {prefix_hit_rate, linear_hit_rate, prefix_evictions,
                         kv_util, kv_tokens_used, kv_tokens_max}
    requests            per-request live rows (Radiance only)
    speculative         draft acceptance stats
    transfers           H2D/D2H/SSD byte counters and rates
    experts             MoE routing summary
    warnings            list[str] of anything odd seen this snapshot
    capabilities        dict of booleans, keys below

``latency``, ``queue``, ``tokens`` and ``cache`` are the contract between the
adapters and the UI: the key *names* are fixed, listed verbatim in ``SCHEMA``,
and every adapter emits every one of those sections with exactly those keys.
The UI indexes them literally (``latency.ttft.p50``), so an adapter that hands
back the engine's raw metric names gives a card whose capability reads true but
which renders empty. ``tests/test_schema_contract.py`` enforces the contract.
"""

CAPABILITIES = (
    "per_request",           # a live per-request table (requests)
    "speculative",           # draft acceptance stats
    "experts",               # MoE expert routing summary
    "transfers",             # H2D / D2H / SSD counters
    "latency_histograms",    # latency section from histogram metrics
    "throughput_levels",     # lifetime/window throughput tables
    "prefix_cache",          # a usable cache.prefix_hit_rate
    "lifecycle",             # load/unload visibility (model.state)
    "controls",              # dashboard may act on the engine (M3)
)


# section -> the exact keys that section must contain, in this order.
SCHEMA = {
    "latency": ("ttft", "e2e", "queue", "itl"),
    "queue": ("running", "waiting", "in_flight", "queue_cap", "max_num_seqs",
              "preemptions"),
    "tokens": ("prompt_total", "generation_total", "prefill_total",
               "decode_total", "cached_total"),
    "cache": ("prefix_hit_rate", "linear_hit_rate", "prefix_evictions",
              "kv_util", "kv_tokens_used", "kv_tokens_max"),
}

# The keys of one latency histogram. A SCHEMA["latency"] key holds either one of
# these dicts or None when the engine does not report that measurement.
LATENCY_STATS = ("count", "sum", "mean", "p50", "p95")


def empty_section(section: str) -> dict:
    """A snapshot section with exactly the SCHEMA keys, every value None."""
    return {key: None for key in SCHEMA[section]}


def empty_capabilities() -> dict:
    """All capabilities False; adapters flip the ones they support."""
    return {key: False for key in CAPABILITIES}


class EngineAdapter:
    """One engine the dashboard watches. Subclasses fill the schema above."""

    def __init__(self, engine_id: str = "", label: str = "", base_url: str = ""):
        self.engine_id = engine_id
        self.label = label or engine_id
        self.base_url = base_url.rstrip("/")

    def snapshot(self) -> dict:
        raise NotImplementedError

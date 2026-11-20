"""Panoptes Lab: a benchmark run of one variant against another (read-only).

The records are written by the supervisor's scorer, never by the dashboard: one
JSON object per line in
``<records_dir>/perf/bench.jsonl``, one line per task run, carrying the gate
result, the hidden-test summary, wall seconds, sampled energy (with peak VRAM
and RAM in bytes), a per-run ``engine`` block and harness token usage. A
missing figure stays ``None`` — nothing is estimated here.

Only tasks run by BOTH variants are compared: a task present in one variant is
reported, never scored against thin air. Every measurement keeps the direction
that makes it readable (a lower wall time or a higher pass rate is better) and
a ``within_noise`` flag: with the few runs a benchmark replay has, the honest
answer to "is this difference real?" is often "no", so a difference smaller
than the larger of the two run-to-run spreads — or measured from fewer than
two runs per side — is marked as not distinguishable from run-to-run variation.
"""

import json
from pathlib import Path
from statistics import fmean
from typing import Callable, NamedTuple

#: Where the bench records live below the records dir.
BENCH_PATH = ("perf", "bench.jsonl")

#: A measurement is better when the number goes up, or down.
HIGHER_IS_BETTER = "higher"
LOWER_IS_BETTER = "lower"

#: Every row is measured over one bench run (a task completion), not over the
#: engine's lifetime.
SCOPE_RUN = "run"


def _is_number(value: object) -> bool:
    """A real number: bools are gate verdicts, never measurements."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number(run: dict, key: str) -> int | float | None:
    value = run.get(key)
    return value if _is_number(value) else None


def _value_pass_rate(run: dict) -> float | None:
    """The gate verdict as a 0/1 value: the mean of those is the pass rate."""
    verdict = run.get("gate_pass")
    return None if not isinstance(verdict, bool) else (1.0 if verdict else 0.0)


def _value_hidden_rate(run: dict) -> int | float | None:
    hidden = run.get("hidden")
    if not isinstance(hidden, dict):
        return None
    return _number(hidden, "pass_rate")


def _value_total_energy(run: dict) -> int | float | None:
    """Wh used by the run: GPU plus CPU, whichever the sampler recorded.

    An ``energy`` table without any number (no ``.bench-samples.csv`` for that
    run) is a missing value, not a zero — the run cost something, we just do
    not know what.
    """
    energy = run.get("energy")
    if not isinstance(energy, dict):
        return None
    parts = [value for value in (_number(energy, "gpu_wh"), _number(energy, "cpu_wh")) if value is not None]
    return sum(parts) if parts else None


#: The energy table records peak memory in bytes; the table shows GB.
BYTES_PER_GB = 1024 ** 3


def _engine_value(key: str) -> Callable[[dict], int | float | None]:
    """One number from the per-run ``engine`` block.

    The block is computed from /metrics deltas over that run alone. A run
    without snapshots, or whose engine restarted mid-run, says nothing about
    throughput: ``available: false`` (or no block at all) is a missing value,
    never a zero.
    """
    def read(run: dict) -> int | float | None:
        engine = run.get("engine")
        if not isinstance(engine, dict) or not engine.get("available"):
            return None
        return _number(engine, key)

    return read


def _energy_gb_value(key: str) -> Callable[[dict], int | float | None]:
    """One peak-memory byte count from the energy table, reported in GB."""
    def read(run: dict) -> int | float | None:
        energy = run.get("energy")
        if not isinstance(energy, dict):
            return None
        value = _number(energy, key)
        return None if value is None else value / BYTES_PER_GB

    return read


def _usage_value(key: str) -> Callable[[dict], int | float | None]:
    def read(run: dict) -> int | float | None:
        usage = run.get("usage")
        return _number(usage, key) if isinstance(usage, dict) else None

    return read


class Measurement(NamedTuple):
    id: str
    label: str
    unit: str
    #: ``"higher"`` or ``"lower"``: which way is better.
    direction: str
    #: One run record -> its value for this measurement, or None when absent.
    value: Callable[[dict], int | float | None]


#: The side-by-side rows, in the order of the measurement table: engine
#: throughput first, then latency, completion time, memory and energy, and the
#: harness verdicts last. Names and units follow the perf sampler's vocabulary
#: (``total_energy_wh`` in ``perf/tasks.jsonl``) so the two screens agree.
MEASUREMENTS: tuple[Measurement, ...] = (
    Measurement("active_decode_tps", "active generation tok/s", "tok/s", HIGHER_IS_BETTER,
                _engine_value("active_decode_tps")),
    Measurement("effective_request_tps", "effective request tok/s", "tok/s", HIGHER_IS_BETTER,
                _engine_value("effective_request_tps")),
    Measurement("prefill_new_tps", "prefill new tok/s", "tok/s", HIGHER_IS_BETTER,
                _engine_value("prefill_new_tps")),
    Measurement("ttft_mean_s", "first-token latency", "s", LOWER_IS_BETTER, _engine_value("ttft_mean_s")),
    Measurement("queue_mean_s", "queue waiting", "s", LOWER_IS_BETTER, _engine_value("queue_mean_s")),
    Measurement("wall_seconds", "wall seconds", "s", LOWER_IS_BETTER, lambda run: _number(run, "wall_seconds")),
    Measurement("peak_ram_gb", "peak RAM", "GB", LOWER_IS_BETTER, _energy_gb_value("peak_ram")),
    Measurement("peak_vram_gb", "peak VRAM", "GB", LOWER_IS_BETTER, _energy_gb_value("peak_vram")),
    Measurement("total_energy_wh", "total energy", "Wh", LOWER_IS_BETTER, _value_total_energy),
    Measurement("gate_pass_rate", "gate pass rate", "ratio", HIGHER_IS_BETTER, _value_pass_rate),
    Measurement("hidden_pass_rate", "hidden-test pass rate", "ratio", HIGHER_IS_BETTER, _value_hidden_rate),
    Measurement("output_tokens", "output tokens", "tokens", LOWER_IS_BETTER, _usage_value("output")),
    Measurement("compactions", "compactions", "count", LOWER_IS_BETTER, _usage_value("compactions")),
    Measurement("peak_context", "peak context", "tokens", LOWER_IS_BETTER, _usage_value("peak_ctx")),
)


def load_runs(records_dir: str | None, warnings: list[str] | None = None) -> list[dict]:
    """Every bench record, file order; ``[]`` when there is no record file.

    A line has to be a JSON object naming both a ``task`` and a ``variant``, or
    it is skipped and — when a ``warnings`` list is handed in — counted there
    with its line number. A broken record costs itself; it never invents a run.
    """
    if not records_dir:
        return []
    path = Path(records_dir).joinpath(*BENCH_PATH)
    try:
        raw_lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    runs: list[dict] = []

    def skipped(index: int, reason: str) -> None:
        """One unusable record, counted for the caller's warnings."""
        if warnings is not None:
            warnings.append(f"{path.name} line {index}: {reason}, skipped")

    for index, line in enumerate(raw_lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            skipped(index, "not JSON")
            continue
        if not isinstance(record, dict):
            skipped(index, f"is {type(record).__name__}, not an object")
            continue
        if not isinstance(record.get("task"), str) or not record["task"]:
            skipped(index, "no task id")
            continue
        if not isinstance(record.get("variant"), str) or not record["variant"]:
            skipped(index, "no variant")
            continue
        runs.append(record)
    return runs


def variants(runs: list[dict]) -> list[dict]:
    """The variants in ``runs``: ``{"variant": name, "runs": count}``, by name."""
    counts: dict[str, int] = {}
    for run in runs:
        counts[run["variant"]] = counts.get(run["variant"], 0) + 1
    return [{"variant": name, "runs": counts[name]} for name in sorted(counts)]


def _by_task(runs: list[dict], variant: str) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for run in runs:
        if run["variant"] == variant:
            grouped.setdefault(run["task"], []).append(run)
    return grouped


def _stats(values: list[int | float]) -> dict:
    """n, mean, min and max of the values a variant actually reported."""
    if not values:
        return {"n": 0, "mean": None, "min": None, "max": None}
    return {"n": len(values), "mean": fmean(values), "min": min(values), "max": max(values)}


def _range(stats: dict) -> float | None:
    """The run-to-run spread of one variant, or None when it cannot be known."""
    if stats["n"] < 2:
        return None
    return stats["max"] - stats["min"]


def _compare_one(measurement: Measurement, base: dict, mod: dict, tasks: list[str]) -> dict:
    base_values = [
        value
        for task in tasks
        for run in base[task]
        for value in (measurement.value(run),)
        if value is not None
    ]
    mod_values = [
        value
        for task in tasks
        for run in mod[task]
        for value in (measurement.value(run),)
        if value is not None
    ]
    base_stats = _stats(base_values)
    mod_stats = _stats(mod_values)

    difference = None
    percent = None
    better = "tie"
    if base_stats["mean"] is not None and mod_stats["mean"] is not None:
        difference = mod_stats["mean"] - base_stats["mean"]
        if base_stats["mean"]:
            percent = round(difference / base_stats["mean"] * 100, 2)
        if difference:
            improved = difference > 0 if measurement.direction == HIGHER_IS_BETTER else difference < 0
            better = "modified" if improved else "baseline"

    # One run per side says nothing about run-to-run spread, so nothing can be
    # distinguished from noise; from two runs up, the bigger spread wins.
    base_spread, mod_spread = _range(base_stats), _range(mod_stats)
    within_noise = True
    if difference is not None and base_spread is not None and mod_spread is not None:
        within_noise = abs(difference) < max(base_spread, mod_spread)

    return {
        "id": measurement.id,
        "label": measurement.label,
        "unit": measurement.unit,
        "direction": measurement.direction,
        "scope": SCOPE_RUN,
        "baseline": base_stats,
        "modified": mod_stats,
        "difference": {"absolute": difference, "percent": percent},
        "better": better,
        "within_noise": within_noise,
    }


def compare(runs: list[dict], baseline_variant: str, modified_variant: str) -> dict:
    """Every measurement of ``modified_variant`` against ``baseline_variant``.

    Difference is always ``modified - baseline`` (percent of the baseline mean,
    ``None`` when that mean is zero or a side has no value at all), so the
    screen can colour a row from ``better`` alone: ``"baseline"``,
    ``"modified"`` or ``"tie"``, where a tie also covers the measurements that
    no side reported. Tasks only one variant ran are listed, not compared.
    """
    base = _by_task(runs, baseline_variant)
    mod = _by_task(runs, modified_variant)
    common = sorted(set(base) & set(mod))
    return {
        "baseline": baseline_variant,
        "modified": modified_variant,
        "runs": {"baseline": sum(len(task_runs) for task_runs in base.values()),
                 "modified": sum(len(task_runs) for task_runs in mod.values())},
        "tasks_compared": common,
        "tasks_only_in_baseline": sorted(set(base) - set(mod)),
        "tasks_only_in_modified": sorted(set(mod) - set(base)),
        "measurements": [_compare_one(measurement, base, mod, common) for measurement in MEASUREMENTS],
    }

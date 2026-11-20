"""Minimal Prometheus text-format parser and histogram quantiles.

``parse`` turns exposition text into ``{metric: [(labels, value), ...]}``.
Label values may contain commas, spaces and escaped quotes; ``NaN``, ``+Inf``
and ``-Inf`` values are kept as floats; an optional trailing timestamp is
ignored. ``histogram`` computes count/sum/mean/p50/p95 from the
``name_bucket`` / ``name_sum`` / ``name_count`` families, interpolating
linearly inside the bucket a quantile falls in.
"""

import math

_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:._")


def _parse_labels(text: str, start: int) -> tuple[dict, int]:
    """Parse ``{...}`` starting at ``text[start] == "{"``; return (labels, index past "}")."""
    labels: dict[str, str] = {}
    i = start + 1
    n = len(text)
    while i < n:
        while i < n and text[i] in " ,\t":
            i += 1
        if i >= n or text[i] == "}":
            break
        k = i
        while k < n and text[k] not in "=,}":
            k += 1
        key = text[i:k].strip()
        if k >= n or text[k] != "=" or not key:
            break
        k += 1
        while k < n and text[k] in " \t":
            k += 1
        if k < n and text[k] == '"':
            k += 1
            buf: list[str] = []
            while k < n:
                c = text[k]
                if c == "\\" and k + 1 < n:
                    buf.append(text[k + 1])
                    k += 2
                    continue
                if c == '"':
                    k += 1
                    break
                buf.append(c)
                k += 1
            labels[key] = "".join(buf)
            i = k
        else:
            k2 = k
            while k2 < n and text[k2] not in ",}":
                k2 += 1
            labels[key] = text[k:k2].strip()
            i = k2
    return labels, i + 1


def _parse_line(line: str) -> tuple[str, dict, float] | None:
    n = len(line)
    i = 0
    while i < n and line[i] in _NAME_CHARS:
        i += 1
    name = line[:i]
    if not name:
        return None
    labels: dict[str, str] = {}
    if i < n and line[i] == "{":
        labels, i = _parse_labels(line, i)
    rest = line[i:].split()
    if not rest:
        return None
    try:
        value = float(rest[0])
    except ValueError:
        return None
    return name, labels, value


def parse(text: str) -> dict[str, list[tuple[dict, float]]]:
    """Parse exposition text into metric name -> [(labels, value)]."""
    out: dict[str, list[tuple[dict, float]]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _parse_line(line)
        if parsed is None:
            continue
        name, labels, value = parsed
        out.setdefault(name, []).append((labels, value))
    return out


def _counter_total(samples) -> float | None:
    entries = samples or ()
    if not entries:
        return None
    return sum(value for _labels, value in entries)


def histogram(samples: dict, name: str) -> dict | None:
    """count/sum/mean/p50/p95 for the histogram ``name``; None when count is 0."""
    buckets: dict[float, float] = {}
    for labels, value in samples.get(name + "_bucket", ()):
        le = labels.get("le")
        if le is None:
            continue
        try:
            bound = float(le)
        except ValueError:
            continue
        if math.isnan(bound):
            continue
        buckets[bound] = buckets.get(bound, 0.0) + value

    count = _counter_total(samples.get(name + "_count"))
    if count is None:
        count = buckets.get(math.inf)
    if count is None or math.isnan(count) or count <= 0:
        return None

    total = _counter_total(samples.get(name + "_sum"))
    if total is not None and math.isnan(total):
        total = None
    mean = total / count if total is not None else None

    bounds = sorted(buckets)
    cums: list[float] = []
    running = 0.0
    for bound in bounds:
        running = max(running, buckets[bound])
        cums.append(running)

    def percentile(q: float) -> float | None:
        if not bounds:
            return None
        target = q * count
        prev_bound = 0.0
        prev_cum = 0.0
        for bound, cum in zip(bounds, cums):
            if cum >= target and cum > prev_cum:
                if math.isinf(bound):
                    return prev_bound
                return prev_bound + (bound - prev_bound) * (target - prev_cum) / (cum - prev_cum)
            prev_bound, prev_cum = bound, max(prev_cum, cum)
        finite = [b for b in bounds if not math.isinf(b)]
        return finite[-1] if finite else None

    return {
        "count": count,
        "sum": total,
        "mean": mean,
        "p50": percentile(0.5),
        "p95": percentile(0.95),
    }

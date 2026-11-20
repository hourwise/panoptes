"""CPU and memory readers.

Every reader takes a ``root`` path (default "/") so tests can point it at
fixture trees instead of the real /proc and /sys.
"""

import re
import threading
from pathlib import Path

_CPU_LINE = re.compile(r"^cpu(\d*)$")


def _read_file(root: str, rel: str) -> str | None:
    """Read a file under root, or None if it is missing or unreadable."""
    try:
        return Path(root, rel).read_text()
    except OSError:
        return None


def parse_proc_stat(text: str) -> dict[str, tuple[int, int]]:
    """Map each CPU line of /proc/stat to (busy_jiffies, total_jiffies).

    Busy = total - idle - iowait.
    """
    result: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if not _CPU_LINE.match(parts[0]):
            continue
        try:
            fields = [int(v) for v in parts[1:]]
        except ValueError:
            continue
        if len(fields) < 4:
            continue
        # user..steal only: guest/guest_nice are already counted in user/nice.
        total = sum(fields[:8])
        idle = fields[3]
        iowait = fields[4] if len(fields) > 4 else 0
        result[parts[0]] = (total - idle - iowait, total)
    return result


def _cpu_number(name: str) -> int:
    return int(name[3:]) if len(name) > 3 else -1


def cpu_utilisation(
    prev: dict[str, tuple[int, int]] | None,
    cur: dict[str, tuple[int, int]] | None,
) -> dict[str, float | None]:
    """Percent busy per CPU between two parse_proc_stat results.

    None where the total did not increase (or the CPU is new).
    """
    result: dict[str, float | None] = {}
    for name in sorted(cur or {}, key=_cpu_number):
        busy, total = cur[name]
        entry = (prev or {}).get(name)
        if entry is None:
            result[name] = None
            continue
        prev_busy, prev_total = entry
        d_total = total - prev_total
        if d_total <= 0:
            result[name] = None
        else:
            result[name] = round(100.0 * (busy - prev_busy) / d_total, 1)
    return result


def read_cpu_freqs_mhz(root: str = "/") -> list[float | None]:
    """Current scaling frequency per CPU in MHz, sorted by CPU number.

    None for CPUs whose scaling_cur_freq is missing or unreadable.
    """
    base = Path(root, "sys/devices/system/cpu")
    freqs: list[float | None] = []
    try:
        entries = list(base.iterdir())
    except OSError:
        return freqs
    numbers = sorted(
        int(m.group(1)) for e in entries if (m := re.fullmatch(r"cpu(\d+)", e.name))
    )
    for n in numbers:
        text = _read_file(root, f"sys/devices/system/cpu/cpu{n}/cpufreq/scaling_cur_freq")
        try:
            freqs.append(round(int(text) / 1000.0, 1))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            freqs.append(None)
    return freqs


def read_cpu_topology(root: str = "/") -> dict:
    """Physical CPU layout: model name, core count, thread count, packages.

    Threads are the cpu<N> directories exposing topology/core_id, cores the
    distinct (physical_package_id, core_id) pairs across them and packages
    the distinct physical_package_id values among them. Missing files give
    None for that field.
    """
    model = None
    text = _read_file(root, "proc/cpuinfo")
    if text is not None:
        for line in text.splitlines():
            if line.startswith("model name"):
                model = line.partition(":")[2].strip()
                break

    threads = cores = packages = None
    base = Path(root, "sys/devices/system/cpu")
    try:
        entries = list(base.iterdir())
    except OSError:
        entries = []
        seen_cpu_dir = False
    else:
        seen_cpu_dir = True
    if seen_cpu_dir:
        threads = 0
        core_keys: set[tuple[str | None, str]] = set()
        package_ids: set[str] = set()
        for entry in entries:
            if not re.fullmatch(r"cpu\d+", entry.name):
                continue
            core_id = _read_file(
                root, f"sys/devices/system/cpu/{entry.name}/topology/core_id"
            )
            if core_id is None:
                continue
            threads += 1
            pkg_text = _read_file(
                root, f"sys/devices/system/cpu/{entry.name}/topology/physical_package_id"
            )
            pkg = pkg_text.strip() if pkg_text is not None else None
            core_keys.add((pkg, core_id.strip()))
            if pkg is not None:
                package_ids.add(pkg)
        cores = len(core_keys)
        packages = len(package_ids)

    return {"model": model, "cores": cores, "threads": threads, "packages": packages}


def parse_meminfo(text: str) -> dict:
    """Extract memory figures in bytes from /proc/meminfo content."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    swap_total = values.get("SwapTotal", 0)
    swap_used = swap_total - values.get("SwapFree", 0)
    return {
        "total": total,
        "available": available,
        "used": total - available,
        "swap_total": swap_total,
        "swap_used": swap_used,
    }


class CpuSampler:
    """Computes CPU utilisation deltas between successive sample() calls.

    Thread-safe: a lock keeps concurrent readers from interleaving the
    read-and-update of the previous sample. The CPU topology is read once
    here because it never changes while the process runs.
    """

    def __init__(self, root: str = "/") -> None:
        self.root = root
        self.topology = read_cpu_topology(root)
        text = _read_file(root, "proc/stat")
        self._prev = parse_proc_stat(text) if text is not None else None
        self._lock = threading.Lock()

    def sample(self) -> dict:
        with self._lock:
            text = _read_file(self.root, "proc/stat")
            cur = parse_proc_stat(text) if text is not None else None
            util = cpu_utilisation(self._prev, cur)
            self._prev = cur
        freqs = read_cpu_freqs_mhz(self.root)
        valid = [f for f in freqs if f is not None]
        return {
            "overall": util.get("cpu"),
            "per_cpu": [util[name] for name in util if name != "cpu"],
            "freq_mhz": {
                "min": round(min(valid), 1) if valid else None,
                "max": round(max(valid), 1) if valid else None,
                "avg": round(sum(valid) / len(valid), 1) if valid else None,
                "per_cpu": freqs,
            },
        }


def read_system(root: str, sampler: CpuSampler) -> dict:
    """Full system snapshot: cpu, memory, load_avg, uptime_s, topology.

    A missing file yields None for that part.
    """
    text = _read_file(root, "proc/meminfo")
    memory = parse_meminfo(text) if text is not None else None

    text = _read_file(root, "proc/loadavg")
    try:
        load_avg = [float(v) for v in text.split()[:3]]  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        load_avg = None

    text = _read_file(root, "proc/uptime")
    try:
        uptime_s = float(text.split()[0])  # type: ignore[union-attr]
    except (AttributeError, ValueError, IndexError):
        uptime_s = None

    return {
        "cpu": sampler.sample(),
        "memory": memory,
        "load_avg": load_avg,
        "uptime_s": uptime_s,
        "topology": sampler.topology,
    }

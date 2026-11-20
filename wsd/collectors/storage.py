"""Storage collectors: mounts, filesystem usage, disk throughput and busy time.

Every reader takes a ``root`` path (default "/") so tests can point it at
fixture trees instead of the real /proc and /sys. Missing values are always
None, never estimated.
"""

import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

# The kernel reports diskstats sectors as always 512 bytes, regardless of the
# device's physical sector size; /sys/block/<name>/size uses the same unit.
_SECTOR_BYTES = 512

_REAL_FSTYPES = frozenset(
    {
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "vfat",
        "exfat",
        "ntfs",
        "ntfs3",
        "fuseblk",
        "zfs",
        "f2fs",
    }
)

_WHOLE_DISK = re.compile(r"(?:sd[a-z]+|nvme\d+n\d+|vd[a-z]+|mmcblk\d+)\Z")
_PARTITION = re.compile(r"(sd[a-z]+|vd[a-z]+|nvme\d+n\d+|mmcblk\d+)p?\d+\Z")
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")

# /proc/diskstats field indexes (1-based in the docs, 0-based here).
_F_SECTORS_READ = 5
_F_SECTORS_WRITTEN = 9
_F_IO_TICKS = 12


def _read_file(root: str, rel: str) -> str | None:
    """Read a file under root, or None if it is missing or unreadable."""
    try:
        return Path(root, rel).read_text()
    except OSError:
        return None


def _read_stripped(root: str, rel: str) -> str | None:
    text = _read_file(root, rel)
    return None if text is None else text.strip()


def _read_int(root: str, rel: str) -> int | None:
    text = _read_stripped(root, rel)
    try:
        return int(text)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _unescape_mount(path: str) -> str:
    """Decode the octal escapes /proc/mounts uses in paths (e.g. ``\\040``)."""
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), path)


def parse_mounts(text: str) -> list[dict]:
    """Real block-device mounts from /proc/mounts content.

    Pseudo filesystems (proc, tmpfs, overlay, squashfs snaps, ...) and network
    mounts are dropped, as are devices that are not /dev/ entries. One entry
    per device: the first mountpoint wins for bind-mounted devices.
    """
    result: list[dict] = []
    seen: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mountpoint, fstype = parts[0], parts[1], parts[2]
        if fstype not in _REAL_FSTYPES or not device.startswith("/dev/"):
            continue
        if device in seen:
            continue
        seen.add(device)
        result.append(
            {
                "device": device,
                "mountpoint": _unescape_mount(mountpoint),
                "fstype": fstype,
            }
        )
    return result


def fs_usage(path: str) -> dict | None:
    """Filesystem usage at ``path`` in bytes, or None if it is unreadable."""
    try:
        stats = os.statvfs(path)
    except OSError:
        return None
    block = stats.f_frsize
    return {
        "total": stats.f_blocks * block,
        "used": (stats.f_blocks - stats.f_bfree) * block,
        "free": stats.f_bavail * block,
    }


def parse_diskstats(text: str) -> dict[str, dict]:
    """Whole-disk I/O counters from /proc/diskstats content, keyed by name.

    Partitions (sda1, nvme0n1p2, mmcblk0p1) and other block devices (loop,
    dm, sr) are skipped so rates are not double counted.
    """
    result: dict[str, dict] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or not _WHOLE_DISK.match(parts[2]):
            continue
        try:
            fields = [int(v) for v in parts[3:]]
        except ValueError:
            continue
        if len(fields) < _F_IO_TICKS - 2:
            continue
        result[parts[2]] = {
            "sectors_read": fields[_F_SECTORS_READ - 3],
            "sectors_written": fields[_F_SECTORS_WRITTEN - 3],
            "io_ticks_ms": fields[_F_IO_TICKS - 3],
        }
    return result


def read_block_info(root: str, name: str) -> dict:
    """Static sysfs info for one whole disk under ``root/sys/block``."""
    base = f"sys/block/{name}"
    size_sectors = _read_int(root, f"{base}/size")
    rotational = _read_stripped(root, f"{base}/queue/rotational")
    return {
        "model": _read_stripped(root, f"{base}/device/model"),
        "size_bytes": None if size_sectors is None else size_sectors * _SECTOR_BYTES,
        "rotational": None if rotational is None else rotational == "1",
    }


def parent_disk(device: str) -> str | None:
    """Whole-disk name a block device belongs to, or None if not derivable.

    ``/dev/nvme0n1p2`` -> ``nvme0n1``, ``/dev/sda1`` -> ``sda``, a whole disk
    maps to itself, ``/dev/mapper/*`` and friends map to None.
    """
    if not device.startswith("/dev/"):
        return None
    name = device[len("/dev/"):]
    if _WHOLE_DISK.match(name):
        return name
    match = _PARTITION.match(name)
    return None if match is None else match.group(1)


def _delta(current: int, previous: int) -> int | None:
    """Counter delta, or None when the counter went backwards (device reset)."""
    value = current - previous
    return value if value >= 0 else None


def _rates(previous: dict | None, current: dict, elapsed_ms: float) -> dict:
    """Per-disk throughput and busy percentage for one sampling interval."""
    if previous is None or elapsed_ms <= 0:
        return {"read_bps": None, "write_bps": None, "busy_pct": None}
    read = _delta(current["sectors_read"], previous["sectors_read"])
    write = _delta(current["sectors_written"], previous["sectors_written"])
    busy = _delta(current["io_ticks_ms"], previous["io_ticks_ms"])
    elapsed_s = elapsed_ms / 1000.0
    return {
        "read_bps": None if read is None else round(read * _SECTOR_BYTES / elapsed_s, 1),
        "write_bps": None if write is None else round(write * _SECTOR_BYTES / elapsed_s, 1),
        "busy_pct": None
        if busy is None
        else round(min(100.0, busy / elapsed_ms * 100.0), 1),
    }


class DiskSampler:
    """Computes per-disk I/O deltas between successive sample() calls.

    Thread-safe: a lock keeps concurrent readers from interleaving the
    read-and-update of the previous sample.
    """

    def __init__(self, root: str = "/", clock: Callable[[], float] = time.monotonic) -> None:
        self.root = root
        self._clock = clock
        self._prev: tuple[dict[str, dict], float] | None = self._read()
        self._lock = threading.Lock()

    def _read(self) -> tuple[dict[str, dict], float] | None:
        text = _read_file(self.root, "proc/diskstats")
        if text is None:
            return None
        return parse_diskstats(text), self._clock()

    def sample(self) -> dict[str, dict]:
        """Rates per disk; all values None until a second sample exists."""
        with self._lock:
            current = self._read()
            result: dict[str, dict] = {}
            if current is not None:
                stats, now = current
                if self._prev is None:
                    previous, previous_now = None, now
                else:
                    previous, previous_now = self._prev
                elapsed_ms = (now - previous_now) * 1000.0
                for name, values in stats.items():
                    result[name] = _rates(previous.get(name), values, elapsed_ms)
            self._prev = current
            return result


def read_storage(root: str, sampler: DiskSampler) -> dict:
    """Full storage snapshot: disks with rates, and mounted filesystems."""
    rates = sampler.sample()
    disks = [
        {"name": name, **read_block_info(root, name), **rates[name]}
        for name in sorted(rates)
    ]

    text = _read_file(root, "proc/mounts")
    filesystems = []
    for mount in parse_mounts(text) if text is not None else []:
        usage = fs_usage(mount["mountpoint"]) or {}
        filesystems.append(
            {
                **mount,
                "disk": parent_disk(mount["device"]),
                "total": usage.get("total"),
                "used": usage.get("used"),
                "free": usage.get("free"),
            }
        )
    return {"disks": disks, "filesystems": filesystems}

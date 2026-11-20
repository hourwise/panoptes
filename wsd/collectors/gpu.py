"""GPU and PCIe collectors (drm sysfs, plus nvidia-smi when available).

Every reader takes a ``root`` path (default "/") so tests can point it at
fixture trees. Missing values are always None, never estimated.
"""

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

_CARD_DIR = re.compile(r"^card\d+$")
_PCI_SLOT = re.compile(r"^PCI_SLOT_NAME=(\S+)", re.MULTILINE)
_PCI_ADDR = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")
_PCI_ROOT = re.compile(r"^pci[0-9a-fA-F]{4}:[0-9a-fA-F]{2}$")
_LINK_NOTE = "links may downshift when idle; check under load"
_DPM_CURRENT = re.compile(r"^\s*\d+:\s*(\d+)\s*[Mm][Hh][Zz].*\*")

_VENDORS = {"0x1002": "amd", "0x10de": "nvidia"}
_LINK_GENS = {2.5: 1, 5.0: 2, 8.0: 3, 16.0: 4, 32.0: 5, 64.0: 6}

NVIDIA_SMI_ARGS = [
    "nvidia-smi",
    "--query-gpu=pci.bus_id,name,utilization.gpu,memory.used,memory.total,"
    "temperature.gpu,power.draw,clocks.sm,clocks.mem,pcie.link.gen.current,"
    "pcie.link.width.current",
    "--format=csv,noheader,nounits",
]

AMD_SMI_ARGS = ["amd-smi", "static", "--asic", "--json"]

_name_cache: dict[str, str | None] = {}


def _read_file(root: str, rel: str) -> str | None:
    try:
        return Path(root, rel).read_text()
    except OSError:
        return None


def _read_stripped(root: str, rel: str) -> str | None:
    text = _read_file(root, rel)
    if text is None:
        return None
    return text.strip()


def _read_int(root: str, rel: str) -> int | None:
    text = _read_stripped(root, rel)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def _first(value: int | None, fallback: int | None) -> int | None:
    return value if value is not None else fallback


def link_gen(speed_text: str | None) -> int | None:
    """PCIe generation from a sysfs link speed string ("16.0 GT/s PCIe")."""
    if not speed_text:
        return None
    try:
        value = float(speed_text.split()[0])
    except (ValueError, IndexError):
        return None
    return _LINK_GENS.get(value)


def parse_dpm(text: str | None) -> int | None:
    """Current clock in MHz from a pp_dpm_* file (the line marked with *)."""
    if not text:
        return None
    for line in text.splitlines():
        match = _DPM_CURRENT.match(line)
        if match:
            return int(match.group(1))
    return None


def _read_link_at(base: str) -> dict:
    """Current and max link values from a PCIe device directory."""
    return {
        "gen": link_gen(_read_stripped(base, "current_link_speed")),
        "width": _read_int(base, "current_link_width"),
        "max_gen": link_gen(_read_stripped(base, "max_link_speed")),
        "max_width": _read_int(base, "max_link_width"),
    }


def _read_chain(root: str, device_rel: str) -> list[dict]:
    """PCIe hops from the GPU endpoint up to the root port, endpoint first.

    Resolves the sysfs device symlink and walks up while the directory name
    looks like a PCI address. Only hops with a current_link_speed become
    entries. Empty list when the path is not rooted at a PCI device.
    """
    chain = []
    path = os.path.realpath(os.path.join(root, device_rel))
    first = True
    while _PCI_ADDR.fullmatch(os.path.basename(path)):
        parent = os.path.dirname(path)
        if first:
            role = "endpoint"
        elif _PCI_ROOT.fullmatch(os.path.basename(parent)):
            role = "root_port"
        else:
            role = "bridge"
        if _read_stripped(path, "current_link_speed") is not None:
            chain.append({"slot": os.path.basename(path), "role": role,
                          **_read_link_at(path)})
        path = parent
        first = False
    return chain


def _min_opt(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _host_link(chain: list[dict]) -> dict | None:
    """Link between the root port and the device directly below it."""
    rp_index = None
    for index, hop in enumerate(chain):
        if hop["role"] == "root_port":
            rp_index = index
            break
    if rp_index is None or rp_index == 0:
        return None
    below = chain[rp_index - 1]
    port = chain[rp_index]
    return {
        "gen": below["gen"],
        "width": below["width"],
        "max_gen": _min_opt(port["max_gen"], below["max_gen"]),
        "max_width": _min_opt(port["max_width"], below["max_width"]),
    }


def _bottleneck(chain: list[dict]) -> dict | None:
    """Lowest current gen and width across the chain, with the limiting slot."""
    gens = [hop["gen"] for hop in chain if hop["gen"] is not None]
    widths = [hop["width"] for hop in chain if hop["width"] is not None]
    if not gens and not widths:
        return None
    gen = min(gens) if gens else None
    width = min(widths) if widths else None
    slot = None
    for hop in chain:
        if gen is not None and hop["gen"] == gen:
            slot = hop["slot"]
            break
    if slot is None:
        for hop in chain:
            if width is not None and hop["width"] == width:
                slot = hop["slot"]
                break
    return {"gen": gen, "width": width, "slot": slot}


def _below_max(host_link: dict | None) -> bool:
    if host_link is None:
        return False
    if (host_link["gen"] is not None and host_link["max_gen"] is not None
            and host_link["gen"] < host_link["max_gen"]):
        return True
    return (host_link["width"] is not None
            and host_link["max_width"] is not None
            and host_link["width"] < host_link["max_width"])


def _amd_market_name(slot: str, run) -> str | None:
    """Market name for the GPU at `slot` from amd-smi, None when unavailable.

    Matches on the entry's bus id when the JSON carries one; the installed
    amd-smi version does not, so a single-GPU listing is used as-is.
    """
    try:
        result = run(AMD_SMI_ARGS, capture_output=True, text=True, timeout=3)
        if result.returncode != 0:
            return None
        entries = json.loads(result.stdout).get("gpu_data", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    target = slot.lower()[-7:]
    for entry in entries:
        asic = entry.get("asic") or {}
        bus_id = str(entry.get("bus_id") or asic.get("bus_id") or "").lower()
        market = asic.get("market_name") or asic.get("MARKET_NAME")
        if market and bus_id and bus_id[-7:] == target:
            return market
    if len(entries) == 1:
        asic = entries[0].get("asic") or {}
        return asic.get("market_name") or asic.get("MARKET_NAME")
    return None


def lookup_name(slot: str, run=subprocess.run) -> str | None:
    """Human GPU name "<vendor> <device>" from lspci, cached per slot.

    lspci prints `"Device 7551"` for recent AMD cards; those names are
    replaced with the market name from amd-smi when it answers.
    """
    if slot in _name_cache:
        return _name_cache[slot]
    name = None
    try:
        result = run(
            ["lspci", "-mm", "-s", slot],
            capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            fields = shlex.split(result.stdout.strip())
            if len(fields) >= 4:
                name = f"{fields[2]} {fields[3]}"
                if ("Device " in name
                        and ("AMD" in fields[2] or "ATI" in fields[2])):
                    name = _amd_market_name(slot, run) or name
    except (OSError, ValueError, subprocess.SubprocessError):
        name = None
    _name_cache[slot] = name
    return name


def _read_hwmon(root: str, device_rel: str) -> tuple[dict, float | None,
                                                     int | None, int | None]:
    """(temps_c, power_w, sclk_mhz, mclk_mhz) from device/hwmon/hwmon*/."""
    temps: dict[str, float] = {}
    power_w = None
    freqs: dict[str, int] = {}
    try:
        hwmons = sorted(Path(root, device_rel, "hwmon").glob("hwmon*"))
    except OSError:
        hwmons = []
    if not hwmons:
        return temps, None, None, None
    hm = hwmons[0]
    rel = str(hm.relative_to(Path(root)))
    for number in range(1, 10):
        raw = _read_int(root, f"{rel}/temp{number}_input")
        if raw is None:
            continue
        label = _read_stripped(root, f"{rel}/temp{number}_label") or f"temp{number}"
        temps[label] = raw / 1000.0
    power_uw = _read_int(root, f"{rel}/power1_average")
    if power_uw is not None:
        power_w = power_uw / 1_000_000.0
    for number in (1, 2):
        raw = _read_int(root, f"{rel}/freq{number}_input")
        label = _read_stripped(root, f"{rel}/freq{number}_label")
        if raw is not None and label in ("sclk", "mclk"):
            freqs[label] = raw // 1_000_000
    return temps, power_w, freqs.get("sclk"), freqs.get("mclk")


def read_drm_gpus(root: str = "/") -> list[dict]:
    """One dict per GPU in sysfs/class/drm, sorted by PCI slot."""
    gpus = []
    try:
        entries = sorted(Path(root, "sys", "class", "drm").iterdir())
    except OSError:
        return gpus
    for entry in entries:
        if not _CARD_DIR.fullmatch(entry.name):
            continue
        device_rel = f"sys/class/drm/{entry.name}/device"
        vendor = _VENDORS.get(_read_stripped(root, f"{device_rel}/vendor") or "")
        if vendor is None:
            continue
        uevent = _read_file(root, f"{device_rel}/uevent") or ""
        slot_match = _PCI_SLOT.search(uevent)
        slot = slot_match.group(1) if slot_match else None
        temps, power_w, freq_sclk, freq_mclk = _read_hwmon(root, device_rel)
        endpoint = _read_link_at(str(Path(root, device_rel)))
        chain = _read_chain(root, device_rel)
        host = _host_link(chain)
        bottleneck = _bottleneck(chain)
        below_max = _below_max(host)
        effective = host if host is not None else endpoint
        gpus.append({
            "id": slot,
            "vendor": vendor,
            "device_id": _read_stripped(root, f"{device_rel}/device"),
            "name": lookup_name(slot) if slot else None,
            "util_pct": _read_int(root, f"{device_rel}/gpu_busy_percent"),
            "vram_used": _read_int(root, f"{device_rel}/mem_info_vram_used"),
            "vram_total": _read_int(root, f"{device_rel}/mem_info_vram_total"),
            "temps_c": temps,
            "power_w": power_w,
            "sclk_mhz": _first(
                parse_dpm(_read_file(root, f"{device_rel}/pp_dpm_sclk")),
                freq_sclk),
            "mclk_mhz": _first(
                parse_dpm(_read_file(root, f"{device_rel}/pp_dpm_mclk")),
                freq_mclk),
            "pcie": {
                "gen": effective["gen"],
                "width": effective["width"],
                "max_gen": effective["max_gen"],
                "max_width": effective["max_width"],
                "endpoint": endpoint,
                "chain": chain,
                "host_link": host,
                "bottleneck": bottleneck,
                "below_max": below_max,
                "note": _LINK_NOTE if below_max else None,
            },
            "source": "sysfs",
        })
    gpus.sort(key=lambda gpu: gpu["id"] or "")
    return gpus


def _smi_number(text: str) -> float | None:
    text = text.strip()
    if text in ("", "[N/A]", "[Not Supported]", "[Not Available]", "N/A"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _smi_int(text: str) -> int | None:
    value = _smi_number(text)
    return int(value) if value is not None else None


def _smi_bytes(text: str) -> int | None:
    """MiB from nvidia-smi to bytes."""
    value = _smi_number(text)
    return int(value * 1024 * 1024) if value is not None else None


def parse_nvidia_smi(csv_text: str) -> list[dict]:
    """Rows of the NVIDIA_SMI_ARGS query output as GPU-shaped dicts."""
    rows = []
    for line in csv_text.splitlines():
        parts = line.split(",")
        if len(parts) != 11:
            continue
        temp = _smi_number(parts[5])
        rows.append({
            "id": parts[0].strip().lower()[-12:],
            "name": parts[1].strip() or None,
            "util_pct": _smi_number(parts[2]),
            "vram_used": _smi_bytes(parts[3]),
            "vram_total": _smi_bytes(parts[4]),
            "temps_c": {"gpu": temp} if temp is not None else {},
            "power_w": _smi_number(parts[6]),
            "sclk_mhz": _smi_int(parts[7]),
            "mclk_mhz": _smi_int(parts[8]),
            "pcie": {
                "gen": _smi_int(parts[9]),
                "width": _smi_int(parts[10]),
                "max_gen": None,
                "max_width": None,
            },
        })
    return rows


def _merge(gpu: dict, row: dict) -> None:
    """Fill None fields of a sysfs GPU from a matching nvidia-smi row."""
    for field in ("name", "util_pct", "vram_used", "vram_total",
                  "power_w", "sclk_mhz", "mclk_mhz"):
        if gpu[field] is None and row[field] is not None:
            gpu[field] = row[field]
    for label, value in row["temps_c"].items():
        gpu["temps_c"].setdefault(label, value)
    for key in ("gen", "width", "max_gen", "max_width"):
        if gpu["pcie"][key] is None and row["pcie"][key] is not None:
            gpu["pcie"][key] = row["pcie"][key]
    gpu["source"] = "sysfs+nvidia-smi"


def read_gpus(root: str = "/", run=subprocess.run) -> list[dict]:
    """Sysfs GPUs with nvidia-smi filling any gaps on NVIDIA cards."""
    gpus = read_drm_gpus(root)
    if not any(gpu["vendor"] == "nvidia" for gpu in gpus):
        return gpus
    try:
        result = run(NVIDIA_SMI_ARGS, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return gpus
    if result.returncode != 0:
        return gpus
    for row in parse_nvidia_smi(result.stdout or ""):
        for gpu in gpus:
            if gpu["id"] and gpu["id"].lower()[-12:] == row["id"]:
                _merge(gpu, row)
    return gpus

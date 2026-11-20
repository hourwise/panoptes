import atexit
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from wsd import routes  # noqa: F401  registers /api/gpus
from wsd.collectors import gpu
from wsd.server import make_server

FIXTURE_DIR = Path(__file__).parent / "fixtures"

#: Each sysfs fixture links a card to its device directory the way sysfs does,
#: and that link is what lets the PCIe chain be read through the parent bridges.
#: A published tree carries no symbolic links at all (tools/export_public.py
#: never copies one), so the link is recreated here instead of being part of the
#: files: the tests then see the same tree on either checkout.
SYSFS_LINKS = {
    "gpu": ("sys/class/drm/card2/device",
            "../../../devices/pci0000:9b/0000:9b:01.1/0000:9c:00.0"
            "/0000:9d:00.0/0000:9e:00.0"),
    "gpu-slowlink": ("sys/class/drm/card0/device",
                     "../../../devices/pci0000:aa/0000:aa:01.1"
                     "/0000:ab:00.0/0000:ac:00.0"),
}


def sysfs_tree(name: str, where: Path) -> Path:
    """A private copy of one sysfs fixture, with its `device` link in place."""
    root = where / name
    shutil.copytree(FIXTURE_DIR / name, root)
    rel, target = SYSFS_LINKS[name]
    device = root / rel
    if device.is_symlink():
        device.unlink()
    elif device.is_dir():
        shutil.rmtree(device)
    device.parent.mkdir(parents=True, exist_ok=True)
    device.symlink_to(target)
    return root


#: Whole-module copies: the link has to exist before any test reads the tree,
#: and they go away with the process.
_SYSFS = tempfile.TemporaryDirectory(prefix="panoptes-sysfs-")
atexit.register(_SYSFS.cleanup)
FIXTURES = sysfs_tree("gpu", Path(_SYSFS.name))
SLOWLINK_FIXTURES = sysfs_tree("gpu-slowlink", Path(_SYSFS.name))

NVIDIA_SMI_CSV = (
    "00000000:51:00.0, NVIDIA GeForce GTX 1050, 33, 512, 2048, 45,"
    " 28.5, 1392, 3505, 3, 16\n"
)


def fake_run(stdout, returncode=0):
    def run(cmd, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
    return run


class LinkGenTest(unittest.TestCase):
    def test_known_speeds(self):
        cases = {"2.5": 1, "5.0": 2, "8.0": 3, "16.0": 4, "32.0": 5, "64.0": 6}
        for speed, gen in cases.items():
            self.assertEqual(gpu.link_gen(f"{speed} GT/s PCIe"), gen)

    def test_unparseable(self):
        self.assertIsNone(gpu.link_gen("nope"))
        self.assertIsNone(gpu.link_gen(""))
        self.assertIsNone(gpu.link_gen(None))
        self.assertIsNone(gpu.link_gen("12.0 GT/s PCIe"))


class ParseDpmTest(unittest.TestCase):
    def test_current_line(self):
        text = "0: 500Mhz \n1: 2350Mhz *\n2: 3000Mhz \n"
        self.assertEqual(gpu.parse_dpm(text), 2350)

    def test_no_star(self):
        self.assertIsNone(gpu.parse_dpm("0: 500Mhz \n1: 3000Mhz \n"))
        self.assertIsNone(gpu.parse_dpm(""))
        self.assertIsNone(gpu.parse_dpm(None))


class ReadDrmGpusTest(unittest.TestCase):
    def setUp(self):
        self._real = gpu.lookup_name
        gpu.lookup_name = lambda slot: f"Vendor For {slot}"

    def tearDown(self):
        gpu.lookup_name = self._real

    def test_amd_card(self):
        gpus = gpu.read_drm_gpus(str(FIXTURES))
        self.assertEqual([g["id"] for g in gpus],
                         ["0000:05:00.0", "0000:51:00.0", "0000:9e:00.0"])
        amd = gpus[0]
        self.assertEqual(amd["vendor"], "amd")
        self.assertEqual(amd["device_id"], "0x7551")
        self.assertEqual(amd["name"], "Vendor For 0000:05:00.0")
        self.assertEqual(amd["util_pct"], 42)
        self.assertEqual(amd["vram_used"], 8589934592)
        self.assertEqual(amd["vram_total"], 34359738368)
        self.assertEqual(amd["temps_c"], {"edge": 61.0, "junction": 71.0})
        self.assertEqual(amd["power_w"], 77.0)
        self.assertEqual(amd["sclk_mhz"], 2350)
        self.assertEqual(amd["mclk_mhz"], 1258)
        self.assertEqual(
            amd["pcie"],
            {"gen": 4, "width": 16, "max_gen": 4, "max_width": 16,
             "endpoint": {"gen": 4, "width": 16, "max_gen": 4, "max_width": 16},
             "chain": [], "host_link": None, "bottleneck": None,
             "below_max": False, "note": None})
        self.assertEqual(amd["source"], "sysfs")

    def test_nouveau_card(self):
        card = gpu.read_drm_gpus(str(FIXTURES))[1]
        self.assertEqual(card["vendor"], "nvidia")
        self.assertEqual(card["device_id"], "0x1c81")
        self.assertIsNone(card["util_pct"])
        self.assertIsNone(card["vram_used"])
        self.assertIsNone(card["vram_total"])
        self.assertEqual(card["temps_c"], {"temp1": 36.0})
        self.assertIsNone(card["power_w"])
        self.assertIsNone(card["sclk_mhz"])
        self.assertIsNone(card["mclk_mhz"])
        self.assertEqual(
            card["pcie"],
            {"gen": 1, "width": 16, "max_gen": 3, "max_width": 16,
             "endpoint": {"gen": 1, "width": 16, "max_gen": 3, "max_width": 16},
             "chain": [], "host_link": None, "bottleneck": None,
             "below_max": False, "note": None})

    def test_connector_dirs_ignored(self):
        self.assertEqual(len(gpu.read_drm_gpus(str(FIXTURES))), 3)

    def test_missing_hwmon_gives_none_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "gpu"
            shutil.copytree(FIXTURES, root)
            device = root / "sys" / "class" / "drm" / "card0" / "device"
            shutil.rmtree(device / "hwmon")
            (device / "pp_dpm_sclk").unlink()
            (device / "pp_dpm_mclk").unlink()
            amd = gpu.read_drm_gpus(str(root))[0]
            self.assertEqual(amd["temps_c"], {})
            self.assertIsNone(amd["power_w"])
            self.assertIsNone(amd["sclk_mhz"])
            self.assertIsNone(amd["mclk_mhz"])

    def test_missing_root(self):
        self.assertEqual(gpu.read_drm_gpus("/nonexistent-root"), [])


class PcieChainTest(unittest.TestCase):
    """The GPU at 0000:9e:00.0 sits behind two bridges under a Gen4 root port."""

    def setUp(self):
        self.chain = gpu.read_drm_gpus(str(FIXTURES))[2]["pcie"]

    def test_chain_hops_and_roles(self):
        self.assertEqual([h["slot"] for h in self.chain["chain"]],
                         ["0000:9e:00.0", "0000:9d:00.0",
                          "0000:9c:00.0", "0000:9b:01.1"])
        self.assertEqual([h["role"] for h in self.chain["chain"]],
                         ["endpoint", "bridge", "bridge", "root_port"])

    def test_endpoint_is_the_gpu_itself(self):
        self.assertEqual(
            self.chain["endpoint"],
            {"gen": 5, "width": 16, "max_gen": 5, "max_width": 16})

    def test_host_link_is_the_root_port_link(self):
        self.assertEqual(
            self.chain["host_link"],
            {"gen": 4, "width": 16, "max_gen": 4, "max_width": 16})
        # the top-level summary follows the host link, not the endpoint
        self.assertEqual(self.chain["gen"], 4)
        self.assertEqual(self.chain["width"], 16)
        self.assertEqual(self.chain["max_gen"], 4)
        self.assertEqual(self.chain["max_width"], 16)

    def test_bottleneck_is_first_hop_at_slowest_gen(self):
        self.assertEqual(self.chain["bottleneck"],
                         {"gen": 4, "width": 16, "slot": "0000:9c:00.0"})

    def test_at_max_so_no_note(self):
        self.assertFalse(self.chain["below_max"])
        self.assertIsNone(self.chain["note"])

    def test_chain_card_has_no_hwmon_data(self):
        card = gpu.read_drm_gpus(str(FIXTURES))[2]
        self.assertIsNone(card["util_pct"])
        self.assertEqual(card["temps_c"], {})
        self.assertIsNone(card["power_w"])


class SlowLinkTest(unittest.TestCase):
    """Endpoint runs Gen5 x16 but the only path to the root port is Gen1 x4."""

    def setUp(self):
        gpus = gpu.read_drm_gpus(str(SLOWLINK_FIXTURES))
        self.assertEqual(len(gpus), 1)
        self.pcie = gpus[0]["pcie"]

    def test_roles(self):
        self.assertEqual([h["role"] for h in self.pcie["chain"]],
                         ["endpoint", "bridge", "root_port"])

    def test_host_link_reports_the_real_cap(self):
        self.assertEqual(
            self.pcie["host_link"],
            {"gen": 1, "width": 4, "max_gen": 4, "max_width": 16})
        self.assertEqual(self.pcie["gen"], 1)
        self.assertEqual(self.pcie["width"], 4)
        self.assertEqual(self.pcie["max_gen"], 4)
        self.assertEqual(self.pcie["max_width"], 16)

    def test_below_max_carries_note(self):
        self.assertTrue(self.pcie["below_max"])
        self.assertEqual(self.pcie["note"], gpu._LINK_NOTE)

    def test_bottleneck(self):
        self.assertEqual(self.pcie["bottleneck"],
                         {"gen": 1, "width": 4, "slot": "0000:ab:00.0"})


class LookupNameTest(unittest.TestCase):
    LSPCI_NVIDIA = ('01:00.0 "3D controller" "NVIDIA Corporation" '
                    '"GP107 [GeForce GTX 1050]" -r00 -p00 "ASUSTek" "Device 8520"\n')
    LSPCI_AMD = ('02:00.0 "VGA compatible controller" '
                 '"Advanced Micro Devices, Inc. [AMD/ATI]" "Device 7551" '
                 '-r00 -p00 "TUL" "Device 2443"\n')
    AMD_JSON = ('{"gpu_data":[{"gpu":0,"asic":{"market_name":'
                '"AMD Radeon AI PRO R9700","device_id":"0x7551"}}]}')

    def make_run(self, lspci_stdout="", amd_stdout=None, lspci_rc=0):
        def run(cmd, **kwargs):
            if cmd[0] == "lspci":
                return SimpleNamespace(returncode=lspci_rc,
                                       stdout=lspci_stdout, stderr="")
            if cmd[0] == "amd-smi":
                if amd_stdout is None:
                    raise FileNotFoundError("amd-smi")
                return SimpleNamespace(returncode=0, stdout=amd_stdout,
                                       stderr="")
            raise AssertionError(f"unexpected command {cmd}")
        return run

    def test_nvidia_name_passes_through(self):
        run = self.make_run(self.LSPCI_NVIDIA)
        self.assertEqual(
            gpu.lookup_name("0000:07:00.0", run=run),
            "NVIDIA Corporation GP107 [GeForce GTX 1050]")

    def test_amd_generic_name_replaced_by_market_name(self):
        run = self.make_run(self.LSPCI_AMD, self.AMD_JSON)
        self.assertEqual(
            gpu.lookup_name("0000:08:00.0", run=run),
            "AMD Radeon AI PRO R9700")

    def test_amd_bus_id_match_picks_right_entry(self):
        amd_json = ('{"gpu_data":['
                    '{"asic":{"bus_id":"0000:03:00.0","market_name":"First Card"}},'
                    '{"asic":{"bus_id":"0000:09:00.0","market_name":"Second Card"}}]}')
        run = self.make_run(self.LSPCI_AMD, amd_json)
        self.assertEqual(gpu.lookup_name("0000:09:00.0", run=run),
                         "Second Card")

    def test_missing_amd_smi_keeps_lspci_name(self):
        run = self.make_run(self.LSPCI_AMD)  # amd-smi raises FileNotFoundError
        self.assertEqual(
            gpu.lookup_name("0000:0a:00.0", run=run),
            "Advanced Micro Devices, Inc. [AMD/ATI] Device 7551")

    def test_lspci_failure_gives_none(self):
        def boom(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])
        self.assertIsNone(gpu.lookup_name("0000:0b:00.0", run=boom))


class ParseNvidiaSmiTest(unittest.TestCase):
    def test_full_row(self):
        rows = gpu.parse_nvidia_smi(NVIDIA_SMI_CSV)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], "0000:51:00.0")
        self.assertEqual(row["name"], "NVIDIA GeForce GTX 1050")
        self.assertEqual(row["util_pct"], 33.0)
        self.assertEqual(row["vram_used"], 512 * 1024 * 1024)
        self.assertEqual(row["vram_total"], 2048 * 1024 * 1024)
        self.assertEqual(row["temps_c"], {"gpu": 45.0})
        self.assertEqual(row["power_w"], 28.5)
        self.assertEqual(row["sclk_mhz"], 1392)
        self.assertEqual(row["mclk_mhz"], 3505)
        self.assertEqual(row["pcie"],
                         {"gen": 3, "width": 16, "max_gen": None, "max_width": None})

    def test_na_fields(self):
        line = ("00000000:01:00.0, Tesla P4, [N/A], 123, [Not Supported],"
                " [N/A], [Not Supported], 1200, [N/A], 3, 16\n")
        row = gpu.parse_nvidia_smi(line)[0]
        self.assertIsNone(row["util_pct"])
        self.assertIsNone(row["vram_total"])
        self.assertEqual(row["temps_c"], {})
        self.assertIsNone(row["power_w"])
        self.assertIsNone(row["mclk_mhz"])
        self.assertEqual(row["vram_used"], 123 * 1024 * 1024)

    def test_malformed_lines_skipped(self):
        self.assertEqual(gpu.parse_nvidia_smi("garbage\n\n"), [])


class ReadGpusTest(unittest.TestCase):
    def setUp(self):
        self._real = gpu.lookup_name
        gpu.lookup_name = lambda slot: None

    def tearDown(self):
        gpu.lookup_name = self._real

    def test_nvidia_smi_missing_is_not_an_error(self):
        def boom(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])
        gpus = gpu.read_gpus(str(FIXTURES), run=boom)
        self.assertEqual([g["source"] for g in gpus], ["sysfs"] * 3)

    def test_nvidia_smi_failure_is_not_an_error(self):
        gpus = gpu.read_gpus(str(FIXTURES), run=fake_run("", returncode=1))
        self.assertEqual([g["source"] for g in gpus], ["sysfs"] * 3)

    def test_nvidia_smi_timeout_is_not_an_error(self):
        def boom(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 3)
        gpus = gpu.read_gpus(str(FIXTURES), run=boom)
        self.assertEqual([g["source"] for g in gpus], ["sysfs"] * 3)

    def test_merge_fills_none_fields_only(self):
        gpus = gpu.read_gpus(str(FIXTURES), run=fake_run(NVIDIA_SMI_CSV))
        nvidia = next(g for g in gpus if g["id"] == "0000:51:00.0")
        amd = next(g for g in gpus if g["id"] == "0000:05:00.0")
        self.assertEqual(nvidia["source"], "sysfs+nvidia-smi")
        self.assertEqual(nvidia["util_pct"], 33.0)
        self.assertEqual(nvidia["vram_used"], 512 * 1024 * 1024)
        self.assertEqual(nvidia["power_w"], 28.5)
        self.assertEqual(nvidia["sclk_mhz"], 1392)
        self.assertEqual(nvidia["temps_c"], {"temp1": 36.0, "gpu": 45.0})
        self.assertEqual({k: nvidia["pcie"][k] for k in
                          ("gen", "width", "max_gen", "max_width")},
                         {"gen": 1, "width": 16, "max_gen": 3, "max_width": 16})
        self.assertEqual(amd["source"], "sysfs")
        self.assertEqual(amd["util_pct"], 42)


class ApiGpusRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_api_gpus(self):
        with urllib.request.urlopen(self.base + "/api/gpus") as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read())
        self.assertEqual(set(data), {"gpus"})
        self.assertIsInstance(data["gpus"], list)


if __name__ == "__main__":
    unittest.main()

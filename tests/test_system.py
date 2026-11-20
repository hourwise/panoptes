import json
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from wsd import routes  # noqa: F401  registers /api/system
from wsd.collectors.system import (
    CpuSampler,
    cpu_utilisation,
    parse_meminfo,
    parse_proc_stat,
    read_cpu_freqs_mhz,
    read_cpu_topology,
    read_system,
)
from wsd.server import make_server

FIXTURES = Path(__file__).parent / "fixtures" / "system"


class ParseProcStatTest(unittest.TestCase):
    def setUp(self):
        self.text = (FIXTURES / "proc" / "stat").read_text()

    def test_values(self):
        result = parse_proc_stat(self.text)
        self.assertEqual(set(result), {"cpu", "cpu0", "cpu1", "cpu2", "cpu3"})
        self.assertEqual(result["cpu"], (420, 1120))
        self.assertEqual(result["cpu0"], (195, 545))
        self.assertEqual(result["cpu1"], (225, 575))
        self.assertEqual(result["cpu2"], (0, 0))
        self.assertEqual(result["cpu3"], (0, 300))

    def test_non_cpu_lines_ignored(self):
        self.assertNotIn("intr", parse_proc_stat(self.text))

    def test_guest_time_not_double_counted(self):
        # guest (field 9) is already included in user, guest_nice (field 10)
        # in nice: totals must cover user..steal only.
        text = (
            "cpu4 100 0 0 500 0 0 0 0 50 0\n"
            "cpu5 0 40 0 400 0 0 0 0 0 20\n"
        )
        result = parse_proc_stat(text)
        self.assertEqual(result["cpu4"], (100, 600))
        self.assertEqual(result["cpu5"], (40, 440))


class CpuUtilisationTest(unittest.TestCase):
    def test_utilisation(self):
        prev = parse_proc_stat((FIXTURES / "proc" / "stat").read_text())
        cur = parse_proc_stat((FIXTURES / "proc" / "stat_next").read_text())
        util = cpu_utilisation(prev, cur)
        self.assertEqual(util["cpu"], 33.3)
        self.assertEqual(util["cpu0"], 50.0)
        self.assertEqual(util["cpu2"], 50.0)

    def test_no_change_gives_none(self):
        prev = parse_proc_stat((FIXTURES / "proc" / "stat").read_text())
        util = cpu_utilisation(prev, dict(prev))
        self.assertEqual(set(util.values()), {None})

    def test_new_cpu_gives_none(self):
        util = cpu_utilisation({}, {"cpu0": (10, 100)})
        self.assertEqual(util, {"cpu0": None})


class CpuFreqTest(unittest.TestCase):
    def test_freqs_sorted_with_none(self):
        freqs = read_cpu_freqs_mhz(str(FIXTURES))
        self.assertEqual(freqs, [2400.0, None, 1800.4, None, None])

    def test_missing_tree(self):
        self.assertEqual(read_cpu_freqs_mhz("/nonexistent-root"), [])


class ParseMeminfoTest(unittest.TestCase):
    def test_values(self):
        mem = parse_meminfo((FIXTURES / "proc" / "meminfo").read_text())
        self.assertEqual(mem["total"], 8000000 * 1024)
        self.assertEqual(mem["available"], 4000000 * 1024)
        self.assertEqual(mem["used"], 4000000 * 1024)
        self.assertEqual(mem["swap_total"], 2000000 * 1024)
        self.assertEqual(mem["swap_used"], 500000 * 1024)


class CpuSamplerTest(unittest.TestCase):
    def test_first_sample_compares_against_construction(self):
        sampler = CpuSampler(str(FIXTURES))
        sample = sampler.sample()
        self.assertIsNone(sample["overall"])
        self.assertEqual(sample["per_cpu"], [None, None, None, None])
        self.assertEqual(
            sample["freq_mhz"]["per_cpu"], [2400.0, None, 1800.4, None, None]
        )
        self.assertEqual(sample["freq_mhz"]["min"], 1800.4)
        self.assertEqual(sample["freq_mhz"]["max"], 2400.0)
        self.assertEqual(sample["freq_mhz"]["avg"], 2100.2)

    def test_sample_after_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "system"
            shutil.copytree(FIXTURES, root)
            sampler = CpuSampler(str(root))
            shutil.copyfile(root / "proc" / "stat_next", root / "proc" / "stat")
            sample = sampler.sample()
            self.assertEqual(sample["overall"], 33.3)
            self.assertEqual(sample["per_cpu"], [50.0, None, 50.0, None])

    def test_concurrent_samples(self):
        sampler = CpuSampler(str(FIXTURES))
        results = []
        errors = []

        def worker():
            try:
                results.append(sampler.sample())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        for sample in results:
            for value in [sample["overall"], *sample["per_cpu"]]:
                self.assertTrue(value is None or isinstance(value, (int, float)))


class CpuTopologyTest(unittest.TestCase):
    def test_values(self):
        # 2 cores x 2 threads in one package; cpu4 has physical_package_id
        # but no core_id, so it counts neither as a thread nor a core.
        self.assertEqual(
            read_cpu_topology(str(FIXTURES)),
            {
                "model": "AMD Fixture 2-Core Processor",
                "cores": 2,
                "threads": 4,
                "packages": 1,
            },
        )

    def test_missing_tree(self):
        self.assertEqual(
            read_cpu_topology("/nonexistent-root"),
            {"model": None, "cores": None, "threads": None, "packages": None},
        )

    def test_read_once_per_sampler(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "system"
            shutil.copytree(FIXTURES, root)
            sampler = CpuSampler(str(root))
            shutil.rmtree(root / "sys/devices/system/cpu/cpu2/topology")
            (root / "proc/cpuinfo").unlink()
            snapshot = read_system(str(root), sampler)
            self.assertEqual(
                sampler.topology, read_cpu_topology(str(FIXTURES)))
            self.assertEqual(snapshot["topology"], sampler.topology)


class ReadSystemTest(unittest.TestCase):
    def test_full_snapshot(self):
        result = read_system(str(FIXTURES), CpuSampler(str(FIXTURES)))
        self.assertEqual(
            set(result), {"cpu", "memory", "load_avg", "uptime_s", "topology"})
        self.assertEqual(result["load_avg"], [0.52, 0.58, 0.59])
        self.assertEqual(result["uptime_s"], 12345.67)
        self.assertEqual(result["memory"]["total"], 8000000 * 1024)
        self.assertEqual(
            result["topology"],
            {
                "model": "AMD Fixture 2-Core Processor",
                "cores": 2,
                "threads": 4,
                "packages": 1,
            },
        )
        self.assertIn("overall", result["cpu"])

    def test_missing_files_give_none(self):
        result = read_system(str(FIXTURES / "min"), CpuSampler(str(FIXTURES / "min")))
        self.assertIsNone(result["memory"])
        self.assertIsNone(result["load_avg"])
        self.assertIsNone(result["uptime_s"])
        self.assertIsNone(result["cpu"]["overall"])
        self.assertIsNone(result["cpu"]["freq_mhz"]["min"])
        self.assertEqual(
            result["topology"],
            {"model": None, "cores": None, "threads": None, "packages": None},
        )


class ApiSystemRouteTest(unittest.TestCase):
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

    def test_api_system(self):
        with urllib.request.urlopen(self.base + "/api/system") as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read())
        self.assertEqual(
            set(data), {"cpu", "memory", "load_avg", "uptime_s", "topology"})
        self.assertEqual(set(data["cpu"]), {"overall", "per_cpu", "freq_mhz"})
        self.assertEqual(
            set(data["cpu"]["freq_mhz"]), {"min", "max", "avg", "per_cpu"})
        self.assertEqual(
            set(data["topology"]), {"model", "cores", "threads", "packages"})
        self.assertIsInstance(data["cpu"]["per_cpu"], list)
        if data["memory"] is not None:
            self.assertEqual(
                set(data["memory"]),
                {"total", "available", "used", "swap_total", "swap_used"})


if __name__ == "__main__":
    unittest.main()

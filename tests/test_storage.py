import json
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from wsd import routes  # noqa: F401  registers /api/storage
from wsd.collectors.storage import (
    DiskSampler,
    fs_usage,
    parent_disk,
    parse_diskstats,
    parse_mounts,
    read_block_info,
    read_storage,
)
from wsd.server import make_server

FIXTURES = Path(__file__).parent / "fixtures" / "storage"

DISKS = {"mmcblk0", "nvme0n1", "sda", "sdaa", "sdb", "vda"}


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class ParseMountsTest(unittest.TestCase):
    def setUp(self):
        self.mounts = parse_mounts((FIXTURES / "proc" / "mounts").read_text())

    def test_real_block_devices_only(self):
        self.assertEqual(
            [m["device"] for m in self.mounts],
            [
                "/dev/nvme0n1p3",
                "/dev/sda1",
                "/dev/nvme0n1p1",
                "/dev/mapper/vg0-srv",
                "/dev/sdb1",
                "/dev/sdc1",
            ],
        )

    def test_fields(self):
        self.assertEqual(
            self.mounts[0],
            {
                "device": "/dev/nvme0n1p3",
                "mountpoint": "/mnt/fixture-root",
                "fstype": "ext4",
            },
        )

    def test_octal_escapes_decoded(self):
        sda = next(m for m in self.mounts if m["device"] == "/dev/sda1")
        self.assertEqual(sda["mountpoint"], "/mnt/fixture data")

    def test_first_mountpoint_wins(self):
        self.assertEqual(
            [m["mountpoint"] for m in self.mounts if m["device"] == "/dev/nvme0n1p3"],
            ["/mnt/fixture-root"],
        )


class FsUsageTest(unittest.TestCase):
    def test_usage_of_existing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            usage = fs_usage(tmp)
        self.assertIsNotNone(usage)
        self.assertEqual(set(usage), {"total", "used", "free"})
        self.assertGreater(usage["total"], 0)
        self.assertGreaterEqual(usage["used"], 0)
        self.assertGreater(usage["free"], 0)
        self.assertLessEqual(usage["free"], usage["total"])

    def test_missing_path_is_none(self):
        self.assertIsNone(fs_usage(str(FIXTURES / "not-mounted-here")))


class ParseDiskstatsTest(unittest.TestCase):
    def setUp(self):
        self.stats = parse_diskstats((FIXTURES / "proc" / "diskstats").read_text())

    def test_whole_disks_only(self):
        self.assertEqual(set(self.stats), DISKS)
        for skipped in ("sda1", "sda2", "nvme0n1p1", "nvme0n1p2", "vda1", "mmcblk0p1"):
            self.assertNotIn(skipped, self.stats)

    def test_field_values(self):
        self.assertEqual(
            self.stats["sda"],
            {"sectors_read": 49447, "sectors_written": 4, "io_ticks_ms": 1396},
        )
        self.assertEqual(
            self.stats["nvme0n1"],
            {"sectors_read": 219890510, "sectors_written": 7886954, "io_ticks_ms": 123624},
        )

    def test_short_line_ignored(self):
        self.assertEqual(parse_diskstats("   8     0 sda 1 2 3"), {})


class ReadBlockInfoTest(unittest.TestCase):
    def test_full_info(self):
        self.assertEqual(
            read_block_info(str(FIXTURES), "sda"),
            {
                "model": "WDC WD3200AAJS-00B8A0",
                "size_bytes": 625142448 * 512,
                "rotational": True,
            },
        )

    def test_missing_model_is_none(self):
        self.assertEqual(
            read_block_info(str(FIXTURES), "nvme0n1"),
            {
                "model": None,
                "size_bytes": 4000797360 * 512,
                "rotational": False,
            },
        )

    def test_unknown_disk_all_none(self):
        self.assertEqual(
            read_block_info(str(FIXTURES), "vda"),
            {"model": None, "size_bytes": None, "rotational": None},
        )


class ParentDiskTest(unittest.TestCase):
    def test_mapping(self):
        cases = [
            ("/dev/nvme0n1p2", "nvme0n1"),
            ("/dev/nvme0n1p12", "nvme0n1"),
            ("/dev/sda1", "sda"),
            ("/dev/sdaa1", "sdaa"),
            ("/dev/vda2", "vda"),
            ("/dev/mmcblk0p1", "mmcblk0"),
            ("/dev/sda", "sda"),
            ("/dev/nvme1n1", "nvme1n1"),
            ("/dev/mapper/vg0-srv", None),
            ("/dev/loop0", None),
            ("/dev/sr0", None),
            ("tmpfs", None),
        ]
        for device, expected in cases:
            with self.subTest(device=device):
                self.assertEqual(parent_disk(device), expected)


class DiskSamplerTest(unittest.TestCase):
    def root_with_diskstats(self, name: str = "diskstats") -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        shutil.copytree(FIXTURES / "sys", root / "sys")
        (root / "proc").mkdir()
        shutil.copyfile(FIXTURES / "proc" / "mounts", root / "proc" / "mounts")
        shutil.copyfile(FIXTURES / "proc" / name, root / "proc" / "diskstats")
        return str(root)

    def test_whole_disks_only_in_sample(self):
        sampler = DiskSampler(self.root_with_diskstats(), clock=FakeClock())
        self.assertEqual(set(sampler.sample()), DISKS)

    def test_first_sample_without_elapsed_time_is_none(self):
        sampler = DiskSampler(self.root_with_diskstats(), clock=FakeClock())
        for rates in sampler.sample().values():
            self.assertEqual(rates, {"read_bps": None, "write_bps": None, "busy_pct": None})

    def test_rates(self):
        root = self.root_with_diskstats()
        clock = FakeClock()
        sampler = DiskSampler(root, clock=clock)
        clock.now = 2.0
        shutil.copyfile(FIXTURES / "proc" / "diskstats_next", Path(root) / "proc" / "diskstats")
        rates = sampler.sample()
        self.assertEqual(
            rates["sda"], {"read_bps": 524288.0, "write_bps": 262144.0, "busy_pct": 5.0}
        )
        self.assertEqual(
            rates["sdb"], {"read_bps": 0.0, "write_bps": 0.0, "busy_pct": 0.0}
        )

    def test_busy_pct_capped_at_100(self):
        root = self.root_with_diskstats()
        clock = FakeClock()
        sampler = DiskSampler(root, clock=clock)
        clock.now = 2.0
        shutil.copyfile(FIXTURES / "proc" / "diskstats_next", Path(root) / "proc" / "diskstats")
        self.assertEqual(sampler.sample()["nvme0n1"]["busy_pct"], 100.0)

    def test_counter_reset_gives_none(self):
        root = self.root_with_diskstats()
        clock = FakeClock()
        sampler = DiskSampler(root, clock=clock)
        clock.now = 2.0
        shutil.copyfile(FIXTURES / "proc" / "diskstats_next", Path(root) / "proc" / "diskstats")
        rates = sampler.sample()["sdaa"]
        self.assertIsNone(rates["read_bps"])
        self.assertEqual(rates["write_bps"], 0.0)

    def test_missing_diskstats_gives_empty_sample(self):
        self.assertEqual(DiskSampler("/nonexistent-root").sample(), {})

    def test_concurrent_samples(self):
        sampler = DiskSampler(self.root_with_diskstats(), clock=FakeClock())
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
            for rates in sample.values():
                for value in rates.values():
                    self.assertTrue(value is None or isinstance(value, (int, float)))


class ReadStorageTest(unittest.TestCase):
    def test_disks_sorted_with_block_info(self):
        result = read_storage(str(FIXTURES), DiskSampler(str(FIXTURES), clock=FakeClock()))
        self.assertEqual(set(result), {"disks", "filesystems"})
        self.assertEqual(
            [d["name"] for d in result["disks"]], ["mmcblk0", "nvme0n1", "sda", "sdaa", "sdb", "vda"]
        )
        sda = result["disks"][2]
        self.assertEqual(
            set(sda),
            {"name", "model", "size_bytes", "rotational", "read_bps", "write_bps", "busy_pct"},
        )
        self.assertEqual(sda["model"], "WDC WD3200AAJS-00B8A0")
        self.assertIs(sda["rotational"], True)
        self.assertIsNone(sda["read_bps"])
        missing = next(d for d in result["disks"] if d["name"] == "vda")
        self.assertEqual(
            missing,
            {
                "name": "vda",
                "model": None,
                "size_bytes": None,
                "rotational": None,
                "read_bps": None,
                "write_bps": None,
                "busy_pct": None,
            },
        )

    def test_filesystems_tagged_with_parent_disk(self):
        result = read_storage(str(FIXTURES), DiskSampler(str(FIXTURES), clock=FakeClock()))
        self.assertEqual(
            [(f["device"], f["disk"]) for f in result["filesystems"]],
            [
                ("/dev/nvme0n1p3", "nvme0n1"),
                ("/dev/sda1", "sda"),
                ("/dev/nvme0n1p1", "nvme0n1"),
                ("/dev/mapper/vg0-srv", None),
                ("/dev/sdb1", "sdb"),
                ("/dev/sdc1", "sdc"),
            ],
        )

    def test_usage_is_none_for_unmounted_paths(self):
        result = read_storage(str(FIXTURES), DiskSampler(str(FIXTURES), clock=FakeClock()))
        for fs in result["filesystems"]:
            self.assertIsNone(fs["total"])
            self.assertIsNone(fs["used"])
            self.assertIsNone(fs["free"])

    def test_missing_proc_gives_empty_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = read_storage(tmp, DiskSampler(tmp, clock=FakeClock()))
        self.assertEqual(result, {"disks": [], "filesystems": []})


class ApiStorageRouteTest(unittest.TestCase):
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

    def test_api_storage(self):
        with urllib.request.urlopen(self.base + "/api/storage") as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read())
        self.assertEqual(set(data), {"disks", "filesystems"})
        self.assertIsInstance(data["disks"], list)
        self.assertIsInstance(data["filesystems"], list)
        for disk in data["disks"]:
            self.assertEqual(
                set(disk),
                {"name", "model", "size_bytes", "rotational", "read_bps", "write_bps", "busy_pct"},
            )
        for fs in data["filesystems"]:
            self.assertEqual(
                set(fs),
                {"device", "mountpoint", "fstype", "disk", "total", "used", "free"},
            )


if __name__ == "__main__":
    unittest.main()

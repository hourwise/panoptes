import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from wsd.lifecycle import control
from wsd.lifecycle.control import ControlClient, OPS, worker_active

HELPER_ARGV = ["sudo", "-n", "/usr/local/sbin/radiance-ctl"]
STATUS_JSON = json.dumps({"ok": True, "op": "status", "name": "/radiance-flashnext"})


class Recorder:
    """Fake subprocess.run: records every call, replays a canned result."""

    def __init__(self, returncode=0, stdout=STATUS_JSON, stderr="", raises=None, by_op=None):
        self.calls = []  # list of (argv, kwargs)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises
        self.by_op = by_op or {}

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises is not None:
            raise self.raises
        spec = self.by_op.get(argv[-1], self)
        return subprocess.CompletedProcess(argv, spec.returncode, spec.stdout, spec.stderr)

    @property
    def ops(self):
        return [argv[-1] for argv, _ in self.calls]


class OpEnumTest(unittest.TestCase):
    def test_only_the_fixed_ops_are_allowed(self):
        rec = Recorder()
        client = ControlClient(runner=rec)
        for bad in ("rm", "Status", "STATUS", "logs -f", "status; reboot", "", "docker", "status ", "shell"):
            with self.subTest(op=bad):
                with self.assertRaises(ValueError):
                    client.run(bad)
        self.assertEqual(rec.calls, [])  # nothing outside the enum ever reached the runner

    def test_argv_is_exact_and_timeout_fixed(self):
        for op in sorted(OPS):
            with self.subTest(op=op):
                rec = Recorder(stdout="x" if op == "logs" else STATUS_JSON)
                client = ControlClient(runner=rec)
                getattr(client, op)()
                self.assertEqual(len(rec.calls), 1)
                argv, kwargs = rec.calls[0]
                self.assertEqual(argv, HELPER_ARGV + [op])
                self.assertEqual(kwargs["timeout"], 15 if op == "logs" else 60)
                self.assertTrue(kwargs["capture_output"])
                self.assertTrue(kwargs["text"])
                self.assertFalse(kwargs.get("shell", False))

    def test_run_is_the_same_argv_as_status(self):
        rec = Recorder()
        ControlClient(runner=rec).run("status")
        self.assertEqual(rec.calls[0][0], HELPER_ARGV + ["status"])


class ResultHandlingTest(unittest.TestCase):
    def test_status_returns_parsed_json(self):
        client = ControlClient(runner=Recorder())
        self.assertEqual(client.status(), {"ok": True, "op": "status", "name": "/radiance-flashnext"})

    def test_non_json_output_is_an_error_dict(self):
        client = ControlClient(runner=Recorder(stdout="container is fine\n"))
        result = client.start()
        self.assertFalse(result["ok"])
        self.assertIn("JSON", result["error"])

    def test_nonzero_exit_reports_stderr(self):
        rec = Recorder(returncode=1, stdout="", stderr="refused: worker task active")
        result = ControlClient(runner=rec).restart()
        self.assertEqual(result, {"ok": False, "error": "refused: worker task active"})

    def test_nonzero_exit_without_output(self):
        result = ControlClient(runner=Recorder(returncode=7, stdout="", stderr="")).stop()
        self.assertFalse(result["ok"])
        self.assertIn("code 7", result["error"])

    def test_timeout_is_an_error_not_a_crash(self):
        rec = Recorder(raises=subprocess.TimeoutExpired(HELPER_ARGV + ["stop"], 60))
        result = ControlClient(runner=rec).stop()
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])

    def test_missing_helper_is_an_error(self):
        rec = Recorder(raises=FileNotFoundError("no sudo"))
        result = ControlClient(runner=rec).start()
        self.assertFalse(result["ok"])

    def test_logs_returns_text(self):
        client = ControlClient(runner=Recorder(stdout="a\nb\nc\n"))
        self.assertEqual(client.logs(), "a\nb\nc")

    def test_logs_keeps_only_the_last_200_lines(self):
        text = "\n".join(f"line {i}" for i in range(500))
        out = ControlClient(runner=Recorder(stdout=text)).logs()
        self.assertEqual(out.splitlines(), [f"line {i}" for i in range(300, 500)])

    def test_logs_are_capped_at_64_kb(self):
        text = "\n".join("x" * 1000 for _ in range(300))  # 300 KB
        out = ControlClient(runner=Recorder(stdout=text)).logs()
        self.assertLessEqual(len(out.encode("utf-8")), 64 * 1024)
        self.assertLessEqual(len(out.splitlines()), 200)
        self.assertTrue(out.endswith("x" * 1000))

    def test_logs_failure_is_an_error_dict(self):
        rec = Recorder(returncode=1, stdout="", stderr="logs not available")
        self.assertEqual(ControlClient(runner=rec).logs(), {"ok": False, "error": "logs not available"})


class StatusCacheTest(unittest.TestCase):
    def test_status_cached_for_five_seconds(self):
        rec = Recorder()
        client = ControlClient(runner=rec)
        client.status()
        client.status()
        self.assertEqual(len(rec.calls), 1)
        with mock.patch.object(control.time, "monotonic", return_value=time.monotonic() + 5.1):
            client.status()
        self.assertEqual(len(rec.calls), 2)
        client.status()  # the fresh result is cached again
        self.assertEqual(len(rec.calls), 2)

    def test_cache_is_per_client(self):
        rec = Recorder()
        ControlClient(runner=rec).status()
        ControlClient(runner=rec).status()
        self.assertEqual(len(rec.calls), 2)

    def test_other_ops_are_never_cached(self):
        rec = Recorder(stdout='{"ok": true}')
        client = ControlClient(runner=rec)
        client.start()
        client.start()
        self.assertEqual(rec.ops, ["start", "start"])


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid  # reaped: guaranteed dead


class WorkerActiveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp)
        self.lock = Path(tmp) / "worker-active"

    def test_alive_owner_means_active(self):
        self.lock.write_text(f"T-020 1795042812 {os.getpid()}\n")
        self.assertTrue(worker_active(self.lock))
        self.assertTrue(ControlClient().worker_active(self.lock))

    def test_dead_owner_means_inactive(self):
        self.lock.write_text(f"T-020 1795042812 {_dead_pid()}\n")
        self.assertFalse(worker_active(self.lock))

    def test_missing_file_means_inactive(self):
        self.assertFalse(worker_active(self.lock))

    def test_directory_means_inactive(self):
        self.lock.mkdir()
        self.assertFalse(worker_active(self.lock))

    def test_garbage_content_means_inactive(self):
        for content in ("", "   ", "T-020", "T-020 not a pid", "T-020 1795042812 0", "T-020 -3"):
            with self.subTest(content=content):
                self.lock.write_text(content)
                self.assertFalse(worker_active(self.lock))

    def test_real_lock_reports_live_task(self):
        """Sanity on the shipped format without touching real state."""
        self.lock.write_text("T-020 1795042812 1\n")  # pid 1 is alive and root-owned
        self.assertTrue(worker_active(self.lock))  # PermissionError => alive


class DryRunTest(unittest.TestCase):
    """PANOPTES_CONTROL_DRYRUN=1 at construction: canned answers, sudo never runs."""

    def dry_client(self):
        rec = Recorder()
        return ControlClient(runner=rec, env={control.DRYRUN_ENV: "1"}), rec

    def test_status_is_a_canned_running_container(self):
        client, rec = self.dry_client()
        status = client.status()
        self.assertEqual(rec.calls, [])
        self.assertTrue(status["ok"])
        self.assertTrue(status["dry_run"])
        self.assertEqual(status["state"], "running")

    def test_lifecycle_ops_are_canned_and_reach_no_runner(self):
        for op in ("start", "stop", "restart"):
            with self.subTest(op=op):
                client, rec = self.dry_client()
                result = getattr(client, op)()
                self.assertEqual(result, {"ok": True, "op": op, "dry_run": True})
                self.assertEqual(rec.calls, [])

    def test_logs_are_fake_text(self):
        client, rec = self.dry_client()
        logs = client.logs()
        self.assertEqual(rec.calls, [])
        self.assertIsInstance(logs, str)
        self.assertTrue(logs.splitlines())

    def test_bad_ops_still_raise_in_dry_run(self):
        client, rec = self.dry_client()
        with self.assertRaises(ValueError):
            client.run("status; reboot")
        self.assertEqual(rec.calls, [])

    def test_flag_is_read_from_the_environment_at_construction(self):
        rec = Recorder()
        with mock.patch.dict(os.environ, {control.DRYRUN_ENV: "1"}):
            client = ControlClient(runner=rec)
        client.status()
        self.assertEqual(rec.calls, [])  # built dry: the helper is never asked
        live_rec = Recorder()
        ControlClient(runner=live_rec).status()
        self.assertEqual(live_rec.ops, ["status"])  # no flag in the environment: live

    def test_flag_arriving_after_construction_does_not_take_effect(self):
        rec = Recorder()
        client = ControlClient(runner=rec)
        with mock.patch.dict(os.environ, {control.DRYRUN_ENV: "1"}):
            client.status()
        self.assertEqual(rec.ops, ["status"])  # live mode was chosen at construction

    def test_only_the_value_1_means_dry(self):
        for value in ("0", "", "true", "1 ", "yes"):
            with self.subTest(value=value):
                rec = Recorder()
                ControlClient(runner=rec, env={control.DRYRUN_ENV: value}).status()
                self.assertEqual(rec.ops, ["status"])


class DryRunLockTest(unittest.TestCase):
    """PANOPTES_CONTROL_DRYRUN_LOCKED=1 forces the interlock in dry run."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lock = Path(tmp.name) / "worker-active"
        self.lock.write_text(f"T-021 1700000000 {os.getpid()}")  # this pid: alive

    def test_dry_run_unlocked_ignores_a_live_lock_file(self):
        client = ControlClient(runner=Recorder(), env={control.DRYRUN_ENV: "1"})
        self.assertFalse(client.worker_active(self.lock))  # file never consulted

    def test_dry_run_locked_reports_the_worker(self):
        client = ControlClient(runner=Recorder(), env={
            control.DRYRUN_ENV: "1", control.DRYRUN_LOCKED_ENV: "1"})
        self.assertTrue(client.worker_active(self.lock))

    def test_live_mode_still_reads_the_lock_file_whatever_the_flag_says(self):
        client = ControlClient(runner=Recorder(), env={control.DRYRUN_LOCKED_ENV: "1"})
        self.assertTrue(client.worker_active(self.lock))  # own pid: alive
        dead = self.lock.with_name("dead-lock")
        dead.write_text(f"T-021 1700000000 {_dead_pid()}")
        self.assertFalse(client.worker_active(dead))

    def test_only_the_value_1_locks(self):
        for value in ("0", "", "true", "1 ", "yes"):
            with self.subTest(value=value):
                client = ControlClient(runner=Recorder(), env={
                    control.DRYRUN_ENV: "1", control.DRYRUN_LOCKED_ENV: value})
                self.assertFalse(client.worker_active())

    def test_lock_flag_is_read_at_construction(self):
        with mock.patch.dict(os.environ, {control.DRYRUN_ENV: "1"}):
            client = ControlClient(runner=Recorder())  # built unlocked
        with mock.patch.dict(os.environ, {control.DRYRUN_ENV: "1",
                                          control.DRYRUN_LOCKED_ENV: "1"}):
            self.assertFalse(client.worker_active())


if __name__ == "__main__":
    unittest.main()

"""T-040: the profile-switch pipeline (journal, backup, rollback, restore) and
the routes that drive it.

The pipeline is driven by a fake :class:`EngineHooks` over a temporary
directory: nothing here talks to a container, shells out, or reads
``~/Applications/Radiance``. The route tests run a real loopback server with
those fake hooks swapped in for ``radiance_hooks``.
"""

import hashlib
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import wsd.controls.radiance
import wsd.routes
import wsd.server
from wsd import controls
from wsd.controls import apply
from wsd.lifecycle.control import DRYRUN_ENV, ControlClient
from wsd.server import make_server

FIXTURES = Path(__file__).parent / "fixtures" / "controls"
ACTIVE_TEXT = (FIXTURES / "flashnext.yaml").read_text()
CANDIDATE_TEXT = (FIXTURES / "flashnext-spec6.yaml").read_text()
ACTIVE = "flashnext.yaml"
CANDIDATE = "flashnext-spec6.yaml"
ENGINE = "radiance-r9700"

SUCCESS_STEPS = ["preconditions", "validate", "backup", "write", "restart", "ready"]


def dead_pid() -> int:
    """A pid that is certainly gone (the child is reaped here)."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


class FakeHooks(apply.EngineHooks):
    """The engine as the pipeline sees it: every answer is a knob, every
    question is recorded. ``down`` and ``unserved`` are the bytes of a profile
    this engine cannot run, so the old profile comes back up after a rollback
    the way a real one would."""

    def __init__(self, root, **knobs):
        super().__init__(root / ACTIVE, root, root / "journal", root / "switch-lock")
        self.worker = False          # supervised worker task running
        self.is_busy = False         # engine has requests in flight
        self.errors = []             # validate errors
        self.warnings = []           # validate warnings
        self.restart_errors = []     # one answer per restart call, None = accepted
        self.down = None             # active bytes the engine is never healthy on
        self.unserved = None         # active bytes that never start serving
        self.restarts = []           # active file bytes at each restart
        self.ready_calls = []        # budget each readiness check was given
        self.validate_calls = []
        for key, value in knobs.items():
            setattr(self, key, value)

    def worker_busy(self) -> bool:
        return self.worker

    def busy(self) -> bool:
        return self.is_busy

    def validate(self, text: str) -> dict:
        self.validate_calls.append(text)
        return {
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "tree": {"models": {"flashnext": {"model": "/models/x.rad"}}},
        }

    def restart(self):
        self.restarts.append(self.active_path.read_bytes())
        return self.restart_errors.pop(0) if self.restart_errors else None

    def healthy(self) -> bool:
        return not self._sick(self.down)

    def ready(self, timeout, candidate) -> bool:
        self.ready_calls.append(timeout)
        return not self._sick(self.unserved)

    def _sick(self, broken) -> bool:
        """True while the profile in the active file is one that is broken."""
        if broken is None:
            return False
        try:
            return self.active_path.read_bytes() == broken
        except OSError:
            return True


class VandalHooks(FakeHooks):
    """Readiness that never arrives, and removes the active file and puts a
    directory in its place: restoring the backup is then impossible too."""

    def ready(self, timeout, candidate) -> bool:
        if self.active_path.is_file():
            self.active_path.unlink()
        if not self.active_path.exists():
            self.active_path.mkdir()
        return False


class ContestedHooks(FakeHooks):
    """The fake shared by two switch threads: the pre-lock precondition
    check lines both threads up at the switch-lock step, and the winner then
    parks inside ``restart`` until the test releases it. The loser can
    therefore only ever meet a lock another live switch is holding."""

    def __init__(self, root, meet: threading.Barrier, released: threading.Event, **knobs):
        super().__init__(root, **knobs)
        self.meet = meet
        self.released = released

    def busy(self) -> bool:
        self.meet.wait(10)
        return False

    def restart(self):
        answer = super().restart()
        self.released.wait(10)
        return answer


class SlowHooks(FakeHooks):
    """An engine that needs 30 s to load whatever it is asked to serve: its
    readiness answer comes from the budget it was handed, never from a sleep."""

    def ready(self, timeout, candidate) -> bool:
        self.ready_calls.append(timeout)
        return timeout >= 30


class StubControl:
    """The radiance-ctl client as the hooks see it: no worker task, a restart
    that is only recorded, and a record of every call so a test can assert
    that nothing ran the helper."""

    def __init__(self):
        self.calls = []

    def worker_active(self, path=None) -> bool:
        self.calls.append("worker_active")
        return False

    def restart(self):
        self.calls.append("restart")
        return {"ok": True, "op": "restart", "dry_run": True}


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.dir = Path(self.root.name)
        (self.dir / ACTIVE).write_text(ACTIVE_TEXT)
        (self.dir / CANDIDATE).write_text(CANDIDATE_TEXT)
        self.hooks = FakeHooks(self.dir)

    # ------------------------------------------------------------------ tools
    def switch(self, name: str = CANDIDATE, **kwargs) -> dict:
        kwargs.setdefault("ready_timeout", 5.0)
        return apply.apply_profile(self.hooks, ENGINE, self.dir / name, name, **kwargs)

    def active(self) -> bytes:
        return (self.dir / ACTIVE).read_bytes()

    def lines(self) -> list[dict]:
        path = apply.journal_path(self.dir / "journal", ENGINE)
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def steps(self, entry: dict) -> list[str]:
        return [step["step"] for step in entry["steps"]]

    # -------------------------------------------------------------- success
    def test_apply_writes_the_candidate_and_backs_up_the_old_profile(self):
        entry = self.switch()
        self.assertEqual(entry["outcome"], "applied")
        self.assertEqual(entry["errors"], [])
        self.assertEqual(self.active(), CANDIDATE_TEXT.encode())
        self.assertEqual(self.steps(entry), SUCCESS_STEPS)
        self.assertEqual(entry["from"], {"name": ACTIVE, "sha256": hashlib.sha256(ACTIVE_TEXT.encode()).hexdigest()})
        self.assertEqual(entry["to"], {"name": CANDIDATE, "sha256": hashlib.sha256(CANDIDATE_TEXT.encode()).hexdigest()})
        self.assertEqual(entry["action"], "apply")
        self.assertIsNone(entry["restore_of"])
        self.assertFalse(entry["dry_run"])
        self.assertIsNotNone(entry["finished"])
        backup = self.dir / "journal" / "backups" / entry["backup"]
        self.assertEqual(backup.read_bytes(), ACTIVE_TEXT.encode())
        self.assertFalse((self.dir / "switch-lock").exists())

    def test_the_backup_is_named_by_stamp_and_content_hash(self):
        entry = self.switch()
        sha = hashlib.sha256(ACTIVE_TEXT.encode()).hexdigest()
        self.assertRegex(entry["backup"], rf"^\d{{8}}T\d{{6}}Z-{sha[:12]}\.yaml$")
        self.assertFalse(Path(entry["backup"]).is_absolute(), "the journal stores a name, not a path")

    def test_the_journal_grows_one_line_per_step_and_is_never_rewritten(self):
        self.switch()
        first_run = self.lines()
        self.assertEqual(len(first_run), len(SUCCESS_STEPS) + 1)
        self.assertEqual(self.steps(first_run[0]), ["preconditions"])
        self.assertEqual(first_run[0]["outcome"], apply.RUNNING)
        self.assertEqual(first_run[-1]["outcome"], "applied")
        self.assertEqual({line["id"] for line in first_run}, {first_run[-1]["id"]})
        self.assertTrue(all(len(self.steps(line)) == index + 1 for index, line in enumerate(first_run[:-1])))
        self.switch()
        after_second = self.lines()
        self.assertEqual(after_second[: len(first_run)], first_run, "an old attempt was rewritten")
        self.assertEqual(apply.load_history(self.dir / "journal", ENGINE), [first_run[-1], after_second[-1]])

    def test_the_candidate_profile_is_never_modified(self):
        before = (self.dir / CANDIDATE).read_bytes()
        self.switch()
        self.assertEqual((self.dir / CANDIDATE).read_bytes(), before)

    # ------------------------------------------------------------- refusals
    def test_a_running_worker_task_refuses_the_switch(self):
        self.hooks.worker = True
        entry = self.switch()
        self.assertEqual(entry["outcome"], "refused")
        self.assertIn("worker task", entry["errors"][0])
        self.assertEqual(self.steps(entry), ["preconditions"])
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())
        self.assertEqual(self.hooks.validate_calls, [])
        self.assertFalse((self.dir / "journal" / "backups").exists())
        self.assertFalse((self.dir / "switch-lock").exists())

    def test_a_refusal_still_carries_the_from_field_and_both_budgets(self):
        applied = self.switch()  # the key set a completed attempt ends with
        self.hooks.worker = True
        entry = self.switch(ready_timeout=5.0)
        self.assertEqual(entry["outcome"], "refused")
        self.assertIn("from", entry)
        self.assertIsNone(entry["from"])
        self.assertEqual(sorted(entry), sorted(applied))
        self.assertEqual(entry["budgets"], {"ready_s": 5, "rollback_s": 600})
        self.hooks.worker = False
        (self.dir / "switch-lock").write_text(f"sw-other 1 {os.getpid()}\n")
        locked = self.switch(ready_timeout=5.0)
        self.assertEqual(locked["outcome"], "refused")
        self.assertIsNone(locked["from"])
        self.assertEqual(sorted(locked), sorted(applied))

    def test_requests_in_flight_refuse_the_switch(self):
        self.hooks.is_busy = True
        entry = self.switch()
        self.assertEqual(entry["outcome"], "refused")
        self.assertIn("in flight", entry["errors"][0])
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())

    def test_a_live_switch_lock_refuses_and_is_not_stolen(self):
        content = f"sw-other 1 {os.getpid()}\n"
        (self.dir / "switch-lock").write_text(content)
        entry = self.switch()
        self.assertEqual(entry["outcome"], "refused")
        self.assertIn("another profile switch", entry["errors"][0])
        self.assertEqual((self.dir / "switch-lock").read_text(), content)
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())

    def test_a_stale_switch_lock_does_not_block_the_switch(self):
        (self.dir / "switch-lock").write_text(f"sw-crashed 1 {dead_pid()}\n")
        entry = self.switch()
        self.assertEqual(entry["outcome"], "applied")
        self.assertFalse((self.dir / "switch-lock").exists())

    def test_the_switch_lock_is_created_with_mode_0600(self):
        apply.acquire_switch_lock(self.dir / "switch-lock", "sw-mode")
        self.addCleanup(apply.release_switch_lock, self.dir / "switch-lock", "sw-mode")
        self.assertEqual((self.dir / "switch-lock").stat().st_mode & 0o777, 0o600)

    def test_two_concurrent_applies_run_exactly_one_switch(self):
        meet = threading.Barrier(2, timeout=10)
        released = threading.Event()
        hooks = ContestedHooks(self.dir, meet, released)
        entries: list[dict] = []
        returned = threading.Event()
        guard = threading.Lock()

        def run() -> None:
            entry = apply.apply_profile(hooks, ENGINE, self.dir / CANDIDATE, CANDIDATE, ready_timeout=5.0)
            with guard:
                entries.append(entry)
                returned.set()

        threads = [threading.Thread(target=run, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            # The refused switch returns at once; the applied one waits in restart.
            self.assertTrue(returned.wait(10), "neither switch returned")
            released.set()
            for thread in threads:
                thread.join(10)
                self.assertFalse(thread.is_alive())
        finally:
            released.set()
        self.assertEqual(len(entries), 2)
        self.assertEqual(sorted(entry["outcome"] for entry in entries), ["applied", "refused"])
        refused = next(entry for entry in entries if entry["outcome"] == "refused")
        self.assertIn("another profile switch", refused["errors"][0])
        self.assertEqual([step["step"] for step in refused["steps"]], ["preconditions"])
        backups = list((self.dir / "journal" / "backups").iterdir())
        self.assertEqual(len(backups), 1, "the refused switch wrote a backup")
        self.assertEqual(self.active(), CANDIDATE_TEXT.encode())
        self.assertFalse((self.dir / "switch-lock").exists())

    def test_an_unapproved_profile_refuses_before_anything_is_written(self):
        self.hooks.errors = ["models.flashnext.num-speculative-tokens is above the approved maximum"]
        entry = self.switch()
        self.assertEqual(entry["outcome"], "refused")
        self.assertIn("approved maximum", entry["errors"][0])
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())
        self.assertFalse((self.dir / "journal" / "backups").exists())
        self.assertEqual(self.hooks.restarts, [])
        self.assertIsNone(entry["backup"])

    def test_a_missing_candidate_refuses(self):
        entry = self.switch(name="no-such-profile.yaml")
        self.assertEqual(entry["outcome"], "refused")
        self.assertIn("cannot be read", entry["errors"][0])
        self.assertEqual(self.steps(entry), ["preconditions", "validate"])

    def test_validate_warnings_are_recorded_but_do_not_refuse(self):
        self.hooks.warnings = ["tensor-parallel-size is unusual on this box"]
        entry = self.switch()
        self.assertEqual(entry["outcome"], "applied")
        detail = [step for step in entry["steps"] if step["step"] == "validate"][0]["detail"]
        self.assertEqual(detail["warnings"], ["tensor-parallel-size is unusual on this box"])

    # ------------------------------------------------------------ rollback
    def test_a_failed_restart_restores_the_old_profile_byte_for_byte(self):
        self.hooks.restart_errors = ["radiance-ctl restart said no"]
        entry = self.switch()
        self.assertEqual(entry["outcome"], "rolled_back")
        self.assertIn("the engine restart failed", entry["errors"][0])
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())
        self.assertEqual(self.hooks.restarts, [CANDIDATE_TEXT.encode(), ACTIVE_TEXT.encode()])
        self.assertEqual(
            self.steps(entry),
            SUCCESS_STEPS[:5] + ["rollback", "restore", "restart", "ready"],
        )
        failed = [step for step in entry["steps"] if step["step"] == "rollback"][0]
        self.assertFalse(failed["ok"])
        self.assertIn("restart", failed["error"])
        self.assertFalse((self.dir / "switch-lock").exists())

    def test_never_becoming_ready_rolls_back(self):
        self.hooks.unserved = CANDIDATE_TEXT.encode()
        entry = self.switch(ready_timeout=0.0)
        self.assertEqual(entry["outcome"], "rolled_back")
        self.assertIn("not serving the new profile", entry["errors"][0])
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())
        self.assertEqual(self.steps(entry)[-2:], ["restart", "ready"])

    def test_an_engine_that_stays_down_after_the_write_rolls_back(self):
        self.hooks.down = CANDIDATE_TEXT.encode()
        entry = self.switch(ready_timeout=0.0)
        self.assertEqual(entry["outcome"], "rolled_back")
        self.assertIn("not healthy within", entry["errors"][0])
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())

    def test_a_rollback_that_does_not_come_back_says_the_engine_needs_attention(self):
        self.hooks.restart_errors = ["boom", "still down"]
        entry = self.switch(ready_timeout=0.0)
        self.assertEqual(entry["outcome"], "rollback_failed")
        self.assertTrue(any(apply.ATTENTION in error for error in entry["errors"]), entry["errors"])
        self.assertIn("was written back but the engine did not come back", entry["errors"][-1])
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())

    def test_a_rollback_that_cannot_write_is_loud_too(self):
        self.hooks = VandalHooks(self.dir)
        entry = self.switch(ready_timeout=0.0)
        self.assertEqual(entry["outcome"], "rollback_failed")
        self.assertTrue(any(apply.ATTENTION in error for error in entry["errors"]), entry["errors"])
        self.assertIn("could not be written back", entry["errors"][-1])

    def test_a_slow_engine_rolls_back_on_its_own_full_budget(self):
        self.hooks = SlowHooks(self.dir)
        entry = self.switch(ready_timeout=5.0)
        self.assertEqual(entry["outcome"], "rolled_back")
        self.assertEqual(
            self.steps(entry), SUCCESS_STEPS + ["rollback", "restore", "restart", "ready"]
        )
        forward, recovery = [step for step in entry["steps"] if step["step"] == "ready"]
        self.assertFalse(forward["ok"])
        self.assertTrue(recovery["ok"])
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())
        self.assertEqual(entry["budgets"], {"ready_s": 5, "rollback_s": 600})
        self.assertEqual(self.lines()[0]["budgets"], {"ready_s": 5, "rollback_s": 600})
        self.assertLess(self.hooks.ready_calls[0], 30)
        self.assertGreaterEqual(self.hooks.ready_calls[1], 30)

    def test_a_rollback_out_of_its_own_budget_is_a_failed_rollback(self):
        self.hooks = SlowHooks(self.dir)
        entry = self.switch(ready_timeout=5.0, rollback_timeout=5.0)
        self.assertEqual(entry["outcome"], "rollback_failed")
        self.assertEqual(entry["budgets"], {"ready_s": 5, "rollback_s": 5})
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())
        self.assertTrue(any(apply.ATTENTION in error for error in entry["errors"]), entry["errors"])

    # ------------------------------------------------------------- restore
    def test_a_journalled_entry_can_be_restored(self):
        first = self.switch()
        history = apply.load_history(self.dir / "journal", ENGINE)
        self.assertEqual([entry["id"] for entry in history], [first["id"]])
        candidate = self.dir / "journal" / "backups" / first["backup"]
        entry = apply.apply_profile(
            self.hooks, ENGINE, candidate, first["from"]["name"],
            action="restore", ready_timeout=5.0, restore_of=first["id"],
        )
        self.assertEqual(entry["outcome"], "applied")
        self.assertEqual(entry["action"], "restore")
        self.assertEqual(entry["restore_of"], first["id"])
        self.assertEqual(entry["to"]["name"], ACTIVE)
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())
        self.assertEqual([e["outcome"] for e in apply.load_history(self.dir / "journal", ENGINE)],
                         ["applied", "applied"])
        self.assertEqual(entry["from"]["sha256"], first["to"]["sha256"])

    def test_a_restore_is_journalled_next_to_the_apply_it_undoes(self):
        first = self.switch()
        candidate = self.dir / "journal" / "backups" / first["backup"]
        apply.apply_profile(self.hooks, ENGINE, candidate, ACTIVE, action="restore", restore_of=first["id"])
        ids = [entry["id"] for entry in apply.load_history(self.dir / "journal", ENGINE)]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])

    # ------------------------------------------------------- journal reading
    def test_history_of_an_absent_journal_is_empty_and_creates_nothing(self):
        missing = self.dir / "nowhere"
        self.assertEqual(apply.load_history(missing, ENGINE), [])
        self.assertFalse(missing.exists())

    def test_history_ignores_junk_lines(self):
        path = apply.journal_path(self.dir / "journal", ENGINE)
        path.parent.mkdir(parents=True)
        path.write_text('not json\n{"id": "sw-1", "outcome": "applied"}\n[1, 2]\n\n')
        self.assertEqual(apply.load_history(self.dir / "journal", ENGINE), [{"id": "sw-1", "outcome": "applied"}])

    def test_a_hostile_engine_id_is_refused_as_a_file_name(self):
        for bad in ("../escape", "a/b", ""):
            with self.assertRaises(ValueError):
                apply.journal_path(self.dir / "journal", bad)
        self.assertEqual(apply.load_history(self.dir / "journal", "../escape"), [])

    # --------------------------------------------------------- switch state
    def test_switch_state_reports_the_running_step_from_the_journal(self):
        journal = self.dir / "journal"
        journal.mkdir()
        apply.journal_path(journal, ENGINE).write_text(
            json.dumps({"id": "sw-1", "outcome": apply.RUNNING, "steps": [{"step": "write", "ok": True}]}) + "\n"
        )
        apply.acquire_switch_lock(self.dir / "switch-lock", "sw-1")
        self.addCleanup(apply.release_switch_lock, self.dir / "switch-lock", "sw-1")
        state = apply.switch_state(journal, ENGINE, self.dir / "switch-lock")
        self.assertTrue(state["running"])
        self.assertTrue(state["lock"])
        self.assertEqual(state["step"], "write")
        self.assertEqual(state["engine"], ENGINE)
        self.assertEqual(state["entry"]["id"], "sw-1")

    def test_a_finished_or_abandoned_switch_is_not_running(self):
        journal = self.dir / "journal"
        apply.journal_path(journal, ENGINE).parent.mkdir(parents=True)
        apply.journal_path(journal, ENGINE).write_text(
            json.dumps({"id": "sw-1", "outcome": "applied", "steps": [{"step": "ready", "ok": True}]}) + "\n"
        )
        state = apply.switch_state(journal, ENGINE, self.dir / "switch-lock")
        self.assertFalse(state["running"])
        self.assertEqual(state["step"], "ready")
        self.assertFalse(state["lock"])
        (self.dir / "switch-lock").write_text(f"sw-x 1 {dead_pid()}\n")
        self.assertFalse(apply.switch_state(journal, ENGINE, self.dir / "switch-lock")["running"])

    def test_switch_state_of_an_engine_that_never_switched(self):
        state = apply.switch_state(self.dir / "journal", ENGINE, self.dir / "switch-lock")
        self.assertEqual(state, {"engine": ENGINE, "running": False, "step": None, "lock": False, "entry": None})


class SwitchLockTest(unittest.TestCase):
    """The lock itself: the race that let two switches run at once (B-008),
    and the stale takeover that must not steal a lock which came alive."""

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.dir = Path(self.root.name)
        self.lock = self.dir / "switch-lock"

    def test_the_lock_is_never_seen_empty_or_partial(self):
        """A lock on disk is either absent or the whole payload: the publish
        is a hard link of a fully written temp file, never a fresh empty file."""
        rounds = 200
        done = threading.Event()
        partial: list[str] = []

        def churn() -> None:
            for _ in range(rounds):
                self.assertTrue(apply.acquire_switch_lock(self.lock, "sw-owner"))
                apply.release_switch_lock(self.lock, "sw-owner")
            done.set()

        def watch() -> None:
            while not done.is_set():
                try:
                    text = self.lock.read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue
                fields = text.split()
                if len(fields) != 3 or fields[0] != "sw-owner":
                    partial.append(repr(text))

        writer = threading.Thread(target=churn, daemon=True)
        watcher = threading.Thread(target=watch, daemon=True)
        writer.start()
        watcher.start()
        writer.join(60)
        watcher.join(10)
        self.assertFalse(writer.is_alive() or watcher.is_alive())
        self.assertEqual(partial[:3], [])

    def test_a_lock_that_turns_live_during_the_takeover_is_left_alone(self):
        """The stale check and the rename are not one step: if a live lock got
        published in between, the takeover must hand it back and refuse."""
        live = f"sw-live 1 {os.getpid()}\n"
        self.lock.write_text(f"sw-crashed 1 {dead_pid()}\n")
        real_rename = os.rename

        def published_in_between(src, dst) -> None:
            os.unlink(self.lock)
            self.lock.write_text(live)
            real_rename(src, dst)  # we are now moving a live lock aside

        with mock.patch.object(apply, "_rename_stale", side_effect=published_in_between):
            self.assertFalse(apply.acquire_switch_lock(self.lock, "sw-mine"))
        self.assertEqual(self.lock.read_text(), live)
        self.assertEqual([path.name for path in self.dir.iterdir()], ["switch-lock"])

    def test_a_stale_empty_or_junk_lock_is_taken_over_exactly_once(self):
        for content in (f"sw-crashed 1 {dead_pid()}\n", "", "junk\n"):
            with self.subTest(content=content):
                self.lock.write_text(content)
                self.assertTrue(apply.acquire_switch_lock(self.lock, "sw-mine"))
                self.assertEqual(self.lock.read_text().split()[0], "sw-mine")
                self.assertEqual([path.name for path in self.dir.iterdir()], ["switch-lock"])
                apply.release_switch_lock(self.lock, "sw-mine")
                self.assertFalse(self.lock.exists())


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.dir = Path(self.root.name)

    def test_the_replacement_leaves_no_temporary_file(self):
        dest = self.dir / "flashnext.yaml"
        apply.atomic_write(dest, b"one\n")
        apply.atomic_write(dest, b"two\n")
        self.assertEqual(dest.read_bytes(), b"two\n")
        self.assertEqual([path.name for path in self.dir.iterdir()], ["flashnext.yaml"])

    def test_two_threads_writing_one_destination_never_mix_or_remode_it(self):
        dest = self.dir / "flashnext.yaml"
        dest.write_bytes(b"original\n")
        os.chmod(dest, 0o644)
        payloads = [b"A" * 8192, b"B" * 8192]
        meet = threading.Barrier(2, timeout=10)

        def writer(payload: bytes) -> None:
            meet.wait(10)
            for _ in range(200):
                apply.atomic_write(dest, payload)

        threads = [threading.Thread(target=writer, args=(payload,), daemon=True) for payload in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
            self.assertFalse(thread.is_alive())
        self.assertIn(dest.read_bytes(), payloads)
        self.assertEqual([path.name for path in self.dir.iterdir()], ["flashnext.yaml"])
        self.assertEqual(dest.stat().st_mode & 0o777, 0o644)


class ReadyTimeoutTest(unittest.TestCase):
    def test_the_environment_overrides_the_waiting_budget(self):
        with mock.patch.dict(os.environ, {wsd.routes.READY_TIMEOUT_ENV: "7"}):
            self.assertEqual(wsd.routes._ready_timeout(), 7.0)

    def test_a_junk_or_zero_timeout_falls_back_to_the_default(self):
        for value in ("", "soon", "0", "-3"):
            with mock.patch.dict(os.environ, {wsd.routes.READY_TIMEOUT_ENV: value}):
                self.assertEqual(wsd.routes._ready_timeout(), apply.READY_TIMEOUT_S)


class RollbackTimeoutTest(unittest.TestCase):
    def test_the_rollback_environment_overrides_the_recovery_budget(self):
        with mock.patch.dict(os.environ, {wsd.routes.ROLLBACK_TIMEOUT_ENV: "45"}):
            self.assertEqual(wsd.routes._rollback_timeout(), 45.0)

    def test_a_junk_or_zero_rollback_timeout_falls_back_to_the_default(self):
        for value in ("abc", "0", "-1"):
            with mock.patch.dict(os.environ, {wsd.routes.ROLLBACK_TIMEOUT_ENV: value}):
                self.assertEqual(wsd.routes._rollback_timeout(), apply.ROLLBACK_TIMEOUT_S)

    def test_no_rollback_environment_means_the_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(wsd.routes.ROLLBACK_TIMEOUT_ENV, None)
            self.assertEqual(wsd.routes._rollback_timeout(), apply.ROLLBACK_TIMEOUT_S)

    def test_the_forward_budget_does_not_move_the_recovery_budget(self):
        with mock.patch.dict(os.environ, {wsd.routes.READY_TIMEOUT_ENV: "5"}):
            self.assertEqual(wsd.routes._ready_timeout(), 5.0)
            self.assertEqual(wsd.routes._rollback_timeout(), apply.ROLLBACK_TIMEOUT_S)


class RouteSwitchTest(unittest.TestCase):
    """POST apply/restore and GET history/switch against a real server."""

    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.dir = Path(self.root.name)
        (self.dir / ACTIVE).write_text(ACTIVE_TEXT)
        (self.dir / CANDIDATE).write_text(CANDIDATE_TEXT)
        self.hooks = FakeHooks(self.dir)
        patches = [
            mock.patch.dict(wsd.routes.ENGINES, {ENGINE: self.Stub(ENGINE)}, clear=True),
            mock.patch.dict(wsd.routes.ENGINE_PROFILES, {ENGINE: (str(self.dir), ACTIVE)}, clear=True),
            mock.patch.dict(wsd.routes.ENGINE_TYPES, {ENGINE: "radiance"}, clear=True),
            mock.patch.dict(wsd.routes.ENGINE_JOURNAL_DIRS, {ENGINE: str(self.dir / "journal")}, clear=True),
            mock.patch.object(wsd.controls, "radiance_hooks", lambda *args, **kwargs: self.hooks),
        ]
        for patcher in patches:
            patcher.start()
        self.addCleanup(mock.patch.stopall)

    class Stub:
        def __init__(self, engine_id):
            self.engine_id = engine_id
            self.base_url = "http://127.0.0.1:1"

        def snapshot(self):
            return {"id": self.engine_id, "type": "stub"}

    # ---------------------------------------------------------------- client
    def request(self, method, path, body=None, **headers):
        """One request; a header passed as ``None`` is left out altogether."""
        sent = {
            "Host": f"127.0.0.1:{self.port}",
            "Origin": f"http://127.0.0.1:{self.port}",
            "Content-Type": "application/json",
            "X-WSD-Token": wsd.server.TOKEN,
        }
        sent.update(headers)
        sent = {name: value for name, value in sent.items() if value is not None}
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=None if body is None else json.dumps(body), headers=sent)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def post(self, path, body=None, **headers):
        return self.request("POST", path, {} if body is None else body, **headers)

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def apply_path(self, name: str = CANDIDATE) -> str:
        return f"/api/engines/{ENGINE}/profiles/{name}/apply"

    def active(self) -> bytes:
        return (self.dir / ACTIVE).read_bytes()

    # -------------------------------------------------------------- guards
    def test_apply_needs_the_dashboard_token(self):
        status, payload = self.post(self.apply_path(), {"confirm": True}, **{"X-WSD-Token": None})
        self.assertEqual(status, 403)
        self.assertIn("token", payload["error"])
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())

    def test_apply_needs_a_loopback_origin(self):
        status, payload = self.post(self.apply_path(), {"confirm": True}, Origin="http://evil.example")
        self.assertEqual(status, 403)
        self.assertIn("origin", payload["error"])
        status, _payload = self.post(self.apply_path(), {"confirm": True}, **{"Origin": None})
        self.assertEqual(status, 403)
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())

    def test_apply_without_confirmation_refuses_and_touches_nothing(self):
        for body in ({}, {"confirm": False}, {"confirm": "true"}):
            status, payload = self.post(self.apply_path(), body)
            self.assertEqual(status, 200, body)
            self.assertFalse(payload["ok"])
            self.assertIn('{"confirm": true}', payload["error"])
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())
        self.assertFalse((self.dir / "journal").exists())

    def test_an_engine_that_cannot_switch_profiles_is_refused(self):
        with mock.patch.dict(wsd.routes.ENGINE_TYPES, {ENGINE: "llama.cpp"}, clear=True):
            status, payload = self.post(self.apply_path(), {"confirm": True})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertIn("cannot switch profiles", payload["error"])
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())

    def test_a_name_that_is_not_a_profile_is_refused(self):
        for name, why in (("notes.txt", "invalid profile name"), ("gone.yaml", "is not a file in")):
            status, payload = self.post(self.apply_path(name), {"confirm": True})
            self.assertEqual(status, 200, name)
            self.assertFalse(payload["ok"], name)
            self.assertIn(why, payload["error"], name)
            self.assertEqual(self.active(), ACTIVE_TEXT.encode())

    def test_an_unknown_switch_path_is_refused(self):
        for tail in ("profiles/flashnext.yaml/activate", "history/sw-1/undo", "switch/now"):
            status, payload = self.post(f"/api/engines/{ENGINE}/{tail}", {"confirm": True})
            self.assertEqual(status, 200, tail)
            self.assertFalse(payload["ok"], tail)
            self.assertIn("unknown route", payload["error"])

    def test_a_path_outside_the_switch_routes_is_still_the_servers_business(self):
        self.assertEqual(self.post("/api/engines", {"confirm": True})[0], 405)

    # --------------------------------------------------------------- apply
    def test_apply_runs_the_pipeline_and_answers_with_the_journal_entry(self):
        status, payload = self.post(self.apply_path(), {"confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["outcome"], "applied")
        self.assertEqual(payload["engine"], ENGINE)
        self.assertEqual(payload["action"], "apply")
        self.assertEqual(payload["to"]["name"], CANDIDATE)
        self.assertEqual([step["step"] for step in payload["steps"]], SUCCESS_STEPS)
        self.assertEqual(self.active(), CANDIDATE_TEXT.encode())
        self.assertTrue((self.dir / "journal" / "backups" / payload["backup"]).is_file())

    def test_apply_reports_a_refused_pipeline_as_not_ok(self):
        self.hooks.errors = ["num-speculative-tokens is above the approved maximum"]
        status, payload = self.post(self.apply_path(), {"confirm": True})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["outcome"], "refused")
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())

    def test_apply_reports_a_rollback_as_not_ok(self):
        self.hooks.restart_errors = ["radiance-ctl restart said no"]
        status, payload = self.post(self.apply_path(), {"confirm": True})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["outcome"], "rolled_back")
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())

    # ------------------------------------------------------------- restore
    def test_restore_puts_the_backed_up_profile_back(self):
        _, applied = self.post(self.apply_path(), {"confirm": True})
        status, payload = self.post(f"/api/engines/{ENGINE}/history/{applied['id']}/restore", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "restore")
        self.assertEqual(payload["restore_of"], applied["id"])
        self.assertEqual(payload["to"]["name"], ACTIVE)
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())

    def test_restoring_an_unknown_entry_is_refused(self):
        status, payload = self.post(f"/api/engines/{ENGINE}/history/sw-nope/restore", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertIn("no history entry", payload["error"])

    def test_restoring_an_entry_whose_backup_is_gone_is_refused(self):
        _, applied = self.post(self.apply_path(), {"confirm": True})
        (self.dir / "journal" / "backups" / applied["backup"]).unlink()
        status, payload = self.post(f"/api/engines/{ENGINE}/history/{applied['id']}/restore", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertIn("no backup file", payload["error"])

    # ----------------------------------------------------- history and state
    def test_history_is_newest_first(self):
        _, first = self.post(self.apply_path(), {"confirm": True})
        _, second = self.post(f"/api/engines/{ENGINE}/history/{first['id']}/restore", {"confirm": True})
        status, payload = self.get(f"/api/engines/{ENGINE}/history")
        self.assertEqual(status, 200)
        self.assertEqual(payload["engine"], ENGINE)
        self.assertEqual([entry["id"] for entry in payload["entries"]], [second["id"], first["id"]])
        self.assertEqual(payload["entries"][0]["action"], "restore")

    def test_history_is_empty_before_the_first_switch(self):
        status, payload = self.get(f"/api/engines/{ENGINE}/history")
        self.assertEqual(status, 200)
        self.assertEqual(payload["entries"], [])
        self.assertFalse((self.dir / "journal").exists(), "reading history created the journal")

    def test_switch_says_nothing_is_running_when_nothing_has_run(self):
        status, payload = self.get(f"/api/engines/{ENGINE}/switch")
        self.assertEqual(status, 200)
        self.assertEqual(payload["running"], False)
        self.assertIsNone(payload["step"])
        self.assertIsNone(payload["entry"])

    def test_switch_reports_the_step_a_running_switch_last_journalled(self):
        journal = self.dir / "journal"
        journal.mkdir()
        apply.journal_path(journal, ENGINE).write_text(
            json.dumps({"id": "sw-1", "outcome": apply.RUNNING,
                        "steps": [{"step": "restart", "ok": True}]}) + "\n"
        )
        apply.acquire_switch_lock(self.dir / "switch-lock", "sw-1")
        self.addCleanup(apply.release_switch_lock, self.dir / "switch-lock", "sw-1")
        status, payload = self.get(f"/api/engines/{ENGINE}/switch")
        self.assertEqual(status, 200)
        self.assertTrue(payload["running"])
        self.assertEqual(payload["step"], "restart")

    def test_history_and_switch_explain_an_engine_with_no_journal(self):
        with mock.patch.dict(wsd.routes.ENGINE_JOURNAL_DIRS, {}, clear=True), \
                mock.patch.object(wsd.routes, "RECORDS_DIR", None):
            for path, key in ((f"/api/engines/{ENGINE}/history", "entries"),
                              (f"/api/engines/{ENGINE}/switch", "running")):
                status, payload = self.get(path)
                self.assertEqual(status, 200, path)
                self.assertFalse(payload[key], path)
                self.assertIn("no switch journal", " ".join(payload["warnings"]), path)
            status, payload = self.post(self.apply_path(), {"confirm": True})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertIn("cannot switch profiles", payload["error"])

    def test_an_unknown_engine_is_still_a_404(self):
        self.assertEqual(self.get("/api/engines/nope/history")[0], 404)
        self.assertEqual(self.get("/api/engines/nope/switch")[0], 404)

    def dry_run_switch(self, make_control):
        """One switch through the real Radiance hooks with the dry-run flag on
        (setUp swapped ``radiance_hooks`` for a fake, so undo that here).
        ``make_control`` builds the radiance-ctl client inside the flag, the
        way the dashboard builds it at start-up; returns the entry, the hooks
        and the sandbox they ran in."""
        radiance = wsd.controls.radiance
        with mock.patch.dict(os.environ, {DRYRUN_ENV: "1"}), \
                mock.patch.dict(radiance._DRYRUN_SANDBOXES, {}, clear=True), \
                mock.patch.object(wsd.controls, "radiance_hooks", radiance.radiance_hooks):
            hooks = radiance.radiance_hooks(ENGINE, "http://127.0.0.1:1", str(self.dir), ACTIVE,
                                            str(self.dir / "journal"), control=make_control())
            entry = apply.apply_profile(hooks, ENGINE, hooks.profiles_dir / CANDIDATE, CANDIDATE,
                                        ready_timeout=0.0)
        sandbox = hooks.profiles_dir.parent
        self.addCleanup(shutil.rmtree, sandbox, True)
        return entry, hooks, sandbox

    def test_a_dry_run_writes_neither_the_real_journal_nor_the_real_profile(self):
        entry, hooks, sandbox = self.dry_run_switch(StubControl)
        self.assertEqual(entry["outcome"], "applied")
        self.assertTrue(entry["dry_run"])
        self.assertEqual(self.active(), ACTIVE_TEXT.encode())
        self.assertFalse((self.dir / "journal").exists(), "the real journal was used")
        self.assertEqual(apply.load_history(self.dir / "journal", ENGINE), [])
        self.assertTrue((hooks.journal_dir / f"{ENGINE}.jsonl").is_file())
        self.assertEqual((sandbox / "profiles" / ACTIVE).read_text(), CANDIDATE_TEXT)

    def test_a_dry_run_never_shells_out(self):
        runs = []
        entry, _hooks, _sandbox = self.dry_run_switch(
            lambda: ControlClient(runner=lambda *args, **kwargs: runs.append(args)))
        self.assertEqual(entry["outcome"], "applied")
        self.assertEqual(runs, [], "a dry run ran the container helper")

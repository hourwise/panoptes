"""OmpAdapter: session parsing, incremental reading, lock states, privacy,
schema contract and the /api/harnesses route.

Fixtures live in tests/fixtures/harness/. Every fixture text is a CANARY
string that must never appear anywhere in a snapshot. The error signals
(failed tool calls, aborted requests) are counts only — never error text.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest import mock

import wsd.routes
import wsd.server
from wsd.harness import build_harnesses
from wsd.harness.base import CAPABILITIES, SCHEMA, TOP_LEVEL
from wsd.harness.omp import OmpAdapter, _iso

FIXTURES = Path(__file__).parent / "fixtures" / "harness"

CANARIES = (
    "CANARY-TITLE-DO-NOT-LEAK",
    "CANARY-USER-PROMPT-DO-NOT-LEAK",
    "CANARY-REASONING-DO-NOT-LEAK",
    "CANARY-ASSISTANT-TEXT-DO-NOT-LEAK",
    "CANARY-TOOL-ARG-DO-NOT-LEAK",
    "CANARY-COMPACTION-SUMMARY-DO-NOT-LEAK",
    "CANARY-DIRTY-PROMPT-DO-NOT-LEAK",
    "CANARY-ERROR-TITLE-DO-NOT-LEAK", "CANARY-ERROR-PROMPT-DO-NOT-LEAK",
    "CANARY-ERROR-TOOL-ARG-DO-NOT-LEAK", "CANARY-TOOL-ERROR-DO-NOT-LEAK",
    "CANARY-TOOL-OK-DO-NOT-LEAK", "CANARY-TOOL-PLAIN-DO-NOT-LEAK",
    "CANARY-SECOND-TOOL-ERROR-DO-NOT-LEAK",
    "secrets.env",
)


def make_sessions_dir(tmp: str, fixture: str = "session-basic.jsonl") -> str:
    """A private sessions dir holding one copy of a fixture."""
    d = Path(tmp) / "sessions"
    d.mkdir(exist_ok=True)
    shutil.copy(FIXTURES / fixture, d / f"{fixture}")
    return str(d)


def adapter(sessions_dir=None, lock_file=None, **kw) -> OmpAdapter:
    return OmpAdapter(id="omp-test", label="Test OMP", sessions_dir=sessions_dir,
                      lock_file=lock_file, **kw)


class SessionParsingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sessions = make_sessions_dir(self.tmp)
        self.snap = adapter(sessions_dir=self.sessions).snapshot()

    def test_session_fields(self):
        s = self.snap["session"]
        self.assertEqual(s["file"], "session-basic.jsonl")
        self.assertEqual(s["started_at"], "2026-11-19T03:00:00.000Z")
        self.assertEqual(s["last_activity_at"], "2026-11-19T03:05:40.000Z")
        self.assertEqual(s["turns"], 5)
        self.assertEqual(s["input_tokens"], 450)
        self.assertEqual(s["output_tokens"], 42)
        self.assertEqual(s["reasoning_tokens"], 10)
        self.assertEqual(s["current_context"], 150)  # last assistant 150 + 0
        self.assertEqual(s["peak_context"], 280)     # 200 + 80
        self.assertEqual(s["compactions"], 1)
        self.assertEqual(s["tool_calls"], {"bash": 2, "read": 1})
        self.assertEqual(s["model"], "radiance/flashnext")
        self.assertEqual(self.snap["warnings"], [])

    def test_file_is_basename_only(self):
        self.assertNotIn("/", self.snap["session"]["file"])

    def test_malformed_lines_skipped_with_warning(self):
        sessions = make_sessions_dir(self.tmp, "session-dirty.jsonl")
        snap = adapter(sessions_dir=sessions).snapshot()
        s = snap["session"]
        self.assertEqual(s["turns"], 3)
        self.assertEqual(s["input_tokens"], 102)
        self.assertEqual(s["peak_context"], 60)  # last assistant 60+0 beats 42+8
        self.assertEqual(s["tool_calls"], {"grep": 1})
        self.assertEqual(len(snap["warnings"]), 1)
        self.assertIn("2 malformed line(s) skipped", snap["warnings"][0])
        self.assertIn("session-dirty.jsonl", snap["warnings"][0])

    def test_no_sessions_dir_is_none(self):
        snap = adapter().snapshot()
        self.assertIsNone(snap["session"])

    def test_empty_dir_is_none(self):
        empty = Path(self.tmp) / "empty"
        empty.mkdir()
        snap = adapter(sessions_dir=str(empty)).snapshot()
        self.assertIsNone(snap["session"])
        self.assertEqual(snap["warnings"], [])

    def test_unreadable_dir_warns(self):
        snap = adapter(sessions_dir=str(Path(self.tmp) / "nope")).snapshot()
        self.assertIsNone(snap["session"])
        self.assertIn("unreadable", "\n".join(snap["warnings"]))


class IncrementalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sessions = Path(make_sessions_dir(self.tmp))
        self.session_file = self.sessions / "session-basic.jsonl"
        self.ad = adapter(sessions_dir=str(self.sessions))

    def append(self, *lines: str) -> None:
        with open(self.session_file, "a") as fh:
            for line in lines:
                fh.write(line + "\n")

    def test_appended_lines_are_added_not_recounted(self):
        first = self.ad.snapshot()["session"]
        self.assertEqual(first["turns"], 5)
        self.append(
            '{"type":"message","timestamp":"2026-11-19T03:06:00.000Z",'
            '"message":{"role":"user","content":[]}}',
            '{"type":"message","timestamp":"2026-11-19T03:06:30.000Z",'
            '"message":{"role":"assistant","model":"flashnext","usage":'
            '{"input":900,"output":7,"cacheRead":100,"reasoningTokens":1},'
            '"content":[{"type":"toolCall","name":"edit"}]}}',
        )
        second = self.ad.snapshot()["session"]
        self.assertEqual(second["turns"], 7)
        self.assertEqual(second["input_tokens"], 450 + 900)
        self.assertEqual(second["output_tokens"], 42 + 7)
        self.assertEqual(second["current_context"], 1000)
        self.assertEqual(second["peak_context"], 1000)
        self.assertEqual(second["tool_calls"], {"bash": 2, "read": 1, "edit": 1})
        self.assertEqual(second["last_activity_at"], "2026-11-19T03:06:30.000Z")

    def test_partial_trailing_line_waits(self):
        self.ad.snapshot()
        with open(self.session_file, "a") as fh:
            fh.write('{"type":"message","timestamp":"2026-11-19T03:0')  # mid-write
        self.assertEqual(self.ad.snapshot()["session"]["turns"], 5)
        with open(self.session_file, "a") as fh:
            fh.write('6:00.000Z","message":{"role":"user","content":[]}}\n')
        self.assertEqual(self.ad.snapshot()["session"]["turns"], 6)

    def test_offset_only_grows(self):
        self.ad.snapshot()
        offset_after_first = self.ad._offset
        self.ad.snapshot()  # nothing new
        self.assertEqual(self.ad._offset, offset_after_first)
        self.append('{"type":"compaction","timestamp":"2026-11-19T03:07:00.000Z"}')
        self.ad.snapshot()
        self.assertGreater(self.ad._offset, offset_after_first)

    def test_rotation_resets(self):
        self.ad.snapshot()
        newer = self.sessions / "newer.jsonl"
        newer.write_text(
            '{"type":"message","timestamp":"2026-11-19T05:00:00.000Z",'
            '"message":{"role":"user","content":[]}}\n'
        )
        snap = self.ad.snapshot()["session"]
        self.assertEqual(snap["file"], "newer.jsonl")
        self.assertEqual(snap["turns"], 1)
        self.assertEqual(snap["input_tokens"], 0)

    def test_truncation_resets(self):
        self.ad.snapshot()
        self.session_file.write_text('{"type":"session","timestamp":"2026-11-19T09:00:00.000Z"}\n')
        snap = self.ad.snapshot()["session"]
        self.assertEqual(snap["turns"], 0)
        self.assertEqual(snap["started_at"], "2026-11-19T09:00:00.000Z")

    def test_newest_by_mtime_wins(self):
        older = self.sessions / "older.jsonl"
        older.write_text('{"type":"session","timestamp":"2026-11-19T01:00:00.000Z"}\n')
        os.utime(older, (time.time() - 1000, time.time() - 1000))
        self.assertEqual(self.ad.snapshot()["session"]["file"], "session-basic.jsonl")


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lock = str(Path(self.tmp) / "worker-active")

    def write_lock(self, text: str) -> None:
        Path(self.lock).write_text(text)

    def test_no_lock_file_configured_is_unknown(self):
        snap = adapter().snapshot()
        self.assertEqual(snap["status"], "unknown")
        self.assertIsNone(snap["active_task"])

    def test_missing_lock_is_idle(self):
        snap = adapter(lock_file=self.lock).snapshot()
        self.assertEqual(snap["status"], "idle")
        self.assertIsNone(snap["active_task"])
        self.assertEqual(snap["warnings"], [])

    def test_dead_pid_is_idle(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.write_lock(f"T-001 {int(time.time()) - 60} {child.pid}\n")
        snap = adapter(lock_file=self.lock).snapshot()
        self.assertEqual(snap["status"], "idle")
        self.assertIsNone(snap["active_task"])

    def test_live_pid_is_working(self):
        start = int(time.time()) - 125
        self.write_lock(f"T-030 {start} {os.getpid()}\n")
        snap = adapter(lock_file=self.lock, now=lambda: start + 125).snapshot()
        self.assertEqual(snap["status"], "working")
        task = snap["active_task"]
        self.assertEqual(task["id"], "T-030")
        self.assertEqual(task["started_at"], _iso(start))
        self.assertTrue(task["started_at"].endswith("Z"))
        self.assertEqual(task["elapsed_s"], 125.0)
        self.assertEqual(set(task), set(SCHEMA["active_task"]))

    def test_malformed_lock_is_idle_with_warning(self):
        self.write_lock("garbage\n")
        snap = adapter(lock_file=self.lock).snapshot()
        self.assertEqual(snap["status"], "idle")
        self.assertIn("cannot tell", "\n".join(snap["warnings"]))

    def test_directory_is_not_a_lock(self):
        Path(self.lock).mkdir()
        snap = adapter(lock_file=self.lock).snapshot()
        self.assertEqual(snap["status"], "idle")


class ErrorSignalTest(unittest.TestCase):
    """failed_tool_calls / aborted_requests — OMP's only error signals.

    OMP writes no retry record, so these are never called retries.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = Path(self.tmp) / "sessions"
        self.dir.mkdir()

    def session_file(self, *fixtures: str) -> Path:
        path = self.dir / "2026-11-19T06-00-00-000Z_err.jsonl"
        with path.open("wb") as sink:
            for name in fixtures:
                sink.write((FIXTURES / name).read_bytes())
        return path

    def snapshot(self, *fixtures: str) -> dict:
        self.session_file(*fixtures)
        return adapter(sessions_dir=str(self.dir)).snapshot()

    def test_error_counts_and_gating(self):
        snap = self.snapshot("session-errors.jsonl")
        s = snap["session"]
        self.assertEqual(s["failed_tool_calls"], 2)
        # e7 is a toolResult carrying stopReason "aborted": not a request.
        self.assertEqual(s["aborted_requests"], 2)
        # tool results are error signals, not turns
        self.assertEqual(s["turns"], 4)
        self.assertEqual(s["tool_calls"], {"bash": 2})
        self.assertEqual(s["model"], "flashnext")
        self.assertEqual(s["started_at"], "2026-11-19T06:00:00.000Z")
        self.assertEqual(s["last_activity_at"], "2026-11-19T06:01:20.000Z")
        self.assertEqual(snap["capabilities"]["errors"], True)
        self.assertEqual(snap["warnings"], [])

    def test_error_fixture_totals(self):
        s = self.snapshot("session-errors.jsonl")["session"]
        self.assertEqual(s["input_tokens"], 400)
        self.assertEqual(s["output_tokens"], 35)
        self.assertEqual(s["reasoning_tokens"], 5)
        self.assertEqual(s["current_context"], 100)
        self.assertEqual(s["peak_context"], 260)
        self.assertEqual(s["compactions"], 0)

    def test_session_without_errors_reports_zero(self):
        s = self.snapshot("session-basic.jsonl")["session"]
        self.assertEqual(s["failed_tool_calls"], 0)
        self.assertEqual(s["aborted_requests"], 0)

    def test_no_retry_word_anywhere(self):
        self.assertNotIn("retr",
                         json.dumps(self.snapshot("session-errors.jsonl")).lower())

    def test_appended_errors_increment_without_recounting(self):
        path = self.session_file("session-errors.jsonl")
        ad = adapter(sessions_dir=str(self.dir))
        first = ad.snapshot()["session"]
        self.assertEqual(
            (first["failed_tool_calls"], first["aborted_requests"]), (2, 2))
        with path.open("ab") as sink:
            sink.write(
                b'{"type":"message","id":"e9","parentId":"e8",'
                b'"timestamp":"2026-11-19T06:02:00.000Z","message":{"role":"toolResult",'
                b'"toolCallId":"t5","isError":true,'
                b'"content":[{"type":"text","text":"boom"}]}}\n')
            sink.write(
                b'{"type":"message","id":"e10","parentId":"e9",'
                b'"timestamp":"2026-11-19T06:02:10.000Z","message":{"role":"assistant",'
                b'"model":"flashnext","stopReason":"aborted",'
                b'"usage":{"input":10,"output":0,"cacheRead":5,"reasoningTokens":0},'
                b'"content":[]}}\n')
        second = ad.snapshot()["session"]
        self.assertEqual(second["failed_tool_calls"], 3)
        self.assertEqual(second["aborted_requests"], 3)
        # earlier records were not recounted: only the two new ones moved
        self.assertEqual(second["turns"], 5)
        self.assertEqual(second["input_tokens"], 410)
        self.assertEqual(second["output_tokens"], 35)
        self.assertEqual(second["last_activity_at"], "2026-11-19T06:02:10.000Z")


class PrivacyTest(unittest.TestCase):
    def test_no_fixture_text_reaches_the_snapshot(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        lock = Path(tmp) / "worker-active"
        lock.write_text(f"T-1 {int(time.time())} {os.getpid()}\n")
        for fixture in ("session-basic.jsonl", "session-dirty.jsonl",
                        "session-errors.jsonl"):
            sessions = make_sessions_dir(tmp, fixture)
            snap = adapter(sessions_dir=sessions, lock_file=str(lock)).snapshot()
            blob = json.dumps(snap)
            for canary in CANARIES:
                self.assertNotIn(canary, blob, f"{canary} leaked from {fixture}")


class SchemaContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lock = Path(self.tmp) / "worker-active"
        self.lock.write_text(f"T-9 {int(time.time())} {os.getpid()}\n")
        self.snap = adapter(
            sessions_dir=make_sessions_dir(self.tmp), lock_file=str(self.lock)
        ).snapshot()

    def test_top_level_keys(self):
        self.assertEqual(tuple(self.snap), TOP_LEVEL)
        self.assertEqual(self.snap["id"], "omp-test")
        self.assertEqual(self.snap["type"], "omp")
        self.assertEqual(self.snap["label"], "Test OMP")
        self.assertIn(self.snap["status"], ("working", "idle", "unknown"))

    def test_section_keys(self):
        self.assertEqual(tuple(self.snap["active_task"]), SCHEMA["active_task"])
        self.assertEqual(tuple(self.snap["session"]), SCHEMA["session"])
        self.assertEqual(tuple(self.snap["capabilities"]), CAPABILITIES)
        self.assertTrue(all(self.snap["capabilities"].values()))

    def test_json_plain(self):
        json.dumps(self.snap)


class BuildHarnessesTest(unittest.TestCase):
    def config(self, *specs):
        return {"engines": [], "harnesses": list(specs), "warnings": []}

    def valid(self, harness_id="h1", **over):
        spec = {"id": harness_id, "type": "omp", "label": harness_id,
                "sessions_dir": "/tmp", "lock_file": "/tmp/lock"}
        spec.update(over)
        return spec

    def test_builds_omp(self):
        harnesses, warnings = build_harnesses(self.config(self.valid()))
        self.assertEqual(list(harnesses), ["h1"])
        self.assertEqual(warnings, [])
        a = harnesses["h1"]
        self.assertIsInstance(a, OmpAdapter)
        self.assertEqual(a.sessions_dir, "/tmp")
        self.assertEqual(a.lock_file, "/tmp/lock")

    def test_unknown_type_skipped(self):
        harnesses, warnings = build_harnesses(
            self.config(self.valid(id="x", type="codex")))
        self.assertEqual(harnesses, OrderedDict())
        self.assertIn("unknown type", "\n".join(warnings))

    def test_duplicate_id_skipped(self):
        harnesses, warnings = build_harnesses(
            self.config(self.valid(), self.valid()))
        self.assertEqual(list(harnesses), ["h1"])
        self.assertIn("duplicate id", "\n".join(warnings))

    def test_optional_paths(self):
        spec = self.valid()
        del spec["lock_file"]
        harnesses, _ = build_harnesses(self.config(spec))
        self.assertIsNone(harnesses["h1"].lock_file)
        self.assertEqual(harnesses["h1"].snapshot()["status"], "unknown")


class RouteTest(unittest.TestCase):
    def test_harnesses_route(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = OrderedDict(omp=adapter(sessions_dir=make_sessions_dir(tmp)))
        with mock.patch.object(wsd.routes, "HARNESSES", fake):
            out = wsd.routes.harnesses()
        self.assertEqual(list(out), ["harnesses", "warnings"])
        self.assertEqual(len(out["harnesses"]), 1)
        self.assertEqual(out["harnesses"][0]["id"], "omp-test")
        self.assertIsInstance(out["warnings"], list)

    def test_registered_on_server(self):
        self.assertIn("/api/harnesses", wsd.server.ROUTES)


if __name__ == "__main__":
    unittest.main()

"""Tests for the smoke gate's decision logic (tools/smoke.py, pure half).

These never touch Chrome: they feed synthetic page reports through
`evaluate_report` / `page_failures` / `page_plan` and assert the gate fails
exactly when a check should fail. `DashboardStartupTest` is the exception: it
really starts the dashboard, in an environment without PYTHONUNBUFFERED, to
prove readiness detection does not depend on a pipe flush.
"""

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tools.smoke import ONLINE, evaluate_report, page_failures, page_plan

REPO_ROOT = Path(__file__).resolve().parents[1]


def clean_page(**overrides):
    page = {
        "name": "#hardware",
        "url": "http://127.0.0.1:9/#hardware",
        "expect_pill": ONLINE,
        "console": [],
        "exceptions": [],
        "failed_requests": [],
        "http_errors": [],
        "pill": ONLINE,
        "hidden_visible": [],
        "overflow": {"scrollWidth": 1700, "innerWidth": 1700},
        "narrow_dials": [],
    }
    page.update(overrides)
    return page


def report(*pages):
    return {"base": "http://127.0.0.1:9", "wait": 10.0, "pages": list(pages)}


class PlanTest(unittest.TestCase):
    def test_plan_covers_every_tab_plus_the_demo(self):
        plan = page_plan("http://127.0.0.1:9/")
        self.assertEqual(
            [p["name"] for p in plan],
            ["#hardware", "#inference", "#engines", "#agents", "#lab", "#control", "demo.html"],
        )
        self.assertEqual(plan[0]["url"], "http://127.0.0.1:9/#hardware")
        self.assertEqual(plan[-1]["url"], "http://127.0.0.1:9/static/demo.html")

    def test_dashboard_tabs_expect_the_online_pill(self):
        plan = page_plan("http://127.0.0.1:9")
        for tab in plan[:6]:
            self.assertEqual(tab["expect_pill"], ONLINE)
        self.assertIsNone(plan[6]["expect_pill"])  # demo has no pill


class CleanReportTest(unittest.TestCase):
    def test_all_green_passes(self):
        self.assertEqual(evaluate_report(report(clean_page(), clean_page(name="demo.html"))), [])


class ConsoleTest(unittest.TestCase):
    def test_console_error_fails_with_its_text(self):
        page = clean_page(console=[{"level": "error", "text": "kaboom at app.js:12"}])
        failures = page_failures(page)
        self.assertEqual(len(failures), 1)
        self.assertIn("console error", failures[0])
        self.assertIn("kaboom", failures[0])
        self.assertTrue(failures[0].startswith("#hardware:"))

    def test_console_warning_fails(self):
        page = clean_page(console=[{"level": "warning", "text": "deprecated"}])
        self.assertEqual(len(page_failures(page)), 1)


class ExceptionTest(unittest.TestCase):
    def test_uncaught_exception_fails(self):
        page = clean_page(exceptions=[{"text": "ReferenceError: x is not defined"}])
        failures = page_failures(page)
        self.assertEqual(len(failures), 1)
        self.assertIn("uncaught", failures[0])


class RequestTest(unittest.TestCase):
    def test_failed_request_fails_with_url(self):
        page = clean_page(failed_requests=[{"url": "/static/app.js", "error": "net::ERR_ABORTED"}])
        failures = page_failures(page)
        self.assertEqual(len(failures), 1)
        self.assertIn("/static/app.js", failures[0])

    def test_http_error_fails_with_status(self):
        page = clean_page(http_errors=[{"url": "/static/ui/theme-broken.css", "status": 404}])
        failures = page_failures(page)
        self.assertEqual(len(failures), 1)
        self.assertIn("404", failures[0])
        self.assertIn("theme-broken.css", failures[0])


class PillTest(unittest.TestCase):
    def test_wrong_pill_fails(self):
        page = clean_page(pill="DEGRADED")
        failures = page_failures(page)
        self.assertEqual(len(failures), 1)
        self.assertIn("DEGRADED", failures[0])
        self.assertIn(ONLINE, failures[0])

    def test_missing_pill_on_dashboard_fails(self):
        self.assertEqual(len(page_failures(clean_page(pill=None))), 1)

    def test_demo_without_pill_passes(self):
        self.assertEqual(page_failures(clean_page(name="demo.html", expect_pill=None, pill=None)), [])


class HiddenTest(unittest.TestCase):
    def test_hidden_but_rendered_fails(self):
        page = clean_page(hidden_visible=["div#gpu-grid.card"])
        failures = page_failures(page)
        self.assertEqual(len(failures), 1)
        self.assertIn("hidden", failures[0])
        self.assertIn("div#gpu-grid.card", failures[0])


class OverflowTest(unittest.TestCase):
    def test_horizontal_overflow_fails(self):
        page = clean_page(overflow={"scrollWidth": 1720, "innerWidth": 1700})
        failures = page_failures(page)
        self.assertEqual(len(failures), 1)
        self.assertIn("overflow", failures[0])
        self.assertIn("1720", failures[0])

    def test_equal_widths_pass(self):
        self.assertEqual(page_failures(clean_page(overflow={"scrollWidth": 1700, "innerWidth": 1700})), [])


class DialRowTest(unittest.TestCase):
    def test_narrow_dial_row_fails(self):
        page = clean_page(
            narrow_dials=[{"selector": "div#cpu-dials.dials", "dials": 2, "width": 200, "needed": 274}]
        )
        failures = page_failures(page)
        self.assertEqual(len(failures), 1)
        self.assertIn("dial", failures[0])
        self.assertIn("#cpu-dials", failures[0])

    def test_rows_with_one_dial_are_never_listed(self):
        # the probe itself skips <2-dial rows; the report stays empty
        self.assertEqual(page_failures(clean_page(narrow_dials=[])), [])


class AggregationTest(unittest.TestCase):
    def test_failures_across_pages_are_all_reported(self):
        good = clean_page(name="#agents")
        bad_one = clean_page(name="#control", console=[{"level": "error", "text": "a"}])
        bad_two = clean_page(name="demo.html", expect_pill=None, pill=None,
                             overflow={"scrollWidth": 1900, "innerWidth": 1700},
                             http_errors=[{"url": "/x.js", "status": 500}])
        failures = evaluate_report(report(good, bad_one, bad_two))
        self.assertEqual(len(failures), 3)
        self.assertEqual(
            {f.split(":")[0] for f in failures}, {"#control", "demo.html"}
        )


class FrameTest(unittest.TestCase):
    """RFC 6455 client frames: the two Chrome-silence bugs were a missing
    4-byte mask key in the frame and a doubled extended-length marker byte."""

    @staticmethod
    def _decode(frame):
        assert frame[0] == 0x81, "FIN + text opcode"
        assert frame[1] & 0x80, "client frames must be masked"
        n = frame[1] & 0x7F
        if n < 126:
            off = 2
        elif n == 126:
            assert frame[1] == 0xFE, "16-bit length: single marker byte"
            off = 4
            n = int.from_bytes(frame[2:4], "big")
        else:
            assert frame[1] == 0xFF, "64-bit length: single marker byte"
            off = 10
            n = int.from_bytes(frame[2:10], "big")
        mask = frame[off:off + 4]
        payload = frame[off + 4:off + 4 + n]
        assert len(payload) == n
        assert len(frame) == off + 4 + n, "no trailing bytes"
        return bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

    def test_roundtrip_all_length_encodings(self):
        from tools.cdp import WebSocket
        ws = object.__new__(WebSocket)
        for size in (1, 125, 126, 300, 70000):
            with self.subTest(size=size):
                text = "x" * size
                frame = ws._frame(0x1, text.encode())
                self.assertEqual(self._decode(frame), text.encode())


class DashboardStartupTest(unittest.TestCase):
    """`start_dashboard` must notice readiness without anyone flushing a pipe.

    The dashboard announces itself with a single print(). On a pipe that is
    block buffered, so a start_dashboard that waits for the text hangs forever
    whenever PYTHONUNBUFFERED is not exported — which is the normal case. The
    child below runs with that variable removed on purpose.
    """

    CHILD = """
import os
import sys
import time
import urllib.request

assert "PYTHONUNBUFFERED" not in os.environ, "the child env must be clean"

from tools.smoke import _free_port, _stop_server, start_dashboard

port = _free_port()
started = time.monotonic()
proc = start_dashboard(port, timeout=12.0)
try:
    with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=2) as r:
        assert r.status == 200, r.status
    print("READY after %.1fs" % (time.monotonic() - started))
finally:
    _stop_server(proc)
"""

    def test_ready_within_the_deadline_without_pythonunbuffered(self):
        env = {key: value for key, value in os.environ.items() if key != "PYTHONUNBUFFERED"}
        started = time.monotonic()
        try:
            result = subprocess.run(
                [sys.executable, "-c", self.CHILD],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            self.fail(f"start_dashboard hung without PYTHONUNBUFFERED: {out}")
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("READY after", result.stdout)
        self.assertLess(elapsed, 30.0, f"starting the dashboard took {elapsed:.1f}s")


if __name__ == "__main__":
    unittest.main()

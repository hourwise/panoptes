#!/usr/bin/env python3
"""Browser smoke test for the dashboard — the gate before every UI receipt.

    python3 tools/smoke.py [--url URL] [--wait 10] [--timeout 180] [--json]

Without --url the dashboard is started on a free loopback port and stopped
afterwards. Each tab (#hardware ... #control) plus /static/demo.html is
loaded in headless Chrome (tools/cdp.py) and checked for: console
errors/warnings, uncaught exceptions, failed requests, HTTP >= 400, a wrong
status pill, elements that render despite the `hidden` attribute, horizontal
overflow and dial rows too narrow for their dials (the T-007 collapse).

Exit codes: 0 every check passed, 1 a check failed, 2 the run itself could not
be completed (the server never came up, Chrome died, `--timeout` ran out).
Every blocking step is bounded, so a hang is always a loud failure.

The decision logic (report -> list of failures) is pure and unit-tested in
tests/test_smoke_logic.py; everything below `collect_page` touches I/O.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any

try:
    from . import cdp as _cdp
except ImportError:  # run as `python3 tools/smoke.py`
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import cdp as _cdp

TABS = ("hardware", "inference", "engines", "agents", "lab", "control")
ONLINE = "SYSTEM ONLINE"

SERVER_READY_TIMEOUT = 15.0  # /api/health must answer within this
CHROME_STARTUP_TIMEOUT = 45.0  # Chrome's DevToolsActivePort within this
PAGE_HEADROOM = 20.0  # load + probe around the `--wait` watch
OVERALL_TIMEOUT = 180.0  # the whole run, unless --timeout says otherwise

# Runs in the page; returns everything the pure checks need from the DOM.
PROBE_JS = """
(() => {
  const sel = (el) => el.tagName.toLowerCase()
    + (el.id ? "#" + el.id : "")
    + (typeof el.className === "string" && el.className.trim()
        ? "." + el.className.trim().split(/\\s+/).join(".") : "");
  const out = {pill: null, hidden_visible: [], narrow_dials: [], overflow: null};
  const pillEl = document.querySelector("#api-lamp span");
  if (pillEl) out.pill = pillEl.textContent.trim();
  for (const el of document.querySelectorAll("[hidden]")) {
    const r = el.getBoundingClientRect();
    if (el.offsetParent !== null || (r.width > 0 && r.height > 0)) out.hidden_visible.push(sel(el));
  }
  out.overflow = {scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth};
  for (const c of document.querySelectorAll(".dials")) {
    const kids = Array.from(c.children).filter((k) => k.classList.contains("dial"));
    if (kids.length < 2) continue;
    if (c.offsetParent === null) continue;  // hidden panel: not painted, cannot overflow
    const gap = parseFloat(getComputedStyle(c).columnGap) || 0;
    const needed = kids[0].getBoundingClientRect().width + kids[1].getBoundingClientRect().width + gap;
    const width = c.getBoundingClientRect().width;
    if (width + 1 < needed) {
      out.narrow_dials.push({selector: sel(c), dials: kids.length,
                             width: Math.round(width), needed: Math.round(needed)});
    }
  }
  return out;
})()
"""


class SmokeError(RuntimeError):
    """A step could not be completed: the run is inconclusive, not clean."""


class DeadlineExpired(SmokeError):
    """The overall `--timeout` budget ran out; the run stopped instead of hanging."""


class Deadline:
    """One budget for the whole run; every blocking step takes its slice from it."""

    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    @property
    def left(self) -> float:
        return self._end - time.monotonic()

    def slice(self, want: float, floor: float = 0.0) -> float:
        """`want` seconds, cut down to what is left of the budget."""
        return max(floor, min(want, self.left))

    def require(self, need: float, what: str) -> None:
        """Refuse to start a step the budget cannot finish, out loud."""
        if self.left < need:
            raise DeadlineExpired(
                f"{what} needs {need:.1f}s, only {max(self.left, 0.0):.1f}s "
                f"left of the overall timeout"
            )


# --------------------------------------------------------------- pure logic


def page_plan(base: str) -> list[dict[str, Any]]:
    """The pages to visit: (name, url, expected status pill or None)."""
    base = base.rstrip("/")
    plan = [{"name": f"#{tab}", "url": f"{base}/#{tab}", "expect_pill": ONLINE} for tab in TABS]
    plan.append({"name": "demo.html", "url": f"{base}/static/demo.html", "expect_pill": None})
    return plan


def page_failures(page: dict[str, Any]) -> list[str]:
    """Every check on one collected page; empty list means the page passes."""
    name = page["name"]
    fails: list[str] = []
    for entry in page["console"]:
        fails.append(f"{name}: console {entry['level']}: {entry['text']}")
    for entry in page["exceptions"]:
        fails.append(f"{name}: uncaught exception: {entry['text']}")
    for entry in page["failed_requests"]:
        fails.append(f"{name}: request failed: {entry['url']} ({entry['error']})")
    for entry in page["http_errors"]:
        fails.append(f"{name}: HTTP {entry['status']}: {entry['url']}")
    if page.get("expect_pill") is not None and page.get("pill") != page["expect_pill"]:
        fails.append(f"{name}: status pill is {page.get('pill')!r}, expected {page['expect_pill']!r}")
    for selector in page["hidden_visible"]:
        fails.append(f"{name}: rendered despite hidden attribute: {selector}")
    overflow = page.get("overflow")
    if overflow and overflow["scrollWidth"] > overflow["innerWidth"]:
        fails.append(
            f"{name}: horizontal overflow: scrollWidth {overflow['scrollWidth']} "
            f"> innerWidth {overflow['innerWidth']}"
        )
    for dial in page["narrow_dials"]:
        fails.append(
            f"{name}: dial row {dial['selector']} holds {dial['dials']} dials but is "
            f"only {dial['width']}px wide (needs {dial['needed']}px)"
        )
    return fails


def evaluate_report(report: dict[str, Any]) -> list[str]:
    """All failures across all pages of a collected report."""
    return [failure for page in report["pages"] for failure in page_failures(page)]


def format_summary(report: dict[str, Any]) -> str:
    lines = [f"smoke {report['base']}  (wait {report['wait']}s per page)"]
    total = 0
    for page in report["pages"]:
        failures = page_failures(page)
        total += len(failures)
        lines.append(f"  {page['name']:<12} {'ok' if not failures else f'FAIL ({len(failures)})'}")
        for failure in failures:
            lines.append(f"      - {failure[len(page['name']) + 2:]}")
    if total:
        lines.append(f"FAIL: {total} problem(s) across {len(report['pages'])} pages")
    else:
        lines.append(
            f"PASS: {len(report['pages'])} pages clean — no console errors, no failed "
            f"requests, no layout faults"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------ I/O glue


def _arg_text(arg: dict[str, Any]) -> str:
    if "value" in arg:
        return str(arg["value"])
    return str(arg.get("description") or arg.get("className") or arg.get("type") or "?")


def _exception_text(details: dict[str, Any]) -> str:
    exception = details.get("exception") or {}
    return str(details.get("text") or exception.get("description") or exception.get("value") or "?")


def collect_page(
    chrome: "_cdp.Chrome", plan: dict[str, str], wait: float, deadline: Deadline | None = None
) -> dict[str, Any]:
    """Load one page in a fresh target, watch it for `wait`s, probe the DOM."""
    if deadline is not None:
        wait = deadline.slice(wait, floor=0.1)
    assert chrome.cdp is not None
    chrome.cdp.clear_events()
    page = chrome.open_page()
    chrome.cdp.call("Log.enable", session_id=page.session_id)
    console: list[dict] = []
    exceptions: list[dict] = []
    failed_requests: list[dict] = []
    http_errors: list[dict] = []
    request_urls: dict[str, str] = {}
    try:
        chrome.navigate(page, plan["url"])
        chrome.cdp.pump(wait)
        for event in chrome.cdp.events_for(page.session_id):
            method = event.get("method")
            params = event.get("params", {})
            if method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "warning"):
                text = " ".join(_arg_text(a) for a in params.get("args", [])).strip()
                console.append({"level": params["type"], "text": text})
            elif method == "Log.entryAdded":
                entry = params.get("entry", {})
                if entry.get("level") in ("error", "warning"):
                    text = str(entry.get("text", "")).strip()
                    if entry.get("url"):
                        text += f" [{entry['url']}]"
                    console.append({"level": entry["level"], "text": text})
            elif method == "Runtime.exceptionThrown":
                exceptions.append({"text": _exception_text(params.get("exceptionDetails", {}))})
            elif method == "Network.requestWillBeSent":
                request_urls[params.get("requestId", "")] = params.get("request", {}).get("url", "?")
            elif method == "Network.responseReceived":
                response = params.get("response", {})
                if int(response.get("status", 0)) >= 400:
                    url = response.get("url") or request_urls.get(params.get("requestId", ""), "?")
                    http_errors.append({"url": url, "status": response["status"]})
            elif method == "Network.loadingFailed":
                request_id = params.get("requestId", "")
                if request_id in request_urls:  # ignore aborted requests of earlier pages
                    failed_requests.append(
                        {"url": request_urls[request_id], "error": params.get("errorText", "?")}
                    )
        dom = chrome.evaluate(page, PROBE_JS) or {}
    finally:
        chrome.close_page(page)
    return {
        "name": plan["name"],
        "url": plan["url"],
        "expect_pill": plan["expect_pill"],
        "console": console,
        "exceptions": exceptions,
        "failed_requests": failed_requests,
        "http_errors": http_errors,
        "pill": dom.get("pill"),
        "hidden_visible": dom.get("hidden_visible", []),
        "overflow": dom.get("overflow"),
        "narrow_dials": dom.get("narrow_dials", []),
    }


def run_smoke(base: str, wait: float, deadline: Deadline | None = None) -> dict[str, Any]:
    pages = []
    startup = CHROME_STARTUP_TIMEOUT
    if deadline is not None:
        startup = deadline.slice(CHROME_STARTUP_TIMEOUT, floor=1.0)
    with _cdp.Chrome(startup_timeout=startup) as chrome:
        for plan in page_plan(base):
            if deadline is not None:
                deadline.require(wait + PAGE_HEADROOM, f"checking {plan['name']}")
            print(f"  smoke: checking {plan['name']} ...", file=sys.stderr, flush=True)
            pages.append(collect_page(chrome, plan, wait, deadline))
    return {"base": base, "wait": wait, "pages": pages}


# ------------------------------------------------------------ dashboard mgmt


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _log_tail(log, lines: int = 10) -> str:
    """The last lines the child wrote, for a failure message."""
    try:
        log.seek(0)
        text = log.read().decode("utf-8", "replace").strip()
    except OSError:
        return "(no output)"
    return "\n".join(text.splitlines()[-lines:]) or "(no output)"


def health_ok(base: str, timeout: float = 1.0) -> bool:
    """True once something answers HTTP on `<base>/api/health`.

    Any status under 500 counts: this only proves the port is serving HTTP.
    Whether the dashboard is actually healthy is the pill check's job.
    """
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/api/health", timeout=timeout) as response:
            return int(response.status) < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, OSError):
        return False


def start_dashboard(port: int, timeout: float = SERVER_READY_TIMEOUT) -> subprocess.Popen:
    """`python3 -u -m wsd --port N` from the repo root, ready to serve.

    Readiness is polled over HTTP, never read off the child's stdout: that is
    a pipe, so its print() sits in a block buffer and waiting for the URL line
    hangs forever unless the child happens to run unbuffered. The child runs
    with `-u` and PYTHONUNBUFFERED anyway, and its output goes to a temporary
    file used only for the failure message.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log = tempfile.TemporaryFile(mode="w+b")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "wsd", "--port", str(port)],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=dict(os.environ, PYTHONUNBUFFERED="1"),
    )
    atexit.register(_stop_server, proc)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = _log_tail(log)
            _stop_server(proc)
            raise SmokeError(
                f"the dashboard server exited with status {proc.returncode} "
                f"before it answered /api/health:\n{tail}"
            )
        if health_ok(f"http://127.0.0.1:{port}"):
            log.close()
            return proc
        time.sleep(0.1)
    tail = _log_tail(log)
    _stop_server(proc)
    raise SmokeError(
        f"the dashboard did not answer /api/health on 127.0.0.1:{port} "
        f"within {timeout}s:\n{tail}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smoke", description=__doc__.splitlines()[0])
    parser.add_argument("--url", help="dashboard base URL; start our own server if omitted")
    parser.add_argument("--wait", type=float, default=10.0, help="seconds to watch each page")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument(
        "--timeout",
        type=float,
        default=OVERALL_TIMEOUT,
        help="overall budget in seconds (default %(default)s)",
    )
    args = parser.parse_args(argv)

    deadline = Deadline(args.timeout)
    server = None
    try:
        if args.url:
            base = args.url
        else:
            port = _free_port()
            server = start_dashboard(port, timeout=deadline.slice(SERVER_READY_TIMEOUT, floor=0.1))
            base = f"http://127.0.0.1:{port}"
        report = run_smoke(base, args.wait, deadline)
    except DeadlineExpired as exc:
        return _abort(args.json, f"overall timeout of {args.timeout:.0f}s expired: {exc}", 2)
    except _cdp.CDPError as exc:
        return _abort(args.json, f"browser step failed: {exc}", 2)
    except SmokeError as exc:
        return _abort(args.json, str(exc), 2)
    except KeyboardInterrupt:
        return _abort(args.json, "interrupted", 130)
    finally:
        if server is not None:
            _stop_server(server)
            atexit.unregister(_stop_server)

    failures = evaluate_report(report)
    if args.json:
        print(json.dumps({"report": report, "failures": failures}, indent=2))
    else:
        print(format_summary(report))
    return 1 if failures else 0


def _abort(as_json: bool, message: str, code: int) -> int:
    """An unfinished run is reported, never swallowed."""
    if as_json:
        print(json.dumps({"error": message}))
    else:
        print(f"FAIL: {message}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())

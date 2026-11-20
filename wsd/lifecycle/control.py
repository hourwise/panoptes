"""Radiance container control through the passwordless sudo helper.

The only sanctioned way to touch the container is exactly
``sudo -n /usr/local/sbin/radiance-ctl <op>`` with ``op`` in :data:`OPS`.
The helper itself refuses stop/restart while a supervised worker task holds
its lock (:func:`worker_active`) or Radiance has requests in flight; this
client never tries to bypass that and never runs any other command.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

HELPER = "/usr/local/sbin/radiance-ctl"
OPS = frozenset({"status", "start", "stop", "restart", "logs"})
OP_TIMEOUT = 60.0
LOGS_TIMEOUT = 15.0
STATUS_CACHE_TTL = 5.0
LOG_MAX_LINES = 200
LOG_MAX_BYTES = 64 * 1024
WORKER_LOCK = Path.home() / ".local" / "state" / "lab01" / "worker-active"
DRYRUN_ENV = "PANOPTES_CONTROL_DRYRUN"
DRYRUN_LOCKED_ENV = "PANOPTES_CONTROL_DRYRUN_LOCKED"
# Canned results for the dry-run switch: the UI can be exercised (and tested)
# without any risk of touching the real container. Dry run never calls sudo.
DRYRUN_STATUS = {
    "ok": True,
    "op": "status",
    "dry_run": True,
    "name": "/radiance-dryrun",
    "image": "radiance:local",
    "state": "running",
    "started_at": "2026-11-18T08:41:07Z",
    "network_mode": "host",
    "ports": "",
    "restart_policy": "unless-stopped",
}
DRYRUN_LOGS = "\n".join(
    f"2026-11-18T08:4{1 + i}:00Z dry-run fake log line {i + 1}" for i in range(5)
)


def worker_active(path: Path | str = WORKER_LOCK) -> bool:
    """True while a supervised worker task holds its lock file.

    The lock is a regular file ``<task-id> <epoch> <pid>``; the task is
    active while the owning process is alive. Missing, unreadable, empty or
    non-numeric content all count as inactive.
    """
    try:
        fields = Path(path).read_text().split()
    except OSError:
        return False
    if not fields:
        return False
    try:
        pid = int(fields[-1])
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by someone else
    return True


def _tail(text: str) -> str:
    """The last ``LOG_MAX_LINES`` lines of text, capped at ``LOG_MAX_BYTES``."""
    clipped = text.encode("utf-8", "replace")[-LOG_MAX_BYTES:]
    return "\n".join(clipped.decode("utf-8", "ignore").splitlines()[-LOG_MAX_LINES:])


class ControlClient:
    """Runs the five fixed radiance-ctl operations through ``runner``.

    ``runner`` defaults to ``subprocess.run`` and is injected in tests so the
    lifecycle never fires a real helper call outside the read-only ones.
    With ``PANOPTES_CONTROL_DRYRUN=1`` in ``env`` at construction time the
    client never calls sudo at all: every op answers with a canned result
    carrying ``dry_run: true``. In dry run the worker interlock is answered
    from ``PANOPTES_CONTROL_DRYRUN_LOCKED=1`` (lock on) instead of reading
    the real lock file, so the UI can be exercised in both lock states.
    """

    def __init__(self, runner: Callable = subprocess.run, env: Mapping | None = None) -> None:
        self._runner = runner
        self._dry_run = (os.environ if env is None else env).get(DRYRUN_ENV, "") == "1"
        self._dry_locked = (os.environ if env is None else env).get(DRYRUN_LOCKED_ENV, "") == "1"
        self._status_cache: tuple[float, dict] | None = None

    def run(self, op: str) -> object:
        """Run one helper op; anything outside :data:`OPS` raises before running."""
        if op not in OPS:
            raise ValueError(f"operation not allowed: {op!r}")
        if self._dry_run:
            return self._dry_result(op)
        timeout = LOGS_TIMEOUT if op == "logs" else OP_TIMEOUT
        argv = ["sudo", "-n", HELPER, op]
        try:
            proc = self._runner(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"{op} timed out after {timeout:g} s"}
        except OSError as exc:  # sudo or the helper missing
            return {"ok": False, "error": f"{op} failed to run: {exc}"}
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            return {
                "ok": False,
                "error": detail or f"{op} exited with code {proc.returncode}",
            }
        if op == "logs":
            return _tail(proc.stdout or "")
        try:
            return json.loads(proc.stdout)
        except ValueError:
            return {"ok": False, "error": "helper did not return JSON"}

    def _dry_result(self, op: str) -> object:
        """Canned answer for the dry-run switch; ``runner`` stays untouched."""
        if op == "status":
            return dict(DRYRUN_STATUS)
        if op == "logs":
            return DRYRUN_LOGS
        return {"ok": True, "op": op, "dry_run": True}

    def status(self) -> dict:
        """Helper status, cached for ``STATUS_CACHE_TTL`` seconds."""
        now = time.monotonic()
        if self._status_cache is not None and now - self._status_cache[0] < STATUS_CACHE_TTL:
            return self._status_cache[1]
        result = self.run("status")
        self._status_cache = (now, result)
        return result

    def start(self) -> dict:
        return self.run("start")

    def stop(self) -> dict:
        return self.run("stop")

    def restart(self) -> dict:
        return self.run("restart")

    def logs(self) -> object:
        """Log text (tail), or an ``{"ok": false, ...}`` error dict."""
        return self.run("logs")

    def worker_active(self, path: Path | str = WORKER_LOCK) -> bool:
        """The worker-task interlock. Dry run never reads the lock file: it
        answers from :data:`DRYRUN_LOCKED_ENV` so both lock states are
        testable without touching the real supervised-worker state."""
        if self._dry_run:
            return self._dry_locked
        return worker_active(path)

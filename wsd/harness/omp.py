"""OMP harness adapter: live worker activity from the worker lock and the
profile's session log.

Two read-only sources:

* ``lock_file`` (e.g. ``~/.local/state/lab01/worker-active``) — a regular
  file ``TASK START_EPOCH PID`` written by the supervisor while a task runs.
  Working while the file exists as a regular file and PID is alive.

* ``sessions_dir`` — one ``*.jsonl`` per session, the most recently modified
  one is the live session. Every line is a JSON record; this adapter reads
  it *incrementally*: the byte offset of the last complete line is kept
  between snapshots and only the bytes appended since are parsed, so a
  multi-megabyte session costs a few kilobytes per poll. A rotation (a new
  newest file) or a truncation restarts the scan from zero.

Privacy: records are distilled to counts, tool names, sizes and timestamps;
message text, reasoning, tool arguments, error text and file contents are never
kept in memory beyond the parsed line and never reach the snapshot.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .base import SCHEMA, HarnessAdapter


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by someone else
    return True


def _new_agg() -> dict:
    return {
        "turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "current_context": None,
        "peak_context": None,
        "compactions": 0,
        "tool_calls": {},
        "failed_tool_calls": 0,
        "aborted_requests": 0,
        "started_at": None,
        "last_activity_at": None,
        "model": None,
        "model_switch": None,
        "malformed": 0,
    }


class OmpAdapter(HarnessAdapter):
    """The local OMP profile: worker lock plus session log."""

    type = "omp"

    def __init__(
        self,
        id: str = "",
        label: str = "",
        sessions_dir: str | None = None,
        lock_file: str | None = None,
        now=time.time,
    ):
        super().__init__(id, label)
        self.sessions_dir = sessions_dir
        self.lock_file = lock_file
        self._now = now
        # incremental session reader state; reset when the file rotates.
        self._file: str | None = None
        self._offset = 0
        self._agg = _new_agg()

    # -- activity ----------------------------------------------------------

    def _activity(self, warnings: list) -> tuple[str, dict | None]:
        """(status, active_task) from the lock file."""
        if not self.lock_file:
            return "unknown", None
        path = Path(self.lock_file)
        try:
            text = path.read_text()
        except OSError:
            return "idle", None  # missing or unreadable: nobody holds the lock
        fields = text.split()
        try:
            task_id, start_epoch, pid = fields[0], float(fields[1]), int(fields[2])
        except (IndexError, ValueError):
            warnings.append(
                f"{path.name}: not 'TASK START_EPOCH PID', cannot tell who runs"
            )
            return "idle", None
        if pid <= 0 or not _pid_alive(pid):
            return "idle", None
        elapsed = max(0.0, self._now() - start_epoch)
        return "working", {
            "id": task_id,
            "started_at": _iso(start_epoch),
            "elapsed_s": round(elapsed, 1),
        }

    # -- session -----------------------------------------------------------

    def _latest_session(self) -> str | None:
        try:
            with os.scandir(self.sessions_dir) as scan:
                entries = [
                    e for e in scan if e.name.endswith(".jsonl") and e.is_file()
                ]
        except OSError as exc:
            return f"__dir__:{exc}"  # surfaced as a warning by _session
        if not entries:
            return None
        newest = max(entries, key=lambda e: e.stat().st_mtime)
        return newest.path

    def _consume(self, path: str) -> None:
        """Parse the bytes appended to ``path`` since the last snapshot."""
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if self._file != path or size < self._offset:
            self._file = path
            self._offset = 0
            self._agg = _new_agg()
        if size == self._offset:
            return
        with open(path, "rb") as fh:
            fh.seek(self._offset)
            data = fh.read()
        # A partial trailing line belongs to a record still being written;
        # leave it unconsumed and pick it up on the next snapshot.
        if data and not data.endswith(b"\n"):
            cut = data.rfind(b"\n")
            if cut < 0:
                return
            data = data[: cut + 1]
        self._offset += len(data)
        for raw in data.split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            self._record(raw)

    def _record(self, raw: bytes) -> None:
        agg = self._agg
        try:
            rec = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            agg["malformed"] += 1
            return
        if not isinstance(rec, dict):
            agg["malformed"] += 1
            return
        stamp = rec.get("timestamp")
        if isinstance(stamp, str) and stamp:
            if agg["started_at"] is None:
                agg["started_at"] = stamp
            agg["last_activity_at"] = stamp
        kind = rec.get("type")
        if kind == "compaction":
            agg["compactions"] += 1
        elif kind == "model_change" and isinstance(rec.get("model"), str):
            agg["model_switch"] = rec["model"]
        elif kind == "message" and isinstance(rec.get("message"), dict):
            self._message(rec["message"])

    def _message(self, msg: dict) -> None:
        agg = self._agg
        role = msg.get("role")
        if role == "toolResult":
            # An error signal, not a turn: counted, text never kept.
            if msg.get("isError") is True:
                agg["failed_tool_calls"] += 1
            return
        if role not in ("user", "assistant"):
            return
        agg["turns"] += 1
        if role != "assistant":
            return
        if msg.get("stopReason") == "aborted":
            agg["aborted_requests"] += 1
        if isinstance(msg.get("model"), str) and msg["model"]:
            agg["model"] = msg["model"]
        usage = msg.get("usage")
        if isinstance(usage, dict):
            def num(key: str) -> int:
                value = usage.get(key)
                return value if isinstance(value, (int, float)) else 0

            agg["input_tokens"] += int(num("input"))
            agg["output_tokens"] += int(num("output"))
            agg["reasoning_tokens"] += int(num("reasoningTokens"))
            context = int(num("input")) + int(num("cacheRead"))
            agg["current_context"] = context
            if agg["peak_context"] is None or context > agg["peak_context"]:
                agg["peak_context"] = context
        content = msg.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "toolCall":
                    name = item.get("name")
                    if isinstance(name, str) and name:
                        agg["tool_calls"][name] = agg["tool_calls"].get(name, 0) + 1

    def _session(self, warnings: list) -> dict | None:
        if not self.sessions_dir:
            return None
        latest = self._latest_session()
        if isinstance(latest, str) and latest.startswith("__dir__:"):
            warnings.append(f"sessions_dir {self.sessions_dir!r} unreadable: {latest[8:]}")
            return None
        if latest is None:
            self._file = None
            self._offset = 0
            self._agg = _new_agg()
            return None
        self._consume(latest)
        agg = self._agg
        if agg["malformed"]:
            warnings.append(
                f"{os.path.basename(latest)}: {agg['malformed']} malformed line(s) skipped"
            )
        session = _empty_session()
        agg["model"] = agg["model_switch"] or agg["model"]
        session.update(agg, file=os.path.basename(latest))
        session.pop("malformed")
        session.pop("model_switch")
        return session

    # -- snapshot ----------------------------------------------------------

    def snapshot(self) -> dict:
        warnings: list[str] = []
        status, active_task = self._activity(warnings)
        session = self._session(warnings)
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "status": status,
            "active_task": active_task,
            "session": session,
            "capabilities": {
                "live_session": True,
                "tokens": True,
                "compactions": True,
                "tool_calls": True,
                "errors": True,
            },
            "warnings": warnings,
        }


def _empty_session() -> dict:
    """A session dict with exactly the SCHEMA keys, every value None."""
    return {key: None for key in SCHEMA["session"]}

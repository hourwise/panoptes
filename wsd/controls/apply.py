"""The profile-apply pipeline: journal, backup, atomic write, rollback.

Engine-agnostic. Everything engine-specific — is it busy, how to restart it,
when it answers ``/health``, when it will really serve the new profile — sits
behind the small :class:`EngineHooks` interface, so the same steps drive
Radiance today and llama.cpp tomorrow.

The steps, each appended to the journal entry as it happens:

1. preconditions — no live worker lock, engine idle, no other switch running
   (``profile-switch-active``, same ``<id> <epoch> <pid>`` format as the worker
   lock, dead pid = stale). Anything live refuses the switch and touches
   nothing. The lock is published by hard-linking a fully written temp file,
   so a visible lock is never empty or partial, and a stale one is only taken
   over after the file actually moved aside has been re-checked.
2. validate — the engine's approved-parameter check (``EngineHooks.validate``);
   any error refuses the switch, still touching nothing.
3. backup — the active file into ``<journal_dir>/backups/<UTC stamp>-<hash>.yaml``.
4. write — atomic: unique temp file in the same directory + ``os.replace``.
5. restart — whatever ``hooks.restart()`` does (Radiance: the existing
   radiance-ctl client). No new root privileges, no other sudo path.
6. healthy AND ready — within ``ready_timeout`` (default 600 s): the budget for
7. on any failure after the write: write the backup back (atomic), restart and
   wait again — with its own ``rollback_timeout`` (default 600 s), never the
   forward budget, so tightening the forward knob never tightens recovery.
   Outcome ``rolled_back``, or ``rollback_failed`` when even that did not work,
   which says out loud that the engine needs attention. Both budgets are
   recorded on the entry as ``budgets``.

The journal is append-only JSONL (``<journal_dir>/<engine>.jsonl``): one line
per step, each line the whole entry so far under the same ``id``, so the last
line of an id is the complete attempt, a crash never loses what happened, and
a running switch's current step can be read straight out of the file. Earlier
lines are never rewritten or pruned.

The candidate file in ``profiles_dir`` is never modified.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from ..lifecycle.control import worker_active

SWITCH_LOCK_NAME = "profile-switch-active"
READY_TIMEOUT_S = 600.0
ROLLBACK_TIMEOUT_S = 600.0
POLL_S = 1.0
BACKUP_DIR_NAME = "backups"
# What a finished attempt's ``outcome`` is; ``RUNNING`` is what an entry in
# flight carries until the last journal line replaces it.
RUNNING = "running"
OUTCOMES = ("applied", "refused", "rolled_back", "rollback_failed")
ATTENTION = "the engine needs attention"
_BACKUP_SUFFIX = ".yaml"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def journal_path(journal_dir, engine_id: str) -> Path:
    """``<journal_dir>/<engine>.jsonl``; a hostile engine id is refused."""
    if not _SAFE_ID_RE.fullmatch(str(engine_id)):
        raise ValueError(f"engine id {engine_id!r} cannot be a journal file name")
    return Path(journal_dir) / f"{engine_id}.jsonl"


def backups_dir(journal_dir) -> Path:
    return Path(journal_dir) / BACKUP_DIR_NAME


def load_history(journal_dir, engine_id: str) -> list[dict]:
    """The journaled attempts, oldest first, one entry per id.

    Missing or unreadable journal, and lines that are not JSON, answer with
    nothing rather than raising: an empty history is a fact about the file,
    not an error. Reading never creates the journal directory.
    """
    try:
        raw = journal_path(journal_dir, engine_id).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    latest: dict[str, dict] = {}
    for number, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        latest[str(record.get("id") or f"line-{number}")] = record
    return list(latest.values())


def switch_lock_live(switch_lock) -> bool:
    """True while another switch holds a live ``profile-switch-active`` lock.

    Same rules as the worker lock (:func:`wsd.lifecycle.control.worker_active`):
    a dead pid, an empty file or junk content all count as stale, so a switch
    killed halfway cannot lock the dashboard out of ever switching again.
    """
    return worker_active(switch_lock)


def acquire_switch_lock(switch_lock, entry_id: str) -> bool:
    """Publish the ``profile-switch-active`` lock atomically and exclusively.

    True when this caller now holds the lock, False when another live switch
    holds it (or won the exclusive publish). The payload is written to a
    uniquely named temp file in the same directory first and then hard-linked
    into place with :func:`os.link`, which fails when the lock exists: a
    visible lock file is always complete, never the empty or half-written file
    an ``O_CREAT`` create would have exposed for a moment.

    A stale lock — dead pid, empty or junk, the same rules as
    :func:`switch_lock_live` — is taken over by :func:`_take_stale_lock` and
    the publish retried exactly once; losing that second attempt also returns
    False. Any other OSError propagates, so the caller can report that the
    lock cannot be written.
    """
    path = Path(switch_lock)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{entry_id} {int(time.time())} {os.getpid()}\n".encode("utf-8")
    stale_retried = False
    while True:
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                # ``mkstemp`` already made this 0600; the lock never widens.
                os.link(temp, path)
            except FileExistsError:
                if stale_retried or switch_lock_live(path):
                    return False
                if not _take_stale_lock(path):
                    return False
                stale_retried = True
                continue
            return True
        finally:
            try:
                os.unlink(temp)
            except OSError:
                pass


def _stale_name(path: Path) -> str:
    return os.path.join(str(path.parent), f".{path.name}.stale-{secrets.token_hex(6)}")


def _rename_stale(src, dst) -> None:
    """The rename of the stale takeover, kept as a call so a test can run the
    race it is there for: another switch publishing a live lock between the
    staleness check and this rename."""
    os.rename(src, dst)


def _take_stale_lock(path: Path) -> bool:
    """Move a stale lock out of the way so the publish can be retried.

    True when the lock path is free to try again. A lock that is judged stale
    may have been replaced by a live one before our rename got there, so the
    file we actually moved aside is re-checked: if it is live we hand it back
    (link it into place when the path is free again) and answer False, because
    deleting it would let a second switch run in parallel with its owner.
    """
    unique = _stale_name(path)
    try:
        _rename_stale(path, unique)
    except FileNotFoundError:
        return True  # it vanished on its own: the retry will see a free path
    try:
        if switch_lock_live(unique):
            try:
                os.link(unique, path)
            except FileExistsError:
                pass  # someone else published while we were looking
            return False
    finally:
        try:
            os.unlink(unique)
        except OSError:
            pass
    return True


def release_switch_lock(switch_lock, entry_id: str) -> None:
    """Remove the lock, but only if it is still ours."""
    try:
        fields = Path(switch_lock).read_text(encoding="utf-8").split()
        if fields and fields[0] == entry_id:
            Path(switch_lock).unlink()
    except OSError:
        pass


def atomic_write(dest, data: bytes) -> None:
    """Write ``data`` to ``dest`` so a reader only ever sees the old or new file.

    The temp file is unique per call (``tempfile.mkstemp`` in the destination's
    directory), so two threads writing the same destination — the dashboard is
    a threading server — never share a scratch file, interleave, or delete each
    other's work. It is fsynced, given the destination's mode when the
    destination exists (rewriting an engine profile must not change its
    permissions; a new file keeps ``mkstemp``'s 0600), then moved over the
    destination with ``os.replace``. Our own temp file is removed on failure.
    """
    dest = Path(dest)
    fd, temp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(temp, dest.stat().st_mode & 0o7777)
        except FileNotFoundError:
            pass
        os.replace(temp, dest)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


class EngineHooks:
    """What the pipeline asks the engine, and where its files live.

    ``active_path`` is the file the engine reads at start-up, ``profiles_dir``
    where the candidate profiles live (read-only for us), ``journal_dir`` the
    journal and ``backups`` directory, ``switch_lock`` the
    ``profile-switch-active`` lock next to the worker lock. ``dry_run`` marks
    an engine that is only pretending, so every answer can say so.

    A method that cannot answer (engine unreachable) answers ``False`` — the
    engine is then not busy, or not healthy — and never raises: the pipeline
    must be able to refuse or roll back instead of dying.
    """

    dry_run = False

    def __init__(self, active_path, profiles_dir, journal_dir, switch_lock) -> None:
        self.active_path = Path(active_path)
        self.profiles_dir = Path(profiles_dir)
        self.journal_dir = Path(journal_dir)
        self.switch_lock = Path(switch_lock)

    def worker_busy(self) -> bool:
        """True while a supervised worker task holds its lock."""
        raise NotImplementedError

    def busy(self) -> bool:
        """True while the engine has requests in flight."""
        raise NotImplementedError

    def validate(self, text: str) -> dict:
        """``{"errors": [...], "warnings": [...], "tree": dict}`` for a profile.

        Checked against the engine's active profile and its approved parameter
        list; ``tree`` is the parsed candidate the pipeline hands to
        :meth:`ready`.
        """
        raise NotImplementedError

    def restart(self) -> str | None:
        """Restart the engine; ``None`` when accepted, else the reason not."""
        raise NotImplementedError

    def healthy(self) -> bool:
        """True when the engine answers its health endpoint."""
        raise NotImplementedError

    def ready(self, timeout: float, candidate: dict) -> bool:
        """True once the engine serves the candidate profile, polling to ``timeout``."""
        raise NotImplementedError


class _Journal:
    """Append-only JSONL journal for one engine."""

    def __init__(self, journal_dir, engine_id: str) -> None:
        self.path = journal_path(journal_dir, engine_id)

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


class _Switch:
    """One switch attempt: the entry it builds, the steps it appends."""

    def __init__(
        self, hooks, engine_id, candidate_path, candidate_name, action, timeout, rollback_timeout, restore_of
    ):
        self.hooks = hooks
        self.candidate_path = Path(candidate_path)
        self.candidate_name = candidate_name
        self.timeout = timeout
        self.rollback_timeout = rollback_timeout
        self.journal = _Journal(hooks.journal_dir, engine_id)
        self.candidate_bytes = b""
        self.candidate_tree: dict = {}
        self.active_bytes = b""
        self.backup: Path | None = None
        self.written = False
        self.entry = {
            "id": _entry_id(),
            "engine": engine_id,
            "action": action,
            "restore_of": restore_of,
            "started": _stamp(),
            "finished": None,
            "from": None,
            "budgets": {"ready_s": timeout, "rollback_s": rollback_timeout},
            "to": {"name": candidate_name, "sha256": None},
            "backup": None,
            "outcome": RUNNING,
            "errors": [],
            "steps": [],
            "dry_run": hooks.dry_run,
        }

    # ---------------------------------------------------------------- journal
    def step(self, name: str, ok: bool, detail=None, error: str | None = None) -> None:
        record = {"step": name, "at": _stamp(), "ok": ok}
        if detail is not None:
            record["detail"] = detail
        if error is not None:
            record["error"] = error
        self.entry["steps"].append(record)
        self.journal.append(self.entry)

    def refuse(self, name: str, error: str) -> bool:
        """Refuse the switch: nothing has been touched, nothing to undo."""
        self.entry["errors"].append(error)
        self._settle("refused")
        self.step(name, False, error=error)
        return False

    def finish(self, outcome: str, errors=()) -> None:
        self.entry["errors"].extend(errors)
        self._settle(outcome)
        self.journal.append(self.entry)

    def _settle(self, outcome: str) -> None:
        self.entry["outcome"] = outcome
        self.entry["finished"] = _stamp()

    # ------------------------------------------------------------------- run
    def run(self) -> dict:
        if not self._preconditions():
            return self.entry
        try:
            self._attempt()
        finally:
            release_switch_lock(self.hooks.switch_lock, self.entry["id"])
        return self.entry

    def _preconditions(self) -> bool:
        if self.hooks.worker_busy():
            return self.refuse("preconditions", "a supervised worker task is running: nothing was touched")
        if self.hooks.busy():
            return self.refuse("preconditions", "the engine has requests in flight: nothing was touched")
        try:
            if not acquire_switch_lock(self.hooks.switch_lock, self.entry["id"]):
                return self.refuse(
                    "preconditions", f"another profile switch is running ({self.hooks.switch_lock}): nothing was touched"
                )
        except OSError as exc:
            return self.refuse(
                "preconditions", f"the profile switch lock {self.hooks.switch_lock} cannot be written: {exc}"
            )
        self.step("preconditions", True)
        return True

    def _attempt(self) -> None:
        if not (self._validate() and self._backup() and self._write()):
            return
        failure = self._restart()
        if failure is None:
            failure = self._wait_ready(self.candidate_tree, self.timeout)
        if failure is None:
            self.finish("applied")
            return
        self._roll_back(failure)

    # ------------------------------------------------------------------- steps
    def _validate(self) -> bool:
        try:
            self.candidate_bytes = self.candidate_path.read_bytes()
        except OSError as exc:
            return self.refuse("validate", f"candidate profile {self.candidate_name!r} cannot be read: {exc}")
        self.entry["to"]["sha256"] = sha256_bytes(self.candidate_bytes)
        try:
            self.active_bytes = self.hooks.active_path.read_bytes()
        except OSError as exc:
            return self.refuse(
                "validate", f"active profile {self.hooks.active_path.name!r} cannot be read: {exc}"
            )
        self.entry["from"] = {
            "name": self.hooks.active_path.name,
            "sha256": sha256_bytes(self.active_bytes),
        }
        try:
            report = self.hooks.validate(_text(self.candidate_bytes))
        except Exception as exc:  # noqa: BLE001 - a broken schema must refuse, not crash
            return self.refuse("validate", f"the candidate could not be checked: {exc}")
        errors = list(report.get("errors") or [])
        if errors:
            return self.refuse("validate", "; ".join(errors))
        self.candidate_tree = report.get("tree") or {}
        self.step("validate", True, detail={"warnings": list(report.get("warnings") or [])})
        return True

    def _backup(self) -> bool:
        directory = backups_dir(self.hooks.journal_dir)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"{stamp}-{self.entry['from']['sha256'][:12]}{_BACKUP_SUFFIX}"
        self.backup = directory / name
        try:
            directory.mkdir(parents=True, exist_ok=True)
            atomic_write(self.backup, self.active_bytes)
        except OSError as exc:
            return self.refuse("backup", f"the active profile could not be backed up to {directory}: {exc}")
        self.entry["backup"] = name
        self.step("backup", True, detail=name)
        return True

    def _write(self) -> bool:
        try:
            atomic_write(self.hooks.active_path, self.candidate_bytes)
        except OSError as exc:
            return self.refuse("write", f"the active profile could not be written: {exc}")
        self.written = True
        self.step("write", True, detail=self.hooks.active_path.name)
        return True

    def _restart(self) -> str | None:
        """Restart the engine; the reason it did not happen, or None."""
        try:
            error = self.hooks.restart()
        except Exception as exc:  # noqa: BLE001 - a raising hook is a failed restart
            error = f"{type(exc).__name__}: {exc}"
        self.step("restart", error is None, error=None if error is None else str(error))
        return None if error is None else f"the engine restart failed: {error}"

    def _wait_ready(self, tree: dict, timeout: float) -> str | None:
        error = wait_ready(self.hooks, tree, timeout)
        self.step("ready", error is None, error=error)
        return error

    # --------------------------------------------------------------- rollback
    def _roll_back(self, failure: str) -> None:
        """Put the backup back, restart and wait again; loudly on a second failure."""
        self.entry["errors"].append(failure)
        self.step("rollback", False, error=failure)
        original = self.entry["from"]["name"]
        try:
            atomic_write(self.hooks.active_path, self.active_bytes)
        except OSError as exc:
            self.step("restore", False, error=str(exc))
            self.finish(
                "rollback_failed",
                [
                    f"rollback failed: {original} could not be written back ({exc}), and the engine was "
                    f"restarted on the profile that failed to start — {ATTENTION}"
                ],
            )
            return
        self.step("restore", True, detail=self.entry["backup"])
        error = self._restart()
        if error is None:
            error = self._wait_ready(self._active_tree(), self.rollback_timeout)
        if error is None:
            self.finish("rolled_back")
            return
        self.finish(
            "rollback_failed",
            [f"rollback failed: {original} was written back but the engine did not come back — {ATTENTION}: {error}"],
        )

    def _active_tree(self) -> dict:
        """The parsed profile that is active again, for the rollback's readiness check."""
        try:
            return (self.hooks.validate(_text(self.active_bytes)) or {}).get("tree") or {}
        except Exception:  # noqa: BLE001 - no expectations is better than a crash here
            return {}


def wait_ready(hooks: EngineHooks, candidate: dict, timeout: float) -> str | None:
    """``None`` once the engine is healthy AND serving ``candidate``.

    Healthy and ready share the one budget; the reason names which of the two
    never happened.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            healthy = bool(hooks.healthy())
        except Exception:  # noqa: BLE001 - an engine that cannot answer is not healthy
            healthy = False
        if healthy:
            break
        left = deadline - time.monotonic()
        if left <= 0:
            return f"the engine is not healthy within {timeout:g} s"
        time.sleep(min(POLL_S, left))
    left = max(0.0, deadline - time.monotonic())
    try:
        ready = bool(hooks.ready(left, candidate))
    except Exception as exc:  # noqa: BLE001 - a raising hook is an engine that is not ready
        return f"the engine is healthy but its readiness check failed: {type(exc).__name__}: {exc}"
    if ready:
        return None
    return f"the engine is healthy but not serving the new profile within {timeout:g} s"


def apply_profile(
    hooks: EngineHooks,
    engine_id: str,
    candidate_path,
    candidate_name: str,
    *,
    action: str = "apply",
    ready_timeout: float = READY_TIMEOUT_S,
    rollback_timeout: float = ROLLBACK_TIMEOUT_S,
    restore_of: str | None = None,
) -> dict:
    """Switch ``engine_id`` to ``candidate_path``, journalled; the entry is returned.

    ``candidate_name`` is what the journal calls the file (its path may be in
    the journal's own backup directory for a restore), ``action`` is
    ``"apply"`` or ``"restore"``, and ``restore_of`` the entry a restore came
    from. ``ready_timeout`` bounds the wait for the candidate,
    ``rollback_timeout`` the wait for the profile put back by a rollback;
    ``hooks.validate`` must reject anything the engine may not run with before
    a single byte is written.
    """
    return _Switch(
        hooks, engine_id, candidate_path, candidate_name, action,
        ready_timeout, rollback_timeout, restore_of,
    ).run()

def switch_state(journal_dir, engine_id: str, switch_lock) -> dict:
    """What ``GET .../switch`` answers: the running switch's current step.

    The step comes from the journal (the last line appended), the liveness
    from the switch lock, so a dashboard that was restarted mid-switch says
    so instead of reporting a switch that died as if it were still running.
    """
    history = load_history(journal_dir, engine_id)
    entry = history[-1] if history else None
    live = switch_lock_live(switch_lock)
    running = bool(live and entry is not None and entry.get("outcome") == RUNNING)
    steps = entry.get("steps") or [] if entry else []
    return {
        "engine": engine_id,
        "running": running,
        "step": steps[-1]["step"] if steps else None,
        "lock": live,
        "entry": entry,
    }


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _entry_id() -> str:
    return f"sw-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def _text(data: bytes) -> str:
    return data.decode("utf-8", "replace")

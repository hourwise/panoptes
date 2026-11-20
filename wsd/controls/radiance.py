"""Radiance: strict parser for its profile YAML subset + the approved schema.

Radiance profiles are simple two-level YAML (the container reads one at
start-up). That simplicity is exploited: ``parse_profile`` accepts *only* the
subset a Radiance profile uses — nested mappings with 2-space indentation,
``key: value`` scalars (integers, ``true``/``false``, plain or singly quoted
strings), blank lines and whole-line ``#`` comments — and rejects everything
else (lists, anchors/aliases, tabs, flow style, multi-line values, duplicate
keys, indentation that is not a multiple of two) with the offending line
number. A file that parses is then checked against the APPROVED PARAMETER
LIST below: server and model identity are ``fixed`` (this path can neither
rebind the server nor swap the model), and every tunable has a type, a range
or an enum.

Nothing here writes a file, shells out or talks to a container by itself. The
engine side of the apply pipeline, :class:`RadianceHooks`, is: it reads and
writes the profile through :mod:`wsd.controls.apply`, asks the running server
its read-only questions (``/stats``, ``/health``, ``/v1/models``, one
completion) and restarts the container through the existing radiance-ctl
client — the only sanctioned control path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..lifecycle.control import DRYRUN_ENV, WORKER_LOCK, ControlClient
from .apply import POLL_S, SWITCH_LOCK_NAME, EngineHooks
from .base import Control, Cross, Schema, validate as check_tree

# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


class ProfileParseError(Exception):
    """The text is not the YAML subset Radiance profiles use. Message names
    the offending line: ``line N: ...``."""


_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_SPECIAL_START = "*&|>[]{}"


def parse_profile(text: str) -> dict:
    """Parse the strict YAML subset into a plain dict tree.

    Only mappings and scalars exist: a line is ``key:`` (opens a nested
    mapping) or ``key: value`` (a leaf). Indentation must be a multiple of
    two spaces and nest one level (+2) at a time.
    """
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-2, root)]
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        body = line.lstrip(" \t")
        if not body:
            continue
        indent = len(line) - len(body)
        if "\t" in line[:indent]:
            raise ProfileParseError(f"line {number}: indentation must use spaces, not tabs")
        if body.startswith("#"):
            continue
        if indent % 2:
            raise ProfileParseError(f"line {number}: indentation must be a multiple of 2 spaces")
        while stack[-1][0] >= indent:
            stack.pop()
        if indent != stack[-1][0] + 2:
            raise ProfileParseError(
                f"line {number}: unexpected indentation {indent}, expected {stack[-1][0] + 2}"
            )
        parent = stack[-1][1]
        if body.startswith("-"):
            raise ProfileParseError(f"line {number}: lists are not part of the profile format")
        if body[0] in _SPECIAL_START:
            raise ProfileParseError(
                f"line {number}: {body[0]!r} starts anchors, aliases, flow style or block "
                f"scalars, none of which profiles use"
            )
        key, colon, rest = body.partition(":")
        if not colon:
            raise ProfileParseError(f"line {number}: expected 'key: value'")
        if not _KEY_RE.match(key):
            raise ProfileParseError(f"line {number}: invalid key {key!r}")
        if key in parent:
            raise ProfileParseError(f"line {number}: duplicate key {key!r}")
        value = rest.strip()
        if not value:
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
            continue
        parent[key] = _scalar(number, key, value)
    return root


def _scalar(number: int, key: str, value: str) -> object:
    if value[0] in "\"'":
        if len(value) < 2 or not value.endswith(value[0]):
            raise ProfileParseError(f"line {number}: quoted value of {key!r} never closes")
        inner = value[1:-1]
        if value[0] in inner:
            raise ProfileParseError(f"line {number}: quote inside the quoted value of {key!r}")
        return inner
    if "#" in value:
        raise ProfileParseError(f"line {number}: comments must be on a line of their own")
    if value[0] in _SPECIAL_START or value[0] in "!%@`":
        raise ProfileParseError(f"line {number}: value of {key!r} uses unsupported YAML syntax")
    if _INT_RE.match(value):
        return int(value)
    if value in ("true", "false"):
        return value == "true"
    return value


# --------------------------------------------------------------------------
# approved parameter list
# --------------------------------------------------------------------------

# Every Radiance profile leaf, as the dashboard may explain and police it.
# Radiance reads the file only at start-up, so restart_required is true for
# all of them. quality_sensitive marks the leaves that change what the model
# can answer with, not just how fast.

_MEMORY_CONTROLS = (
    Control(
        "memory.gpu-headroom-mib", "int", min=512, max=8192, unit="MiB",
        description="GPU memory left unused for other users of the card.",
    ),
    Control(
        "memory.host-headroom-mib", "int", min=4096, max=32768, unit="MiB",
        description="Host RAM left unused for the rest of the system.",
    ),
)

_MODEL_SUFFIXES = (
    Control("model", "fixed", description="Model file — fixed by the active profile."),
    Control("tp", "fixed", description="Tensor parallel width — fixed by the active profile."),
    Control("placement", "enum", values=("expert_tiered",), description="Expert placement strategy."),
    Control(
        "host-pool-mib", "int", min=16384, max=98304, unit="MiB",
        description="Host RAM the tiered expert pool may use.",
    ),
    Control(
        "ngram-placement", "enum", values=("disk", "ram"),
        description="Where the speculative n-gram store lives.",
    ),
    Control(
        "weights-disk-tier", "bool",
        description="Allow the expert pool to tier further down to disk.",
    ),
    Control(
        "max-model-len", "int", min=8192, max=262144, unit="tokens", warn_multiple=1024,
        quality_sensitive=True, description="Context capacity per request.",
    ),
    Control("max-num-seqs", "int", min=1, max=4, description="Requests processed concurrently."),
    Control(
        "max-num-batched-tokens", "int", min=512, max=8192, unit="tokens",
        description="Tokens packed into one batch.",
    ),
    Control(
        "kv-cache-dtype", "enum", values=("bf16",), quality_sensitive=True,
        description="KV cache precision.",
    ),
    Control(
        "prefix-cache-mib", "int", min=0, max=16384, unit="MiB",
        description="Prefix cache size on top of the KV cache.",
    ),
    Control(
        "num-speculative-tokens", "int", min=0, max=8, unit="tokens",
        description="Draft tokens speculated per step (0 disables speculation).",
    ),
    Control("draft-window", "enum", values=("adaptive",), description="Speculation window policy."),
    Control(
        "mm-max-patches", "int", min=0, max=4096, unit="patches",
        description="Multimodal vision budget (0 disables image input).",
    ),
    Control(
        "reasoning-effort", "enum", values=("low", "medium", "high"), quality_sensitive=True,
        description="Default reasoning effort.",
    ),
)

_SERVER_KEYS = ("host", "port", "live", "log")

# Host RAM the tiered pool plus the headroom may claim together (MiB).
HOST_MEMORY_LIMIT_MIB = 110592


def model_key(active: dict) -> str:
    """The model key of the active profile, or ``"<name>"`` when it cannot be
    told (no models section, or more than one model)."""
    models = active.get("models") if isinstance(active, dict) else None
    if isinstance(models, dict) and len(models) == 1:
        return next(iter(models))
    return "<name>"


def build_schema(active: dict | None) -> Schema:
    """The approved Radiance schema for one active profile.

    The model-dependent paths are spelled out with the active profile's model
    key, so a candidate that renames the key is an unknown key, not a match.
    """
    key = model_key(active or {})
    model = f"models.{key}"
    controls = [
        *[
            Control(f"server.{name}", "fixed", description="Server identity — fixed by the active profile.")
            for name in _SERVER_KEYS
        ],
        *_MEMORY_CONTROLS,
        *[Control(f"{model}.{control.name}", control.type, min=control.min, max=control.max,
                  values=control.values, unit=control.unit,
                  quality_sensitive=control.quality_sensitive, description=control.description,
                  warn_multiple=control.warn_multiple)
          for control in _MODEL_SUFFIXES],
    ]
    cross = (
        Cross(
            paths=(f"{model}.host-pool-mib", "memory.host-headroom-mib"),
            max_sum=HOST_MEMORY_LIMIT_MIB,
            description="expert pool plus host headroom must fit the machine",
        ),
    )
    return Schema(controls=tuple(controls), cross=cross)


# --------------------------------------------------------------------------
# the engine side of the apply pipeline
# --------------------------------------------------------------------------

GET_TIMEOUT_S = 2.0
# The probe is a real generation: the first request after a restart pays the
# model-load cost, so it gets a generous timeout of its own.
PROBE_TIMEOUT_S = 60.0
PROBE_PROMPT = "ok"


def served_expectation(candidate: dict) -> tuple[str | None, int | None]:
    """What ``/v1/models`` must show once ``candidate`` is loaded: the model id
    (its key under ``models``) and the ``max-model-len`` it asks for."""
    models = candidate.get("models") if isinstance(candidate, dict) else None
    key = model_key(candidate)
    model = models.get(key) if isinstance(models, dict) else None
    if not isinstance(model, dict):
        return None, None
    max_len = model.get("max-model-len")
    expected = max_len if isinstance(max_len, int) and not isinstance(max_len, bool) else None
    return key, expected


class RadianceHooks(EngineHooks):
    """The apply pipeline's Radiance side.

    Busy and ready come from the server itself, the restart from the existing
    radiance-ctl client, and the approved-parameter check from the schema built
    off the active profile. ``fetch(method, path, payload) -> str`` can be
    swapped for a fake in tests; nothing here ever talks to a container that is
    not the configured one, and nothing here shells out except through
    :class:`~wsd.lifecycle.control.ControlClient`.
    """

    def __init__(self, base_url, active_path, profiles_dir, journal_dir, switch_lock, *, control=None, fetch=None):
        super().__init__(active_path, profiles_dir, journal_dir, switch_lock)
        self.base_url = str(base_url).rstrip("/")
        self._control = control if control is not None else ControlClient()
        self._fetch = fetch if fetch is not None else self._urlopen

    # ---------------------------------------------------------------- plumbing
    def _urlopen(self, method: str, path: str, payload: dict | None = None) -> str:
        timeout = PROBE_TIMEOUT_S if path == "/v1/completions" else GET_TIMEOUT_S
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")

    def _json(self, method: str, path: str, payload: dict | None = None) -> dict | None:
        """Parsed JSON from the server; ``None`` when it is down or talks junk."""
        try:
            parsed = json.loads(self._fetch(method, path, payload))
        except Exception:  # noqa: BLE001 - down, slow or junk all mean "no answer"
            return None
        return parsed if isinstance(parsed, dict) else None

    # ----------------------------------------------------------------- questions
    def worker_busy(self) -> bool:
        return self._control.worker_active()

    def busy(self) -> bool:
        """``/stats`` says requests are running or waiting. An engine that does
        not answer is not busy: restarting it is how the switch finds out."""
        stats = self._json("GET", "/stats")
        if stats is None:
            return False
        return any(isinstance(stats.get(key), (int, float)) and stats[key] > 0 for key in ("running", "waiting"))

    def healthy(self) -> bool:
        health = self._json("GET", "/health")
        return bool(health) and health.get("status") == "ok"

    def restart(self) -> str | None:
        result = self._control.restart()
        if isinstance(result, dict) and result.get("ok"):
            return None
        detail = result.get("error") if isinstance(result, dict) else None
        return str(detail) if detail else f"the control helper returned {result!r}"

    def ready(self, timeout: float, candidate: dict) -> bool:
        """The model is listed with the length the profile asked for AND one
        completion comes back; polling until ``timeout`` runs out."""
        model, max_len = served_expectation(candidate)
        if model is None:
            return False  # a profile we cannot name a model for cannot be probed
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self._serves(model, max_len) and self._completes(model):
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(POLL_S, left))

    def _serves(self, model: str, max_len: int | None) -> bool:
        listing = self._json("GET", "/v1/models")
        entries = listing.get("data") if isinstance(listing, dict) else None
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if isinstance(entry, dict) and entry.get("id") == model:
                return max_len is None or entry.get("max_model_len") == max_len
        return False

    def _completes(self, model: str) -> bool:
        answer = self._json(
            "POST",
            "/v1/completions",
            {"model": model, "prompt": PROBE_PROMPT, "max_tokens": 1, "temperature": 0},
        )
        return bool(answer and answer.get("choices"))

    def validate(self, text: str) -> dict:
        """Approved-parameter verdict for ``text`` against the active profile;
        a profile that does not even parse is one error naming its line."""
        try:
            candidate = parse_profile(text)
        except ProfileParseError as exc:
            return {"errors": [str(exc)], "warnings": [], "tree": None}
        try:
            active = parse_profile(self.active_path.read_text(encoding="utf-8"))
        except OSError as exc:
            return {
                "errors": [f"the active profile {self.active_path} cannot be read: {exc}"],
                "warnings": [],
                "tree": None,
            }
        except ProfileParseError as exc:
            return {
                "errors": [f"the active profile is not a Radiance profile: {exc}"],
                "warnings": [],
                "tree": None,
            }
        report = check_tree(build_schema(active), active, candidate)
        return {"errors": list(report["errors"]), "warnings": list(report["warnings"]), "tree": candidate}


class DryRunRadianceHooks(RadianceHooks):
    """Dry run: the same pipeline over a temporary copy of the profile
    directory, with an engine that is always idle, healthy and ready.

    Nothing real is read (except the copy), written or restarted — the restart
    goes to the control client, which answers dry run from its canned result
    without calling sudo. Every answer carries ``dry_run: true``.
    """

    dry_run = True

    def busy(self) -> bool:
        return False

    def healthy(self) -> bool:
        return True

    def ready(self, timeout: float, candidate: dict) -> bool:
        return served_expectation(candidate)[0] is not None


# One sandbox per engine id for the life of the process: dry runs must not
# accumulate a directory each, and the second switch should see the first one's
# profile as active, the way the real one would.
_DRYRUN_SANDBOXES: dict[str, dict] = {}


def dry_run_sandbox(engine_id: str, profiles_dir) -> dict:
    """``{"profiles_dir", "journal_dir", "switch_lock"}`` inside a temporary
    copy of ``profiles_dir``; built once per engine id."""
    sandbox = _DRYRUN_SANDBOXES.get(engine_id)
    if sandbox is None:
        root = Path(tempfile.mkdtemp(prefix=f"wsd-dryrun-{engine_id}-"))
        shutil.copytree(str(profiles_dir), str(root / "profiles"), dirs_exist_ok=True)
        sandbox = {
            "profiles_dir": root / "profiles",
            "journal_dir": root / "journal",
            "switch_lock": root / SWITCH_LOCK_NAME,
        }
        _DRYRUN_SANDBOXES[engine_id] = sandbox
    return sandbox


def radiance_hooks(
    engine_id: str,
    base_url: str,
    profiles_dir,
    active_profile: str,
    journal_dir,
    *,
    control: ControlClient | None = None,
    fetch=None,
) -> EngineHooks:
    """Hooks for one Radiance engine — real, or under ``PANOPTES_CONTROL_DRYRUN=1``
    pointed entirely at a sandbox (profile copy, journal and switch lock included,
    so a dry run cannot pollute the real journal).
    """
    dry_run = os.environ.get(DRYRUN_ENV, "") == "1"
    if dry_run:
        sandbox = dry_run_sandbox(engine_id, profiles_dir)
        profiles, journal, switch_lock = (
            sandbox["profiles_dir"],
            sandbox["journal_dir"],
            sandbox["switch_lock"],
        )
    else:
        profiles, journal, switch_lock = Path(profiles_dir), Path(journal_dir), WORKER_LOCK.parent / SWITCH_LOCK_NAME
    hooks = DryRunRadianceHooks if dry_run else RadianceHooks
    return hooks(base_url, profiles / active_profile, profiles, journal, switch_lock, control=control, fetch=fetch)

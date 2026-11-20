"""Engine control schemas: declare what an engine's profiles may change.

An engine adapter with profile files gets a *control schema* (every parameter
the dashboard knows how to change, with its type, range, unit, restart and
quality-sensitivity flags) plus two pure functions on parsed profiles:

* ``validate(schema, active, candidate)`` — errors and warnings for a
  candidate profile against the active one;
* ``preview(schema, active, candidate)`` — the leaf-level changes the
  candidate would make.

``validate`` and ``preview`` are read-only; ``base`` is engine-agnostic and the
only engine-specific schema today is ``radiance`` (its strict YAML-subset
parser and its approved parameter list).

``apply`` is the writing half: the engine-agnostic profile-switch pipeline —
journal, backup, atomic write, restart, automatic rollback — driven by a small
``EngineHooks`` interface, whose Radiance implementation (``radiance_hooks``)
is what talks to a running engine.
"""

from .apply import (
    EngineHooks,
    apply_profile,
    atomic_write,
    journal_path,
    load_history,
    switch_state,
)
from .base import Control, Cross, Schema, flatten, preview, validate
from .radiance import ProfileParseError, build_schema, parse_profile, radiance_hooks

__all__ = [
    "Control",
    "Cross",
    "Schema",
    "EngineHooks",
    "ProfileParseError",
    "apply_profile",
    "atomic_write",
    "build_schema",
    "flatten",
    "journal_path",
    "load_history",
    "parse_profile",
    "preview",
    "radiance_hooks",
    "switch_state",
    "validate",
]

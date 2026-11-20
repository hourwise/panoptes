"""The engine-agnostic control contract: controls, validation, preview.

A *control* is one leaf of a profile file the dashboard knows how to change.
A *schema* is the approved list of controls (plus cross-parameter checks) an
engine accepts — anything outside it is rejected, so a profile file can never
rebind the server or swap the model through this path.

``validate`` and ``preview`` are pure functions over already-parsed profiles
(plain ``dict`` trees of mappings and scalars, the shape ``radiance.parse_profile``
produces). They never touch the file system. Every message names the dotted
path of the leaf it is about.

A control of type ``fixed`` must equal the *active* profile's value at that
path — it is declared so the diff and the unknown-key check stay honest, not
because the dashboard can change it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Control:
    """One approved profile leaf.

    ``type`` is ``"int"`` (``min``/``max`` inclusive), ``"bool"``, ``"enum"``
    (``values``) or ``"fixed"`` (must equal the active profile).
    ``warn_multiple`` adds a warning (not an error) when an int value is not a
    multiple of it.
    """

    name: str
    type: str
    min: int | None = None
    max: int | None = None
    values: tuple[str, ...] | None = None
    unit: str | None = None
    restart_required: bool = True
    quality_sensitive: bool = False
    description: str = ""
    warn_multiple: int | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "min": self.min,
            "max": self.max,
            "values": list(self.values) if self.values is not None else None,
            "unit": self.unit,
            "restart_required": self.restart_required,
            "quality_sensitive": self.quality_sensitive,
            "description": self.description,
        }


@dataclass(frozen=True)
class Cross:
    """A sum over several control paths that must stay at or below ``max_sum``."""

    paths: tuple[str, ...]
    max_sum: int
    description: str = ""


@dataclass(frozen=True)
class Schema:
    controls: tuple[Control, ...]
    cross: tuple[Cross, ...] = field(default_factory=tuple)


def flatten(tree: dict, prefix: str = "") -> dict[str, object]:
    """Leaf paths of a parsed profile as ``{dotted path: scalar}`` (document order)."""
    leaves: dict[str, object] = {}
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            leaves.update(flatten(value, path))
        else:
            leaves[path] = value
    return leaves


def _declared_children(schema: Schema, parent: str) -> list[str]:
    """Direct child key names the schema declares under ``parent`` ("" = root)."""
    head = f"{parent}." if parent else ""
    kids = {name[len(head) :].split(".", 1)[0] for name in _names(schema) if name.startswith(head)}
    return sorted(kids)


def _names(schema: Schema) -> frozenset[str]:
    return frozenset(control.name for control in schema.controls)


def _by_name(schema: Schema) -> dict[str, Control]:
    return {control.name: control for control in schema.controls}


def _shortest_unknown(schema: Schema, path: str) -> str:
    """Shortest dotted prefix of ``path`` under which the schema declares nothing."""
    parts = path.split(".")
    for cut in range(1, len(parts) + 1):
        prefix = ".".join(parts[:cut])
        head = prefix + "."
        if not any(name == prefix or name.startswith(head) for name in _names(schema)):
            return prefix
    return path


def _unknown_message(schema: Schema, prefix: str) -> str:
    parent, _, last = prefix.rpartition(".")
    kids = _declared_children(schema, parent)
    if len(kids) == 1:
        return f"{prefix}: unknown key {last!r}, the schema declares {kids[0]!r} here"
    return f"{prefix}: not a declared control"


def _check_value(control: Control, value: object, active_leaves: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    name = control.name
    if control.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{name}: expected an integer, got {value!r}")
        else:
            if control.min is not None and value < control.min or control.max is not None and value > control.max:
                unit = f" {control.unit}" if control.unit else ""
                errors.append(f"{name}: {value} is outside the allowed range {control.min}..{control.max}{unit}")
            if control.warn_multiple and value % control.warn_multiple:
                warnings.append(
                    f"{name}: {value} is not a multiple of {control.warn_multiple} "
                    f"and may waste memory or fail to load"
                )
    elif control.type == "bool":
        if not isinstance(value, bool):
            errors.append(f"{name}: expected true or false, got {value!r}")
    elif control.type == "enum":
        if value not in control.values:
            allowed = ", ".join(repr(v) for v in control.values)
            errors.append(f"{name}: {value!r} is not one of {allowed}")
    elif control.type == "fixed":
        if name not in active_leaves:
            errors.append(f"{name}: fixed control has no value in the active profile")
        elif value != active_leaves[name]:
            errors.append(
                f"{name}: {value!r} differs from the active profile's fixed value {active_leaves[name]!r}"
            )
    return errors, warnings


def validate(schema: Schema, active: dict, candidate: dict) -> dict:
    """Judge ``candidate`` against ``schema`` and the ``active`` profile.

    Returns ``{"valid": bool, "errors": [...], "warnings": [...]}``; every
    message names a dotted path. Rules: every candidate leaf must be a
    declared control, types/ranges/enums must hold, ``fixed`` leaves must
    equal the active profile, leaves the active profile sets may not be
    dropped, and the schema's cross checks must hold.
    """
    errors: list[str] = []
    warnings: list[str] = []
    controls = _by_name(schema)
    active_leaves = flatten(active)
    candidate_leaves = flatten(candidate)

    seen_unknown: set[str] = set()
    for path in candidate_leaves:
        if path in controls:
            continue
        prefix = _shortest_unknown(schema, path)
        if prefix not in seen_unknown:
            seen_unknown.add(prefix)
            errors.append(_unknown_message(schema, prefix))

    for control in schema.controls:
        name = control.name
        if name in active_leaves and name not in candidate_leaves:
            errors.append(f"{name}: missing, the active profile sets it to {active_leaves[name]!r}")
            continue
        if name not in candidate_leaves:
            continue
        leaf_errors, leaf_warnings = _check_value(control, candidate_leaves[name], active_leaves)
        errors.extend(leaf_errors)
        warnings.extend(leaf_warnings)

    for rule in schema.cross:
        values = [candidate_leaves.get(path) for path in rule.paths]
        if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
            continue  # a missing or non-integer leaf is already an error above
        total = sum(values)
        if total > rule.max_sum:
            joined = " + ".join(rule.paths)
            errors.append(f"{joined} = {total} exceeds the limit {rule.max_sum}")

    return {"valid": not errors, "errors": errors, "warnings": warnings}


def preview(schema: Schema, active: dict, candidate: dict) -> list[dict]:
    """Every declared leaf the candidate changes, as
    ``{path, from, to, unit, restart_required, quality_sensitive}``.

    Leaves missing on one side show as ``None``; unchanged leaves are not
    listed. ``preview`` does not judge validity — ``validate`` does.
    """
    active_leaves = flatten(active)
    candidate_leaves = flatten(candidate)
    changes: list[dict] = []
    for control in schema.controls:
        name = control.name
        was = active_leaves.get(name)
        now = candidate_leaves.get(name)
        if was == now and (name in active_leaves) == (name in candidate_leaves):
            continue
        changes.append(
            {
                "path": name,
                "from": was,
                "to": now,
                "unit": control.unit,
                "restart_required": control.restart_required,
                "quality_sensitive": control.quality_sensitive,
            }
        )
    return changes

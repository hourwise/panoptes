"""Harness adapters exposed under /api/harnesses.

Built from :func:`wsd.config.load_config`: one adapter per configured
harness, keyed by its config id in load order. Unknown types are skipped
with a warning (config validation normally catches them first);
``WARNINGS`` collects everything skipped so the supervisor log shows a
broken config.
"""

from collections import OrderedDict

from ..config import load_config
from .base import HarnessAdapter
from .omp import OmpAdapter

TYPES = {"omp": OmpAdapter}


def build_harnesses(config=None):
    """(ordered id -> adapter, warnings) from a validated or raw config dict."""
    cfg = config if config is not None else load_config()
    warnings = list(cfg.get("warnings") or [])
    harnesses = OrderedDict()
    for spec in cfg.get("harnesses") or []:
        cls = TYPES.get(spec.get("type"))
        if cls is None:
            warnings.append(f"harness {spec.get('id')!r}: unknown type {spec.get('type')!r}, skipped")
            continue
        harness_id = spec["id"]
        if harness_id in harnesses:
            warnings.append(f"harness {harness_id!r}: duplicate id, skipped")
            continue
        harnesses[harness_id] = cls(
            id=harness_id,
            label=spec.get("label") or harness_id,
            sessions_dir=spec.get("sessions_dir"),
            lock_file=spec.get("lock_file"),
        )
    return harnesses, warnings


HARNESSES, WARNINGS = build_harnesses()

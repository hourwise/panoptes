"""Inference engine adapters exposed under /api/inference.

Built from :func:`wsd.config.load_config`: one adapter per configured engine,
keyed by its config id in load order. Unknown types are skipped with a
warning (config validation normally catches them first); ``WARNINGS`` collects
everything skipped so the supervisor log shows a broken config.
"""

from collections import OrderedDict

from ..config import load_config
from .base import EngineAdapter
from .llamacpp import LlamaCppAdapter
from .openai_compat import OpenAICompatAdapter
from .radiance import RadianceAdapter

TYPES = {"radiance": RadianceAdapter, "openai-compatible": OpenAICompatAdapter, "llama.cpp": LlamaCppAdapter}


def build_engines(config=None):
    """(ordered id -> adapter, warnings) from a validated or raw config dict."""
    cfg = config if config is not None else load_config()
    warnings = list(cfg.get("warnings") or [])
    engines = OrderedDict()
    for spec in cfg.get("engines") or []:
        cls = TYPES.get(spec.get("type"))
        if cls is None:
            warnings.append(f"engine {spec.get('id')!r}: unknown type {spec.get('type')!r}, skipped")
            continue
        engine_id = spec["id"]
        if engine_id in engines:
            warnings.append(f"engine {engine_id!r}: duplicate id, skipped")
            continue
        engines[engine_id] = cls(
            engine_id=engine_id, label=spec.get("label") or engine_id, base_url=spec["base_url"]
        )
    return engines, warnings


ENGINES, WARNINGS = build_engines()

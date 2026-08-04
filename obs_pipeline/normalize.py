"""Rule category 2 -- canonicalization (design doc §2.2).

Get this wrong and you get silent false conflicts or false corroboration in
§2.3, with no extraction rule looking broken.

Vendor and device_type normalization returns the alias target when one
matches and the cleaned surface string otherwise. It never returns the escape
value: closed-vocabulary rejection happens at field resolution (§6.3), where
it is visible in the trace and countable in metrics.
"""
from __future__ import annotations

import re

from obs_pipeline.trace import Traced, Tracer

_WS = re.compile(r"\s+")


def _surface_key(value: str) -> str:
    return _WS.sub(" ", value.strip()).lower()


def _emit(tracer, rule_id, before, after, parent):
    return tracer.step(
        op="normalize",
        rule_id=rule_id,
        output=after,
        before=before,
        parents=[parent] if parent is not None else [],
    )


def normalize_mac(raw, rules, tracer: Tracer, parent: Traced | None = None) -> Traced[str]:
    cfg = rules.normalization["mac"]
    out = raw or ""
    for delim in cfg["strip_delimiters"]:
        out = out.replace(delim, "")
    out = out.upper() if cfg["case"] == "upper" else out.lower()
    return _emit(tracer, f"normalization.yaml#{cfg['rule_id_suffix']}", raw, out, parent)


def normalize_hostname(raw, rules, tracer: Tracer, parent: Traced | None = None) -> Traced[str]:
    cfg = rules.normalization["hostname"]
    out = (raw or "").strip() if cfg.get("strip") else (raw or "")
    out = out.lower() if cfg["case"] == "lower" else out
    return _emit(tracer, f"normalization.yaml#{cfg['rule_id_suffix']}", raw, out, parent)


def _alias(block, raw, rules, tracer, parent):
    cfg = rules.normalization[block]
    key = _surface_key(raw or "")
    out = (cfg.get("map") or {}).get(key, _WS.sub(" ", (raw or "").strip()))
    step = _emit(tracer, f"normalization.yaml#{cfg['rule_id_suffix']}", raw, out, parent)

    rebrand = (rules.normalization.get("oem_rebrand", {}).get("map") or {})
    if block == "vendor_alias" and _surface_key(out) in rebrand:
        target = rebrand[_surface_key(out)]
        suffix = rules.normalization["oem_rebrand"]["rule_id_suffix"]
        return _emit(tracer, f"normalization.yaml#{suffix}", out, target, step)
    return step


def normalize_vendor(raw, rules, tracer: Tracer, parent: Traced | None = None) -> Traced[str]:
    return _alias("vendor_alias", raw, rules, tracer, parent)


def normalize_device_type(raw, rules, tracer: Tracer, parent: Traced | None = None) -> Traced[str]:
    return _alias("device_type_alias", raw, rules, tracer, parent)

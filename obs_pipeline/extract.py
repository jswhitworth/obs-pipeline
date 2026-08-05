# obs_pipeline/extract.py
"""Rule category 1 -- extraction (design doc §2.2), with normalization applied
immediately so cross-source comparison in §2.3 is valid."""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from obs_pipeline.normalize import (
    normalize_device_type, normalize_hostname, normalize_mac, normalize_vendor,
)
from obs_pipeline.trace import Traced, Tracer

_NORMALIZERS = {
    ("link_basis", "mac"): normalize_mac,
    ("link_basis", "hostname_token"): normalize_hostname,
    ("field", "vendor"): normalize_vendor,
    ("field", "device_type"): normalize_device_type,
}


@dataclass(frozen=True)
class Extraction:
    target_kind: str
    target: str
    value: str
    source: str
    witness_group: str
    rule_id: str
    traced: Traced[str]


def load_observations(path: str | Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _ports(obs) -> set[int]:
    return {int(p) for p in (obs.get("open_ports") or "").split(",") if p.strip()}


def _finish(kind, target, raw_value, source, group, rule_id, rules, tracer, parent):
    normalizer = _NORMALIZERS.get((kind, target))
    traced = normalizer(raw_value, rules, tracer, parent) if normalizer else parent
    if not traced.value:
        return None
    return Extraction(kind, target, traced.value, source, group, rule_id, traced)


def extract_observation(obs: dict, rules, tracer: Tracer) -> list[Extraction]:
    obs_id, source = obs["obs_id"], obs["source"]
    source_cfg = rules.claims["sources"].get(source)
    if source_cfg is None:
        tracer.step(op="unmapped_source", rule_id="claims.yaml#sources",
                    inputs=[f"obs:{obs_id}"], output=source)
        return []
    group = source_cfg["witness_group"]
    payload = obs.get("raw_payload") or ""
    out: list[Extraction] = []

    for name, rule in rules.extraction.get("rules", {}).items():
        if source not in rule["sources"]:
            continue
        m = re.search(rule["pattern"], payload)
        if not m:
            continue
        rule_id = f"extraction.yaml#{name}"
        # The span must bound exactly the stripped value -- raw_payload[start:end]
        # is the evidence pointer (§9.4), and re.search's `v` group can include
        # leading/trailing whitespace before its delimiter that .strip() removes.
        captured = m.group("v")
        value = captured.strip()
        start = m.start("v") + (len(captured) - len(captured.lstrip()))
        end = start + len(value)
        step = tracer.step(
            op="extract",
            rule_id=rule_id,
            inputs=[f"obs:{obs_id}#raw_payload[{start}:{end}]"],
            output=value,
        )
        e = _finish(rule["target_kind"], rule["target"], value,
                    source, group, rule_id, rules, tracer, step)
        if e:
            out.append(e)

    for name, rule in rules.extraction.get("structured", {}).items():
        raw = (obs.get(rule["column"]) or "").strip()
        if not raw and rules.claims.get("drop_empty", True):
            continue
        rule_id = f"extraction.yaml#{name}"
        step = tracer.step(op="extract", rule_id=rule_id,
                           inputs=[f"obs:{obs_id}#{rule['column']}"], output=raw)
        e = _finish(rule["target_kind"], rule["target"], raw, source,
                    "structured_column", rule_id, rules, tracer, step)
        if e:
            out.append(e)

    # OUI-as-vendor is deliberately hardcoded to ("field", "vendor") rather
    # than read from oui_cfg, unlike the port_signatures branch below. This
    # structurally enforces that an OUI can never become a link_basis no
    # matter what a future YAML edit says: an OUI prefix is shared by every
    # device a manufacturer ever shipped, so as a link_basis it would merge
    # all 8 Axis P3245-LVE cameras in this dataset into one entity.
    oui_cfg = rules.extraction.get("oui", {})
    mac = (obs.get("mac") or "").strip().upper()
    if mac and len(mac) >= 8:
        vendor = oui_cfg.get("map", {}).get(mac[:8])
        if vendor:
            rule_id = "extraction.yaml#oui"
            step = tracer.step(op="extract", rule_id=rule_id,
                               inputs=[f"obs:{obs_id}#mac[0:8]"], output=vendor)
            out.append(Extraction("field", "vendor", vendor, source, "oui",
                                  rule_id, step))

    ps_cfg = rules.extraction.get("port_signatures", {})
    ps_kind = ps_cfg.get("target_kind", "field")
    ps_target = ps_cfg.get("target", "device_type")
    ports = _ports(obs)
    for dtype, spec in ps_cfg.get("rules", {}).items():
        if not all(p in ports for p in spec.get("all_of", [])):
            continue
        if spec.get("any_of") and not any(p in ports for p in spec["any_of"]):
            continue
        if any(p in ports for p in spec.get("none_of", [])):
            continue
        rule_id = f"extraction.yaml#port_signatures.{dtype}"
        step = tracer.step(op="extract", rule_id=rule_id,
                           inputs=[f"obs:{obs_id}#open_ports"], output=dtype)
        out.append(Extraction(ps_kind, ps_target, dtype, source,
                              "port_signature", rule_id, step))

    if not out:
        tracer.step(op="no_extraction", rule_id="extraction.yaml",
                    inputs=[f"obs:{obs_id}"], output=None)
    return out

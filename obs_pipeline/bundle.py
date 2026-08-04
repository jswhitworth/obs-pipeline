# obs_pipeline/bundle.py
"""Run bundle writers (design doc §3).

Every artifact is machine-readable. Every tabular row carries run_id and
derivation_step; neither rule versions nor derivation chains are repeated per
row -- they resolve through run_id -> manifest.json and derivation_step ->
trace.jsonl, keeping rows narrow while remaining fully traceable.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

FIELDS = ["vendor", "model", "device_type", "firmware"]


def file_hash(path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_manifest(rules, input_hash: str, run_id: str, engine_commit: str) -> dict:
    return {
        "run_id": run_id,
        "input_hash": input_hash,
        "rules_version": rules.version,
        "rules_rollup": rules.rollup,
        "rules_files": dict(sorted(rules.file_hashes.items())),
        "version_verified": rules.version_verified,
        "engine_commit": engine_commit,
    }


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def write_bundle(run_dir, *, manifest, claims, memberships, resolved, obs_fields,
                 entity_steps, stability_steps, tracer) -> None:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = manifest["run_id"]

    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    _write_csv(
        run_dir / "claims.csv",
        ["run_id", "derivation_step", "obs_id", "kind", "key", "value",
         "weight", "witness_groups", "sources", "in_vocab"],
        [{"run_id": run_id, "derivation_step": c.traced.step_id,
          "obs_id": c.obs_id, "kind": c.kind, "key": c.key, "value": c.value,
          "weight": c.weight, "witness_groups": "|".join(c.witness_groups),
          "sources": "|".join(c.sources), "in_vocab": c.in_vocab}
         for c in claims],
    )

    _write_csv(
        run_dir / "membership.csv",
        ["run_id", "derivation_step", "obs_id", "entity_id", "link_basis",
         "link_weight", "basis_agreement", "conflict_detail"],
        [{"run_id": run_id, "derivation_step": m.traced.step_id,
          "obs_id": m.obs_id, "entity_id": m.entity_id,
          "link_basis": m.link_basis, "link_weight": m.link_weight,
          "basis_agreement": m.basis_agreement,
          "conflict_detail": m.conflict_detail or ""}
         for m in memberships],
    )

    entity_header = (["run_id", "derivation_step", "entity_id"]
                     + FIELDS + [f"{f}_confidence" for f in FIELDS]
                     + ["confidence", "stability"])
    entity_rows = []
    for entity_id in sorted(resolved):
        fields = resolved[entity_id]
        row = {"run_id": run_id,
               "derivation_step": entity_steps[entity_id].step_id,
               "entity_id": entity_id,
               "confidence": entity_steps[entity_id].value,
               "stability": stability_steps[entity_id].value}
        for f in FIELDS:
            row[f] = fields[f].value
            row[f"{f}_confidence"] = fields[f].confidence
        entity_rows.append(row)
    _write_csv(run_dir / "entities.csv", entity_header, entity_rows)

    # §3.1 -- a PURE JOIN VIEW. It makes no new decisions, so it emits no trace
    # steps and points at the same resolve_entity step as the entity row.
    res_header = (["obs_id", "run_id", "derivation_step", "entity_id"]
                  + [c for f in FIELDS for c in (f, f"{f}_provenance")]
                  + ["confidence", "stability"])
    res_rows = []
    for m in sorted(memberships, key=lambda m: m.obs_id):
        # Per-OBSERVATION provenance (§3.1), not the entity's -- OBS-061
        # witnessed its vendor directly while its sibling OBS-073 inherited it.
        fields = obs_fields[m.obs_id]
        row = {"obs_id": m.obs_id, "run_id": run_id,
               "derivation_step": entity_steps[m.entity_id].step_id,
               "entity_id": m.entity_id,
               "confidence": entity_steps[m.entity_id].value,
               "stability": stability_steps[m.entity_id].value}
        for f in FIELDS:
            row[f] = fields[f].value
            row[f"{f}_provenance"] = fields[f].provenance
        res_rows.append(row)
    _write_csv(run_dir / "resolutions.csv", res_header, res_rows)

    with open(run_dir / "trace.jsonl", "w", encoding="utf-8") as fh:
        for step in tracer.steps():
            fh.write(json.dumps(step, sort_keys=True, separators=(",", ":"),
                                default=str) + "\n")

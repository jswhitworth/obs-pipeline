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

from obs_pipeline.fields import absent_values


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
        # §2.5 -- report.py renders a written bundle and has no RuleSet of
        # its own, so the escape/absence vocabulary it needs to classify a
        # value as "unknown" is written here, once, at the only point that
        # DOES have the rules loaded. This keeps a bundle self-describing
        # and regenerable (REPORT.md's own claim) without report.py either
        # re-reading rules/ (which may have moved on since this run) or
        # carrying a second hardcoded copy of the escape literals.
        "absent_values": sorted(absent_values(rules)),
    }


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def write_bundle(run_dir, *, manifest, claims, memberships, resolved, obs_fields,
                 entity_steps, stability_steps, tracer, rules) -> None:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = manifest["run_id"]

    # §6.1 -- read from claims.yaml#fields rather than a hardcoded copy.
    # Three modules (here, report.py, replay.py) used to carry their own
    # ["vendor", "model", "device_type", "firmware"] literal; a rules-legal
    # fifth field silently produced trace steps with no corresponding column
    # anywhere the pipeline writes.
    #
    # Do NOT sort this: claims.yaml's declared order (vendor, model,
    # firmware, device_type) is a deliberate authoring choice that tracks
    # design doc §3's most-identifying-field-first ordering. A YAML list is
    # ordered; discarding that order for an alphabetical one would make the
    # written schema differ from the documented one for no reason. This is
    # THE writer of entities.csv/resolutions.csv, so it is the one place
    # that must preserve the rules' order rather than just any stable order
    # (contrast replay.py, which never writes a CSV and sorts deliberately
    # for comparison stability -- see the comment there).
    field_names = list(rules.claims["fields"])

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
                     + field_names + [f"{f}_confidence" for f in field_names]
                     + ["confidence", "stability"])
    entity_rows = []
    for entity_id in sorted(resolved):
        entity_fields = resolved[entity_id]
        row = {"run_id": run_id,
               "derivation_step": entity_steps[entity_id].step_id,
               "entity_id": entity_id,
               "confidence": entity_steps[entity_id].value,
               "stability": stability_steps[entity_id].value}
        for f in field_names:
            row[f] = entity_fields[f].value
            row[f"{f}_confidence"] = entity_fields[f].confidence
        entity_rows.append(row)
    _write_csv(run_dir / "entities.csv", entity_header, entity_rows)

    # §3.1 -- resolutions.csv makes no new merge/scoring decisions of its
    # own and points derivation_step at the entity's resolve_entity step.
    # It is NOT trace-step-free, though: the per-observation values below
    # come from `observation_fields` (fields.py), which DOES emit its own
    # `observation_field`/`propagate` trace steps for exactly these values
    # (Task 10's undecidable fix made this true). Those per-observation
    # steps are children of resolve_field, not of resolve_entity, so they
    # are not reachable by walking the pointer this row carries -- meaning
    # resolutions.csv sits outside replay.py's three-file scope (claims,
    # membership, entities). That is a known, accepted gap, not a bug;
    # widening replay's scope to cover it is a separate decision.
    res_header = (["obs_id", "run_id", "derivation_step", "entity_id"]
                  + [c for f in field_names for c in (f, f"{f}_provenance")]
                  + ["confidence", "stability"])
    res_rows = []
    for m in sorted(memberships, key=lambda m: m.obs_id):
        # Per-OBSERVATION provenance (§3.1), not the entity's -- OBS-061
        # witnessed its vendor directly while its sibling OBS-073 inherited it.
        obs_field_values = obs_fields[m.obs_id]
        row = {"obs_id": m.obs_id, "run_id": run_id,
               "derivation_step": entity_steps[m.entity_id].step_id,
               "entity_id": m.entity_id,
               "confidence": entity_steps[m.entity_id].value,
               "stability": stability_steps[m.entity_id].value}
        for f in field_names:
            row[f] = obs_field_values[f].value
            row[f"{f}_provenance"] = obs_field_values[f].provenance
        res_rows.append(row)
    _write_csv(run_dir / "resolutions.csv", res_header, res_rows)

    with open(run_dir / "trace.jsonl", "w", encoding="utf-8") as fh:
        for step in tracer.steps():
            fh.write(json.dumps(step, sort_keys=True, separators=(",", ":"),
                                default=str) + "\n")

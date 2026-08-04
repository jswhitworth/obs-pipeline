#!/usr/bin/env python3
"""Pipeline entry point (design doc §3).

This module and everything it imports have NO code path that reads ground
truth (invariant #5). Evaluation lives in a separate entry point that invokes
this one as a black box.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from obs_pipeline.bundle import file_hash, make_manifest, write_bundle
from obs_pipeline.claims import build_claims
from obs_pipeline.confidence import entity_confidence, stability
from obs_pipeline.entity import resolve_entities
from obs_pipeline.extract import load_observations
from obs_pipeline.fields import observation_fields, resolve_fields
from obs_pipeline.loader import load_rules
from obs_pipeline.trace import Tracer


def _engine_commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, check=True).stdout.strip()
        return f"git:{sha}"
    except Exception:
        return "git:unknown"


def _run_id(input_hash: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{stamp}-{input_hash.split(':')[1][:6]}"


def run_pipeline(observations_path, rules_dir, out_root) -> Path:
    tracer = Tracer()
    # load_rules writes its stale-bump state file here, so the directory must
    # exist before the first run -- otherwise a fresh checkout crashes on
    # `python3 run.py`, which pytest hides because its fixture pre-creates it.
    Path(out_root).mkdir(parents=True, exist_ok=True)
    rules = load_rules(rules_dir, observations_path,
                       state_path=Path(out_root) / "last_rules_state.json")
    observations = load_observations(observations_path)

    claims = build_claims(observations, rules, tracer)
    memberships = resolve_entities(claims, observations, rules, tracer)
    resolved = resolve_fields(claims, memberships, rules, tracer)
    obs_fields = observation_fields(claims, memberships, resolved, rules, tracer)

    entity_steps, stability_steps = {}, {}
    for entity_id in sorted(resolved):
        entity_steps[entity_id] = entity_confidence(resolved[entity_id], tracer)
        stability_steps[entity_id] = stability(resolved[entity_id], rules, tracer)

    input_hash = file_hash(observations_path)
    run_id = _run_id(input_hash)
    manifest = make_manifest(rules, input_hash, run_id, _engine_commit())

    run_dir = Path(out_root) / run_id
    write_bundle(run_dir, manifest=manifest, claims=claims,
                 memberships=memberships, resolved=resolved,
                 obs_fields=obs_fields, entity_steps=entity_steps,
                 stability_steps=stability_steps, tracer=tracer)
    return run_dir


if __name__ == "__main__":
    out = run_pipeline(
        sys.argv[1] if len(sys.argv) > 1 else "obs-data/observations.csv",
        sys.argv[2] if len(sys.argv) > 2 else "rules",
        sys.argv[3] if len(sys.argv) > 3 else "runs",
    )
    print(out)

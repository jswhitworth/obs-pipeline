#!/usr/bin/env python3
"""Replay: the trace completeness test (design doc §9.5).

Reads trace.jsonl ALONE -- no observations.csv, no rules -- and reconstructs
claims.csv, membership.csv and entities.csv. The reconstruction is diffed
against the actual run outputs. If they match, the trace is provably
sufficient to explain every emitted value.

This module must NEVER import the engine. Reaching scoring/entity/fields would
let it recompute a value instead of reading it, and the guarantee would be
vacuous. Enforced by tests/test_replay.py.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

FIELDS = ["vendor", "model", "device_type", "firmware"]


def _obs_of(step, by_id) -> str | None:
    """Recover which observation a step derives from, by walking parents to an
    `obs:` input. Only possible because score steps name their evidence."""
    frontier, seen = list(step["parents"]), set()
    while frontier:
        sid = frontier.pop()
        if sid in seen:
            continue
        seen.add(sid)
        parent = by_id.get(sid)
        if parent is None:
            continue
        for ref in parent["inputs"]:
            if ref.startswith("obs:"):
                return ref.split(":", 1)[1].split("#", 1)[0]
        frontier += parent["parents"]
    return None


def _load_trace(path) -> list[dict]:
    return [json.loads(line) for line in
            Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def reconstruct(trace_path) -> dict[str, list[dict]]:
    steps = _load_trace(trace_path)
    by_id = {s["step_id"]: s for s in steps}

    claims = []
    for s in steps:
        if s["op"] != "score":
            continue
        d = s["decomposition"]
        claims.append({
            "derivation_step": s["step_id"],
            "obs_id": _obs_of(s, by_id),
            "key": s["key"],
            "value": s["value"],
            "weight": s["output"],
            "witness_groups": "|".join(d["witness_groups"]),
        })

    membership = []
    for s in steps:
        if s["op"] != "assign_entity":
            continue
        membership.append({
            "derivation_step": s["step_id"],
            "obs_id": s["inputs"][0].split(":", 1)[1],
            "entity_id": s["output"],
            "link_basis": s["detail"]["link_basis"] or "none",
            "basis_agreement": str(s["detail"]["basis_agreement"]),
        })

    field_by_entity: dict[str, dict[str, dict]] = {}
    for s in steps:
        if s["op"] != "resolve_field":
            continue
        entity_id = s["inputs"][0].split(":", 1)[1]
        field_by_entity.setdefault(entity_id, {})[s["field"]] = {
            "value": s["output"], "confidence": s["confidence"],
        }

    stability_by_entity = {}
    for s in steps:
        if s["op"] == "stability":
            for parent in s["parents"]:
                p = by_id.get(parent)
                if p and p["op"] == "resolve_field":
                    stability_by_entity[p["inputs"][0].split(":", 1)[1]] = s["output"]

    entities = []
    for s in steps:
        if s["op"] != "resolve_entity":
            continue
        entity_id = None
        for parent in s["parents"]:
            p = by_id.get(parent)
            if p and p["op"] == "resolve_field":
                entity_id = p["inputs"][0].split(":", 1)[1]
                break
        if entity_id is None:
            continue
        row = {"derivation_step": s["step_id"], "entity_id": entity_id,
               "confidence": s["output"],
               "stability": stability_by_entity.get(entity_id)}
        for f in FIELDS:
            got = field_by_entity.get(entity_id, {}).get(f, {})
            row[f] = got.get("value", "")
            row[f"{f}_confidence"] = got.get("confidence")
        entities.append(row)

    return {"claims": claims, "membership": membership, "entities": entities}


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def replay_diff(run_dir) -> list[str]:
    run_dir = Path(run_dir)
    problems: list[str] = []

    # Graph integrity runs FIRST, before any reconstruction. §9.5 promises the
    # diff "names the hole precisely" -- so a damaged trace must produce a
    # report, never a stack trace. A crash also exits 1, exactly like a
    # detected failure, leaving CI unable to tell a caught hole from a broken
    # gate.
    #
    # Every parent reference must resolve to a step that is present. Without
    # this, deleting a step that nothing reconstructs from -- a merge
    # decision, say -- is invisible, and the gate certifies a trace that has
    # had its reasoning removed.
    steps = _load_trace(run_dir / "trace.jsonl")
    present = {s["step_id"] for s in steps}
    for step in steps:
        for parent in step["parents"]:
            if parent not in present:
                problems.append(
                    f"trace: step {step['step_id']} ({step['op']}) references "
                    f"missing parent {parent}"
                )

    rebuilt = reconstruct(run_dir / "trace.jsonl")

    # A claim whose obs_id could not be recovered is itself a hole: nothing in
    # the trace connects it to an observation. Report it explicitly rather
    # than letting a None flow into the comparison below, where it would
    # either poison the sort or silently bucket unrelated claims together.
    for row in rebuilt["claims"]:
        if row["obs_id"] is None:
            problems.append(
                f"claims: step {row['derivation_step']} has no recoverable "
                f"obs_id -- its parent chain reaches no obs: input"
            )

    # Compare per-(obs_id, key, value) MULTISETS, not a set of step ids.
    # A set comparison cannot see aliasing: if N claims collapsed onto one
    # shared step id, both sides reduce to the same set and the diff reports
    # nothing while the trace is genuinely short by N-1 derivations.
    actual_claims = Counter(
        (r["obs_id"], r["key"], r["value"])
        for r in _read_csv(run_dir / "claims.csv")
    )
    rebuilt_claims = Counter(
        (r["obs_id"], r["key"], r["value"]) for r in rebuilt["claims"]
        if r["obs_id"] is not None
    )
    for signature in sorted(set(actual_claims) | set(rebuilt_claims)):
        want, got = actual_claims[signature], rebuilt_claims[signature]
        if want != got:
            problems.append(
                f"claims: {signature} appears {want}x in output, {got}x in trace"
            )

    actual_claims_rows = {
        (r["obs_id"], r["key"], r["value"]): r
        for r in _read_csv(run_dir / "claims.csv")
    }
    rebuilt_claims_rows = {
        (r["obs_id"], r["key"], r["value"]): r for r in rebuilt["claims"]
        if r["obs_id"] is not None
    }
    for sig, row in sorted(actual_claims_rows.items()):
        got = rebuilt_claims_rows.get(sig)
        if got is not None and str(got["weight"]) != row["weight"]:
            problems.append(
                f"claims: {sig} weight {row['weight']} != {got['weight']}"
            )

    actual_mem = {r["obs_id"]: r for r in _read_csv(run_dir / "membership.csv")}
    rebuilt_mem = {r["obs_id"]: r for r in rebuilt["membership"]}
    for obs_id, row in sorted(actual_mem.items()):
        got = rebuilt_mem.get(obs_id)
        if got is None:
            problems.append(f"membership: {obs_id} not reconstructible from trace")
            continue
        # Compare every column reconstruct() produces. Computing a value and
        # then not diffing it is the same as not reconstructing it at all.
        for column in ("entity_id", "link_basis", "basis_agreement"):
            if str(got[column]) != row[column]:
                problems.append(
                    f"membership: {obs_id}.{column} "
                    f"{row[column]!r} != {got[column]!r}"
                )

    actual_ent = {r["entity_id"]: r for r in _read_csv(run_dir / "entities.csv")}
    rebuilt_ent = {r["entity_id"]: r for r in rebuilt["entities"]}
    for entity_id, row in sorted(actual_ent.items()):
        got = rebuilt_ent.get(entity_id)
        if got is None:
            problems.append(f"entities: {entity_id} not reconstructible from trace")
            continue
        for f in FIELDS:
            if str(got.get(f, "")) != row[f]:
                problems.append(
                    f"entities: {entity_id}.{f} '{row[f]}' != '{got.get(f)}'"
                )
            if str(got.get(f"{f}_confidence")) != row[f"{f}_confidence"]:
                problems.append(
                    f"entities: {entity_id}.{f}_confidence "
                    f"{row[f'{f}_confidence']} != {got.get(f'{f}_confidence')}"
                )
        for column in ("confidence", "stability"):
            if str(got[column]) != row[column]:
                problems.append(
                    f"entities: {entity_id}.{column} "
                    f"{row[column]} != {got[column]}"
                )
    return problems


if __name__ == "__main__":
    diff = replay_diff(sys.argv[1])
    for line in diff:
        print(line)
    print(f"{'REPLAY OK' if not diff else f'REPLAY FAILED: {len(diff)} holes'}")
    sys.exit(1 if diff else 0)

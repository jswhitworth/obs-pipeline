import ast
import json
from pathlib import Path

import pytest

from replay import replay_diff
from run import run_pipeline


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return run_pipeline("obs-data/observations.csv", "rules",
                        tmp_path_factory.mktemp("runs"))


def test_trace_alone_reconstructs_every_output(bundle):
    """§9.5: if they match, the trace is PROVABLY sufficient to explain every
    emitted value. This is the mechanical enforcement of invariant #7."""
    assert replay_diff(bundle) == []


def test_replay_detects_claim_step_aliasing(bundle, tmp_path):
    """A set-of-step-ids comparison cannot see aliasing: if several claims
    shared one derivation step, both sides of the diff reduce to the same set
    and the gate passes while the trace is genuinely short. Deleting one
    score step must therefore be caught by COUNT, not by set membership."""
    aliased = tmp_path / "aliased"
    aliased.mkdir()
    for p in bundle.iterdir():
        (aliased / p.name).write_bytes(p.read_bytes())
    lines = (aliased / "trace.jsonl").read_text().strip().splitlines()
    dropped, kept = None, []
    for line in lines:
        step = json.loads(line)
        if dropped is None and step["op"] == "score":
            dropped = step
            continue
        kept.append(line)
    (aliased / "trace.jsonl").write_text("\n".join(kept) + "\n")
    diff = replay_diff(aliased)
    assert any(line.startswith("claims:") for line in diff), diff


def test_replay_detects_a_hole_in_the_trace(bundle, tmp_path):
    """If any field cannot be reconstructed, the diff must name the hole."""
    broken = tmp_path / "broken"
    broken.mkdir()
    for p in bundle.iterdir():
        (broken / p.name).write_bytes(p.read_bytes())

    lines = (broken / "trace.jsonl").read_text().strip().splitlines()
    kept = [ln for ln in lines if json.loads(ln)["op"] != "resolve_entity"]
    (broken / "trace.jsonl").write_text("\n".join(kept) + "\n")

    diff = replay_diff(broken)
    assert diff
    assert any("entities" in line for line in diff)


def test_deleting_merge_decisions_is_caught(bundle, tmp_path):
    """§9: cluster identity is emergent from a SEQUENCE of merge decisions,
    and that sequence is not recoverable from the outcome. If every merge can
    be deleted while the gate still says REPLAY OK, the trace is asserting
    the partition rather than explaining it."""
    stripped = tmp_path / "stripped"
    stripped.mkdir()
    for p in bundle.iterdir():
        (stripped / p.name).write_bytes(p.read_bytes())
    kept = [ln for ln in (stripped / "trace.jsonl").read_text().splitlines()
            if json.loads(ln)["op"] not in ("merge", "merge_redundant")]
    (stripped / "trace.jsonl").write_text("\n".join(kept) + "\n")
    diff = replay_diff(stripped)
    assert any("missing parent" in line for line in diff), diff


def test_mutating_a_reconstructed_value_is_caught(bundle, tmp_path):
    """A value that reconstruct() computes but replay_diff never compares is
    a value the gate does not actually cover. Corrupt one claim weight."""
    mutated = tmp_path / "mutated"
    mutated.mkdir()
    for p in bundle.iterdir():
        (mutated / p.name).write_bytes(p.read_bytes())
    lines, done = [], False
    for ln in (mutated / "trace.jsonl").read_text().splitlines():
        step = json.loads(ln)
        if not done and step["op"] == "score" and step["output"] > 0.2:
            step["output"] = round(step["output"] - 0.1, 6)
            ln, done = json.dumps(step, sort_keys=True, separators=(",", ":")), True
        lines.append(ln)
    assert done, "no score step was mutated"
    (mutated / "trace.jsonl").write_text("\n".join(lines) + "\n")
    diff = replay_diff(mutated)
    assert any("weight" in line for line in diff), diff


def test_replay_does_not_import_the_engine():
    """§2.1 of the implementation spec: if replay could reach the engine it
    might reconstruct a value by RECOMPUTING it rather than by reading the
    trace, and the completeness test would pass on an incomplete trace."""
    tree = ast.parse(Path("replay.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    # ALLOWLIST, not a blocklist: a blocklist silently stops guarding the
    # moment someone adds a new engine module.
    allowed = {"obs_pipeline.trace", "obs_pipeline.bundle"}
    reached = {m for m in imported
               if (m == "run" or m.startswith("obs_pipeline"))
               and m not in allowed}
    assert not reached, f"replay.py reaches the engine: {sorted(reached)}"


def test_replay_reads_no_file_other_than_the_trace(bundle, tmp_path):
    """§9.5: replay.py reads trace.jsonl ALONE -- no observations.csv, no rules."""
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    for name in ["trace.jsonl", "claims.csv", "membership.csv", "entities.csv"]:
        (isolated / name).write_bytes((bundle / name).read_bytes())
    assert replay_diff(isolated) == []

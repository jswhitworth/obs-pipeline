# tests/test_bundle.py
import csv
import json
from pathlib import Path

import pytest

from run import run_pipeline

BUNDLE = None


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    global BUNDLE
    if BUNDLE is None:
        BUNDLE = run_pipeline("obs-data/observations.csv", "rules",
                              tmp_path_factory.mktemp("runs"))
    return BUNDLE


def _rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_bundle_contains_every_declared_artifact(bundle):
    for name in ["manifest.json", "claims.csv", "membership.csv",
                 "entities.csv", "resolutions.csv", "trace.jsonl"]:
        assert (bundle / name).exists(), name


def test_manifest_records_both_rule_identifiers(bundle):
    m = json.loads((bundle / "manifest.json").read_text())
    assert m["rules_version"] == "0.1.0"
    assert m["rules_rollup"].startswith("sha256:")
    assert set(m["rules_files"]) >= {"scoring.yaml", "canonical_vocab.csv"}
    assert m["input_hash"].startswith("sha256:")
    assert "version_verified" in m


def test_manifest_omits_label_identifiers(bundle):
    """§6.2: run.py never reads labels, so a run manifest asserting a label
    hash would claim knowledge the pipeline structurally cannot have."""
    m = json.loads((bundle / "manifest.json").read_text())
    assert "labels_hash" not in m
    assert "labels_version" not in m


def test_every_row_carries_run_id_and_derivation_step(bundle):
    for name in ["claims.csv", "membership.csv", "entities.csv", "resolutions.csv"]:
        rows = _rows(bundle / name)
        assert rows, name
        for row in rows:
            assert row["run_id"]
            assert row["derivation_step"].startswith("sha256:")


def test_rows_do_not_repeat_rule_hashes(bundle):
    """§3/§6.2: run_id is constant across a run; repeating six hashes on every
    row would be pure duplication."""
    header = _rows(bundle / "membership.csv")[0]
    assert "rules_rollup" not in header
    assert "rules_version" not in header


def test_membership_carries_the_tie_break_columns(bundle):
    rows = _rows(bundle / "membership.csv")
    assert {"obs_id", "entity_id", "link_basis", "link_weight",
            "basis_agreement", "conflict_detail"} <= set(rows[0])
    assert all(r["basis_agreement"] in ("True", "False") for r in rows)


def test_resolutions_is_one_row_per_observation_with_per_field_provenance(bundle):
    rows = _rows(bundle / "resolutions.csv")
    assert len(rows) == 74
    for f in ["vendor", "model", "device_type", "firmware"]:
        assert f"{f}_provenance" in rows[0]
    allowed = {"direct", "propagated", "unknown"}
    assert {r["vendor_provenance"] for r in rows} <= allowed


def test_provenance_differs_between_members_of_one_entity(bundle):
    """§3.1: the whole point of the column. OBS-001's ONVIF payload states
    Model=P3245-LVE; OBS-002 is the same device over HTTP, whose realm yields
    no model, so it can only show one by inheritance. If both rows said the
    same thing, Stage 1/2 and Stage 4 would score the same population.

    Do NOT use OBS-061/073 vendor here -- OBS-073's mDNS payload carries
    `vendor=HIKVISION` outright, so both witness it directly."""
    rows = {r["obs_id"]: r for r in _rows(bundle / "resolutions.csv")}
    assert rows["OBS-001"]["entity_id"] == rows["OBS-002"]["entity_id"]
    assert rows["OBS-001"]["model_provenance"] == "direct"
    assert rows["OBS-002"]["model_provenance"] == "propagated"


def test_undecidable_entity_keeps_per_observation_firmware_in_resolutions(bundle):
    """§2.5: the ambiguity is entity-level only. resolutions.csv must show
    what each observation actually saw, not the entity's `undecidable`."""
    rows = {r["obs_id"]: r for r in _rows(bundle / "resolutions.csv")}
    assert rows["OBS-069"]["firmware"] == "8.10.0135"
    assert rows["OBS-074"]["firmware"] == "8.11.0021"
    assert rows["OBS-069"]["firmware_provenance"] == "direct"


def test_resolutions_points_at_the_same_resolve_entity_step_as_the_entity(bundle):
    """§3.1: resolutions.csv makes no new merge/scoring decisions of its own,
    and its `derivation_step` points at the entity's `resolve_entity` step
    rather than minting its own -- that pointer equality is what this test
    asserts. It is NOT trace-step-free, though: the per-observation
    value/provenance columns come from `observation_fields` (fields.py),
    which DOES emit its own observation_field/propagate trace steps for
    exactly these values; those steps are children of resolve_field, not of
    resolve_entity, so they are not reachable via the pointer this row
    carries."""
    ents = {r["entity_id"]: r["derivation_step"] for r in _rows(bundle / "entities.csv")}
    for r in _rows(bundle / "resolutions.csv"):
        assert r["derivation_step"] == ents[r["entity_id"]]


def test_entities_carry_per_field_confidence_plus_rollup_and_stability(bundle):
    row = _rows(bundle / "entities.csv")[0]
    for f in ["vendor", "model", "device_type", "firmware"]:
        assert f in row and f"{f}_confidence" in row
    assert "confidence" in row and "stability" in row


def test_engine_commit_records_a_dirty_working_tree(bundle):
    """§6.2: a manifest reporting a clean sha while uncommitted engine code
    ran is worse than omitting the field -- precise-looking and wrong."""
    import subprocess
    m = json.loads((bundle / "manifest.json").read_text())
    dirty = subprocess.run(["git", "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    assert m["engine_commit"].endswith("-dirty") == bool(dirty), (
        f"engine_commit={m['engine_commit']} but working tree "
        f"{'is' if dirty else 'is not'} dirty"
    )


def test_two_runs_in_the_same_second_do_not_clobber_each_other(tmp_path):
    """run_id is second-precision, and runs take a few hundred ms. Without a
    collision suffix an edit-and-rerun inside one second silently destroys
    the earlier bundle."""
    from run import run_pipeline
    root = tmp_path / "runs"
    first = run_pipeline("obs-data/observations.csv", "rules", root)
    second = run_pipeline("obs-data/observations.csv", "rules", root)
    assert first != second, "second run reused the first run's directory"
    assert first.exists() and second.exists()


def test_trace_is_jsonl_with_content_addressed_ids(bundle):
    lines = (bundle / "trace.jsonl").read_text().strip().splitlines()
    assert len(lines) > 500
    ids = [json.loads(line)["step_id"] for line in lines]
    assert all(i.startswith("sha256:") for i in ids)
    assert ids == sorted(ids)


def test_absence_steps_are_present_in_the_trace(bundle):
    """`merge_refused` is deliberately NOT asserted here. Every identity claim
    in this dataset clears the 0.55 threshold and no cross-basis contradiction
    occurs, so a refusal step would only appear if the rules were bent to
    manufacture one. The refusal path is unit-tested in tests/test_entity.py
    against a raised threshold instead."""
    ops = {json.loads(line)["op"]
           for line in (bundle / "trace.jsonl").read_text().splitlines()}
    assert {"no_extraction", "no_identity_claim", "vocab_reject"} <= ops


def test_run_py_never_reads_labels(bundle):
    """Invariant #5, checked against the source rather than asserted.

    The invariant is about READING ground truth, not about the word 'labels'
    appearing -- metrics.py legitimately carries a `requires_labels` flag
    (§8.1). What must not exist is a path that opens a label file or imports
    the label tooling."""
    forbidden = ["labels/", "labels.csv", "labels-initial",
                 "import label_tools", "from label_tools"]
    for p in list(Path("obs_pipeline").glob("*.py")) + [Path("run.py")]:
        text = p.read_text()
        for needle in forbidden:
            assert needle not in text, f"{p} reaches ground truth via '{needle}'"

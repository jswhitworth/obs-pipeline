"""Task 19: the eval harness (design doc §7.6, §8.4).

eval.py sits at the repo root, outside obs_pipeline/, and invokes run.py as a
black box. It is the ONLY component that reads both rules and labels, so it
scores a written bundle against labels/labels.csv and emits its own parallel
bundle under evals/<eval_id>/.
"""
import json

import pytest

from eval import evaluate
from obs_pipeline.metrics import load_registry


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return evaluate("obs-data/observations.csv", "rules", "labels/labels.csv",
                    tmp_path_factory.mktemp("evals"))


def _rows(bundle):
    return [json.loads(line) for line in
            (bundle / "metrics.jsonl").read_text().splitlines() if line.strip()]


def test_harness_emits_its_own_parallel_bundle(bundle):
    """§3: the eval harness emits its own bundle under evals/<eval_id>/."""
    for name in ["eval_manifest.json", "metrics.jsonl", "regressions.csv"]:
        assert (bundle / name).exists(), name


def test_eval_manifest_versions_both_sides(bundle):
    """§7.6: the harness is the only component that reads both rules and
    labels, so it emits its own manifest recording both."""
    m = json.loads((bundle / "eval_manifest.json").read_text())
    assert m["rules_rollup"].startswith("sha256:")
    assert m["labels_hash"].startswith("sha256:")
    assert m["labels_version"] == "0.1.0"
    assert m["run_id"]


def test_eval_emits_label_dependent_metrics(bundle):
    registry = load_registry("metrics.yaml")
    metrics = {r["metric"] for r in _rows(bundle)}
    assert any(registry[m]["requires_labels"] for m in metrics)
    assert "top1_claim_accuracy" in metrics


def test_stage3_counts_false_merge_and_false_split_separately(bundle):
    """§8.4: their costs differ sharply -- a false merge corrupts every field
    on every member via propagation."""
    metrics = {r["metric"] for r in _rows(bundle)}
    assert "false_merge_count" in metrics
    assert "false_split_count" in metrics


def test_stage3_reports_the_positive_pair_count_so_n_is_visible(bundle):
    """§7.4: any threshold 'met' at ~5 positive pairs is noise, so n must
    travel with the number.

    The true count is 7, not 6: E-001 has 3 members => C(3,2) = 3 pairs,
    plus one pair each from E-002/E-052/E-066/E-074 = 3 + 4 = 7. Task 18's
    ledger (progress.md) independently verified this by counting
    labels.csv's `same_device` rows directly (7 rows). A stale draft of this
    test asserted 6; confirmed against the actual labels.csv content before
    writing this file.
    """
    row = next(r for r in _rows(bundle) if r["metric"] == "pairwise_precision")
    assert row["n"] < 30       # below the §11 defensible floor
    assert row["n"] == 7


def test_blank_labels_are_excluded_from_denominators(bundle):
    """§7.2: scoring a correctly sibling-propagated value as wrong against a
    blank would break Stage 4 precisely where it is meant to work."""
    row = next(r for r in _rows(bundle)
               if r["metric"] == "top1_claim_accuracy" and r["scope"] == "field:firmware")
    assert row["n"] < 74       # OBS-003, OBS-005 etc. have blank firmware


def test_stage1_and_2_score_direct_fields_only(bundle):
    """§3.1: diffing a propagated row naively against labels would credit the
    pipeline for extraction it never performed."""
    scopes = {r["scope"] for r in _rows(bundle) if r["metric"] == "extraction_recall"}
    assert any(s.endswith(":direct") for s in scopes)


def test_stage4_scores_propagated_fields_only(bundle):
    rows = [r for r in _rows(bundle) if r["metric"] == "propagated_value_accuracy"]
    assert rows
    assert all(r["n"] >= 0 for r in rows)


def test_undecidable_is_excluded_from_denominators_and_reported_separately(bundle):
    """§7.2.1: forcing these to a single correct_value scores the rules
    against something unlearnable from the observation."""
    metrics = {r["metric"] for r in _rows(bundle)}
    assert "undecidable_rate" in metrics


def test_firmware_undecidable_entities_are_not_scored_wrong(bundle):
    """§2.5: the per-observation readings remain individually correct; the
    ambiguity exists at the entity level only."""
    outcomes = json.loads((bundle / "outcomes.json").read_text())
    for o in outcomes:
        if o["obs_id"] in ("OBS-069", "OBS-074") and o["key"] == "firmware":
            assert o["correct"] is not False


def test_eval_py_does_not_live_inside_the_package():
    """§7.6: eval.py is a separate entry point, NOT a flag on run.py. This
    keeps invariant #5 structural rather than conventional."""
    from pathlib import Path
    assert Path("eval.py").exists()
    assert not Path("obs_pipeline/eval.py").exists()


def test_eval_does_not_hardcode_the_field_vocabulary(bundle):
    """§6.1: three modules once carried private copies of claims.yaml's
    declared field list; adding a field silently moved published numbers
    while stale consumers ignored the new column. eval.py has no RuleSet
    (it scores a WRITTEN bundle), so its field list must come from the
    bundle's own entities.csv header, not a literal in eval.py."""
    import eval as eval_module
    assert not hasattr(eval_module, "FIELDS"), (
        "eval.py must not carry a hardcoded field-vocabulary constant")

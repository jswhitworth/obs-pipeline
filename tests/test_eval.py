"""Task 19: the eval harness (design doc §7.6, §8.4).

eval.py sits at the repo root, outside obs_pipeline/, and invokes run.py as a
black box. It is the ONLY component that reads both rules and labels, so it
scores a written bundle against labels/labels.csv and emits its own parallel
bundle under evals/<eval_id>/.
"""
import csv
import json

import pytest

from eval import evaluate
from label_tools import labels_hash
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


def test_extraction_precision_numerator_is_direct_only(bundle):
    """§3.1: diffing a propagated row naively against labels would credit the
    pipeline for extraction it never performed. That constrains precision's
    scope (and both metrics' numerator) to `direct` rows -- it does NOT
    constrain recall's denominator, which must count every labelled value
    including the ones extraction missed (see
    test_precision_and_recall_have_different_denominators)."""
    scopes = {r["scope"] for r in _rows(bundle) if r["metric"] == "extraction_precision"}
    assert any(s.endswith(":direct") for s in scopes)


def test_precision_and_recall_have_different_denominators(bundle):
    """They answer different questions and must not collapse into one number
    reported twice. Precision asks "of what we extracted, how much was
    right"; recall asks "of what was there, how much did we get". A row the
    pipeline failed to extract is a recall MISS and belongs in recall's
    denominator -- filtering it out first is what made the two identical."""
    rows = _rows(bundle)
    rec = {r["scope"].split(":")[1]: r for r in rows if r["metric"] == "extraction_recall"}
    prec = {r["scope"].split(":")[1]: r for r in rows if r["metric"] == "extraction_precision"}
    assert rec and prec
    for field in prec:
        assert rec[field]["n"] > prec[field]["n"], (
            f"{field}: recall n={rec[field]['n']} should exceed precision "
            f"n={prec[field]['n']} -- extraction failures belong in recall"
        )
        assert rec[field]["value"] < prec[field]["value"], field


def test_extraction_recall_counts_the_known_failures(bundle):
    """OBS-034 is a labelled Genetec device whose banner yields no vendor at
    all. If recall does not count it as a miss, recall is not measuring
    recall: vendor is 68 correct of 74 labelled, not 68 of 69 extracted."""
    rows = _rows(bundle)
    rec = next(r for r in rows
               if r["metric"] == "extraction_recall" and r["scope"] == "field:vendor")
    assert rec["n"] == 74
    assert rec["value"] == pytest.approx(68 / 74, abs=1e-4)


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


def test_calibration_buckets_on_per_field_confidence(bundle):
    """The unit of correctness is a FIELD, so the unit of confidence must be
    too. Bucketing on the entity rollup files a 1.0-confidence vendor and a
    0.25-confidence device_type into one bucket and measures neither -- and
    it empties the high-confidence/low-stability quadrant §8.4 exists to
    interrogate."""
    rows = _rows(bundle)
    quad = {r["scope"]: r for r in rows
            if r["metric"] == "accuracy_by_stability"
            and r["scope"].startswith("quadrant:")}
    assert quad["quadrant:high_conf_low_stab"]["n"] > 0, (
        "the quadrant §8.4 asks about is empty -- check the confidence "
        "population before concluding anything about stability"
    )
    assert (quad["quadrant:high_conf_low_stab"]["value"]
            < quad["quadrant:high_conf_high_stab"]["value"])

    # The top calibration bucket must be reachable; on the entity rollup it
    # never was, because device_type drags every rollup below 0.6.
    cal = {r["scope"] for r in rows if r["metric"] == "confidence_calibration_error"}
    assert "bucket:0.9" in cal


def test_top1_accuracy_validates_ranking_not_extraction(bundle):
    """§8.4: Stage 2 "validates ranking only". An observation with no claim
    for a field has no ranking to validate, and including it makes this a
    restatement of extraction_recall -- which it was, byte-identically, for
    two of four fields."""
    rows = _rows(bundle)
    top1 = {r["scope"].split(":")[1]: r for r in rows
            if r["metric"] == "top1_claim_accuracy"}
    rec = {r["scope"].split(":")[1]: r for r in rows
           if r["metric"] == "extraction_recall"}
    for field in top1:
        assert top1[field]["n"] <= rec[field]["n"]
        assert not (top1[field]["n"] == rec[field]["n"]
                    and top1[field]["value"] == rec[field]["value"]), (
            f"{field}: top1 and recall are the same number over the same n"
        )


def test_eval_surfaces_regressions_in_the_report(tmp_path):
    """§7.6: the counts and the broken list belong in REPORT.md, "not only in
    a side file someone has to know to open". Rendering the section is not
    enough if no production caller ever passes an eval bundle."""
    first = evaluate("obs-data/observations.csv", "rules", "labels/labels.csv",
                     tmp_path, runs_root=tmp_path / "runs")
    outcomes = json.loads((first / "outcomes.json").read_text())
    manifest = json.loads((first / "eval_manifest.json").read_text())
    evaluate("obs-data/observations.csv", "rules", "labels/labels.csv",
             tmp_path, runs_root=tmp_path / "runs",
             baseline_outcomes=outcomes,
             baseline_labels_hash=labels_hash("labels/labels.csv"),
             baseline_run_id=manifest["run_id"])
    reports = sorted((tmp_path / "runs").glob("*/REPORT.md"))
    assert reports, "eval produced no report"
    assert "## Regressions" in reports[-1].read_text()


def test_eval_manifest_reports_whether_labels_moved_outside_the_apply_path(
        tmp_path):
    """§7.6 + §6.2's discipline on the label side. The four-bucket diff
    assumes a FROZEN label set; a hand-edit to labels.csv breaks that
    assumption silently, because the edited file still hashes and loads.
    The manifest carries the verdict so an eval is self-describing about
    the ground truth it scored against.
    """
    import shutil

    from label_tools import LONG_HEADER, load_labels, write_label_state

    labels_dir = tmp_path / "labels"
    labels_dir.mkdir()
    labels = labels_dir / "labels.csv"
    shutil.copy("labels/labels.csv", labels)
    shutil.copy("labels/VERSION", labels_dir / "VERSION")
    write_label_state(labels)

    clean = evaluate("obs-data/observations.csv", "rules", labels,
                     tmp_path / "evals-clean", runs_root=tmp_path / "runs")
    assert json.loads((clean / "eval_manifest.json").read_text())[
        "labels_version_verified"] is True

    rows = load_labels(labels)
    rows[0]["value"] = "tampered"
    with open(labels, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LONG_HEADER)
        w.writeheader()
        w.writerows(rows)

    dirty = evaluate("obs-data/observations.csv", "rules", labels,
                     tmp_path / "evals-dirty", runs_root=tmp_path / "runs")
    assert json.loads((dirty / "eval_manifest.json").read_text())[
        "labels_version_verified"] is False

import csv
import json

import pytest

from eval import LabelsMovedError, four_bucket_diff, propose_bump


def _o(obs_id, key, correct):
    return {"obs_id": obs_id, "key_type": "field", "key": key,
            "expected": "X", "actual": "X" if correct else "Y",
            "correct": correct, "provenance": "direct"}


def test_classifies_every_label_into_one_of_four_buckets():
    before = [_o("A", "vendor", False), _o("B", "vendor", True),
              _o("C", "vendor", True), _o("D", "vendor", False)]
    after = [_o("A", "vendor", True), _o("B", "vendor", False),
             _o("C", "vendor", True), _o("D", "vendor", False)]
    d = four_bucket_diff(before, after)
    assert [o["obs_id"] for o in d["fixed"]] == ["A"]
    assert [o["obs_id"] for o in d["broken"]] == ["B"]
    assert [o["obs_id"] for o in d["stable_correct"]] == ["C"]
    assert [o["obs_id"] for o in d["stable_incorrect"]] == ["D"]


def test_aggregate_improvement_does_not_hide_specific_breakage():
    """§7.6: a widened regex that fixes ten observations and breaks two nets
    +8 and looks like an unambiguous win. The aggregate hides the two."""
    before = [_o(f"F{i}", "model", False) for i in range(10)] + \
             [_o(f"B{i}", "model", True) for i in range(2)]
    after = [_o(f"F{i}", "model", True) for i in range(10)] + \
            [_o(f"B{i}", "model", False) for i in range(2)]
    d = four_bucket_diff(before, after)
    assert len(d["fixed"]) == 10
    assert len(d["broken"]) == 2      # visible, not netted away


def test_undecidable_outcomes_are_not_bucketed():
    before = [{"obs_id": "A", "key_type": "field", "key": "firmware",
               "correct": None, "expected": "1", "actual": "undecidable",
               "provenance": "unknown"}]
    after = list(before)
    d = four_bucket_diff(before, after)
    assert sum(len(v) for v in d.values()) == 0


def test_behavior_neutral_change_proposes_patch():
    d = {"fixed": [], "broken": [], "stable_correct": [_o("A", "vendor", True)],
         "stable_incorrect": []}
    assert propose_bump(d, before_keys={"vendor"}, after_keys={"vendor"},
                        before_vocab={"Axis Communications"},
                        after_vocab={"Axis Communications"}) == "patch"


def test_outcome_change_with_vocabulary_intact_proposes_minor():
    d = {"fixed": [_o("A", "vendor", True)], "broken": [],
         "stable_correct": [], "stable_incorrect": []}
    assert propose_bump(d, before_keys={"vendor"}, after_keys={"vendor"},
                        before_vocab={"Axis Communications"},
                        after_vocab={"Axis Communications"}) == "minor"


def test_widened_vocabulary_proposes_minor():
    """Additive; existing values keep resolving as before."""
    d = {"fixed": [], "broken": [], "stable_correct": [], "stable_incorrect": []}
    assert propose_bump(d, before_keys={"vendor"}, after_keys={"vendor"},
                        before_vocab={"Axis"}, after_vocab={"Axis", "Ruckus"}) == "minor"


def test_narrowed_vocabulary_proposes_major():
    """§7.6: a value a consumer previously saw can no longer be emitted --
    same breakage class as a schema change, even though the schema is
    untouched."""
    d = {"fixed": [], "broken": [], "stable_correct": [], "stable_incorrect": []}
    assert propose_bump(d, before_keys={"vendor"}, after_keys={"vendor"},
                        before_vocab={"Axis", "Ruckus"}, after_vocab={"Axis"}) == "major"


def test_changed_claims_vocabulary_proposes_major():
    """Downstream consumers of the entity pool or membership.csv may break."""
    d = {"fixed": [], "broken": [], "stable_correct": [], "stable_incorrect": []}
    assert propose_bump(d, before_keys={"vendor", "model"}, after_keys={"vendor"},
                        before_vocab={"Axis"}, after_vocab={"Axis"}) == "major"


def test_diff_refuses_to_run_across_differing_label_hashes():
    """§7.6: if a label flips from Dahua to Hikvision between eval runs, the
    case lands in `broken` and reads as a rule regression when in fact the
    ground truth moved. The harness must refuse the single-axis diff."""
    with pytest.raises(LabelsMovedError):
        four_bucket_diff([], [], before_labels_hash="sha256:aaa",
                         after_labels_hash="sha256:bbb")


def test_evaluate_refuses_a_baseline_without_its_labels_hash(tmp_path):
    """The guard in four_bucket_diff only fires when BOTH hashes are known.
    Supplying a baseline and forgetting its hash would slip past it and
    misattribute a moved label as a rule regression."""
    from eval import evaluate
    with pytest.raises(LabelsMovedError, match="without baseline_labels_hash"):
        evaluate("obs-data/observations.csv", "rules", "labels/labels.csv",
                 tmp_path, baseline_outcomes=[_o("A", "vendor", True)])


def test_evaluate_refuses_before_writing_anything(tmp_path):
    """A refusal must not leave a half-written eval directory: manifest,
    metrics and outcomes on disk but no regressions.csv."""
    from eval import evaluate
    with pytest.raises(LabelsMovedError):
        evaluate("obs-data/observations.csv", "rules", "labels/labels.csv",
                 tmp_path, baseline_outcomes=[_o("A", "vendor", True)],
                 baseline_labels_hash="sha256:definitely-not-current")
    assert list(tmp_path.iterdir()) == []


def test_write_regressions_names_each_broken_label(tmp_path):
    """One row per flipped label, with the BEFORE value -- not the after
    repeated -- and correct quoting for values containing commas, which real
    model strings do."""
    from eval import write_regressions
    diff = {"fixed": [], "stable_correct": [], "stable_incorrect": [],
            "broken": [{"obs_id": "OBS-069", "key_type": "field",
                        "key": "model", "before_value": "FLEXIDOME IP, 8000i",
                        "actual": "NDE-8503-R"}]}
    path = tmp_path / "regressions.csv"
    write_regressions(path, diff, {"extraction.yaml", "scoring.yaml"})
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["obs_id"] == "OBS-069"
    assert rows[0]["before"] == "FLEXIDOME IP, 8000i"   # comma survives
    assert rows[0]["after"] == "NDE-8503-R"
    assert rows[0]["changed_rule_files"] == "extraction.yaml|scoring.yaml"


def test_evaluate_populates_the_baseline_fields_and_writes_the_diff(tmp_path):
    """The integration seam: evaluate with a matching baseline must write
    four_bucket.json, fill the manifest's baseline fields, and produce an
    empty regressions.csv when nothing broke."""
    from eval import evaluate
    from label_tools import labels_hash
    # `runs_root` is passed explicitly here for the reason given in
    # tests/test_eval.py: defaulting it writes into the real runs/ and
    # re-stamps the §6.2 version baseline.
    first = evaluate("obs-data/observations.csv", "rules", "labels/labels.csv",
                     tmp_path, runs_root=tmp_path / "runs")
    outcomes = json.loads((first / "outcomes.json").read_text())
    manifest = json.loads((first / "eval_manifest.json").read_text())
    second = evaluate("obs-data/observations.csv", "rules", "labels/labels.csv",
                      tmp_path, runs_root=tmp_path / "runs",
                      baseline_outcomes=outcomes,
                      baseline_labels_hash=labels_hash("labels/labels.csv"),
                      baseline_run_id=manifest["run_id"])
    buckets = json.loads((second / "four_bucket.json").read_text())
    assert buckets["broken"] == 0 and buckets["fixed"] == 0
    assert buckets["stable_correct"] + buckets["stable_incorrect"] == 276
    m2 = json.loads((second / "eval_manifest.json").read_text())
    assert m2["baseline_run_id"] == manifest["run_id"]
    assert m2["baseline_labels_hash"] is not None
    assert len((second / "regressions.csv").read_text().strip().splitlines()) == 1


def test_diff_permits_matching_label_hashes():
    d = four_bucket_diff([], [], before_labels_hash="sha256:aaa",
                         after_labels_hash="sha256:aaa")
    assert sum(len(v) for v in d.values()) == 0

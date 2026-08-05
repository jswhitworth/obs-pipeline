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


def test_diff_permits_matching_label_hashes():
    d = four_bucket_diff([], [], before_labels_hash="sha256:aaa",
                         after_labels_hash="sha256:aaa")
    assert sum(len(v) for v in d.values()) == 0

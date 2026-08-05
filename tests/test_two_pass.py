"""§7.6: when both rules and labels have changed, run two passes instead --
rules-held-constant (isolating the label delta) and labels-held-constant
(isolating the rule delta) -- so the two causes stay separable. Without this,
adjudication silently corrupts the regression signal."""
import pytest

from eval import LabelsMovedError, four_bucket_diff, two_pass_diff


def _o(obs_id, correct, actual):
    return {"obs_id": obs_id, "key_type": "field", "key": "vendor",
            "expected": "Hikvision", "actual": actual, "correct": correct,
            "provenance": "direct"}


def test_single_axis_diff_still_refuses_when_labels_moved():
    with pytest.raises(LabelsMovedError):
        four_bucket_diff([], [], before_labels_hash="sha256:a",
                         after_labels_hash="sha256:b")


def test_two_pass_separates_the_label_delta_from_the_rule_delta():
    baseline = [_o("OBS-011", True, "Hikvision")]
    # Rules held constant, labels moved: the label now says Dahua.
    rules_held = [_o("OBS-011", False, "Hikvision")]
    # Labels held constant, rules moved: the pipeline now emits Unknown.
    labels_held = [_o("OBS-011", False, "Unknown")]

    out = two_pass_diff(baseline_outcomes=baseline,
                        rules_held_outcomes=rules_held,
                        labels_held_outcomes=labels_held,
                        baseline_labels_hash="sha256:a",
                        current_labels_hash="sha256:b")
    assert len(out["label_delta"]["broken"]) == 1
    assert len(out["rule_delta"]["broken"]) == 1


def test_a_label_flip_is_not_attributed_to_the_rules():
    """A label flipping from Dahua to Hikvision must not read as a rule
    regression."""
    baseline = [_o("OBS-011", True, "Hikvision")]
    rules_held = [_o("OBS-011", False, "Hikvision")]   # only ground truth moved
    labels_held = [_o("OBS-011", True, "Hikvision")]   # rules unchanged in effect

    out = two_pass_diff(baseline_outcomes=baseline,
                        rules_held_outcomes=rules_held,
                        labels_held_outcomes=labels_held,
                        baseline_labels_hash="sha256:a",
                        current_labels_hash="sha256:b")
    assert out["rule_delta"]["broken"] == []
    assert len(out["label_delta"]["broken"]) == 1


def test_two_pass_is_unnecessary_when_labels_held_still():
    out = two_pass_diff(baseline_outcomes=[_o("OBS-011", True, "Hikvision")],
                        rules_held_outcomes=None,
                        labels_held_outcomes=[_o("OBS-011", False, "Unknown")],
                        baseline_labels_hash="sha256:a",
                        current_labels_hash="sha256:a")
    assert out["label_delta"] is None
    assert len(out["rule_delta"]["broken"]) == 1

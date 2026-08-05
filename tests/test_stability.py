"""§8.4 stability validation, unit-tested directly against `_accuracy_by_stability`
rather than only by eyeballing a live `eval.py` run.

Bucket boundaries, the 0.9 cap, exclusion of unscoreable rows, and both
confidence x stability quadrants are all exercised here with synthetic
outcomes -- no pipeline run required. Each group below uses a stability
value that lands in its own bucket, so bucket totals can be asserted exactly
with no cross-contamination between the boundary checks and the quadrant
checks.
"""
from eval import _accuracy_by_stability


def _o(correct, confidence, stability):
    # Keyed `field_confidence` to match score_against_labels's real outcome
    # shape: the quadrant predicates bucket on PER-FIELD confidence, not the
    # entity-level harmonic rollup (also present on a real outcome under
    # `confidence`, but not read here).
    return {"correct": correct, "field_confidence": confidence,
            "stability": stability}


def _by_scope(rows):
    return {r["scope"]: r for r in rows}


def test_accuracy_by_stability_buckets_and_quadrants():
    outcomes = [
        # bucket:0.0 -- stability in [0.0, 0.1)
        _o(True, 0.5, 0.05),
        _o(False, 0.5, 0.05),
        # bucket:0.3 -- an interior boundary, three rows
        _o(True, 0.5, 0.30),
        _o(True, 0.5, 0.35),
        _o(False, 0.5, 0.39),
        # bucket:0.9 cap -- 0.90 and 1.00 both fold into bucket:0.9 rather
        # than 1.00 spawning its own bucket:1.0
        _o(True, 0.5, 0.90),
        _o(True, 0.5, 1.00),
        # excluded entirely: an unscoreable row (undecidable/no label) must
        # not appear in any bucket's n or numerator
        _o(None, 0.9, 0.90),
        # bucket:0.7 rows, ALSO high_conf_high_stab (confidence >= 0.7 and
        # stability >= 0.7)
        _o(True, 0.8, 0.75),
        _o(False, 0.7, 0.70),
        # bucket:0.4, ALSO high_conf_low_stab (confidence >= 0.7 and
        # stability < 0.7)
        _o(True, 0.8, 0.40),
        # bucket:0.5, excluded from BOTH quadrants: confidence is below the
        # 0.7 floor even though stability alone would qualify
        _o(True, 0.2, 0.55),
    ]

    rows = _by_scope(_accuracy_by_stability(outcomes))

    # --- bucket boundaries ---------------------------------------------
    assert rows["bucket:0.0"]["n"] == 2
    assert rows["bucket:0.0"]["value"] == 0.5           # 1 of 2 correct

    assert rows["bucket:0.3"]["n"] == 3
    assert rows["bucket:0.3"]["value"] == round(2 / 3, 6)  # 2 of 3 correct

    # --- the 0.9 cap: 0.90 and 1.00 both land in bucket:0.9, never bucket:1.0
    assert "bucket:1.0" not in rows
    assert rows["bucket:0.9"]["n"] == 2
    assert rows["bucket:0.9"]["value"] == 1.0

    # --- correct is None is excluded from every bucket's n --------------
    total_bucketed_n = sum(v["n"] for k, v in rows.items()
                           if k.startswith("bucket:"))
    assert total_bucketed_n == 11   # 12 outcomes minus the 1 unscoreable row

    # --- quadrants --------------------------------------------------------
    assert rows["quadrant:high_conf_high_stab"]["n"] == 2
    assert rows["quadrant:high_conf_high_stab"]["value"] == 0.5   # 1 of 2

    assert rows["quadrant:high_conf_low_stab"]["n"] == 1
    assert rows["quadrant:high_conf_low_stab"]["value"] == 1.0

    # the low-confidence row (bucket:0.5) is counted in the bucket totals
    # above but must not leak into either quadrant
    assert rows["bucket:0.5"]["n"] == 1


def test_empty_quadrant_reports_zero_not_a_crash():
    """§8.4 -- on this dataset high_conf_low_stab is empty (n=0); the metric
    must still emit a row rather than raising ZeroDivisionError."""
    outcomes = [_o(True, 0.9, 0.9)]   # only high_conf_high_stab is populated
    rows = _by_scope(_accuracy_by_stability(outcomes))
    assert rows["quadrant:high_conf_low_stab"]["n"] == 0
    assert rows["quadrant:high_conf_low_stab"]["value"] == 0.0

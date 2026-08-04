# obs_pipeline/confidence.py
"""The confidence model (design doc §2.6).

Three different combining operations, deliberately not conflated:
  - witnesses of the SAME value  -> max + saturating bonus  (scoring.py)
  - a propagation CHAIN          -> multiplicative          (fields.py)
  - DIFFERENT fields into a record -> harmonic mean         (here)

Harmonic mean is dominated by its smallest input, which is the desired
behavior: a record is only as trustworthy as its weakest field. Same reasoning
that makes F1 a harmonic mean of precision and recall.
"""
from __future__ import annotations

from obs_pipeline.trace import Traced, Tracer


def harmonic_mean(values) -> float:
    vals = [v for v in values if v > 0]
    if not vals:
        return 0.0
    return len(vals) / sum(1.0 / v for v in vals)


def entity_confidence(resolved_fields, tracer: Tracer) -> Traced[float]:
    contributing = {
        name: f.confidence
        for name, f in sorted(resolved_fields.items())
        if f.confidence > 0
    }
    value = round(harmonic_mean(contributing.values()), 6)
    return tracer.step(
        op="resolve_entity",
        rule_id="scoring.yaml#confidence_rollup",
        output=value,
        parents=[f.traced for _, f in sorted(resolved_fields.items())],
        detail={"contributing_fields": contributing,
                "excluded_unknown": sorted(set(resolved_fields) - set(contributing))},
    )


def stability(resolved_fields, rules, tracer: Tracer) -> Traced[float]:
    """§2.6 -- how CONTESTED the answer is, which a mean cannot express.

    A result can be high-confidence and low-stability; that combination is the
    early-warning signal for values that will move under recalibration or one
    new observation.
    """
    w = rules.scoring["stability"]["weights"]
    margins, dependence, conflict = [], [], []

    for _, f in sorted(resolved_fields.items()):
        if f.confidence <= 0:
            continue
        margins.append(max(0.0, f.confidence - f.runner_up_weight) / f.confidence)
        # Witness dependence: a lone witness is one observation away from moving.
        dependence.append(1.0 if f.confidence >= 0.75 else f.confidence / 0.75)
        conflict.append(0.0 if f.runner_up else 1.0)

    if not margins:
        value = 0.0
    else:
        value = (
            w["margin"] * (sum(margins) / len(margins))
            + w["witness_dependence"] * (sum(dependence) / len(dependence))
            + w["live_conflict"] * (sum(conflict) / len(conflict))
        )
    value = round(max(0.0, min(1.0, value)), 6)
    return tracer.step(
        op="stability",
        rule_id="scoring.yaml#stability",
        output=value,
        parents=[f.traced for _, f in sorted(resolved_fields.items())],
        detail={"weights": dict(w)},
    )

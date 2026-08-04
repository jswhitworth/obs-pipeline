"""Rule category 4 -- THE shared scoring function (design doc §2.3).

    independence_bonus(k) = b * (1 - r^(k-1))
    claim_weight = clamp(max_base + independence_bonus - conflict_penalty, 0, 1)

This function is used by BOTH claim construction (§2.3) and entity resolution
(§2.4). Invariant #3: the formula shape is identical for both; only the
coefficients differ, to reflect their different error blast-radii. Do not
write a second implementation -- import this one.
"""
from __future__ import annotations

from dataclasses import dataclass

from obs_pipeline.trace import Traced, Tracer


@dataclass(frozen=True)
class Coefficients:
    b: float
    r: float
    conflict_penalty_per_group: float
    max_conflict_penalty: float
    name: str

    @classmethod
    def from_rules(cls, rules, kind: str) -> "Coefficients":
        cfg = rules.scoring[kind]
        return cls(
            b=float(cfg["b"]),
            r=float(cfg["r"]),
            conflict_penalty_per_group=float(cfg["conflict_penalty_per_group"]),
            max_conflict_penalty=float(cfg["max_conflict_penalty"]),
            name=kind,
        )


def independence_bonus(k: int, b: float, r: float) -> float:
    """Saturating, not linear. Caps what corroboration alone can buy."""
    if k < 2:
        return 0.0
    return b * (1.0 - r ** (k - 1))


def score(
    *,
    key: str,
    value: str,
    witness_groups,
    base_weights: dict[str, float],
    conflicting_groups,
    coeff: Coefficients,
    tracer: Tracer,
    rule_id: str,
) -> Traced[float]:
    groups = sorted(set(witness_groups))
    conflicts = sorted(set(conflicting_groups))

    base_from, base_max = None, 0.0
    for g in groups:
        w = float(base_weights.get(g, 0.0))
        if w > base_max or (w == base_max and base_from is None):
            base_from, base_max = g, w

    bonus = independence_bonus(len(groups), coeff.b, coeff.r)
    penalty = min(
        coeff.max_conflict_penalty,
        coeff.conflict_penalty_per_group * len(conflicts),
    )
    weight = max(0.0, min(1.0, base_max + bonus - penalty))

    return tracer.step(
        op="score",
        rule_id=rule_id,
        output=round(weight, 6),
        key=key,
        value=value,
        decomposition={
            "base_max": base_max,
            "base_from": base_from,
            "bonus": round(bonus, 6),
            "witness_groups": groups,
            "penalty": round(penalty, 6),
            "conflicting_groups": conflicts,
            "coefficient_set": coeff.name,
        },
    )

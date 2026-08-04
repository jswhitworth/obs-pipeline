# obs_pipeline/claims.py
"""Rule category 3 -- claim construction (design doc §2.3).

Claims are keyed by (obs_id, key, value). `source` is metadata carried ON the
claim, never folded into the key (invariant #2), so multiple sources can
collide on the same key and either corroborate or contradict as intended.

Claims are NOT vocabulary-constrained; only resolved output is (§6.3). An
out-of-vocab value is kept and flagged, because rejecting it here would
discard the evidence that the vocabulary is incomplete.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from obs_pipeline.extract import extract_observation
from obs_pipeline.scoring import Coefficients, score
from obs_pipeline.trace import Traced, Tracer


@dataclass(frozen=True)
class Claim:
    obs_id: str
    kind: str            # "field" | "link_basis"
    key: str
    value: str
    weight: float
    witness_groups: tuple[str, ...]
    sources: tuple[str, ...]
    traced: Traced
    in_vocab: bool


def field_claims(claims):
    return [c for c in claims if c.kind == "field"]


def identity_claims(claims):
    return [c for c in claims if c.kind == "link_basis"]


def _in_vocab(kind, key, value, rules) -> bool:
    closed = rules.claims.get("closed_vocabulary_fields", {})
    if kind != "field" or key not in closed:
        return True
    column = closed[key]["vocab_column"]
    pool = rules.vocab.vendors if column == "vendor" else rules.vocab.device_types
    return value in pool


def build_claims(observations, rules, tracer: Tracer) -> list[Claim]:
    coeff = {
        "field": Coefficients.from_rules(rules, "field_claims"),
        "link_basis": Coefficients.from_rules(rules, "identity_claims"),
    }
    base_weights = rules.scoring["base_weights"]
    out: list[Claim] = []

    for obs in observations:
        obs_id = obs["obs_id"]
        grouped: dict[tuple[str, str, str], dict] = defaultdict(
            lambda: {"groups": set(), "sources": set(), "parents": []}
        )
        for e in extract_observation(obs, rules, tracer):
            slot = grouped[(e.target_kind, e.target, e.value)]
            slot["groups"].add(e.witness_group)
            slot["sources"].add(e.source)
            slot["parents"].append(e.traced)

        by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
        for (kind, key, value), slot in grouped.items():
            by_key[(kind, key)] |= slot["groups"]

        for (kind, key, value), slot in sorted(grouped.items()):
            # Conflicting = groups on this same key asserting a DIFFERENT value.
            conflicting = by_key[(kind, key)] - slot["groups"]
            traced = score(
                key=key,
                value=value,
                witness_groups=sorted(slot["groups"]),
                base_weights=base_weights,
                conflicting_groups=sorted(conflicting),
                coeff=coeff[kind],
                tracer=tracer,
                rule_id=f"scoring.yaml#{coeff[kind].name}",
            )
            out.append(Claim(
                obs_id=obs_id,
                kind=kind,
                key=key,
                value=value,
                weight=traced.value,
                witness_groups=tuple(sorted(slot["groups"])),
                sources=tuple(sorted(slot["sources"])),
                traced=traced,
                in_vocab=_in_vocab(kind, key, value, rules),
            ))

        if not any(k == "link_basis" for (k, _, _) in grouped):
            tracer.step(op="no_identity_claim", rule_id="claims.yaml#link_bases",
                        inputs=[f"obs:{obs_id}"], output=None)

    return sorted(out, key=lambda c: (c.obs_id, c.kind, c.key, c.value))

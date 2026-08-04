# obs_pipeline/fields.py
"""Rule category 7 -- field resolution (design doc §2.5).

Runs on field claims plus PERSISTED membership. Sibling propagation decays
multiplicatively because a chain is conjunctive: every link must hold
independently, so a low-confidence link discounts appropriately and a
multi-hop path does not inherit full strength from one strong link.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from obs_pipeline.claims import field_claims
from obs_pipeline.trace import Traced, Tracer

UNDECIDABLE = "undecidable"


def absent_values(rules) -> frozenset[str]:
    """The absence markers, derived from the rules rather than hardcoded.

    claims.yaml#closed_vocabulary_fields declares each field's `escape`
    value; a copy of those literals here would silently diverge the moment
    a rules-legal edit changes one (e.g. `escape: Unknown` -> `UNKNOWN`),
    which would flip that field's absence rows into looking like inherited
    evidence -- exactly the failure §2.5 names. The empty string is added
    separately: it is the open-vocabulary (model/firmware) absence marker
    and is not itself rules-declared.

    UNDECIDABLE is deliberately NOT among them: absence means nothing was
    witnessed, while undecidable arises because two things were witnessed
    and disagreed.
    """
    closed = rules.claims.get("closed_vocabulary_fields", {})
    return frozenset({""} | {cfg["escape"] for cfg in closed.values()})


@dataclass(frozen=True)
class ResolvedField:
    """Entity-level: the winning claim among all members (§2.5)."""
    entity_id: str
    field: str
    value: str
    confidence: float
    runner_up: str | None
    runner_up_weight: float
    witness_groups: tuple[str, ...]
    traced: Traced


@dataclass(frozen=True)
class ObsField:
    """Observation-level: did THIS payload witness the value, or inherit it
    from a sibling (§3.1)? Decay applies here, because this is the only level
    at which a hop actually occurs."""
    obs_id: str
    field: str
    value: str
    confidence: float
    provenance: str          # "direct" | "propagated" | "unknown"
    traced: Traced


def _escape(field: str, rules) -> str:
    return rules.claims["closed_vocabulary_fields"][field]["escape"]


def resolve_fields(claims, memberships, rules, tracer: Tracer):
    cfg = rules.field_resolution
    closed = rules.claims.get("closed_vocabulary_fields", {})
    per_field = cfg["conflict_policy"].get("per_field", {})

    members: dict[str, list[str]] = defaultdict(list)
    for m in memberships:
        members[m.entity_id].append(m.obs_id)

    by_obs: dict[str, list] = defaultdict(list)
    for c in field_claims(claims):
        by_obs[c.obs_id].append(c)

    out: dict[str, dict[str, ResolvedField]] = {}

    for entity_id in sorted(members):
        obs_list = sorted(members[entity_id])
        resolved: dict[str, ResolvedField] = {}

        for field in sorted(rules.claims["fields"]):
            candidates = []
            for oid in obs_list:
                for c in by_obs[oid]:
                    if c.key != field:
                        continue
                    if not c.in_vocab:
                        tracer.step(op="vocab_reject",
                                    rule_id="claims.yaml#closed_vocabulary_fields",
                                    inputs=[f"obs:{oid}"], output=c.value,
                                    parents=[c.traced], field=field)
                        continue
                    candidates.append((c, oid))

            if not candidates:
                value = _escape(field, rules) if field in closed else ""
                traced = tracer.step(op="resolve_field",
                                     rule_id="field_resolution.yaml#unknown_handling",
                                     inputs=[f"entity:{entity_id}"], output=value,
                                     field=field, confidence=0.0)
                resolved[field] = ResolvedField(entity_id, field, value, 0.0,
                                                None, 0.0, (), traced)
                continue

            distinct = {c.value for c, _ in candidates}

            # §2.5 -- firmware is temporal and the data model is atemporal.
            if len(distinct) > 1 and per_field.get(field) == UNDECIDABLE:
                traced = tracer.step(
                    op="resolve_field",
                    rule_id="field_resolution.yaml#conflict_policy.per_field",
                    inputs=[f"entity:{entity_id}"], output=UNDECIDABLE,
                    field=field, confidence=0.0,
                    parents=[c.traced for c, _ in candidates],
                    detail={"reason": "temporal_field_disagreement",
                            "values": sorted(distinct)},
                )
                resolved[field] = ResolvedField(entity_id, field, UNDECIDABLE,
                                                0.0, None, 0.0, (), traced)
                continue

            # §2.5 -- highest claim_weight wins. No decay here: the winner is a
            # direct reading by SOME member, so at entity level hop is 0.
            ranked = sorted(candidates, key=lambda ci: (-ci[0].weight, ci[0].value))
            best_claim, _best_obs = ranked[0]
            runner = next((c for c, _ in ranked if c.value != best_claim.value), None)

            traced = tracer.step(
                op="resolve_field",
                rule_id="field_resolution.yaml#conflict_policy",
                inputs=[f"entity:{entity_id}"], output=best_claim.value,
                parents=[best_claim.traced],
                field=field, confidence=round(best_claim.weight, 6),
                detail={"runner_up": runner.value if runner else None,
                        "runner_up_weight": round(runner.weight, 6) if runner else 0.0},
            )
            resolved[field] = ResolvedField(
                entity_id, field, best_claim.value, round(best_claim.weight, 6),
                runner.value if runner else None,
                round(runner.weight, 6) if runner else 0.0,
                tuple(best_claim.witness_groups), traced,
            )
        out[entity_id] = resolved
    return out


def observation_fields(claims, memberships, resolved, rules, tracer: Tracer):
    """§3.1 -- the per-observation view.

    Did THIS payload witness the value, or inherit it by being clustered with
    a sibling that did? Diffing a propagated row naively against labels would
    credit the pipeline for extraction it never performed, so the eval harness
    computes Stage 1/2 accuracy over `direct` rows and Stage 4 over
    `propagated` rows. That split is only possible if provenance is recorded
    here, per observation, rather than once per entity.
    """
    cfg = rules.field_resolution
    decay_base = float(cfg["decay_base"])
    # UNDECIDABLE is deliberately NOT in this set. It is not an absence
    # marker: it only arises when candidates exist and disagree, so the
    # individual readings are real, in-vocab, directly-witnessed evidence.
    # §2.5 -- "the ambiguity exists at the entity level only... no observation
    # is scored wrong for reporting what it actually saw."
    absent = absent_values(rules)

    weight_of = {m.obs_id: m.link_weight for m in memberships}
    entity_of = {m.obs_id: m.entity_id for m in memberships}

    own: dict[tuple[str, str], list] = defaultdict(list)
    for c in field_claims(claims):
        if c.in_vocab:
            own[(c.obs_id, c.key)].append(c)

    out: dict[str, dict[str, ObsField]] = {}
    for obs_id in sorted(entity_of):
        fields: dict[str, ObsField] = {}
        for field in sorted(rules.claims["fields"]):
            winner = resolved[entity_of[obs_id]][field]

            if winner.value in absent:
                # §2.5 -- Unknown is an absence marker, not a value to spread.
                traced = tracer.step(op="observation_field",
                                     rule_id="field_resolution.yaml#unknown_handling",
                                     inputs=[f"obs:{obs_id}"], output=winner.value,
                                     parents=[winner.traced], field=field,
                                     provenance="unknown", confidence=0.0)
                fields[field] = ObsField(obs_id, field, winner.value, 0.0,
                                         "unknown", traced)
                continue

            if winner.value == UNDECIDABLE:
                # The entity cannot pick a value, but THIS observation saw
                # something specific and it was true when taken. Report it.
                # Collapsing it to the entity marker would score an
                # observation wrong for reporting what it actually witnessed.
                seen = own[(obs_id, field)]
                if seen:
                    best = max(seen, key=lambda c: (c.weight, c.value))
                    traced = tracer.step(
                        op="observation_field",
                        rule_id="field_resolution.yaml#conflict_policy.per_field",
                        inputs=[f"obs:{obs_id}"], output=best.value,
                        parents=[best.traced], field=field, provenance="direct",
                        confidence=round(best.weight, 6),
                        detail={"entity_value": UNDECIDABLE})
                    fields[field] = ObsField(obs_id, field, best.value,
                                             round(best.weight, 6), "direct", traced)
                else:
                    traced = tracer.step(
                        op="observation_field",
                        rule_id="field_resolution.yaml#conflict_policy.per_field",
                        inputs=[f"obs:{obs_id}"], output=UNDECIDABLE,
                        parents=[winner.traced], field=field,
                        provenance="unknown", confidence=0.0)
                    fields[field] = ObsField(obs_id, field, UNDECIDABLE, 0.0,
                                             "unknown", traced)
                continue

            mine = [c for c in own[(obs_id, field)] if c.value == winner.value]
            if mine:
                best = max(mine, key=lambda c: c.weight)
                traced = tracer.step(op="observation_field",
                                     rule_id="field_resolution.yaml#conflict_policy",
                                     inputs=[f"obs:{obs_id}"], output=winner.value,
                                     parents=[best.traced], field=field,
                                     provenance="direct",
                                     confidence=round(best.weight, 6))
                fields[field] = ObsField(obs_id, field, winner.value,
                                         round(best.weight, 6), "direct", traced)
                continue

            # Inherited from a sibling: conjunctive chain, so multiply (§2.5).
            hop = 1
            conf = round(winner.confidence * max(weight_of.get(obs_id, 0.0), 0.0)
                         * (decay_base ** hop), 6)
            traced = tracer.step(op="propagate",
                                 rule_id="field_resolution.yaml#decay_base",
                                 inputs=[f"obs:{obs_id}"], output=winner.value,
                                 parents=[winner.traced], field=field,
                                 provenance="propagated", confidence=conf,
                                 detail={"hop": hop,
                                         "link_weight": weight_of.get(obs_id, 0.0),
                                         "source_confidence": winner.confidence})
            fields[field] = ObsField(obs_id, field, winner.value, conf,
                                     "propagated", traced)
        out[obs_id] = fields
    return out

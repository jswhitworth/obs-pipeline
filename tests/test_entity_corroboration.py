# tests/test_entity_corroboration.py
"""§2.3/§2.5 -- cross-observation corroboration, priced at field resolution.

FINDINGS.md §2 recorded the contradiction this resolves: claims are keyed by
obs_id, so the claim-time independence_bonus never saw agreement ACROSS an
entity's members, while §2.6 said "agreement must raise confidence" with no
scoping caveat. The amended design prices corroboration at exactly two
scopes: within an observation at claim time, across members at field
resolution -- a re-score of the winning value over the UNION of distinct
witness groups, under the entity_corroboration coefficient set.

Expected numbers below are derived by hand from scoring.yaml's seeded
coefficients (b=0.30, r=0.50, penalty 0.10/group) and the real witness-group
topology of the five multi-member entities. If a test fails on one of these
expectations, report it -- do not adjust the number to match the code.
"""
import shutil

import pytest

from obs_pipeline.claims import build_claims
from obs_pipeline.entity import resolve_entities
from obs_pipeline.extract import load_observations
from obs_pipeline.fields import resolve_fields
from obs_pipeline.loader import CrossFileError, load_rules
from obs_pipeline.trace import Tracer

RULES = load_rules("rules", "obs-data/observations.csv")
OBSERVATIONS = load_observations("obs-data/observations.csv")
TRACER = Tracer()
CLAIMS = build_claims(OBSERVATIONS, RULES, TRACER)
MEMBERSHIPS = resolve_entities(CLAIMS, OBSERVATIONS, RULES, TRACER)
RESOLVED = resolve_fields(CLAIMS, MEMBERSHIPS, RULES, TRACER)


def _entity_with(obs_id):
    return next(m.entity_id for m in MEMBERSHIPS if m.obs_id == obs_id)


def test_cross_observation_agreement_fires_the_bonus():
    """E-001 model: OBS-001 witnesses P3245-LVE via onvif (0.85), OBS-003 via
    mdns (0.65). Pooled groups {onvif, mdns} -> k=2 -> bonus 0.30*(1-0.5) =
    0.15 -> 0.85 + 0.15 = 1.0. Before pooling this was 0.85: three sources
    agreeing raised confidence nowhere."""
    e = RESOLVED[_entity_with("OBS-001")]
    assert e["model"].confidence == 1.0


def test_agreement_through_a_second_protocol_counts_for_firmware_too():
    """E-001 firmware: onvif (0.85) + http (0.55) both read 10.12.114.
    k=2 -> 0.85 + 0.15 = 1.0."""
    e = RESOLVED[_entity_with("OBS-001")]
    assert e["firmware"].confidence == 1.0


def test_same_group_reobserved_pools_to_one_witness_and_no_bonus():
    """E-001 device_type: ip_camera is witnessed by port_signature on ALL
    THREE members. The union is {port_signature} -- k=1, no bonus. The same
    underlying signal observed three times is not independent agreement;
    this is the guard against an implementation that counts CLAIMS instead
    of distinct groups. (Green before the change and green after -- its value
    is as a counterexample, not as a red test.)"""
    e = RESOLVED[_entity_with("OBS-001")]
    assert e["device_type"].confidence == 0.25
    assert e["device_type"].witness_groups == ("port_signature",)


def test_pooled_union_is_recorded_on_the_resolved_field():
    """The pooled group set replaces the winning claim's own set on
    ResolvedField, so stability's witness_dependence counts the same union
    that priced the corroboration (§2.6, amended)."""
    e = RESOLVED[_entity_with("OBS-001")]
    assert e["model"].witness_groups == ("mdns", "onvif")
    assert e["firmware"].witness_groups == ("http", "onvif")


def test_single_claim_value_rescores_to_exactly_the_claim_weight():
    """E-052 model: only OBS-047 witnesses P3245-LVE (onvif). The pooled set
    equals the claim's own, the coefficient sets are seeded identical, so the
    re-score must reproduce 0.85 exactly -- corroboration pricing is a no-op
    wherever there is nothing to corroborate. This is what keeps all 63
    singleton entities byte-identical."""
    e = RESOLVED[_entity_with("OBS-047")]
    assert e["model"].confidence == 0.85
    assert e["model"].witness_groups == ("onvif",)


def test_saturation_still_clamps_at_one():
    """E-001 vendor was already clamped: onvif+oui within OBS-001 alone
    reaches 1.0. Pooling {onvif, http, mdns, oui} must not push past the
    clamp or below it."""
    e = RESOLVED[_entity_with("OBS-001")]
    assert e["vendor"].confidence == 1.0


def test_cross_member_disagreement_prices_into_the_winner():
    """E-074 model: OBS-069 says 'FLEXIDOME IP starlight 8000i NDE-8503-R'
    (onvif 0.85), OBS-074 says 'NDE-8503-R' (snmp 0.70). Winner selection is
    untouched -- highest single claim wins -- but the winner's confidence now
    carries the entity-scoped conflict: 0.85 + 0 bonus - 0.10 = 0.75. At
    claim time OBS-069 saw no model rival, so this contention was previously
    invisible to confidence and lived only in runner_up/stability."""
    e = RESOLVED[_entity_with("OBS-069")]
    assert e["model"].value == "FLEXIDOME IP starlight 8000i NDE-8503-R"
    assert e["model"].confidence == 0.75


def test_out_of_vocab_claims_still_conflict_at_entity_scope():
    """E-002 vendor: both members witness Hanwha Vision via oui -- the SAME
    group, k=1, no bonus, base 0.50. But wisenet (snmp, OBS-004) and apache
    (http, OBS-005) contest the key: claims are not vocabulary-constrained
    (§2.3), so both conflict exactly as they did at claim time.
    0.50 - 2*0.10 = 0.30."""
    e = RESOLVED[_entity_with("OBS-004")]
    assert e["vendor"].value == "Hanwha Vision"
    assert e["vendor"].confidence == 0.30


def test_rescore_is_a_traced_step_distinct_from_claim_scores():
    """replay.py reconstructs claims.csv from every op=='score' step
    (replay.py:87), so the entity-level re-score must carry its own op or it
    becomes a phantom claim in the replay diff."""
    steps = [s for s in TRACER.steps() if s["op"] == "score_entity"]
    assert steps, "entity re-score emitted no trace step"
    assert all(s["rule_id"] == "field_resolution.yaml#entity_corroboration"
               for s in steps)
    # The resolve_field step's confidence and the re-score output must agree
    # for the winning value, otherwise replay reconstructs one number while
    # the bundle carries another.
    e = RESOLVED[_entity_with("OBS-001")]
    outputs = {s["output"] for s in steps
               if s.get("key") == "model" and s.get("value") == "P3245-LVE"}
    assert e["model"].confidence in outputs


def test_missing_entity_corroboration_coefficients_fail_at_load(tmp_path):
    """§6.1 -- a cross-file reference gets a load-time check. fields.py reads
    scoring.yaml#entity_corroboration; without this guard, deleting the block
    would surface as a KeyError three stages into a run instead of a
    CrossFileError at init."""
    d = tmp_path / "rules"
    shutil.copytree("rules", d)
    scoring = d / "scoring.yaml"
    text = scoring.read_text()
    assert "entity_corroboration" in text, (
        "rules/scoring.yaml no longer declares entity_corroboration -- "
        "this test needs updating alongside that change")
    head, _, _ = text.partition("entity_corroboration:")
    scoring.write_text(head)
    with pytest.raises(CrossFileError, match="entity_corroboration"):
        load_rules(d, "obs-data/observations.csv")

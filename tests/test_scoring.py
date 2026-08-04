import inspect

import pytest

from obs_pipeline.loader import load_rules
from obs_pipeline.scoring import Coefficients, independence_bonus, score
from obs_pipeline.trace import Tracer

RULES = load_rules("rules", "obs-data/observations.csv")
FIELD = Coefficients.from_rules(RULES, "field_claims")
IDENT = Coefficients.from_rules(RULES, "identity_claims")


def _score(groups, base, conflicts, coeff):
    return score(key="vendor", value="Hikvision", witness_groups=groups,
                 base_weights=base, conflicting_groups=conflicts,
                 coeff=coeff, tracer=Tracer(), rule_id="scoring.yaml#test").value


def test_single_witness_earns_no_bonus():
    assert independence_bonus(1, b=0.3, r=0.5) == 0.0


def test_bonus_saturates_rather_than_growing_linearly():
    """§2.3: b=0.3, r=0.5 -> +0.15, +0.225, +0.2625, converging on b."""
    b, r = 0.3, 0.5
    assert independence_bonus(2, b, r) == pytest.approx(0.15)
    assert independence_bonus(3, b, r) == pytest.approx(0.225)
    assert independence_bonus(4, b, r) == pytest.approx(0.2625)
    assert independence_bonus(50, b, r) < b


def test_corroboration_can_never_outrank_by_more_than_b():
    """Linear accumulation would let several mediocre sources outrank one
    authoritative source without bound."""
    many_weak = _score(["a", "b", "c", "d", "e"], {"a": 0.3, "b": 0.3, "c": 0.3,
                                                   "d": 0.3, "e": 0.3}, [], FIELD)
    assert many_weak < 0.3 + FIELD.b + 1e-9


def test_weight_uses_max_base_not_mean():
    """Averaging in a weak agreeing witness would LOWER confidence in a value
    that just gained support (§2.3)."""
    strong_alone = _score(["onvif"], {"onvif": 0.85}, [], FIELD)
    strong_plus_weak = _score(["onvif", "http"], {"onvif": 0.85, "http": 0.55}, [], FIELD)
    assert strong_plus_weak > strong_alone


def test_conflict_lowers_the_weight():
    clean = _score(["onvif"], {"onvif": 0.85}, [], FIELD)
    contested = _score(["onvif"], {"onvif": 0.85}, ["http"], FIELD)
    assert contested < clean


def test_identity_coefficients_penalize_conflict_harder_than_field():
    """§2.3 blast radius: a false merge multiplies across every field on every
    member, so identity resolution must refuse contested merges."""
    base, groups, conflict = {"onvif": 0.85}, ["onvif"], ["http"]
    field_drop = _score(groups, base, [], FIELD) - _score(groups, base, conflict, FIELD)
    ident_drop = _score(groups, base, [], IDENT) - _score(groups, base, conflict, IDENT)
    assert ident_drop > field_drop
    assert IDENT.b < FIELD.b


def test_weight_is_clamped_to_unit_interval():
    assert _score(["a"], {"a": 0.99}, [], FIELD) <= 1.0
    assert _score(["a"], {"a": 0.10}, ["b", "c", "d", "e"], IDENT) >= 0.0


def test_formula_lives_in_exactly_one_function():
    """Invariant #3: scoring parity is structural. If a second implementation
    of the formula appears, this test is the tripwire."""
    src = inspect.getsource(score)
    assert "max(" in src and "independence_bonus(" in src
    import obs_pipeline.claims as claims_mod
    import obs_pipeline.entity as entity_mod
    for mod in (claims_mod, entity_mod):
        text = inspect.getsource(mod)
        assert "1 - " not in text.replace("1 - r", ""), \
            f"{mod.__name__} appears to reimplement the bonus formula"


def test_score_links_back_to_the_evidence_it_scored():
    """§9.1 Q1: 'which extraction rule fired, on which substring of which
    raw_payload?' is only answerable if the score step names its evidence.
    Without parents the chain claim -> extraction -> payload span is broken
    and a claim can only be matched to its origin by guessing."""
    t = Tracer()
    ev = t.step(op="extract", rule_id="extraction.yaml#x", output="Hikvision")
    out = score(key="vendor", value="Hikvision", witness_groups=["onvif"],
                base_weights={"onvif": 0.85}, conflicting_groups=[],
                coeff=FIELD, tracer=t, rule_id="scoring.yaml#field_claims",
                parents=[ev])
    row = next(s for s in t.steps() if s["step_id"] == out.step_id)
    assert row["parents"] == [ev.step_id]


def test_score_emits_a_decomposition_step():
    """§9.1 Q3: which source supplied the max base, which groups earned the
    bonus, which conflicts caused the penalty."""
    t = Tracer()
    out = score(key="vendor", value="Hikvision",
                witness_groups=["onvif", "http"],
                base_weights={"onvif": 0.85, "http": 0.55},
                conflicting_groups=["snmp"], coeff=FIELD, tracer=t,
                rule_id="scoring.yaml#field_claims")
    row = next(s for s in t.steps() if s["step_id"] == out.step_id)
    d = row["decomposition"]
    assert d["base_max"] == 0.85
    assert d["base_from"] == "onvif"
    assert sorted(d["witness_groups"]) == ["http", "onvif"]
    assert d["penalty"] > 0

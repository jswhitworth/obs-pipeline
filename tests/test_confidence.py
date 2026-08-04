# tests/test_confidence.py
import pytest

from obs_pipeline.confidence import entity_confidence, harmonic_mean, stability
from obs_pipeline.fields import ResolvedField
from obs_pipeline.loader import load_rules
from obs_pipeline.trace import Tracer

RULES = load_rules("rules", "obs-data/observations.csv")


def _field(name, value, conf, runner=None, runner_w=0.0):
    """ResolvedField is ENTITY-level and carries no provenance — that lives on
    ObsField, per the two-level split in §2.5/§3.1."""
    t = Tracer()
    return ResolvedField("E-x", name, value, conf, runner, runner_w,
                         t.step(op="resolve_field", output=value))


def test_harmonic_mean_is_dominated_by_its_smallest_input():
    """§2.6: vendor 0.95, model 0.90, device_type 0.30 should NOT read as a
    solid record. Arithmetic mean says 0.72; harmonic says ~0.55 and states
    plainly that something in it is weak."""
    assert harmonic_mean([0.95, 0.90, 0.30]) == pytest.approx(0.55, abs=0.02)
    assert harmonic_mean([0.95, 0.90, 0.30]) < sum([0.95, 0.90, 0.30]) / 3


def test_unknown_fields_are_excluded_not_zeroing_the_rollup():
    """§2.6: fields resolving to Unknown (0.0) are excluded rather than
    zeroing the mean; they are visible through known_rate instead."""
    fields = {
        "vendor": _field("vendor", "Hikvision", 0.9),
        "model": _field("model", "DS-2CD2143G0-I", 0.8),
        "device_type": _field("device_type", "unknown", 0.0),
        "firmware": _field("firmware", "", 0.0),
    }
    out = entity_confidence(fields, Tracer()).value
    assert out == pytest.approx(harmonic_mean([0.9, 0.8]))
    assert out > 0.0


def test_entity_with_no_known_fields_has_zero_confidence():
    fields = {"vendor": _field("vendor", "Unknown", 0.0)}
    assert entity_confidence(fields, Tracer()).value == 0.0


def test_stability_separates_settled_from_knife_edge_results():
    """§2.6: two sources at 0.8 backing Hikvision yield the same CONFIDENCE
    whether the runner-up sat at 0.1 or 0.79 -- magnitude alone cannot tell a
    settled result from a knife-edge one."""
    settled = {"vendor": _field("vendor", "Hikvision", 0.8, "Dahua Technology", 0.10)}
    knife = {"vendor": _field("vendor", "Hikvision", 0.8, "Dahua Technology", 0.79)}
    assert entity_confidence(settled, Tracer()).value == \
           entity_confidence(knife, Tracer()).value
    assert stability(settled, RULES, Tracer()).value > \
           stability(knife, RULES, Tracer()).value


def test_stability_is_bounded_to_unit_interval():
    for runner_w in (0.0, 0.4, 0.79, 0.8):
        s = stability({"vendor": _field("vendor", "X", 0.8, "Y", runner_w)},
                      RULES, Tracer()).value
        assert 0.0 <= s <= 1.0

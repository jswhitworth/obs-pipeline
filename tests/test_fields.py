# tests/test_fields.py
from obs_pipeline.claims import build_claims
from obs_pipeline.entity import partition, resolve_entities
from obs_pipeline.extract import load_observations
from obs_pipeline.fields import UNDECIDABLE, observation_fields, resolve_fields
from obs_pipeline.loader import load_rules
from obs_pipeline.trace import Tracer

RULES = load_rules("rules", "obs-data/observations.csv")
OBSERVATIONS = load_observations("obs-data/observations.csv")
TRACER = Tracer()
CLAIMS = build_claims(OBSERVATIONS, RULES, TRACER)
MEMBERSHIPS = resolve_entities(CLAIMS, OBSERVATIONS, RULES, TRACER)
PARTITION = partition(MEMBERSHIPS)
RESOLVED = resolve_fields(CLAIMS, MEMBERSHIPS, RULES, TRACER)


def _entity_with(obs_id):
    return next(m.entity_id for m in MEMBERSHIPS if m.obs_id == obs_id)


def test_clean_entity_resolves_all_four_fields():
    e = RESOLVED[_entity_with("OBS-001")]
    assert e["vendor"].value == "Axis Communications"
    assert e["model"].value == "P3245-LVE"
    assert e["device_type"].value == "ip_camera"


def test_firmware_conflict_within_entity_is_undecidable():
    """§2.5, the stated exception. OBS-069 reports 8.10.0135 and OBS-074
    reports 8.11.0021 -- the device was upgraded between scans and BOTH
    readings were true when taken. Highest-weight-wins would emit a
    confidently single-valued answer to something multi-valued over time."""
    e = RESOLVED[_entity_with("OBS-069")]
    assert e["firmware"].value == UNDECIDABLE


def test_agreeing_firmware_still_resolves_normally():
    e = RESOLVED[_entity_with("OBS-001")]
    assert e["firmware"].value == "10.12.114"


def test_out_of_vocab_vendor_resolves_to_the_escape_value():
    """§2.5 VOCAB GAP: a value extracted and normalized cleanly, but absent
    from the vocabulary. OBS-033 is the only such case in this data -- its
    banner yields `microsoft-httpapi`, which rejects to `Unknown`.

    Note OBS-036 is NOT a vocab-gap case despite carrying `amcrest`: its
    3C:EF:8C OUI supplies an in-vocab `Dahua Technology` that wins, which
    happens to match the label. Out-of-vocab claims losing to an in-vocab
    rival is the design working, not a rejection."""
    e = RESOLVED[_entity_with("OBS-033")]
    assert e["vendor"].value == "Unknown"


def test_no_evidence_and_vocab_gap_both_emit_the_escape_value():
    """§2.5: two distinct situations collapse to the same emitted value but
    stay separable in the trace. OBS-042 has no vendor evidence at all;
    OBS-033 had a value and it was rejected. Only the second is actionable."""
    assert RESOLVED[_entity_with("OBS-042")]["vendor"].value == "Unknown"
    assert RESOLVED[_entity_with("OBS-033")]["vendor"].value == "Unknown"
    rejected_for = {s["output"] for s in TRACER.steps()
                    if s["op"] == "vocab_reject" and s.get("field") == "vendor"}
    assert "microsoft-httpapi" in rejected_for


def test_vocab_reject_is_a_trace_step_not_a_silence():
    """§9.3: without this step the rejected value vanishes and the gap is
    never learned."""
    rejects = [s for s in TRACER.steps() if s["op"] == "vocab_reject"]
    assert rejects
    # Lowercase: normalization canonicalises unmapped surface strings to the
    # alias-map key form, which is the key a human pastes into the rules.
    assert "amcrest" in {s["output"] for s in rejects}


def test_unknown_confidence_is_exactly_zero():
    """§2.5: a non-zero confidence on an absence marker is not interpretable."""
    e = RESOLVED[_entity_with("OBS-033")]
    assert e["vendor"].confidence == 0.0


def test_escape_values_keep_their_per_column_casing():
    e = RESOLVED[_entity_with("OBS-043")]      # telnet, no usable evidence
    assert e["vendor"].value == "Unknown"
    assert e["device_type"].value == "unknown"


def test_closed_vocabulary_fields_are_never_null():
    for fields in RESOLVED.values():
        assert fields["vendor"].value
        assert fields["device_type"].value


def test_provenance_is_per_observation_not_per_entity():
    """§3.1: OBS-001's ONVIF payload states Model=P3245-LVE directly. OBS-002
    is the same device seen over HTTP, whose realm `AXIS_ACCC8E4F21A9` yields
    no model at all -- it can only show a model by inheriting one from its
    sibling. A single entity-level provenance cannot tell those apart, and
    Stage 1/2 vs Stage 4 accuracy depend entirely on the distinction.

    Do NOT use OBS-061/073 vendor for this: OBS-073's mDNS payload asserts
    `vendor=HIKVISION` outright, so both members witness vendor directly."""
    obs_view = observation_fields(CLAIMS, MEMBERSHIPS, RESOLVED, RULES, TRACER)
    assert obs_view["OBS-001"]["model"].provenance == "direct"
    assert obs_view["OBS-002"]["model"].provenance == "propagated"
    assert obs_view["OBS-001"]["model"].value == \
           obs_view["OBS-002"]["model"].value == "P3245-LVE"


def test_propagated_confidence_is_decayed_below_the_direct_reading():
    """§2.5: propagated = source_confidence x link_weight x decay_base^hop."""
    obs_view = observation_fields(CLAIMS, MEMBERSHIPS, RESOLVED, RULES, TRACER)
    direct = obs_view["OBS-001"]["model"].confidence
    propagated = obs_view["OBS-002"]["model"].confidence
    assert 0 < propagated < direct


def test_both_members_are_direct_when_both_genuinely_witness_the_field():
    """The mirror case, and a correction to the design doc's §3.1 example:
    OBS-073 does NOT inherit its vendor. Its mDNS payload carries
    `vendor=HIKVISION` explicitly, so both members of E-066 are `direct`."""
    obs_view = observation_fields(CLAIMS, MEMBERSHIPS, RESOLVED, RULES, TRACER)
    assert obs_view["OBS-061"]["vendor"].provenance == "direct"
    assert obs_view["OBS-073"]["vendor"].provenance == "direct"


def test_singleton_members_are_always_direct_or_unknown():
    obs_view = observation_fields(CLAIMS, MEMBERSHIPS, RESOLVED, RULES, TRACER)
    assert obs_view["OBS-045"]["vendor"].provenance == "direct"
    assert obs_view["OBS-043"]["vendor"].provenance == "unknown"


def test_unknown_does_not_propagate():
    """§2.5: propagating it would fill a sibling's genuine gap with a
    non-answer that then reads as a resolved field."""
    steps = [s for s in TRACER.steps() if s["op"] == "propagate"]
    assert all(s["output"] not in ("Unknown", "unknown") for s in steps)
    obs_view = observation_fields(CLAIMS, MEMBERSHIPS, RESOLVED, RULES, TRACER)
    for fields in obs_view.values():
        for f in fields.values():
            if f.value in ("Unknown", "unknown", UNDECIDABLE, ""):
                assert f.provenance == "unknown"
                assert f.confidence == 0.0


def test_runner_up_is_recorded_in_the_trace():
    """§9.1 Q6 -- what lost, and by how much."""
    with_runner_up = [f for fields in RESOLVED.values() for f in fields.values()
                      if f.runner_up is not None]
    assert with_runner_up

# tests/test_claims.py
from obs_pipeline.claims import build_claims, field_claims, identity_claims
from obs_pipeline.extract import load_observations
from obs_pipeline.loader import load_rules
from obs_pipeline.trace import Tracer

RULES = load_rules("rules", "obs-data/observations.csv")
OBSERVATIONS = load_observations("obs-data/observations.csv")
CLAIMS = build_claims(OBSERVATIONS, RULES, Tracer())


def _for(obs_id, key):
    return {c.value: c for c in CLAIMS if c.obs_id == obs_id and c.key == key}


def test_claim_key_excludes_source():
    """Invariant #2: source is metadata on the claim, never in the key, so
    multiple sources can collide and either corroborate or contradict."""
    c = _for("OBS-001", "vendor")["Axis Communications"]
    assert c.key == "vendor"
    assert "onvif" in c.witness_groups


def test_multiple_witness_groups_collapse_onto_one_claim():
    """OBS-001: ONVIF Manufacturer=AXIS and the AC:CC:8E OUI both say Axis."""
    axis = _for("OBS-001", "vendor")["Axis Communications"]
    assert set(axis.witness_groups) >= {"onvif", "oui"}
    assert axis.weight > 0.85   # max base plus a real independence bonus


def test_out_of_vocab_claims_are_kept_and_flagged():
    """§6.3: rejecting them at construction would discard the evidence that
    the vocabulary or alias map is incomplete at the moment it is generated.

    The value is lowercase because normalization canonicalises unmapped
    surface strings to the alias-map key form (§2.2) -- which is exactly the
    key a human pastes into normalization.yaml when acting on the queue."""
    lts = _for("OBS-012", "vendor")
    assert "lts security" in lts, f"got {sorted(lts)}"
    assert lts["lts security"].in_vocab is False


def test_out_of_vocab_claims_survive_alongside_an_in_vocab_rival():
    """OBS-012 carries three vendor claims: `Hikvision` from its OUI (in
    vocab), plus `app-webs` and `lts security` from the HTTP banner (both
    out of vocab). All three must be KEPT -- the rejection happens at field
    resolution, where it is visible in the trace and countable."""
    lts = _for("OBS-012", "vendor")
    assert {v for v, c in lts.items() if c.in_vocab} == {"Hikvision"}
    assert {v for v, c in lts.items() if not c.in_vocab} == {"app-webs", "lts security"}


def test_in_vocab_claims_are_flagged_in_vocab():
    assert _for("OBS-001", "vendor")["Axis Communications"].in_vocab is True


def test_open_vocabulary_fields_are_always_in_vocab():
    """model and firmware are open vocabulary -- enumerating model designators
    is not tractable (§2.5)."""
    assert all(c.in_vocab for c in CLAIMS if c.key in {"model", "firmware"})


def test_identity_and_field_claims_are_separable():
    ident = identity_claims(CLAIMS)
    fields = field_claims(CLAIMS)
    assert {c.key for c in ident} <= {"mac", "serial", "hostname_token"}
    assert {c.key for c in fields} <= {"vendor", "model", "firmware", "device_type"}
    assert len(ident) + len(fields) == len(CLAIMS)


def test_identity_claims_are_scored_with_identity_coefficients():
    t = Tracer()
    claims = build_claims(OBSERVATIONS, RULES, t)
    steps = {s["step_id"]: s for s in t.steps()}
    mac = next(c for c in identity_claims(claims) if c.key == "mac")
    assert steps[mac.traced.step_id]["decomposition"]["coefficient_set"] == "identity_claims"
    vendor = next(c for c in field_claims(claims) if c.key == "vendor")
    assert steps[vendor.traced.step_id]["decomposition"]["coefficient_set"] == "field_claims"


def test_conflicting_values_on_one_key_penalize_each_other():
    """OBS-009 says 'Pelco by Schneider Electric...' and its OUI says Pelco;
    any obs whose sources disagree on vendor must show a penalty."""
    contested = [c for c in CLAIMS
                 if c.key == "vendor" and len(_for(c.obs_id, "vendor")) > 1]
    assert contested, "expected at least one obs with competing vendor claims"
    t = Tracer()
    build_claims(OBSERVATIONS, RULES, t)
    penalties = [s["decomposition"]["penalty"] for s in t.steps()
                 if s["op"] == "score"]
    assert any(p > 0 for p in penalties)


def test_a_claim_is_walkable_back_to_its_payload_span():
    """§9.1 Q1/Q2. The score step must name the extraction/normalization
    steps it scored, so an auditor can walk a claim back to the substring it
    came from rather than searching the trace for a matching output."""
    t = Tracer()
    claims = build_claims(OBSERVATIONS, RULES, t)
    steps = {s["step_id"]: s for s in t.steps()}
    claim = next(c for c in claims if c.obs_id == "OBS-001" and c.key == "model")
    frontier, seen = list(steps[claim.traced.step_id]["parents"]), set()
    spans = []
    while frontier:
        sid = frontier.pop()
        if sid in seen:
            continue
        seen.add(sid)
        step = steps[sid]
        spans += [i for i in step["inputs"] if "#raw_payload[" in i]
        frontier += step["parents"]
    assert any(s.startswith("obs:OBS-001#raw_payload[") for s in spans), spans


def test_obs_with_no_identity_evidence_emits_an_absence_step():
    t = Tracer()
    build_claims(OBSERVATIONS, RULES, t)
    assert any(s["op"] == "no_identity_claim" for s in t.steps())

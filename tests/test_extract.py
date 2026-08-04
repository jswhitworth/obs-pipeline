# tests/test_extract.py
from obs_pipeline.extract import extract_observation, load_observations
from obs_pipeline.loader import load_rules
from obs_pipeline.trace import Tracer

RULES = load_rules("rules", "obs-data/observations.csv")
OBS = {o["obs_id"]: o for o in load_observations("obs-data/observations.csv")}


def _values(obs_id, target):
    out = extract_observation(OBS[obs_id], RULES, Tracer())
    return {e.value for e in out if e.target == target}


def test_loads_all_74_observations():
    assert len(OBS) == 74


def test_onvif_structured_fields():
    assert _values("OBS-001", "vendor") == {"Axis Communications"}
    assert _values("OBS-001", "model") == {"P3245-LVE"}
    assert _values("OBS-001", "firmware") == {"10.12.114"}
    assert "ACCC8E4F21A9" in _values("OBS-001", "serial")


def test_http_realm_yields_both_model_and_serial():
    assert "AXIS_ACCC8E4F21A9" not in _values("OBS-002", "model")
    assert "ACCC8E4F21A9" in _values("OBS-002", "serial")


def test_mdns_macaddress_normalizes_to_the_mac_column_form():
    """OBS-003 carries macaddress=ACCC8E4F21A9 with no delimiters; the mac
    column carries AC:CC:8E:4F:21:A9. Both must land on one value or the
    §2.3 corroboration is silently lost."""
    assert _values("OBS-003", "mac") == {"ACCC8E4F21A9"}


def test_obs_073_has_no_mac_but_still_yields_identity_evidence():
    """E-066 depends on this: OBS-073's mac column is empty."""
    assert _values("OBS-073", "mac") == set()
    assert "00012E" in _values("OBS-073", "serial")
    assert "hik-2143-c2-03" in _values("OBS-073", "hostname_token")


def test_oui_contributes_vendor_but_never_a_link_basis():
    """An OUI is shared by every device a vendor ever shipped; as a link_basis
    it would merge unrelated devices wholesale."""
    out = extract_observation(OBS["OBS-011"], RULES, Tracer())
    oui = [e for e in out if e.witness_group == "oui"]
    assert oui and all(e.target_kind == "field" and e.target == "vendor" for e in oui)
    assert "Hikvision" in {e.value for e in oui}


def test_port_signature_yields_device_type():
    assert "ip_camera" in _values("OBS-001", "device_type")
    assert "network_switch" in _values("OBS-022", "device_type")


def test_empty_hostname_yields_no_claim_rather_than_a_claim_of_empty():
    assert _values("OBS-044", "hostname_token") == set()


def test_no_extraction_emits_an_explicit_absence_step():
    """§9.3: 'no rule matched' and 'a rule matched and yielded nothing' are
    different failures with different fixes."""
    t = Tracer()
    extract_observation(OBS["OBS-043"], RULES, t)   # telnet control bytes
    assert any(s["op"] == "no_extraction" for s in t.steps())


def test_extraction_step_records_payload_offsets_not_payload_text():
    """§9.4 volume control."""
    t = Tracer()
    out = extract_observation(OBS["OBS-001"], RULES, t)
    model = next(e for e in out if e.target == "model")
    chain = {s["step_id"]: s for s in t.steps()}
    step = chain[model.traced.step_id]
    while step["op"] != "extract":
        step = chain[step["parents"][0]]
    assert step["inputs"] and step["inputs"][0].startswith("obs:OBS-001#raw_payload[")

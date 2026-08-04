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
    different failures with different fixes.

    OBS-044 is the genuine dead zone: empty mac, empty hostname, a truncated
    `Server: Ax` that matches no pattern, and port 80 alone, which satisfies
    no port signature."""
    t = Tracer()
    extract_observation(OBS["OBS-044"], RULES, t)
    assert any(s["op"] == "no_extraction" for s in t.steps())


def test_unparseable_payload_still_yields_its_out_of_band_identity():
    """OBS-043's telnet payload is control-byte noise, but its mac column
    reads 00:23:AA:11:04:77. The mac and hostname columns are SCAN METADATA,
    not payload content -- a garbage banner does not invalidate the address
    observed on the wire. Dropping it would discard real identity evidence
    and misreport the row as unclusterable in no_identity_claim_rate."""
    out = extract_observation(OBS["OBS-043"], RULES, Tracer())
    assert {e.value for e in out if e.target == "mac"} == {"0023AA110477"}


def test_code_constant_witness_groups_have_base_weights():
    """`oui`, `port_signature` and `structured_column` are witness groups
    hardcoded in extract.py rather than declared in claims.yaml#sources, so
    loader.py's cross-file validation (§6.1) cannot see them -- there is no
    YAML node naming them for it to check. Guard them here instead: each
    must still have a scoring.yaml#base_weights entry, or scoring.py's
    `base_weights.get(g, 0.0)` silently scores them at zero."""
    base_weights = RULES.scoring["base_weights"]
    for literal in ("oui", "port_signature", "structured_column"):
        assert literal in base_weights, (
            f"extract.py hardcodes witness_group '{literal}' with no "
            f"scoring.yaml#base_weights entry"
        )


def test_structured_lifts_apply_to_every_source():
    """The structured block is deliberately source-independent. Filtering it
    by source would make the mac column conditional on payload quality."""
    assert "sources" not in RULES.extraction["structured"]["mac_column"]
    assert "sources" not in RULES.extraction["structured"]["hostname_column"]


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


def test_every_payload_offset_resolves_back_to_its_captured_value():
    """§9.4 stores spans instead of payload text, so a span that does not
    bound its value silently corrupts provenance while every other test
    still passes."""
    by_id = {o["obs_id"]: o for o in OBS.values()}
    t = Tracer()
    for obs in by_id.values():
        extract_observation(obs, RULES, t)
    checked = 0
    for step in t.steps():
        if step["op"] != "extract" or not step["inputs"]:
            continue
        ref = step["inputs"][0]
        if "#raw_payload[" not in ref:
            continue
        obs_id, span = ref.split("#raw_payload[")
        start, end = (int(x) for x in span.rstrip("]").split(":"))
        payload = by_id[obs_id.split(":", 1)[1]]["raw_payload"]
        assert payload[start:end] == step["output"], ref
        checked += 1
    assert checked > 40, f"only {checked} spans checked — test is not covering"

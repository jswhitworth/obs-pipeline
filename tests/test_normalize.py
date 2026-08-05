# tests/test_normalize.py
from obs_pipeline.loader import load_rules
from obs_pipeline.normalize import (
    normalize_device_type, normalize_hostname, normalize_mac, normalize_vendor,
)
from obs_pipeline.trace import Tracer

RULES = load_rules("rules", "obs-data/observations.csv")


def test_mac_delimiters_and_case_collapse_to_one_form():
    t = Tracer()
    a = normalize_mac("AC:CC:8E:4F:21:A9", RULES, t)
    b = normalize_mac("accc8e4f21a9", RULES, t)          # mdns macaddress= form
    c = normalize_mac("ac-cc-8e-4f-21-a9", RULES, t)
    assert a.value == b.value == c.value == "ACCC8E4F21A9"


def test_hostname_lowercases():
    t = Tracer()
    assert normalize_hostname("HIK-4481", RULES, t).value == "hik-4481"
    assert normalize_hostname("nvr-bldgB-01", RULES, t).value == "nvr-bldgb-01"


def test_vendor_aliases_map_onto_vocabulary():
    t = Tracer()
    for surface, expected in [
        ("AXIS", "Axis Communications"),
        ("Avigilon Corporation", "Avigilon"),
        ("VIVOTEK Inc.", "Vivotek"),
        ("HIKVISION", "Hikvision"),
        ("Hanwha Techwin", "Hanwha Vision"),
        ("Panasonic i-PRO Sensing Solutions", "i-PRO"),
    ]:
        assert normalize_vendor(surface, RULES, t).value == expected


def test_known_alias_gaps_pass_through_unmapped():
    """§6.3: these must NOT normalize -- they are the live vocab_reject path."""
    t = Tracer()
    for surface in ["LTS Security", "Amcrest", "Wisenet", "VVTK"]:
        out = normalize_vendor(surface, RULES, t).value
        assert out not in RULES.vocab.vendors, f"{surface} unexpectedly mapped"
        assert out == surface.lower()


def test_unmapped_spellings_of_one_vendor_collapse_to_one_value():
    """§2.2: get this wrong and two sources spelling the same unknown vendor
    differently register as a CONFLICT and penalise each other, with no
    extraction rule looking broken."""
    t = Tracer()
    variants = {normalize_vendor(s, RULES, t).value
                for s in ["Amcrest", "AMCREST", "amcrest", "  Amcrest  "]}
    assert len(variants) == 1


def test_normalization_never_emits_the_escape_value():
    """Rejection belongs at field resolution, where it is visible in the
    trace and countable (§6.3). Normalization must not pre-empt it."""
    t = Tracer()
    assert normalize_vendor("Amcrest", RULES, t).value != "Unknown"


def test_device_type_alias_maps_to_snake_case():
    t = Tracer()
    assert normalize_device_type("Network Camera", RULES, t).value == "ip_camera"
    assert normalize_device_type("Ethernet Switch", RULES, t).value == "network_switch"


def test_every_normalization_emits_a_step_with_before_and_after():
    t = Tracer()
    out = normalize_vendor("AXIS", RULES, t)
    row = next(s for s in t.steps() if s["step_id"] == out.step_id)
    assert row["op"] == "normalize"
    assert row["before"] == "AXIS"
    assert row["output"] == "Axis Communications"
    assert row["rule_id"] == "normalization.yaml#vendor_alias"

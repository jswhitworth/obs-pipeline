# tests/test_regression_cases.py
"""The five named cases from design doc §7.4, plus the false-merge trap.

These are asserted INDIVIDUALLY, not as a rate. §7.4: ground truth is 68
entities over 74 observations with only ~5 positive pairs, and any threshold
'met' at that n is noise. The small multi-observation set is still valuable --
as named regression cases that must each resolve correctly in isolation.
"""
import csv

import pytest

from run import run_pipeline


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return run_pipeline("obs-data/observations.csv", "rules",
                        tmp_path_factory.mktemp("runs"))


@pytest.fixture(scope="module")
def resolutions(bundle):
    with open(bundle / "resolutions.csv", newline="", encoding="utf-8") as fh:
        return {r["obs_id"]: r for r in csv.DictReader(fh)}


@pytest.fixture(scope="module")
def entities(bundle):
    with open(bundle / "entities.csv", newline="", encoding="utf-8") as fh:
        return {r["entity_id"]: r for r in csv.DictReader(fh)}


def _same_entity(res, *obs_ids):
    return len({res[o]["entity_id"] for o in obs_ids}) == 1


def test_e001_three_sources_one_mac(resolutions):
    assert _same_entity(resolutions, "OBS-001", "OBS-002", "OBS-003")
    assert resolutions["OBS-001"]["vendor"] == "Axis Communications"
    assert resolutions["OBS-001"]["model"] == "P3245-LVE"


def test_e002_two_sources_one_mac_differing_hostnames(resolutions):
    assert _same_entity(resolutions, "OBS-004", "OBS-005")


def test_e052_mac_beats_hostname_token(resolutions):
    """Same MAC, different hostname AND different IP."""
    assert _same_entity(resolutions, "OBS-047", "OBS-072")


def test_e066_links_by_serial_when_mac_is_empty(resolutions):
    assert _same_entity(resolutions, "OBS-061", "OBS-073")


def test_e066_propagates_vendor_to_the_evidence_poor_member(resolutions):
    """§3.1: OBS-073 contributes almost nothing -- whatever vendor it shows
    was carried in from OBS-061. Diffing that row naively against labels would
    credit the pipeline for extraction it never performed."""
    assert resolutions["OBS-073"]["vendor"] == "Hikvision"


def test_e074_firmware_conflict_is_undecidable(resolutions, entities):
    """§2.5: firmware is temporal and the data model atemporal. The ENTITY
    cannot pick a value, so it reads `undecidable` and is excluded from
    Stage 4 denominators.

    But the ambiguity is recorded at the entity level ONLY. Each observation
    keeps what it actually witnessed — OBS-069 saw 8.10.0135 and OBS-074 saw
    8.11.0021, and both were true when taken. No observation is scored wrong
    for reporting what it saw."""
    assert _same_entity(resolutions, "OBS-069", "OBS-074")
    entity_id = resolutions["OBS-069"]["entity_id"]
    assert entities[entity_id]["firmware"] == "undecidable"
    assert entities[entity_id]["firmware_confidence"] == "0.0"

    assert resolutions["OBS-069"]["firmware"] == "8.10.0135"
    assert resolutions["OBS-074"]["firmware"] == "8.11.0021"
    assert resolutions["OBS-069"]["firmware_provenance"] == "direct"
    assert resolutions["OBS-074"]["firmware_provenance"] == "direct"


def test_no_false_merge_across_identical_model_and_vendor(resolutions):
    """OBS-045..052 are eight distinct Axis P3245-LVE cameras. §7.3 Stage 3
    biases toward precision because a false merge corrupts every member's
    fields via propagation in Stage 4."""
    block = [f"OBS-{n:03d}" for n in range(45, 53)]
    entities = {resolutions[o]["entity_id"] for o in block}
    assert len(entities) == 8


def test_hikvision_block_stays_six_distinct_devices(resolutions):
    """OBS-059..064 share a model realm and differ only by X-Serial."""
    block = [f"OBS-{n:03d}" for n in range(59, 65)]
    assert len({resolutions[o]["entity_id"] for o in block}) == 6

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
def shared(bundle):
    """Values two observations have in common on one link_basis."""
    with open(bundle / "claims.csv", newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r["kind"] == "link_basis"]

    def _shared(a, b, basis):
        va = {r["value"] for r in rows if r["obs_id"] == a and r["key"] == basis}
        vb = {r["value"] for r in rows if r["obs_id"] == b and r["key"] == basis}
        return va & vb

    return _shared


@pytest.fixture(scope="module")
def entities(bundle):
    with open(bundle / "entities.csv", newline="", encoding="utf-8") as fh:
        return {r["entity_id"]: r for r in csv.DictReader(fh)}


def _same_entity(res, *obs_ids):
    return len({res[o]["entity_id"] for o in obs_ids}) == 1


def test_e001_three_sources_one_mac(resolutions):
    """Three sources, one device. Note mac and hostname_token are fully
    redundant for this triple — all three observations share both — so this
    case cannot isolate mac as the linking mechanism, and does not try to.
    What it verifies is end-to-end value resolution across three sources."""
    assert _same_entity(resolutions, "OBS-001", "OBS-002", "OBS-003")
    assert resolutions["OBS-001"]["vendor"] == "Axis Communications"
    assert resolutions["OBS-001"]["model"] == "P3245-LVE"


def test_e002_two_sources_one_mac_differing_hostnames(resolutions, shared):
    """OBS-004 and OBS-005 are one NVR seen over SNMP and HTTP, with genuinely
    DIFFERENT hostnames (`nvr-bldgb-01` vs `bldgb-recorder-a`).

    Co-membership alone is not enough to test this: the pair also shares
    serial `ZN9K8H2M4001`, so a regression that broke mac linking entirely is
    absorbed by that redundant fallback and the merge still happens. The
    basis-level assertions are what make a mac-specific break visible."""
    assert _same_entity(resolutions, "OBS-004", "OBS-005")
    assert shared("OBS-004", "OBS-005", "mac") == {"00166C22AA01"}
    assert shared("OBS-004", "OBS-005", "hostname_token") == set()


def test_e052_differing_hostname_and_ip_do_not_prevent_the_merge(resolutions, shared):
    """OBS-047 and OBS-072 are one camera seen twice, with DIFFERENT hostnames
    (`axis-p3245-l4-03` vs `cam-l4-east-conf`) and different IPs.

    Note what does NOT happen here: hostname_token holds no opinion at all,
    because the two hostnames share no value. So this is not a mac-versus-
    hostname contest, despite how it is described in §7.4. What links them is
    mac and serial, which happen to carry the same string `ACCC8E000066`. The
    property under test is that disagreeing metadata does not block a merge
    backed by hard identity."""
    assert _same_entity(resolutions, "OBS-047", "OBS-072")
    assert shared("OBS-047", "OBS-072", "mac") == {"ACCC8E000066"}
    assert shared("OBS-047", "OBS-072", "hostname_token") == set()


def test_e066_links_without_any_mac(resolutions, shared):
    """OBS-073's mac column is EMPTY, so the merge cannot rest on mac at all.
    §7.4 allows serial or hostname; the property is that a mac-less
    observation still clusters with its sibling."""
    assert _same_entity(resolutions, "OBS-061", "OBS-073")
    assert shared("OBS-061", "OBS-073", "mac") == set()
    assert (shared("OBS-061", "OBS-073", "serial")
            or shared("OBS-061", "OBS-073", "hostname_token"))


def test_propagation_fills_an_evidence_poor_sibling(resolutions):
    """§3.1, and the ONLY thing in this suite that exercises propagation.

    OBS-002 is the same camera as OBS-001 seen over HTTP; its realm
    `AXIS_ACCC8E4F21A9` yields no model, so any model it shows was inherited.
    Asserting the VALUE alone would not be enough -- a direct extraction
    satisfies that too -- so the provenance column is the real assertion.

    Do NOT use OBS-073 vendor for this: its mDNS payload carries
    `vendor=HIKVISION` outright, so it witnesses vendor directly and inherits
    nothing. A test built on that premise stays green even when propagation
    is completely broken."""
    assert resolutions["OBS-002"]["model"] == "P3245-LVE"
    assert resolutions["OBS-002"]["model_provenance"] == "propagated"
    assert resolutions["OBS-001"]["model_provenance"] == "direct"


def test_out_of_vocab_vendor_resolves_to_the_escape_value(resolutions):
    """§6.3: closed-vocabulary enforcement. OBS-033's banner yields
    `microsoft-httpapi`, which normalizes cleanly but is not a vocabulary
    member, and no OUI supplies a fallback. Without this, disabling vocabulary
    enforcement leaks the raw string into output and every other regression
    test still passes."""
    assert resolutions["OBS-033"]["vendor"] == "Unknown"


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

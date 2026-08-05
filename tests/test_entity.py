# tests/test_entity.py
import copy

import pytest

from obs_pipeline.claims import build_claims
from obs_pipeline.entity import partition, resolve_entities
from obs_pipeline.extract import load_observations
from obs_pipeline.loader import load_rules
from obs_pipeline.trace import Tracer

RULES = load_rules("rules", "obs-data/observations.csv")
OBSERVATIONS = load_observations("obs-data/observations.csv")


def _run():
    t = Tracer()
    claims = build_claims(OBSERVATIONS, RULES, t)
    return resolve_entities(claims, OBSERVATIONS, RULES, t), t


MEMBERSHIPS, TRACER = _run()
PARTITION = partition(MEMBERSHIPS)
IDENT = [c for c in build_claims(OBSERVATIONS, RULES, Tracer())
         if c.kind == "link_basis"]


def _cluster_of(obs_id):
    return next(m for eid, m in PARTITION.items() if obs_id in m)


def test_every_observation_lands_in_exactly_one_entity():
    assigned = [m.obs_id for m in MEMBERSHIPS]
    assert len(assigned) == len(set(assigned)) == 74


def test_shared_mac_merges_across_three_sources():
    """E-001 -- OBS-001/002/003, clean corroboration."""
    assert _cluster_of("OBS-001") == {"OBS-001", "OBS-002", "OBS-003"}


def test_shared_mac_merges_despite_differing_hostnames():
    """E-002 -- OBS-004 'nvr-bldgB-01' vs OBS-005 'bldgb-recorder-a'."""
    assert _cluster_of("OBS-004") == {"OBS-004", "OBS-005"}


def test_mac_beats_hostname_and_ip_disagreement():
    """E-052 -- OBS-047/072 share a MAC but differ on hostname AND IP."""
    assert _cluster_of("OBS-047") == {"OBS-047", "OBS-072"}


def test_links_by_serial_when_mac_is_empty():
    """E-066 -- OBS-073 has no MAC at all."""
    assert _cluster_of("OBS-061") == {"OBS-061", "OBS-073"}
    for obs_id in ("OBS-061", "OBS-073"):
        basis = next(m.link_basis for m in MEMBERSHIPS if m.obs_id == obs_id)
        assert basis in {"serial", "hostname_token"}, f"{obs_id} -> {basis}"


def test_link_basis_names_a_claim_that_actually_linked_the_observation():
    """OBS-061 carries a private mac (C056E300012E) that OBS-073 does not
    share, so no mac edge exists. Reporting `mac` because it outranks by
    precedence would misattribute the merge in membership.csv AND point the
    trace parent at evidence that played no part in the decision."""
    for m in MEMBERSHIPS:
        if m.link_basis == "none":
            continue
        siblings = _cluster_of(m.obs_id) - {m.obs_id}
        if not siblings:
            continue
        shared = {c.value for c in IDENT
                  if c.obs_id == m.obs_id and c.key == m.link_basis}
        sibling_values = {c.value for c in IDENT
                          if c.obs_id in siblings and c.key == m.link_basis}
        assert shared & sibling_values, (
            f"{m.obs_id} reports link_basis={m.link_basis} but shares no "
            f"{m.link_basis} value with {sorted(siblings)}"
        )


def test_firmware_conflict_pair_still_merges():
    """E-074 -- OBS-069/074 are one device; the firmware disagreement is
    resolved in §2.5, not by refusing the merge."""
    assert _cluster_of("OBS-069") == {"OBS-069", "OBS-074"}


def test_identical_model_and_vendor_do_not_merge_distinct_devices():
    """OBS-045..052 are eight distinct Axis P3245-LVE cameras with distinct
    MACs and serials. A careless oui_model_pair basis merges them all. §7.3
    Stage 3: bias toward precision -- a false merge corrupts every member's
    fields via propagation."""
    block = [f"OBS-{n:03d}" for n in range(45, 53)]
    clusters = {frozenset(_cluster_of(o)) for o in block}
    assert len(clusters) == 8
    assert _cluster_of("OBS-045") == {"OBS-045"}


def test_hq_l4_east_resolves_to_eight_entities_not_one():
    l4 = {o["obs_id"] for o in OBSERVATIONS if o["site"] == "HQ-L4-East"}
    assert len({frozenset(_cluster_of(o)) for o in l4}) == 8


def test_entity_id_is_content_addressed_and_stable():
    again, _ = _run()
    assert {m.obs_id: m.entity_id for m in MEMBERSHIPS} == \
           {m.obs_id: m.entity_id for m in again}
    assert all(m.entity_id.startswith("E-") for m in MEMBERSHIPS)


def test_entity_id_changes_when_membership_changes():
    """§2.4: IDs are stable across identical runs but CHANGE when membership
    changes -- an ID that silently denoted a different device set would be
    worse than one that changes visibly."""
    singleton = next(m for m in MEMBERSHIPS
                     if len(PARTITION[m.entity_id]) == 1)
    pair = next(m for m in MEMBERSHIPS if len(PARTITION[m.entity_id]) > 1)
    assert singleton.entity_id != pair.entity_id


def test_merge_order_is_pinned_not_inherited_from_row_order():
    """§1/§2.4 determinism."""
    shuffled = list(reversed(OBSERVATIONS))
    t = Tracer()
    claims = build_claims(shuffled, RULES, t)
    other = partition(resolve_entities(claims, shuffled, RULES, t))
    assert {frozenset(v) for v in other.values()} == \
           {frozenset(v) for v in PARTITION.values()}


def test_membership_records_basis_agreement_and_conflict_detail():
    """§2.4: the tie-break decision is recorded as its own auditable columns."""
    m = next(m for m in MEMBERSHIPS if m.obs_id == "OBS-047")
    assert isinstance(m.basis_agreement, bool)
    assert m.conflict_detail is None or isinstance(m.conflict_detail, str)


def test_refused_merges_are_first_class_steps():
    """§9.3: a refused merge is a decision, not a non-event.

    This dataset contains no naturally weak identity claim -- every mac,
    serial and hostname_token claim clears 0.55 -- so the refusal path is
    exercised by raising the threshold above every claim weight."""
    strict = copy.deepcopy(RULES)
    strict.entity_resolution["link_weight_threshold"] = 0.99
    t = Tracer()
    claims = build_claims(OBSERVATIONS, RULES, t)
    memberships = resolve_entities(claims, OBSERVATIONS, strict, t)
    refused = [s for s in t.steps() if s["op"] == "merge_refused"]
    assert refused, "no merge was refused even at threshold 0.99"
    assert all(s["reason"] == "below_threshold" for s in refused)
    assert len(partition(memberships)) == 74, "every merge should be refused"


def test_no_merge_is_refused_at_the_configured_threshold():
    """The mirror, and a real finding about this data: at the configured
    0.55 nothing is refused, so the refusal rate is legitimately zero here
    rather than untested."""
    assert [s for s in TRACER.steps() if s["op"] == "merge_refused"] == []


def test_unimplemented_conflict_policy_fails_loudly():
    """A declared policy the engine does not honour must not load -- the
    same rule already enforced for edge_weight and merge_order."""
    strict = copy.deepcopy(RULES)
    strict.entity_resolution["cross_basis_conflict"]["policy"] = "refuse_and_flag"
    with pytest.raises(ValueError, match="refuse_and_flag"):
        resolve_entities(build_claims(OBSERVATIONS, RULES, Tracer()),
                         OBSERVATIONS, strict, Tracer())

# obs_pipeline/entity.py
"""Rule categories 5 and 6 -- entity resolution (design doc §2.4).

Runs on IDENTITY claims only, never on resolved field values: that keeps the
pipeline a strict DAG and prevents a field->cluster feedback loop (invariant #1).

Merges are applied in the order pinned by entity_resolution.yaml, because
merge order changes cluster outcomes and determinism (§1) requires it be a
rule rather than an accident of input row order.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from obs_pipeline.claims import identity_claims
from obs_pipeline.trace import Traced, Tracer


@dataclass(frozen=True)
class Membership:
    obs_id: str
    entity_id: str
    link_basis: str
    link_weight: float
    basis_agreement: bool
    conflict_detail: str | None
    traced: Traced


class _UnionFind:
    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        lo, hi = sorted((ra, rb))     # deterministic: lower obs_id becomes root
        self.parent[hi] = lo
        return True


def _entity_id(members, cfg) -> str:
    digest = hashlib.sha256(
        "|".join(sorted(members)).encode("utf-8")
    ).hexdigest()[: cfg["hash_length"]]
    return f"{cfg['prefix']}{digest}"


def partition(memberships) -> dict[str, frozenset[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for m in memberships:
        out[m.entity_id].add(m.obs_id)
    return {k: frozenset(v) for k, v in out.items()}


def _components(edges, obs_ids) -> dict[str, frozenset[str]]:
    """Union-find over ONE subset of edges -> obs_id to its component.

    Used to build a provisional clustering per link_basis, which is what §2.4's
    cross-basis contradiction actually compares. Reading roots out of the main
    union-find mid-loop cannot answer that question: roots keep changing as
    later merges land, and every accepted edge unions unconditionally, so two
    accepted edges touching one observation are always in the same final
    component by construction.
    """
    uf = _UnionFind(obs_ids)
    for edge in edges:
        uf.union(edge[2], edge[3])
    groups: dict[str, set[str]] = defaultdict(set)
    for oid in obs_ids:
        groups[uf.find(oid)].add(oid)
    return {oid: frozenset(groups[uf.find(oid)]) for oid in obs_ids}


def resolve_entities(claims, observations, rules, tracer: Tracer) -> list[Membership]:
    cfg = rules.entity_resolution
    threshold = float(cfg["link_weight_threshold"])
    precedence = list(cfg["basis_precedence"])
    prec_rank = {b: i for i, b in enumerate(precedence)}

    # The YAML is authoritative: fail loudly rather than silently ignoring a
    # value the code does not implement.
    if cfg["edge_weight"] != "min_of_endpoints":
        raise ValueError(
            f"entity_resolution.yaml#edge_weight '{cfg['edge_weight']}' is not "
            f"implemented; only 'min_of_endpoints' is"
        )
    if list(cfg["merge_order"]) != ["link_weight_desc", "basis_precedence", "obs_id_asc"]:
        raise ValueError(
            f"entity_resolution.yaml#merge_order {cfg['merge_order']} does not "
            f"match the implemented order; merge order changes cluster outcomes, "
            f"so a declared order the engine does not honour must not load"
        )
    policy = cfg["cross_basis_conflict"]["policy"]
    if policy != "precedence_wins":
        raise ValueError(
            f"entity_resolution.yaml#cross_basis_conflict.policy '{policy}' is "
            f"not implemented; only 'precedence_wins' is. The YAML comment "
            f"lists refuse_and_flag as an alternative, but the engine never "
            f"undoes a merge, so accepting it would silently mislabel the trace"
        )

    obs_ids = sorted(o["obs_id"] for o in observations)
    ident = [c for c in identity_claims(claims) if c.key in prec_rank]

    # Candidate edges: two observations sharing one (link_basis, value).
    by_value: dict[tuple[str, str], list] = defaultdict(list)
    for c in ident:
        by_value[(c.key, c.value)].append(c)

    edges = []
    for (basis, value), group in sorted(by_value.items()):
        if len(group) < 2:
            continue
        group = sorted(group, key=lambda c: c.obs_id)
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                weight = min(a.weight, b.weight)   # edge_weight: min_of_endpoints
                edges.append((weight, prec_rank[basis], a.obs_id, b.obs_id,
                              basis, value, a, b))

    # §2.4 pinned merge order: (link_weight desc, basis_precedence, obs_id asc).
    edges.sort(key=lambda e: (-e[0], e[1], e[2], e[3]))

    accepted, uf = [], _UnionFind(obs_ids)
    # §9: cluster identity is emergent from a SEQUENCE of merge decisions, and
    # that sequence is not recoverable from the outcome. Unless each merge is
    # a parent of the assignment it produced, the trace ASSERTS the entity id
    # rather than explaining it, and replay cannot tell a complete trace from
    # one with every merge deleted.
    merge_steps: dict[str, list] = defaultdict(list)
    # Claims that actually produced an accepted edge FOR THIS OBSERVATION.
    # Selecting link_basis from all of an obs's claims instead would let a
    # private claim it shares with nobody outrank the claim that genuinely
    # linked it, misattributing the merge in both membership.csv and the trace.
    linking: dict[str, dict[tuple[str, str], object]] = defaultdict(dict)

    for edge in edges:
        weight, _rank, a_id, b_id, basis, value, a, b = edge
        if weight < threshold:
            tracer.step(op="merge_refused",
                        rule_id="entity_resolution.yaml#link_weight_threshold",
                        inputs=[f"obs:{a_id}", f"obs:{b_id}"],
                        output=None, reason="below_threshold",
                        detail={"basis": basis, "link_weight": round(weight, 6),
                                "threshold": threshold})
            continue
        accepted.append(edge)
        merged = uf.union(a_id, b_id)
        step = tracer.step(op="merge" if merged else "merge_redundant",
                            rule_id="entity_resolution.yaml#merge_order",
                            inputs=[f"obs:{a_id}", f"obs:{b_id}"],
                            output=None, parents=[a.traced, b.traced],
                            detail={"basis": basis, "value": value,
                                    "link_weight": round(weight, 6)})
        merge_steps[a_id].append(step)
        merge_steps[b_id].append(step)
        linking[a_id][(a.key, a.value)] = a
        linking[b_id][(b.key, b.value)] = b

    groups: dict[str, set[str]] = defaultdict(set)
    for oid in obs_ids:
        groups[uf.find(oid)].add(oid)
    entity_of = {
        oid: _entity_id(members, cfg["entity_id"])
        for members in groups.values()
        for oid in members
    }

    # Provisional clustering per basis, computed AFTER the merge loop.
    per_basis = {
        basis: _components([e for e in accepted if e[4] == basis], obs_ids)
        for basis in precedence
    }

    memberships = []
    for oid in obs_ids:
        candidates = list(linking[oid].values())
        if not candidates:
            candidates = [c for c in ident if c.obs_id == oid]
        # Explicit final tie-break on value: do not rely on upstream sort order.
        candidates.sort(key=lambda c: (-c.weight, prec_rank[c.key], c.value))
        best = candidates[0] if candidates else None

        # A basis only holds an opinion if it actually grouped this obs with
        # someone. Two bases contradict when neither opinion contains the
        # other -- i.e. NEITHER is a subset of the other. A nested pair (e.g.
        # mac groups {A,B} while the looser hostname_token groups {A,B,C}) is
        # scored as agreement, not a contradiction: asserting MORE than a
        # tighter basis is not asserting something contradictory. This flag
        # is scoped narrowly to genuine contradiction between bases; it does
        # NOT mean "no basis over-merged" -- over-merging by a looser basis is
        # a real but separate concern, and widening this check to catch it
        # would fire constantly (mac's precise pairs are routinely subsets of
        # hostname's looser groupings) and destroy the flag's signal.
        opinions = {b: comp[oid] for b, comp in per_basis.items()
                    if len(comp[oid]) > 1}
        contending = sorted(
            {b for b, c1 in opinions.items() for b2, c2 in opinions.items()
             if b != b2 and not (c1 <= c2 or c2 <= c1)},
            key=lambda b: prec_rank[b],
        )
        agreement, detail = not contending, None
        if contending:
            winner = contending[0]
            detail = ("cross_basis_conflict: "
                      + ",".join(f"{b}->{sorted(opinions[b])[0]}" for b in contending)
                      + f"; precedence_winner={winner}")
            tracer.step(op="merge_refused",
                        rule_id="entity_resolution.yaml#basis_precedence",
                        inputs=[f"obs:{oid}"], output=None,
                        reason="cross_basis_conflict",
                        detail={"bases": contending, "precedence_winner": winner,
                                "policy": policy})

        traced = tracer.step(
            op="assign_entity",
            rule_id="entity_resolution.yaml#entity_id",
            inputs=[f"obs:{oid}"],
            output=entity_of[oid],
            parents=([best.traced] if best else []) + merge_steps[oid],
            detail={"link_basis": best.key if best else None,
                    "basis_agreement": agreement},
        )
        memberships.append(Membership(
            obs_id=oid,
            entity_id=entity_of[oid],
            link_basis=best.key if best else "none",
            link_weight=best.weight if best else 0.0,
            basis_agreement=agreement,
            conflict_detail=detail,
            traced=traced,
        ))
    return memberships

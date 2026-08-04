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


def resolve_entities(claims, observations, rules, tracer: Tracer) -> list[Membership]:
    cfg = rules.entity_resolution
    threshold = float(cfg["link_weight_threshold"])
    precedence = list(cfg["basis_precedence"])
    prec_rank = {b: i for i, b in enumerate(precedence)}

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
                weight = min(a.weight, b.weight)   # conjunctive: weaker endpoint governs
                edges.append((weight, prec_rank[basis], a.obs_id, b.obs_id,
                              basis, value, a, b))

    # §2.4 pinned merge order: (link_weight desc, basis_precedence, obs_id asc).
    edges.sort(key=lambda e: (-e[0], e[1], e[2], e[3]))

    uf = _UnionFind(obs_ids)
    basis_hits: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    for weight, _rank, a_id, b_id, basis, value, a, b in edges:
        if weight < threshold:
            tracer.step(op="merge_refused",
                        rule_id="entity_resolution.yaml#link_weight_threshold",
                        inputs=[f"obs:{a_id}", f"obs:{b_id}"],
                        output=None, reason="below_threshold",
                        detail={"basis": basis, "link_weight": round(weight, 6),
                                "threshold": threshold})
            continue
        merged = uf.union(a_id, b_id)
        tracer.step(op="merge" if merged else "merge_redundant",
                    rule_id="entity_resolution.yaml#merge_order",
                    inputs=[f"obs:{a_id}", f"obs:{b_id}"],
                    output=None, parents=[a.traced, b.traced],
                    detail={"basis": basis, "value": value,
                            "link_weight": round(weight, 6)})
        for oid in (a_id, b_id):
            basis_hits[oid][basis].add(uf.find(oid))

    groups: dict[str, set[str]] = defaultdict(set)
    for oid in obs_ids:
        groups[uf.find(oid)].add(oid)
    entity_of = {
        oid: _entity_id(members, cfg["entity_id"])
        for members in groups.values()
        for oid in members
    }

    memberships = []
    for oid in obs_ids:
        mine = [c for c in ident if c.obs_id == oid]
        # Winning basis: highest weight, then declared precedence.
        mine.sort(key=lambda c: (-c.weight, prec_rank[c.key]))
        best = mine[0] if mine else None

        # §2.4 cross-basis contradiction: a graph-level property, kept OUT of
        # link_weight and decided by an explicit precedence rule instead.
        roots_by_basis = {b: r for b, r in basis_hits[oid].items()}
        distinct_roots = {next(iter(r)) for r in roots_by_basis.values() if len(r) == 1}
        agreement = len(distinct_roots) <= 1
        detail = None
        if not agreement:
            winner = min(roots_by_basis, key=lambda b: prec_rank[b])
            detail = (
                f"cross_basis_conflict: "
                + ",".join(f"{b}->{sorted(roots_by_basis[b])[0]}"
                           for b in sorted(roots_by_basis, key=lambda x: prec_rank[x]))
                + f"; precedence_winner={winner}"
            )
            tracer.step(op="merge_refused",
                        rule_id="entity_resolution.yaml#basis_precedence",
                        inputs=[f"obs:{oid}"], output=None,
                        reason="cross_basis_conflict",
                        detail={"bases": sorted(roots_by_basis),
                                "precedence_winner": winner})

        traced = tracer.step(
            op="assign_entity",
            rule_id="entity_resolution.yaml#entity_id",
            inputs=[f"obs:{oid}"],
            output=entity_of[oid],
            parents=[best.traced] if best else [],
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

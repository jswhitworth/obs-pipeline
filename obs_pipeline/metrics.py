# obs_pipeline/metrics.py
"""Label-free metrics (design doc §8.1-§8.3).

These need no ground truth and are the DRIFT DETECTOR -- they catch movement
in the gap between rule changes and eval runs. This module runs inside the
pipeline and must never read labels (invariant #5); label-dependent metrics
are emitted by eval.py.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from obs_pipeline.fields import UNDECIDABLE, absent_values


def _fields(rules) -> list[str]:
    """claims.yaml#fields, in its DECLARED order (vendor, model, firmware,
    device_type). §6.1: three modules used to carry a hardcoded copy of this
    vocabulary; a rules-legal field addition silently produced numbers no
    consumer of that copy knew to look for. Read it, and do not sort it --
    the order is an authoring choice (design doc §3's most-identifying-field-
    first ordering), not a set."""
    return list(rules.claims["fields"])


def load_registry(path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def metrics_hash(path) -> str:
    """Canonicalized the same way loader.py hashes rule files, so line-ending
    churn alone does not move the hash -- but this is a SEPARATE hash from
    rules_rollup (§8.1): editing metrics.yaml must never move rules_rollup,
    and editing rules/ must never move this one."""
    text = Path(path).read_text(encoding="utf-8").replace("\r\n", "\n").strip() + "\n"
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row(metric: str, scope: str, value: float, n: int) -> dict:
    return {"metric": metric, "scope": scope, "value": round(float(value), 6),
            "n": int(n)}


def emit_metrics(*, claims, memberships, resolved, obs_fields, entity_steps,
                 stability_steps, rules, registry, trace_steps) -> list[dict]:
    rows: list[dict] = []
    fields = _fields(rules)
    closed = rules.claims.get("closed_vocabulary_fields", {})
    open_fields = list(rules.claims.get("open_vocabulary_fields", []))
    absent = absent_values(rules)
    n_obs = len(memberships)
    n_ent = len(resolved)

    def _known(value: str) -> bool:
        # Undecidable is real, witnessed evidence (§2.5) -- it is not an
        # absence marker -- but it is also not a USABLE value: the entity
        # could not settle on one. Treating it as "filled"/"known" would
        # make firmware's coverage number report an answer nobody can read
        # off the field. §7.4/Stage 4 already excludes it from denominators
        # for the identical reason; these coverage metrics follow suit.
        return value not in absent and value != UNDECIDABLE

    # --- coverage / completeness -------------------------------------------
    # field_fill_rate: open-vocabulary fields only. Closed-vocabulary fields
    # are never null (§2.5 -- no evidence resolves to the escape value, not
    # to blank), so a fill rate there is structurally 100% and known_rate
    # replaces it instead (§8.3).
    for field in fields:
        if field not in open_fields:
            continue
        filled = sum(1 for e in resolved if _known(resolved[e][field].value))
        rows.append(_row("field_fill_rate", f"field:{field}",
                         filled / max(n_ent, 1), n_ent))

        # §8.3: 90% model fill means something different if 60% of it arrived
        # by propagation. The split is per OBSERVATION, because provenance is
        # only recorded at that level (§3.1) -- an entity's fill rate has no
        # single provenance to split by.
        for prov in ("direct", "propagated"):
            values = [obs_fields[oid][field] for oid in obs_fields]
            n_prov = sum(1 for f in values
                        if f.provenance == prov and _known(f.value))
            rows.append(_row("field_fill_rate", f"field:{field}:{prov}",
                             n_prov / max(n_obs, 1), n_obs))

    # known_rate: closed-vocabulary fields only -- the escape-value complement
    # of field_fill_rate for the fields where "null" cannot occur.
    for field in fields:
        if field not in closed:
            continue
        known = sum(1 for e in resolved if _known(resolved[e][field].value))
        rows.append(_row("known_rate", f"field:{field}",
                         known / max(n_ent, 1), n_ent))

    # vocab_reject_frequency: the ranked, distinct set of rejected surface
    # values across ALL closed fields (§6.3's expansion work queue).
    rejected = [s["output"] for s in trace_steps if s["op"] == "vocab_reject"]
    for value, count in Counter(rejected).most_common():
        rows.append(_row("vocab_reject_frequency", f"value:{value}",
                         count, len(rejected)))

    # vocab_gap_rate: of the entities resolving to a field's escape value,
    # how many are Unknown SPECIFICALLY because a witnessed value was
    # rejected by the vocabulary -- as opposed to having no evidence at all.
    # Only the rejected half is actionable (adding a vocab entry fixes it);
    # the no-evidence half needs a new extraction rule or source, not a
    # vocabulary edit.
    entity_of = {m.obs_id: m.entity_id for m in memberships}
    reject_obs_by_field: dict[str, set] = defaultdict(set)
    for s in trace_steps:
        if s["op"] != "vocab_reject":
            continue
        obs_id = s["inputs"][0].split(":", 1)[1]
        reject_obs_by_field[s.get("field")].add(obs_id)

    for field in fields:
        if field not in closed:
            continue
        escape = closed[field]["escape"]
        unknown_entities = {e for e in resolved if resolved[e][field].value == escape}
        reject_entities = {entity_of[o] for o in reject_obs_by_field.get(field, ())
                           if o in entity_of}
        gap = unknown_entities & reject_entities
        rows.append(_row("vocab_gap_rate", f"field:{field}",
                         len(gap) / max(n_ent, 1), n_ent))

    no_extract = sum(1 for s in trace_steps if s["op"] == "no_extraction")
    no_ident = sum(1 for s in trace_steps if s["op"] == "no_identity_claim")
    rows.append(_row("no_extraction_rate", "global", no_extract / max(n_obs, 1), n_obs))
    rows.append(_row("no_identity_claim_rate", "global", no_ident / max(n_obs, 1), n_obs))

    # --- evidence structure -------------------------------------------------
    def _scope(kind: str, key: str) -> str:
        return f"field:{key}" if kind == "field" else f"link_basis:{key}"

    by_key: dict[tuple[str, str], list] = defaultdict(list)
    for c in claims:
        by_key[(c.kind, c.key)].append(c)

    for (kind, key), group in sorted(by_key.items()):
        scope = _scope(kind, key)
        rows.append(_row("claims_per_obs", scope, len(group) / max(n_obs, 1), n_obs))
        corroborated = sum(1 for c in group if len(c.witness_groups) >= 2)
        rows.append(_row("corroboration_rate", scope,
                         corroborated / max(len(group), 1), len(group)))

    # corroboration_rate above is CLAIM-scoped and structurally blind to
    # agreement across an entity's members -- claims are keyed by obs_id
    # (§2.3), so it stayed 0.0 for model/firmware even when three members
    # agreed through different protocols. This one reads the pooled union
    # that §2.5's entity_corroboration re-score priced, over resolved
    # values only: absent markers carry no witnesses, and undecidable is
    # excluded for the same reason it leaves every coverage denominator.
    for field in fields:
        real = [resolved[e][field] for e in resolved
                if _known(resolved[e][field].value)]
        corroborated = sum(1 for r in real if len(r.witness_groups) >= 2)
        rows.append(_row("entity_corroboration_rate", f"field:{field}",
                         corroborated / max(len(real), 1), len(real)))

    # conflict_rate: does a claim carry a non-zero conflict penalty (§2.3)?
    # The penalty lives on the `score` trace step that produced the claim's
    # weight, keyed by that step's content-addressed id -- which is exactly
    # what claim.traced.step_id already points at.
    penalties = {s["step_id"]: s["decomposition"]["penalty"]
                for s in trace_steps if s["op"] == "score"}
    for (kind, key), group in sorted(by_key.items()):
        scope = _scope(kind, key)
        conflicted = sum(1 for c in group if penalties.get(c.traced.step_id, 0) > 0)
        rows.append(_row("conflict_rate", scope,
                         conflicted / max(len(group), 1), len(group)))

    # source_win_rate: over in-vocabulary FIELD claims only. A claim "wins"
    # when its value matches the entity's resolved value for that field --
    # the same comparison resolve_fields itself makes when it picks a
    # winner. Vocab-rejected claims are excluded: they are structurally
    # barred from ever winning (a vocabulary gap, not a weight contest), and
    # that failure mode already has its own metric (vocab_reject_frequency).
    winners: Counter = Counter()
    totals: Counter = Counter()
    for c in claims:
        if c.kind != "field" or not c.in_vocab:
            continue
        eid = entity_of.get(c.obs_id)
        if eid is None:
            continue
        won = c.value == resolved[eid][c.key].value
        for src in c.sources:
            totals[src] += 1
            if won:
                winners[src] += 1
    for src, total in sorted(totals.items()):
        rows.append(_row("source_win_rate", f"source:{src}",
                         winners[src] / max(total, 1), total))

    # --- clustering shape ---------------------------------------------------
    sizes = Counter(m.entity_id for m in memberships)
    rows.append(_row("entity_count", "global", n_ent, n_ent))
    rows.append(_row("singleton_rate", "global",
                     sum(1 for v in sizes.values() if v == 1) / max(n_ent, 1), n_ent))

    basis_counts = Counter(m.link_basis for m in memberships)
    for basis, count in sorted(basis_counts.items()):
        rows.append(_row("link_basis_distribution", f"link_basis:{basis}",
                         count / max(n_obs, 1), n_obs))

    agree = sum(1 for m in memberships if m.basis_agreement)
    rows.append(_row("basis_agreement_rate", "global", agree / max(n_obs, 1), n_obs))
    rows.append(_row("cross_basis_conflict_rate", "global",
                     (n_obs - agree) / max(n_obs, 1), n_obs))

    hops = Counter(s["detail"]["hop"] for s in trace_steps if s["op"] == "propagate")
    total_hops = sum(hops.values())
    for hop, count in sorted(hops.items()):
        rows.append(_row("propagation_depth_distribution", f"hop:{hop}",
                         count / max(total_hops, 1), total_hops))

    # --- confidence -----------------------------------------------------
    # Decile buckets, with the top bucket combining [0.9, 1.0] -- otherwise
    # exact 1.0 (onvif + one corroborator, or three witnesses, both clamp to
    # 1.00 -- see §2.6) would sit alone in its own bucket instead of inside
    # the bucket the calibration question actually needs scrutinised.
    def _bucket(x: float) -> str:
        return f"{min(int(round(x, 6) * 10 + 1e-9) / 10, 0.9):.1f}"

    for field in fields:
        confs = [resolved[e][field].confidence for e in resolved]
        hist = Counter(_bucket(c) for c in confs)
        for b, count in sorted(hist.items()):
            rows.append(_row("confidence_distribution", f"field:{field}:bucket:{b}",
                             count / max(len(confs), 1), len(confs)))

    ent_conf = [entity_steps[e].value for e in sorted(entity_steps)]
    for b, count in sorted(Counter(_bucket(c) for c in ent_conf).items()):
        rows.append(_row("confidence_distribution", f"global:bucket:{b}",
                         count / max(len(ent_conf), 1), len(ent_conf)))

    stab = [stability_steps[e].value for e in sorted(stability_steps)]
    for b, count in sorted(Counter(_bucket(s) for s in stab).items()):
        rows.append(_row("stability_distribution", f"global:bucket:{b}",
                         count / max(len(stab), 1), len(stab)))

    # §8.1: a plain run emits ONLY the label-free subset. Filtering against
    # the registry (rather than trusting this function's own bookkeeping) is
    # what makes that guarantee mechanical rather than a promise kept by
    # hand: a metric renamed here without a matching registry edit disappears
    # from the bundle instead of silently leaking through.
    known_metrics = set(registry)
    return [r for r in rows if r["metric"] in known_metrics
            and registry[r["metric"]]["requires_labels"] is False]


def write_metrics(run_dir, rows) -> None:
    with open(Path(run_dir) / "metrics.jsonl", "w", encoding="utf-8") as fh:
        for row in sorted(rows, key=lambda r: (r["metric"], r["scope"])):
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def append_history(history_path, run_id: str, rows) -> None:
    """§8.6: each run's metrics append to one history.jsonl keyed by run_id,
    so trending doesn't require walking every run bundle."""
    with open(history_path, "a", encoding="utf-8") as fh:
        for row in sorted(rows, key=lambda r: (r["metric"], r["scope"])):
            fh.write(json.dumps({"run_id": run_id, **row},
                                sort_keys=True, separators=(",", ":")) + "\n")

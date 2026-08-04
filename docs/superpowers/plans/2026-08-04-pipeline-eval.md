# Device Fingerprinting Pipeline — Metrics & Eval Harness Plan (Phases 4–5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Prerequisite:** `docs/superpowers/plans/2026-08-04-pipeline-core.md` complete — `run.py` produces a bundle and `replay.py` exits 0.

**Goal:** Measure the pipeline without contaminating it — label-free metrics on every run, and a separate harness that scores against ground truth, diffs per-label outcomes across rule versions, and exports blinded adjudication packets.

**Architecture:** `metrics.py` runs inside the pipeline and emits only `requires_labels: false` metrics. `eval.py` is a standalone entry point that invokes `run.py` as a black box and is the only component that reads both rules and labels. Task numbering continues from Plan 1.

**Tech Stack:** Python 3.11.5, PyYAML (`safe_load` only), pytest.

## Global Constraints

- **Python 3.11.5.** `TypeVar` + `Generic[T]`, never PEP 695 syntax.
- **Invariant #5 — labels never enter the runtime path.** `run.py` and every module under `obs_pipeline/` must contain no code path that reads `labels/`. `tests/test_bundle.py::test_run_py_never_reads_labels` enforces this and must keep passing. `eval.py` and `label_tools.py` live at repo root, outside the package, for this reason.
- **Invariant #6 — pipeline output never enters hard-stratum label creation.** The adjudication packet carries evidence columns only.
- **`metrics.yaml` sits OUTSIDE the rules rollup hash** (§8.1) and carries its own `metrics_hash`. A metric definition does not affect pipeline output, so it must not change the rules version.
- **`n` is mandatory on every metric row** (§8.2). Precision of 1.00 at n=3 must not render identically to n=300.
- **Blank label cells are not assertions** (§7.2) — excluded from denominators, never counted incorrect.
- **`undecidable` labels are excluded from precision/recall denominators** (§7.2.1) and reported separately.
- Repo root is `/Users/jeffreywhitworth/work/jswhitworth/viakoo.com/obs-pipeline-01`.

## File Structure

```
metrics.yaml                  registry, outside the rules rollup
obs_pipeline/metrics.py       label-free metric emitters (inside the pipeline)
label_tools.py                wide->long import, transitivity check  (outside)
eval.py                       the harness                            (outside)
adjudicate.py                 blinded packet export + import guards  (outside)
labels/labels.csv             long-format ground truth (generated)
labels/VERSION                label-set version
tests/
  test_metrics.py  test_label_import.py  test_eval.py  test_adjudication.py
```

---

### Task 16: `metrics.yaml` registry and `metrics.py`

**Files:**
- Create: `metrics.yaml`, `obs_pipeline/metrics.py`
- Modify: `run.py` — emit `metrics.jsonl` into the bundle; `obs_pipeline/bundle.py` — add `metrics_hash` to the manifest
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: the in-memory `claims`, `memberships`, `resolved`, `entity_steps`, `stability_steps` from `run_pipeline`; `RuleSet`.
- Produces: `load_registry(path) -> dict`; `metrics_hash(path) -> str`; `emit_metrics(*, claims, memberships, resolved, obs_fields, entity_steps, stability_steps, rules, registry, trace_steps) -> list[dict]` where each row is `{"metric", "scope", "value", "n"}`; `write_metrics(run_dir, rows) -> None`; `append_history(history_path, run_id, rows) -> None`.

- [ ] **Step 1: Write `metrics.yaml`**

```yaml
# metrics.yaml -- the metric registry (design doc §8.1).
#
# Lives beside rules/ but OUTSIDE the rules rollup hash: a metric definition
# does not affect pipeline output, so it must not change the rules version.
# It carries its own metrics_hash in the manifest, so "recall dropped" can
# never silently mean "we changed how recall is computed."

field_fill_rate:
  scope_type: field
  requires_labels: false
  direction: higher_better
  description: Share of entities with a non-null value for this field

known_rate:
  scope_type: field
  requires_labels: false
  direction: higher_better
  description: >
    Share of entities whose closed-vocabulary field is NOT the escape value.
    Fill rate is structurally 100% for these fields since they are never null
    (§2.5), so field_fill_rate carries no signal there and this replaces it.

vocab_gap_rate:
  scope_type: field
  requires_labels: false
  direction: lower_better
  description: >
    Share of entities resolving to Unknown because an extracted value fell
    outside the vocabulary, as distinct from having no evidence at all. Only
    this half is actionable.

vocab_reject_frequency:
  scope_type: value
  requires_labels: false
  direction: lower_better
  description: Ranked vocabulary-expansion work queue (§6.3)

no_extraction_rate:
  scope_type: global
  requires_labels: false
  direction: lower_better
  description: Observations yielding zero extracted values (extraction dead zones)

no_identity_claim_rate:
  scope_type: global
  requires_labels: false
  direction: lower_better
  description: Observations unclusterable by construction

claims_per_obs:
  scope_type: key
  requires_labels: false
  direction: higher_better
  description: Mean claims per observation, scoped per field or link_basis

corroboration_rate:
  scope_type: key
  requires_labels: false
  direction: higher_better
  description: >
    Share of claims with >=2 independent witness groups. Reveals whether the
    independence bonus ever fires or every claim is single-source.

conflict_rate:
  scope_type: key
  requires_labels: false
  direction: lower_better
  description: Share of claims carrying a non-zero conflict penalty

source_win_rate:
  scope_type: source
  requires_labels: false
  direction: higher_better
  description: >
    Which sources produce claims that win vs. get outvoted. A source that
    never wins is redundant or mis-weighted in scoring.yaml.

entity_count:
  scope_type: global
  requires_labels: false
  direction: neutral
  description: Number of resolved entities

singleton_rate:
  scope_type: global
  requires_labels: false
  direction: neutral
  description: Share of entities with exactly one member observation

link_basis_distribution:
  scope_type: link_basis
  requires_labels: false
  direction: neutral
  description: Which bases do the clustering work

basis_agreement_rate:
  scope_type: global
  requires_labels: false
  direction: higher_better
  description: Share of observations whose bases agree on cluster assignment

cross_basis_conflict_rate:
  scope_type: global
  requires_labels: false
  direction: lower_better
  description: How often the §2.4 tie-break rule is invoked at all

propagation_depth_distribution:
  scope_type: hop
  requires_labels: false
  direction: neutral
  description: How far values travel, and the decay actually applied

confidence_distribution:
  scope_type: bucket
  requires_labels: false
  direction: neutral
  description: Confidence histogram, scoped per field

stability_distribution:
  scope_type: bucket
  requires_labels: false
  direction: neutral
  description: Stability histogram

# --- label-dependent: emitted only by eval.py (§8.4) ---

extraction_precision:
  scope_type: key
  requires_labels: true
  direction: higher_better
  description: Stage 1 -- extraction precision per field/link_basis

extraction_recall:
  scope_type: key
  requires_labels: true
  direction: higher_better
  description: Stage 1 -- extraction recall per field/link_basis

top1_claim_accuracy:
  scope_type: key
  requires_labels: true
  direction: higher_better
  description: Stage 2 -- does the highest claim_weight claim match the label

pairwise_precision:
  scope_type: global
  requires_labels: true
  direction: higher_better
  description: Stage 3 -- non-gating until the positive-pair floor is crossed (§7.4)

pairwise_recall:
  scope_type: global
  requires_labels: true
  direction: higher_better
  description: Stage 3 -- non-gating until the positive-pair floor is crossed (§7.4)

false_merge_count:
  scope_type: global
  requires_labels: true
  direction: lower_better
  description: >
    Counted SEPARATELY from false splits because their costs differ sharply
    (§2.3): a false merge corrupts every field on every member.

false_split_count:
  scope_type: global
  requires_labels: true
  direction: lower_better
  description: Same-device pairs the pipeline separated

propagated_value_accuracy:
  scope_type: field
  requires_labels: true
  direction: higher_better
  description: >
    Stage 4 -- accuracy on fields absent from an obs's own claims. Computed
    over `propagated` provenance rows only (§3.1).

confidence_calibration_error:
  scope_type: bucket
  requires_labels: true
  direction: lower_better
  description: >
    Asks whether confidence is HONEST, not whether it is high. A 0.9 bucket
    right 70% of the time is worse than emitting no confidence at all.

undecidable_rate:
  scope_type: global
  requires_labels: true
  direction: neutral
  description: >
    A coverage statistic about the SOURCES, not a rules failure. Excluded from
    precision/recall denominators; actionable in a different direction --
    add a protocol probe, not a regex.
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_metrics.py
import json

import pytest

from obs_pipeline.metrics import load_registry, metrics_hash
from run import run_pipeline


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return run_pipeline("obs-data/observations.csv", "rules",
                        tmp_path_factory.mktemp("runs"))


def _rows(bundle):
    return [json.loads(line) for line in
            (bundle / "metrics.jsonl").read_text().splitlines() if line.strip()]


def test_metrics_are_long_format_one_row_per_measurement(bundle):
    """§8.2: wide format breaks trending the moment a metric is added."""
    rows = _rows(bundle)
    assert rows
    assert all(set(r) >= {"metric", "scope", "value", "n"} for r in rows)


def test_n_is_mandatory_on_every_row(bundle):
    """§8.2: precision of 1.00 at n=3 must not render identically to n=300."""
    assert all(isinstance(r["n"], int) and r["n"] >= 0 for r in _rows(bundle))


def test_scope_carries_the_dimension_without_name_explosion(bundle):
    """§8.2: one metric name works at every granularity -- no
    fill_rate_model_propagated."""
    scopes = {r["scope"] for r in _rows(bundle)}
    assert "global" in scopes
    assert any(s.startswith("field:") for s in scopes)
    assert any(s.startswith("link_basis:") for s in scopes)
    assert not any("_propagated" in r["metric"] for r in _rows(bundle))


def test_a_plain_run_emits_only_label_free_metrics(bundle):
    """§8.1: requires_labels lets a plain run emit the label-free subset
    cleanly rather than writing nulls for metrics it structurally cannot
    compute."""
    registry = load_registry("metrics.yaml")
    for row in _rows(bundle):
        assert registry[row["metric"]]["requires_labels"] is False


def test_no_metric_row_has_a_null_value(bundle):
    assert all(r["value"] is not None for r in _rows(bundle))


def test_field_fill_rate_is_suppressed_for_closed_vocabulary_fields(bundle):
    """§8.3: fill rate is structurally 100% for vendor/device_type since they
    are never null, so it carries no signal there -- known_rate replaces it."""
    rows = _rows(bundle)
    fill_scopes = {r["scope"] for r in rows if r["metric"] == "field_fill_rate"}
    assert "field:vendor" not in fill_scopes
    assert "field:device_type" not in fill_scopes
    assert "field:model" in fill_scopes
    known_scopes = {r["scope"] for r in rows if r["metric"] == "known_rate"}
    assert {"field:vendor", "field:device_type"} <= known_scopes


def test_fill_rate_is_split_by_provenance(bundle):
    """§8.3: 90% model fill means something different if 60% of it arrived by
    propagation."""
    scopes = {r["scope"] for r in _rows(bundle) if r["metric"] == "field_fill_rate"}
    assert "field:model:propagated" in scopes
    assert "field:model:direct" in scopes


def test_vocab_reject_frequency_ranks_the_expansion_queue(bundle):
    """§6.3: the distinct set of vocab_reject values ranked by frequency."""
    rows = [r for r in _rows(bundle) if r["metric"] == "vocab_reject_frequency"]
    values = {r["scope"].split("value:", 1)[1] for r in rows}
    # Lowercase: normalization canonicalises unmapped values to the alias-map
    # key form, so the queue emits exactly what gets pasted into the rules.
    assert {"lts security", "amcrest", "wisenet"} <= values


def test_metrics_hash_is_in_the_manifest_and_separate_from_rules_rollup(bundle):
    """§8.1: 'recall dropped' can never silently mean 'we changed how recall
    is computed'."""
    m = json.loads((bundle / "manifest.json").read_text())
    assert m["metrics_hash"].startswith("sha256:")
    assert m["metrics_hash"] != m["rules_rollup"]


def test_editing_metrics_yaml_does_not_move_the_rules_rollup(bundle, tmp_path):
    """§8.1: a metric definition must not change the rules version."""
    from obs_pipeline.loader import load_rules
    before = load_rules("rules", "obs-data/observations.csv").rollup
    tweaked = tmp_path / "metrics.yaml"
    tweaked.write_text(open("metrics.yaml").read() + "\nnew_metric:\n"
                       "  scope_type: global\n  requires_labels: false\n"
                       "  direction: neutral\n  description: x\n")
    assert metrics_hash(tweaked) != metrics_hash("metrics.yaml")
    assert load_rules("rules", "obs-data/observations.csv").rollup == before
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_metrics.py -v`
Expected: FAIL — `No module named 'obs_pipeline.metrics'`

- [ ] **Step 4: Write `obs_pipeline/metrics.py`**

```python
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

FIELDS = ["vendor", "model", "device_type", "firmware"]


def load_registry(path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def metrics_hash(path) -> str:
    text = Path(path).read_text(encoding="utf-8").replace("\r\n", "\n").strip() + "\n"
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row(metric, scope, value, n):
    return {"metric": metric, "scope": scope, "value": round(float(value), 6), "n": int(n)}


def emit_metrics(*, claims, memberships, resolved, obs_fields, entity_steps,
                 stability_steps, rules, registry, trace_steps) -> list[dict]:
    rows: list[dict] = []
    closed = set(rules.claims.get("closed_vocabulary_fields", {}))
    n_obs = len(memberships)
    n_ent = len(resolved)

    # --- coverage / completeness -------------------------------------------
    for field in FIELDS:
        values = [resolved[e][field] for e in sorted(resolved)]
        if field in closed:
            escape = rules.claims["closed_vocabulary_fields"][field]["escape"]
            known = sum(1 for v in values if v.value not in (escape, "undecidable"))
            rows.append(_row("known_rate", f"field:{field}", known / max(n_ent, 1), n_ent))
        else:
            filled = sum(1 for v in values if v.value)
            rows.append(_row("field_fill_rate", f"field:{field}",
                             filled / max(n_ent, 1), n_ent))
            # §8.3: 90% model fill means something different if 60% of it
            # arrived by propagation. The split is per OBSERVATION, because
            # that is the level provenance is recorded at (§3.1).
            for prov in ("direct", "propagated"):
                n_prov = sum(1 for f in obs_fields.values()
                             if f[field].value and f[field].provenance == prov)
                rows.append(_row("field_fill_rate", f"field:{field}:{prov}",
                                 n_prov / max(n_obs, 1), n_obs))

    rejected = [s["output"] for s in trace_steps if s["op"] == "vocab_reject"]
    for value, count in Counter(rejected).most_common():
        rows.append(_row("vocab_reject_frequency", f"value:{value}",
                         count, len(rejected)))

    rejected_fields = Counter(s.get("field") for s in trace_steps
                              if s["op"] == "vocab_reject")
    for field in sorted(closed):
        escape = rules.claims["closed_vocabulary_fields"][field]["escape"]
        unknown_entities = [e for e in resolved if resolved[e][field].value == escape]
        gap = sum(1 for e in unknown_entities
                  if any(m.entity_id == e for m in memberships)) if rejected_fields[field] else 0
        rows.append(_row("vocab_gap_rate", f"field:{field}",
                         min(gap, rejected_fields[field]) / max(n_ent, 1), n_ent))

    no_extract = sum(1 for s in trace_steps if s["op"] == "no_extraction")
    no_ident = sum(1 for s in trace_steps if s["op"] == "no_identity_claim")
    rows.append(_row("no_extraction_rate", "global", no_extract / max(n_obs, 1), n_obs))
    rows.append(_row("no_identity_claim_rate", "global", no_ident / max(n_obs, 1), n_obs))

    # --- evidence structure -------------------------------------------------
    by_key: dict[tuple[str, str], list] = defaultdict(list)
    for c in claims:
        by_key[(c.kind, c.key)].append(c)
    for (kind, key), group in sorted(by_key.items()):
        scope = f"{'field' if kind == 'field' else 'link_basis'}:{key}"
        rows.append(_row("claims_per_obs", scope, len(group) / max(n_obs, 1), n_obs))
        corroborated = sum(1 for c in group if len(c.witness_groups) >= 2)
        rows.append(_row("corroboration_rate", scope,
                         corroborated / max(len(group), 1), len(group)))

    penalties = {s["step_id"]: s["decomposition"]["penalty"]
                 for s in trace_steps if s["op"] == "score"}
    for (kind, key), group in sorted(by_key.items()):
        scope = f"{'field' if kind == 'field' else 'link_basis'}:{key}"
        conflicted = sum(1 for c in group if penalties.get(c.traced.step_id, 0) > 0)
        rows.append(_row("conflict_rate", scope,
                         conflicted / max(len(group), 1), len(group)))

    winners = Counter()
    for entity_id in resolved:
        for field in FIELDS:
            f = resolved[entity_id][field]
            match = [c for c in claims if c.key == field and c.value == f.value]
            for c in match:
                for src in c.sources:
                    winners[src] += 1
    all_sources = Counter(src for c in claims for src in c.sources)
    for src, total in sorted(all_sources.items()):
        rows.append(_row("source_win_rate", f"source:{src}",
                         winners[src] / max(total, 1), total))

    # --- clustering shape ---------------------------------------------------
    sizes = Counter(m.entity_id for m in memberships)
    rows.append(_row("entity_count", "global", n_ent, n_ent))
    rows.append(_row("singleton_rate", "global",
                     sum(1 for v in sizes.values() if v == 1) / max(n_ent, 1), n_ent))

    basis = Counter(m.link_basis for m in memberships)
    for b, count in sorted(basis.items()):
        rows.append(_row("link_basis_distribution", f"link_basis:{b}",
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

    # --- confidence ---------------------------------------------------------
    def _bucket(x):
        return f"{min(int(x * 10) / 10, 0.9):.1f}"

    for field in FIELDS:
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

    known = set(registry)
    return [r for r in rows if r["metric"] in known
            and registry[r["metric"]]["requires_labels"] is False]


def write_metrics(run_dir, rows) -> None:
    with open(Path(run_dir) / "metrics.jsonl", "w", encoding="utf-8") as fh:
        for row in sorted(rows, key=lambda r: (r["metric"], r["scope"])):
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def append_history(history_path, run_id: str, rows) -> None:
    """§8.6: each run's metrics append to one history.jsonl keyed by run_id, so
    trending doesn't require walking every run bundle."""
    with open(history_path, "a", encoding="utf-8") as fh:
        for row in sorted(rows, key=lambda r: (r["metric"], r["scope"])):
            fh.write(json.dumps({"run_id": run_id, **row},
                                sort_keys=True, separators=(",", ":")) + "\n")
```

- [ ] **Step 5: Wire into `run.py` and `bundle.py`**

In `obs_pipeline/bundle.py`, add a `metrics_hash` parameter to `make_manifest`:

```python
def make_manifest(rules, input_hash: str, run_id: str, engine_commit: str,
                  metrics_hash: str) -> dict:
    return {
        "run_id": run_id,
        "input_hash": input_hash,
        "rules_version": rules.version,
        "rules_rollup": rules.rollup,
        "rules_files": dict(sorted(rules.file_hashes.items())),
        "metrics_hash": metrics_hash,
        "version_verified": rules.version_verified,
        "engine_commit": engine_commit,
    }
```

In `run.py`, add the imports:

```python
from obs_pipeline.metrics import (
    append_history, emit_metrics, load_registry, metrics_hash, write_metrics,
)
```

and replace the manifest/write block with:

```python
    registry = load_registry("metrics.yaml")
    manifest = make_manifest(rules, input_hash, run_id, _engine_commit(),
                             metrics_hash("metrics.yaml"))

    run_dir = Path(out_root) / run_id
    write_bundle(run_dir, manifest=manifest, claims=claims,
                 memberships=memberships, resolved=resolved,
                 obs_fields=obs_fields, entity_steps=entity_steps,
                 stability_steps=stability_steps, tracer=tracer)

    rows = emit_metrics(claims=claims, memberships=memberships, resolved=resolved,
                        obs_fields=obs_fields, entity_steps=entity_steps,
                        stability_steps=stability_steps, rules=rules,
                        registry=registry, trace_steps=tracer.steps())
    write_metrics(run_dir, rows)
    append_history(Path(out_root) / "history.jsonl", run_id, rows)

    write_report(run_dir)
    return run_dir
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python3 -m pytest tests/test_metrics.py -v`
Expected: PASS, 10 tests

- [ ] **Step 7: Confirm the earlier suites still pass**

Run: `python3 -m pytest -q`
Expected: all pass. `tests/test_bundle.py::test_manifest_omits_label_identifiers` must still pass — `metrics_hash` is not a label identifier.

- [ ] **Step 8: Commit**

```bash
git add metrics.yaml obs_pipeline/metrics.py obs_pipeline/bundle.py run.py tests/test_metrics.py
git commit -m "feat: metric registry and label-free metric emission"
```

---

### Task 17: `history.jsonl` trend store

**Files:**
- Test: `tests/test_history.py`

**Interfaces:**
- Consumes: `obs_pipeline.metrics.append_history` (written in Task 16).
- Produces: nothing new — this task proves the trend store behaves.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_history.py
"""§8.6: each run's metrics.jsonl appends to a single history.jsonl keyed by
run_id, so trending doesn't require walking every run bundle. Joined to
manifest.json this yields metrics-by-rule-version directly."""
import json

from run import run_pipeline


def test_history_accumulates_across_runs(tmp_path):
    root = tmp_path / "runs"
    a = run_pipeline("obs-data/observations.csv", "rules", root)
    b = run_pipeline("obs-data/observations.csv", "rules", root)
    rows = [json.loads(line) for line in
            (root / "history.jsonl").read_text().splitlines() if line.strip()]
    run_ids = {r["run_id"] for r in rows}
    assert {a.name, b.name} <= run_ids


def test_every_history_row_carries_run_id_metric_scope_value_n(tmp_path):
    root = tmp_path / "runs"
    run_pipeline("obs-data/observations.csv", "rules", root)
    rows = [json.loads(line) for line in
            (root / "history.jsonl").read_text().splitlines() if line.strip()]
    assert rows
    assert all(set(r) >= {"run_id", "metric", "scope", "value", "n"} for r in rows)


def test_history_joins_to_manifest_for_metrics_by_rule_version(tmp_path):
    """'singleton rate jumped at rules 1.4.0' should fall out of a query."""
    root = tmp_path / "runs"
    d = run_pipeline("obs-data/observations.csv", "rules", root)
    manifest = json.loads((d / "manifest.json").read_text())
    rows = [json.loads(line) for line in
            (root / "history.jsonl").read_text().splitlines() if line.strip()]
    mine = [r for r in rows if r["run_id"] == manifest["run_id"]]
    singleton = next(r for r in mine if r["metric"] == "singleton_rate")
    assert 0.0 <= singleton["value"] <= 1.0
    assert singleton["n"] > 0
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `python3 -m pytest tests/test_history.py -v`
Expected: PASS if Task 16's `append_history` wiring is correct. If it fails, fix the wiring in `run.py` — do not weaken the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_history.py
git commit -m "test: history.jsonl trend store"
```

---

### Task 18: Label import and consistency checks

**Files:**
- Create: `label_tools.py`, `labels/VERSION`
- Test: `tests/test_label_import.py`

**Interfaces:**
- Consumes: `obs-data/observations.csv`, `labels/labels-initial.csv`.
- Produces: `import_wide_labels(wide_path, observations_path, out_path) -> list[dict]`; `load_labels(path) -> list[dict]`; `labels_hash(path) -> str`; `obs_hash(row) -> str`; `check_transitivity(labels) -> list[str]`; `TransitivityError(Exception)`.

- [ ] **Step 1: Write `labels/VERSION`**

```
0.1.0
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_label_import.py
import csv

import pytest

from label_tools import (
    check_transitivity, import_wide_labels, load_labels, obs_hash,
)

WIDE = "labels/labels-initial.csv"
OBS = "obs-data/observations.csv"


@pytest.fixture(scope="module")
def labels(tmp_path_factory):
    out = tmp_path_factory.mktemp("labels") / "labels.csv"
    import_wide_labels(WIDE, OBS, out)
    return load_labels(out)


def test_long_format_is_authoritative(labels):
    """§7.2: a wide per-observation file is a RENDERING of this schema, not
    the schema. Wide files carry no status, label_basis or blinded."""
    required = {"obs_id", "key_type", "key", "value", "status", "label_basis",
                "labeler_certainty", "blinded", "obs_hash", "labeled_by",
                "labeled_at"}
    assert required <= set(labels[0])


def test_one_row_per_asserted_field_not_per_observation(labels):
    assert len(labels) > 74
    assert {r["key_type"] for r in labels} <= {"field", "link_basis"}


def test_blank_cells_produce_no_row_at_all(labels):
    """§7.2: a blank is not an assertion. Blank cells mean 'not derivable from
    THIS payload' and are excluded from denominators -- never counted as
    incorrect."""
    with open(WIDE, newline="", encoding="utf-8") as fh:
        wide = {r["obs_id"]: r for r in csv.DictReader(fh)}
    assert wide["OBS-003"]["firmware"] == ""
    assert not [r for r in labels
                if r["obs_id"] == "OBS-003" and r["key"] == "firmware"]


def test_provenance_is_stamped_honestly_not_optimistically(labels):
    """§7.2: on import from a wide file, stamp status: proposed,
    label_basis: payload_inference, blinded: false."""
    assert {r["status"] for r in labels} == {"proposed"}
    assert {r["label_basis"] for r in labels} == {"payload_inference"}
    assert {r["blinded"] for r in labels} == {"false"}


def test_confidence_column_is_imported_as_labeler_certainty(labels):
    """§7.2.3: the wide file's high|medium|low column is the LABELER's own
    difficulty assessment, not a pipeline confidence."""
    assert {r["labeler_certainty"] for r in labels} <= {"high", "medium", "low"}
    mine = [r for r in labels if r["obs_id"] == "OBS-002"]
    assert all(r["labeler_certainty"] == "medium" for r in mine)


def test_dual_label_stratum_is_medium_plus_low(labels):
    """§7.2.3: high -> single-label; medium and low -> dual-label and
    blind-adjudicate. 55 high / 14 medium / 5 low in the initial file."""
    by_obs = {r["obs_id"]: r["labeler_certainty"] for r in labels}
    assert sum(1 for v in by_obs.values() if v == "high") == 55
    assert sum(1 for v in by_obs.values() if v in ("medium", "low")) == 19


def test_obs_hash_binds_each_label_to_the_evidence_it_was_made_against(labels):
    """§7.7 round-trip guard: a label whose obs_hash no longer matches the
    current observations row is stale."""
    assert all(r["obs_hash"].startswith("sha256:") for r in labels)
    assert len({r["obs_hash"] for r in labels}) == 74


def test_entity_id_becomes_pairwise_link_basis_labels(labels):
    """Stage 3 needs same-device judgments, not ID strings -- §2.4 says the
    harness matches on the PARTITION, never on ID strings."""
    ident = [r for r in labels if r["key_type"] == "link_basis"]
    assert ident
    assert {r["key"] for r in ident} == {"same_device"}
    pairs = {tuple(sorted([r["obs_id"], r["value"]])) for r in ident}
    assert ("OBS-001", "OBS-002") in pairs
    assert ("OBS-069", "OBS-074") in pairs


def test_positive_pair_count_matches_the_designs_stated_figure(labels):
    """§7.4: 68 entities over 74 observations, 63 singletons, 5
    multi-observation entities covering 11 observations -- roughly 5 positive
    pairs. Any Stage 3 threshold 'met' at that n is noise."""
    pairs = {tuple(sorted([r["obs_id"], r["value"]]))
             for r in labels if r["key"] == "same_device"}
    assert len(pairs) == 6      # 3 from E-001 (C(3,2)) + 1 each from the other four


def test_transitivity_violation_is_detected_mechanically():
    """§7.2.4: a labeler asserts A~B and A~C but B!~C. Detecting it requires
    no adjudicator, and it must run BEFORE the labels are used -- Stage 3
    validation against an inconsistent label set produces meaningless
    precision numbers."""
    bad = [
        {"obs_id": "A", "key_type": "link_basis", "key": "same_device",
         "value": "B", "status": "proposed"},
        {"obs_id": "A", "key_type": "link_basis", "key": "same_device",
         "value": "C", "status": "proposed"},
    ]
    problems = check_transitivity(bad)
    assert problems
    assert any("B" in p and "C" in p for p in problems)


def test_imported_labels_are_transitively_consistent(labels):
    assert check_transitivity(labels) == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_label_import.py -v`
Expected: FAIL — `No module named 'label_tools'`

- [ ] **Step 4: Write the implementation**

```python
#!/usr/bin/env python3
"""Label import and consistency checks (design doc §7.2, §7.2.4).

Lives OUTSIDE obs_pipeline/ deliberately: invariant #5 says no code path in
the pipeline reads labels, and keeping this module out of the package makes
that structural rather than conventional.
"""
from __future__ import annotations

import csv
import hashlib
from itertools import combinations
from pathlib import Path

LONG_HEADER = ["obs_id", "key_type", "key", "value", "status", "label_basis",
               "labeler_certainty", "blinded", "obs_hash", "labeled_by",
               "labeled_at"]

WIDE_FIELDS = ["vendor", "model", "device_type", "firmware"]


class TransitivityError(Exception):
    """The label set is internally incoherent (§7.2.4)."""


def obs_hash(row: dict) -> str:
    payload = "|".join(str(row.get(k, "")) for k in sorted(row))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def labels_hash(path) -> str:
    text = Path(path).read_text(encoding="utf-8").replace("\r\n", "\n").strip() + "\n"
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def import_wide_labels(wide_path, observations_path, out_path) -> list[dict]:
    with open(observations_path, newline="", encoding="utf-8") as fh:
        obs_hashes = {r["obs_id"]: obs_hash(r) for r in csv.DictReader(fh)}
    with open(wide_path, newline="", encoding="utf-8") as fh:
        wide = list(csv.DictReader(fh))

    rows: list[dict] = []
    by_entity: dict[str, list[str]] = {}

    for r in wide:
        obs_id = r["obs_id"]
        # The wide file's `confidence` column is the LABELER's difficulty
        # assessment (§7.2.3), not a pipeline confidence.
        certainty = (r.get("confidence") or "").strip().lower()
        base = {
            "obs_id": obs_id,
            "status": "proposed",             # §7.2 -- stamped honestly
            "label_basis": "payload_inference",
            "labeler_certainty": certainty,
            "blinded": "false",
            "obs_hash": obs_hashes[obs_id],
            "labeled_by": "import:labels-initial.csv",
            "labeled_at": "",
        }
        for field in WIDE_FIELDS:
            value = (r.get(field) or "").strip()
            if not value:
                continue              # a blank is NOT an assertion (§7.2)
            rows.append({**base, "key_type": "field", "key": field, "value": value})
        entity = (r.get("entity_id") or "").strip()
        if entity:
            by_entity.setdefault(entity, []).append(obs_id)

    # Entity ids become PAIRWISE same-device judgments: §2.4 says the harness
    # matches on the partition, never on ID strings.
    for entity, members in sorted(by_entity.items()):
        for a, b in combinations(sorted(members), 2):
            rows.append({
                "obs_id": a, "key_type": "link_basis", "key": "same_device",
                "value": b, "status": "proposed",
                "label_basis": "payload_inference",
                "labeler_certainty": "high", "blinded": "false",
                "obs_hash": obs_hashes[a],
                "labeled_by": "import:labels-initial.csv", "labeled_at": "",
            })

    rows.sort(key=lambda r: (r["obs_id"], r["key_type"], r["key"], r["value"]))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LONG_HEADER)
        w.writeheader()
        w.writerows(rows)
    return rows


def load_labels(path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def check_transitivity(labels) -> list[str]:
    """§7.2.4 -- A~B and A~C but B!~C violates transitivity regardless of who
    is right, and detecting it requires no adjudicator."""
    same = {tuple(sorted([r["obs_id"], r["value"]]))
            for r in labels if r.get("key") == "same_device"}
    neighbors: dict[str, set[str]] = {}
    for a, b in same:
        neighbors.setdefault(a, set()).add(b)
        neighbors.setdefault(b, set()).add(a)

    problems: list[str] = []
    for node, adj in sorted(neighbors.items()):
        for x, y in combinations(sorted(adj), 2):
            if tuple(sorted([x, y])) not in same:
                problems.append(
                    f"transitivity: {node}~{x} and {node}~{y} but not {x}~{y}"
                )
    return sorted(set(problems))


if __name__ == "__main__":
    imported = import_wide_labels("labels/labels-initial.csv",
                                  "obs-data/observations.csv",
                                  "labels/labels.csv")
    problems = check_transitivity(imported)
    print(f"imported {len(imported)} labels")
    for p in problems:
        print(p)
    if problems:
        raise TransitivityError(f"{len(problems)} transitivity violations")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_label_import.py -v`
Expected: PASS, 11 tests

- [ ] **Step 6: Generate the long-format label file**

Run: `python3 label_tools.py`
Expected: `imported N labels` with no transitivity violations

- [ ] **Step 7: Commit**

```bash
git add label_tools.py labels/VERSION labels/labels.csv tests/test_label_import.py
git commit -m "feat: wide-to-long label import with transitivity check"
```

---

### Task 19: `eval.py` — the harness

**Files:**
- Create: `eval.py`
- Test: `tests/test_eval.py`

**Interfaces:**
- Consumes: `run.run_pipeline` (as a black box), `label_tools.*`, `obs_pipeline.metrics.load_registry`.
- Produces: `evaluate(observations_path, rules_dir, labels_path, out_root) -> Path` (returns the eval bundle dir); `score_against_labels(run_dir, labels) -> tuple[list[dict], dict]` returning `(metric_rows, per_label_outcomes)` where an outcome is `{"obs_id","key_type","key","expected","actual","correct","provenance"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_eval.py
import json

import pytest

from eval import evaluate
from obs_pipeline.metrics import load_registry


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return evaluate("obs-data/observations.csv", "rules", "labels/labels.csv",
                    tmp_path_factory.mktemp("evals"))


def _rows(bundle):
    return [json.loads(line) for line in
            (bundle / "metrics.jsonl").read_text().splitlines() if line.strip()]


def test_harness_emits_its_own_parallel_bundle(bundle):
    """§3: the eval harness emits its own bundle under evals/<eval_id>/."""
    for name in ["eval_manifest.json", "metrics.jsonl", "regressions.csv"]:
        assert (bundle / name).exists(), name


def test_eval_manifest_versions_both_sides(bundle):
    """§7.6: the harness is the only component that reads both rules and
    labels, so it emits its own manifest recording both."""
    m = json.loads((bundle / "eval_manifest.json").read_text())
    assert m["rules_rollup"].startswith("sha256:")
    assert m["labels_hash"].startswith("sha256:")
    assert m["labels_version"] == "0.1.0"
    assert m["run_id"]


def test_eval_emits_label_dependent_metrics(bundle):
    registry = load_registry("metrics.yaml")
    metrics = {r["metric"] for r in _rows(bundle)}
    assert any(registry[m]["requires_labels"] for m in metrics)
    assert "top1_claim_accuracy" in metrics


def test_stage3_counts_false_merge_and_false_split_separately(bundle):
    """§8.4: their costs differ sharply -- a false merge corrupts every field
    on every member via propagation."""
    metrics = {r["metric"] for r in _rows(bundle)}
    assert "false_merge_count" in metrics
    assert "false_split_count" in metrics


def test_stage3_reports_the_positive_pair_count_so_n_is_visible(bundle):
    """§7.4: any threshold 'met' at ~5 positive pairs is noise, so n must
    travel with the number."""
    row = next(r for r in _rows(bundle) if r["metric"] == "pairwise_precision")
    assert row["n"] < 30       # below the §11 defensible floor
    assert row["n"] == 6


def test_blank_labels_are_excluded_from_denominators(bundle):
    """§7.2: scoring a correctly sibling-propagated value as wrong against a
    blank would break Stage 4 precisely where it is meant to work."""
    row = next(r for r in _rows(bundle)
               if r["metric"] == "top1_claim_accuracy" and r["scope"] == "field:firmware")
    assert row["n"] < 74       # OBS-003, OBS-005 etc. have blank firmware


def test_stage1_and_2_score_direct_fields_only(bundle):
    """§3.1: diffing a propagated row naively against labels would credit the
    pipeline for extraction it never performed."""
    scopes = {r["scope"] for r in _rows(bundle) if r["metric"] == "extraction_recall"}
    assert any(s.endswith(":direct") for s in scopes)


def test_stage4_scores_propagated_fields_only(bundle):
    rows = [r for r in _rows(bundle) if r["metric"] == "propagated_value_accuracy"]
    assert rows
    assert all(r["n"] >= 0 for r in rows)


def test_undecidable_is_excluded_from_denominators_and_reported_separately(bundle):
    """§7.2.1: forcing these to a single correct_value scores the rules
    against something unlearnable from the observation."""
    metrics = {r["metric"] for r in _rows(bundle)}
    assert "undecidable_rate" in metrics


def test_firmware_undecidable_entities_are_not_scored_wrong(bundle):
    """§2.5: the per-observation readings remain individually correct; the
    ambiguity exists at the entity level only."""
    outcomes = json.loads((bundle / "outcomes.json").read_text())
    for o in outcomes:
        if o["obs_id"] in ("OBS-069", "OBS-074") and o["key"] == "firmware":
            assert o["correct"] is not False


def test_eval_py_does_not_live_inside_the_package():
    """§7.6: eval.py is a separate entry point, NOT a flag on run.py. This
    keeps invariant #5 structural rather than conventional."""
    from pathlib import Path
    assert Path("eval.py").exists()
    assert not Path("obs_pipeline/eval.py").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_eval.py -v`
Expected: FAIL — `No module named 'eval'`

- [ ] **Step 3: Write the implementation**

```python
#!/usr/bin/env python3
"""The eval harness (design doc §7.6, §8.4).

Sits OUTSIDE the pipeline. Invokes run.py as a black box and scores its
outputs against labels.csv. It is not a flag on run.py: because the pipeline
has no code path that reads labels, ground truth cannot leak into scoring
(invariant #5), and the deterministic runtime path stays free of evaluation
logic.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from label_tools import labels_hash, load_labels
from obs_pipeline.metrics import load_registry
from run import run_pipeline

FIELDS = ["vendor", "model", "device_type", "firmware"]
UNDECIDABLE = "undecidable"


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _row(metric, scope, value, n):
    return {"metric": metric, "scope": scope, "value": round(float(value), 6),
            "n": int(n)}


def score_against_labels(run_dir, labels):
    resolutions = {r["obs_id"]: r for r in _read_csv(Path(run_dir) / "resolutions.csv")}
    claims = _read_csv(Path(run_dir) / "claims.csv")

    top1: dict[str, dict[str, str]] = defaultdict(dict)
    best: dict[tuple[str, str], float] = {}
    for c in claims:
        if c["kind"] != "field":
            continue
        key = (c["obs_id"], c["key"])
        weight = float(c["weight"])
        if weight > best.get(key, -1.0):
            best[key] = weight
            top1[c["obs_id"]][c["key"]] = c["value"]

    outcomes = []
    for label in labels:
        if label["key_type"] != "field":
            continue
        obs_id, field, expected = label["obs_id"], label["key"], label["value"]
        if obs_id not in resolutions:
            continue
        row = resolutions[obs_id]
        actual = row[field]
        provenance = row[f"{field}_provenance"]

        # §7.2.1 / §2.5: undecidable is excluded from denominators entirely.
        if actual == UNDECIDABLE or label["status"] == "undecidable":
            correct = None
        else:
            correct = actual == expected

        outcomes.append({
            "obs_id": obs_id, "key_type": "field", "key": field,
            "expected": expected, "actual": actual, "correct": correct,
            "provenance": provenance,
            "top1_claim": top1.get(obs_id, {}).get(field, ""),
            "labeler_certainty": label["labeler_certainty"],
            "confidence": float(row["confidence"]),
            "stability": float(row["stability"]),
        })

    rows: list[dict] = []

    # --- Stage 1 & 2: direct fields only (§3.1) ----------------------------
    for field in FIELDS:
        scored = [o for o in outcomes if o["key"] == field and o["correct"] is not None]
        direct = [o for o in scored if o["provenance"] == "direct"]
        if direct:
            hit = sum(1 for o in direct if o["correct"])
            rows.append(_row("extraction_recall", f"field:{field}:direct",
                             hit / len(direct), len(direct)))
            emitted = [o for o in direct if o["actual"] not in ("Unknown", "unknown", "")]
            if emitted:
                rows.append(_row("extraction_precision", f"field:{field}:direct",
                                 sum(1 for o in emitted if o["correct"]) / len(emitted),
                                 len(emitted)))
        if scored:
            hit = sum(1 for o in scored if o["top1_claim"] == o["expected"])
            rows.append(_row("top1_claim_accuracy", f"field:{field}",
                             hit / len(scored), len(scored)))

    # --- Stage 4: propagated fields only (§3.1) ----------------------------
    for field in FIELDS:
        prop = [o for o in outcomes
                if o["key"] == field and o["correct"] is not None
                and o["provenance"] == "propagated"]
        rows.append(_row("propagated_value_accuracy", f"field:{field}",
                         (sum(1 for o in prop if o["correct"]) / len(prop)) if prop else 0.0,
                         len(prop)))

    # --- Stage 3: pairwise, non-gating (§7.4) ------------------------------
    truth_pairs = {tuple(sorted([l["obs_id"], l["value"]]))
                   for l in labels if l.get("key") == "same_device"}
    entity_of = {r["obs_id"]: r["entity_id"] for r in resolutions.values()}
    predicted_pairs = {
        tuple(sorted([a, b]))
        for a, b in combinations(sorted(entity_of), 2)
        if entity_of[a] == entity_of[b]
    }
    tp = len(truth_pairs & predicted_pairs)
    false_merges = predicted_pairs - truth_pairs
    false_splits = truth_pairs - predicted_pairs

    rows.append(_row("pairwise_precision", "global",
                     tp / max(len(predicted_pairs), 1), len(truth_pairs)))
    rows.append(_row("pairwise_recall", "global",
                     tp / max(len(truth_pairs), 1), len(truth_pairs)))
    rows.append(_row("false_merge_count", "global", len(false_merges),
                     len(predicted_pairs)))
    rows.append(_row("false_split_count", "global", len(false_splits),
                     len(truth_pairs)))

    # --- coverage statistic, not a rules failure (§7.2.1) ------------------
    total = len([o for o in outcomes])
    undec = len([o for o in outcomes if o["correct"] is None])
    rows.append(_row("undecidable_rate", "global",
                     undec / max(total, 1), total))

    # --- calibration: is confidence HONEST, not high (§8.4) ----------------
    buckets: dict[str, list] = defaultdict(list)
    for o in outcomes:
        if o["correct"] is None:
            continue
        buckets[f"{min(int(o['confidence'] * 10) / 10, 0.9):.1f}"].append(o["correct"])
    for b, results in sorted(buckets.items()):
        observed = sum(1 for r in results if r) / len(results)
        rows.append(_row("confidence_calibration_error", f"bucket:{b}",
                         abs(observed - float(b)), len(results)))

    return rows, outcomes


def evaluate(observations_path, rules_dir, labels_path, out_root,
             runs_root="runs") -> Path:
    run_dir = run_pipeline(observations_path, rules_dir, runs_root)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    labels = load_labels(labels_path)

    rows, outcomes = score_against_labels(run_dir, labels)
    registry = load_registry("metrics.yaml")
    rows = [r for r in rows if r["metric"] in registry]

    eval_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = Path(out_root) / eval_id
    out.mkdir(parents=True, exist_ok=True)

    eval_manifest = {
        "eval_id": eval_id,
        "run_id": manifest["run_id"],
        "rules_rollup": manifest["rules_rollup"],
        "labels_version": Path("labels/VERSION").read_text(encoding="utf-8").strip(),
        "labels_hash": labels_hash(labels_path),
        "baseline_run_id": None,
        "baseline_labels_hash": None,
    }
    (out / "eval_manifest.json").write_text(
        json.dumps(eval_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with open(out / "metrics.jsonl", "w", encoding="utf-8") as fh:
        for r in sorted(rows, key=lambda r: (r["metric"], r["scope"])):
            fh.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")

    (out / "outcomes.json").write_text(
        json.dumps(outcomes, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Written empty here; Task 20 fills it from the four-bucket diff.
    with open(out / "regressions.csv", "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow(
            ["obs_id", "key_type", "key", "before", "after", "changed_rule_files"])

    return out


if __name__ == "__main__":
    print(evaluate(
        sys.argv[1] if len(sys.argv) > 1 else "obs-data/observations.csv",
        sys.argv[2] if len(sys.argv) > 2 else "rules",
        sys.argv[3] if len(sys.argv) > 3 else "labels/labels.csv",
        sys.argv[4] if len(sys.argv) > 4 else "evals",
    ))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_eval.py -v`
Expected: PASS, 11 tests

- [ ] **Step 5: Run the harness and read the numbers**

Run:
```bash
python3 eval.py
python3 -c "
import json,sys,pathlib
d=sorted(pathlib.Path('evals').iterdir())[-1]
for line in (d/'metrics.jsonl').read_text().splitlines():
    r=json.loads(line); print(f\"{r['metric']:32} {r['scope']:28} {r['value']:.3f}  n={r['n']}\")
"
```
Expected: `false_merge_count` is 0 and `pairwise_recall` is 1.0 at n=6. Per §7.4 these are **reported but non-gating** — n=6 is far below the n≥30 floor and any threshold met here is noise. Do not tune rules against them.

- [ ] **Step 6: Commit**

```bash
git add eval.py tests/test_eval.py
git commit -m "feat: eval harness with stage-mapped label-dependent metrics"
```

---

### Task 20: Four-bucket diff, `regressions.csv`, version-bump proposal

**Files:**
- Modify: `eval.py` — add `four_bucket_diff`, `propose_bump`, `write_regressions`, and baseline handling in `evaluate`
- Test: `tests/test_four_bucket.py`

**Interfaces:**
- Consumes: two outcome lists from `score_against_labels`.
- Produces: `four_bucket_diff(before, after) -> dict[str, list[dict]]` keyed `fixed | broken | stable_correct | stable_incorrect`; `propose_bump(diff, before_rules, after_rules, before_vocab, after_vocab) -> str` returning `"major" | "minor" | "patch"`; `LabelsMovedError(Exception)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_four_bucket.py
import pytest

from eval import LabelsMovedError, four_bucket_diff, propose_bump


def _o(obs_id, key, correct):
    return {"obs_id": obs_id, "key_type": "field", "key": key,
            "expected": "X", "actual": "X" if correct else "Y",
            "correct": correct, "provenance": "direct"}


def test_classifies_every_label_into_one_of_four_buckets():
    before = [_o("A", "vendor", False), _o("B", "vendor", True),
              _o("C", "vendor", True), _o("D", "vendor", False)]
    after = [_o("A", "vendor", True), _o("B", "vendor", False),
             _o("C", "vendor", True), _o("D", "vendor", False)]
    d = four_bucket_diff(before, after)
    assert [o["obs_id"] for o in d["fixed"]] == ["A"]
    assert [o["obs_id"] for o in d["broken"]] == ["B"]
    assert [o["obs_id"] for o in d["stable_correct"]] == ["C"]
    assert [o["obs_id"] for o in d["stable_incorrect"]] == ["D"]


def test_aggregate_improvement_does_not_hide_specific_breakage():
    """§7.6: a widened regex that fixes ten observations and breaks two nets
    +8 and looks like an unambiguous win. The aggregate hides the two."""
    before = [_o(f"F{i}", "model", False) for i in range(10)] + \
             [_o(f"B{i}", "model", True) for i in range(2)]
    after = [_o(f"F{i}", "model", True) for i in range(10)] + \
            [_o(f"B{i}", "model", False) for i in range(2)]
    d = four_bucket_diff(before, after)
    assert len(d["fixed"]) == 10
    assert len(d["broken"]) == 2      # visible, not netted away


def test_undecidable_outcomes_are_not_bucketed():
    before = [{"obs_id": "A", "key_type": "field", "key": "firmware",
               "correct": None, "expected": "1", "actual": "undecidable",
               "provenance": "unknown"}]
    after = list(before)
    d = four_bucket_diff(before, after)
    assert sum(len(v) for v in d.values()) == 0


def test_behavior_neutral_change_proposes_patch():
    d = {"fixed": [], "broken": [], "stable_correct": [_o("A", "vendor", True)],
         "stable_incorrect": []}
    assert propose_bump(d, before_keys={"vendor"}, after_keys={"vendor"},
                        before_vocab={"Axis Communications"},
                        after_vocab={"Axis Communications"}) == "patch"


def test_outcome_change_with_vocabulary_intact_proposes_minor():
    d = {"fixed": [_o("A", "vendor", True)], "broken": [],
         "stable_correct": [], "stable_incorrect": []}
    assert propose_bump(d, before_keys={"vendor"}, after_keys={"vendor"},
                        before_vocab={"Axis Communications"},
                        after_vocab={"Axis Communications"}) == "minor"


def test_widened_vocabulary_proposes_minor():
    """Additive; existing values keep resolving as before."""
    d = {"fixed": [], "broken": [], "stable_correct": [], "stable_incorrect": []}
    assert propose_bump(d, before_keys={"vendor"}, after_keys={"vendor"},
                        before_vocab={"Axis"}, after_vocab={"Axis", "Ruckus"}) == "minor"


def test_narrowed_vocabulary_proposes_major():
    """§7.6: a value a consumer previously saw can no longer be emitted --
    same breakage class as a schema change, even though the schema is
    untouched."""
    d = {"fixed": [], "broken": [], "stable_correct": [], "stable_incorrect": []}
    assert propose_bump(d, before_keys={"vendor"}, after_keys={"vendor"},
                        before_vocab={"Axis", "Ruckus"}, after_vocab={"Axis"}) == "major"


def test_changed_claims_vocabulary_proposes_major():
    """Downstream consumers of the entity pool or membership.csv may break."""
    d = {"fixed": [], "broken": [], "stable_correct": [], "stable_incorrect": []}
    assert propose_bump(d, before_keys={"vendor", "model"}, after_keys={"vendor"},
                        before_vocab={"Axis"}, after_vocab={"Axis"}) == "major"


def test_diff_refuses_to_run_across_differing_label_hashes():
    """§7.6: if a label flips from Dahua to Hikvision between eval runs, the
    case lands in `broken` and reads as a rule regression when in fact the
    ground truth moved. The harness must refuse the single-axis diff."""
    with pytest.raises(LabelsMovedError):
        four_bucket_diff([], [], before_labels_hash="sha256:aaa",
                         after_labels_hash="sha256:bbb")


def test_diff_permits_matching_label_hashes():
    d = four_bucket_diff([], [], before_labels_hash="sha256:aaa",
                         after_labels_hash="sha256:aaa")
    assert sum(len(v) for v in d.values()) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_four_bucket.py -v`
Expected: FAIL — `cannot import name 'four_bucket_diff'`

- [ ] **Step 3: Add to `eval.py`**

Append these to `eval.py`, above the `evaluate` function:

```python
class LabelsMovedError(Exception):
    """Both rules and labels changed; a single-axis diff would misattribute
    the cause (§7.6)."""


def four_bucket_diff(before, after, *, before_labels_hash=None,
                     after_labels_hash=None) -> dict[str, list[dict]]:
    """§7.6 -- aggregate metrics are insufficient on their own.

    A rule change can improve overall precision/recall while silently breaking
    specific cases that previously resolved correctly. This diffs PER-LABEL
    outcomes, not totals.
    """
    if (before_labels_hash is not None and after_labels_hash is not None
            and before_labels_hash != after_labels_hash):
        raise LabelsMovedError(
            "labels_hash differs between runs; run two passes instead -- "
            "rules-held-constant to isolate the label delta, and "
            "labels-held-constant to isolate the rule delta (§7.6)"
        )

    def key(o):
        return (o["obs_id"], o["key_type"], o["key"])

    b = {key(o): o for o in before if o["correct"] is not None}
    a = {key(o): o for o in after if o["correct"] is not None}

    out: dict[str, list[dict]] = {
        "fixed": [], "broken": [], "stable_correct": [], "stable_incorrect": [],
    }
    for k in sorted(set(b) & set(a)):
        was, now = b[k]["correct"], a[k]["correct"]
        bucket = ("stable_correct" if was and now else
                  "stable_incorrect" if not was and not now else
                  "fixed" if now else "broken")
        out[bucket].append({**a[k], "before_value": b[k]["actual"]})
    return out


def propose_bump(diff, *, before_keys, after_keys, before_vocab, after_vocab) -> str:
    """§7.6 -- derive the version bump from MEASURED BEHAVIOR rather than
    leaving it to whoever wrote the commit. Advisory to the human, paired with
    the load-time hash enforcement in §6.2."""
    if set(before_keys) != set(after_keys):
        return "major"                      # entity pool / membership consumers break
    if set(before_vocab) - set(after_vocab):
        return "major"                      # a value consumers saw can no longer be emitted
    if set(after_vocab) - set(before_vocab):
        return "minor"                      # additive
    if diff["fixed"] or diff["broken"]:
        return "minor"                      # recalibration; schema intact
    return "patch"                          # behavior-neutral


def write_regressions(path, diff, changed_rule_files) -> None:
    """§7.6 -- regressions are LOGGED, not blocked. Merging is not gated on
    this; the log is advisory. Surfaced in the scorecard and REPORT.md rather
    than buried in a side file someone has to know to open."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["obs_id", "key_type", "key", "before", "after",
                    "changed_rule_files"])
        for o in diff["broken"]:
            w.writerow([o["obs_id"], o["key_type"], o["key"],
                        o.get("before_value", ""), o["actual"],
                        "|".join(sorted(changed_rule_files))])
```

Then in `evaluate`, accept an optional baseline and use it:

```python
def evaluate(observations_path, rules_dir, labels_path, out_root,
             runs_root="runs", baseline_outcomes=None,
             baseline_labels_hash=None, baseline_run_id=None,
             changed_rule_files=()) -> Path:
```

and replace the `regressions.csv` stub at the end with:

```python
    current_labels_hash = labels_hash(labels_path)
    if baseline_outcomes is not None:
        diff = four_bucket_diff(baseline_outcomes, outcomes,
                                before_labels_hash=baseline_labels_hash,
                                after_labels_hash=current_labels_hash)
        eval_manifest["baseline_run_id"] = baseline_run_id
        eval_manifest["baseline_labels_hash"] = baseline_labels_hash
        (out / "eval_manifest.json").write_text(
            json.dumps(eval_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        (out / "four_bucket.json").write_text(
            json.dumps({k: len(v) for k, v in diff.items()}, indent=2,
                       sort_keys=True) + "\n", encoding="utf-8")
    else:
        diff = {"fixed": [], "broken": [], "stable_correct": [],
                "stable_incorrect": []}
    write_regressions(out / "regressions.csv", diff, changed_rule_files)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_four_bucket.py tests/test_eval.py -v`
Expected: PASS, 21 tests

- [ ] **Step 5: Commit**

```bash
git add eval.py tests/test_four_bucket.py
git commit -m "feat: four-bucket diff, regression log, version-bump proposal"
```

---

### Task 21: `adjudicate.py` — blinded packet export and round-trip guards

**Files:**
- Create: `adjudicate.py`
- Test: `tests/test_adjudication.py`

**Interfaces:**
- Consumes: a run bundle, `obs-data/observations.csv`, `label_tools.obs_hash`.
- Produces: `export_packet(run_dir, observations_path, labels_path, out_dir) -> Path`; `import_returned_labels(packet_path, returned_path, observations_path) -> tuple[list[dict], list[str]]` returning `(accepted, rejected_reasons)`; `PACKET_COLUMNS: tuple[str, ...]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_adjudication.py
import csv

import pytest

from adjudicate import PACKET_COLUMNS, export_packet, import_returned_labels
from run import run_pipeline


@pytest.fixture(scope="module")
def packet(tmp_path_factory):
    root = tmp_path_factory.mktemp("adj")
    run_dir = run_pipeline("obs-data/observations.csv", "rules", root / "runs")
    return export_packet(run_dir, "obs-data/observations.csv",
                         "labels/labels.csv", root / "adjudication")


def _rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_packet_carries_evidence_columns_only(packet):
    """§7.7: no vendor, no model, no confidence, no entity_id. Blinding is
    STRUCTURAL rather than procedural -- the column isn't hidden by policy,
    it isn't in the file, so no one has to be trusted not to look."""
    header = set(_rows(packet)[0])
    assert header == set(PACKET_COLUMNS)
    for forbidden in ["vendor", "model", "device_type", "firmware",
                      "confidence", "stability", "entity_id"]:
        assert forbidden not in header


def test_packet_retains_the_join_key_but_not_the_answer(packet):
    """§7.7: run_id and obs_hash are retained so returned labels join back
    cleanly -- the join key survives, the answer doesn't."""
    row = _rows(packet)[0]
    assert row["obs_id"]
    assert row["obs_hash"].startswith("sha256:")


def test_selection_reads_pipeline_output_but_presentation_does_not(packet):
    """§7.7: the export tool necessarily reads confidence and cluster size to
    IDENTIFY the hard stratum. What it must not do is put those columns in
    front of the human."""
    rows = _rows(packet)
    assert 0 < len(rows) < 74     # a stratum, not everything
    assert "confidence" not in rows[0]


def test_out_of_packet_labels_are_refused(packet, tmp_path):
    """§7.7: catches adjudication done from a full export via a back channel."""
    returned = tmp_path / "returned.csv"
    with open(returned, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["obs_id", "key_type", "key", "value", "obs_hash"])
        w.writerow(["OBS-999", "field", "vendor", "Hikvision", "sha256:whatever"])
    accepted, rejected = import_returned_labels(
        packet, returned, "obs-data/observations.csv")
    assert accepted == []
    assert any("OBS-999" in r for r in rejected)


def test_stale_obs_hash_is_flagged_not_silently_merged(packet, tmp_path):
    """§7.7: bind to evidence, not just the run."""
    row = _rows(packet)[0]
    returned = tmp_path / "returned.csv"
    with open(returned, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["obs_id", "key_type", "key", "value", "obs_hash"])
        w.writerow([row["obs_id"], "field", "vendor", "Hikvision",
                    "sha256:stale0000"])
    accepted, rejected = import_returned_labels(
        packet, returned, "obs-data/observations.csv")
    assert accepted == []
    assert any("stale" in r.lower() for r in rejected)


def test_valid_return_is_accepted_and_marked_blinded(packet, tmp_path):
    row = _rows(packet)[0]
    returned = tmp_path / "returned.csv"
    with open(returned, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["obs_id", "key_type", "key", "value", "obs_hash"])
        w.writerow([row["obs_id"], "field", "vendor", "Hikvision",
                    row["obs_hash"]])
    accepted, rejected = import_returned_labels(
        packet, returned, "obs-data/observations.csv")
    assert rejected == []
    assert len(accepted) == 1
    assert accepted[0]["blinded"] == "true"
    assert accepted[0]["label_basis"] == "physical_inspection"


def test_sticky_labels_are_not_re_exported(packet, tmp_path_factory):
    """§7.7: once adjudicated at a given tier, an obs is not re-adjudicated
    unless its obs_hash changed or higher-tier evidence arrives. Otherwise
    every recalibration re-queues previously-settled observations."""
    root = tmp_path_factory.mktemp("sticky")
    run_dir = run_pipeline("obs-data/observations.csv", "rules", root / "runs")
    settled = _rows(packet)[0]["obs_id"]

    labels_path = root / "labels.csv"
    with open("labels/labels.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
        header = list(rows[0])
    for r in rows:
        if r["obs_id"] == settled:
            r["status"] = "adjudicated"
            r["label_basis"] = "physical_inspection"
    with open(labels_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(rows)

    second = export_packet(run_dir, "obs-data/observations.csv", labels_path,
                           root / "adjudication2")
    assert settled not in {r["obs_id"] for r in _rows(second)}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_adjudication.py -v`
Expected: FAIL — `No module named 'adjudicate'`

- [ ] **Step 3: Write the implementation**

```python
#!/usr/bin/env python3
"""Blind adjudication packet export and round-trip guards (design doc §7.7).

Invariant #6 is the mirror of #5: labels don't reach the runtime path, and
pipeline output doesn't reach hard-stratum label creation.

A human handed a plausible answer and asked "is this right?" agrees more often
than one asked "what is this?". Unblinded labeling drifts ground truth toward
whatever the pipeline already believes, after which Stages 1-4 measure
self-consistency rather than correctness -- worst precisely where the pipeline
is confidently wrong.

SELECTION may read pipeline output; PRESENTATION must not. This tool sits
outside the pipeline with read access to both sides, and emits an
evidence-only packet.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from label_tools import obs_hash

PACKET_COLUMNS = ("obs_id", "obs_hash", "source", "raw_payload", "mac",
                  "hostname", "open_ports", "site")

HARD_STRATUM_CONFIDENCE = 0.6
STICKY_TIERS = ("adjudicated", "agreed")
TIER_ORDER = ["physical_inspection", "asset_inventory", "vendor_doc",
              "payload_inference"]


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def export_packet(run_dir, observations_path, labels_path, out_dir) -> Path:
    resolutions = _read_csv(Path(run_dir) / "resolutions.csv")
    observations = {r["obs_id"]: r for r in _read_csv(observations_path)}

    settled = {
        r["obs_id"] for r in _read_csv(labels_path)
        if r.get("status") in STICKY_TIERS
        and r.get("label_basis") != "payload_inference"
    }

    # Selection reads confidence and cluster size to IDENTIFY the hard stratum.
    cluster_size: dict[str, int] = {}
    for r in resolutions:
        cluster_size[r["entity_id"]] = cluster_size.get(r["entity_id"], 0) + 1

    selected = sorted(
        r["obs_id"] for r in resolutions
        if r["obs_id"] not in settled
        and (float(r["confidence"]) < HARD_STRATUM_CONFIDENCE
             or float(r["stability"]) < HARD_STRATUM_CONFIDENCE
             or cluster_size[r["entity_id"]] == 1
             and float(r["confidence"]) < 0.75)
    )

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    packet = out_dir / "packet.csv"

    # Presentation carries evidence only. No vendor, no model, no confidence,
    # no entity_id -- the column isn't hidden by policy, it isn't in the file.
    with open(packet, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(PACKET_COLUMNS))
        w.writeheader()
        for obs_id in selected:
            obs = observations[obs_id]
            w.writerow({
                "obs_id": obs_id,
                "obs_hash": obs_hash(obs),
                "source": obs["source"],
                "raw_payload": obs["raw_payload"],
                "mac": obs["mac"],
                "hostname": obs["hostname"],
                "open_ports": obs["open_ports"],
                "site": obs["site"],
            })
    return packet


def import_returned_labels(packet_path, returned_path, observations_path):
    packet = {r["obs_id"]: r for r in _read_csv(packet_path)}
    current = {r["obs_id"]: obs_hash(r) for r in _read_csv(observations_path)}

    accepted, rejected = [], []
    for row in _read_csv(returned_path):
        obs_id = row["obs_id"]
        if obs_id not in packet:
            rejected.append(
                f"{obs_id}: not in the packet it was exported from -- "
                f"adjudication done via a back channel is refused (§7.7)"
            )
            continue
        if row.get("obs_hash") != current.get(obs_id):
            rejected.append(
                f"{obs_id}: stale obs_hash -- the observations row changed "
                f"since the packet was exported, so the label is flagged "
                f"rather than silently merged (§7.7)"
            )
            continue
        accepted.append({
            "obs_id": obs_id,
            "key_type": row["key_type"],
            "key": row["key"],
            "value": row["value"],
            "status": "adjudicated",
            "label_basis": "physical_inspection",
            "labeler_certainty": row.get("labeler_certainty", "high"),
            "blinded": "true",       # recorded PER LABEL, not assumed per stratum
            "obs_hash": row["obs_hash"],
            "labeled_by": row.get("labeled_by", "adjudicator"),
            "labeled_at": row.get("labeled_at", ""),
        })
    return accepted, rejected


if __name__ == "__main__":
    run_dir = sys.argv[1]
    out = export_packet(run_dir, "obs-data/observations.csv",
                        "labels/labels.csv", f"adjudication/{Path(run_dir).name}")
    print(out)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_adjudication.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all tests pass across both plans

- [ ] **Step 6: Commit**

```bash
git add adjudicate.py tests/test_adjudication.py
git commit -m "feat: blinded adjudication packet with round-trip guards"
```

---

### Task 22: Surface regressions in `REPORT.md`

§7.6 is explicit that advisory protection is only as good as the log being read, and names two things that make that likely.

**Files:**
- Modify: `obs_pipeline/report.py` — add a regressions section; `eval.py` — write a scorecard the report can read
- Test: `tests/test_report_regressions.py`

**Interfaces:**
- Consumes: `evals/<eval_id>/regressions.csv`, `evals/<eval_id>/four_bucket.json`.
- Produces: `write_scorecard(eval_dir, diff, cumulative) -> Path`; `report.write_report(run_dir, eval_dir=None)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report_regressions.py
"""§7.6: 'Surface, don't bury' and 'Make it cumulative'."""
import csv
import json

import pytest

from obs_pipeline.report import write_report
from run import run_pipeline


@pytest.fixture()
def run_and_eval(tmp_path):
    run_dir = run_pipeline("obs-data/observations.csv", "rules", tmp_path / "runs")
    eval_dir = tmp_path / "evals" / "e1"
    eval_dir.mkdir(parents=True)
    (eval_dir / "four_bucket.json").write_text(json.dumps(
        {"fixed": 10, "broken": 2, "stable_correct": 50, "stable_incorrect": 3}))
    with open(eval_dir / "regressions.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["obs_id", "key_type", "key", "before", "after",
                    "changed_rule_files"])
        w.writerow(["OBS-012", "field", "vendor", "Hikvision", "Unknown",
                    "normalization.yaml"])
    return run_dir, eval_dir


def test_broken_counts_appear_in_the_report_not_only_a_side_file(run_and_eval):
    run_dir, eval_dir = run_and_eval
    write_report(run_dir, eval_dir=eval_dir)
    text = (run_dir / "REPORT.md").read_text()
    assert "broken" in text.lower()
    assert "2" in text


def test_the_full_broken_list_is_shown_not_just_the_count(run_and_eval):
    run_dir, eval_dir = run_and_eval
    write_report(run_dir, eval_dir=eval_dir)
    text = (run_dir / "REPORT.md").read_text()
    assert "OBS-012" in text
    assert "normalization.yaml" in text


def test_report_without_an_eval_still_renders(run_and_eval):
    run_dir, _ = run_and_eval
    write_report(run_dir)
    assert (run_dir / "REPORT.md").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_report_regressions.py -v`
Expected: FAIL — `write_report() got an unexpected keyword argument 'eval_dir'`

- [ ] **Step 3: Modify `obs_pipeline/report.py`**

Change the signature and append a section before the final write:

```python
def write_report(run_dir, eval_dir=None) -> Path:
```

Immediately before `out = run_dir / "REPORT.md"`, insert:

```python
    # §7.6 -- surface, don't bury. The fixed/broken counts and the full broken
    # list belong here, not only in a side file someone has to know to open.
    if eval_dir is not None:
        eval_dir = Path(eval_dir)
        lines += ["", "## Regressions since the baseline rule state", ""]
        fb_path = eval_dir / "four_bucket.json"
        if fb_path.exists():
            fb = json.loads(fb_path.read_text(encoding="utf-8"))
            lines += [
                "| Bucket | Count |", "|---|---|",
                f"| `fixed` | {fb.get('fixed', 0)} |",
                f"| **`broken`** | **{fb.get('broken', 0)}** |",
                f"| `stable_correct` | {fb.get('stable_correct', 0)} |",
                f"| `stable_incorrect` | {fb.get('stable_incorrect', 0)} |",
                "",
            ]
        reg_path = eval_dir / "regressions.csv"
        if reg_path.exists():
            regressions = _read(reg_path)
            if regressions:
                lines += [
                    "*Advisory, not blocking (§7.6). A label that flips "
                    "`broken` and is never fixed stays visible across "
                    "subsequent runs.*",
                    "",
                    "| obs_id | key | before | after | changed rules |",
                    "|---|---|---|---|---|",
                ]
                lines += [
                    f"| `{r['obs_id']}` | `{r['key']}` | `{r['before']}` | "
                    f"`{r['after']}` | `{r['changed_rule_files']}` |"
                    for r in regressions
                ]
            else:
                lines.append("*No regressions.*")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_report_regressions.py tests/test_report.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add obs_pipeline/report.py tests/test_report_regressions.py
git commit -m "feat: surface four-bucket counts and broken list in REPORT.md"
```

---

### Task 23: Label adjudication rules, two-pass diff, stability validation

Closes the four remaining §10 Phase 5 checklist items.

**Files:**
- Modify: `label_tools.py` — add `resolve_by_basis_precedence`, `validate_against_vocabulary`
- Modify: `eval.py` — add `two_pass_diff`, plus the stability-validation metric in `score_against_labels`
- Test: `tests/test_label_adjudication.py`, `tests/test_two_pass.py`

**Interfaces:**
- Consumes: `label_tools.load_labels`, `obs_pipeline.vocab.load_vocab`, `eval.four_bucket_diff`.
- Produces: `resolve_by_basis_precedence(labels) -> list[dict]`; `validate_against_vocabulary(labels, vocab, rules) -> list[str]`; `BASIS_PRECEDENCE: tuple[str, ...]`; `two_pass_diff(*, baseline_outcomes, rules_held_outcomes, labels_held_outcomes, baseline_labels_hash, current_labels_hash) -> dict[str, dict]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_label_adjudication.py
from label_tools import (
    BASIS_PRECEDENCE, resolve_by_basis_precedence, validate_against_vocabulary,
)
from obs_pipeline.loader import load_rules
from obs_pipeline.vocab import load_vocab

RULES = load_rules("rules", "obs-data/observations.csv")
VOCAB = load_vocab("rules/canonical_vocab.csv")


def _l(obs_id, value, basis, labeler="a"):
    return {"obs_id": obs_id, "key_type": "field", "key": "vendor",
            "value": value, "status": "proposed", "label_basis": basis,
            "labeler_certainty": "medium", "blinded": "false",
            "obs_hash": "sha256:x", "labeled_by": labeler, "labeled_at": ""}


def test_precedence_order_matches_the_design():
    assert BASIS_PRECEDENCE == ("physical_inspection", "asset_inventory",
                                "vendor_doc", "payload_inference")


def test_higher_basis_tier_wins_deterministically():
    """§7.2.2: most type-2 disputes resolve without a human meeting."""
    out = resolve_by_basis_precedence([
        _l("OBS-011", "Dahua Technology", "payload_inference", "a"),
        _l("OBS-011", "Hikvision", "physical_inspection", "b"),
    ])
    winner = [r for r in out if r["obs_id"] == "OBS-011"]
    assert len(winner) == 1
    assert winner[0]["value"] == "Hikvision"
    assert winner[0]["status"] == "adjudicated"


def test_same_tier_disagreement_becomes_disputed_not_silently_picked():
    """§7.2.2: a human adjudicator is required only when two labels share the
    same basis tier and disagree."""
    out = resolve_by_basis_precedence([
        _l("OBS-011", "Dahua Technology", "asset_inventory", "a"),
        _l("OBS-011", "Hikvision", "asset_inventory", "b"),
    ])
    assert {r["status"] for r in out} == {"disputed"}
    assert len(out) == 2      # both retained; neither is thrown away


def test_agreeing_labels_at_the_same_tier_are_not_disputed():
    out = resolve_by_basis_precedence([
        _l("OBS-011", "Hikvision", "asset_inventory", "a"),
        _l("OBS-011", "Hikvision", "asset_inventory", "b"),
    ])
    assert len(out) == 1
    assert out[0]["status"] == "agreed"


def test_precedence_is_a_flat_list_not_a_scoring_formula():
    """§7.2.2: resist recursing the claim-scoring math onto labels. Ground
    truth that needs a weighted confidence model is no longer ground truth."""
    import inspect
    src = inspect.getsource(resolve_by_basis_precedence)
    for banned in ["weight", "independence_bonus", "conflict_penalty", "score("]:
        assert banned not in src


def test_out_of_vocabulary_label_is_a_rules_change_request_not_a_label():
    """§7.2.1 kind 1: adjudicating vocabulary disagreement case by case papers
    over a gap in claims.yaml or normalization.yaml."""
    problems = validate_against_vocabulary(
        [_l("OBS-011", "HIKVISION", "payload_inference")], VOCAB, RULES)
    assert problems
    assert "HIKVISION" in problems[0]
    assert "rules change request" in problems[0].lower()


def test_in_vocabulary_label_passes_validation():
    assert validate_against_vocabulary(
        [_l("OBS-011", "Hikvision", "payload_inference")], VOCAB, RULES) == []


def test_open_vocabulary_fields_are_not_constrained():
    """§7.2.1: free text where the vocabulary isn't closed (model), but passed
    through normalization.yaml before comparison."""
    label = {**_l("OBS-011", "DS-2CD2143G0-I", "payload_inference"),
             "key": "model"}
    assert validate_against_vocabulary([label], VOCAB, RULES) == []


def test_the_imported_label_set_is_vocabulary_clean():
    """§6.3: every vendor and device type in the initial label set is present
    in canonical_vocab.csv -- zero genuinely out-of-vocab labels."""
    from label_tools import load_labels
    assert validate_against_vocabulary(load_labels("labels/labels.csv"),
                                       VOCAB, RULES) == []
```

```python
# tests/test_two_pass.py
"""§7.6: when both rules and labels have changed, run two passes instead --
rules-held-constant (isolating the label delta) and labels-held-constant
(isolating the rule delta) -- so the two causes stay separable. Without this,
adjudication silently corrupts the regression signal."""
import pytest

from eval import LabelsMovedError, four_bucket_diff, two_pass_diff


def _o(obs_id, correct, actual):
    return {"obs_id": obs_id, "key_type": "field", "key": "vendor",
            "expected": "Hikvision", "actual": actual, "correct": correct,
            "provenance": "direct"}


def test_single_axis_diff_still_refuses_when_labels_moved():
    with pytest.raises(LabelsMovedError):
        four_bucket_diff([], [], before_labels_hash="sha256:a",
                         after_labels_hash="sha256:b")


def test_two_pass_separates_the_label_delta_from_the_rule_delta():
    baseline = [_o("OBS-011", True, "Hikvision")]
    # Rules held constant, labels moved: the label now says Dahua.
    rules_held = [_o("OBS-011", False, "Hikvision")]
    # Labels held constant, rules moved: the pipeline now emits Unknown.
    labels_held = [_o("OBS-011", False, "Unknown")]

    out = two_pass_diff(baseline_outcomes=baseline,
                        rules_held_outcomes=rules_held,
                        labels_held_outcomes=labels_held,
                        baseline_labels_hash="sha256:a",
                        current_labels_hash="sha256:b")
    assert len(out["label_delta"]["broken"]) == 1
    assert len(out["rule_delta"]["broken"]) == 1


def test_a_label_flip_is_not_attributed_to_the_rules():
    """A label flipping from Dahua to Hikvision must not read as a rule
    regression."""
    baseline = [_o("OBS-011", True, "Hikvision")]
    rules_held = [_o("OBS-011", False, "Hikvision")]   # only ground truth moved
    labels_held = [_o("OBS-011", True, "Hikvision")]   # rules unchanged in effect

    out = two_pass_diff(baseline_outcomes=baseline,
                        rules_held_outcomes=rules_held,
                        labels_held_outcomes=labels_held,
                        baseline_labels_hash="sha256:a",
                        current_labels_hash="sha256:b")
    assert out["rule_delta"]["broken"] == []
    assert len(out["label_delta"]["broken"]) == 1


def test_two_pass_is_unnecessary_when_labels_held_still():
    out = two_pass_diff(baseline_outcomes=[_o("OBS-011", True, "Hikvision")],
                        rules_held_outcomes=None,
                        labels_held_outcomes=[_o("OBS-011", False, "Unknown")],
                        baseline_labels_hash="sha256:a",
                        current_labels_hash="sha256:a")
    assert out["label_delta"] is None
    assert len(out["rule_delta"]["broken"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_label_adjudication.py tests/test_two_pass.py -v`
Expected: FAIL — `cannot import name 'BASIS_PRECEDENCE'` / `'two_pass_diff'`

- [ ] **Step 3: Add to `label_tools.py`**

```python
BASIS_PRECEDENCE = ("physical_inspection", "asset_inventory", "vendor_doc",
                    "payload_inference")


def resolve_by_basis_precedence(labels) -> list[dict]:
    """§7.2.2 -- adjudication by a FLAT precedence order.

    This is intentionally not a scoring formula. Resist recursing the
    claim-scoring math (§2.3) onto labels: a flat precedence list is
    sufficient and stays legible, and ground truth that needs a weighted
    confidence model is no longer serving as ground truth.
    """
    rank = {b: i for i, b in enumerate(BASIS_PRECEDENCE)}
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for label in labels:
        grouped.setdefault(
            (label["obs_id"], label["key_type"], label["key"]), []
        ).append(label)

    out: list[dict] = []
    for _, group in sorted(grouped.items()):
        best_tier = min(rank.get(l["label_basis"], len(rank)) for l in group)
        top = [l for l in group if rank.get(l["label_basis"], len(rank)) == best_tier]
        values = {l["value"] for l in top}

        if len(values) == 1:
            winner = dict(top[0])
            # A single tier-mate agreeing is `agreed`; a lower tier overruled
            # is `adjudicated`.
            winner["status"] = "adjudicated" if len(group) > len(top) or len(top) > 1 \
                else winner.get("status", "proposed")
            if len(top) > 1 and len(group) == len(top):
                winner["status"] = "agreed"
            out.append(winner)
        else:
            # Same tier, genuine disagreement: a human adjudicator is required.
            for l in top:
                out.append({**l, "status": "disputed"})
    return sorted(out, key=lambda r: (r["obs_id"], r["key_type"], r["key"],
                                      r["value"]))


def validate_against_vocabulary(labels, vocab, rules) -> list[str]:
    """§7.2.1 kind 1 -- vocabulary disagreement is a RULES defect, not a
    labeling one.

    Two labelers writing `Hikvision` and `HIKVISION` agree about the device and
    differ on the string. Adjudicating that case by case papers over a gap in
    claims.yaml vocabulary or normalization.yaml. A labeler needing a value
    outside the vocabulary is filing a rules change request, not a label.
    """
    closed = rules.claims.get("closed_vocabulary_fields", {})
    problems: list[str] = []
    for label in labels:
        if label["key_type"] != "field" or label["key"] not in closed:
            continue          # open vocabulary: model, firmware
        column = closed[label["key"]]["vocab_column"]
        pool = vocab.vendors if column == "vendor" else vocab.device_types
        if label["value"] not in pool:
            problems.append(
                f"{label['obs_id']}.{label['key']}: '{label['value']}' is not "
                f"in canonical_vocab.csv -- this is a rules change request "
                f"(claims.yaml vocabulary or normalization.yaml alias), "
                f"not a label (§7.2.1)"
            )
    return problems
```

- [ ] **Step 4: Add `two_pass_diff` to `eval.py`**

```python
def two_pass_diff(*, baseline_outcomes, rules_held_outcomes,
                  labels_held_outcomes, baseline_labels_hash,
                  current_labels_hash) -> dict[str, dict | None]:
    """§7.6 -- label mutation must not masquerade as rule regression.

    Adjudication (§7.2.2) mutates labels. If a label flips from Dahua to
    Hikvision between eval runs, the affected case lands in `broken` and reads
    as a rule regression when in fact the ground truth moved. When both rules
    and labels changed, run two passes so the two causes stay separable.

    Stickiness (§7.7) is what makes this load-bearing rather than theoretical:
    labels mostly hold still but occasionally move, which is exactly the
    mutation pattern that would otherwise contaminate the regression signal.
    """
    labels_moved = baseline_labels_hash != current_labels_hash

    label_delta = None
    if labels_moved:
        if rules_held_outcomes is None:
            raise LabelsMovedError(
                "labels_hash changed but no rules-held-constant pass was "
                "supplied; the label delta cannot be isolated (§7.6)"
            )
        # Rules held constant -> every flip here is attributable to the labels.
        label_delta = four_bucket_diff(baseline_outcomes, rules_held_outcomes)

    # Labels held constant -> every flip here is attributable to the rules.
    reference = rules_held_outcomes if labels_moved else baseline_outcomes
    rule_delta = four_bucket_diff(reference, labels_held_outcomes)

    return {"label_delta": label_delta, "rule_delta": rule_delta}
```

- [ ] **Step 5: Add the stability-validation metric to `score_against_labels`**

§8.4: *"bucket results by `stability` and ask whether accuracy falls as stability falls. If it doesn't, `stability` isn't measuring anything."* Insert before the `return rows, outcomes` line in `eval.py`:

```python
    # §8.4 -- stability validation. Needs no partition: it asks the honest
    # question directly rather than defining a stratum (§7.2.3).
    stab_buckets: dict[str, list] = defaultdict(list)
    for o in outcomes:
        if o["correct"] is None:
            continue
        stab_buckets[f"{min(int(o['stability'] * 10) / 10, 0.9):.1f}"].append(
            o["correct"])
    for b, results in sorted(stab_buckets.items()):
        rows.append(_row("accuracy_by_stability", f"bucket:{b}",
                         sum(1 for r in results if r) / len(results),
                         len(results)))

    # The high-confidence / low-stability quadrant is where stability earns
    # its keep: these should be materially less accurate than
    # high-confidence / high-stability results, and if they aren't,
    # confidence alone was sufficient after all (§8.4).
    for label, predicate in (
        ("high_conf_high_stab", lambda o: o["confidence"] >= 0.7 and o["stability"] >= 0.7),
        ("high_conf_low_stab", lambda o: o["confidence"] >= 0.7 and o["stability"] < 0.7),
    ):
        subset = [o for o in outcomes if o["correct"] is not None and predicate(o)]
        rows.append(_row("accuracy_by_stability", f"quadrant:{label}",
                         (sum(1 for o in subset if o["correct"]) / len(subset))
                         if subset else 0.0, len(subset)))
```

- [ ] **Step 6: Register the new metric in `metrics.yaml`**

```yaml
accuracy_by_stability:
  scope_type: bucket
  requires_labels: true
  direction: neutral
  description: >
    §8.4 stability validation -- does accuracy fall as stability falls? If it
    does not, stability isn't measuring anything and its component weighting
    in scoring.yaml needs revisiting. The high-confidence / low-stability
    quadrant should be materially less accurate than high-confidence /
    high-stability.
```

- [ ] **Step 7: Run the tests**

Run: `python3 -m pytest tests/test_label_adjudication.py tests/test_two_pass.py -v`
Expected: PASS, 13 tests

- [ ] **Step 8: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all pass

- [ ] **Step 9: Commit**

```bash
git add label_tools.py eval.py metrics.yaml tests/test_label_adjudication.py tests/test_two_pass.py
git commit -m "feat: basis precedence, vocab-constrained labels, two-pass diff, stability validation"
```

---

## Done criteria for this plan

- [ ] `python3 -m pytest -q` passes across both plans
- [ ] `python3 run.py` emits `metrics.jsonl` with `n` on every row and appends to `runs/history.jsonl`
- [ ] `python3 label_tools.py` imports the wide file with zero transitivity violations
- [ ] `python3 eval.py` emits `eval_manifest.json` carrying both `rules_rollup` and `labels_hash`
- [ ] `false_merge_count` is 0 and `pairwise_recall` is 1.0 at n=6 — **reported, not gating** (§7.4, n well below the n≥30 floor in §11)
- [ ] `python3 adjudicate.py <run_dir>` emits a packet with the eight evidence columns and nothing else
- [ ] `vocab_reject_frequency` ranks `LTS Security`, `Amcrest`, `Wisenet`, `VVTK` — the §7.3 Stage 1 work queue, populated from real data
- [ ] `validate_against_vocabulary` reports zero problems on the imported label set (§6.3: the vocabulary is already complete for this data)
- [ ] `accuracy_by_stability` emits both bucket rows and the two confidence×stability quadrants

## Deliberately not built

Everything in design doc §11: the Stage 3 positive-pair floor as an enforced gate, stratified pairwise sampling, LLM proposal hygiene and rule mining (§7.3 Stage 5), per-stage stopping criteria, and adjudicator staffing. The mechanisms these would sit on are all in place; the policy decisions are not made.

Two §8.5 process metrics are also deferred, for a data reason rather than a policy one — **inter-annotator agreement** on the dual-labeled stratum (§7.2.3) and the **blinded vs. unblinded agreement gap** (§7.7). Both need a second labeler's judgments, and the current label set has exactly one author. The columns they read (`labeler_certainty`, `blinded`, `labeled_by`) are imported and carried from Task 18 onward, so both become computable the moment a second set of labels arrives — no schema change required. The four-bucket counts and cumulative unfixed regressions, the other two §8.5 metrics, ship in Tasks 20 and 22.

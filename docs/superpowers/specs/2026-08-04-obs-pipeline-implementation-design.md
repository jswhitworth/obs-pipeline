# Implementation Architecture — Device Fingerprinting Pipeline

**Date:** 2026-08-04
**Status:** approved
**Scope:** Phases 1–5 of `device_fingerprinting_pipeline_design.md` §10

## 1. Relationship to the design document

`device_fingerprinting_pipeline_design.md` is the domain spec and remains
authoritative for *what* the pipeline computes. This document covers only
*how* it is built: module decomposition, the tracing mechanism, and the test
strategy. Where the two disagree, the design document wins and this one is
wrong.

Section references below (§2.3, §9.5, …) point into the design document.

## 2. The tracing mechanism

§9.3 requires trace *by construction*: "a code path that doesn't emit is a
code path that doesn't execute." §9.5 makes `replay.py` a per-build gate. The
conventional approach — compute, then call `tracer.step(...)` to log what
happened — fails this requirement in the exact case it exists for, because a
forgotten call is silent and the trace becomes fiction precisely where the
code is buggy.

**Decision: values carry their own derivation.**

```python
T = TypeVar("T")

@dataclass(frozen=True)
class Traced(Generic[T]):
    value: T
    step_id: str
```

(`Generic[T]` rather than PEP 695 `class Traced[T]` — the target interpreter
is Python 3.11.5.)

`Traced` instances are constructible only through `Tracer.step(...)`, which
emits the trace row and returns the wrapper. Rule application functions take
`Traced` inputs and return `Traced` outputs, so a step's `parents` are derived
from its inputs rather than passed by hand. An untraced value is therefore a
type error at a module boundary, not a replay diff discovered three phases
later.

`step_id` is `sha256` over the canonicalized step content (§9.2) — never a
counter — so two runs over identical input and rules produce byte-identical
traces and diffing across rule versions works.

Rejected alternatives:

- **Threaded tracer object.** Readable and conventional, but forgetting a call
  is silent — the §9.3 failure mode restated.
- **Decorators on rule functions.** Cannot see inside a function body, so the
  union-find merge loop stays untraced. §9.1 Q4 (which merges were *refused*,
  in what order) is the trace question with the highest audit value, and it
  lives inside that loop.

### 2.1 What replay may import

`replay.py` imports `trace.py` and `bundle.py` only. It must never import
`scoring.py`, `entity.py`, `fields.py`, or `loader.py`. If replay could reach
the engine it might reconstruct a value by recomputing it rather than by
reading the trace, and the completeness test would pass on an incomplete
trace — a silently useless gate.

This is enforced by an import test, not by convention.

## 3. Module decomposition

One module per rule category (§5), so the tuning surface and the code boundary
are the same line: a `scoring.yaml` change has exactly one module to read.

| Module | Responsibility | Design ref |
|---|---|---|
| `trace.py` | `Step`, `Tracer`, `Traced[T]`, content-addressed `step_id` | §9.2 |
| `loader.py` | six-file load, cross-file validation, per-file + rollup hashing, VERSION stale-bump check | §6.1, §6.2 |
| `vocab.py` | `canonical_vocab.csv` read **column-wise**, blanks dropped, no vendor↔device_type pairing inferred | §6.3 |
| `extract.py` | rule category 1 — regex, OUI lookup, port signatures | §2.2 |
| `normalize.py` | category 2 — MAC/hostname canonicalization, alias & OEM rebrand maps | §2.2 |
| `claims.py` | category 3 — claim construction, `field`/`link_basis` vocabulary, source→key mapping | §2.3 |
| `scoring.py` | category 4 — the single shared scoring function | §2.3, §2.4 |
| `entity.py` | categories 5 + 6 — union-find, pinned merge order, basis-precedence tie-break | §2.4 |
| `fields.py` | category 7 — aggregation, multiplicative decay, `firmware` → `undecidable`, closed-vocab constraint | §2.5 |
| `confidence.py` | harmonic-mean entity rollup, `stability` components | §2.6 |
| `bundle.py` | run bundle writers (`manifest.json`, the four CSVs, the two JSONL files) | §3 |
| `metrics.py` | `metrics.yaml` registry, long-format emitters, `history.jsonl` append | §8 |
| `report.py` | `REPORT.md` render — pure, makes no decisions, emits no trace steps | §3 |

Three entry points, none a flag on another (§7.6):

- `run.py` — the pipeline. Has no code path that reads labels (invariant #5).
- `replay.py` — trace-only reconstruction and diff (§9.5).
- `eval.py` — the harness. The only component reading both rules and labels.

### 3.1 Scoring parity as a structural property

`scoring.py` exports **one** function, taking a coefficient set as an
argument:

```python
def claim_weight(claims, key, coefficients) -> Traced[float]
```

`claims.py` calls it with the field-claim coefficients; `entity.py` calls it
with the identity-claim coefficients. Invariant #3 then cannot drift without
someone deliberately writing a second function — the structural form of what
§6.1 asks for.

Per §2.3, only the coefficients differ, never the formula shape:

```
independence_bonus(k) = b × (1 − r^(k−1))
claim_weight          = clamp(max_base + independence_bonus − conflict_penalty, 0, 1)
```

### 3.2 Coefficient calibration is deferred, deliberately

`scoring.yaml` ships with seeded defaults and a written rationale, not with
values fitted to `labels-initial.csv`. §7.4 already states that gates
calibrated against a same-author label set are provisional rather than valid,
and the dataset carries roughly 5 positive pairs — fitting to it would
manufacture confidence the data cannot support.

Identity claims get a heavier `conflict_penalty` relative to
`independence_bonus` than field claims do, per §2.3's blast-radius argument.
The two sets live in one file as two named parameter sets under one shared
function.

## 4. Data-layout decisions

Two files move to match §6.3:

- `vocab/canonical_vocab.csv` → `rules/canonical_vocab.csv`. It must be
  **inside** the rules rollup hash, because changing it changes pipeline
  output.
- `labels/labels-initial.csv` stays where it is and is read only by the
  import step. It is a wide rendering (§7.2), not the schema.

The wide label file's `confidence` column holds `high | medium | low` and is
`labeler_certainty` (§7.2.3), not a pipeline confidence. Import maps it as
such and stamps the remaining provenance honestly: `status: proposed`,
`label_basis: payload_inference`, `blinded: false`, `obs_hash` computed at
import. Distribution in the current file is 55 high / 14 medium / 5 low, so
the dual-label stratum (`medium` + `low`) is 19 observations.

`metrics.yaml` sits beside `rules/` but **outside** the rollup hash, carrying
its own `metrics_hash` (§8.1).

## 5. Testing

pytest, in three tiers.

**Unit tests per rule category.** Each module tested against its own rule
file, in isolation.

**The replay diff as a build gate (§9.5).** `replay.py` reconstructs
`claims.csv`, `membership.csv`, and `entities.csv` from `trace.jsonl` alone;
the reconstruction is diffed against the actual run outputs. This is the
mechanical enforcement of invariant #7 and runs on every build, not on demand.

**The five named entity cases (§7.4)** as individual regression tests,
asserted in isolation rather than as a rate:

| Entity | Observations | Assertion |
|---|---|---|
| `E-001` | OBS-001/002/003 | three sources, one MAC — clean corroboration; independence bonus fires |
| `E-002` | OBS-004/005 | two sources, one MAC, differing hostnames — MAC still merges |
| `E-052` | OBS-047/072 | same MAC, different hostname *and* IP — `mac` must beat `hostname_token` |
| `E-066` | OBS-061/073 | OBS-073 has empty MAC — must link by serial `00012E` or hostname |
| `E-074` | OBS-069/074 | firmware 8.10.0135 vs 8.11.0021 → entity value `undecidable` |

**One added negative case.** OBS-001 and OBS-047 are different devices sharing
vendor *and* model (`P3245-LVE`, distinct MACs and serials). A careless
`oui_model_pair` basis merges them. Asserted as a false-merge regression test,
since §7.3 Stage 3 requires bias toward refusing weak merges and this dataset
contains a live instance of the trap.

**Determinism test.** Two runs over identical input and rules produce
byte-identical `trace.jsonl`. This covers §10's "no reliance on dict ordering,
input row order, or wall clock" convention with an assertion rather than a
code-review habit.

## 6. Conventions

- PyYAML, `safe_load` only.
- No reliance on dict iteration order, input row order, or wall clock anywhere
  in the pipeline path. `run_id` carries the only timestamp, and it is not
  read by any decision.
- Every rule definition carries a `rule_id` anchor (`file#rule_name`)
  resolvable through the manifest's per-file hashes.

## 7. Out of scope

Everything in §11 of the design document (Stage 3 positive-pair floor,
pairwise sampling strategy, LLM proposal hygiene, stopping criteria,
adjudicator staffing). The blind-adjudication *packet export and round-trip
guards* (§7.7) are in scope as Phase 5 mechanism; who staffs adjudication is
not.

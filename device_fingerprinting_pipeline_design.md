# Device Fingerprinting & Entity Resolution Pipeline — Design Document

## 1. Purpose

Resolve raw observations of network security assets (IP cameras, NVRs, access
controllers, switches, PLCs, and similar devices) from multiple protocol
sources (ONVIF, SNMP, HTTP banners, mDNS, telnet banners) into structured,
high-confidence entity records with fields for `vendor`, `model`, `device_type`, `firmware`,
`entity_id`, and `confidence`.

**Design constraints:**
- Deterministic — identical input produces identical output on every run.
- Auditable — every emitted value, and every clustering decision, traces to
  specific evidence with an explicit scoring rationale.

## 2. Pipeline Stages

```
observations.csv
      │
      ▼
1. Load observations
      │
      ▼
2. Extract values  (extraction + normalization rules)
      │
      ▼
3. Build claims  (claim rules; scoring rules shared with stage 4)
      │
      ▼
4. Entity resolution  (identity claims only; clustering + basis-precedence
   tie-break rules) → membership evidence, persisted to membership.csv
      │
      ▼
5. Field resolution  (field claims + persisted membership; aggregation +
   sibling-propagation decay rules)
      │
      ▼
Entity pool + REPORT.md
```

### 2.1 Load observations
Raw rows from `observations.csv`: `obs_id`, `source`, `raw_payload`, `mac`,
`hostname`, `open_ports`, `ip_address`, `site`.

### 2.2 Extract values
Extraction rules (regex, OUI lookup, port signatures) pull structured values
out of raw fields. Normalization rules (MAC delimiter/case, hostname casing,
vendor alias/OEM rebrand maps) canonicalize those values immediately after
extraction, so identical real-world values compare equal regardless of
source formatting. This is what makes cross-source conflict detection and
corroboration in step 3 meaningful — get normalization wrong and you get
silent false conflicts or false corroboration, with no extraction rule
looking broken.

Each source's extraction rule declares which canonical target key it
populates:
- Identity evidence → `link_basis` (`mac`, `serial`, `hostname_token`,
  `oui_model_pair`, …)
- Descriptive evidence → `field` (`vendor`, `model`, `firmware`,
  `device_type`, …)

### 2.3 Build claims
Every normalized value becomes a claim keyed by
`(obs_id, claim_field_or_link_basis, value)`, tagged with `source_locator`
(signal origin) and `source`.

**One unified scoring function applies to both identity claims and field
claims** — and to the cross-observation re-score at field resolution (§2.5)
— defined once and shared, not duplicated:

```
claim_weight = max(base_weight over contributing sources)
              + independence_bonus(distinct witness/source groups
                                    agreeing on this key+value)
              − conflict_penalty(other claims sharing this key,
                                  disagreeing on value)
```

Conflict and independence are computed on the same canonical key in both
cases — `(obs_id, field, value)` for field claims, `(obs_id, link_basis,
value)` for identity claims. `source` is metadata carried on the claim,
never folded into the key, so multiple sources can actually collide on the
same key and either corroborate or contradict as intended.

**Formula shape vs. calibrated coefficients:** the function itself
(`max + independence_bonus − conflict_penalty`) is identical for both claim
types and lives in exactly one place. The *coefficients* feeding it may
legitimately differ per claim type: a wrong field claim corrupts one field
on one entity, while a wrong identity claim can merge two physically
different devices into one entity — an error that then multiplies across
every field on every member via sibling propagation (§2.5). This asymmetric
blast radius justifies weighting `conflict_penalty` more heavily relative
to `independence_bonus` for identity claims than for field claims, biasing
identity resolution toward refusing weak/contested merges rather than
optimistically clustering. This is a calibration decision (§7.3, Stages 2–3)
validated against labeled same-device/different-device pairs — not a
formula-shape decision — so all coefficient sets live in `scoring.yaml` as
named parameter sets under one shared function: two for the claim types
here, and a third (`entity_corroboration`) for the cross-observation
re-score defined in §2.5.

**Function form.** The shape above is realized as:

```
independence_bonus(k) = b × (1 − r^(k−1))      # k = distinct witness groups
claim_weight          = clamp(max_base + independence_bonus − conflict_penalty, 0, 1)
```

`b` and `r` are per-claim-type coefficients in `scoring.yaml`. The bonus
**saturates** rather than growing linearly: with b=0.3, r=0.5, successive
agreeing witnesses contribute +0.15, +0.225, +0.2625, converging on b. Linear
accumulation would let several mediocre sources outrank one authoritative
source without bound; saturation caps what corroboration alone can buy.

A resolved field's confidence is **max-plus-bonus over the winning value's
pooled evidence** (decayed if propagated, §2.5) — *never* an average over
corroborating claims. Corroboration is priced at exactly two scopes, each
exactly once, and the scoping must be stated because claims are keyed by
`obs_id`:

- **Within an observation, at claim time.** The `independence_bonus` on a
  claim sees only witness groups colliding on that one observation — e.g. a
  payload regex and the OUI map both asserting `vendor` on the same row.
- **Across observations, at field resolution (§2.5).** Membership does not
  exist yet at claim time, so cross-observation agreement cannot be priced
  here. Once membership is persisted, the winning value is re-scored by the
  same function over the union of distinct witness groups across all member
  claims asserting it, under the third named coefficient set
  (`entity_corroboration`).

The union is what prevents double-counting: the entity-level score is
recomputed from the pooled distinct-group set, never stacked on top of
per-claim bonuses. It is also what keeps re-observation honest: three
observations that each read `vendor` from the same OUI map pool to one
witness group and earn no bonus, because the same underlying signal observed
three times is not independent agreement.

Note this asymmetry explicitly: reinforcing evidence combines by
max-plus-bonus, never by averaging, because averaging in a weak agreeing
witness would *lower* confidence in a value that just gained support.

### 2.4 Entity resolution
Runs on **identity claims only** — never on resolved field values. This
keeps the pipeline a strict DAG and prevents a field→cluster feedback loop.

Produces cluster assignments and emits **membership evidence**:

```
(obs_id, entity_id, link_basis, link_weight)
```

**Algorithm: deterministic union-find** over identity claims exceeding a
`link_weight` threshold, with merges applied in a fixed sorted order —
`(link_weight desc, basis_precedence, obs_id asc)`. The ordering is declared
in `entity_resolution.yaml`, not left in code: merge order changes cluster
outcomes, so it is a rule (§5 category 5), and determinism (§1) requires it be
pinned rather than inherited from input row order.

**`entity_id` is content-addressed** — a hash over the sorted member `obs_id`
set — so identical membership yields an identical ID across runs. Two
consequences that must be stated to consumers: IDs are stable across identical
runs but **change when membership changes**, and the eval harness therefore
matches on the **partition**, never on ID strings (pairwise precision/recall,
§8.4 Stage 3, does this naturally). An ID that silently denoted a different
device set would be worse than one that changes visibly, but IDs are
per-run-state, not durable asset identifiers.

`link_weight` reuses the same scoring function from §2.3, calibrated with
identity-claim coefficients (see the formula-shape-vs-coefficients note in
§2.3).

**Cross-basis clustering contradictions** (e.g., `mac` evidence points to
entity A while `hostname_token` evidence points to entity B for the same
`obs_id`) are a graph-level property, not a per-claim one, so they are kept
out of `link_weight`. Instead, an explicit basis-precedence tie-break rule
decides the outcome (precedence order, or refuse-to-merge-and-flag), and
the decision is recorded as its own auditable columns — `basis_agreement`
(boolean) and `conflict_detail` (nullable, naming the contending bases) — in
the persisted output.

**Persisted to `membership.csv`** — closing the original audit-trail gap.

### 2.5 Field resolution
Runs on field claims (§2.3) plus persisted membership (§2.4). Resolves
final field values per entity, using **sibling propagation with decay** to
fill gaps across entity members.

**Decay is multiplicative:**

```
propagated_confidence = source_confidence × link_weight × decay_base^hop
```

with `decay_base` declared in `field_resolution.yaml`. A chain is conjunctive
— every link must hold independently — so multiplication is the principled
form: a low-confidence link discounts appropriately, and multi-hop transitive
paths don't inherit full strength from one strong link along the way.

**Field conflict within an entity.** When members of one entity carry
different values for the same field, the default rule is **highest
`claim_weight` wins**, with the runner-up recorded in the trace (§9.1 Q6).
This is stated as a rule in `field_resolution.yaml`, and it is more auditable
than a precedence table would be, since the trace shows *why* the winner won
rather than only that a table said so. An optional per-field
`source_precedence` block is available for cases where a human genuinely
knows better than the weights (e.g. "for `firmware`, ONVIF beats SNMP always")
— empty by default.

**Winner selection and winner confidence are separate decisions.** *Which*
value wins is decided by single-claim weight, per the rule above. *What
confidence* the winner carries is the §2.3 cross-observation re-score: pool
every member claim asserting the winning value, take the union of their
distinct witness groups, and apply the shared scoring function under the
`entity_corroboration` coefficient set, with groups backing other values in
the entity as the conflicting set. Keeping selection single-claim means
corroboration can raise confidence in a winner but never overturn one strong
direct reading by weight of numbers; if the pooled score ever ranks a
different value above the per-claim winner, that disagreement is a finding
to record (§7.6), not a resolution the pipeline makes. Where the entity
contributes no additional agreeing or disagreeing groups — a singleton, or a
value witnessed by exactly one member — the pooled set equals the winning
claim's own and the re-score reproduces its weight exactly: corroboration
pricing is a no-op wherever there is nothing to corroborate.

***`firmware` is the stated exception.*** Firmware is **temporal** and the
data model is atemporal. Two members reporting different firmware are not
contradicting each other — a device gets upgraded between scans, and both
readings may be true at different times. Applying highest-weight-wins here
would emit a confidently single-valued answer to something legitimately
multi-valued over time. Instead, when entity members disagree on `firmware`,
the entity-level value is marked `undecidable` (§7.2.1) and excluded from
Stage 4 denominators. The per-observation readings remain individually correct
and are emitted as such in `resolutions.csv` (§3) — the ambiguity exists at
the entity level only, so it is recorded at the entity level only, and no
observation is scored wrong for reporting what it actually saw.

**Closed-vocabulary fields.** `vendor` and `device_type` may only emit values
drawn from `canonical_vocab.csv` (§6.3). `model` and `firmware` remain open
vocabulary — model designators are effectively unbounded and enumerating them
is not tractable.

**`Unknown` is the absence marker, and these fields are never null.** When no
in-vocab value resolves, the field emits `Unknown` (vendor) or `unknown`
(device_type) — the escape values already present in the vocabulary. Two
distinct situations collapse to the same emitted value but are separated in
the trace and in metrics:

- **No evidence** — nothing extracted, or nothing survived scoring.
- **Vocab gap** — a value extracted and normalized cleanly, but is not in the
  vocabulary (e.g. `Ruckus`). The rejected value is recorded as a
  `vocab_reject` trace step (§9.3) and counted in `vocab_gap_rate` (§8.3).

Collapsing both to `Unknown` keeps downstream consumers free of null handling;
keeping them separate in the trace preserves the distinction that matters,
since only the second is actionable — it says the vocabulary is incomplete,
not that the device is unidentifiable.

**Two consequences that must be handled explicitly:**

- ***`Unknown` does not propagate.*** For sibling propagation, `Unknown` is
  treated as absence, not as a value. Propagating it would fill a sibling's
  genuine gap with a non-answer that then reads as a resolved field.
- ***`Unknown` does not conflict.*** It is excluded from `conflict_penalty`
  computation (§2.3). It is an absence marker, not a competing assertion —
  scoring it as a conflicting claim would penalize precisely the
  low-evidence observations that already have the least support.

**Confidence for `Unknown` is `0.0`.** A non-zero confidence attached to an
absence marker is not interpretable by a consumer. Near-misses — a rejected
out-of-vocab value, or an in-vocab candidate that scored below threshold —
are preserved in the trace rather than encoded in the confidence number.

### 2.6 Confidence model

Evidence is combined by three different operations, and conflating them is a
live failure mode. Each is stated explicitly:

| Combining… | Operation | Why |
|---|---|---|
| Witnesses of the *same* value | `max` + saturating bonus (§2.3) — within an observation at claim time, across an entity's members at field resolution (§2.5) | Reinforcing. Agreement must raise confidence at both scopes — averaging in a weak agreeing witness would *lower* it. |
| A propagation *chain* | multiplicative (§2.5) | Conjunctive and strict: every link must hold independently. |
| *Different fields* into an entity record | **harmonic mean** | Conjunctive: a record is only as trustworthy as its weakest field. |

**Entity-level `confidence` is the harmonic mean of the per-field
confidences.** Harmonic mean is dominated by its smallest input, which is
exactly the desired behavior — an entity with `vendor` 0.95, `model` 0.90 and
a port-signature-guessed `device_type` 0.30 should not read as a solid record
(arithmetic mean 0.72); harmonic mean gives 0.55 and says plainly that
something in it is weak. This is the same reasoning that makes F1 a harmonic
mean of precision and recall.

Per-field confidences are emitted alongside the rollup, so a consumer who
cares only about `vendor` is not penalized by a weak `device_type`. Fields
resolving to `Unknown` (0.0) are excluded from the harmonic mean rather than
zeroing it, and are visible through `known_rate` (§8.3) instead.

**`stability` is a separate field, because a mean cannot express it.**
Confidence measures how *strong* the support is; it is blind to how
*contested* the answer is. Two sources at 0.8 backing `Hikvision` yield the
same confidence whether the runner-up `Dahua` sat at 0.1 or at 0.79 — a
settled result and a knife-edge result are indistinguishable by magnitude
alone. `stability` is computed from the §2.3 decomposition already carried in
the trace, over three components:

- **margin** — distance from the winning claim to the runner-up
- **witness dependence** — how many independent witness groups would have to
  be removed to change the winner, counted over the same pooled
  cross-observation union that prices corroboration (§2.5)
- **live conflict** — unresolved contradiction sitting beneath the winner

A result can be high-confidence and low-stability; that combination is the
early-warning signal for values that will move under recalibration or one new
observation. Component weighting is declared in `scoring.yaml`.

## 3. Outputs

Each run emits a **run bundle** — a self-contained directory in which every
artifact is machine-readable and `REPORT.md` is a derived render, not a
source of truth:

```
runs/<run_id>/
  manifest.json     # run provenance (§6.2)
  metrics.jsonl     # all measurements, long format (§8)
  trace.jsonl       # derivation DAG — every decision step (§9)
  claims.csv        # full claim ledger with unified scoring
  membership.csv    # obs_id, entity_id, link_basis, link_weight,
                    #   basis_agreement/conflict
  entities.csv      # entity_id, vendor, model, device_type, firmware,
                    #   per-field confidence, confidence (harmonic rollup),
                    #   stability
  resolutions.csv   # per-obs join view with per-field provenance (§3.1)
  REPORT.md         # regenerable human view — never hand-edited
```

`REPORT.md` is a build artifact derived from the structured files. If it is
lost it can be regenerated; nothing downstream may depend on parsing it. This
inverts the usual arrangement deliberately — anything worth trending should
be queryable without re-parsing prose.

Every row in every tabular output carries a `run_id` and a
`derivation_step`. Neither rule versions nor derivation chains are repeated
per row — they resolve through `run_id` → `manifest.json` (§6.2) and
`derivation_step` → `trace.jsonl` (§9) respectively, keeping output rows
narrow while remaining fully traceable.

A multi-field row cannot carry one derivation chain, since `vendor` and
`firmware` have different ancestry. This is resolved in the DAG rather than
the schema: `derivation_step` points at a `resolve_entity` step whose
`parents` are the per-field resolution steps. Fan-in is what the DAG is for;
per-field step columns would be the DAG flattened badly into CSV.

### 3.1 `resolutions.csv`

```
obs_id, run_id, derivation_step, entity_id,
vendor, vendor_provenance,
model, model_provenance,
device_type, device_type_provenance,
firmware, firmware_provenance,
confidence, stability
```

`*_provenance` ∈ `direct | propagated | unknown`. For `propagated`, the
source observation is recoverable through `derivation_step` → trace rather
than being carried as a column.

**Why provenance is per-field and not omitted.** Some fields on an
observation were witnessed directly by that payload; others were inherited by
being clustered with a sibling that witnessed them. `OBS-073` in `E-066` has
an empty MAC and contributes almost nothing — whatever `vendor` it shows was
carried in from `OBS-061`. Diffing that row naively against labels would
credit the pipeline for extraction it never performed. The provenance column
lets the harness compute Stage 1/2 accuracy over `direct` fields only and
Stage 4 (propagated-value accuracy) over `propagated` fields only — two
metrics that were always meant to be distinct, now separable in practice
rather than only in principle.

`resolutions.csv` is a **pure join view** — `entities.csv` × `membership.csv`,
making no new decisions — so it emits no trace steps of its own and its
`derivation_step` points at the same `resolve_entity` step as the entity row.

The eval harness (§7.6) emits its own parallel bundle under `evals/<eval_id>/`
containing `eval_manifest.json`, `metrics.jsonl` (label-dependent metrics),
and `regressions.csv`.

## 4. Key Invariants

1. **DAG discipline** — entity resolution never reads resolved field values.
2. **Canonical keys, not namespaced-by-source** — `field` and `link_basis`
   are fixed vocabularies; `source` is always a separate attribute on the
   claim, never baked into the key.
3. **Scoring parity** — identity and field claims use the same scoring
   *function*, defined in exactly one place; calibrated coefficients may
   differ per claim type to reflect their different error blast-radii
   (§2.3), but never the formula shape. Graph-level ambiguity (cross-basis
   clustering contradiction) is handled as its own explicit decision, never
   laundered into a claim-level confidence number.
4. **Rule-state traceability** — determinism means *identical input and
   identical rules* produce identical output. Since rules change over time,
   every output is bound to the exact rule state that produced it via
   `run_id` → `manifest.json` (§6.2). An output record whose rule version
   cannot be resolved is not auditable, regardless of how complete its
   evidence trail is.
5. **Labels never enter the runtime path** — `labels.csv` is consumed only
   by the eval harness (§7.6), which sits outside the pipeline. `run.py`
   has no code path that reads labels, so ground truth can never leak into
   scoring or resolution.
6. **Pipeline output never enters hard-stratum label creation** — the mirror
   of #5, enforcing the same separation at the other end. Adjudicators
   working the hard stratum see evidence only, never the pipeline's resolved
   values (§7.7). Together, #5 and #6 keep the two epistemic paths — what the
   rules infer, and what is known to be true — from contaminating each other
   in either direction.
7. **No output value without a derivation** — every emitted value traces to a
   step in `trace.jsonl`, and the trace alone is sufficient to reconstruct
   every output. This is enforced mechanically by replay (§9.5), not asserted
   by convention: an undocumented value fails replay immediately.

## 5. Rule Taxonomy

The pipeline stages above are implemented over **seven distinct rule
categories**, each with its own tuning surface and failure mode:

| # | Rule category | Lives in stage | Governs |
|---|---|---|---|
| 1 | Extraction | 2.2 | raw_payload → candidate value (regex, OUI lookup, port signatures) |
| 2 | Normalization | 2.2 | canonicalizing candidate values so cross-source comparison is valid |
| 3 | Claim construction | 2.3 | which normalized values become claims; `field`/`link_basis` vocabulary |
| 4 | Scoring | 2.3 & 2.4 (shared) | `base_weight`, `independence_bonus`, `conflict_penalty` — single function used by both claim weight and `link_weight`, with separate coefficient sets calibrated per claim type |
| 5 | Entity resolution (clustering) | 2.4 | how identity claims group into entities |
| 6 | Basis-precedence tie-break | 2.4 | which `link_basis` wins when cross-basis evidence disagrees on cluster assignment |
| 7 | Field resolution + decay | 2.5 | how field claims aggregate per entity; sibling-propagation decay vs. `link_weight` and hop distance |

Categories 4 (scoring) and 6 (tie-break) are the two most likely to get
silently entangled with their parent stage if not named and isolated
explicitly — 4 because it's reused across two stages, 6 because it's easy
to treat as an implicit side-effect of the clustering algorithm rather than
a first-class rule.

## 6. Rules File Architecture

### 6.1 File layout

Each rule category gets its own file rather than one monolithic
`rules.yaml`, so each can be independently tuned, versioned, and diffed:

```
rules/
  extraction.yaml         # regex, OUI lookup, port signatures
  normalization.yaml      # MAC/hostname canonicalization, alias & rebrand maps
  claims.yaml              # field/link_basis vocabulary, source→key mapping
  scoring.yaml              # shared scoring function; separate coefficient
                              # sets for identity vs. field claims
  entity_resolution.yaml   # clustering rules, basis-precedence tie-break
  field_resolution.yaml    # aggregation rules, sibling-propagation decay
```

**Why this matters beyond tidiness:**
- **Scoring parity becomes structural, not conventional.** `scoring.yaml`
  is the single source of truth for the shared formula; entity resolution
  and claim-building both import from it, so parity can't silently drift —
  it would require deliberately forking the file.
- **Independent versioning matches the calibration loop (§7).** Each loop
  stage tunes one file. A diff scoped to `entity_resolution.yaml` alone
  makes it obvious what changed and why, and makes regressions easy to
  bisect back to the responsible rule category.
- **Basis-precedence stays visible** as a named block inside
  `entity_resolution.yaml` rather than buried in code — it doesn't need its
  own file, since it has no meaning outside the clustering context.

**Cross-file integrity:** references between files must be validated at
load time — e.g., `entity_resolution.yaml` referencing `link_basis: mac`
should fail loudly at pipeline init if `mac` isn't declared in
`claims.yaml`, rather than silently producing an empty cluster three stages
downstream. This schema-validation pass belongs in `run.py`'s existing load
step, checked across all six files before any stage executes.

### 6.2 Rule versioning and the run manifest

The auditability constraint (§1) is only half-satisfied by tracing a value
to its evidence — the *rules that scored that evidence* are equally part of
the explanation. A `confidence` of 0.82 emitted last month means something
different if `scoring.yaml` has since been recalibrated. Rule state is
therefore versioned and bound to every run.

**Two identifiers, two jobs.** Rule state carries both a content hash and a
declared version — the same split git makes between a commit SHA and a
release tag, where the tag points at the SHA rather than replacing it.

*The hash is the identity.* At load time (same pass as cross-file validation,
§6.1), `run.py` computes a SHA-256 over the canonicalized content of each of
the six rule files, plus a rollup hash over the ordered set of per-file
hashes. The hash is canonical because it cannot be forgotten — a rule edit
always changes it, whether or not anyone remembered to bump anything. A
declared version alone can lie: edit `scoring.yaml`, forget to bump `1.4.0`,
and two materially different rule states now share a version, making every
output stamped with it silently ambiguous and breaking the audit trail in a
way that's nearly undetectable after the fact.

Per-file hashes are retained alongside the rollup so a diff can be attributed
to a specific rule category: if only `field_resolution.yaml` changed between
two runs, membership results are provably unaffected, and only Stage 4 of the
improvement loop needs re-validation.

*The version is the label.* A hash answers "is this the same rule state?" and
nothing else — it can't tell you which release this was, whether it's newer
than another, or what changed. So the rule set also carries a human-readable
`rules_version` from a single `rules/VERSION` file. One version for the rule
set, not six: per-file semver is more bookkeeping than it's worth, since
per-file *hashes* already provide the change-scoping benefit used in §7.4.

**The manifest is the join key.** Each run emits
`runs/<run_id>/manifest.json`:

```json
{
  "run_id": "2026-08-04T14:22:07Z-a3f9c1",
  "input_hash": "sha256:…",           // observations.csv
  "rules_version": "1.4.0",
  "rules_rollup": "sha256:…",
  "rules_files": {
    "extraction.yaml":        "sha256:…",
    "normalization.yaml":     "sha256:…",
    "claims.yaml":            "sha256:…",
    "scoring.yaml":           "sha256:…",
    "entity_resolution.yaml": "sha256:…",
    "field_resolution.yaml":  "sha256:…"
  },
  "version_verified": true,
  "engine_commit": "git:…"
}
```
Output rows carry only `run_id`; both rule identifiers resolve through the
manifest. This keeps `membership.csv` and the entity pool narrow — `run_id`
is constant across a run, so repeating six hashes on every row would be pure
duplication — while preserving full traceability from any single record back
to the exact rule state and input that produced it.

**The hash polices the version.** The load pass gains one more check: if the
rollup hash differs from the last recorded run but `rules_version` is
unchanged, that's a forgotten bump — warn loudly (or fail, once the rule set
stabilizes). This converts semver's inherent weakness, that it's advisory and
humans forget, into something actively enforced. `version_verified` records
whether that check passed, so a run made under a stale version is
self-identifying rather than quietly wrong.

`engine_commit` is included because rule files alone don't determine
behavior: the Python engine interprets them, and an engine change can alter
output with rules untouched. Versioning rules without versioning the engine
would leave a reproducibility gap.

**Label-set versioning deliberately lives elsewhere.** `labels_version` and
`labels_hash` are *not* in this manifest — `run.py` never reads labels
(invariant #5), so a run manifest asserting a label hash would be claiming
knowledge the pipeline structurally cannot have. Label versioning belongs to
the eval harness's own manifest (§7.6), which is the only component that
reads both sides.

### 6.3 Canonical vocabulary

`canonical_vocab.csv` defines the closed value domains for `vendor` and
`device_type`. It lives in `rules/` alongside the rule files, is referenced
by `claims.yaml`, and — unlike `metrics.yaml` (§8.1) — is **inside the rules rollup hash**,
because changing it changes pipeline output. Adding a vendor is a rules
change and bumps the rules version accordingly.

**The file holds two independent column-wise lists, not paired rows.** 30
vendors and 17 device types, padded with blanks to a common row count. Row
alignment is an artifact of CSV shape: `Axis Communications,ip_camera`
appearing on one row does **not** assert that Axis makes IP cameras. The
loader must read each column independently, drop blanks, and must not derive
any vendor↔device_type constraint from row position. A loader that treats
rows as pairs would silently invent a constraint absent from the data.

No cross-field plausibility constraint is imposed in either direction. There
is no evidence in this artifact for which vendors produce which device types,
and inventing one would reject legitimate combinations.

**Casing convention differs per column by design** — vendors are title-case
proper nouns (`Hikvision`, `HP Inc.`), device types are snake_case
identifiers (`ip_camera`, `access_control_panel`). The escape values follow
suit: `Unknown` for vendor, `unknown` for device_type. This is each column
observing its own convention, not an inconsistency to be normalized away.

**Load-time validation** (§6.1's cross-file pass) asserts that every alias
target in `normalization.yaml` and every OEM rebrand target resolves to a
vocabulary member. A rebrand map pointing at a vendor not in vocab is a
latent bug that would otherwise surface as an unexplained `Unknown` at
resolution time.

**Claims are not vocabulary-constrained; only resolved output is.** If claim
construction rejected out-of-vocab values, the evidence that the vocabulary
or alias map is incomplete would be discarded at exactly the moment it was
generated. Claims therefore carry whatever normalization produced, tagged
in-vocab or not, and the constraint applies at field resolution (§2.5) where
the rejection is visible in the trace and countable in metrics.

**Vocabulary expansion has a natural work queue:** the distinct set of
`vocab_reject` values ranked by frequency. This is also high-quality input to
LLM rule mining (§7.3 Stage 5) — a frequently-rejected value is either a
missing vocabulary entry or a missing alias, and the frequency distinguishes
which is worth acting on.

**For the current dataset the vocabulary is already complete.** Every vendor
and device type appearing in the initial label set is present in
`canonical_vocab.csv` — zero genuinely out-of-vocab labels. The vocab-gap
machinery will therefore be exercised only by the **alias-gap** path:
unmapped surface strings such as `LTS Security`, `Amcrest`, `Wisenet` and
`VVTK` failing to normalize onto an existing vocabulary member. §6.3's
motivating example (`Ruckus`, a vendor genuinely absent from vocab) has no
instance in this data. No mechanism changes — both paths produce `Unknown`
plus a `vocab_reject` step — but the interpretation differs: a `vocab_reject`
here indicates a `normalization.yaml` gap, not a missing vendor.

**Source coverage is validated the same way.** Every distinct `source` value
in `observations.csv` must have a mapping in `claims.yaml`; an unmapped source
emits an explicit `unmapped_source` trace step rather than silently
contributing nothing (same family as §9.3's absence steps). The source list is
data-driven and has already grown once — `telnet_banner` appears in the data
beyond the originally documented ONVIF/SNMP/HTTP/mDNS set.

## 7. Rule Improvement Loop (Human & LLM Labels)

Human and LLM labels improve rules — they are **not** runtime pipeline
inputs. This preserves determinism: an LLM call is neither guaranteed
reproducible nor auditable to a fixed rule, so it must never enter
claim-building as a live "source."

### 7.1 Roles
- **Human labels** → ground truth, used for calibration and validation.
- **LLM labels** → candidate rule proposals, always human-reviewed before
  merging into the relevant rules file, never written directly.

*(Optional, deliberate exception: if LLM extraction output is frozen to a
checked-in artifact at data-prep time — not called live — it can be treated
as a legitimate low-`base_weight` `source` subject to the same scoring
formula. Per-field opt-in, not a default.)*

### 7.2 Ground truth construction

Build `labels.csv` before any tuning begins and freeze it as the regression
baseline. Sample deliberately across all sources, all sites, and — most
importantly — obs that currently produce low-confidence claims or
small/singleton entity clusters, since that's where rule gaps concentrate.
This split defines two strata referenced throughout: the **easy stratum**
(unambiguous evidence) and the **hard stratum** (pipeline-uncertain cases).

**Schema:**

```
labels.csv:
  obs_id, key_type, key, value, status, label_basis,
  labeler_certainty, blinded, obs_hash, labeled_by, labeled_at
```

- `key_type` ∈ `field | link_basis`; `key` is the canonical vocabulary term
- `status` ∈ `proposed | agreed | disputed | adjudicated | undecidable`
- `label_basis` ∈ `physical_inspection | asset_inventory | vendor_doc |
  payload_inference`
- `labeler_certainty` ∈ `high | medium | low` — the labeler's own difficulty
  assessment; defines the dual-label stratum (§7.2.3)
- `blinded` — whether the labeler saw pipeline output (§7.7)
- `obs_hash` — hash of the observation row this label was made against

**Long format is authoritative.** A wide per-observation label file (one row
per `obs_id` with a column per field) is a *rendering* of this schema, not the
schema. Wide files carry no `status`, `label_basis`, or `blinded` — the three
columns §7.2.1, §7.2.2 and §7.7 are built on. On import from a wide file,
provenance is stamped honestly rather than optimistically: `status: proposed`,
`label_basis: payload_inference`, `blinded: false` unless known otherwise, and
`obs_hash` computed at import.

**A blank is not an assertion.** Blank cells mean "not derivable from *this*
payload," and are excluded from precision/recall denominators — never counted
as incorrect. Scoring a correctly sibling-propagated value as wrong against a
blank would break §8.4 Stage 4 precisely where it is meant to work.

### 7.2.1 Three kinds of disagreement

Disagreement is not one problem, and only the second kind is adjudication:

**1. Vocabulary disagreement — a rules defect, not a labeling one.** Two
labelers write `Hikvision` and `HIKVISION`, or `camera` vs `ip_camera`. They
agree about the device and differ on the string. Adjudicating this case by
case papers over a gap in `claims.yaml` vocabulary or `normalization.yaml`.
Prevent it instead: labels are entered against the *declared* vocabulary — a
closed pick-list where the vocabulary is closed (`device_type`), free text
where it isn't (`model`) but passed through `normalization.yaml` before
comparison. A labeler needing a value outside the vocabulary is filing a
rules change request, not a label. This collapses a large share of raw
disagreement before any adjudicator is involved, and routes it to the right
place.

**2. Resolvable factual disagreement — the real adjudication case.**
Labelers genuinely differ on what the device is, and an external fact settles
it. Handled by `label_basis` precedence (§7.2.2).

**3. Undecidable from evidence — must not be adjudicated.** The payload
genuinely underdetermines the answer; neither a competent labeler nor any
possible rule could resolve it. Forcing these to a single `correct_value`
scores the rules against something unlearnable from the observation, docking
Stage 1 recall for cases no rule could have succeeded on — distorting exactly
the metric the loop gates on.

`undecidable` is therefore a first-class `status`, **excluded from
precision/recall denominators** and reported separately as a coverage
statistic. "12% of sampled obs are undecidable from payload" is a finding
about the *sources*, not a rules failure, and it's actionable in a different
direction: add a protocol probe, not a regex.

### 7.2.2 Adjudication by basis precedence

Most type-2 disputes resolve without a human meeting. `label_basis` is a flat
precedence order:

```
physical_inspection > asset_inventory > vendor_doc > payload_inference
```

A label backed by physical inspection beats one backed by payload inference,
deterministically. This is intentionally **not** a scoring formula — resist
recursing the claim-scoring math (§2.3) onto labels. A flat precedence list
is sufficient and stays legible; ground truth that needs a weighted
confidence model is no longer serving as ground truth.

A human adjudicator is required only when two labels share the same basis
tier and disagree. Those cases move to `status: disputed`, and once resolved,
`status: adjudicated`.

### 7.2.3 Tiered dual-labeling

**Two axes, deliberately not conflated.** Earlier drafts used "hard stratum"
for two unrelated things:

- **Labeling difficulty** — a *human* found this payload hard to label. A
  property of the observation and the labeler, known before any pipeline run.
- **Result stability** — the pipeline's evidence stack for a result is
  contested or thin (§2.6). A property of a run's output.

These do not track each other. A clean ONVIF banner is labeling-easy and
stable; `OBS-044`'s truncated `Server: Ax` is labeling-hard yet the pipeline
may resolve it stably (and wrongly). Each drives a different mechanism:

| Axis | Source | Drives |
|---|---|---|
| Labeling difficulty | `labeler_certainty` (imported) | dual-labeling effort (§7.2.3), blind adjudication (§7.7) |
| Result stability | `stability` field (§2.6) | where calibration is scrutinized (§8.4) |

**The dual-label stratum is defined by `labeler_certainty`**, imported from
the initial label file as `high | medium | low`. `high` → single-label;
`medium` and `low` → dual-label and blind-adjudicate. Using human certainty
here has a practical advantage beyond correctness: it requires no pipeline run
to compute, so labeling never waits on a bootstrap run and the stratum doesn't
thrash when scoring is recalibrated.

Stability is deliberately *not* a labeling stratum. It is a continuous output
property, and §8.4 uses it by asking the honest question directly — *are
low-stability results in fact less accurate?* — which requires no partition
at all.

Agreement is measured where it's informative — nobody learns anything from two
people agreeing that a clean ONVIF banner reads Axis — at a fraction of full
dual-labeling cost.

Agreement metrics: raw agreement rate for open-vocabulary fields like
`model` (Cohen's kappa requires a fixed category set and misbehaves on open
vocabularies), Cohen's kappa for closed vocabularies and for pairwise
same-device judgments.

**Cross-axis diagnostic.** Cases that are labeling-easy but low-stability are
the most informative disagreements in the set: a human read it trivially and
the pipeline did not, which isolates a pure rules gap from genuinely ambiguous
payloads where both struggle.

At the current 74-observation scale everything could be dual-labeled today;
the tiering is established now because it's cheap to adopt before the label
set grows past the point where retrofitting is painful.

### 7.2.4 Transitivity consistency check

Pairwise identity labels can be internally incoherent: a labeler asserts
A~B and A~C but B≁C. That violates transitivity regardless of who is right,
and detecting it requires no adjudicator — it's a mechanical check over the
label set, run **before** the labels are used. Stage 3 clustering validation
against a transitively-inconsistent label set produces meaningless precision
numbers.

### 7.3 Improvement sequence (gated, mapped to rule files)

| Stage | Rule file(s) tuned | Gate before proceeding |
|---|---|---|
| 1 — Extraction & normalization precision/recall | `extraction.yaml`, `normalization.yaml` | Recall must be solid — every downstream stage inherits extraction quality |
| 2 — Claim-scoring calibration | `scoring.yaml` (field-claim coefficients) | Claim ranking must match labels — the shared function means a formula-shape fix here also fixes Stage 3 |
| 3 — Entity resolution validation | `scoring.yaml` (identity-claim coefficients), `entity_resolution.yaml` | Bias toward precision over recall — a false merge corrupts every member's fields via propagation in Stage 4 |
| 4 — Field resolution / decay | `field_resolution.yaml` | Last, since it inherits every upstream error |
| 5 — LLM-proposed rule mining (ongoing) | any, via human-reviewed diff | Every proposal re-run through Stage 1's precision/recall check before merge |

### 7.4 When stage gates are not valid gates

Two conditions void a gate regardless of the number it reports.

**Same-author labels.** Invariants #5 and #6 (§4) block *mechanical*
contamination — no code path reads labels, and the adjudication packet omits
resolved values. They do not block *same-head* contamination. If one party
authors both the rules and the labels, ground truth and the thing being graded
come from the same judgment, and Stages 1–4 measure self-consistency rather
than correctness — the exact failure §7.7 exists to prevent, arriving by
another route.

Stage gates calibrated against a label set authored by the rule author are
therefore **provisional, not valid gates**. They become valid when labels
exist at a higher `label_basis` tier (asset inventory, physical inspection) or
come from an independent labeler. An initial same-author label set is a
bootstrap for building the loop's machinery, not yet ground truth for
certifying the pipeline.

**Insufficient positive pairs at Stage 3.** Pairwise clustering metrics
require enough positive (same-device) pairs to be meaningful. In the current
dataset, ground truth is 68 entities over 74 observations — 63 singletons,
with only 5 multi-observation entities covering 11 observations, yielding
roughly 5 positive pairs. Any threshold "met" at that n is noise.

Stage 3 metrics are therefore **reported but non-gating** until positive-pair
count crosses a stated floor (§11). The small multi-observation set is still
valuable, but as **named regression cases** that must each resolve correctly
in isolation, rather than as a rate:

| Entity | Observations | What it tests |
|---|---|---|
| `E-001` | OBS-001/002/003 | three sources, one MAC — clean corroboration |
| `E-002` | OBS-004/005 | two sources, one MAC, differing hostnames |
| `E-052` | OBS-047/072 | same MAC, different hostname *and* IP — the §2.4 cross-basis contradiction; `mac` must beat `hostname_token` |
| `E-066` | OBS-061/073 | empty MAC on OBS-073 — must link by serial or hostname |
| `E-074` | OBS-069/074 | within-entity firmware conflict → `undecidable` (§2.5) |

### 7.5 Iteration

Re-run Stages 1→4 in order whenever any rules file changes or the label set
grows — a new extraction rule can shift claim rankings, which can shift
clustering, which can shift propagation. Per-file rule hashes (§6.2) scope
this: if a change touched only `field_resolution.yaml`, Stages 1–3 are
provably unaffected and only Stage 4 needs re-running. The frozen label set
from §7.2 is the standing regression check on every pass, not a one-time
bootstrap.

### 7.6 Eval harness and regression protection

**The harness sits outside the pipeline.** `eval.py` is a separate entry
point that invokes `run.py` as a black box and scores its outputs against
`labels.csv`. It is not a flag on `run.py`. This keeps invariant #5 (§4)
structural rather than conventional: because the pipeline has no code path
that reads labels, ground truth cannot leak into scoring, and the
deterministic runtime path stays free of evaluation logic.

**The eval manifest versions both sides.** The harness is the only component
that reads both rules and labels, so it emits its own manifest recording
both:

```json
{
  "eval_id": "…",
  "run_id": "2026-08-04T14:22:07Z-a3f9c1",
  "rules_rollup": "sha256:…",
  "labels_version": "0.3.0",
  "labels_hash": "sha256:…",
  "baseline_run_id": "…",
  "baseline_labels_hash": "sha256:…"
}
```

**Label mutation must not masquerade as rule regression.** The four-bucket
diff below compares outcomes across rule versions against a *frozen* label
set — but adjudication (§7.2.2) mutates labels. If a label flips from
`Dahua` to `Hikvision` between eval runs, the affected case lands in `broken`
and reads as a rule regression when in fact the ground truth moved.

The harness therefore **refuses to compute a single-axis four-bucket diff
across differing `labels_hash` values.** When both rules and labels have
changed, it runs two passes instead — rules-held-constant (isolating the
label delta) and labels-held-constant (isolating the rule delta) — so the two
causes stay separable. Without this, adjudication silently corrupts the
regression signal.

**Aggregate metrics are insufficient on their own.** A rule change can
improve overall precision/recall while silently breaking specific cases that
previously resolved correctly — a widened regex that fixes ten observations
and breaks two that matched a narrower pattern nets +8 and looks like an
unambiguous win. The aggregate hides the two.

The harness therefore diffs **per-label outcomes**, not just totals. On each
rule change it scores the frozen label set under both the previous and the
new rule version (identified by rollup hash, §6.2) and classifies every label
into one of four buckets:

| Bucket | Meaning |
|---|---|
| `fixed` | incorrect → correct |
| `broken` | correct → incorrect **(regression)** |
| `stable_correct` | correct → correct |
| `stable_incorrect` | incorrect → incorrect |

**Regressions are logged, not blocked.** The `broken` set is written to
`runs/<run_id>/regressions.csv` — one row per flipped label, with the
`obs_id`, the affected `field`/`link_basis`, the before and after values, and
the per-file hash diff identifying which rule category changed. Merging is
not gated on this; the log is advisory.

**The harness proposes the version bump.** Because it already computes the
four-bucket diff between the old and new rule state, it can derive the
appropriate `rules/VERSION` bump (§6.2) from measured behavior rather than
leaving it to whoever wrote the commit:

| Observed change | Proposed bump | Rationale |
|---|---|---|
| No label outcomes changed | **patch** | comments, reordering, refactors — behavior-neutral |
| Outcomes changed, vocabulary intact | **minor** | new rules or recalibration; consumers still see the same schema |
| `canonical_vocab.csv` **narrowed** (value removed) | **major** | a value a consumer previously saw can no longer be emitted — same breakage class as a schema change, even though the schema is untouched |
| `canonical_vocab.csv` **widened** (value added) | **minor** | additive; existing values keep resolving as before |
| `claims.yaml` vocabulary changed (added/removed `field` or `link_basis`) | **major** | downstream consumers of the entity pool or `membership.csv` may break |

The proposal is advisory to the human, but it pairs with the load-time
enforcement in §6.2: the harness suggests the level, and the hash check
catches it if the bump never happens.

That choice trades enforcement for velocity, which is reasonable while the
rule set is still moving fast — but it means the protection is only as good
as the log actually being read. Two things make that likely rather than
aspirational:

- **Surface, don't bury.** The `fixed`/`broken` counts and the full `broken`
  list belong in the eval scorecard output and in `REPORT.md`, not only in a
  side file someone has to know to open.
- **Make it cumulative.** A label that flips `broken` and is never fixed
  should stay visible across subsequent runs rather than scrolling out of
  view after the run that caused it, so persistent regressions accumulate
  visibly instead of being forgotten one run at a time.

If regression volume later outgrows advisory handling, the natural escalation
is to gate only the highest-blast-radius category — a `broken` flip on an
identity claim or a same-device/different-device pair (§2.3: these corrupt
every field on every member) — while leaving field-claim flips advisory.

### 7.7 Blind adjudication

Invariant #6 (§4) is the mirror of #5: labels don't reach the runtime path,
and pipeline output doesn't reach hard-stratum label creation.

**Blind** means the adjudicator sees the raw evidence — `raw_payload`,
hostname, MAC, open ports — plus any external source their `label_basis`
tier permits, but *not* the pipeline's resolved values. Unblinded means they
also see `vendor=Hikvision, confidence=0.71` on screen while deciding.

**Why it matters, and only for the hard stratum.** A human handed a plausible
answer and asked "is this right?" agrees more often than one asked "what is
this?". Unblinded labeling drifts ground truth toward whatever the pipeline
already believes, after which Stages 1–4 measure self-consistency rather than
correctness — a silent, self-reinforcing failure, worst precisely where the
pipeline is confidently wrong. In the easy stratum the evidence dominates any
suggestion (a clean banner reading `AXIS P3245-LVE` anchors nothing), so
blinding there costs throughput and buys little. The hard stratum is *defined*
as cases where the pipeline was unsure — exactly where independent human
judgment is the entire value of the label, and where a displayed guess most
easily substitutes for reasoning. It also disproportionately drives Stage 2–3
calibration.

**Selection may read pipeline output; presentation must not.** The export
tool necessarily reads `confidence` and cluster size to *identify* the hard
stratum. What it must not do is put those columns in front of the human. It
sits where the harness sits — outside the pipeline, with read access to both
sides — and emits an evidence-only packet:

```
adjudication/<run_id>/packet.csv
  obs_id, obs_hash, source, raw_payload, mac, hostname, open_ports, site
```

No `vendor`, no `model`, no `confidence`, no `entity_id`. `run_id` and
`obs_hash` are retained so returned labels join back cleanly — the join key
survives, the answer doesn't. Blinding is then structural rather than
procedural: the column isn't hidden by policy, it isn't in the file, so no one
has to be trusted not to look.

**`blinded` is recorded per label, not assumed per stratum.** Beyond
provenance, this makes the anchoring effect measurable: compare how often
blinded vs. unblinded labels agree with the pipeline's answer at comparable
difficulty. If unblinded agrees at 94% and blinded at 79%, the bias is
quantified rather than asserted — and if the gap turns out small for this
data, blinding can be relaxed on evidence instead of guesswork.

**Round-trip guards on import:**
- *Reject out-of-packet labels.* A returned label whose `obs_id` wasn't in the
  packet it was exported from is refused — this catches adjudication done
  from a full export via a back channel.
- *Bind to evidence, not just the run.* If the returned `obs_hash` no longer
  matches the current `observations.csv` row, the label is stale and is
  flagged rather than silently merged.

**Labels are sticky, to control recurring cost.** Once an `obs_id` is
adjudicated at a given `label_basis` tier, it is not re-adjudicated unless
its `obs_hash` changed or new evidence arrives at a higher tier (payload
inference superseded by asset inventory). Otherwise every recalibration that
shifts confidence scores would push previously-settled observations back into
the hard stratum and re-queue them, paying the blind-adjudication premium
repeatedly for labels that never changed.

Stickiness is also what makes the §7.6 two-pass diff load-bearing rather than
theoretical: labels mostly hold still but occasionally move, which is exactly
the mutation pattern that would otherwise contaminate the regression signal.

## 8. Metrics

### 8.1 Metric registry

Metrics are declared in `metrics.yaml`, beside the rules directory but
**outside the rules rollup hash** — a metric definition does not affect
pipeline output, so it must not change the rules version. It carries its own
`metrics_hash` in the manifest, so "recall dropped" can never silently mean
"we changed how recall is computed."

```yaml
# metrics.yaml
field_fill_rate:
  scope_type: field
  requires_labels: false
  direction: higher_better
  description: Share of entities with a non-null value for this field
```

`requires_labels` lets a plain run emit the label-free subset cleanly rather
than writing nulls for metrics it structurally cannot compute.
`direction` lets a renderer or gate mark a trend delta as improvement or
regression without hardcoding that knowledge.

### 8.2 Long format

`metrics.jsonl` is long, not wide — one row per measurement:

```jsonl
{"metric":"field_fill_rate","scope":"field:model","value":0.78,"n":41}
{"metric":"field_fill_rate","scope":"field:model:propagated","value":0.31,"n":41}
{"metric":"corroboration_rate","scope":"link_basis:mac","value":0.62,"n":58}
{"metric":"singleton_rate","scope":"global","value":0.19,"n":74}
{"metric":"cross_basis_conflict_rate","scope":"global","value":0.04,"n":74}
```

Wide format breaks trending the moment a metric is added — historical runs
have missing columns and every query reconciles schemas. Long format makes a
new metric simply new rows.

`scope` carries the dimension (`global`, `field:model`, `source:snmp`,
`link_basis:mac`), so one metric name works at every granularity without name
explosion like `fill_rate_model_propagated`. `n` is mandatory: precision of
1.00 at n=3 must not render identically to n=300.

### 8.3 Label-free metrics (every run)

These need no ground truth and are the **drift detector** — they catch
movement in the gap between rule changes and eval runs.

**Coverage / completeness**
- `field_fill_rate` per `field`, split by provenance: directly claimed vs.
  sibling-propagated. 90% `model` fill means something different if 60% of it
  arrived by propagation.
- `known_rate` for closed-vocabulary fields — share of entities whose
  `vendor`/`device_type` is **not** the escape value. Fill rate is
  structurally 100% for these fields since they are never null (§2.5), so
  `field_fill_rate` carries no signal there and `known_rate` replaces it.
- `vocab_gap_rate` — share of entities resolving to `Unknown` because an
  extracted value fell outside the vocabulary, as distinct from having no
  evidence at all. Only this half is actionable: it says the vocabulary or
  alias map is incomplete, not that the device is unidentifiable.
- `vocab_reject_frequency`, scoped per rejected value — the ranked expansion
  queue described in §6.3.
- `no_extraction_rate` — obs yielding zero extracted values (extraction dead
  zones)
- `no_identity_claim_rate` — obs unclusterable by construction

**Evidence structure**
- `claims_per_obs`, scoped per `field`/`link_basis`
- `corroboration_rate` — share of claims with ≥2 independent witness groups;
  reveals whether the independence bonus ever fires or every claim is
  single-source
- `conflict_rate` per key
- `source_win_rate` — which sources produce claims that win vs. get outvoted.
  A source that never wins is redundant or mis-weighted in `scoring.yaml`.

**Clustering shape**
- `entity_count`, `cluster_size_distribution`, `singleton_rate`
- `link_basis_distribution` across membership rows — which bases do the
  clustering work
- `basis_agreement_rate` and `cross_basis_conflict_rate` — how often the
  tie-break rule (§2.4) is invoked at all
- `propagation_depth_distribution` — how far values travel, and the
  `link_weight` × hop decay actually applied

**Confidence**
- `confidence_distribution`, and share below the actionable threshold
- scoped per field, to expose which fields are systematically shakier

### 8.4 Label-dependent metrics (eval runs)

Mapped to the §7.3 loop stages:

| Stage | Metric |
|---|---|
| 1 | extraction precision/recall per rule, per `field`/`link_basis` |
| 2 | top-1 claim accuracy — does the highest `claim_weight` claim match the label |
| 3 | pairwise clustering precision/recall, with **false-merge and false-split counted separately** since their costs differ sharply (§2.3) |
| 4 | propagated-value accuracy, specifically on fields absent from an obs's own claims |
| — | `undecidable_rate` (§7.2.1) — a coverage statistic about the *sources*, excluded from precision/recall denominators |

**Confidence calibration** deserves separate mention: bucket entities by
predicted confidence and compare observed accuracy per bucket. This asks
whether confidence is *honest*, not whether it is high. A 0.9 bucket that is
right 70% of the time is actively misleading to downstream consumers — worse
than emitting no confidence at all. It is distinct from Stage 2 top-1
accuracy, which validates ranking only, not magnitude, and it is the metric
that determines whether `scoring.yaml`'s coefficients mean anything.

**Stability validation** is the parallel check for §2.6's second number, and
it needs no partition: bucket results by `stability` and ask whether accuracy
falls as stability falls. If it doesn't, `stability` isn't measuring anything
and its component weighting needs revisiting. This is also where the
high-confidence / low-stability quadrant earns its keep — those results should
show materially worse accuracy than high-confidence / high-stability ones, and
if they don't, confidence alone was sufficient after all.

### 8.5 Process metrics

- Four-bucket counts per rule change (`fixed`/`broken`/`stable_*`), plus
  cumulative unfixed regressions (§7.6)
- Inter-annotator agreement on the dual-labeled hard stratum (§7.2.3)
- Blinded vs. unblinded agreement-with-pipeline gap (§7.7) — the anchoring
  measurement
- LLM proposal accept/reject rate over time (§7.3 Stage 5)

### 8.6 Trend store

Each run's `metrics.jsonl` appends to a single `history.jsonl` keyed by
`run_id`, so trending doesn't require walking every run bundle. Joined to
`manifest.json`, this yields metrics-by-rule-version directly — "singleton
rate jumped at rules 1.4.0" falls out of a query rather than manual
comparison.

This is also what makes the §7.3 stage gates and the §11 stopping-criteria
item expressible as threshold expressions the harness evaluates, rather than
prose judgments — and it's a precondition for the §7.6 escalation path
(gating identity-claim regressions while leaving field-claim flips advisory),
which cannot be implemented against unstructured output at all.

## 9. Trace & Replay

Persisting evidence and decisions makes outputs *auditable*; it does not make
them *traced*. `membership.csv` records that obs 1043 landed in entity A, but
cluster identity is emergent from a sequence of merge decisions, and that
sequence isn't recoverable from the outcome. "Why is A with B" currently has
no answer beyond "the algorithm said so."

### 9.1 Specify by the questions the trace must answer

Trace scope is defined as a fixed question set, because "more logging" has no
stopping condition. For any emitted value:

1. Which extraction rule fired, on which substring of which `raw_payload`?
2. What was the pre-normalization value, and which normalization rule
   transformed it?
3. How did `claim_weight` decompose — which source supplied the max base,
   which witness groups earned the bonus, which conflicting claims caused the
   penalty?
4. Why is this obs in *this* entity — which merge steps, in what order, and
   which merges were **refused**?
5. Was this field value direct or propagated? If propagated: from which
   sibling, at what hop distance, with what decay applied?
6. What lost? Which competing claim ranked second, and by how much?
7. If two candidates tied, what broke the tie?

### 9.2 Structure: a derivation DAG, not a log

`trace.jsonl` holds one row per derivation step:

```jsonl
{"step_id":"sha256:…","op":"extract","rule_id":"extraction.yaml#hik_model_v2",
 "inputs":["obs:1043#raw_payload[41:58]"],"output":"DS-2CD2143G0-I",
 "parents":[]}

{"step_id":"sha256:…","op":"normalize","rule_id":"normalization.yaml#vendor_alias",
 "inputs":["sha256:…"],"output":"Hikvision","before":"HIKVISION DIGITAL"}

{"step_id":"sha256:…","op":"score","rule_id":"scoring.yaml#field_coefficients",
 "output":0.83,
 "decomposition":{"base_max":0.6,"base_from":"onvif","bonus":0.25,
                  "witness_groups":["onvif","snmp"],"penalty":0.02,
                  "conflicting_steps":["sha256:…"]}}

{"step_id":"sha256:…","op":"merge_refused","rule_id":"entity_resolution.yaml#basis_precedence",
 "inputs":["obs:1043","obs:1088"],"reason":"cross_basis_conflict",
 "detail":{"mac":"entity_A","hostname_token":"entity_B","precedence_winner":"mac"}}
```

**`step_id` is content-addressed, not a sequence counter.** This carries more
weight than it appears: with content-derived IDs, two runs over identical
input and rules produce byte-identical traces, making traces *diffable across
rule versions*. A sequence counter would make every trace superficially
different and destroy that property. It is the same determinism discipline
already applied to rule versioning in §6.2.

### 9.3 Four principles

**Trace by construction, not reconstruction.** If the engine computes and
*then* separately logs what it believes it did, the two drift — and the trace
becomes fiction precisely in the cases where the code is buggy, which is when
it's needed. Trace steps are emitted from the decision path itself, so a code
path that doesn't emit is a code path that doesn't execute.

**Rejected alternatives carry most of the value.** Recording the winner
explains little; recording that `Dahua` scored 0.81 against `Hikvision`'s
0.83 explains the result *and* its fragility. Likewise a refused merge is a
decision, not a non-event — `merge_refused` steps are first-class.

**Every step names its rule and version.** `rule_id` is an addressable anchor
(`file#rule_name`) resolving through the manifest's per-file hashes to exact
rule text. Without this, a trace from three months ago points at rules whose
meaning has since changed.

**Absence needs a step too.** `no_extraction` and `no_identity_claim` emit
explicit steps rather than silently producing nothing. "No rule matched" and
"a rule matched and yielded nothing" are different failures with different
fixes, and only one is a rules gap.

**Vocabulary rejections are steps, not silences.** A `vocab_reject` step
records the extracted-and-normalized value that failed closed-vocabulary
validation (§2.5), the field it targeted, and the rule that produced it.
Without this step the rejected value simply vanishes and the vocabulary gap
is never learned; with it, `Unknown` at the output always has a stated cause.

### 9.4 Volume controls

Trace will dwarf the outputs. Two controls that don't compromise the
guarantee:
- Store **references** into `raw_payload` (offsets, as in the `extract`
  example) rather than copying payload text.
- Keep score decomposition's `conflicting_steps` as `step_id` references
  rather than embedded objects.

**Do not sample.** A sampled trace cannot pass replay (§9.5), and the
guarantee is the entire point.

### 9.5 Replay: the completeness test

`replay.py` reads `trace.jsonl` **alone** — no `observations.csv`, no rules —
and reconstructs `claims.csv`, `membership.csv`, and `entities.csv`. The
reconstruction is diffed against the actual run outputs.

If they match, the trace is *provably* sufficient to explain every emitted
value. If any field cannot be reconstructed, the diff names the hole
precisely. This converts trace completeness from an aspiration into a
regression test run on every build, and is the mechanical enforcement behind
invariant #7 (§4).

**What replay does and does not prove.** Replay reconstructs from step
**outputs**, which §9.2's schema carries. It proves the trace *explains* every
emitted value; it does **not** independently re-derive that value from source
evidence. The `raw_payload` offsets stored per §9.4 are provenance —
verifiable only when `observations.csv` is also present, which can be an
optional `--deep` mode. The §9.5 guarantee is the shallow one, and that is
sufficient: an output value with no derivation fails immediately either way.

### 9.6 Interaction with the improvement loop

Replay belongs in the harness bundle alongside `eval.py` (§7.6). Beyond
completeness checking, trace diffing gives the four-bucket diff a *reason*
for each flip rather than only the fact of it: a `broken` label whose trace
shows a widened regex now matching a different substring is a diagnosis, not
a data point. Because `step_id` is content-addressed (§9.2), that diff is a
direct comparison between two runs' traces rather than a bespoke
investigation.

## 10. Build Plan

This is a **greenfield build** against the current design. A prior five-file
implementation exists (`evidence.py`, `engine.py`, `rules.yaml`, `resolve.py`,
`run.py`) and is useful as reference for extraction patterns and scoring
intent, but is not the starting point: the design has since moved to six rule
files, trace-by-construction, manifests, and replay. Retrofitting
trace-by-construction into an engine not built for it produces exactly the
reconstruction-style tracing §9.3 warns against.

### Phase 1 — Rules and loading
- [ ] Create `rules/` with the six files (§6.1) plus `VERSION` and
      `canonical_vocab.csv`
- [ ] Load `canonical_vocab.csv` **column-wise**, not row-wise; drop blanks;
      infer no vendor↔device_type pairing (§6.3)
- [ ] Cross-file schema validation at load: `link_basis`/`field` references
      resolve; every alias and OEM rebrand target is a vocabulary member;
      every `source` in observations has a `claims.yaml` mapping (§6.1, §6.3)
- [ ] Per-file + rollup rule hashes, `canonical_vocab.csv` inside the rollup
      (§6.2, §6.3)
- [ ] Stale-bump check (rollup changed, `VERSION` didn't → warn); record
      `version_verified`
- [ ] `rule_id` anchors (`file#rule_name`) on every rule definition

### Phase 2 — Pipeline
- [ ] Extraction + normalization (§2.2)
- [ ] Claim construction with the §2.3 scoring function: saturating
      `independence_bonus`, `[0,1]` clamp, separate identity/field coefficient
      sets
- [ ] Entity resolution: deterministic union-find, pinned merge order,
      content-addressed `entity_id`, basis-precedence tie-break (§2.4)
- [ ] `membership.csv` with `basis_agreement` (bool) and `conflict_detail`
      (nullable)
- [ ] Field resolution: multiplicative decay, highest-weight-wins for static
      fields, `firmware` conflict → `undecidable`, closed-vocabulary
      constraint emitting `Unknown`/`unknown` at `confidence = 0.0` (§2.5)
- [ ] Exclude `Unknown` from sibling propagation and from `conflict_penalty`
- [ ] Confidence model: per-field confidences, harmonic-mean entity rollup,
      `stability` from margin / witness dependence / live conflict (§2.6)

### Phase 3 — Outputs and trace
- [ ] Run bundle: `manifest.json`, `claims.csv`, `membership.csv`,
      `entities.csv`, `resolutions.csv`, `metrics.jsonl`, `trace.jsonl` (§3)
- [ ] `resolutions.csv` with per-field `*_provenance` ∈
      `direct | propagated | unknown` (§3.1)
- [ ] `run_id` + `derivation_step` on every tabular row; `resolve_entity`
      fan-in step for multi-field rows
- [ ] `trace.jsonl` emitted from the decision path itself, content-addressed
      `step_id` (§9.2, §9.3)
- [ ] Explicit absence steps: `no_extraction`, `no_identity_claim`,
      `merge_refused`, `vocab_reject`, `unmapped_source`
- [ ] `replay.py` + replay diff as a per-build check (§9.5)
- [ ] `REPORT.md` as a pure render over the bundle (§3)

### Phase 4 — Metrics
- [ ] `metrics.yaml` registry with `requires_labels` / `direction`, hashed
      separately from the rules rollup (§8.1)
- [ ] `metrics.jsonl` long format with mandatory `n` (§8.2)
- [ ] Label-free metric set including `known_rate`, `vocab_gap_rate`,
      `vocab_reject_frequency`; suppress `field_fill_rate` on
      closed-vocabulary fields (§8.3)
- [ ] `history.jsonl` append for trending (§8.6)

### Phase 5 — Labels and eval
- [ ] Import the initial wide label file to the §7.2 long schema; stamp
      `status: proposed`, `label_basis: payload_inference`, `blinded: false`,
      computed `obs_hash`; preserve `labeler_certainty`
- [ ] Treat blanks as no-assertion, excluded from denominators
- [ ] Vocabulary-constrained label entry (closed pick-lists from
      `canonical_vocab.csv`; open fields normalized before comparison)
- [ ] `label_basis` precedence resolution; same-tier conflicts →
      `status: disputed`
- [ ] Transitivity consistency check before labels are used (§7.2.4)
- [ ] `eval.py` as a standalone harness invoking `run.py` (§7.6)
- [ ] Eval manifest with `labels_version` / `labels_hash`; two-pass diff when
      both rules and labels changed
- [ ] Per-label four-bucket diff, `regressions.csv`, semver bump proposal
- [ ] Stage-1/2 accuracy over `direct` fields only; Stage 4 over `propagated`
      only (§3.1)
- [ ] Named regression cases E-001, E-002, E-052, E-066, E-074 (§7.4)
- [ ] Confidence calibration bucketing; stability-vs-accuracy check (§8.4)
- [ ] Blind adjudication packet export + round-trip guards (§7.7)

### Conventions
- **YAML parsing:** PyYAML (`safe_load` only) unless stdlib-only is required
- **Tests:** pytest; the replay diff and the five named entity cases are
  regression tests, not manual checks
- **Determinism:** no reliance on dict ordering, input row order, or wall
  clock anywhere in the pipeline path

## 11. Deferred / Future Work
Process decisions deliberately left out of the architecture for now:
- **Stage 3 positive-pair floor** — the minimum number of same-device pairs
  below which pairwise clustering metrics stay reported-but-non-gating
  (§7.4). Currently ~5; a defensible starting floor is n≥30.
- **Pairwise sampling strategy for Stage 3** — same-device/different-device
  judgments are O(n²); needs stratified sampling weighted toward pipeline-
  ambiguous pairs rather than random pairs.
- **LLM proposal hygiene** — rejection tracking to filter repeatedly-rejected
  patterns, and a decision on whether the LLM sees the frozen label set when
  generating proposals (if it does, proposals risk being tuned to the very
  set used to evaluate them).
- **Stopping criteria** — per-stage thresholds defining when a stage is "done
  for now" versus worth further investment.
- **Who adjudicates** — staffing and escalation path for `status: disputed`
  cases; the mechanism is specified (§7.2.2, §7.7) but the role is not
  assigned.

# Findings from the initial build

Produced while implementing `device_fingerprinting_pipeline_design.md`. These
are things the build surfaced that need a human decision — they are **not**
open defects in the code. Everything here was verified against the data, and
each was independently confirmed by a reviewer who did not find it.

---

## 1. Three errors in `labels/labels-initial.csv`

Each contradicts the payload it claims to be inferred from, and all three are
self-rated `high` certainty.

| observation | field | payload states | label says |
|---|---|---|---|
| OBS-039 | `model` | `Model=Q6135-LE` | `Q6315-LE` — 61↔63 transposed |
| OBS-040 | `vendor` | `Manufacturer=VIVOTEK Inc.` (OUI `00:02:D1` → Vivotek; its own `model` cell `IB9389-EHT` is a Vivotek model) | `Bosch Security Systems` — apparently copied from the adjacent OBS-041 row |
| OBS-041 | `firmware` | `firmware 8.12.0022` | `8.12.2200` — 0022↔2200 transposed |

All three are stamped `label_basis: payload_inference`, so a label contradicting
its own payload is decisive rather than arguable.

**Effect if left as-is.** All three are scored against the pipeline. Corrected,
vendor recall goes 68/74 → 69/74, model 46/71 → 47/71, firmware 46/57 → 47/57.

**They have not been changed.** §7.2.2 makes adjudication a human process and
invariant #6 forbids pipeline output reaching label creation. Editing ground
truth because the pipeline disagrees is the contamination §7.7 describes.

**Nothing in the built system would catch these automatically.**
`validate_against_vocabulary` checks membership, and all three bad values are
valid vocabulary terms. Worse, the adjudication packet selects on
`labeler_certainty ∈ {medium, low}`, so all three are excluded by construction.
On this data self-rated certainty is *anti*-correlated with accuracy: the
`medium`/`low` flags were sound judgements and the errors are all `high`.

A `label_basis: payload_inference` consistency check — does the labelled value
appear in, or normalise from, the payload it claims to come from? — would have
caught all three. That is new scope, not a defect.

---

## 2. Four internal inconsistencies in the design document

**§2.3 vs §2.6 — cross-observation corroboration is priced in nowhere.**
§2.3 justifies not aggregating at field resolution: *"Corroboration is already
priced in via `independence_bonus`."* But claims are keyed by `obs_id`, so the
bonus only ever sees witnesses *within* one observation. E-001's three
observations all independently assert `Axis Communications` and that agreement
raises confidence nowhere. §2.6's table says *"Agreement must raise confidence"*
without the scoping caveat. The code implements §2.3 faithfully; the two
sections disagree with each other.

Measured consequence: `corroboration_rate` is 0.0 for `model`, `firmware`,
`device_type`, `serial` and `hostname_token`. Only `vendor` is ever corroborated
(0.449), because it is the one key with two producers on a single observation —
a payload regex *and* the OUI map.

**Resolved (rules 0.1.1, 2026-08-05).** §2.3/§2.5/§2.6 were amended to price
corroboration at exactly two scopes, each once: within an observation at claim
time (unchanged), and across an entity's members at field resolution — a
re-score of the winning value over the **union** of distinct witness groups,
under a third coefficient set (`entity_corroboration`, seeded identical to
`field_claims` so singletons are byte-identical). The union keeps
re-observation honest: E-001's `device_type` is witnessed by `port_signature`
on all three members and pools to k=1, no bonus. Winner *selection* is
untouched — only the winner's confidence moves. Measured effect: zero value
changes, zero four-bucket movement, 17 field-confidence changes confined to
the 11 observations in the 5 multi-member entities. The new
`entity_corroboration_rate` observes what the claim-scoped rate structurally
cannot: model 3/49, firmware 2/46, vendor 41/63, device_type 0.0. The
claim-scoped `corroboration_rate` still reads 0.0 for model/firmware, which
is now documented as correct-by-construction rather than a gap.

**§3.1 — the OBS-073 example is false about this data.** It says OBS-073
*"contributes almost nothing — whatever `vendor` it shows was carried in from
OBS-061."* OBS-073's mDNS payload carries `vendor=HIKVISION` explicitly, so it
witnesses vendor directly. The empty-MAC premise is true; the inheritance
conclusion is not. The principle — provenance must be per-field — is right and
load-bearing. A genuine direct/propagated pair is OBS-001/OBS-002 on `model`.

**§7.4 — "roughly 5 positive pairs" is 7.** Five multi-observation entities and
eleven observations are both right, but E-001 has three members and contributes
C(3,2)=3 pairs. The same figure is repeated in `rules/scoring.yaml`. The
conclusion is unaffected: 7 is still far below where a pairwise rate means
anything, so Stage 3 stays reported-but-non-gating.

**§7.7 vs §7.2.3 — what defines the hard stratum.** §7.7 says the export tool
reads confidence to identify it. §7.2.3 opens by noting earlier drafts conflated
two unrelated things, disentangles them, and routes blind adjudication to
`labeler_certainty` because it *"requires no pipeline run to compute... and the
stratum doesn't thrash when scoring is recalibrated."* The implementation
follows §7.2.3. Following §7.7 selected 68 of 74 observations — a 92% "stratum"
that would be redrawn by every coefficient change.

---

## 3. A concrete calibration target

The pipeline is systematically **under-confident**:

| predicted | observed | n |
|---|---|---|
| 0.25 | 0.92 | 59 |
| 0.40 | 1.00 | 22 |
| 0.55 | 0.94 | 17 |
| 0.85 | 0.92 | 61 |
| 1.00 | 0.96 | 28 |

Only the top bucket is slightly over-confident. `device_type` scores 0.25 on
every observation — the `port_signature` base weight — and is right about 90% of
the time, so **`port_signature`'s `base_weight` in `scoring.yaml` is the
concrete §7.3 Stage-2 target.** Because entity confidence is a harmonic mean,
that single weight drags every entity's rollup down.

§8.4's stability check is answerable once confidence is read per field:
`high_conf_low_stab` 0.700 (n=10) against `high_conf_high_stab` 0.933 (n=119) —
the direction §8.4 predicts.

---

## 4. Deliberate gaps, left in place

- **Four vendor aliases are missing on purpose** — `LTS Security`, `Amcrest`,
  `Wisenet`, `VVTK` — so the vocabulary-rejection machinery runs on real data
  instead of being dead code (§6.3). They appear in the reject queue by design.
- **Stage-1 extraction gaps are unfixed on purpose.** `http_server_vendor`
  over-captures server software (`microsoft-httpapi` on OBS-033);
  `snmp_vendor_leading` takes only the first token (`Hanwha Techwin` →
  `hanwha`); OBS-034 yields no vendor because its banner lacks a terminator.
  Hand-tuning rules against known labels is what §7.4 warns makes the gates
  measure self-consistency rather than correctness.
- **Scoring coefficients are seeded defaults, not calibrated** (§7.4).
- **Stage 3 and Stage 4 are reported but non-gating** — 7 positive pairs, and
  n=3 for propagated accuracy after blank-label exclusion.
- **`cross_basis_conflict_rate` is legitimately 0.0.** The tie-break rule is
  implemented and unit-tested against a raised threshold, and unexercised by
  these 74 rows.
- **§7.6's cumulative regression tracking is not built.** `REPORT.md` says so
  in the section itself rather than implying persistence it does not provide.
- ~~**The adjudication round trip is open at the far end**~~ — **closed
  (labels 0.1.0 → versioned, 2026-08-05).** `apply_adjudicated` writes
  accepted labels back under §7.2.2 precedence, and the label set now
  versions forward like runs and rules: archive, append-only journal,
  measured VERSION bump, and `labels_version_verified` in the eval manifest.
  Two hazards surfaced while closing it, both now guarded: importing into a
  labels directory with no `VERSION` crashed, and re-running
  `python3 label_tools.py` after an adjudication would have silently
  reverted human ground truth to the `payload_inference` the wide file
  carries — producing a file that still hashes and still loads, so nothing
  downstream could have caught it.

  This also gives the three label errors above a sanctioned correction path.
  They are `high` certainty, so blind adjudication will never select them;
  a human can now write a correction file, put it through the same
  precedence gate, and have the journal record it as a human decision
  rather than a silent edit.

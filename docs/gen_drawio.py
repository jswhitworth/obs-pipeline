#!/usr/bin/env python3
"""Generate obs-pipeline.drawio -- native diagrams.net XML."""
from xml.sax.saxutils import escape

CELLS = []


def esc(s):
    """XML-escape for an attribute value.

    Two things saxutils.escape does not do on its own and both break the file:
    quotes must be escaped or they terminate the attribute, and a raw newline
    in an attribute is normalized to a space by any conforming parser -- so
    line breaks have to be HTML <br> (drawio values are HTML, html=1).

    Note the asymmetry: a literal '<' meant for DISPLAY is written '&lt;' in
    the source string here, so escaping yields '&amp;lt;' -- which the XML
    parser turns back into '&lt;' and the HTML renderer draws as '<'. Writing
    a bare '<' instead would produce a value the renderer reads as a tag and
    silently hides.
    """
    return escape(s.replace("\n", "<br>"), {'"': "&quot;"})


def node(nid, label, x, y, w, h, style):
    CELLS.append(
        f'        <mxCell id="{nid}" value="{esc(label)}" style="{esc(style)}" '
        f'vertex="1" parent="1">\n'
        f'          <mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry" />\n'
        f'        </mxCell>')


def edge(eid, src, tgt, style, label=""):
    CELLS.append(
        f'        <mxCell id="{eid}" value="{esc(label)}" style="{esc(style)}" '
        f'edge="1" parent="1" source="{src}" target="{tgt}">\n'
        f'          <mxGeometry relative="1" as="geometry" />\n'
        f'        </mxCell>')


# ---------- palette ----------
ZONE = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#F7F7F7;strokeColor=#9E9E9E;"
        "dashed=1;verticalAlign=top;align=left;spacingLeft=12;spacingTop=4;"
        "fontStyle=1;fontSize=13;fontColor=#404040;")
ZONE_PIPE = ZONE.replace("#F7F7F7", "#EDF4FC").replace("#9E9E9E", "#6C8EBF")
ZONE_LABEL = ZONE.replace("#F7F7F7", "#F5EFF8").replace("#9E9E9E", "#9673A6")
ZONE_OUT = ZONE.replace("#F7F7F7", "#EEF7EC").replace("#9E9E9E", "#82B366")

RULE = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#FFF2CC;strokeColor=#D6B656;"
        "fontSize=10;align=left;spacingLeft=8;")
STAGE = ("rounded=1;whiteSpace=wrap;html=1;fillColor=#DAE8FC;strokeColor=#6C8EBF;"
         "fontSize=10;align=left;spacingLeft=8;verticalAlign=top;spacingTop=4;")
SHARED = ("rounded=1;whiteSpace=wrap;html=1;fillColor=#FFE6CC;strokeColor=#D79B00;"
          "fontSize=10;align=left;spacingLeft=8;verticalAlign=top;spacingTop=4;")
OUT = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#D5E8D4;strokeColor=#82B366;"
       "fontSize=10;align=left;spacingLeft=8;")
LBL = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#E1D5E7;strokeColor=#9673A6;"
       "fontSize=10;align=left;spacingLeft=8;")
NOTE = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#F8CECC;strokeColor=#B85450;"
        "fontSize=10;align=left;spacingLeft=8;verticalAlign=top;spacingTop=6;")
INPUT = ("shape=parallelogram;perimeter=parallelogramPerimeter;whiteSpace=wrap;html=1;"
         "fixedSize=1;fillColor=#FFF2CC;strokeColor=#D6B656;fontSize=11;")
TITLE = ("text;html=1;align=left;verticalAlign=middle;fontSize=20;fontStyle=1;"
         "fontColor=#1A1A1A;")
SUB = "text;html=1;align=left;verticalAlign=middle;fontSize=11;fontColor=#5A5A5A;"
TRACER = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#D4E1F5;strokeColor=#6C8EBF;"
          "horizontal=0;fontSize=10;fontStyle=1;")

E = "edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeColor=#4D4D4D;"
E_DASH = E + "dashed=1;strokeColor=#D79B00;"
E_RED = ("edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;dashed=1;strokeColor=#B85450;"
         "endArrow=none;fontColor=#B85450;fontSize=10;fontStyle=1;")

# ================= PAGE 1 =================
node("t1", "Device Fingerprinting Pipeline — stage flow", 40, 18, 900, 30, TITLE)
node("t1s", "run.py:run_pipeline · one module per rule category · every stage emits trace steps",
     40, 48, 900, 20, SUB)

node("z1", "rules/  —  the tuning surface", 40, 90, 260, 380, ZONE)
node("z2", "run.py  —  the pipeline   (invariant #5: no code path here reads labels/)",
     330, 90, 570, 990, ZONE_PIPE)
node("z3", "runs/&lt;run_id&gt;/  —  the run bundle", 930, 90, 280, 390, ZONE_OUT)
node("z4", "labels/  —  ground truth", 40, 600, 260, 270, ZONE_LABEL)

rules = [
    ("extraction.yaml", "→ extract.py"),
    ("normalization.yaml", "→ normalize.py"),
    ("claims.yaml", "→ claims.py"),
    ("scoring.yaml", "→ scoring.py"),
    ("entity_resolution.yaml", "→ entity.py"),
    ("field_resolution.yaml", "→ fields.py"),
    ("canonical_vocab.csv", "→ vocab.py"),
    ("VERSION", "hash polices it"),
]
y = 128
for i, (f, m) in enumerate(rules):
    node(f"r{i}", f"{f}\n{m}", 58, y, 224, 30, RULE)
    y += 34
node("rhash", "all 8 canonicalized + hashed → rules_rollup", 58, y + 4, 224, 28,
     "rounded=0;whiteSpace=wrap;html=1;fillColor=#FFF2CC;strokeColor=#D6B656;"
     "fontSize=9;fontStyle=2;align=left;spacingLeft=8;")

node("obs", "obs-data/observations.csv\n74 raw observations", 40, 500, 260, 60, INPUT)

node("tracer",
     "trace.py · Tracer  —  Traced[T] cannot be constructed except through "
     "Tracer.step(), so a code path that produces a value necessarily emitted one. "
     "step_id is content-addressed.",
     345, 130, 34, 920, TRACER)

stages = [
    ("s1", "1.  load_rules", "loader.py",
     "Parse 6 YAML + vocab · cross-file validation raises CrossFileError at init, "
     "not three stages later · per-file + rollup hash · stale-bump check"),
    ("s2", "2.  load_observations", "extract.py + normalize.py",
     "regex / OUI map / port signatures → MAC, hostname, vendor, device_type "
     "canonicalization · absence emits no_extraction"),
    ("s3", "3.  build_claims", "claims.py",
     "keyed (obs_id, field | link_basis, value) · source is metadata ON the claim, "
     "never in the key · NOT vocab-constrained · absence emits no_identity_claim"),
    ("s4", "4.  resolve_entities", "entity.py",
     "deterministic union-find · merge order pinned by entity_resolution.yaml, not by "
     "input order · basis-precedence tie-break · content-addressed entity_id · "
     "refusals emit merge_refused"),
    ("s5", "5.  resolve_fields", "fields.py",
     "entity level · highest claim_weight wins · winner re-scored over POOLED "
     "witness groups across members · firmware conflict → undecidable · "
     "out-of-vocab emits vocab_reject"),
    ("s6", "6.  observation_fields", "fields.py",
     "per observation · direct vs propagated provenance · multiplicative decay: "
     "source_conf × link_weight × decay_base^hop · Unknown never propagates"),
    ("s7", "7.  entity_confidence + stability", "confidence.py",
     "harmonic mean across fields — weakest field dominates · stability is a "
     "SEPARATE measure: margin, witness_dependence (a count), live_conflict"),
    ("s8", "8.  write_bundle", "bundle.py",
     "manifest + 4 CSVs + trace.jsonl · rows stay narrow: each carries run_id and "
     "derivation_step rather than repeating rule versions"),
    ("s9", "9.  emit_metrics", "metrics.py",
     "label-free metrics only · long format, one row per measurement, n mandatory · "
     "appended to runs/history.jsonl"),
    ("s10", "10.  write_report", "report.py",
     "REPORT.md is a derived render — regenerable, never hand-edited, nothing may "
     "depend on parsing it"),
]
y = 130
for nid, head, mod, body in stages:
    node(nid, f"{head}\n{mod}\n{body}", 392, y, 340, 78, STAGE)
    y += 92

node("score", "scoring.py · score()\nTHE single scoring function.\n"
     "claim_weight = clamp(max_base + independence_bonus − conflict_penalty, 0, 1)\n"
     "3 coefficient sets, one formula.",
     744, 330, 146, 150, SHARED)

for a, b in [("s1", "s2"), ("s2", "s3"), ("s3", "s4"), ("s4", "s5"),
             ("s5", "s6"), ("s6", "s7"), ("s7", "s8"), ("s8", "s9"), ("s9", "s10")]:
    edge(f"e_{a}_{b}", a, b, E)

edge("e_r_s1", "z1", "s1", E)
edge("e_o_s2", "obs", "s2", E)
edge("e_s3_sc", "s3", "score", E_DASH, "field_claims")
edge("e_s4_sc", "s4", "score", E_DASH, "identity_claims")
edge("e_s5_sc", "s5", "score", E_DASH, "entity_corroboration")
edge("e_s8_z3", "s8", "z3", E)
edge("e_lbl", "z4", "z2", E_RED, "invariant #5 — FORBIDDEN")

bundle = [
    ("manifest.json", "rules_rollup, rules_version, engine_commit,\ninput_hash, run_id, version_verified"),
    ("trace.jsonl", "the derivation DAG"),
    ("claims.csv", ""),
    ("membership.csv", "obs → entity, link_basis, link_weight"),
    ("entities.csv", "resolved values + per-field confidence"),
    ("resolutions.csv", "pure join view — makes no new decisions,\nso emits no trace steps"),
    ("metrics.jsonl", "label-free metrics"),
    ("REPORT.md", "derived render"),
]
y = 128
for i, (f, d) in enumerate(bundle):
    h = 42 if d and "\n" in d else (34 if d else 26)
    node(f"b{i}", f + ("\n" + d if d else ""), 948, y, 244, h, OUT)
    y += h + 6

labels = [
    "labels-initial.csv  (wide)",
    "labels.csv  (long, authoritative)",
    "VERSION",
    "archive/&lt;version&gt;-labels.csv",
    "journal.jsonl",
    "last_label_state.json",
]
y = 638
for i, f in enumerate(labels):
    node(f"l{i}", f, 58, y, 224, 30, LBL)
    y += 34

notes = [
    ("n1", "invariant #4 — rule-state traceability",
     "Every output binds to the exact rule state via run_id → manifest.json. "
     "engine_commit carries a -dirty suffix when the tree was not clean, because a "
     "clean sha reported while uncommitted code ran is worse than no value at all."),
    ("n2", "invariant #1 — DAG discipline",
     "Entity resolution reads identity claims ONLY, never resolved field values. "
     "There is no field → cluster feedback loop, which is what keeps the pipeline "
     "a strict DAG and replay meaningful."),
    ("n3", "invariant #3 — scoring parity",
     "One scoring function, used by claim construction, entity resolution and the "
     "cross-observation re-score. Coefficients may differ per claim type; the formula "
     "shape may not. Do not write a second implementation."),
    ("n4", "invariant #7 — no output without a derivation",
     "Enforced mechanically by replay.py, not by convention. Absence needs a step too: "
     "no_extraction, no_identity_claim, vocab_reject and merge_refused are "
     "first-class steps, not silences."),
    ("n5", "determinism",
     "No reliance on dict ordering, input row order, or wall clock anywhere in the "
     "pipeline path. run_id carries the only timestamp and no decision reads it. "
     "Two runs over identical input produce byte-identical trace.jsonl."),
    ("n6", "three combining operations, never conflated",
     "same value witnessed twice → max + saturating bonus (scoring.py) · "
     "a propagation chain → multiplicative (fields.py) · "
     "different fields into one record → harmonic mean (confidence.py)"),
]
y = 128
for nid, head, body in notes:
    node(nid, f"{head}\n\n{body}", 1240, y, 320, 128, NOTE)
    y += 142

PAGE1 = "\n".join(CELLS)

# ================= PAGE 2 =================
CELLS = []
node("t2", "The five surfaces — what each is for", 40, 18, 900, 30, TITLE)
node("t2s", "Each is a standalone entry point. None is a flag on another. Run everything from the repo root.",
     40, 48, 1000, 20, SUB)

hdr = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#333333;strokeColor=none;"
       "fontColor=#FFFFFF;fontSize=11;fontStyle=1;align=left;spacingLeft=10;"
       "verticalAlign=top;spacingTop=8;")
cell = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#B3B3B3;"
        "fontSize=10;align=left;spacingLeft=8;verticalAlign=top;spacingTop=6;")
cell_g = cell.replace("#FFFFFF", "#EEF7EC").replace("#B3B3B3", "#82B366")
cell_y = cell.replace("#FFFFFF", "#FFF9E6").replace("#B3B3B3", "#D6B656")
cell_p = cell.replace("#FFFFFF", "#F7F1FA").replace("#B3B3B3", "#9673A6")

node("h0", "SURFACE", 40, 96, 300, 26,
     "text;html=1;fontSize=10;fontStyle=1;fontColor=#666666;align=left;spacingLeft=10;")
node("h1", "READS", 350, 96, 250, 26,
     "text;html=1;fontSize=10;fontStyle=1;fontColor=#666666;align=left;spacingLeft=8;")
node("h2", "DOES", 610, 96, 420, 26,
     "text;html=1;fontSize=10;fontStyle=1;fontColor=#666666;align=left;spacingLeft=8;")
node("h3", "WRITES", 1040, 96, 250, 26,
     "text;html=1;fontSize=10;fontStyle=1;fontColor=#666666;align=left;spacingLeft=8;")
node("h4", "USE IT WHEN", 1300, 96, 260, 26,
     "text;html=1;fontSize=10;fontStyle=1;fontColor=#666666;align=left;spacingLeft=8;")

bands = [
    ("run", "run.py\n\npython3 run.py\npython3 run.py &lt;obs.csv&gt; &lt;rules_dir&gt; &lt;out_root&gt;",
     "rules/\nobs-data/observations.csv\n\nNEVER labels/",
     "Runs the 10 stages on page 1 and writes a self-describing bundle. "
     "Two runs over unchanged input and rules are byte-identical — re-running "
     "to see if something changed tells you nothing.",
     "runs/&lt;run_id&gt;/\nruns/history.jsonl\nruns/last_rules_state.json",
     "You changed a rule, or you want fresh output. This is the golden path; "
     "everything else consumes what it writes.", cell_g),
    ("replay", "replay.py\n\npython3 replay.py runs/&lt;run_id&gt;",
     "runs/&lt;run_id&gt;/trace.jsonl\n\nNEVER the engine — an AST test "
     "forbids importing scoring / entity / fields",
     "Reconstructs claims.csv, membership.csv and entities.csv from the trace ALONE, "
     "then diffs against what actually shipped. If it could reach the engine it might "
     "recompute a value instead of reading it, and the gate would pass on an "
     "incomplete trace.",
     "stdout: REPLAY OK\nexit 1 + the diff on any hole",
     "After ANY change touching trace emission. It is cheap, and it is the only thing "
     "that catches a silently broken audit trail.", cell_g),
    ("eval", "eval.py\n\npython3 eval.py\npython3 eval.py &lt;obs&gt; &lt;rules&gt; &lt;labels&gt; &lt;out&gt;",
     "rules/ AND labels/\n\nThe only component that reads both sides. "
     "Invokes run_pipeline as a black box.",
     "Scores the run against ground truth. Four-bucket diff (fixed / broken / "
     "stable_correct / stable_incorrect) per label, because aggregate metrics hide "
     "cases a rule change silently broke. Raises LabelsMovedError BEFORE writing "
     "anything if the label set moved — a moved label must never read as a "
     "rule regression.",
     "evals/&lt;eval_id&gt;/\n  outcomes.json\n  metrics.jsonl\n  regressions.csv\n  eval_manifest.json\n"
     "+ re-renders REPORT.md",
     "You want accuracy, or you want to know what a rule change actually moved. "
     "Regressions are LOGGED, not blocked.", cell_y),
    ("labeltools", "label_tools.py\n\npython3 label_tools.py",
     "labels/labels-initial.csv\nobs-data/observations.csv\nclaims.yaml#fields "
     "(passed in at the entry point)",
     "Converts the wide rendering into the long-format labels.csv everything else "
     "reads, and runs the transitivity check. Refuses to regenerate over adjudicated "
     "labels, and refuses a column that is neither wide-file metadata nor a declared "
     "field — a silently ignored column drops that field's whole ground truth.",
     "labels/labels.csv\nlabels/VERSION\nlabels/archive/\nlabels/journal.jsonl\n"
     "labels/last_label_state.json",
     "You edited labels-initial.csv. labels.csv is generated, never maintained "
     "by hand.", cell_p),
    ("adj", "adjudicate.py\n\npython3 adjudicate.py runs/&lt;run_id&gt;\n"
     "python3 adjudicate.py runs/&lt;run_id&gt; returned.csv",
     "resolutions.csv (to EXCLUDE what is settled)\nlabels/labels.csv\n"
     "claims.yaml#fields",
     "EXPORT: an evidence-only packet for the medium/low certainty stratum — no "
     "resolved values, no confidence, so the adjudicator is asked \"what is this?\" "
     "rather than \"is this right?\". IMPORT: two independent gates — rejected "
     "(inadmissible) and refused (does not supersede, per basis precedence).",
     "adjudication/&lt;run_id&gt;/packet.csv\nthen, on import, all of labels/ above",
     "You need better ground truth where the labeller was unsure. An observation "
     "leaves the queue only when EVERY row it has is settled.", cell_p),
]

y = 126
for nid, surface, reads, does, writes, when, tint in bands:
    node(f"{nid}_s", surface, 40, y, 300, 168, hdr)
    node(f"{nid}_r", reads, 350, y, 250, 168, tint)
    node(f"{nid}_d", does, 610, y, 420, 168, cell)
    node(f"{nid}_w", writes, 1040, y, 250, 168, tint)
    node(f"{nid}_u", when, 1300, y, 260, 168, cell)
    y += 182

node("guard",
     "The two guard rails, and why they point in opposite directions\n\n"
     "invariant #5 — labels never enter the runtime path.  run.py and everything it "
     "imports have no code path that reads labels/. This is why eval.py, label_tools.py and "
     "adjudicate.py live OUTSIDE obs_pipeline/: structural, not conventional. It is what stops "
     "the pipeline from doing better on labelled data than it would on new data.\n\n"
     "invariant #6 — pipeline output never enters hard-stratum label creation.  The mirror. "
     "In adjudicate.py, SELECTION may read pipeline output; PRESENTATION must not. A human handed "
     "a plausible answer and asked \"is this right?\" agrees more often than one asked \"what is "
     "this?\", and unblinded labelling drifts ground truth toward whatever the pipeline already "
     "believes — worst precisely where it is confidently wrong.",
     40, y + 10, 1520, 168, NOTE)

node("order",
     "Everyday order:   python3 -m pytest -q   →   python3 eval.py   →   "
     "python3 replay.py runs/&lt;run_id&gt;   →   read regressions.csv   →   bump rules/VERSION",
     40, y + 192, 1520, 34,
     "rounded=0;whiteSpace=wrap;html=1;fillColor=#DAE8FC;strokeColor=#6C8EBF;"
     "fontSize=11;fontStyle=1;align=center;")

PAGE2 = "\n".join(CELLS)


def page(name, pid, w, h, body):
    return (f'  <diagram name="{escape(name)}" id="{pid}">\n'
            f'    <mxGraphModel dx="1400" dy="900" grid="1" gridSize="10" guides="1" '
            f'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
            f'pageWidth="{w}" pageHeight="{h}" math="0" shadow="0">\n'
            f'      <root>\n'
            f'        <mxCell id="0" />\n'
            f'        <mxCell id="1" parent="0" />\n'
            f'{body}\n'
            f'      </root>\n'
            f'    </mxGraphModel>\n'
            f'  </diagram>')


xml = ('<mxfile host="app.diagrams.net" type="device">\n'
       + page("Pipeline stage flow", "pipeline-flow", 1600, 1120, PAGE1) + "\n"
       + page("Surfaces", "surfaces", 1620, 1300, PAGE2) + "\n"
       + "</mxfile>\n")

with open("obs-pipeline.drawio", "w", encoding="utf-8") as fh:
    fh.write(xml)
print("wrote obs-pipeline.drawio", len(xml), "bytes")

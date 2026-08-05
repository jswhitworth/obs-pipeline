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


def edge(eid, src, tgt, style, label="", pts=None):
    geo = '          <mxGeometry relative="1" as="geometry" />'
    if pts:
        way = "".join(f'\n              <mxPoint x="{px}" y="{py}" as="point" />'
                      for px, py in pts)
        geo = ('          <mxGeometry relative="1" as="geometry">\n'
               f'            <Array as="points">{way}\n            </Array>\n'
               '          </mxGeometry>')
    CELLS.append(
        f'        <mxCell id="{eid}" value="{esc(label)}" style="{esc(style)}" '
        f'edge="1" parent="1" source="{src}" target="{tgt}">\n'
        f'{geo}\n'
        f'        </mxCell>')


# ---------- palette ----------
# Every shape carries an explicit near-black fontColor. Relying on the theme
# default is what made these unreadable: drawio resolves it against the
# EDITOR theme, so pastel fills that look fine in light mode end up with
# light text on light fill for anyone in dark mode.
INK = "fontColor=#101010;"

ZONE = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#F2F2F2;strokeColor=#7A7A7A;"
        "dashed=1;verticalAlign=top;align=left;spacingLeft=12;spacingTop=6;"
        "fontStyle=1;fontSize=14;fontColor=#101010;")
ZONE_PIPE = ZONE.replace("#F2F2F2", "#E4EEFA").replace("#7A7A7A", "#3B6BA5")
ZONE_LABEL = ZONE.replace("#F2F2F2", "#F1E9F6").replace("#7A7A7A", "#7A5091")
ZONE_OUT = ZONE.replace("#F2F2F2", "#E6F3E3").replace("#7A7A7A", "#5D9152")

BODY = "fontSize=11;align=left;spacingLeft=8;verticalAlign=top;spacingTop=6;" + INK
RULE = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#FFF0B3;strokeColor=#B39A2E;"
        "fontSize=11;align=left;spacingLeft=8;verticalAlign=middle;" + INK)
STAGE = ("rounded=1;whiteSpace=wrap;html=1;fillColor=#CFE2FA;strokeColor=#3B6BA5;"
         + BODY)
SHARED = ("rounded=1;whiteSpace=wrap;html=1;fillColor=#FFDDB0;strokeColor=#B87A00;"
          + BODY)
OUT = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#CDE8C9;strokeColor=#5D9152;"
       + BODY)
LBL = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#E4D3EE;strokeColor=#7A5091;"
       "fontSize=11;align=left;spacingLeft=8;verticalAlign=middle;" + INK)
NOTE = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#FBD9D6;strokeColor=#A33A30;"
        + BODY)
INPUT = ("shape=parallelogram;perimeter=parallelogramPerimeter;whiteSpace=wrap;html=1;"
         "fixedSize=1;fillColor=#FFF0B3;strokeColor=#B39A2E;fontSize=12;" + INK)
TITLE = "text;html=1;align=left;verticalAlign=middle;fontSize=21;fontStyle=1;" + INK
SUB = ("text;html=1;align=left;verticalAlign=middle;fontSize=12;fontColor=#2E2E2E;")
TRACER = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#B9D2F2;strokeColor=#3B6BA5;"
          "horizontal=0;fontSize=11;fontStyle=1;" + INK)

# Edges: explicit exit/entry anchors everywhere. Left to route themselves they
# leave from whichever side drawio guesses, which is what made the chain
# zig-zag and the fan-in cross.
def EV(exit_x, exit_y, entry_x, entry_y, extra=""):
    """Orthogonal edge with pinned anchors."""
    return (f"edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;jettySize=auto;"
            f"orthogonalLoop=1;strokeWidth=2;strokeColor=#2E2E2E;"
            f"exitX={exit_x};exitY={exit_y};exitDx=0;exitDy=0;"
            f"entryX={entry_x};entryY={entry_y};entryDx=0;entryDy=0;{extra}")


def SV(exit_x, exit_y, entry_x, entry_y, extra=""):
    """Straight edge -- used for the fan-in, where orthogonal routing in a
    narrow gutter produces overlapping right-angle stubs."""
    return (f"edgeStyle=none;rounded=0;html=1;strokeWidth=2;"
            f"exitX={exit_x};exitY={exit_y};exitDx=0;exitDy=0;"
            f"entryX={entry_x};entryY={entry_y};entryDx=0;entryDy=0;{extra}")


DOWN = EV(0.5, 1, 0.5, 0)
RIGHT = EV(1, 0.5, 0, 0.5)

# ================= PAGE 1 =================
node("t1", "Device Fingerprinting Pipeline — stage flow", 40, 16, 900, 32, TITLE)
node("t1s", "run.py:run_pipeline · one module per rule category · every stage emits trace steps",
     40, 50, 1000, 22, SUB)

node("z1", "rules/  —  the tuning surface", 40, 92, 264, 372, ZONE)
node("z2", "run.py  —  the pipeline   (invariant #5: no code path here reads labels/)",
     332, 92, 578, 1114, ZONE_PIPE)
node("z3", "runs/&lt;run_id&gt;/  —  the run bundle", 946, 92, 268, 472, ZONE_OUT)
node("z4", "labels/  —  ground truth", 40, 606, 264, 268, ZONE_LABEL)

rules = [("extraction.yaml", "extract.py"), ("normalization.yaml", "normalize.py"),
         ("claims.yaml", "claims.py"), ("scoring.yaml", "scoring.py"),
         ("entity_resolution.yaml", "entity.py"), ("field_resolution.yaml", "fields.py"),
         ("canonical_vocab.csv", "vocab.py"), ("VERSION", "the hash polices it")]
y = 128
for i, (f, m) in enumerate(rules):
    node(f"r{i}", f"<b>{f}</b>\n→ {m}", 58, y, 228, 34, RULE)
    y += 38
node("rhash", "<b>all 8 canonicalized + hashed → rules_rollup</b>", 58, y + 2, 228, 32,
     "rounded=0;whiteSpace=wrap;html=1;fillColor=#F5E08A;strokeColor=#B39A2E;"
     "fontSize=10;align=center;verticalAlign=middle;" + INK)

node("obs", "<b>obs-data/observations.csv</b>\n74 raw observations", 40, 500, 264, 66, INPUT)

node("tracer",
     "trace.py · Tracer  —  Traced[T] cannot be constructed except through Tracer.step(), "
     "so a code path that produces a value necessarily emitted one.  step_id is "
     "content-addressed, never a counter.",
     346, 132, 36, 1058, TRACER)

stages = [
    ("s1", "1.  load_rules", "loader.py",
     "Parse 6 YAML + vocab · cross-file validation raises CrossFileError at init, not "
     "three stages later · per-file + rollup hash · stale-bump check"),
    ("s2", "2.  load_observations", "extract.py + normalize.py",
     "regex / OUI map / port signatures → MAC, hostname, vendor, device_type "
     "canonicalization · absence emits no_extraction"),
    ("s3", "3.  build_claims", "claims.py",
     "keyed (obs_id, field | link_basis, value) · source is metadata ON the claim, never "
     "in the key · NOT vocab-constrained · absence emits no_identity_claim"),
    ("s4", "4.  resolve_entities", "entity.py",
     "deterministic union-find · merge order pinned by entity_resolution.yaml, not by "
     "input order · basis-precedence tie-break · content-addressed entity_id · refusals "
     "emit merge_refused"),
    ("s5", "5.  resolve_fields", "fields.py",
     "entity level · highest claim_weight wins · winner re-scored over POOLED witness "
     "groups across members · firmware conflict → undecidable · out-of-vocab emits "
     "vocab_reject"),
    ("s6", "6.  observation_fields", "fields.py",
     "per observation · direct vs propagated provenance · multiplicative decay: "
     "source_conf × link_weight × decay_base^hop · Unknown never propagates"),
    ("s7", "7.  entity_confidence + stability", "confidence.py",
     "harmonic mean across fields — the weakest field dominates · stability is a SEPARATE "
     "measure: margin, witness_dependence (a count), live_conflict"),
    ("s8", "8.  write_bundle", "bundle.py",
     "manifest + 4 CSVs + trace.jsonl · rows stay narrow: each carries run_id and "
     "derivation_step rather than repeating rule versions"),
    ("s9", "9.  emit_metrics", "metrics.py",
     "label-free metrics only · long format, one row per measurement, n mandatory · "
     "appended to runs/history.jsonl"),
    ("s10", "10.  write_report", "report.py",
     "REPORT.md is a derived render — regenerable, never hand-edited, and nothing may "
     "depend on parsing it"),
]
y = 132
for nid, head, mod, body in stages:
    node(nid, f"<b>{head}</b>   <i>{mod}</i>\n{body}", 398, y, 322, 90, STAGE)
    y += 106

node("score",
     "<b>scoring.py · score()</b>\nTHE single scoring function.\n\n"
     "clamp(max_base + independence_bonus − conflict_penalty, 0, 1)\n\n"
     "Three coefficient sets, one formula shape.",
     748, 356, 150, 210, SHARED)

for a, b in [("s1", "s2"), ("s2", "s3"), ("s3", "s4"), ("s4", "s5"), ("s5", "s6"),
             ("s6", "s7"), ("s7", "s8"), ("s8", "s9"), ("s9", "s10")]:
    edge(f"e_{a}_{b}", a, b, DOWN)

edge("e_r_s1", "rhash", "s1", EV(1, 0.5, 0, 0.5))
edge("e_o_s2", "obs", "s2", EV(1, 0.5, 0, 0.5))

FAN = "strokeColor=#B87A00;dashed=1;fontColor=#7A5200;fontSize=10;fontStyle=1;"
edge("e_s3_sc", "s3", "score", SV(1, 0.5, 0, 0.12, FAN), "field_claims")
edge("e_s4_sc", "s4", "score", SV(1, 0.5, 0, 0.42, FAN), "identity_claims")
edge("e_s5_sc", "s5", "score", SV(1, 0.5, 0, 0.78, FAN), "entity_corroboration")

edge("e_s8_b", "s8", "b0", EV(1, 0.25, 0, 0.5))

FORBID = ("edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;dashed=1;strokeWidth=3;"
          "strokeColor=#A33A30;endArrow=none;startArrow=none;fontColor=#A33A30;"
          "fontSize=11;fontStyle=1;labelBackgroundColor=#FFFFFF;"
          "exitX=1;exitY=0.5;exitDx=0;exitDy=0;entryX=0;entryY=0.88;entryDx=0;entryDy=0;")
edge("e_lbl", "z4", "z2", FORBID, "✕  invariant #5 — FORBIDDEN")

bundle = [
    ("manifest.json", "rules_rollup · rules_version · engine_commit\ninput_hash · run_id · version_verified"),
    ("trace.jsonl", "the derivation DAG"),
    ("claims.csv", "every claim + its weight"),
    ("membership.csv", "obs → entity · link_basis · link_weight"),
    ("entities.csv", "resolved values + per-field confidence"),
    ("resolutions.csv", "pure join view — makes no new decisions,\nso emits no trace steps of its own"),
    ("metrics.jsonl", "label-free metrics"),
    ("REPORT.md", "derived render"),
]
y = 130
for i, (f, d) in enumerate(bundle):
    h = 54 if "\n" in d else 42
    node(f"b{i}", f"<b>{f}</b>\n{d}", 962, y, 238, h, OUT)
    y += h + 8

labels = ["<b>labels-initial.csv</b>  (wide)", "<b>labels.csv</b>  (long, authoritative)",
          "<b>VERSION</b>", "archive/&lt;version&gt;-labels.csv", "journal.jsonl",
          "last_label_state.json"]
y = 644
for i, f in enumerate(labels):
    node(f"l{i}", f, 58, y, 228, 32, LBL)
    y += 36

notes = [
    ("n1", "invariant #4 — rule-state traceability",
     "Every output binds to the exact rule state via run_id → manifest.json. engine_commit "
     "carries a -dirty suffix when the tree was not clean, because a clean sha reported "
     "while uncommitted code actually ran is worse than no value at all."),
    ("n2", "invariant #1 — DAG discipline",
     "Entity resolution reads identity claims ONLY, never resolved field values. There is "
     "no field → cluster feedback loop, which is what keeps the pipeline a strict DAG and "
     "keeps replay meaningful."),
    ("n3", "invariant #3 — scoring parity",
     "One scoring function, used by claim construction, entity resolution and the "
     "cross-observation re-score. Coefficients may differ per claim type; the formula shape "
     "may not. Do not write a second implementation."),
    ("n4", "invariant #7 — no output without a derivation",
     "Enforced mechanically by replay.py, not by convention. Absence needs a step too: "
     "no_extraction, no_identity_claim, vocab_reject and merge_refused are first-class "
     "steps, not silences."),
    ("n5", "determinism",
     "No reliance on dict ordering, input row order, or wall clock anywhere in the pipeline "
     "path. run_id carries the only timestamp and no decision reads it. Two runs over "
     "identical input produce byte-identical trace.jsonl."),
    ("n6", "three combining operations, never conflated",
     "same value witnessed twice → max + saturating bonus (scoring.py)\n"
     "a propagation chain → multiplicative (fields.py)\n"
     "different fields into one record → harmonic mean (confidence.py)"),
]
y = 130
for nid, head, body in notes:
    node(nid, f"<b>{head}</b>\n\n{body}", 1248, y, 322, 150, NOTE)
    y += 164

PAGE1 = "\n".join(CELLS)

# ================= PAGE 2 =================
CELLS = []
node("t2", "The five surfaces — what each is for", 40, 16, 900, 32, TITLE)
node("t2s", "Each is a standalone entry point. None is a flag on another. Run everything from the repo root.",
     40, 50, 1100, 22, SUB)

hdr = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#232323;strokeColor=none;"
       "fontColor=#FFFFFF;fontSize=12;align=left;spacingLeft=10;verticalAlign=top;"
       "spacingTop=10;")
cell = ("rounded=0;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#8C8C8C;"
        "fontSize=11;align=left;spacingLeft=9;verticalAlign=top;spacingTop=8;" + INK)
cell_g = cell.replace("#FFFFFF", "#E6F3E3").replace("#8C8C8C", "#5D9152")
cell_y = cell.replace("#FFFFFF", "#FFF6D9").replace("#8C8C8C", "#B39A2E")
cell_p = cell.replace("#FFFFFF", "#F1E9F6").replace("#8C8C8C", "#7A5091")

COL = "text;html=1;fontSize=11;fontStyle=1;fontColor=#101010;align=left;spacingLeft=9;"
for cid, txt, cx, cw in [("h0", "SURFACE", 40, 300), ("h1", "READS", 352, 250),
                         ("h2", "DOES", 614, 430), ("h3", "WRITES", 1056, 250),
                         ("h4", "USE IT WHEN", 1318, 262)]:
    node(cid, txt, cx, 96, cw, 24, COL)

bands = [
    ("run", "<b>run.py</b>\n\npython3 run.py\npython3 run.py &lt;obs.csv&gt; &lt;rules_dir&gt; &lt;out_root&gt;",
     "rules/\nobs-data/observations.csv\n\n<b>NEVER labels/</b>",
     "Runs the ten stages on page 1 and writes a self-describing bundle. Two runs over "
     "unchanged input and rules are byte-identical — so re-running to see whether "
     "something changed tells you nothing.",
     "runs/&lt;run_id&gt;/\nruns/history.jsonl\nruns/last_rules_state.json",
     "You changed a rule, or you want fresh output.\n\nThis is the golden path; every "
     "other surface consumes what it writes.", cell_g),
    ("replay", "<b>replay.py</b>\n\npython3 replay.py runs/&lt;run_id&gt;",
     "runs/&lt;run_id&gt;/trace.jsonl\n\n<b>NEVER the engine</b> — an AST test forbids "
     "importing scoring / entity / fields",
     "Reconstructs claims.csv, membership.csv and entities.csv from the trace ALONE, then "
     "diffs against what actually shipped. If it could reach the engine it might recompute "
     "a value instead of reading it, and the gate would pass on an incomplete trace.",
     "stdout: REPLAY OK\nexit 1 + the diff on any hole",
     "After ANY change touching trace emission.\n\nIt is cheap, and it is the only thing "
     "that catches a silently broken audit trail.", cell_g),
    ("eval", "<b>eval.py</b>\n\npython3 eval.py\npython3 eval.py &lt;obs&gt; &lt;rules&gt; &lt;labels&gt; &lt;out&gt;",
     "rules/ <b>AND</b> labels/\n\nThe only component that reads both sides. Invokes "
     "run_pipeline as a black box.",
     "Scores the run against ground truth. Four-bucket diff (fixed / broken / "
     "stable_correct / stable_incorrect) per label, because aggregate metrics hide cases a "
     "rule change silently broke. Raises LabelsMovedError BEFORE writing anything if the "
     "label set moved — a moved label must never read as a rule regression.",
     "evals/&lt;eval_id&gt;/\n   outcomes.json\n   metrics.jsonl\n   regressions.csv\n"
     "   eval_manifest.json\n+ re-renders REPORT.md",
     "You want accuracy, or you want to know what a rule change actually moved.\n\n"
     "Regressions are LOGGED, not blocked.", cell_y),
    ("labeltools", "<b>label_tools.py</b>\n\npython3 label_tools.py",
     "labels/labels-initial.csv\nobs-data/observations.csv\nclaims.yaml#fields "
     "(passed in at the entry point)",
     "Converts the wide rendering into the long-format labels.csv everything else reads, "
     "and runs the transitivity check. Refuses to regenerate over adjudicated labels, and "
     "refuses a column that is neither wide-file metadata nor a declared field — a "
     "silently ignored column drops that field's whole ground truth.",
     "labels/labels.csv\nlabels/VERSION\nlabels/archive/\nlabels/journal.jsonl\n"
     "labels/last_label_state.json",
     "You edited labels-initial.csv.\n\nlabels.csv is generated, never maintained by hand.",
     cell_p),
    ("adj", "<b>adjudicate.py</b>\n\npython3 adjudicate.py runs/&lt;run_id&gt;\n"
     "python3 adjudicate.py runs/&lt;run_id&gt; returned.csv",
     "resolutions.csv — read only to EXCLUDE what is already settled\nlabels/labels.csv\n"
     "claims.yaml#fields",
     "<b>EXPORT</b>: an evidence-only packet for the medium/low certainty stratum — no "
     "resolved values, no confidence, so the adjudicator is asked &quot;what is this?&quot; "
     "rather than &quot;is this right?&quot;.\n"
     "<b>IMPORT</b>: two independent gates — rejected (inadmissible) and refused (does not "
     "supersede, per basis precedence).",
     "adjudication/&lt;run_id&gt;/packet.csv\n\nthen, on import, all of labels/ above",
     "You need better ground truth where the labeller was unsure.\n\nAn observation leaves "
     "the queue only when EVERY row it has is settled.", cell_p),
]

y = 126
for nid, surface, reads, does, writes, when, tint in bands:
    node(f"{nid}_s", surface, 40, y, 300, 184, hdr)
    node(f"{nid}_r", reads, 352, y, 250, 184, tint)
    node(f"{nid}_d", does, 614, y, 430, 184, cell)
    node(f"{nid}_w", writes, 1056, y, 250, 184, tint)
    node(f"{nid}_u", when, 1318, y, 262, 184, cell)
    y += 198

node("guard",
     "<b>The two guard rails, and why they point in opposite directions</b>\n\n"
     "<b>invariant #5 — labels never enter the runtime path.</b>  run.py and everything it "
     "imports have no code path that reads labels/. This is why eval.py, label_tools.py and "
     "adjudicate.py live OUTSIDE obs_pipeline/: structural, not conventional. It is what "
     "stops the pipeline from doing better on labelled data than it would on new data.\n\n"
     "<b>invariant #6 — pipeline output never enters hard-stratum label creation.</b>  The "
     "mirror. In adjudicate.py, SELECTION may read pipeline output; PRESENTATION must not. A "
     "human handed a plausible answer and asked &quot;is this right?&quot; agrees more often "
     "than one asked &quot;what is this?&quot;, and unblinded labelling drifts ground truth "
     "toward whatever the pipeline already believes — worst precisely where it is "
     "confidently wrong.",
     40, y + 12, 1540, 186, NOTE)

node("order",
     "<b>Everyday order:</b>   python3 -m pytest -q   →   python3 eval.py   →   "
     "python3 replay.py runs/&lt;run_id&gt;   →   read regressions.csv   →   bump rules/VERSION",
     40, y + 214, 1540, 40,
     "rounded=0;whiteSpace=wrap;html=1;fillColor=#CFE2FA;strokeColor=#3B6BA5;"
     "fontSize=12;align=center;verticalAlign=middle;" + INK)

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
       + page("Pipeline stage flow", "pipeline-flow", 1620, 1250, PAGE1) + "\n"
       + page("Surfaces", "surfaces", 1640, 1420, PAGE2) + "\n"
       + "</mxfile>\n")

with open("obs-pipeline.drawio", "w", encoding="utf-8") as fh:
    fh.write(xml)
print("wrote obs-pipeline.drawio", len(xml), "bytes")

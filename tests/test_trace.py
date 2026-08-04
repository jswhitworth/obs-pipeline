from obs_pipeline.trace import Traced, Tracer, canonical_json


def test_step_id_is_content_addressed_not_a_counter():
    a, b = Tracer(), Tracer()
    x = a.step(op="extract", rule_id="extraction.yaml#r1", output="P3245-LVE")
    y = b.step(op="extract", rule_id="extraction.yaml#r1", output="P3245-LVE")
    assert x.step_id == y.step_id
    assert x.step_id.startswith("sha256:")


def test_different_content_yields_different_step_id():
    t = Tracer()
    x = t.step(op="extract", rule_id="extraction.yaml#r1", output="P3245-LVE")
    y = t.step(op="extract", rule_id="extraction.yaml#r1", output="Q6135-LE")
    assert x.step_id != y.step_id


def test_parents_are_recorded_as_step_ids_of_traced_inputs():
    t = Tracer()
    raw = t.step(op="extract", rule_id="extraction.yaml#r1", output="AXIS")
    norm = t.step(op="normalize", rule_id="normalization.yaml#vendor_alias",
                  output="Axis Communications", parents=[raw])
    row = next(s for s in t.steps() if s["step_id"] == norm.step_id)
    assert row["parents"] == [raw.step_id]


def test_identical_steps_are_deduplicated():
    t = Tracer()
    t.step(op="extract", rule_id="r", output="v")
    t.step(op="extract", rule_id="r", output="v")
    assert len(t.steps()) == 1


def test_steps_are_returned_sorted_by_step_id():
    t = Tracer()
    for v in ["c", "a", "b"]:
        t.step(op="extract", rule_id="r", output=v)
    ids = [s["step_id"] for s in t.steps()]
    assert ids == sorted(ids)


def test_canonical_json_is_key_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

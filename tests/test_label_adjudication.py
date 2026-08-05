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


def test_no_scoring_machinery_reaches_the_label_module():
    """§7.2.2: resist recursing the claim-scoring math (§2.3) onto labels.

    Complementary to test_precedence_is_a_flat_list_not_a_scoring_formula,
    which greps ONE function's source for four literal substrings -- it would
    not fire against a module-scope import (outside that function's body),
    an aliased import (`from obs_pipeline.scoring import score as _s`), or a
    renamed reimplementation, and `score_claims(` does not even match the
    literal "score(". This walks the WHOLE MODULE's AST instead, so it
    catches the pattern rather than the spelling: any obs_pipeline import
    anywhere in the file outside a narrow rule-reading allowlist, the power
    operator the independence bonus
    (`b * (1 - r ** (k - 1))`) is built on, and the named scoring
    identifiers themselves as actual AST names/attributes -- not as
    substrings of unrelated words (so "weighted" in a docstring, a plain
    string constant, does not trip this check; only a real reference to the
    identifier `score`, `weight`, etc. would).
    """
    import ast
    import inspect

    import label_tools

    tree = ast.parse(inspect.getsource(label_tools))

    banned_identifiers = {
        "score", "independence_bonus", "conflict_penalty", "weight",
        "base_weights", "witness_groups", "Coefficients",
    }

    # An ALLOWLIST, not a denylist: anything new under obs_pipeline is banned
    # by default, so a future scoring module cannot slip in by being unlisted.
    #
    # This was a blanket ban on all of obs_pipeline, which was the right
    # proxy while label_tools needed nothing from the package. It no longer
    # is: `claims.yaml#fields` gates what may become ground truth, and a
    # private copy of that list here is the rules-as-authority defect this
    # repo has now found four times. `loader` and `vocab` READ RULES; they
    # carry no scoring math. The principle §7.2.2 actually protects is
    # enforced by the two checks below -- the scoring identifiers and the
    # power operator -- which are untouched and remain exact.
    allowed_modules = {"obs_pipeline.loader", "obs_pipeline.vocab"}

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", None) or ""
            names = [alias.name for alias in node.names]
            offenders = [
                m for m in [module] + names
                if m.startswith("obs_pipeline") and m not in allowed_modules
            ]
            assert not offenders, (
                f"label_tools.py may import only {sorted(allowed_modules)} "
                f"from obs_pipeline (rule reading, no scoring math): "
                f"{offenders}")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            raise AssertionError(
                "label_tools.py uses the power operator -- the "
                "independence bonus is built on one (§2.3); ground truth "
                "must not recurse the claim-scoring math onto labels"
            )
        name = getattr(node, "id", None) or getattr(node, "attr", None)
        if name in banned_identifiers:
            raise AssertionError(
                f"label_tools.py references scoring identifier '{name}' "
                f"as an actual name/attribute, not just a substring (§7.2.2)"
            )


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


def test_the_obs_pipeline_allowance_is_narrow():
    """The import ban above was relaxed from "no obs_pipeline at all" to an
    allowlist, so this pins that the relaxation is narrow: the scoring
    modules are still refused, and refused by DEFAULT rather than by being
    enumerated. A guard loosened without a test of its new edge is a guard
    that quietly becomes "anything goes".
    """
    import ast

    def _offenders(source, allowed={"obs_pipeline.loader", "obs_pipeline.vocab"}):
        found = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", None) or ""
                names = [a.name for a in node.names]
                found += [m for m in [module] + names
                          if m.startswith("obs_pipeline") and m not in allowed]
        return found

    assert _offenders("from obs_pipeline.loader import load_rules") == []
    assert _offenders("from obs_pipeline.scoring import score") == \
        ["obs_pipeline.scoring"]
    assert _offenders("import obs_pipeline.entity") == ["obs_pipeline.entity"]
    # Not yet written, and banned anyway -- the allowlist fails closed.
    assert _offenders("from obs_pipeline.calibration import fit") == \
        ["obs_pipeline.calibration"]

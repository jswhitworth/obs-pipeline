# tests/test_final_output.py
"""final-output.csv: the per-observation consumer projection.

Its column set and both rendering decisions (confidence bands, direct-only
suppression) live in field_resolution.yaml#final_output and are validated
at load time -- these tests pin the projection to resolutions.csv (the
full-fidelity row it is derived from) and the validation to the loader.
"""
import csv
import shutil
from pathlib import Path

import pytest
import yaml

from obs_pipeline.loader import (CrossFileError, RULE_FILES, VOCAB_FILE,
                                 load_rules)
from run import run_pipeline

BUNDLE = None


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    global BUNDLE
    if BUNDLE is None:
        BUNDLE = run_pipeline("obs-data/observations.csv", "rules",
                              tmp_path_factory.mktemp("runs"))
    return BUNDLE


def _rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _config():
    return yaml.safe_load(
        Path("rules/field_resolution.yaml").read_text())["final_output"]


def test_header_is_the_configured_columns_plus_identity_and_band(bundle):
    with open(bundle / "final-output.csv", newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    assert header == (["obs_id"] + _config()["columns"]
                      + ["entity_id", "confidence"])


def test_one_row_per_observation_in_resolution_order(bundle):
    final = _rows(bundle / "final-output.csv")
    resolutions = _rows(bundle / "resolutions.csv")
    assert len(final) == len(resolutions)
    assert [(f["obs_id"], f["entity_id"]) for f in final] == \
        [(r["obs_id"], r["entity_id"]) for r in resolutions]


def test_confidence_is_banded_per_the_configured_thresholds(bundle):
    bands = _config()["confidence_bands"]
    final = _rows(bundle / "final-output.csv")
    resolutions = _rows(bundle / "resolutions.csv")
    assert {f["confidence"] for f in final} <= {"high", "medium", "low"}
    for f, r in zip(final, resolutions):
        c = float(r["confidence"])
        expected = ("high" if c >= bands["high"]
                    else "medium" if c >= bands["medium"] else "low")
        assert f["confidence"] == expected, r["obs_id"]


def test_direct_only_fields_are_blank_unless_witnessed(bundle):
    """An inherited firmware must not read as if this observation reported
    it; every other configured field passes through untouched."""
    cfg = _config()
    final = _rows(bundle / "final-output.csv")
    resolutions = _rows(bundle / "resolutions.csv")
    suppressed = 0
    for f, r in zip(final, resolutions):
        for field in cfg["columns"]:
            if field in cfg["direct_only"] and \
                    r[f"{field}_provenance"] != "direct":
                assert f[field] == "", (r["obs_id"], field)
                suppressed += r[field] != ""
            else:
                assert f[field] == r[field], (r["obs_id"], field)
    # The dataset genuinely exercises the rule (propagated firmware exists);
    # otherwise this test would pass vacuously.
    assert suppressed >= 1


def _rules_copy(tmp_path) -> Path:
    dest = tmp_path / "rules"
    dest.mkdir()
    for name in (*RULE_FILES, VOCAB_FILE, "VERSION"):
        shutil.copy(Path("rules") / name, dest / name)
    return dest


def _edit_final_output(rules_dir, mutate):
    path = Path(rules_dir) / "field_resolution.yaml"
    parsed = yaml.safe_load(path.read_text())
    mutate(parsed)
    path.write_text(yaml.safe_dump(parsed, sort_keys=True))


def test_loader_refuses_columns_that_drift_from_declared_fields(tmp_path):
    """The recurring hardcoded-vocabulary defect, prevented at init: a
    declared field missing from the columns list must fail loudly, never
    silently vanish from the file consumers read."""
    rules_dir = _rules_copy(tmp_path)
    _edit_final_output(rules_dir,
                       lambda p: p["final_output"]["columns"].remove("model"))
    with pytest.raises(CrossFileError, match="final_output.columns"):
        load_rules(rules_dir)


def test_loader_refuses_unordered_confidence_bands(tmp_path):
    rules_dir = _rules_copy(tmp_path)
    _edit_final_output(
        rules_dir,
        lambda p: p["final_output"].update(
            confidence_bands={"high": 0.4, "medium": 0.7}))
    with pytest.raises(CrossFileError, match="confidence_bands"):
        load_rules(rules_dir)


def test_loader_refuses_a_missing_final_output_block(tmp_path):
    rules_dir = _rules_copy(tmp_path)
    _edit_final_output(rules_dir, lambda p: p.pop("final_output"))
    with pytest.raises(CrossFileError, match="final_output"):
        load_rules(rules_dir)

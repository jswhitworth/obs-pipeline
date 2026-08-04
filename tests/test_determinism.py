# tests/test_determinism.py
"""Design doc §1 and §9.2: identical input and rules produce identical output,
and content-addressed step_ids make traces diffable across rule versions. A
sequence counter would make every trace superficially different and destroy
that property."""
import csv
import json

import pytest

from replay import replay_diff
from run import run_pipeline


@pytest.fixture(scope="module")
def two_runs(tmp_path_factory):
    root = tmp_path_factory.mktemp("runs")
    return (run_pipeline("obs-data/observations.csv", "rules", root),
            run_pipeline("obs-data/observations.csv", "rules", root))


def test_trace_is_byte_identical_across_runs(two_runs):
    a, b = two_runs
    assert (a / "trace.jsonl").read_bytes() == (b / "trace.jsonl").read_bytes()


def test_entity_ids_are_identical_across_runs(two_runs):
    """Every tabular row carries `run_id` by design (§3), so these files can
    never be byte-identical across runs — that is provenance, not
    nondeterminism. The determinism claim is about everything else: the same
    entities, the same members, the same values, the same derivation steps."""
    a, b = two_runs

    def rows_without_run_id(path):
        with open(path, newline="", encoding="utf-8") as fh:
            return [{k: v for k, v in row.items() if k != "run_id"}
                    for row in csv.DictReader(fh)]

    for name in ("entities.csv", "membership.csv", "claims.csv",
                 "resolutions.csv"):
        assert rows_without_run_id(a / name) == rows_without_run_id(b / name), name


def test_run_id_is_the_only_thing_that_varies(two_runs):
    """The mirror: confirm the files really do differ, so the test above is
    comparing two distinct runs rather than a directory with itself."""
    a, b = two_runs
    assert a != b
    assert (a / "entities.csv").read_text() != (b / "entities.csv").read_text()


def test_only_run_id_and_engine_commit_vary_between_manifests(two_runs):
    a, b = two_runs
    ma = json.loads((a / "manifest.json").read_text())
    mb = json.loads((b / "manifest.json").read_text())
    for key in ["input_hash", "rules_rollup", "rules_version", "rules_files"]:
        assert ma[key] == mb[key]


def test_replay_passes_on_a_fresh_run(two_runs):
    assert replay_diff(two_runs[0]) == []

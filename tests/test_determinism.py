# tests/test_determinism.py
"""Design doc §1 and §9.2: identical input and rules produce identical output,
and content-addressed step_ids make traces diffable across rule versions. A
sequence counter would make every trace superficially different and destroy
that property."""
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
    a, b = two_runs
    assert (a / "entities.csv").read_text() == (b / "entities.csv").read_text()


def test_only_run_id_and_engine_commit_vary_between_manifests(two_runs):
    a, b = two_runs
    ma = json.loads((a / "manifest.json").read_text())
    mb = json.loads((b / "manifest.json").read_text())
    for key in ["input_hash", "rules_rollup", "rules_version", "rules_files"]:
        assert ma[key] == mb[key]


def test_replay_passes_on_a_fresh_run(two_runs):
    assert replay_diff(two_runs[0]) == []

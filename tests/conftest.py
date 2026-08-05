"""Session guard: the suite must not write into the repo's build output.

Every test runs the pipeline against a tmp root. The one way that silently
stops being true is a call that DEFAULTS an output root instead of passing
one -- `eval.evaluate(..., runs_root="runs")` is CWD-relative, so forgetting
the keyword sends real bundles into the repo.

The clutter is not the problem. `runs/last_rules_state.json` is the §6.2
baseline that decides `version_verified`: re-stamping it to the current
rollup is exactly what makes the unbumped-version check pass when it should
have fired. A test run would disarm the check for the next real run.

`git status` cannot police this -- runs/, evals/ and adjudication/ are
gitignored -- so it is checked here instead.
"""
import hashlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The gitignored build-output roots. Tracked state (rules/, labels/) is left
# to git, which can already see it.
GUARDED = ("runs", "evals", "adjudication")

# Sizes catch a new bundle or an appended history. They do NOT catch
# last_rules_state.json, whose rollup is a fixed-width sha256 -- a re-stamp
# rewrites the content at an identical size. That file is the reason this
# guard exists, so it is hashed rather than measured.
HASHED = ("runs/last_rules_state.json",)


def _snapshot() -> dict:
    state = {}
    for name in GUARDED:
        root = REPO_ROOT / name
        if not root.exists():
            # Absent is a distinct state from empty: a suite that CREATES
            # runs/ on a fresh checkout has still written to the repo.
            state[name] = None
            continue
        state[name] = {
            str(p.relative_to(root)): p.stat().st_size
            for p in sorted(root.rglob("*")) if p.is_file()
        }
    for rel in HASHED:
        p = REPO_ROOT / rel
        state[f"sha256:{rel}"] = (
            hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
        )
    return state


@pytest.fixture(scope="session", autouse=True)
def repo_build_output_is_untouched():
    before = _snapshot()
    yield
    after = _snapshot()
    if before == after:
        return

    changed = sorted(k for k in before.keys() | after.keys()
                     if before.get(k) != after.get(k))
    raise AssertionError(
        "the test suite wrote into the repo's build output: "
        f"{', '.join(changed)}.\n"
        "Some call site is defaulting an output root instead of passing a "
        "tmp one -- check for `evaluate(...)` without `runs_root=` or "
        "`run_pipeline(...)` without an explicit out_root.\n"
        "This matters beyond clutter: rewriting runs/last_rules_state.json "
        "re-stamps the §6.2 version baseline and silences the "
        "unbumped-version check for the next real run."
    )

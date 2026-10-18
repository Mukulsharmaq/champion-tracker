"""Seller corrections from Slack, replayed against today's rules (written by pg_system.feedback_loop).
A case marked "fixed" must stay fixed; "open" cases are listed so the next rule change can close them.
Live corrections hold real names, so they are written to feedback/ (git-ignored) by default; point REGRESSION_FILE
(or config.json -> regression_file) at a file in your private repo if you want them in CI.
So this test never passes on an empty file, it also replays tests/data/regression_sample.jsonl: two synthetic cases
built from the demo run (invented names), one marked fixed that must pass and one open that still fails. The open one
carries the right answer the seller picked (expected_play): it passes only when the rules give exactly that play.
Run:  python3 tests/test_regressions.py     ·     PYTHONPATH=src python3 -m pg_system --check-regressions (exit 1 on a broken fix)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import copy  # noqa: E402

from pg_system.feedback_loop import broken_fixes, check_regressions, replay_case  # noqa: E402

PATH = os.environ.get("REGRESSION_FILE") or os.path.join(ROOT, "feedback", "regression_cases.jsonl")
SAMPLE = os.path.join(ROOT, "tests", "data", "regression_sample.jsonl")


def test_fixed_corrections_stay_fixed():
    cases = check_regressions(PATH)
    broken = broken_fixes(cases)
    assert not broken, [(c["action"], c.get("person"), c["result"]) for c in broken]
    open_ = [c for c in cases if c.get("status") != "fixed"]
    if open_:
        print(f"{len(open_)} open correction(s) waiting for a rule change")


def test_the_committed_sample_replays_both_ways():
    """Not vacuous: the sample loads, its fixed case passes and its open case is still caught as wrong. If a rule
    change fixes the open case, mark it fixed in the sample and add a new open one."""
    cases = check_regressions(SAMPLE)
    assert len(cases) >= 2, len(cases)
    fixed = [c for c in cases if c.get("status") == "fixed"]
    open_ = [c for c in cases if c.get("status") != "fixed"]
    assert fixed and all(c["result"]["passes"] for c in fixed), [(c["person"], c["result"]) for c in fixed]
    assert open_ and not any(c["result"]["passes"] for c in open_), [(c["person"], c["result"]) for c in open_]
    assert not broken_fixes(cases)


def test_a_fixed_case_that_fails_again_is_reported():
    """broken_fixes is what --check-regressions exits 1 on."""
    cases = check_regressions(SAMPLE)
    flipped = [dict(c, status="fixed") for c in cases]
    assert [c["person"] for c in broken_fixes(flipped)] == [c["person"] for c in cases if not c["result"]["passes"]]


def test_a_case_with_the_right_answer_passes_only_on_that_answer():
    """The sample's open case: the seller said Tailspin signed (a customer now) and picked P5. A data or rule fix that
    makes it a customer gives P5 and passes; a fix that lands on any other play does not, though it changed the play."""
    case = next(c for c in check_regressions(SAMPLE) if c.get("expected_play"))
    assert case["expected_play"] == "P5" and case["result"] == {"now": "P7", "passes": False}, case["result"]
    fixed = copy.deepcopy(case)
    fixed["replay"]["account"]["type"] = "customer"
    assert replay_case(fixed) == {"now": "P5", "passes": True}
    assert not replay_case(dict(fixed, expected_play="P4"))["passes"]


if __name__ == "__main__":
    failed = 0
    for name, fn in [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]:
        try:
            fn()
            print("PASS", name)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL", name, "->", type(e).__name__, str(e)[:400])
    sys.exit(1 if failed else 0)

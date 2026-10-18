"""Precision and recall of the brief's extractor on two LLM-written synthetic sets (no real call text).

1. The old set (tests/data/brief_eval_set.csv): 160 call turns, CRM notes and field notes,
   written and labelled by one LLM agent from a spec the rule author wrote, so it is not independent of the rule
   author. Lexicon v2.0 was measured once on all 160 rows before any tuning. v2.1 was tuned on E001-E080 only;
   E081-E160 is the holdout and its floors below fail the build when a change makes things worse.
   v2.2 (a reviewer's critique, not tuning on the holdout): pricing friction needs the price word and the friction cue
   within 6 words in one sentence and is never the customer's own travel spend unless it points at us; a month,
   quarter or date counts as timing only next to a decision / contract / RFP / deadline / go-live word; past expansion
   with "no plans for more" is not a request; competitor_rules for names that are everyday words. On the holdout only
   timing moved: precision 0.75 -> 1.00, recall 0.75 -> 0.92.
   v2.3 (error analysis on the independent dev half below, not on this set): holdout unchanged except people change
   recall 0.60 -> 0.70 (E088 "Sarah's taken over": 'took over' now covers every verb form) and engagement recall
   0.50 -> 0.60 (E092 "I read your blog"). Honest caveat: E088 was seen while grepping this set for false-hit risk of
   the new people patterns, so that one gain is not a clean holdout number. Nothing dropped; floors raised to match.
   v2.4 (red-team probes, not any evaluation row): price comparisons that point at us, a role that left / quit / moved
   on, competitor aliases and exclude_match_case, parent-company pressure as a competitor risk. No row's themes,
   competitors, stance or confidence changed on any of the 360 rows, so every number below is the same as v2.3.

2. The independent set: 200 texts written by one agent and labelled by another, neither of which saw the lexicon.
   N001-N100 (tests/data/indep_dev_texts.csv + indep_dev_labels.csv) is the DEV half, and it is now the TUNING half:
   v2.3 and observer check v1.1 were fixed against its errors, so its numbers below are optimistic by construction.
   The other 100 (N101-N200, tests/data/indep_test_*.csv) were held out and measured once after that tuning
   (2026-10-18). Only those numbers say whether the fixes generalise; their floors at the end of this file guard
   against regressions and are never tuned toward.
   Before (lexicon v2.2, observer check v1.0), dev half, precision / recall:
                       pricing    competitor expansion  timing     people     engagement
     lexicon only      0.80/0.57  1.00/1.00  1.00/0.60  0.57/0.57  1.00/0.29  1.00/0.20
     + observer run 1  0.86/0.86  1.00/1.00  0.50/0.60  0.47/1.00  1.00/0.86  0.67/0.80
     + observer run 2  0.86/0.86  1.00/1.00  0.50/0.60  0.47/1.00  1.00/0.86  0.67/0.80
     + observer run 3  0.75/0.86  1.00/1.00  0.50/0.60  0.47/1.00  1.00/0.86  0.67/0.80
   Causes found and fixed (each rule names its cause in contracts/lexicon.json -> _doc):
     pricing    pushback on commercial terms (contract length: "a multi-year term is a non-starter"); "doesn't make
                sense" next to a price word; price above what they budgeted ("more than we budgeted")
     timing     operational dates counted as buying windows: a bare "deadline" or a travel / hiring "freeze" (the
                customer's own operations) is no longer context; procurement, purchase paperwork (paperwork, PO,
                order form, MSA) and "make a call" are; month abbreviations and holidays name a date
     speculation our team's read written as "my read", "imo", "feels like", "I imagine" (was counted as reported)
     expansion  a move to a higher tier ("outgrown", "next tier", "upsell")
     people     a named role leaving / retiring / resigning / new; a move to another side or team; "takes over"
     engagement using, attending or passing round our content ("used the ROI calculator", "did that webinar",
                "the benchmark report is getting forwarded"), including note shorthand without "the"
     observer   timing evidence needs a purchase word (a date alone counts only when its sentence has one); expansion
                evidence must point at more of our product, not the customer's own growth (headcount, hiring, a new
                office); a request for content (or our team sending it) is not engagement. Prompt v1.1 says the same.
   Not fixed: N010 "If we expand ..., what kind of discount are we talking?" (labelled not friction; "discount" is a
   standalone friction phrase the old set relies on), N045 / N083 single-run observer false hits.

Observer: tests/data/observer_haiku_output.json (old set) and tests/data/indep_dev_observer_haiku_run{1,2,3}.json (dev
half) are what Claude Haiku returned for observer prompt v1.0 (no labels shown). They stay valid for scoring the code's
check (v1.1); new runs with prompt v1.1 are recorded on the held-out half.
The competitor numbers on every set score three invented names the writers were given (Ledgerly, Spendwise,
TripStack), so a competitor precision or recall of 1.00 says the matcher finds names it was handed, nothing more.
contracts/lexicon.travel-expense.json (a real market list) has no figure here; its test is at the end of this file.
Run:  python3 tests/test_brief_eval.py            (tables)
      python3 tests/test_brief_eval.py --errors   (every dev false hit and miss, by theme, with the labeller's note)
"""
import csv
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from gtm_briefs.extract import classify  # noqa: E402
from gtm_briefs.observer import validate  # noqa: E402

THEMES = ["pricing_friction", "competitor", "expansion", "timing", "people_change", "engagement"]
DATA = lambda name: os.path.join(ROOT, "tests", "data", name)  # noqa: E731
ROWS = list(csv.DictReader(open(DATA("brief_eval_set.csv"), encoding="utf-8")))
HOLDOUT = [r for r in ROWS if int(r["id"][1:]) > 80]
FLOORS = {  # old-set holdout, lexicon v2.3: (precision, recall). v2.2 people (1.00, 0.60), engagement (1.00, 0.50).
    "pricing_friction": (1.00, 0.66), "competitor": (0.94, 1.00), "expansion": (1.00, 0.80),
    "timing": (1.00, 0.91), "people_change": (1.00, 0.70), "engagement": (1.00, 0.60)}


def predict(r, observed=()):
    c = classify({"source": r["source"], "side": r["side"], "text": r["text"]}, observed)
    return set(c["themes"]) if c["confidence"] in ("direct", "reported", "unverified") else set()


def scores(rows, pred=predict):
    st = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        p = pred(r)
        for t in THEMES:
            y, hit = r[t] == "1", t in p
            if hit and y:
                st[t][0] += 1
            elif hit:
                st[t][1] += 1
            elif y:
                st[t][2] += 1
    return {t: (tp / (tp + fp) if tp + fp else 0.0, tp / (tp + fn) if tp + fn else 0.0, tp, fp, fn)
            for t, (tp, fp, fn) in ((t, st[t]) for t in THEMES)}


def below(s, floors):
    """Themes whose precision or recall fell under its floor -> their (precision, recall)."""
    return {t: (round(s[t][0], 2), round(s[t][1], 2)) for t, (p, r) in floors.items()
            if s[t][0] + 1e-9 < p or s[t][1] + 1e-9 < r}


def _observed(path):
    return {o["id"]: o.get("themes") or {} for o in json.load(open(path, encoding="utf-8"))}


def with_observer(observed):
    """A predict function that adds the themes the code accepts from one stored observer run."""
    return lambda r: predict(r, validate(r["text"], observed.get(r["id"], {})))


OBSERVED = _observed(DATA("observer_haiku_output.json"))
TEXT = {r["id"]: r["text"] for r in ROWS}
FLOORS_WITH_OBSERVER = {  # old-set holdout, lexicon v2.3 + observer prompt v1.0 outputs, code check v1.1.
    "pricing_friction": (0.90, 1.00), "competitor": (0.94, 1.00), "expansion": (1.00, 1.00),     # v2.1 timing: (0.66, 1.00)
    "timing": (0.85, 1.00), "people_change": (1.00, 1.00), "engagement": (1.00, 0.90)}
predict_with_observer = with_observer(OBSERVED)


def proposals(rows=ROWS, observed=OBSERVED):
    """(proposed, accepted, rejected [(id, theme, evidence)]) for a stored observer run on these rows."""
    text = {r["id"]: r["text"] for r in rows}
    out = [(i, t, ev, t in validate(text[i], {t: ev})) for i, th in observed.items() if i in text for t, ev in th.items()]
    return len(out), sum(ok for *_, ok in out), [(i, t, ev) for i, t, ev, ok in out if not ok]


# ---- the independent set, DEV half = the TUNING half (N001-N100) ------------------------------------------------
def _dev_rows():
    labels = {r["id"]: r for r in csv.DictReader(open(DATA("indep_dev_labels.csv"), encoding="utf-8"))}
    return [dict(r, **{k: v for k, v in labels[r["id"]].items() if k != "id"})
            for r in csv.DictReader(open(DATA("indep_dev_texts.csv"), encoding="utf-8"))]


DEV = _dev_rows()
DEV_RUNS = {f"run{i}": _observed(DATA(f"indep_dev_observer_haiku_run{i}.json")) for i in (1, 2, 3)}
FLOORS_DEV = {  # TUNING half, lexicon v2.3 (v2.2 in the module docstring). Optimistic: the fixes came from these rows.
    "pricing_friction": (0.87, 1.00), "competitor": (1.00, 1.00), "expansion": (1.00, 1.00),
    "timing": (1.00, 1.00), "people_change": (1.00, 1.00), "engagement": (1.00, 1.00)}
FLOORS_DEV_WITH_OBSERVER = {  # TUNING half, lexicon v2.3 + each stored observer run (prompt v1.0), check v1.1: worst run.
    "pricing_friction": (0.77, 1.00), "competitor": (1.00, 1.00), "expansion": (1.00, 1.00),
    "timing": (0.87, 1.00), "people_change": (1.00, 1.00), "engagement": (1.00, 1.00)}


def errors(rows, pred=predict):
    """Every false hit and miss: [(theme, 'FP'|'FN', id, text, labeller's note)], by theme."""
    out = []
    for r in rows:
        p = pred(r)
        for t in THEMES:
            y, hit = r[t] == "1", t in p
            if hit != y:
                out.append((t, "FP" if hit else "FN", r["id"], r["text"], r.get("notes", "")))
    return sorted(out, key=lambda e: (THEMES.index(e[0]), e[1], e[2]))


# ---- tests --------------------------------------------------------------------------------------------------------
def test_holdout_precision_and_recall_hold_their_floors():
    bad = below(scores(HOLDOUT), FLOORS)
    assert not bad, bad


def test_observer_lifts_recall_and_the_code_still_rejects_what_it_cannot_verify():
    bad = below(scores(HOLDOUT, predict_with_observer), FLOORS_WITH_OBSERVER)
    assert not bad, bad


def test_observer_proposals_without_a_cue_for_their_theme_are_rejected():
    n, accepted, rejected = proposals()
    assert n == 142 and accepted <= 136, (n, accepted)        # a looser check would accept more: review it first
    got = {(i, t) for i, t, _ in rejected}
    assert {("E047", "engagement"), ("E087", "timing"), ("E082", "competitor"), ("E110", "competitor")} <= got, rejected


def test_dev_tuning_half_lexicon_holds_its_floors():
    """TUNING half: v2.3 was fixed against these rows, so this only guards against a later change undoing a fix."""
    bad = below(scores(DEV), FLOORS_DEV)
    assert not bad, bad


def test_dev_tuning_half_with_each_observer_run_holds_its_floors():
    """TUNING half, every stored run (not just the best): a model's run-to-run variation is part of the number."""
    bad = {name: b for name, run in DEV_RUNS.items()
           for b in [below(scores(DEV, with_observer(run)), FLOORS_DEV_WITH_OBSERVER)] if b}
    assert not bad, bad


def test_dev_tuning_half_observer_check_refuses_dates_alone_customer_growth_and_content_requests():
    """The v1.0 over-fires on the dev half, by cause, are refused by check v1.1 in every run that proposed them;
    the misses the old cue lists caused (a higher tier, contract-length terms, a role move, our calculator) pass."""
    text = {r["id"]: r["text"] for r in DEV}
    proposed = lambda i, t: [run[i][t] for run in DEV_RUNS.values() if t in run.get(i, {})]  # noqa: E731
    operational_dates = [("N006", "timing"), ("N036", "timing"), ("N042", "timing"), ("N057", "timing"),
                         ("N071", "timing"), ("N076", "timing"), ("N089", "timing")]
    own_growth = [("N004", "expansion"), ("N005", "expansion"), ("N099", "expansion")]
    content_requests = [("N002", "engagement"), ("N079", "engagement")]
    for i, t in operational_dates + own_growth + content_requests:
        assert proposed(i, t) and all(validate(text[i], {t: ev}) == [] for ev in proposed(i, t)), (i, t, proposed(i, t))
    for i, t in [("N014", "expansion"), ("N054", "expansion"), ("N037", "pricing_friction"), ("N035", "people_change"),
                 ("N041", "engagement"), ("N008", "timing"), ("N059", "timing"), ("N064", "timing")]:
        assert proposed(i, t) and all(validate(text[i], {t: ev}) == [t] for ev in proposed(i, t)), (i, t, proposed(i, t))


def _print(title, s):
    print(title)
    for t in THEMES:
        p, r, tp, fp, fn = s[t]
        print(f"  {t:17} precision {p:.2f}  recall {r:.2f}  (tp {tp}, fp {fp}, fn {fn})")


def report():
    _print("old set, all 160, lexicon only", scores(ROWS))
    _print("old set, holdout E081-E160, lexicon only", scores(HOLDOUT))
    _print("old set, all 160, lexicon + observer (Claude Haiku, prompt v1.0)", scores(ROWS, predict_with_observer))
    _print("old set, holdout E081-E160, lexicon + observer (Claude Haiku, prompt v1.0)",
           scores(HOLDOUT, predict_with_observer))
    for name, rows in (("all 160", ROWS), ("holdout", HOLDOUT)):
        n, accepted, rejected = proposals(rows)
        print(f"observer proposals, old set {name}: {accepted} of {n} accepted; rejected: "
              + "; ".join(f"{i} {t} {ev!r}" for i, t, ev in rejected))
    _print("independent DEV half N001-N100 (TUNING half), lexicon only", scores(DEV))
    for name, run in DEV_RUNS.items():
        _print(f"independent DEV half (TUNING half), lexicon + observer {name} (Claude Haiku, prompt v1.0, check v1.1)",
               scores(DEV, with_observer(run)))
        n, accepted, _ = proposals(DEV, run)
        print(f"  observer proposals {name}: {accepted} of {n} accepted")


def print_errors():
    runs = [(f"lexicon + observer {k}", with_observer(v)) for k, v in DEV_RUNS.items()]
    for title, pred in [("lexicon only", predict)] + runs:
        print(f"== DEV half errors, {title}")
        for t, kind, i, text, note in errors(DEV, pred):
            print(f"  {t:17} {kind} {i}  {text}" + (f"   [label note: {note}]" if note else ""))


if __name__ == "__main__":
    report()
    if "--errors" in sys.argv:
        print_errors()
    for fn in (test_holdout_precision_and_recall_hold_their_floors,
               test_observer_lifts_recall_and_the_code_still_rejects_what_it_cannot_verify,
               test_observer_proposals_without_a_cue_for_their_theme_are_rejected,
               test_dev_tuning_half_lexicon_holds_its_floors,
               test_dev_tuning_half_with_each_observer_run_holds_its_floors,
               test_dev_tuning_half_observer_check_refuses_dates_alone_customer_growth_and_content_requests):
        fn()
        print("PASS", fn.__name__)

# ---- Independent set, held-out half (N101-N200): measured ONCE after tuning on N001-N100 (2026-10-18) ------------
# Written by one agent, labelled by another, neither shown the rules. These floors are the numbers as first measured;
# they are a regression guard, not a target to tune toward (tuning on this half would turn it into a dev set).
INDEP_TEST = [dict(t, **{k: v for k, v in l.items() if k != "id"}) for t, l in zip(
    csv.DictReader(open(os.path.join(ROOT, "tests", "data", "indep_test_texts.csv"), encoding="utf-8")),
    csv.DictReader(open(os.path.join(ROOT, "tests", "data", "indep_test_labels.csv"), encoding="utf-8")))]
INDEP_TEST_FLOORS_LEXICON = {"pricing_friction": (0.66, 0.18), "competitor": (1.00, 1.00), "expansion": (0.60, 0.60),
                             "timing": (0.66, 0.66), "people_change": (1.00, 0.75), "engagement": (1.00, 0.75)}


def test_independent_heldout_half_measured_once_holds_its_floors():
    assert len(INDEP_TEST) == 100 and all(r["id"].startswith("N") for r in INDEP_TEST)
    s = scores(INDEP_TEST)
    bad = {t: (round(s[t][0], 2), round(s[t][1], 2)) for t, (p, r) in INDEP_TEST_FLOORS_LEXICON.items()
           if s[t][0] + 1e-9 < p or s[t][1] + 1e-9 < r}
    assert not bad, bad


# Observer prompt v1.1, three runs on N101-N200 (tests/data/indep_test_observer_v1.1_run{1,2,3}.json, no labels shown),
# scored with the code's check. Floors = the worst run per theme as first measured (indep_test_measurement_2026-10-18.json).
INDEP_TEST_RUNS = {f"run{i}": _observed(DATA(f"indep_test_observer_v1.1_run{i}.json")) for i in (1, 2, 3)}
INDEP_TEST_FLOORS_WITH_OBSERVER = {"pricing_friction": (0.72, 0.72), "competitor": (1.00, 1.00), "expansion": (0.50, 0.80),
                                   "timing": (0.33, 0.66), "people_change": (1.00, 1.00), "engagement": (1.00, 1.00)}


def test_independent_heldout_half_with_each_observer_run_holds_its_floors():
    bad = {name: b for name, run in INDEP_TEST_RUNS.items()
           for b in [below(scores(INDEP_TEST, with_observer(run)), INDEP_TEST_FLOORS_WITH_OBSERVER)] if b}
    assert not bad, bad


TE_PATH = os.path.join(ROOT, "contracts", "lexicon.travel-expense.json")


def test_travel_expense_lexicon_fires_only_where_a_real_competitor_is_written():
    """The sets were labelled against the demo's invented names, so the real market list has no precision or recall here.
    What they can check: on all 360 rows it fires only on the two rows that write a real name (N058, N145: Concur pushed
    by a parent company, labelled 0 only because the labeller was given the demo names, both read as a risk), and never
    on the rows that use the everyday words ('cost center' in N030 and N134, the card issuer 'Amex' in E156 and N160)."""
    from gtm_briefs.lexicon import compile_all, load
    rx = compile_all(load(TE_PATH))
    got = {r["id"]: classify({"source": r["source"], "side": r["side"], "text": r["text"]}, rx=rx)
           for r in ROWS + DEV + INDEP_TEST}
    assert {i: c["competitors"] for i, c in got.items() if c["competitors"]} == {"N058": ["Concur"], "N145": ["Concur"]}
    assert got["N058"]["stance"] == got["N145"]["stance"] == "risk"
    assert not any(got[i]["competitors"] for i in ("N030", "N134", "E156", "N160"))


# ---- Held-out half through the REAL API code path (observer.call_openai via OpenRouter, 2026-10-18) -------------
# Model openai/gpt-5.6-luna (the account's provider settings block Anthropic models). Recorded replies are re-scored
# through today's validate(); floors are the numbers as measured, not targets (timing over-fires: N113 "three quotes").
REAL_RUNS = [json.load(open(os.path.join(ROOT, "tests", "data", f"indep_test_observer_real_api_luna_run{n}.json"), encoding="utf-8"))
             for n in (1, 2, 3)]
REAL_FLOORS = {"pricing_friction": (0.88, 0.72), "competitor": (1.00, 1.00), "expansion": (0.57, 0.80),
               "timing": (0.28, 0.66), "people_change": (1.00, 1.00), "engagement": (1.00, 1.00)}


def test_real_api_runs_on_the_heldout_half_hold_their_measured_floors():
    for run in REAL_RUNS:
        prop = {p["id"]: p.get("themes") or {} for p in run["proposals_raw"]}
        def pred(r, prop=prop):
            c = classify({"source": r["source"], "side": r["side"], "text": r["text"]}, validate(r["text"], prop.get(r["id"], {})))
            return set(c["themes"]) if c["confidence"] in ("direct", "reported", "unverified") else set()
        s = scores(INDEP_TEST, pred)
        bad = {t: (round(s[t][0], 2), round(s[t][1], 2)) for t, (p, r) in REAL_FLOORS.items() if s[t][0] + 1e-9 < p or s[t][1] + 1e-9 < r}
        assert not bad, (run["started_utc"], bad)
        assert run["stats"]["malformed_batches"] == 0 and run["model"] == "openai/gpt-5.6-luna"


if __name__ == "__main__":
    print("independent held-out half N101-N200, lexicon only (measured once)")
    for t, (p, r, tp, fp, fn) in scores(INDEP_TEST).items():
        print(f"  {t:17} precision {p:.2f}  recall {r:.2f}  (tp {tp}, fp {fp}, fn {fn})")
    for name, run in INDEP_TEST_RUNS.items():
        _print(f"independent held-out half, lexicon + observer {name} (prompt v1.1, check v1.1, measured once)",
               scores(INDEP_TEST, with_observer(run)))
    for fn in (test_independent_heldout_half_measured_once_holds_its_floors,
               test_independent_heldout_half_with_each_observer_run_holds_its_floors,
               test_travel_expense_lexicon_fires_only_where_a_real_competitor_is_written):
        fn()
        print("PASS", fn.__name__)
    test_real_api_runs_on_the_heldout_half_hold_their_measured_floors()
    print("PASS test_real_api_runs_on_the_heldout_half_hold_their_measured_floors")

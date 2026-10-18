"""Broken-record tests. Each case is a failure that broke a naive version on real data, now locked in.
Run without pytest:  python3 tests/test_rules.py      (pytest also works: python3 -m pytest tests)
"""
import json
import os
import re
import sys
import tempfile
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from champion_tracker import config as C  # noqa: E402
from champion_tracker.adapters import people_data  # noqa: E402
from champion_tracker.adapters.fixtures import load_lookups, load_seed  # noqa: E402
from champion_tracker.matching import days_since  # noqa: E402
from champion_tracker.feedback import crm_write_back, record, regression_case  # noqa: E402
from champion_tracker.rules import row_id, run  # noqa: E402
from champion_tracker.slack_card import build_card  # noqa: E402

TODAY = date(2026, 10, 18)
ACME_JOB = {"company": "Acme Robotics", "website": "acme.com", "start": "2020-01-01", "end": "2026-09-01"}
ACME_OPEN = dict(ACME_JOB, end=None)


def seed(tag="owns_program", title="Global Travel Manager", excl=""):
    return {"Slug": "acme", "Story URL": "u", "Customer": "Acme Robotics", "Name": "Pat Lee", "First": "Pat", "Last": "Lee",
            "Title on page": title, "Action tag": tag, "Evidence (verbatim)": "e", "Exclude reason": excl}


def cand(cur="", site="", title="", exp=(), headline="", change="", size="1001 to 5000", updated="2026-08-10", name="Pat Lee", **kw):
    return dict({"full_name": name, "current_company_name": cur, "current_company_website": site, "current_title": title,
                 "headline": headline, "experience": list(exp), "latest_company_change_at": change,
                 "employee_count_range": size, "updated_at": updated, "linkedin_url": "x"}, **kw)


def one(cands, **seed_kw):
    s = seed(**seed_kw)
    return run([s], {("Pat Lee", "acme"): {"customer": "Acme Robotics", "ok": True, "candidates": cands}}, TODAY)[0]


# ---- 1. same person? ------------------------------------------------------------------------------------------
def test_same_name_different_people_is_held():
    r = one([cand("Target", "target.com", "Cashier"), cand("Law Office", "", "Paralegal")])
    assert r["Decision"] == "Hold" and "share this name" in r["Reason"], r

def test_substring_name_hit_is_not_the_person():
    r = one([cand("Acme Robotics", "acme.com", "Travel Manager", name="Patricia Leeson")])
    assert r["Decision"] == "Hold" and "No profile" in r["Reason"], r

def test_nickname_prefix_still_matches():
    r = one([cand("Newco", "newco.com", "Head of Travel", [ACME_JOB, {"company": "Newco", "website": "newco.com", "start": "2026-09-01"}],
                  name="Patrick Lee")])
    assert r["Same person?"] == "Confirmed", r

def test_no_profile_is_held():
    r = one([])
    assert r["Decision"] == "Hold" and "No profile" in r["Reason"], r

def test_only_one_namesake_without_a_tie_is_only_likely():
    r = one([cand("Newco", "newco.com", "Head of Travel", change="2026-09-20")])
    assert r["Same person?"] == "Likely" and r["Decision"] == "Hold", r

def test_name_collision_company_is_not_the_customer():
    r = one([cand("Acmerealty", "acmerealty.com", "Agent")])
    assert r["Decision"] == "Hold", r

def test_stale_profile_that_contradicts_evidence_is_held():
    r = one([cand("Acme Robotics", "acme.com", "PA", updated="2024-07-31")], title="Head of Travel")
    assert r["Decision"] == "Hold" and "Records disagree" in r["Reason"], r


# ---- 2. moved? --------------------------------------------------------------------------------------------------
def test_old_employer_in_a_lagging_field_is_ignored():
    r = one([cand("Newco", "newco.com", "Head of Travel", [ACME_JOB], change="2026-09-01", org_domain="acme.com")])
    assert r["Moved?"] == "Yes", r

def test_board_seat_is_not_a_move():
    r = one([cand("Travel Association", "assoc.org", "Chair, Regional Board", [ACME_OPEN])])
    assert r["Moved?"] == "No" and "side seat" in r["Reason"], r

def test_self_employed_side_gig_is_not_a_move():
    r = one([cand("Self-employed", "", "Content Creator", [{"company": "Self-employed", "start": "2025-03-01"}, ACME_OPEN])])
    assert r["Moved?"] == "No", r

def test_latest_job_already_ended_means_current_employer_unknown():
    r = one([cand("Newco", "newco.com", "Head of Travel",
                  [ACME_JOB, {"company": "Newco", "website": "newco.com", "start": "2026-09-01", "end": "2026-10-01"}])])
    assert r["Decision"] == "Hold" and "ended" in r["Reason"], r

def test_rename_or_acquisition_is_not_a_move():
    r = one([cand("Bigco Media", "bigco.com", "AP Manager", [ACME_JOB, {"company": "Bigco Media", "website": "bigco.com", "start": "2020-01-01"}])])
    assert r["Decision"] == "Hold" and "rename or acquisition" in r["Reason"], r


# ---- 3. champion or user, fresh or old, worth working? ---------------------------------------------------------
def test_quoted_only_person_who_moved_gets_no_play():
    r = one([cand("Newco", "newco.com", "VP Sales", [ACME_JOB], change="2026-09-01")], tag="quoted")
    assert r["Decision"] == "No action" and not r.get("Play"), r

def test_move_older_than_90_days_goes_to_review():
    r = one([cand("Newco", "newco.com", "Head of Travel", [dict(ACME_JOB, end="2026-03-01"),
             {"company": "Newco", "website": "newco.com", "start": "2026-03-01"}])])
    assert r["Decision"] == "Review" and "days old" in r["Reason"], r

def test_move_age_counts_from_joining_new_company():
    r = one([cand("Newco", "newco.com", "Head of Finance", [{"company": "Newco", "website": "newco.com", "start": "2026-09-15"},
             {"company": "Midco", "start": "2022-02-01", "end": "2026-08-01"}, dict(ACME_JOB, end="2022-02-01")])])
    assert r["Decision"] == "Card" and r["Move age (days)"] == 33, r

def test_small_new_company_goes_to_review():
    r = one([cand("Tinyco", "tiny.co", "Head of Travel", [ACME_JOB, {"company": "Tinyco", "website": "tiny.co", "start": "2026-09-15"}], size="11 to 50")])
    assert r["Decision"] == "Review" and "small" in r["Reason"], r

def test_old_job_still_open_goes_to_review():
    r = one([cand("Newco", "newco.com", "Head of Travel", [ACME_OPEN, {"company": "Newco", "website": "newco.com", "start": "2026-09-15"}])])
    assert r["Decision"] == "Review" and "still open" in r["Reason"], r

def test_clean_recent_champion_move_becomes_a_card():
    r = one([cand("Newco", "newco.com", "Head of Travel", [ACME_JOB, {"company": "Newco", "website": "newco.com", "start": "2026-09-01"}])])
    assert r["Decision"] == "Card" and r["Play"] == C.PLAYS["P1"], r


# ---- 4. end to end on the synthetic fixtures -------------------------------------------------------------------
EXPECTED = {"Dana Reyes": "Card", "Ben Ortiz": "Card", "Leo Park": "Review", "Sam Lee": "Hold", "Grace Holt": "Hold",
            "Elena Cruz": "Hold", "Hugo Lind": "Hold", "Morgan Ellis": "Hold", "Zed Coe": "Hold", "Priya Nair": "Watch",
            "Rita Shah": "Watch", "Ivy Chen": "Watch", "Max Hale": "No action", "Pat Vendor": "Excluded"}

def test_fixtures_end_to_end():
    s = load_seed(os.path.join(ROOT, "fixtures", "champions.csv"))
    lookups, aliases, known = load_lookups(os.path.join(ROOT, "fixtures", "candidates.json"))
    got = {r["Person"]: r for r in run(s, lookups, TODAY, aliases, known)}
    bad = {p: (got[p]["Decision"], d, got[p]["Reason"]) for p, d in EXPECTED.items() if got[p]["Decision"] != d}
    assert not bad, bad
    assert got["Ben Ortiz"]["Play"] == C.PLAYS["P2"], got["Ben Ortiz"]   # moved to an existing customer


# ---- 5. card, feedback, contract consistency --------------------------------------------------------------------
def test_card_has_evidence_link_and_correction_buttons():
    r = one([cand("Newco", "newco.com", "Head of Travel", [ACME_JOB, {"company": "Newco", "website": "newco.com", "start": "2026-09-01"}])])
    card = build_card(r)
    actions = [e["action_id"] for b in card["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert actions == ["accept", "snooze", "dismiss", "wrong_person", "wrong_play"], actions
    assert any("Source" in json.dumps(b) for b in card["blocks"])

def test_only_card_and_review_rows_make_cards():
    r = one([])
    try:
        build_card(r)
        raise AssertionError("a Hold row must not become a card")
    except ValueError:
        pass

def test_seller_correction_becomes_a_regression_case():
    with tempfile.TemporaryDirectory() as d:
        r = one([cand("Newco", "newco.com", "Head of Travel", [ACME_JOB, {"company": "Newco", "website": "newco.com", "start": "2026-09-01"}])])
        ev = record("wrong_person", r, user="rep@example.com", note="Different Pat Lee", log_path=os.path.join(d, "f.jsonl"), at="2026-10-18T10:00:00+00:00")
        case = regression_case(ev)
        assert case["expect_at_most"] == "Hold" and case["was"] == "Card", case
        assert crm_write_back(ev)["fields"]["Champion_Move_Status__c"] == "Identity rejected"

def test_config_matches_the_written_contract():
    text = open(os.path.join(ROOT, "contracts", "context_contract.yaml"), encoding="utf-8").read()
    assert int(re.search(r"move_recent_days:\s*(\d+)", text).group(1)) == C.RECENT_DAYS
    assert int(re.search(r"profile_stale_days:\s*(\d+)", text).group(1)) == C.PROFILE_STALE_DAYS
    defs = open(os.path.join(ROOT, "contracts", "champion_definition.yaml"), encoding="utf-8").read()
    for tag in C.CHAMPION_TAGS:
        assert f"{tag}:" in defs, tag


# ---- 6. cases found by the multi-lens review (each reproduced first, then fixed) ---------------------------------
NEWCO = {"company": "Newco", "website": "newco.com", "start": "2026-09-15"}

def test_board_seat_after_old_job_ended_is_held_not_carded():
    r = one([cand("Travel Association", "assoc.org", "Chair, Regional Board", [ACME_JOB, {"company": "Travel Association", "website": "assoc.org", "start": "2026-09-15"}])])
    assert r["Decision"] == "Hold" and "side seat" in r["Reason"], r

def test_freelance_gig_after_old_job_ended_is_held():
    r = one([cand("Studio X", "studiox.com", "Designer", [ACME_JOB, {"company": "Studio X", "website": "studiox.com", "start": "2026-09-15", "job_type": "freelance"}])])
    assert r["Decision"] == "Hold", r

def test_common_word_in_headline_is_not_an_identity_tie():
    s = dict(seed(), Customer="Travel Leaders Group")
    rows = run([s], {("Pat Lee", "acme"): {"customer": "Travel Leaders Group", "ok": True,
                "candidates": [cand("Contoso", "contoso.com", "Head of Travel", [{"company": "Contoso", "website": "contoso.com", "start": "2026-09-01"}],
                                    headline="Head of Travel at Contoso")]}}, TODAY)
    assert rows[0]["Same person?"] != "Confirmed", rows[0]

def test_headline_with_ex_marker_is_a_tie():
    r = one([cand("Newco", "newco.com", "Head of Travel", [NEWCO], headline="Head of Travel | ex-Acme")])
    assert r["Same person?"] == "Confirmed" and "headline" in r["Tied by"], r

def test_domain_beats_name_for_existing_customer():
    seeds = [seed(), dict(seed(), Name="Kim Roe", First="Kim", Last="Roe", Customer="Hooli", Slug="hooli")]
    looks = {("Pat Lee", "acme"): {"customer": "Acme Robotics", "ok": True, "candidates": [
                cand("Hooli Realty", "hoolirealty.com", "Head of Travel", [ACME_JOB, {"company": "Hooli Realty", "website": "hoolirealty.com", "start": "2026-09-15"}])]},
             ("Kim Roe", "hooli"): {"customer": "Hooli", "ok": True, "candidates": [
                cand("Hooli", "hooli.com", "Travel Manager", [{"company": "Hooli", "website": "hooli.com", "start": "2019-01-01"}], name="Kim Roe")]}}
    r = next(x for x in run(seeds, looks, TODAY) if x["Person"] == "Pat Lee")
    assert r["Play"] == C.PLAYS["P1"] and r["New account"].startswith("Not a known customer"), r

def test_duplicate_profiles_at_same_domain_are_merged():
    a = cand("Contoso Systems", "contoso.com", "Head of Travel", [ACME_JOB, {"company": "Contoso Systems", "website": "contoso.com", "start": "2026-09-15"}])
    b = cand("Contoso", "contoso.com", "Head of Travel", [ACME_JOB, {"company": "Contoso", "website": "contoso.com", "start": "2026-09-15"}], updated="2026-01-01")
    r = one([a, b])
    assert r["Same person?"] == "Confirmed", r

def test_returning_employee_is_aged_from_the_current_stint():
    r = one([cand("Newco", "newco.com", "Head of Travel", [{"company": "Newco", "website": "newco.com", "start": "2015-01-01", "end": "2018-01-01"},
             dict(ACME_JOB, start="2018-02-01", end="2026-09-01"), {"company": "Newco", "website": "newco.com", "start": "2026-09-15"}])])
    assert r["Move age (days)"] == 33 and r["Decision"] == "Card", r

def test_promotion_at_new_company_does_not_reset_move_age():
    r = one([cand("Newco", "newco.com", "VP Finance", [{"company": "Newco", "website": "newco.com", "start": "2026-05-01"},
             {"company": "Newco", "website": "newco.com", "start": "2025-06-01", "end": "2026-05-01"}, dict(ACME_JOB, end="2025-05-01")])])
    assert r["Joined new company"] == "2025-06-01" and r["Move age (days)"] == 504, r

def test_unknown_join_date_never_falls_back_to_leaving_date():
    r = one([cand("Newco", "newco.com", "Head of Travel", [ACME_JOB, {"company": "Newco", "website": "newco.com"}])])
    assert r["Decision"] == "Review" and "move date unknown" in r["Reason"], r

def test_champion_move_without_evidence_or_link_is_held():
    s = dict(seed(), **{"Evidence (verbatim)": "", "Story URL": ""})
    rows = run([s], {("Pat Lee", "acme"): {"customer": "Acme Robotics", "ok": True, "candidates": [
                cand("Newco", "newco.com", "Head of Travel", [ACME_JOB, NEWCO])]}}, TODAY)
    assert rows[0]["Decision"] == "Hold" and "evidence" in rows[0]["Reason"], rows[0]

def test_unknown_action_tag_is_held():
    r = one([cand("Newco", "newco.com", "Head of Travel", [ACME_JOB, NEWCO])], tag="sat_on_a_call")
    assert r["Decision"] == "Hold" and "Unknown action tag" in r["Reason"], r

def test_malformed_dates_and_null_lists_never_crash():
    assert days_since("2026-09", TODAY) == 47 and days_since("unknown", TODAY) is None and days_since(None, TODAY) is None
    r = one([dict(cand("Newco", "newco.com", "Head of Travel"), experience=None, latest_company_change_at="2026-09")])
    assert r["Decision"] in ("Hold", "Review", "Card"), r
    r2 = run([seed()], {("Pat Lee", "acme"): {"customer": "Acme Robotics", "ok": True, "candidates": None}}, TODAY)[0]
    assert r2["Decision"] == "Hold", r2

def test_seed_with_byte_order_mark_still_loads():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "seed.csv")
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write("Slug,Name,First,Last,Customer,Action tag\nacme,Pat Lee,Pat,Lee,Acme Robotics,chose\n")
        assert load_seed(path)[0]["Slug"] == "acme"

def test_live_adapter_maps_provider_keys_and_searches_deep():
    c = people_data.normalise({"current_employer": "Newco", "current_employer_website": "newco.com", "linkedin_headline": "ex-Acme"})
    assert c["current_company_name"] == "Newco" and c["current_company_website"] == "newco.com" and c["headline"] == "ex-Acme"
    import inspect
    assert inspect.signature(people_data.search_candidates).parameters["depth"].default >= 50

def test_row_id_is_stable_and_write_back_never_matches_by_name():
    s = seed()
    assert row_id(s) == row_id(dict(s)) and row_id(dict(s, **{"Row id": "003ABC"})) == "003ABC"
    try:
        crm_write_back({"action": "accept", "row_id": None, "note": "", "rule_version": "1.0"})
        raise AssertionError("write-back without a row id must fail")
    except ValueError:
        pass


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except AssertionError as e:
            failed += 1
            print("FAIL", name, "->", str(e)[:300])
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)

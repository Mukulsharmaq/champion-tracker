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
    # name + company first; the deep search runs only when that finds nobody tied to the customer
    calls = []
    tied = {"full_name": "Pat Lee", "current_employer": "Acme Robotics", "experience_json": "[]"}
    def fake(url, body, tries=4):
        calls.append((url, body.get("limit")))
        return {"contacts": [dict(tied)]} if url == "M" and body.get("company_name") == "Acme Robotics" else {"contacts": []}
    saved = (people_data._post, people_data.MATCH_URL, people_data.SEARCH_URL)
    people_data._post, people_data.MATCH_URL, people_data.SEARCH_URL = fake, "M", "S"
    try:
        r = people_data.lookup_record(seed())
        assert r["source"] == "match" and calls == [("M", None)], (r, calls)
        calls.clear()
        r = people_data.lookup_record(dict(seed(), Customer="Other Co"))
        assert r["source"] == "search" and calls == [("M", None), ("S", 50)], calls
    finally:
        people_data._post, people_data.MATCH_URL, people_data.SEARCH_URL = saved

def test_row_id_is_stable_and_write_back_never_matches_by_name():
    s = seed()
    assert row_id(s) == row_id(dict(s)) and row_id(dict(s, **{"Row id": "003ABC"})) == "003ABC"
    try:
        crm_write_back({"action": "accept", "row_id": None, "note": "", "rule_version": "1.0"})
        raise AssertionError("write-back without a row id must fail")
    except ValueError:
        pass


# ---- 7. hiring-manager critique (each reproduced first, then fixed) ---------------------------------------------
def _fake_provider(plain, filtered, calls, past=None):
    """A fake _post: the plain same-name search returns `plain`, the company-filtered one returns `filtered`, the
    past-company (work-history) one returns `past` (default: nothing)."""
    def fake(url, body, tries=4):
        calls.append((url, body.get("limit"), body.get("company_name")) + ((("past", body["past_company_name"]),)
                                                                            if "past_company_name" in body else ()))
        if url == "M":
            return {"contacts": []}
        if "past_company_name" in body:
            return {"contacts": [dict(x) for x in (past or [])]}
        return {"contacts": [dict(x) for x in (filtered if body.get("company_name") else plain)]}
    return fake

def _namesakes(k):
    return [{"full_name": "Pat Lee", "current_employer": f"Co{i}", "linkedin_url": f"li/{i}", "experience_json": "[]"} for i in range(k)]

def _with_fake(fake, fn):
    saved = (people_data._post, people_data.MATCH_URL, people_data.SEARCH_URL)
    people_data._post, people_data.MATCH_URL, people_data.SEARCH_URL = fake, "M", "S"
    try:
        return fn()
    finally:
        people_data._post, people_data.MATCH_URL, people_data.SEARCH_URL = saved

def test_capped_search_runs_one_full_same_name_pull_and_keeps_only_tied_profiles():
    calls = []
    tied = {"full_name": "Pat Lee", "current_employer": "Acme Robotics", "linkedin_url": "li/acme", "experience_json": "[]"}
    def fake(url, body, tries=4):
        calls.append((url, body.get("limit")))
        if url == "M":
            return {"contacts": []}
        return {"contacts": _namesakes(50) if body.get("limit") == 50 else _namesakes(300) + [dict(tied)]}
    r = _with_fake(fake, lambda: people_data.lookup_record(seed()))
    assert calls == [("M", None), ("S", 50), ("S", people_data.DEEP_LIMIT)], calls
    assert r["capped"] and r["full_search"] and r["searched"] == 301 and r["source"] == "search+full_pull"
    assert len(r["candidates"]) == 51          # the 50 first results + the one tied profile; untied namesakes are not kept
    assert run([seed()], {("Pat Lee", "acme"): r}, TODAY)[0]["Same person?"] == "Confirmed"

def test_capped_search_with_a_tied_profile_needs_no_second_search():
    calls = []
    plain = _namesakes(49) + [{"full_name": "Pat Lee", "current_employer": "Acme Robotics", "experience_json": "[]"}]
    r = _with_fake(_fake_provider(plain, [], calls), lambda: people_data.lookup_record(seed()))
    assert r["capped"] is True and [c for c in calls if c[0] == "S"] == [("S", 50, None)], calls

def test_short_search_is_not_capped():
    calls = []
    r = _with_fake(_fake_provider(_namesakes(12), [], calls), lambda: people_data.lookup_record(seed()))
    assert r["capped"] is False and r["source"] == "search" and len(calls) == 2, (r, calls)

def test_search_candidates_sends_the_company_filter():
    calls = []
    _with_fake(_fake_provider([], [], calls), lambda: people_data.search_candidates("Pat", "Lee", company="Acme Robotics"))
    assert calls == [("S", 50, "Acme Robotics")], calls

def test_capped_lookup_with_no_tie_is_held_with_the_cap_reason():
    def looks(capped):
        return {("Pat Lee", "acme"): {"customer": "Acme Robotics", "ok": True, "capped": capped,
                "candidates": [cand("Target", "target.com", "Cashier"), cand("Law Office", "", "Paralegal")]}}
    r = run([seed()], looks(True), TODAY)[0]
    assert r["Decision"] == "Hold" and r["Reason"].startswith("Search hit the 50-result cap"), r["Reason"]
    assert "none of those is tied to Acme Robotics" in r["Reason"] and "further down" in r["Reason"], r["Reason"]
    r = run([seed()], looks(False), TODAY)[0]
    assert r["Reason"] == "2 people share this name; none is tied to Acme Robotics.", r["Reason"]
    r = run([seed()], {("Pat Lee", "acme"): {"customer": "Acme Robotics", "ok": True, "capped": True, "candidates": []}}, TODAY)[0]
    assert r["Reason"].startswith("Search hit the 50-result cap") and "No profile" not in r["Reason"], r["Reason"]

def test_different_companies_never_merge():
    from champion_tracker.matching import same_company
    for a, b in [("Bank of America", "BNY Mellon"), ("US Bank", "Bank of America"), ("Delta", "Delta Dental"),
                 ("American Express GBT", "American Express"), ("Travel Leaders Group", "Travel Leaders Network"),
                 ("Bank", "Bank Technologies"), ("Travel", "Travel Leaders Group"), ("Delta", "Delta Systems"),
                 ("Acme", "Acme Robotics"), ("Ace", "AceRealty")]:
        assert not same_company(a, b), (a, b)
    for a, b in [("Acme USA, LLC", "Acme North America Inc"), ("Acme Robotics", "Acme Robotics, Inc."),
                 ("Pendo.io", "Pendo"), ("Hertha BSC GmbH & Co. KGaA", "Hertha BSC"), ("SurveyMonkey", "Survey Monkey"),
                 ("Luck Grove", "Luck Grove Telecom"), ("The Coca-Cola Company", "Coca Cola"), ("Tony Holdings", "Tony")]:
        assert same_company(a, b), (a, b)

def test_still_at_the_customer_under_its_legal_name_by_its_own_domain():
    s = dict(seed(), Customer="Ryder")
    rows = run([s], {("Pat Lee", "acme"): {"customer": "Ryder", "ok": True, "candidates": [
        cand("Ryder System, Inc.", "ryder.com/en-us", "SVP Procurement", [{"company": "Ryder System, Inc.", "website": "ryder.com", "start": "2015-01-01"}])]}}, TODAY)
    assert rows[0]["Decision"] == "Watch" and rows[0]["Reason"] == "Still at Ryder.", rows[0]
    rows = run([s], {("Pat Lee", "acme"): {"customer": "Ryder", "ok": True, "candidates": [      # stale + contradicting title
        cand("Ryder System, Inc.", "ryder.com", "PA", [{"company": "Ryder System, Inc.", "website": "ryder.com", "start": "2015-01-01"}], updated="2024-07-31")]}}, TODAY)
    assert rows[0]["Decision"] == "Hold" and "Records disagree" in rows[0]["Reason"], rows[0]

def test_a_namesake_at_a_lookalike_company_never_teaches_the_customers_domain():
    from champion_tracker.rules import learn_customer_domains
    acme = dict(seed(), Customer="Acme")
    kim = dict(seed(), Name="Kim Roe", First="Kim", Last="Roe", Customer="Hooli", Slug="hooli")
    looks = {("Pat Lee", "acme"): {"customer": "Acme", "ok": True, "candidates": [
                 cand("Acme Realty", "acmerealty.com", "Agent", [{"company": "Acme Realty", "website": "acmerealty.com", "start": "2019-01-01"}]),
                 cand("Acme", "acme.com", "Travel Manager", [{"company": "Acme", "website": "acme.com", "start": "2018-01-01"}])]},
             ("Kim Roe", "hooli"): {"customer": "Hooli", "ok": True, "candidates": [
                 cand("Acme Realty", "acmerealty.com", "Head of Travel", [{"company": "Hooli", "website": "hooli.com", "start": "2019-01-01", "end": "2026-09-01"},
                      {"company": "Acme Realty", "website": "acmerealty.com", "start": "2026-09-15"}], name="Kim Roe")]}}
    assert learn_customer_domains(looks.values())["Acme"] == {"acme.com"}
    r = next(x for x in run([acme, kim], looks, TODAY) if x["Person"] == "Kim Roe")
    assert r["New account"].startswith("Not a known customer") and r["Play"] == C.PLAYS["P1"], r

def test_evidence_date_rules_out_a_namesakes_old_internship():
    old = cand("Lee Advisory Partners", "leeadv.com", "Owner", [{"company": "Lee Advisory Partners", "website": "leeadv.com", "start": "2015-01-01"},
               {"company": "Acme Robotics", "website": "acme.com", "start": "2012-06-01", "end": "2014-08-01", "job_type": "internship"}])
    r = one([old], tag="chose")
    assert r["Same person?"] == "Confirmed", r                                   # no Evidence date: as before
    s = dict(seed(tag="chose"), **{"Evidence date": "2023-05-01"})
    r = run([s], {("Pat Lee", "acme"): {"customer": "Acme Robotics", "ok": True, "candidates": [old]}}, TODAY)[0]
    assert r["Same person?"] != "Confirmed" and r["Decision"] == "Hold", r
    s = dict(s, **{"Evidence date": "2015"})                                      # within a year of leaving: still counts
    r = run([s], {("Pat Lee", "acme"): {"customer": "Acme Robotics", "ok": True, "candidates": [old]}}, TODAY)[0]
    assert r["Same person?"] == "Confirmed", r

def test_nicknames_reversed_order_honorifics_variants_and_non_latin_names():
    from champion_tracker.matching import name_matches
    yes = [("Bill Smith", "William", "Smith", None), ("William Smith", "Bill", "Smith", None), ("Bob Jones", "Robert", "Jones", None),
           ("Yang Gary", "Gary", "Yang", None), ("Dr. Gary Yang", "Gary", "Yang", None), ("Mrs Jane Doe", "Jane", "Doe", None),
           ("Prof Jane Doe, Ph.D.", "Jane", "Doe", None), ("Jane Smith", "Jane", "Doe", ["Jane Smith"]),
           ("Bob Church", "Robert", "Church", ["Bob"]), ("王小明", "小明", "王", None), ("Mike McGee", "Michael", "McGee", None)]
    no = [("Christina Clifford", "Christopher", "Clifford", None), ("Jane Smith", "Jane", "Doe", None), ("王小红", "小明", "王", None),
          ("Dr. K. Jeyaprakash, Ph.D.", "K.", "D.", None), ("Joanna Devrieson", "Jo", "de Vries", None)]
    for full, f, l, v in yes:
        assert name_matches({"full_name": full}, f, l, v), (full, f, l, v)
    for full, f, l, v in no:
        assert not name_matches({"full_name": full}, f, l, v), (full, f, l, v)
    s = dict(seed(), **{"Name variants": "Pat Smith; Patricia Smith"})        # married name, from the seed column
    r = run([s], {("Pat Lee", "acme"): {"customer": "Acme Robotics", "ok": True, "candidates": [
        cand("Newco", "newco.com", "Head of Travel", [ACME_JOB, NEWCO], name="Patricia Smith")]}}, TODAY)[0]
    assert r["Same person?"] == "Confirmed", r

def test_real_jobs_are_not_side_seats_but_board_and_advisor_seats_are():
    from champion_tracker.matching import is_self_employed, is_side_role
    r = one([cand("Deloitte", "deloitte.com", "Director, Advisory Services", [ACME_JOB, {"company": "Deloitte", "website": "deloitte.com", "start": "2026-09-15"}])])
    assert r["Decision"] == "Card", r
    r = one([cand("Independent Bank", "independentbank.com", "VP Treasury", [ACME_JOB, {"company": "Independent Bank", "website": "independentbank.com", "start": "2026-09-15"}])])
    assert r["Decision"] == "Card", r
    for t in ["Board Member", "Chair, Regional Board", "Advisory Board Member", "Strategic Advisor", "Advisor", "Trustee",
              "Volunteer Mentor", "Brand Ambassador", "Board Director", "Member of the Board of Directors"]:
        assert is_side_role(t), t
    for t in ["Director, Advisory Services", "Financial Advisor", "VP Treasury", "Chairman & CEO", "Head of Travel"]:
        assert not is_side_role(t), t
    assert is_self_employed("Self-employed") and is_self_employed("Freelance") and is_self_employed("Independent Consultant")
    assert is_self_employed("Acme", "Freelance Designer") and not is_self_employed("Independent Bank", "VP Treasury")
    assert not is_self_employed("Fusion Consulting", "Consultant")

def test_month_only_start_is_aged_from_the_end_of_its_month():
    from champion_tracker.matching import date_range
    assert date_range("2026-07") == (date(2026, 7, 1), date(2026, 7, 31), "month")
    assert date_range("2024-02")[1] == date(2024, 2, 29) and date_range("2026")[2] == "year" and date_range("2026-07-15")[2] == "day"
    r = one([cand("Newco", "newco.com", "Head of Travel", [dict(ACME_JOB, end="2026-06"), {"company": "Newco", "website": "newco.com", "start": "2026-07"}])])
    assert r["Decision"] == "Review" and r["Move age (days)"] == 79, r
    assert "check: start date known only to the month (2026-07)" in r["Reason"], r["Reason"]
    r = one([cand("Newco", "newco.com", "Head of Travel", [ACME_JOB, {"company": "Newco", "website": "newco.com", "start": "2026-09"}])])
    assert r["Decision"] == "Card" and r["Move age (days)"] == 18, r              # fresh even on the 1st: no check
    r = one([cand("Newco", "newco.com", "Head of Travel", [dict(ACME_JOB, end="2026-03-01"), {"company": "Newco", "website": "newco.com", "start": "2026-04"}])])
    assert "move is 171 days old" in r["Reason"] and "known only" not in r["Reason"], r["Reason"]

def test_future_start_date_is_held():
    r = one([cand("Newco", "newco.com", "Head of Travel", [ACME_JOB, {"company": "Newco", "website": "newco.com", "start": "2026-12"}])])
    assert r["Decision"] == "Hold" and "start date is in the future" in r["Reason"] and r["Move age (days)"] == "", r

def test_live_adapter_restores_month_precision():
    assert people_data.month_only("2026-07-01") == "2026-07" and people_data.month_only("2026-07-15") == "2026-07-15"
    assert people_data.month_only(None) is None
    exp = people_data._slim_experience('[{"company": {"name": "Newco"}, "start_date": "2026-07-01", "end_date": null}]')
    assert exp[0]["start"] == "2026-07", exp

def test_product_activity_after_leaving_is_held():
    newco = [dict(ACME_JOB, end="2026-06-01"), {"company": "Newco", "website": "newco.com", "start": "2026-09-15"}]
    s = dict(seed(), **{"Last activity": "2026-09-20"})                          # 111 days after leaving (grace 60)
    r = run([s], {("Pat Lee", "acme"): {"customer": "Acme Robotics", "ok": True, "candidates": [cand("Newco", "newco.com", "Head of Travel", newco)]}}, TODAY)[0]
    assert r["Decision"] == "Hold" and r["Reason"].startswith("Still active in the product at Acme Robotics"), r["Reason"]
    s = dict(seed(), **{"Last activity": "2026-05-20"})                          # before leaving
    r = run([s], {("Pat Lee", "acme"): {"customer": "Acme Robotics", "ok": True, "candidates": [cand("Newco", "newco.com", "Head of Travel", newco)]}}, TODAY)[0]
    assert r["Decision"] == "Card", r

def test_clay_style_size_bands_count_as_small():
    from champion_tracker.matching import normalize_size_band
    from champion_tracker.adapters.clay_export import lookups_from_csv
    assert [normalize_size_band(x) for x in ["11-50", "2-10", "1-10", "51-200", "11 - 50 employees", "11 to 50", "10,001+", ""]] == \
        ["11 to 50", "2 to 10", "1 to 10", "51 to 200", "11 to 50", "11 to 50", "10001+", ""]
    r = one([cand("Tinyco", "tiny.co", "Head of Travel", [ACME_JOB, {"company": "Tinyco", "website": "tiny.co", "start": "2026-09-15"}], size="11-50")])
    assert r["Decision"] == "Review" and "small (11 to 50 staff)" in r["Reason"], r
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "clay.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("Full Name,Customer,Current Company,Company Size\nPat Lee,Acme Robotics,Tinyco,11 - 50 employees\n")
        lk = lookups_from_csv(path, [seed()])
        assert lk[("Pat Lee", "acme")]["candidates"][0]["employee_count_range"] == "11 to 50", lk

def test_owns_program_contract_says_one_thing():
    defs = open(os.path.join(ROOT, "contracts", "champion_definition.yaml"), encoding="utf-8").read()
    line = next(l for l in defs.splitlines() if l.strip().startswith("owns_program:"))
    assert "verbatim sentence" in line and "Never a title alone" in line and "accepted" not in line, line
    assert "title-only ownership is marked weak" not in defs and "program-owner title next to a quote" not in defs
    text = open(os.path.join(ROOT, "contracts", "context_contract.yaml"), encoding="utf-8").read()
    assert int(re.search(r"evidence_grace_days:\s*(\d+)", text).group(1)) == C.EVIDENCE_GRACE_DAYS
    assert int(re.search(r"search_depth:\s*(\d+)", text).group(1)) == C.SEARCH_DEPTH



# ---- 8. final fix round: red team v2 and code review (each reproduced first, then fixed) -------------------------
def _one_for(customer, cands, **seed_kw):
    s = dict(seed(**seed_kw), Customer=customer)
    return run([s], {("Pat Lee", "acme"): {"customer": customer, "ok": True, "candidates": cands}}, TODAY)[0]

def test_geography_never_leaves_a_generic_or_short_head():
    """Red team C8: Air Canada = Air India = 'Air Asia' -> ['air']; Virgin Australia = Virgin America; Union Pacific = Union."""
    from champion_tracker.matching import company_tokens, mentions_company, same_company
    for a, b in [("Air Canada", "Air India"), ("Air Asia", "Air India"), ("Air Canada", "Air Asia"),
                 ("Virgin Australia", "Virgin America"), ("Union Pacific", "Union"), ("Canon USA", "Canon Europe"),
                 ("Delta USA", "Delta"), ("First National", "First"), ("Virgin America", "Virgin USA")]:
        assert not same_company(a, b), (a, b)
    for a, b in [("Air Canada Inc.", "Air Canada"), ("Air Asia", "AirAsia"), ("Cathay Pacific", "Cathay"),
                 ("Contoso North America Inc", "Contoso"), ("Acme USA, LLC", "Acme North America Inc"), ("Deloitte India", "Deloitte")]:
        assert same_company(a, b), (a, b)
    assert company_tokens("Air Canada") == ["air", "canada"] and company_tokens("Union Pacific") == ["union", "pacific"]
    # headline ties: a generic head never ties alone
    assert not mentions_company("Senior Manager at Air India", "Air Canada")
    assert not mentions_company("ex-Air India, now freelance", "Air Canada")
    assert not mentions_company("Analyst @Union Pacific", "Union")
    assert mentions_company("Head of Travel | ex-Air Canada", "Air Canada") and mentions_company("Ops | ex-Acme", "Acme Robotics")
    assert mentions_company("Head of Travel at Acme | ex-Brex", "Acme") and mentions_company("formerly at Acme Inc", "Acme")
    # end to end: Air Canada -> Air India is a move, never "Still at Air Canada"
    job = [{"company": "Air Canada", "website": "aircanada.com", "start": "2018-01-01", "end": "2026-08-31"},
           {"company": "Air India", "website": "airindia.com", "start": "2026-09-15"}]
    r = _one_for("Air Canada", [cand("Air India", "airindia.com", "Head of Travel", job)])
    assert r["Moved?"] == "Yes" and r["Decision"] == "Card" and r["Play"] == C.PLAYS["P1"], r
    # a namesake tied only by a headline naming the other airline is not the champion
    r = _one_for("Air Canada", [cand("Air India", "airindia.com", "Senior Manager", headline="Senior Manager at Air India")])
    assert r["Same person?"] != "Confirmed" and r["Decision"] == "Hold", r

def test_a_lookalike_website_is_never_learned_as_the_customers_domain():
    """Code review 4: a champion who moves from Ramp (ramp.com) to Ramp Network (ramp.network) read 'Still at Ramp'."""
    from champion_tracker.rules import learn_customer_domains
    jane = {"Name": "Pat Lee", "First": "Pat", "Last": "Lee", "Customer": "Ramp", "Slug": "acme", "Title on page": "Head of Finance Ops",
            "Action tag": "chose", "Evidence (verbatim)": "Pat chose it", "Story URL": "https://example.com/s"}
    mover = cand("Ramp Network", "https://ramp.network", "VP Finance", [{"company": "Ramp", "website": "ramp.com", "start": "2020-01", "end": "2026-08"},
                 {"company": "Ramp Network", "website": "ramp.network", "start": "2026-09-15"}])
    lk = {("Pat Lee", "acme"): {"customer": "Ramp", "ok": True, "candidates": [mover]}}
    assert learn_customer_domains(lk.values()) == {"Ramp": {"ramp.com"}}
    r = run([jane], lk, TODAY)[0]
    assert r["Moved?"] == "Yes" and r["Decision"] == "Card" and r["New domain"] == "ramp.network", r
    # the CRM website on the seed is the customer's own domain: a named-for domain that is not it is never learned,
    # even when no job under the exact name has a website
    bare = cand("Ramp Network", "ramp.network", "VP Finance", [{"company": "Ramp", "start": "2020-01", "end": "2026-08"},
                {"company": "Ramp Network", "website": "ramp.network", "start": "2026-09-15"}])
    lk = {("Pat Lee", "acme"): {"customer": "Ramp", "ok": True, "candidates": [bare]}}
    r = run([dict(jane, **{"Customer domain": "https://www.ramp.com/"})], lk, TODAY)[0]
    assert r["Moved?"] == "Yes" and r["Decision"] == "Card", r
    assert learn_customer_domains(lk.values(), None, {"Ramp": {"ramp.com"}}) == {"Ramp": {"ramp.com"}}
    # a named-for domain that IS a known customer domain still counts ('Ryder System, Inc.' at ryder.com)
    ry = {("Pat Lee", "acme"): {"customer": "Ryder", "ok": True, "candidates": [
        cand("Ryder System, Inc.", "ryder.com", "SVP", [{"company": "Ryder System, Inc.", "website": "ryder.com", "start": "2015-01-01"}]),
        cand("Ryder", "", "Driver", [{"company": "Ryder", "website": "ryder.co.uk", "start": "2015-01-01"}])]}}
    assert learn_customer_domains(ry.values(), None, None, {"ryder.com"}) == {"Ryder": {"ryder.co.uk", "ryder.com"}}
    assert learn_customer_domains(ry.values()) == {"Ryder": {"ryder.co.uk"}}

def test_a_last_expense_report_after_the_last_day_keeps_the_card():
    """Red team C9: product use 4 days after the end date put both demo Cards on Hold; fatal for an expense product."""
    from datetime import timedelta
    from champion_tracker.matching import date_range
    s = load_seed(os.path.join(ROOT, "fixtures", "champions.csv"))
    lookups, aliases, known = load_lookups(os.path.join(ROOT, "fixtures", "candidates.json"))
    base = {r["Person"]: r for r in run(s, lookups, TODAY, aliases, known)}
    cards = sorted(p for p, r in base.items() if r["Decision"] == "Card")
    assert cards == ["Ben Ortiz", "Dana Reyes"], cards
    for name in cards:
        after = (date_range(base[name]["Left on"])[1] + timedelta(days=4)).isoformat()
        s2 = [dict(x, **{"Last activity": after}) if x["Name"] == name else x for x in s]
        r = {x["Person"]: x for x in run(s2, lookups, TODAY, aliases, known)}[name]
        assert r["Decision"] == "Card", (name, after, r["Reason"])
    assert C.ACTIVITY_AFTER_LEAVING_GRACE_DAYS == 60
    newco = [dict(ACME_JOB, end="2026-06-01"), {"company": "Newco", "website": "newco.com", "start": "2026-09-15"}]
    def dec(act):
        return run([dict(seed(), **{"Last activity": act})], {("Pat Lee", "acme"): {"customer": "Acme Robotics", "ok": True,
                   "candidates": [cand("Newco", "newco.com", "Head of Travel", newco)]}}, TODAY)[0]
    assert dec("2026-07-31")["Decision"] == "Card"                     # day 60 after leaving: still the grace
    r = dec("2026-08-01")                                              # day 61: records disagree
    assert r["Decision"] == "Hold" and "more than 60 days after" in r["Reason"], r["Reason"]

def test_compound_surnames_apostrophes_spaces_accents_and_transliterations():
    """Red team C10 (names): Pérez Rodríguez vs Pérez, O'Brien vs OBrien, Mc Donald vs McDonald, Mohammed vs Mohamed."""
    from champion_tracker.matching import name_matches
    yes = [("Juan Pérez", "Juan Carlos", "Pérez Rodríguez"), ("Juan Carlos Pérez", "Juan Carlos", "Pérez Rodríguez"),
           ("Juan Perez Rodriguez", "Juan", "Pérez"), ("Mary Smith", "Mary", "Smith-Jones"), ("Sean OBrien", "Sean", "O'Brien"),
           ("Sean O'Brien", "Sean", "OBrien"), ("Ian Mc Donald", "Ian", "McDonald"), ("Ian McDonald", "Ian", "Mc Donald"),
           ("José Núñez", "Jose", "Nunez"), ("Søren Kierkegaard", "Soren", "Kierkegaard"), ("Łukasz Wróbel", "Lukasz", "Wrobel"),
           ("Mohammed Al-Rashid", "Mohamed", "Al Rashid"), ("Muhammad Khan", "Mohammad", "Khan"), ("Mohamed Ali", "Muhammed", "Ali"),
           ("Gabriel García", "Gabriel", "García Márquez")]
    no = [("Maria De", "Maria", "De la Cruz"), ("Mohammed Ali", "Ahmed", "Ali"), ("Christina Clifford", "Christopher", "Clifford"),
          ("Joanna Devrieson", "Jo", "de Vries"), ("Juan Rodriguez", "Juan", "Perez"), ("Sean Bryan", "Sean", "O'Brien")]
    for full, f, l in yes:
        assert name_matches({"full_name": full}, f, l), (full, f, l)
    for full, f, l in no:
        assert not name_matches({"full_name": full}, f, l), (full, f, l)

def test_staff_jobs_named_after_side_roles_are_real_moves():
    """Red team C12: Senior / Principal Advisor, Volunteer Coordinator Manager, Brand Ambassador Program Manager and
    Trustee Services Officer were treated as side seats."""
    from champion_tracker.matching import is_side_role
    for t in ["Senior Advisor", "Principal Advisor", "Volunteer Coordinator Manager", "Brand Ambassador Program Manager",
              "Trustee Services Officer", "Mentor Program Lead", "Head of Volunteer Programs", "Policy Advisor", "Financial Adviser"]:
        assert not is_side_role(t), t
    for t in ["Board Advisor", "Advisor to the CEO", "Advisor to the Founders", "Investor and Advisor", "Angel Investor & Advisor",
              "Strategic Advisor", "Advisor", "Volunteer", "Mentor", "Brand Ambassador", "Trustee", "Member, Advisory Board"]:
        assert is_side_role(t), t
    assert is_side_role("Senior Advisor", job_type="advisor") and is_side_role("Coordinator", job_type="Board Member")
    for title in ["Senior Advisor", "Principal Advisor", "Volunteer Coordinator Manager"]:
        r = one([cand("Accenture", "accenture.com", title, [ACME_JOB, {"company": "Accenture", "website": "accenture.com", "start": "2026-09-15"}])])
        assert r["Decision"] == "Card" and r["Moved?"] == "Yes", (title, r["Reason"])
    r = one([cand("Accenture", "accenture.com", "Senior Advisor", [ACME_JOB, {"company": "Accenture", "website": "accenture.com",
                  "start": "2026-09-15", "job_type": "advisor"}])])
    assert r["Decision"] == "Hold" and "side seat" in r["Reason"], r

def test_fractional_or_interim_seat_is_a_real_move_sent_to_review():
    for title in ["Fractional CFO", "Interim Head of Finance"]:
        r = one([cand("Newco", "newco.com", title, [ACME_JOB, {"company": "Newco", "website": "newco.com", "start": "2026-09-15"}])])
        assert r["Moved?"] == "Yes" and r["Decision"] == "Review" and "fractional or interim role" in r["Reason"], (title, r)

def test_profile_urls_are_normalised_so_a_raw_reject_meets_a_pasted_link():
    """Code review 8: the reject held the provider's raw URL, the paste was normalised, so the reject never matched."""
    from champion_tracker.matching import normalize_profile_url as norm
    from champion_tracker.rules import candidate_key
    assert norm("https://linkedin.com/in/Jane-Roe/") == "https://www.linkedin.com/in/jane-roe"
    assert norm("uk.linkedin.com/in/jos%C3%A9-p?trk=x#about") == "https://www.linkedin.com/in/josé-p"
    assert norm(" http://www.linkedin.com/in/jane-roe/details/experience/ ") == "https://www.linkedin.com/in/jane-roe"
    assert norm("HTTPS://Example.COM/Team/Zed/?q=1#x") == "https://example.com/Team/Zed"
    assert norm("Pat Lee @ Newco") == "Pat Lee @ Newco" and norm("") == "" and norm(None) == ""
    assert candidate_key({"linkedin_url": "https://linkedin.com/in/Pat-Lee/"}) == "https://www.linkedin.com/in/pat-lee"
    assert candidate_key({"full_name": "Pat Lee", "current_company_name": "Newco"}) == "Pat Lee @ Newco"
    raw = "https://linkedin.com/in/Pat-Lee/"
    c = cand("Newco", "newco.com", "Head of Travel", [ACME_JOB, NEWCO], linkedin_url=raw)
    other = cand("Target", "target.com", "Cashier", linkedin_url="https://www.linkedin.com/in/pat-lee-2")
    def go(override):
        s = dict(seed(), **{"Row id": "003A"})
        return run([s], {("Pat Lee", "acme"): {"customer": "Acme Robotics", "ok": True, "candidates": [c, other]}}, TODAY,
                   overrides={"003A": override})[0]
    r = go({"reject": ["https://www.linkedin.com/in/pat-lee"]})          # a pasted-style reject removes the raw profile
    assert r["Decision"] == "Hold" and r["Candidates (same name)"] == 1, r
    r = go({"reject": [], "confirm": "linkedin.com/in/PAT-LEE"})           # a confirm in any spelling confirms it
    assert r["Tied by"] == "confirmed by a reviewer" and r["Decision"] == "Card", r
    r = go({"reject": [raw], "confirm": "https://www.linkedin.com/in/pat-lee"})   # both at once: never silently pick one
    assert r["Decision"] == "Hold" and "disagree" in r["Reason"], r

def test_move_rows_carry_the_new_accounts_icp_fields():
    r = one([cand("Newco", "newco.com", "Head of Travel", [ACME_JOB, NEWCO], size="1,001-5,000", main_industry="Software",
                  current_location="Austin, Texas, United States")])
    assert (r["New company size"], r["New company industry"], r["New company country"]) == ("1001 to 5000", "Software", "United States"), r
    r = one([cand("Newco", "newco.com", "Head of Travel", [ACME_JOB, NEWCO], size="", country="Canada")])
    assert (r["New company size"], r["New company industry"], r["New company country"]) == ("", "", "Canada"), r
    r = one([cand("Newco", "newco.com", "Head of Travel", [ACME_JOB, NEWCO], change="2026-09-01")], tag="quoted")
    assert r["Decision"] == "No action" and "New company size" in r, r       # every move row has the fields
    from champion_tracker.adapters.clay_export import lookups_from_csv       # the Clay path carries them too
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "clay.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("Full Name,Customer,Current Company,Company Size,Company Industry,Location\n"
                    'Pat Lee,Acme Robotics,Newco,51-200,Software,"Leeds, England, United Kingdom"\n')
        c = lookups_from_csv(path, [seed()])[("Pat Lee", "acme")]["candidates"][0]
    from champion_tracker.rules import icp_fields
    assert icp_fields(c) == {"New company size": "51 to 200", "New company industry": "Software",
                             "New company country": "United Kingdom"}, icp_fields(c)

def _http_error(code, retry_after=None):
    import email.message
    import urllib.error
    h = email.message.Message()
    if retry_after is not None:
        h["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://people.example/search", code, "x", h, None)

def _with_urlopen(responses, fn):
    """Run fn with people_data's HTTP layer faked: responses are consumed in order (an exception is raised, a dict is
    returned as JSON). Returns (result or exception, calls, sleeps)."""
    import io
    import urllib.request
    calls, sleeps = [], []
    def fake_urlopen(req, timeout=None):
        calls.append(json.loads(req.data))
        x = responses.pop(0)
        if isinstance(x, BaseException):
            raise x
        return io.BytesIO(json.dumps(x).encode())
    saved = (urllib.request.urlopen, people_data._sleep, people_data.TOKEN, people_data.LIMITER)
    urllib.request.urlopen, people_data._sleep, people_data.TOKEN = fake_urlopen, sleeps.append, "t"
    people_data.LIMITER = people_data.RateLimiter(0)
    try:
        try:
            out = fn()
        except Exception as e:  # noqa: BLE001
            out = e
        return out, calls, sleeps
    finally:
        urllib.request.urlopen, people_data._sleep, people_data.TOKEN, people_data.LIMITER = saved

def test_people_api_honours_retry_after_and_never_retries_other_4xx():
    """Code review 15: _post ignored Retry-After and retried 400/404/422 with fixed sleeps."""
    post = lambda: people_data._post("https://people.example/search", {"q": 1})  # noqa: E731
    out, calls, sleeps = _with_urlopen([_http_error(429, 7), {"contacts": []}], post)
    assert out == {"contacts": []} and len(calls) == 2 and sleeps == [7.0], (out, sleeps)
    for code in (400, 404, 422):
        out, calls, sleeps = _with_urlopen([_http_error(code), {"contacts": []}], post)
        assert isinstance(out, people_data.LookupError_) and len(calls) == 1 and sleeps == [], (code, out, calls, sleeps)
    out, calls, sleeps = _with_urlopen([_http_error(503), _http_error(408), {"ok": 1}], post)
    assert out == {"ok": 1} and sleeps == [4.0, 8.0], (out, sleeps)
    import urllib.error
    out, calls, sleeps = _with_urlopen([urllib.error.URLError("reset"), {"ok": 1}], post)
    assert out == {"ok": 1} and sleeps == [4.0], (out, sleeps)
    out, calls, sleeps = _with_urlopen([_http_error(429, 9999)] * 4, post)
    assert isinstance(out, people_data.LookupError_) and len(calls) == 4 and sleeps == [120.0] * 3, (out, sleeps)
    out, calls, sleeps = _with_urlopen([_http_error(401)], post)
    assert isinstance(out, people_data.LookupError_) and "auth" in str(out) and len(calls) == 1

def test_one_rate_limiter_is_shared_by_every_lookup():
    clock = [0.0]
    slept = []
    def sleep(x):
        slept.append(x)
        clock[0] += x
    lim = people_data.RateLimiter(2, clock=lambda: clock[0], sleep=sleep)
    for _ in range(6):
        lim.acquire()
    assert abs(sum(slept) - 2.0) < 1e-9 and abs(clock[0] - 2.0) < 1e-9, slept     # 2 free, then 2 a second
    assert people_data.RateLimiter(0).acquire() == 0.0
    import threading
    n = []
    counting = type("L", (), {"acquire": lambda self: n.append(threading.get_ident())})()
    saved = people_data.LIMITER
    people_data.LIMITER = counting
    try:
        import io
        import urllib.request
        old = (urllib.request.urlopen, people_data.TOKEN)
        urllib.request.urlopen, people_data.TOKEN = (lambda req, timeout=None: io.BytesIO(b'{"contacts": []}')), "t"
        try:
            people_data._post("https://people.example/search", {})
            people_data._post("https://people.example/match", {})
        finally:
            urllib.request.urlopen, people_data.TOKEN = old
    finally:
        people_data.LIMITER = saved
    assert len(n) == 2, n
    assert isinstance(people_data.LIMITER, people_data.RateLimiter) and people_data.LIMITER.rate == float(os.environ.get("PEOPLE_RATE_PER_SEC", "5"))

def test_every_lookup_record_carries_depth_and_capped():
    tied = {"full_name": "Pat Lee", "current_employer": "Acme Robotics", "experience_json": "[]"}
    def fake(url, body, tries=4):
        return {"contacts": [dict(tied)]} if url == "M" else {"contacts": []}
    r = _with_fake(fake, lambda: people_data.lookup_record(seed()))
    assert r["source"] == "match" and r["depth"] == 50 and r["capped"] is False, r
    def broken(url, body, tries=4):
        raise people_data.LookupError_("provider refused the request (400); not retried")
    r = _with_fake(broken, lambda: people_data.lookup_record(seed(), depth=25))
    assert r["ok"] is False and r["depth"] == 25 and r["capped"] is False and "error" in r, r

def test_a_work_history_filter_is_used_only_when_the_provider_has_one():
    """Red team C1: a company_name filter means the CURRENT employer, so it can never find a mover. The work-history
    filter is sent only when PEOPLE_PAST_COMPANY_FIELD names one; by default the full same-name pull does the work."""
    calls = []
    mover = {"full_name": "Pat Lee", "current_employer": "Newco", "linkedin_url": "li/mover",
             "experience_json": json.dumps([{"company": {"name": "Acme Robotics"}, "start_date": "2019-01-01", "end_date": "2026-08-01"},
                                            {"company": {"name": "Newco"}, "start_date": "2026-09-01"}])}
    assert people_data.PAST_COMPANY_FIELD == ""                              # off by default
    saved = people_data.PAST_COMPANY_FIELD
    people_data.PAST_COMPANY_FIELD = "past_company_name"
    try:
        r = _with_fake(_fake_provider(_namesakes(50), [], calls, past=[mover]), lambda: people_data.lookup_record(seed()))
    finally:
        people_data.PAST_COMPANY_FIELD = saved
    assert ("S", 50, None, ("past", "Acme Robotics")) in calls and not any(c[2] for c in calls if c[0] == "S"), calls
    assert any(c.get("linkedin_url") == "li/mover" for c in r["candidates"])
    row = run([seed()], {("Pat Lee", "acme"): r}, TODAY)[0]
    assert row["Same person?"] == "Confirmed" and row["Moved?"] == "Yes", row
    assert "work history" in people_data.__doc__ and "PEOPLE_PAST_COMPANY_FIELD" in people_data.__doc__

def test_a_capped_search_pulls_every_namesake_and_ties_locally():
    """Most people APIs cannot filter on past employers; the full same-name pull is what resolved 24 of 37 real rows."""
    full = [cand("Other Co", "other.com", "Analyst", name="Pat Lee") for _ in range(50)]
    mover = cand("Newco", "newco.com", "Director", exp=[dict(ACME_JOB), {"company": "Newco", "website": "newco.com",
                 "start": "2026-08-01", "end": None}], name="Pat Lee", linkedin_url="https://www.linkedin.com/in/pat-lee-9")
    calls = []
    def fake(url, body, tries=4):
        calls.append(body.get("limit"))
        if body.get("limit") == 50:
            return {"contacts": [dict(c, experience_json=[]) for c in full]}
        return {"contacts": [dict(c, experience_json=[]) for c in full] + [dict(mover, experience_json=[
            {"company": {"name": "Acme Robotics", "website": "acme.com"}, "start_date": "2020-01-01", "end_date": "2026-07-01"},
            {"company": {"name": "Newco", "website": "newco.com"}, "start_date": "2026-08-01", "end_date": None}])]}
    saved = (people_data._post, people_data.SEARCH_URL, people_data.MATCH_URL)
    people_data._post, people_data.SEARCH_URL, people_data.MATCH_URL = fake, "S", ""
    try:
        r = people_data.lookup_record(seed())
    finally:
        people_data._post, people_data.SEARCH_URL, people_data.MATCH_URL = saved
    assert calls == [50, people_data.DEEP_LIMIT] and r["capped"] and r["full_search"] and r["searched"] == 51, (calls, r.get("searched"))
    assert r["source"] == "search+full_pull" and any(c.get("linkedin_url", "").endswith("pat-lee-9") for c in r["candidates"])

def test_after_a_full_search_the_hold_says_nothing_is_further_down():
    s = seed()
    rec = {"customer": "Acme Robotics", "ok": True, "capped": True, "full_search": True, "searched": 812,
           "candidates": [cand("Target", "target.com", "Cashier")]}
    r = run([s], {("Pat Lee", "acme"): rec}, TODAY)[0]
    assert r["Decision"] == "Hold" and r["Reason"].startswith("A full search of 812 same-name profiles found none tied"), r["Reason"]

def test_a_profile_that_still_names_the_customer_after_the_job_ended_is_held():
    """Real row: the only customer job ended in 2026-07 but the profile's current-company field still says the customer."""
    c = cand("Acme Robotics", "acme.com", "Ops Manager", exp=[dict(ACME_JOB, end="2026-07-01")])
    r = one([c])
    assert r["Decision"] == "Hold" and "job ended 2026-07-01; current employer unknown" in r["Reason"], r


def test_one_linkedin_member_under_an_old_and_a_new_url_is_one_person():
    """Real row: the same member id (…-4a2548232) under an old and a new slug; the old copy still names the customer."""
    old = cand("Acme Robotics", "acme.com", "Travel Manager", exp=[dict(ACME_JOB)], updated="2025-01-10",
               linkedin_url="https://www.linkedin.com/in/pat-lee-4a2548232")
    new = cand("Newco", "newco.com", "Head of Travel", exp=[dict(ACME_JOB), {"company": "Newco", "website": "newco.com",
               "start": "2026-09-01", "end": None}], updated="2026-09-20",
               linkedin_url="https://www.linkedin.com/in/patlee-travel-4a2548232/")
    r = one([old, new])
    assert r["Same person?"] == "Confirmed" and r["Moved?"] == "Yes" and r["New company"] == "Newco", r
    other = dict(new, linkedin_url="https://www.linkedin.com/in/pat-lee-99bb77cc1")
    assert one([old, other])["Decision"] == "Hold"          # different member ids that disagree still hold


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

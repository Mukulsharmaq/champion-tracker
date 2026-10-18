"""Tests for the GTM brief, the pipeline surface, the connectors, the one-command pipeline and the Slack feedback loop.
Run without pytest:  python3 tests/test_system.py      (pytest also works)
"""
import copy
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from champion_tracker.adapters.fixtures import load_lookups, load_seed  # noqa: E402
from pg_surface.slack_out import list_rows  # noqa: E402
from champion_tracker.rules import run as run_rules  # noqa: E402
from gtm_connectors import MissingConfig, gong, salesforce, slack_notes, slack_post, warehouse  # noqa: E402
from gtm_briefs.brief import build_brief, to_markdown  # noqa: E402
from gtm_briefs.extract import classify, extract  # noqa: E402
from pg_surface.plays import PLAYS  # noqa: E402
from pg_surface.slack_out import canvas_markdown, digest_blocks, esc, ops_blocks, ops_markdown  # noqa: E402
from pg_surface.surface import build_cards  # noqa: E402
from pg_system import monitor  # noqa: E402
from pg_system.feedback_loop import check_regressions, handle_click, replace_actions, replay_case  # noqa: E402
from pg_system.pipeline import run as run_pipeline  # noqa: E402

TODAY = date(2026, 10, 18)
PERIOD, PREVIOUS = ("2026-10-12", "2026-10-18"), ("2026-10-05", "2026-10-11")
FX = lambda *p: os.path.join(ROOT, "fixtures", *p)  # noqa: E731


def _csv(*p):
    with open(FX(*p), encoding="utf-8") as f:
        return list(csv.DictReader(f))


SIGNALS, ACCOUNTS = _csv("briefs", "signals.csv"), _csv("briefs", "accounts.csv")
FINDINGS = extract(SIGNALS)
LOOKUPS, ALIASES, KNOWN = load_lookups(FX("candidates.json"))
ROWS = run_rules(load_seed(FX("champions.csv")), LOOKUPS, TODAY, ALIASES, KNOWN)
NOW = [f for f in FINDINGS if PERIOD[0] <= f["date"] <= PERIOD[1]]
CARDS = build_cards(ACCOUNTS, ROWS, NOW, TODAY)
BY = {c["account"]: c for c in CARDS}


def sig(text, side="customer", source="call"):
    return {"source": source, "id": "T-1", "date": "2026-10-15", "account": "Acme Robotics", "speaker": "X",
            "speaker_title": "Y", "side": side, "text": text, "url": "u"}


# ---- GTM brief: what counts -------------------------------------------------------------------------------------
def test_competitor_names_match_whole_words_only():
    assert classify(sig("We spend a lot of time on approvals."))["competitors"] == []
    assert classify(sig("We export everything to the general ledger."))["competitors"] == []
    assert classify(sig("Ledgerly's quote came in lower."))["competitors"] == ["Ledgerly"]

def test_price_is_not_friction_when_they_say_it_is_fine():
    c = classify(sig("Honestly, the price was never the issue."))
    assert "pricing_friction" not in c["themes"] and c["held_reason"], c

def test_our_own_speculation_never_counts_and_rep_reports_are_labelled():
    assert classify(sig("I think they'll say our price is too high.", side="internal"))["confidence"] == "excluded"
    assert classify(sig("Champion says the CFO pushed back on price.", side="internal", source="crm"))["confidence"] == "reported"

def test_competitor_stance_risk_vs_win():
    assert classify(sig("We have a demo with TripStack next week."))["stance"] == "risk"
    assert classify(sig("We moved off TripStack last year."))["stance"] == "win"

def test_brief_says_what_changed_with_numbers():
    b = build_brief(FINDINGS, PERIOD, PREVIOUS)
    led = next(c for c in b["competitors"] if c["name"] == "Ledgerly")
    assert led["new"] and led["mentions"] == 2 and led["accounts"] == ["Globex Media", "Initech"], led
    assert any(line.startswith("Ledgerly: 2 mentions at 2 accounts (new: none last period)") for line in b["what_changed"]), b["what_changed"]
    assert b["themes"]["pricing_friction"]["signals"] == 3 and b["themes"]["pricing_friction"]["repeats"] == 1
    assert b["themes"]["competitor"]["repeats"] == 2      # the rep notes repeating the Ledgerly and TripStack quotes

def test_every_brief_item_links_to_its_source_and_held_items_are_listed():
    md = to_markdown(build_brief(FINDINGS, PERIOD, PREVIOUS))
    items = [l for l in md.splitlines() if l.startswith('- "')]
    assert items and all("[source](" in l for l in items), items[:3]
    assert "## Held (did not count)" in md and "price was never the issue" in md


# ---- pipeline surface: which account, why now, who, which play --------------------------------------------------
def test_renewal_with_competitor_quote_is_top_and_gets_the_renewal_save():
    g = CARDS[0]
    assert g["account"] == "Globex Media" and g["play"] == "P4" and g["status"] == "Ready", g
    assert g["who"]["name"] == "Hannah Cole" and any("Renewal in 58 days" in w for w in g["why_now"])

def test_champion_moved_to_a_customer_gets_the_expansion_play():
    n = BY["Northwind Foods"]
    assert n["play"] == "P2" and n["who"]["name"] == "Ben Ortiz", n
    assert any("Seen here this week" in w for w in n["why_now"])

def test_champion_moved_to_a_prospect_gets_the_intro_play():
    assert BY["Contoso Systems"]["play"] == "P1" and BY["Contoso Systems"]["status"] == "Ready"

def test_old_move_needs_a_check_and_is_not_news_at_the_old_account():
    f = BY["Fabrikam"]
    assert f["status"] == "Check first" and "159 days" in f["checks"][0], f
    assert not any("Leo Park" in w for w in BY["Northwind Foods"]["why_now"]), BY["Northwind Foods"]["why_now"]

def test_champion_left_a_customer_means_re_anchor():
    a = BY["Acme Robotics"]
    assert a["play"] == "P9" and "name one" in a["who"]["why"], a

def test_report_only_signal_needs_a_customer_confirmation():
    assert BY["Litware"]["status"] == "Check first" and "confirm with the customer" in BY["Litware"]["checks"][0]

def test_each_quote_appears_once_per_card():
    for c in CARDS:
        quotes = re.findall(r'"([^"]+)"', " ".join(c["why_now"]))
        assert len(quotes) == len(set(quotes)), (c["account"], quotes)

def test_champion_at_a_company_not_in_the_crm_still_surfaces_with_a_check():
    row = dict(next(r for r in ROWS if r["Person"] == "Dana Reyes"), **{"New company": "Brand New Co", "New domain": "brandnew.co"})
    cards = build_cards(ACCOUNTS, [row], [], TODAY)
    c = next(c for c in cards if c["account"] == "Brand New Co")
    assert c["status"] == "Check first" and "not in the CRM" in c["checks"][0], c

def test_plays_come_from_the_contract_and_clay_uses_the_same_rules():
    import importlib.util
    from pg_surface.plays import CONTRACT, RULES
    assert set(PLAYS) == set(CONTRACT["plays"]) and all(r["play"] in PLAYS for r in RULES)
    assert all(f in CONTRACT["facts"] for r in RULES for f in r.get("all", []) + r.get("any", []))
    spec = importlib.util.spec_from_file_location("cf", os.path.join(ROOT, "scripts", "clay_formula.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.formula() in open(os.path.join(ROOT, "clay", "surface_formulas.md"), encoding="utf-8").read()


# ---- Slack delivery ---------------------------------------------------------------------------------------------
def test_digest_fits_slack_limits_and_every_card_has_its_buttons():
    d = digest_blocks(CARDS, PERIOD, footer=["queue"])
    assert len(d["blocks"]) <= 50
    acts = [b for b in d["blocks"] if b["type"] == "actions"]
    assert len(acts) == 6 and all(len(b["elements"]) in (5, 6) and b["block_id"] == f"pg:{b['elements'][0]['value'].split(':')[-1]}" for b in acts)
    wp = [e for b in acts for e in b["elements"] if e["action_id"] == "wrong_person"]
    assert wp and all(e["value"] for e in wp)          # only on cards whose contact is a matched champion
    for b in d["blocks"]:
        for t in [b.get("text", {}).get("text", "")] + [f["text"] for f in b.get("fields", [])]:
            assert len(t) <= 3000

def test_sellers_canvas_has_the_brief_and_next_steps_but_no_ops_queue():
    md = canvas_markdown("# brief\n", CARDS)
    assert "## Next steps" in md and "| 1 | Globex Media | High | P4 Renewal save" in md and "review queue" not in md.lower()
    ops = ops_markdown([r for r in ROWS if r["Decision"] == "Hold"], "RevOps", ["x"])
    assert "Champion review queue (6 rows, owner: RevOps)" in ops and "## Run health" in ops


# ---- connectors: real API shapes in, same answers out -----------------------------------------------------------
def _api_signals():
    acc = salesforce.load_accounts(FX("salesforce", "accounts.json"))
    notes, skipped = slack_notes.load(FX("slack", "field_notes_history.json"), FX("slack", "users_list.json"), acc)
    return acc, gong.load(FX("gong", "calls_extensive.json"), FX("gong", "calls_transcript.json")) + \
        salesforce.load_notes(FX("salesforce", "tasks.json")) + notes, skipped

def test_api_shaped_sources_give_the_same_findings_as_the_readable_csv():
    acc, sigs, _ = _api_signals()
    key = lambda f: (f["date"], f["account"], f["speaker"], f["theme"], f["text"], f["confidence"], f["stance"])  # noqa: E731
    assert sorted(map(key, extract(sigs))) == sorted(map(key, FINDINGS))
    assert [(a["account"], a["type"], a["owner"], a["renewal_date"]) for a in acc] == \
        [(a["account"], a["type"], a["owner"], a["renewal_date"]) for a in ACCOUNTS]

def test_gong_filler_turns_and_internal_speakers_are_parsed_but_never_count():
    sigs = gong.load(FX("gong", "calls_extensive.json"), FX("gong", "calls_transcript.json"))
    assert len(sigs) == 3 * 18 and {s["side"] for s in sigs} == {"customer", "internal"}
    fillers = [s for s in sigs if s["text"].startswith(("Thanks for", "Good to see", "That's helpful", "Got it", "Makes sense"))]
    assert fillers and extract(fillers) == []

def test_field_note_prefix_wins_and_notes_without_an_account_are_skipped():
    acc, sigs, skipped = _api_signals()
    dana = next(s for s in sigs if "Dana Reyes" in s["text"] and s["source"] == "field")
    assert dana["account"] == "Contoso Systems" and not dana["text"].startswith("Contoso"), dana
    assert len(skipped) == 1 and skipped[0]["reason"] == "No account named"

def test_warehouse_sql_drops_test_workspaces_lost_deals_and_internal_users():
    seed = warehouse.champions_sqlite(FX("warehouse", "demo_warehouse.sql"), "2026-10-18")
    names = {s["Name"] for s in seed}
    assert not names & {"Quinn Tester", "Noor Haddad", "Pat Vendor"} and len(seed) == 14, names
    a = {r["Person"]: r["Decision"] for r in run_rules(seed, LOOKUPS, TODAY, ALIASES, KNOWN)}
    assert a.pop("Owen Marsh") == "No action"           # the warehouse-only trap: role on the deal, no product use
    b = {r["Person"]: r["Decision"] for r in ROWS if r["Person"] != "Pat Vendor"}
    assert a == b, {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}

def test_live_mode_without_keys_fails_loudly_before_any_network_call():
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith(("GONG_", "SF_", "SLACK_", "SNOWFLAKE_", "PEOPLE_"))}
    try:
        for fn in (lambda: gong.fetch("a", "b"), salesforce.fetch_accounts, lambda: slack_post.update_message("c", "1", "t", [])):
            try:
                fn()
                raise AssertionError("expected MissingConfig")
            except MissingConfig as e:
                assert "environment variables" in str(e)
        with tempfile.TemporaryDirectory() as d:
            try:
                run_pipeline("live", TODAY, d, cfg={})
                raise AssertionError("expected MissingConfig")
            except MissingConfig as e:
                assert "GONG_ACCESS_KEY" in str(e) and "SNOWFLAKE_ACCOUNT" in str(e)
    finally:
        os.environ.update(saved)

def test_committed_fixtures_match_the_builder():
    import importlib.util
    rels = [("gong", "calls_extensive.json"), ("gong", "calls_transcript.json"), ("salesforce", "accounts.json"),
            ("salesforce", "tasks.json"), ("warehouse", "demo_warehouse.sql"), ("slack", "field_notes_history.json")]
    spec = importlib.util.spec_from_file_location("mf", os.path.join(ROOT, "scripts", "make_fixtures.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as d:
        mod.main(d)                                   # builds into a temp folder; the repo is never rewritten
        for r in rels:
            assert open(os.path.join(d, *r), "rb").read() == open(FX(*r), "rb").read(), r


# ---- one command, and run health --------------------------------------------------------------------------------
def test_demo_pipeline_writes_every_output():
    with tempfile.TemporaryDirectory() as d:
        s = run_pipeline("demo", TODAY, d, cfg={"review_owner": {"name": "RevOps"}})
        for f in ("decisions.csv", "brief.md", "surface.json", "next_steps.csv", "slack_digest.json", "canvas.md",
                  "slack_requests.json", "review_queue.csv", "summary.json"):
            assert os.path.getsize(os.path.join(d, f)) > 0, f
        reqs = json.load(open(os.path.join(d, "slack_requests.json")))
        methods = [r["method"] for r in reqs]
        # Slack Connect check first; digest and its thread (accounts 7-10); the review canvas, shared through the review
        # owner's DM channel; then the ops queue (which can say the share failed) and the QA sample
        assert methods == ["conversations.info", "canvases.create", "slackLists.create", "slackLists.access.set"] \
            + ["slackLists.items.create"] * 10 + ["chat.postMessage"] * 2 \
            + ["canvases.create", "conversations.open", "canvases.access.set"] + ["chat.postMessage"] * 2, methods
        assert reqs[1]["body"]["channel_id"] and reqs[3]["body"]["channel_ids"]
        schema = reqs[2]["body"]["schema"]
        assert sum(c.get("is_primary_column", False) for c in schema) == 1 and schema[0]["type"] == "text"
        item = reqs[4]["body"]["initial_fields"]
        assert item[0]["rich_text"][0]["elements"][0]["elements"][0]["text"] == "Globex Media" and {"select", "number", "link"} <= {k for f in item for k in f}
        assert reqs[1]["body"]["document_content"]["type"] == "markdown"
        assert reqs[-4]["body"]["users"] == "U_REVIEW_OWNER" and "channel_id" not in reqs[-5]["body"]
        assert reqs[-3]["body"]["channel_ids"] == ["<DM channel with U_REVIEW_OWNER>"] and "user_ids" not in reqs[-3]["body"]
        assert [r["body"]["text"] for r in reqs[-2:]] == ["Champion review queue", "Weekly QA sample"]
        assert s["cards"] == 10 and s["decisions"]["Card"] == 2 and s["slack"]["dry_run"]

def test_monitor_flags_a_jump_in_hold_reasons_and_a_silent_source():
    cur = {"hold_reasons": {"same name": 12}, "signals_by_source": {"call": 2, "crm": 5}, "field_notes_skipped": 0}
    prev = {"hold_reasons": {"same name": 4}, "signals_by_source": {"call": 40, "crm": 5}}
    a = " ".join(monitor.alerts(cur, prev))
    assert "'same name' jumped from 4 to 12" in a and "No field signals" in a and "call signals fell from 40 to 2" in a, a


# ---- Slack clicks: permission, write-back, corrections become tests --------------------------------------------
def _demo_run(d, cfg):
    run_pipeline("demo", TODAY, d, cfg=cfg)
    return json.load(open(os.path.join(d, "surface.json")))


def _val(d, target):
    return open(os.path.join(d, "replay", "LATEST")).read() + ":" + target

def test_owner_click_writes_back_and_anyone_else_is_a_comment():
    with tempfile.TemporaryDirectory() as d:
        cfg = {"owner_slack_ids": {"Riley Shaw": "U_RILEY"}, "regression_file": os.path.join(d, "reg.jsonl")}
        cards = _demo_run(d, cfg)
        g = next(c for c in cards if c["account"] == "Globex Media")
        log = os.path.join(d, "fb", "feedback.jsonl")
        r1 = handle_click("do_play", _val(d, g["id"]), "U_RILEY", os.path.join(d, "replay"), cfg, log_path=log, at="2026-10-18T14:05:00+00:00")
        assert r1["write_back"]["match_on"]["Id"].startswith("001DEMO") and r1["write_back"]["dry_run"], r1
        assert r1["write_back"]["fields"]["Pipeline_Play__c"] == "P4" and "CRM updated and a Task created (dry run)" in r1["message"]
        task = r1["write_back"]["task"]["fields"]
        assert task["WhatId"].startswith("001DEMO") and task["Subject"].startswith("[GTM Signal] P4") and task["ActivityDate"] == "2026-10-25"
        r2 = handle_click("do_play", _val(d, g["id"]), "U_SOMEONE", os.path.join(d, "replay"), cfg, log_path=log)
        assert r2["write_back"] == {"skipped": "not the account owner"} and "logged as a comment" in r2["message"]
        assert len(open(log).read().splitlines()) == 2

def test_wrong_play_becomes_a_replayable_test_case():
    with tempfile.TemporaryDirectory() as d:
        cfg = {"owner_slack_ids": {"Alex Kim": "U_ALEX"}, "regression_file": os.path.join(d, "reg.jsonl")}
        cards = _demo_run(d, cfg)
        t = next(c for c in cards if c["account"] == "Tailspin Toys")
        r = handle_click("wrong_play", _val(d, t["id"]), "U_ALEX", os.path.join(d, "replay"), cfg, note="They are a customer now",
                         log_path=os.path.join(d, "fb", "f.jsonl"))
        assert r["regression_case"] and "saved as a test case" in r["message"]
        case = check_regressions(cfg["regression_file"])[0]
        assert case["status"] == "open" and case["result"] == {"now": "P7", "passes": False}, case["result"]
        fixed = copy.deepcopy(case)
        fixed["replay"]["account"]["type"] = "customer"          # what a rule/data fix would change
        assert replay_case(fixed)["passes"]

def test_wrong_person_on_a_champion_card_replays_against_the_rules():
    with tempfile.TemporaryDirectory() as d:
        cfg = {"review_owner": {"slack_id": "U_OPS"}, "regression_file": os.path.join(d, "reg.jsonl")}
        _demo_run(d, cfg)
        run = open(os.path.join(d, "replay", "LATEST")).read()
        dana = next(f for f in os.listdir(os.path.join(d, "replay", run)) if f.startswith("champion_") and
                    json.load(open(os.path.join(d, "replay", run, f)))["row"]["Person"] == "Dana Reyes")
        rid = dana[len("champion_"):-5]
        r = handle_click("wrong_person", _val(d, rid), "U_OPS", os.path.join(d, "replay"), cfg, log_path=os.path.join(d, "f.jsonl"))
        assert r["write_back"]["fields"]["Champion_Move_Status__c"] == "Identity rejected", r
        case = check_regressions(cfg["regression_file"])[0]
        assert case["expect_at_most"] == "Hold" and case["result"] == {"now": "Card", "passes": False}

def test_card_from_an_older_run_is_refused_and_buttons_are_replaced_only_on_the_clicked_card():
    with tempfile.TemporaryDirectory() as d:
        assert not handle_click("do_play", "acct_gone.com", "U", d, {})["ok"]
        cfg = {"owner_slack_ids": {"Riley Shaw": "U_RILEY"}, "regression_file": os.path.join(d, "reg.jsonl")}
        run_pipeline("demo", TODAY, d, cfg=cfg)
        old = _val(d, "acct_globex.com")
        run_pipeline("demo", date(2026, 12, 20), d, cfg=cfg)          # a newer run replaces the old digest
        r = handle_click("do_play", old, "U_RILEY", os.path.join(d, "replay"), cfg, log_path=os.path.join(d, "f.jsonl"))
        assert not r["ok"] and "older run" in r["message"], r
    blocks = digest_blocks(CARDS, PERIOD)["blocks"]
    new = replace_actions(blocks, "pg:acct_globex.com", "done")
    assert len(new) == len(blocks) and sum(b["type"] == "actions" for b in new) == sum(b["type"] == "actions" for b in blocks) - 1

# ---- the review fixes, each locked in ---------------------------------------------------------------------------
def _acc(name, dom, typ="customer", crm="", renew=""):
    return {"account": name, "domain": dom, "type": typ, "owner": "O", "renewal_date": renew, "open_opportunity": "", "crm_id": crm}

def _f(account, theme, text="t", conf="direct", stance="", comps=(), sid="S1", day="2026-10-15"):
    return {"source": "call", "id": sid, "date": day, "account": account, "speaker": "X", "speaker_title": "Y", "side": "customer",
            "text": text, "url": "u", "theme": theme, "competitors": list(comps), "stance": stance, "confidence": conf}

def test_exact_account_name_beats_a_partial_match():
    accs = [_acc("Acme Robotics", "acme.com", renew="2026-11-30"), _acc("Acme Robotics Germany", "acme.de", "prospect")]
    cards = build_cards(accs, [], [_f("Acme Robotics Germany", "pricing_friction", "too expensive")], TODAY)
    assert [(c["account"], c["play"]) for c in cards] == [("Acme Robotics Germany", "P8")], cards

def test_two_crm_records_on_one_domain_get_two_cards_with_unique_ids():
    accs = [_acc("Globex Media", "globex.com", crm="001A"), _acc("Globex Studios", "globex.com", crm="001B")]
    f = [_f("Globex Media", "expansion", sid="S1"), _f("Globex Studios", "expansion", sid="S2")]
    ids = [c["id"] for c in build_cards(accs, [], f, TODAY)]
    assert sorted(ids) == ["acct_001A", "acct_001B"], ids

def test_nothing_actionable_means_no_card():
    accs = [_acc("Initech", "initech.com"), _acc("Litware", "litware.com", "prospect")]
    f = [_f("Initech", "competitor", stance="win", comps=["Ledgerly"]), _f("Litware", "expansion", sid="S2")]
    assert build_cards(accs, [], f, TODAY) == []

def test_a_recent_review_move_still_flags_the_old_customer():
    r = dict(next(x for x in ROWS if x["Person"] == "Dana Reyes"), Decision="Review",
             Reason="Confirmed champion move; check: new company is small (11 to 50 staff).")
    cards = {c["account"]: c for c in build_cards(ACCOUNTS, [r], [], TODAY)}
    assert cards["Acme Robotics"]["play"] == "P9" and cards["Contoso Systems"]["status"] == "Check first"

def test_card_leads_with_the_signal_that_triggered_its_play_and_explains_its_rank():
    g = BY["Globex Media"]
    assert g["why_now"][0].startswith("Renewal in 58 days") and "Ledgerly" in g["headline"], g["headline"]
    assert g["tier"] == "High" and g["rank_reason"].startswith("competitor risk 30")
    assert BY["Woodgrove Bank"]["why_now"][0].startswith("Expansion")

def test_no_contact_means_check_first():
    a = BY["Acme Robotics"]
    assert a["status"] == "Check first" and any("No one to contact" in k for k in a["checks"]), a["checks"]

def test_curly_apostrophes_do_not_turn_our_guess_into_evidence():
    assert classify(sig("They\u2019ll say our price is too high.", side="internal", source="crm"))["confidence"] == "excluded"

def test_rep_turns_on_calls_are_not_evidence_unless_they_repeat_the_customer():
    assert classify(sig("Our pricing is per seat, as I mentioned.", side="internal"))["confidence"] == "ignored"
    assert classify(sig("Just to recap, your CFO wants a discount before signing.", side="internal"))["confidence"] == "reported"

def test_rep_note_repeating_a_customer_quote_is_counted_once_and_quiet_competitors_are_reported():
    f = [_f("Globex Media", "competitor", comps=["Ledgerly"], stance="risk"),
         _f("Globex Media", "competitor", conf="reported", comps=["Ledgerly"], stance="risk", sid="S2"),
         _f("Initech", "competitor", comps=["Spendwise"], stance="risk", sid="S3", day="2026-10-07")]
    b = build_brief(f, PERIOD, PREVIOUS)
    led = next(c for c in b["competitors"] if c["name"] == "Ledgerly")
    assert led["mentions"] == 1 and b["themes"]["competitor"]["repeats"] == 1
    assert "Spendwise: no mentions this period (was 1)." in b["what_changed"], b["what_changed"]

def test_slack_text_is_escaped_and_long_lines_are_capped():
    assert esc("P&G <x>") == "P&amp;G &lt;x&gt;"
    many = [dict(CARDS[0], id=f"acct_{i}", account="A" * 40 + str(i), rank=i) for i in range(300)]
    d = digest_blocks(many, PERIOD)
    assert all(len(e.get("text", "")) <= 3000 for b in d["blocks"] for e in b.get("elements", []) if b["type"] == "context")

def test_snooze_dismiss_and_working_carry_over_to_the_next_run():
    from pg_system.state import apply
    c = {x["account"]: x for x in CARDS}
    ev = [{"card_id": c["Globex Media"]["id"], "allowed": True, "action": "snooze", "user": "U1", "at": "2026-10-16T10:00:00"},
          {"card_id": c["Initech"]["id"], "allowed": True, "action": "dismiss", "user": "U1", "at": "2026-10-16T10:00:00",
           "evidence": c["Initech"]["built_from"]["finding_ids"]},
          {"card_id": c["Umbrella Health"]["id"], "allowed": True, "action": "do_play", "user": "U1", "at": "2026-10-16T10:00:00"},
          {"card_id": c["Litware"]["id"], "allowed": False, "action": "dismiss", "user": "U9", "at": "2026-10-16T10:00:00", "evidence": []}]
    shown, hidden = apply([dict(x) for x in CARDS], ev, TODAY)
    assert {h["account"] for h in hidden} == {"Globex Media", "Initech"}
    assert next(x for x in shown if x["account"] == "Umbrella Health")["working"].startswith("Working since 2026-10-16")
    assert "Litware" in {x["account"] for x in shown}                               # a non-owner's click changes nothing
    shown, hidden = apply([dict(x) for x in CARDS], ev, date(2026, 10, 24))         # snooze over after 7 days
    assert "Globex Media" in {x["account"] for x in shown}
    newer = [dict(x, built_from=dict(x["built_from"], finding_ids=x["built_from"]["finding_ids"] + ["NEW"])) for x in CARDS]
    assert "Initech" in {x["account"] for x in apply(newer, ev, TODAY)[0]}           # dismissed returns with new evidence

def test_wrong_person_from_an_allowed_user_changes_the_next_run():
    from pg_system.state import overrides
    dana = next(x for x in ROWS if x["Person"] == "Dana Reyes")
    ev = [{"row_id": dana["Row id"], "profile": dana["LinkedIn"], "action": "wrong_person", "allowed": True}]
    again = {r["Person"]: r for r in run_rules(load_seed(FX("champions.csv")), LOOKUPS, TODAY, ALIASES, KNOWN, overrides=overrides(ev))}
    assert dana["Decision"] == "Card" and again["Dana Reyes"]["Decision"] == "Hold", again["Dana Reyes"]["Reason"]
    assert overrides([dict(ev[0], allowed=False)]) == {}

def test_review_owner_can_confirm_a_namesake_and_the_rules_follow():
    from pg_system.state import overrides
    sam = next(x for x in ROWS if x["Person"] == "Sam Lee")
    from champion_tracker.rules import candidate_key
    cand = next(c for c in LOOKUPS[("Sam Lee", "globex")]["candidates"] if c["current_company_name"] == "Massive Dynamic")
    ev = [{"row_id": sam["Row id"], "profile": candidate_key(cand), "action": "confirm_match", "allowed": True}]
    again = {r["Person"]: r for r in run_rules(load_seed(FX("champions.csv")), LOOKUPS, TODAY, ALIASES, KNOWN, overrides=overrides(ev))}
    assert sam["Decision"] == "Hold" and again["Sam Lee"]["Same person?"] == "Confirmed", again["Sam Lee"]
    blocks = ops_blocks([dict(sam, candidates=[{"name": "Sam Lee", "title": "t", "company": "c", "key": candidate_key(cand)}])], "RUN", "Ops", [])
    vals = [e["value"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
    assert vals[0] == f"RUN:{sam['Row id']}|Sam Lee @ Massive Dynamic" and len(vals) == 4     # + None of these, Paste, Can't resolve
    assert vals[2] == vals[3] == f"RUN:{sam['Row id']}"

def test_delivery_creates_once_then_updates_in_place_and_survives_a_failed_canvas():
    calls = []
    def fake(method, body, get=False):
        calls.append(method)
        if method == "canvases.create":
            return {"ok": True, "canvas_id": "F_C"}
        if method == "slackLists.create":
            return {"ok": True, "list_id": "F_L", "list_metadata": {"schema": [dict(c, id="Col_" + c["key"]) for c in slack_post.LIST_SCHEMA]}}
        if method == "slackLists.items.create":
            return {"ok": True, "item": {"id": f"Rec{len(calls)}"}}
        if method == "files.info":
            return {"ok": True, "file": {"permalink": "https://x.slack.com/docs/T/F"}}
        return {"ok": True, "ts": "1.0"}
    rows = list_rows(CARDS)
    with tempfile.TemporaryDirectory() as d:
        digest = digest_blocks(CARDS, PERIOD)
        r1 = slack_post.deliver("C1", digest, "t", "md", dry_run=False, out_dir=d, next_steps=rows, call=fake)
        assert r1["canvas_id"] == "F_C" and calls.count("slackLists.items.create") == 10 and not r1["errors"]
        calls.clear()
        slack_post.deliver("C1", digest, "t", "md", dry_run=False, out_dir=d, next_steps=rows[:8], call=fake)
        assert "canvases.create" not in calls and calls.count("canvases.edit") == 2 and "slackLists.create" not in calls
        assert calls.count("slackLists.items.update") == 9            # 8 rows updated + 1 call marking 2 rows 'not this week'
    def broken(method, body, get=False):
        if method == "canvases.create":
            raise RuntimeError("canvases.create: free_teams_cannot_create_standalone_canvases")
        return fake(method, body, get)
    with tempfile.TemporaryDirectory() as d:
        r = slack_post.deliver("C1", digest_blocks(CARDS, PERIOD), "t", "md", dry_run=False, out_dir=d, next_steps=[], call=broken)
        assert r["message_ts"] == "1.0" and r["errors"] and r["canvas_id"] is None

def test_live_requirements_follow_the_switched_on_sources():
    from pg_system.pipeline import required_env
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith(("GONG_", "SF_", "SLACK_", "SNOWFLAKE_", "PEOPLE_"))}
    try:
        only_sf = {"sources": {"warehouse": False, "people": False, "gong": False, "salesforce": True, "field_notes": False},
                   "champion_seed_csv": "x.csv"}
        assert required_env(only_sf, False) == ["SF_INSTANCE_URL", "SF_CLIENT_ID + SF_CLIENT_SECRET (or SF_ACCESS_TOKEN)"]
        assert "GONG_BASE_URL" in required_env({}, False)
    finally:
        os.environ.update(saved)

def test_clay_export_becomes_candidates_for_the_rules():
    from champion_tracker.adapters.clay_export import lookups_from_csv
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "clay.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write('Full Name,Customer,Current Company,Current Company Domain,Current Title,LinkedIn Profile,Experience\n'
                    'Dana Reyes,Acme Robotics,Contoso Systems,contoso.com,Director,https://li/dana,'
                    '"[{""company"": ""Acme Robotics"", ""website"": ""acme.com"", ""start"": ""2019-01-01"", ""end"": ""2026-08-01""},'
                    '{""company"": ""Contoso Systems"", ""website"": ""contoso.com"", ""start"": ""2026-09-01"", ""end"": null}]"\n')
        seed = [s for s in load_seed(FX("champions.csv")) if s["Name"] == "Dana Reyes"]
        lk = lookups_from_csv(path, seed)
        r = run_rules(seed, lk, TODAY)[0]
        assert r["Moved?"] == "Yes" and r["New company"] == "Contoso Systems" and r["Same person?"] == "Confirmed", r

def test_gong_unknown_speaker_is_placed_by_email_domain_and_slack_replies_keep_their_account():
    from gtm_connectors.gong import _side
    assert _side({"affiliation": "Unknown", "emailAddress": "cfo@globex.com"}, "https://www.globex.com") == "customer"
    assert _side({"affiliation": "Unknown", "emailAddress": "x@gmail.com"}, "globex.com") == "unknown"
    hist = {"messages": [{"user": "U1", "text": "Tailspin Toys: met the CFO", "ts": "1792162800.000001", "reply_count": 1},
                         {"user": "U1", "text": "CFO said the price is too high", "ts": "1792162900.000001", "thread_ts": "1792162800.000001"},
                         {"subtype": "bot_message", "bot_id": "B_FORM", "text": "Litware: budget frozen until Q1", "ts": "1792163000.000001"}]}
    sigs, skipped = slack_notes.to_signals(hist, {"members": []}, ACCOUNTS, "C1", "ex", bot_ids=["B_FORM"])
    assert [s["account"] for s in sigs] == ["Tailspin Toys", "Tailspin Toys", "Litware"] and not skipped

def test_salesforce_notes_query_never_filters_a_long_text_field():
    assert "Description !=" not in salesforce.NOTES_SOQL and "TaskSubtype" not in salesforce.NOTES_SOQL.split("{subtypes}")[0]


def test_observer_proposals_only_count_when_the_code_can_verify_them():
    from gtm_briefs.observer import observe, validate
    assert validate("We were on your webinar last week", {"engagement": "on your webinar"}) == ["engagement"]
    assert validate("We were on your webinar last week", {"engagement": "attended a webinar"}) == []        # paraphrase
    assert validate("We use Acme Expense today", {"competitor": "Acme Expense"}) == []                      # not a listed rival
    assert validate("Honestly the price isn't the issue", {"pricing_friction": "the price isn't the issue"}) == []
    fake = lambda prompt: '[{"id": "a", "themes": {"people_change": "our new controller"}}, {"id": "b", "themes": {"timing": "made up"}}]'
    obs = observe([{"id": "a", "text": "Loop in our new controller, she owns this now"}, {"id": "b", "text": "Sounds good"}], model=fake)
    assert obs == {"a": ["people_change"], "b": []}
    f = extract([{"source": "call", "id": "a", "side": "customer", "text": "Loop in our new controller, she owns this now",
                  "date": "2026-10-15", "account": "Initech", "speaker": "X", "speaker_title": "Y", "url": "u"}], obs)
    assert [x["theme"] for x in f] == ["people_change"]

def test_gong_tracker_hits_become_themes_on_the_turn_they_happened_in():
    from gtm_briefs.observer import tracker_themes
    ext = {"calls": [{"metaData": {"id": "9", "url": "u", "started": "2026-10-15T10:00:00Z"},
                      "parties": [{"speakerId": "s1", "name": "Ann", "title": "CFO", "affiliation": "External"}],
                      "context": [], "content": {"trackers": [{"name": "Budget", "occurrences": [{"startTime": 65.0, "speakerId": "s1"}]}]}}]}
    tr = {"callTranscripts": [{"callId": "9", "transcript": [
        {"speakerId": "s1", "sentences": [{"start": 1000, "end": 5000, "text": "Hello."}]},
        {"speakerId": "s1", "sentences": [{"start": 60000, "end": 70000, "text": "We have to watch the spend this year."}]}]}]}
    sigs = gong.to_signals(ext, tr)
    assert [s["trackers"] for s in sigs] == [[], ["Budget"]]
    assert tracker_themes(sigs, {"Budget": "pricing_friction"}) == {"GC-9-2": ["pricing_friction"]}

def test_each_run_is_written_to_the_warehouse_and_a_rerun_replaces_it():
    import sqlite3
    con = sqlite3.connect(":memory:")
    for _ in range(2):
        n = warehouse.write_run(con, "R1", "2026-10-18", ROWS, FINDINGS, CARDS, [{"at": "t", "user": "U", "action": "snooze"}], {"x": 1})
    got = {t: con.execute(f"select count(*) from {t}").fetchone()[0] for t in warehouse.RUN_TABLES}
    assert got == {"pg_decisions": len(ROWS), "pg_findings": len(FINDINGS), "pg_cards": len(CARDS), "pg_feedback": 1, "pg_runs": 1}, got
    assert n["pg_cards"] == len(CARDS)

def test_backtest_reports_hit_rate_per_play_and_against_no_card():
    import importlib.util
    spec = importlib.util.spec_from_file_location("bt", os.path.join(ROOT, "scripts", "backtest.py"))
    bt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bt)
    cards = [{"account": "Globex Media", "play": "P4", "tier": "High", "run_date": "2026-10-18"},
             {"account": "Litware", "play": "P8", "tier": "Low", "run_date": "2026-10-18"}]
    opps = [{"account": "Globex Media", "created_date": "2026-11-02"}, {"account": "Initech", "created_date": "2026-12-30"}]
    r = bt.backtest(cards, opps, 60, ["Globex Media", "Litware", "Initech"])
    assert r["play P4"]["rate"] == 1.0 and r["play P8"]["rate"] == 0.0 and r["no card"] == {"cards": 1, "with_new_opportunity": 0, "rate": 0.0}


def test_slack_app_compiles():
    import py_compile
    py_compile.compile(os.path.join(ROOT, "slack_app", "app.py"), doraise=True)


# ---- round 2: the red-team fixes, each locked in ----------------------------------------------------------------
import contextlib  # noqa: E402
import io  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from datetime import timedelta  # noqa: E402

from gtm_briefs import observer as observer_mod  # noqa: E402
from pg_system import pipeline as pipeline_mod, state as state_mod  # noqa: E402
from pg_system.feedback_loop import broken_fixes, paste_modal, profile_url  # noqa: E402


def _patched(obj, **attrs):
    """Swap module attributes for one test and give back a function that restores them."""
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    return lambda: [setattr(obj, k, v) for k, v in saved.items()]


def _cfg(d, **kw):
    return dict({"review_owner": {"name": "RevOps", "slack_id": "U_OPS"}, "regression_file": os.path.join(d, "reg.jsonl"),
                 "feedback_log": os.path.join(d, "fb", "feedback.jsonl")}, **kw)


def _replay_file(d, run, prefix, person):
    rdir = os.path.join(d, "replay", run)
    return next(f for f in os.listdir(rdir) if f.startswith(prefix) and
                json.load(open(os.path.join(rdir, f)))["row"]["Person"] == person)


def _reqs(d):
    return json.load(open(os.path.join(d, "slack_requests.json")))


def test_review_queue_candidates_use_the_seed_name_variants():
    """A maiden name in 'Name variants' must reach the review owner as a candidate (round 1 matched only First/Last)."""
    seed = {"Row id": "r1", "Name": "Ann Smith", "First": "Ann", "Last": "Smith", "Slug": "acme", "Name variants": "Ann Jones"}
    lk = {("Ann Smith", "acme"): {"ok": True, "candidates": [{"full_name": "Ann Jones", "linkedin_url": "https://li/ann"},
                                                             {"full_name": "Bob Jones", "linkedin_url": "https://li/bob"}]}}
    got = pipeline_mod._candidates({"Row id": "r1"}, {"r1": seed}, lk)
    assert [c["name"] for c in got] == ["Ann Jones"], got


def test_accounts_past_the_top_six_are_thread_replies_with_their_own_buttons():
    with tempfile.TemporaryDirectory() as d:
        s = run_pipeline("demo", TODAY, d, cfg=_cfg(d))
        replies = [r["body"] for r in _reqs(d) if r["method"] == "chat.postMessage" and r["body"].get("thread_ts")]
        acts = [b for r in replies for b in r["blocks"] if b["type"] == "actions"]
        assert len(replies) == 1 and len(acts) == 4, replies
        assert all(e["value"].startswith(s["run_id"] + ":") for b in acts for e in b["elements"])


def test_blocked_moves_get_no_card_and_reach_run_health():
    with tempfile.TemporaryDirectory() as d:
        s = run_pipeline("demo", TODAY, d, cfg=_cfg(d, blocked={"domains": {"contoso.com": "competitor"}}))
        cards = json.load(open(os.path.join(d, "surface.json")))
        assert "Contoso Systems" not in {c["account"] for c in cards}
        assert any("Dana Reyes joined Contoso Systems (competitor)" in a for a in s["alerts"]), s["alerts"]
        assert isinstance(s["findings_unmatched"], int) and "unmatched_accounts" in s


def test_run_health_names_what_the_run_could_not_do():
    rows = [{"Decision": "Hold", "Reason": "Search hit the 50-result cap: 50 profiles with this name came back and none ..."},
            {"Decision": "Hold", "Reason": "4 people share this name; none is tied to X."}]
    lookups = {("a", "x"): {"capped": True}, ("b", "x"): {"capped": False}, ("c", "x"): {"ok": False}}
    unmatched = [{"account": "Ghost Corp"}, {"account": "Phantom Ltd"}]
    cur = monitor.summary(rows, [{"source": "call", "id": "C-1"}, {"source": "crm", "id": "N"}, {"source": "field", "id": "F"}],
                          [], [], [], lookups=lookups, unmatched=unmatched, notes=["Ann Lee joined Rivalco (competitor): no outreach"],
                          observer={"enabled": True, "sent": 10, "truncated_by_cap": 4, "malformed_batches": 1},
                          owners_without_slack=["Riley Shaw", "Riley Shaw"], corrupt_log_lines=2)
    assert cur["hold_reasons"] == {"search capped": 1, "same name": 1} and cur["lookups_capped"] == 1
    a = " | ".join(monitor.alerts(cur, None))
    for want in ("1 people-data searches hit the result cap (1 held", "2 finding(s) matched no CRM account", "Ghost Corp, Phantom Ltd",
                 "Blocked move: Ann Lee joined Rivalco", "4 turn(s) past OBSERVER_MAX_TURNS", "1 batch(es) came back unreadable",
                 "1 card owner(s) have no Slack id", "Riley Shaw", "2 line(s) in the feedback log could not be read"):
        assert want in a, (want, a)


def test_observer_stats_reach_the_summary_and_its_env_is_required():
    with tempfile.TemporaryDirectory() as d:
        stats = {"sent": 3, "skipped_not_counted": 1, "truncated_by_cap": 2, "malformed_batches": 1}
        restore = _patched(observer_mod, required_env=lambda: [],
                           observe_with_stats=lambda signals: {"observations": {}, "stats": stats})
        try:
            s = run_pipeline("demo", TODAY, d, cfg=_cfg(d, observer={"enabled": True}))
        finally:
            restore()
        assert s["observer"] == dict(stats, enabled=True, prompt_version=observer_mod.PROMPT_VERSION)
        assert any("Observer: 2 turn(s)" in a for a in s["alerts"])
    saved = {k: os.environ.pop(k) for k in ("ANTHROPIC_API_KEY", "OBSERVER_PROVIDER") if k in os.environ}
    try:
        from pg_system.pipeline import required_env
        assert "ANTHROPIC_API_KEY" in required_env({"observer": {"enabled": True}}, False)
        assert "ANTHROPIC_API_KEY" not in required_env({}, False)
        os.environ["OBSERVER_PROVIDER"] = "bogus"
        assert any("OBSERVER_PROVIDER='bogus'" in m for m in required_env({"observer": {"enabled": True}}, False))
        with tempfile.TemporaryDirectory() as d:     # demo runs on without the key, and says so
            s = run_pipeline("demo", TODAY, d, cfg=_cfg(d, observer={"enabled": True}))
            assert s["observer"]["skipped"] and any("Observer is on but did not run" in a for a in s["alerts"])
    finally:
        os.environ.pop("OBSERVER_PROVIDER", None)
        os.environ.update(saved)


def test_a_subsidiary_card_replays_with_its_parent_to_the_same_play():
    accs = [{"account": "Globex Media", "domain": "globex.com", "type": "customer", "owner": "Riley Shaw", "crm_id": "001P",
             "renewal_date": "", "open_opportunity": "", "owner_id": "005R"},
            {"account": "Globex Studios", "domain": "globexstudios.com", "type": "prospect", "owner": "", "crm_id": "001S",
             "parent_id": "001P", "renewal_date": "", "open_opportunity": ""}]
    row = dict(next(r for r in ROWS if r["Person"] == "Dana Reyes"), **{"New company": "Globex Studios", "New domain": "globexstudios.com"})
    card = next(c for c in build_cards(accs, [row], [], TODAY) if c["account"] == "Globex Studios")
    assert card["part_of"] == "Globex Media" and card["play"] == "P2", card
    replay = {"kind": "card", "today": TODAY.isoformat(), "card": card, "account": card["built_from"]["account"],
              "parent": card["built_from"]["parent"], "champion_rows": [row], "findings": []}
    case = {"action": "wrong_play", "rejected_play": "P2", "replay": replay}
    assert replay_case(case) == {"now": "P2", "passes": False}
    assert replay_case(dict(case, replay=dict(replay, parent=None)))["now"] == "P1"    # what round 1 replayed
    with tempfile.TemporaryDirectory() as d:          # the pipeline writes the parent (and owner id) into each card replay
        s = run_pipeline("demo", TODAY, d, cfg=_cfg(d))
        r = json.load(open(os.path.join(d, "replay", s["run_id"], "card_acct_001DEMO0000000004.json")))
        assert "parent" in r and r["owner_id"].startswith("005DEMO")


def test_preflight_counts_instead_of_pulling():
    from pg_system import check
    def no_pull(*a, **k):
        raise AssertionError("preflight must not pull accounts or transcripts")
    info = {"missing_account_fields": [], "customer_types_found": ["Customer"], "account_types_in_org": ["Customer"],
            "accounts": 1234, "account_filter": "Type != null"}
    r1 = _patched(salesforce, preflight=lambda: info, fetch_accounts=no_pull)
    r2 = _patched(gong, count_calls=lambda a, b: 7, fetch=no_pull)
    try:
        rep = check.preflight({"sources": {"warehouse": False, "people": False, "gong": True, "salesforce": True,
                                           "field_notes": False}, "champion_seed_csv": "x.csv"})
    finally:
        r1()
        r2()
    assert rep["sources"]["salesforce"]["accounts"] == 1234 and rep["sources"]["gong"] == {"ok": True, "calls_last_2_days": 7}, rep


def test_identity_is_the_review_owner_and_cards_are_their_owner_by_name_or_salesforce_id():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, owner_slack_ids={"005DEMO0000000003": "U_RILEY", "Jordan Blake": "U_JORDAN"})
        cards = _demo_run(d, cfg)
        g = next(c for c in cards if c["account"] == "Globex Media")
        n = next(c for c in cards if c["account"] == "Northwind Foods")
        rp, log = os.path.join(d, "replay"), cfg["feedback_log"]
        assert handle_click("snooze", _val(d, g["id"]), "U_RILEY", rp, cfg, log_path=log)["allowed"]      # by owner id
        assert not handle_click("snooze", _val(d, g["id"]), "U_OPS", rp, cfg, log_path=log)["allowed"]    # review owner: no
        assert handle_click("snooze", _val(d, n["id"]), "U_JORDAN", rp, cfg, log_path=log)["allowed"]     # by name
        wp = _val(d, n["who"]["row_id"])                     # "Wrong person" on a seller card is an identity decision
        r = handle_click("wrong_person", wp, "U_JORDAN", rp, cfg, note="x", log_path=log)
        assert not r["allowed"] and "only the review owner decides identity" in r["message"] and r["regression_case"]
        assert handle_click("wrong_person", wp, "U_OPS", rp, cfg, note="x", log_path=log)["allowed"]
        hold = _replay_file(d, open(os.path.join(rp, "LATEST")).read(), "champion_", "Sam Lee")[len("champion_"):-5]
        assert not handle_click("reject_match", _val(d, hold) + "|x", "U_JORDAN", rp, cfg, log_path=log)["allowed"]


def test_a_routed_card_belongs_to_the_person_it_was_routed_to():
    row = dict(next(r for r in ROWS if r["Person"] == "Dana Reyes"), **{"New company": "Brand New Co", "New domain": "brandnew.co"})
    for default, ids, yes, no in ((None, {}, "U_OPS", "U_X"), ("Pat Router", {"Pat Router": "U_PAT"}, "U_PAT", "U_OPS")):
        cfg = {"review_owner": {"name": "RevOps", "slack_id": "U_OPS"}, "owner_slack_ids": ids}
        if default:
            cfg["default_owner"] = default
        card = next(c for c in build_cards(ACCOUNTS, [row], [], TODAY, default_owner=default or "RevOps") if c["account"] == "Brand New Co")
        assert card["owner_routed"]
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "RUN"))
            open(os.path.join(d, "LATEST"), "w").write("RUN")
            json.dump({"kind": "card", "today": "2026-10-18", "card": card, "account": card["built_from"]["account"]},
                      open(os.path.join(d, "RUN", f"card_{card['id']}.json"), "w"))
            log = os.path.join(d, "f.jsonl")
            assert handle_click("snooze", f"RUN:{card['id']}", yes, d, cfg, log_path=log)["allowed"], default
            assert not handle_click("snooze", f"RUN:{card['id']}", no, d, cfg, log_path=log)["allowed"], default


def test_pasted_profile_urls_are_checked_and_normalised():
    assert profile_url("linkedin.com/in/zed-coe/") == "https://www.linkedin.com/in/zed-coe"
    assert profile_url(" http://uk.linkedin.com/in/zed-coe?trk=x ") == "https://www.linkedin.com/in/zed-coe"
    assert profile_url("https://example.com/team/zed") == "https://example.com/team/zed"
    for bad in ("http://example.com/zed", "not a url", "https://", "https://x.com/" + "a" * 400, "ftp://x.com/a"):
        assert profile_url(bad) == "", bad
    m = paste_modal("RUN:r1", "C1", "1.0", "id:r1")
    el = m["blocks"][0]["element"]
    assert m["callback_id"] == "paste_profile" and m["blocks"][0]["block_id"] == "url" and m["blocks"][0]["hint"]["text"]
    assert el["type"] == "plain_text_input" and el["action_id"] == "text" and el["max_length"] == 300
    assert json.loads(m["private_metadata"])["value"] == "RUN:r1" and len(m["title"]["text"]) <= 24


def test_paste_the_right_profile_is_kept_on_the_next_run():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, owner_slack_ids={"Riley Shaw": "U_RILEY"})
        run_pipeline("demo", TODAY, d, cfg=cfg)
        rp, log = os.path.join(d, "replay"), cfg["feedback_log"]
        zed = _replay_file(d, open(os.path.join(rp, "LATEST")).read(), "champion_", "Zed Coe")[len("champion_"):-5]
        bad = handle_click("paste_profile", _val(d, zed), "U_OPS", rp, cfg, note="zed coe on linkedin", log_path=log)
        assert not bad["ok"] and not os.path.exists(log)                     # nothing saved
        seller = handle_click("paste_profile", _val(d, zed), "U_RILEY", rp, cfg, note="https://www.linkedin.com/in/zed-coe", log_path=log)
        assert not seller["allowed"]
        ok = handle_click("paste_profile", _val(d, zed), "U_OPS", rp, cfg, note="linkedin.com/in/zed-coe/", log_path=log)
        assert ok["allowed"] and ok["event"]["profile"] == "https://www.linkedin.com/in/zed-coe" and ok["event"]["action"] == "paste_profile"
        assert ok["write_back"]["fields"]["Champion_Move_Status__c"] == "Identity confirmed"
        assert state_mod.overrides(state_mod.read_log(log))[zed]["confirm_url"] == "https://www.linkedin.com/in/zed-coe"
        s = run_pipeline("demo", TODAY, d, cfg=cfg)
        row = next(r for r in csv.DictReader(open(os.path.join(d, "decisions.csv"))) if r["Person"] == "Zed Coe")
        assert s["profiles_pasted"] == 1 and row["Same person?"] == "Confirmed" and row["Reason"] == "No current employer on file.", row


def test_a_fetched_profile_joins_the_lookup_once_and_is_confirmed():
    from pg_system.pipeline import apply_pasted_profiles
    seed = [s for s in warehouse.champions_sqlite(FX("warehouse", "demo_warehouse.sql"), "2026-10-18") if s["Name"] == "Zed Coe"]
    rid, url = seed[0]["Row id"], "https://www.linkedin.com/in/zed-coe"
    calls = []
    def fetch(u):
        calls.append(u)
        return {"full_name": "Zed Smith", "linkedin_url": u, "current_company_name": "Newco"}
    lk, ov, changed = apply_pasted_profiles(seed, LOOKUPS, {rid: {"reject": [], "confirm_url": url}}, fetch)
    cand = lk[(seed[0]["Name"], seed[0]["Slug"])]["candidates"][-1]
    assert changed == [rid] and ov[rid] == {"reject": [], "confirm": url} and calls == [url]
    assert cand["full_name"] == "Zed Coe" and cand["name_on_profile"] == "Zed Smith" and cand["current_company_name"] == "Newco"
    have = {k: dict(v, candidates=[{"full_name": "Zed Coe", "linkedin_url": url + "/"}]) for k, v in LOOKUPS.items()}
    have[(seed[0]["Name"], seed[0]["Slug"])] = {"ok": True, "candidates": [{"full_name": "Zed Coe", "linkedin_url": url + "/"}]}
    lk, ov, _ = apply_pasted_profiles(seed, have, {rid: {"reject": [], "confirm_url": url}}, fetch)
    # the provider's spelling (trailing slash) and the pasted link are one candidate; the key is the normalised URL
    assert len(lk[(seed[0]["Name"], seed[0]["Slug"])]["candidates"]) == 1 and ov[rid]["confirm"] == url and len(calls) == 1


def test_fetch_profile_reads_one_profile_by_url_or_returns_none():
    from champion_tracker.adapters import people_data
    sent = []
    def fake(url, body, tries=4):
        sent.append((url, body))
        return {"contact": {"full_name": "Zed Coe", "current_employer": "Newco", "current_employer_website": "newco.com",
                            "role_start_date": "2026-07-01", "experience_json": '[{"company": {"name": "Initrode"}, '
                            '"start_date": "2019-01-01", "end_date": "2026-06-01"}]'}}
    saved_env = os.environ.pop("PEOPLE_PROFILE_URL", None)
    restore = _patched(people_data, _post=fake)
    try:
        assert people_data.fetch_profile("https://www.linkedin.com/in/zed-coe") is None and not sent    # endpoint not set
        os.environ["PEOPLE_PROFILE_URL"] = "https://people.example/profile"
        c = people_data.fetch_profile("https://www.linkedin.com/in/zed-coe")
        assert sent[0] == ("https://people.example/profile", {"linkedin_url": "https://www.linkedin.com/in/zed-coe",
                                                              "columns": people_data.COLUMNS})
        assert c["current_company_name"] == "Newco" and c["linkedin_url"].endswith("/in/zed-coe") and c["role_start_date"] == "2026-07"
        assert c["experience"][0]["company"] == "Initrode" and c["experience"][0]["end"] == "2026-06"
        def failing(url, body, tries=4):
            raise people_data.LookupError_("lookup failed after 4 tries")
        people_data._post = failing
        assert people_data.fetch_profile("https://www.linkedin.com/in/zed-coe") is None
    finally:
        restore()
        os.environ.pop("PEOPLE_PROFILE_URL", None)
        if saved_env is not None:
            os.environ["PEOPLE_PROFILE_URL"] = saved_env


def test_a_live_dry_run_leaves_the_live_buttons_working():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, owner_slack_ids={"Riley Shaw": "U_RILEY"}, keep_runs=1)
        cards = _demo_run(d, cfg)
        old = _val(d, next(c for c in cards if c["account"] == "Globex Media")["id"])
        restore = _patched(pipeline_mod, gather_live=lambda today, prev, c, post, out, info: pipeline_mod.gather_demo(today, c, info))
        try:
            for day in (date(2026, 10, 25), date(2026, 11, 1)):
                s = run_pipeline("live", day, d, cfg=cfg)
                assert not s["replay"]["latest_updated"] and os.path.isdir(os.path.join(d, "replay", s["run_id"]))
        finally:
            restore()
        assert open(os.path.join(d, "replay", "LATEST")).read() == old.split(":")[0]
        assert os.path.isdir(os.path.join(d, "replay", old.split(":")[0]))          # pruning never removes the live run
        r = handle_click("snooze", old, "U_RILEY", os.path.join(d, "replay"), cfg, log_path=cfg["feedback_log"])
        assert r["ok"] and r["allowed"], r


def test_posting_moves_latest_and_a_slack_connect_channel_is_refused():
    calls = []
    def fake(method, body, get=False):
        calls.append(method)
        if method == "conversations.info":
            return {"ok": True, "channel": {"id": body["channel"], "is_ext_shared": True}}
        if method == "canvases.create":
            return {"ok": True, "canvas_id": "F_C"}
        if method == "slackLists.create":
            return {"ok": True, "list_id": "F_L", "list_metadata": {"schema": [dict(c, id="Col_" + c["key"]) for c in slack_post.LIST_SCHEMA]}}
        if method == "slackLists.items.create":
            return {"ok": True, "item": {"id": f"Rec{len(calls)}"}}
        return {"ok": True, "ts": "1.0", "file": {"permalink": ""}}
    restore = _patched(slack_post, _call=fake)
    try:
        with tempfile.TemporaryDirectory() as d:
            try:
                run_pipeline("demo", TODAY, d, post_slack=True, channel="C1", cfg=_cfg(d))
                raise AssertionError("expected a refusal")
            except slack_post.SharedChannelError:
                pass
            assert calls == ["conversations.info"] and not os.path.exists(os.path.join(d, "replay", "LATEST"))
            assert json.load(open(os.path.join(d, "summary.json")))["slack"]["posted"] is False
            s = run_pipeline("demo", TODAY, d, post_slack=True, channel="C1", cfg=_cfg(d, allow_shared_channel=True))
            assert open(os.path.join(d, "replay", "LATEST")).read() == s["run_id"] and calls.count("chat.postMessage") == 4
    finally:
        restore()


def test_a_corrupt_feedback_line_is_skipped_counted_and_the_rest_still_applies():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        cards = _demo_run(d, cfg)
        g = next(c for c in cards if c["account"] == "Globex Media")
        os.makedirs(os.path.dirname(cfg["feedback_log"]), exist_ok=True)
        with open(cfg["feedback_log"], "w") as f:
            f.write(json.dumps({"card_id": g["id"], "allowed": True, "action": "snooze", "user": "U1", "at": "2026-10-17T10:00:00"}) + "\n")
            f.write('{"card_id": "acct_x", "action": "sno\n')
        bad = []
        assert len(state_mod.read_log(cfg["feedback_log"], bad)) == 1 and bad == [2]
        s = run_pipeline("demo", TODAY, d, cfg=cfg)
        assert s["feedback_log_corrupt_lines"] == 1 and s["held_back"] == 1
        assert any("1 line(s) in the feedback log could not be read" in a for a in s["alerts"])


def test_check_regressions_exits_one_when_a_fixed_case_breaks():
    from pg_system.pipeline import main
    sample = os.path.join(ROOT, "tests", "data", "regression_sample.jsonl")
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["--check-regressions", "--regression-file", sample]) == 0
    with tempfile.TemporaryDirectory() as d:
        broken = os.path.join(d, "reg.jsonl")
        with open(broken, "w") as f:
            for line in open(sample):
                f.write(json.dumps(dict(json.loads(line), status="fixed")) + "\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert main(["--check-regressions", "--regression-file", broken]) == 1
        assert "marked fixed now FAIL again" in out.getvalue()
        assert len(broken_fixes(check_regressions(broken))) == 1


def test_people_lookups_run_in_parallel_and_reuse_a_fresh_cache():
    from pg_system.pipeline import lookup_people
    seed = [{"Name": f"P{i}", "Slug": "s", "Customer": "Acme", "First": "P", "Last": str(i)} for i in range(16)]
    lock, live, seen = threading.Lock(), [0, 0], []
    def fake(p):
        with lock:
            live[0] += 1
            live[1] = max(live[1], live[0])
            seen.append(p["Name"])
        time.sleep(0.03)
        with lock:
            live[0] -= 1
        return {"name": p["Name"], "slug": "s", "ok": p["Name"] != "P3", "candidates": []}
    with tempfile.TemporaryDirectory() as d:
        lk, st = lookup_people(seed, TODAY, d, {"people_workers": 8}, lookup=fake)
        assert len(lk) == 16 and st["looked_up"] == 16 and st["failed"] == 1 and 1 < live[1] <= 8, (st, live)
        assert len(open(os.path.join(d, "people_cache.jsonl")).read().splitlines()) == 15       # a failure is never cached
        seen.clear()
        lk, st = lookup_people(seed, TODAY + timedelta(days=7), d, {}, lookup=fake)          # next week's run (default 8 days)
        assert seen == ["P3"] and st["from_cache"] == 15 and lk[("P0", "s")]["ok"]
        seen.clear()
        lookup_people(seed, TODAY + timedelta(days=8), d, {"people_cache_days": 7}, lookup=fake)
        assert len(seen) == 16                                                                  # 8 days old > 7: stale


def test_old_moves_go_to_the_canvas_not_one_by_one():
    with tempfile.TemporaryDirectory() as d:
        s = run_pipeline("demo", TODAY, d, cfg=_cfg(d, old_move_days=100))        # Leo Park moved 159 days ago
        assert s["old_moves"] == 1
        queue = [r["Person"] for r in csv.DictReader(open(os.path.join(d, "review_queue.csv")))]
        assert "Leo Park" not in queue and len(queue) == 6
        ops = open(os.path.join(d, "ops.md")).read()
        assert "## Old moves (1)" in ops and "| Leo Park |" in ops.split("## Old moves")[1]
        ops_msg = next(r["body"] for r in _reqs(d) if r["method"] == "chat.postMessage" and r["body"]["text"] == "Champion review queue")
        assert "Old moves (over 100 days): 1" in json.dumps(ops_msg) and "Leo Park" not in json.dumps(ops_msg)


def test_metrics_rates_queue_age_and_time_to_detect():
    from pg_system.pipeline import metrics
    ev = [{"action": a} for a in ("do_play", "accept", "snooze", "not_now", "wrong_play", "wrong_person", "wrong_account", "confirm_match")]
    with tempfile.TemporaryDirectory() as h:
        rows = [{"Row id": "c1", "Joined new company": "2026-09-01"}, {"Row id": "c2", "Joined new company": "2026-09"}]
        m = metrics(ev, ["r1", "r2"], rows, TODAY, h)
        assert m["seller_clicks"] == 7 and m["acceptance_rate"] == round(2 / 7, 3) and m["snooze_rate"] == round(2 / 7, 3)
        assert m["wrong_person_rate"] == m["wrong_play_rate"] == m["wrong_account_rate"] == round(1 / 7, 3)
        assert m["clicks_by_action"]["confirm_match"] == 1
        assert m["time_to_detect_days"]["by_row"] == {"c1": 47, "c2": 18}                   # month-only: its last day
        m = metrics([], ["r1", "r3"], rows, TODAY + timedelta(days=7), h)
        assert m["queue_age_days"]["by_row"] == {"r1": 7, "r3": 0} and m["acceptance_rate"] is None
        assert m["time_to_detect_days"]["by_row"] == {"c1": 47, "c2": 18}                   # first sighting is kept
    with tempfile.TemporaryDirectory() as d:
        s = run_pipeline("demo", TODAY, d, cfg=_cfg(d))
        assert s["metrics"]["time_to_detect_days"]["by_row"]["003W001"] == 47
        ops_msg = next(r["body"] for r in _reqs(d) if r["method"] == "chat.postMessage" and r["body"]["text"] == "Champion review queue")
        assert "Metrics: 0 seller clicks" in json.dumps(ops_msg) and "## Metrics" in open(os.path.join(d, "ops.md")).read()


def test_weekly_qa_sample_goes_to_the_review_owner_and_wrong_becomes_a_test():
    from champion_tracker.feedback import VALID
    from pg_system.pipeline import qa_sample
    assert {"qa_ok", "qa_wrong", "paste_profile"} <= VALID
    a = qa_sample(ROWS, "2026-10-18-aaaa", 5)
    assert a == qa_sample(ROWS, "2026-10-18-aaaa", 5) and len(a) == 5 and {r["Decision"] for r in a} <= {"Watch", "No action", "Hold"}
    assert qa_sample(ROWS, "x", 0) == []
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, qa_sample_size=3)
        s = run_pipeline("demo", TODAY, d, cfg=cfg)
        qa = next(r["body"] for r in _reqs(d) if r["method"] == "chat.postMessage" and r["body"]["text"] == "Weekly QA sample")
        btns = [e for b in qa["blocks"] if b["type"] == "actions" for e in b["elements"]]
        assert qa["channel"] == "U_OPS" and len(btns) == 6 and {e["action_id"] for e in btns} == {"qa_ok", "qa_wrong"}
        rp, log = os.path.join(d, "replay"), cfg["feedback_log"]
        assert not handle_click("qa_ok", btns[0]["value"], "U_RILEY", rp, cfg, log_path=log)["allowed"]
        ok = handle_click("qa_ok", btns[0]["value"], "U_OPS", rp, cfg, log_path=log)
        assert ok["allowed"] and ok["write_back"]["skipped"].startswith("QA label") and not ok["regression_case"]
        wrong = handle_click("qa_wrong", btns[1]["value"], "U_OPS", rp, cfg, note="Still there, but a namesake", log_path=log)
        assert wrong["regression_case"]
        case = check_regressions(cfg["regression_file"])[0]
        assert case["expect_not"] == case["replay"]["decision"] and case["result"]["passes"] is False
        assert replay_case(dict(case, expect_not="Card"))["passes"]                  # fixed once the decision changes
        assert s["run_id"] in btns[0]["value"]


def test_every_run_keeps_its_cards_for_the_backtest_which_joins_on_account_id_without_renewals():
    import importlib.util
    spec = importlib.util.spec_from_file_location("bt", os.path.join(ROOT, "scripts", "backtest.py"))
    bt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bt)
    with tempfile.TemporaryDirectory() as d:
        run_pipeline("demo", TODAY, d, cfg=_cfg(d, keep_runs=1))
        run_pipeline("demo", date(2026, 10, 25), d, cfg=_cfg(d, keep_runs=1))
        json.dump({"run_id": "dry", "run_date": "2026-11-01", "dry_run": True, "cards": [
            {"account": "Ghost", "crm_id": "", "play": "P1", "tier": "Low", "status": "Ready", "run_date": "2026-11-01"}]},
            open(os.path.join(d, "history", "cards-dry.json"), "w"))
        files = [f for f in os.listdir(os.path.join(d, "history")) if f.startswith("cards-")]
        assert len(files) == 3 and len(os.listdir(os.path.join(d, "replay"))) == 2      # replay pruned (1 run + LATEST)
        cards = bt.load_cards(d)
        assert {c["run_date"] for c in cards} == {"2026-10-18", "2026-10-25"} and "Ghost" not in {c["account"] for c in cards}
        assert len(cards) == len({(c["crm_id"] or c["account"], c["play"], c["run_date"]) for c in cards})
        globex = next(c for c in cards if c["account"] == "Globex Media")
        assert globex["crm_id"].startswith("001DEMO")
    globex = dict(globex, crm_id="001A000001ABCDE")                      # a real 15-character Salesforce Id
    opps = [{"account": "Globex Media Group LLC", "account_id": "001A000001ABCDEAAA", "created_date": "2026-11-02", "type": "New Business"},
            {"account": "Initech", "account_id": "001DEMO0000000005", "created_date": "2026-10-20", "type": "Renewal"}]
    r = bt.backtest([globex, {"account": "Initech", "crm_id": "001DEMO0000000005", "play": "P5", "tier": "Medium", "run_date": "2026-10-18"}],
                    opps, 60)
    assert r["play P4"]["rate"] == 1.0 and r["play P5"]["rate"] == 0.0, r          # 18-char id joins; the renewal is dropped
    assert r["_opportunities"] == {"used": 1, "excluded_by_type": 1, "excluded_types": ["renewal"], "joined_on": "account_id"}
    assert bt.backtest([globex], opps, 60, exclude_types=())["_opportunities"]["excluded_by_type"] == 0


def test_old_notes_are_blanked_and_the_log_is_rewritten_atomically():
    with tempfile.TemporaryDirectory() as d:
        log = os.path.join(d, "f.jsonl")
        with open(log, "w") as f:
            f.write(json.dumps({"at": "2025-01-01T10:00:00", "action": "wrong_play", "note": "Ann Lee left for Rivalco"}) + "\n")
            f.write(json.dumps({"at": "2026-10-01T10:00:00", "action": "wrong_play", "note": "keep me"}) + "\n")
            f.write("not json\n")
        assert state_mod.purge(log, 365, today=TODAY) == 1
        lines = open(log).read().splitlines()
        old, new = json.loads(lines[0]), json.loads(lines[1])
        assert old["note"] == "" and old["note_purged_on"] == "2026-10-18" and old["action"] == "wrong_play"
        assert new["note"] == "keep me" and lines[2] == "not json"
        assert sorted(os.listdir(d)) == ["f.jsonl", "f.jsonl.lock"]          # no temp file left; the lock file stays
        assert state_mod.purge(log, 365, today=TODAY) == 0
    with tempfile.TemporaryDirectory() as d:          # every run purges, by the real date (not the pinned demo day)
        cfg = _cfg(d, retention_days=365)
        os.makedirs(os.path.dirname(cfg["feedback_log"]))
        with open(cfg["feedback_log"], "w") as f:
            f.write(json.dumps({"at": "2020-01-01T10:00:00", "action": "dismiss", "note": "old words about a person"}) + "\n")
        s = run_pipeline("demo", TODAY, d, cfg=cfg)
        assert s["feedback_notes_purged"] == 1 and json.loads(open(cfg["feedback_log"]).read())["note"] == ""


def test_champion_card_snooze_says_what_the_system_does_and_play_names_match_the_contract():
    from champion_tracker import config as C
    from champion_tracker.slack_card import ACTIONS
    from pg_surface.plays import CONTRACT
    from pg_system.state import HIDE_DAYS
    assert dict((a, label) for a, label, _ in ACTIONS)["snooze"] == "Snooze 1 week" and HIDE_DAYS["snooze"] == 7
    assert C.PLAYS["P1"] == CONTRACT["plays"]["P1"]["name"] and C.PLAYS["P2"] == CONTRACT["plays"]["P2"]["name"]


def test_example_config_lists_every_key_the_pipeline_reads():
    ex = json.load(open(os.path.join(ROOT, "config.example.json")))
    for k in ("blocked", "default_owner", "allow_shared_channel", "observer", "people_workers", "people_cache_days",
              "old_move_days", "qa_sample_size", "retention_days", "pipeline_admins"):
        assert k in ex, k
    assert ex["keep_runs"] == 52 and ex["pipeline_admins"] == [] and ex["observer"] == {"enabled": False}
    env = open(os.path.join(ROOT, ".env.example")).read()
    for v in ("OBSERVER_PROVIDER", "OBSERVER_MODEL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
              "PEOPLE_PROFILE_URL", "PIPELINE_MODE"):
        assert re.search(rf"^{v}=", env, re.M), v


def test_gtm_home_points_an_installed_package_at_its_contracts():
    """A wheel install has no contracts/ beside the code; GTM_HOME names the folder that holds them."""
    import shutil, subprocess
    with tempfile.TemporaryDirectory() as d:
        shutil.copytree(os.path.join(ROOT, "contracts"), os.path.join(d, "contracts"))
        plays = json.load(open(os.path.join(d, "contracts", "plays.json")))
        plays["version"] = "9.9-home"
        json.dump(plays, open(os.path.join(d, "contracts", "plays.json"), "w"))
        out = subprocess.run([sys.executable, "-c", "from pg_surface.plays import VERSION; print(VERSION)"],
                             env=dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), GTM_HOME=d), capture_output=True, text=True)
        assert out.stdout.strip() == "9.9-home", out


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL", name, "->", type(e).__name__, str(e)[:400])
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)

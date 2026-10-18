"""Tests for the pipeline surface fixes: CRM-id joins, where the champion went (blocked companies, subsidiaries,
sister domains, open deals), owner routing, plain play copy, buttons for every card, the review queue, Slack mentions,
old moves, account context, ICP fit, owner DMs, safe links and parking rows that cannot be resolved (surface 1.3).
Run without pytest:  python3 tests/test_surface.py      (pytest also works)
"""
import importlib.util
import os
import re
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from pg_surface.plays import CONTRACT, PLAYS, RULES, problems  # noqa: E402
from pg_surface.slack_out import (canvas_markdown, card_blocks, card_messages, clay_rows, digest_blocks, list_rows,  # noqa: E402
                                  neutralize, ops_blocks, ops_canvas_markdown, ops_markdown, queue_summary, sort_queue)
from pg_surface.surface import SURFACE_VERSION, build_cards, icp_fit, match_account, registrable  # noqa: E402

TODAY = date(2026, 10, 18)
PERIOD = ("2026-10-12", "2026-10-18")
CARD_FIELDS = {"id", "account", "owner", "crm_id", "play", "play_name", "play_do", "status", "checks", "headline", "why_now", "who",
               "links", "inputs", "built_from", "tier", "rank_reason", "priority", "rank", "surface_version", "plays_version",
               "domain", "type", "part_of", "owner_routed", "renewal_days", "signals", "context", "icp"}


def _acc(name, dom, typ="customer", crm="", owner="O", opp="", parent="", renew=""):
    return {"account": name, "domain": dom, "type": typ, "owner": owner, "renewal_date": renew, "open_opportunity": opp,
            "crm_id": crm, "parent_id": parent}


def _f(account, theme, text="t", sid="S1", account_id="", conf="direct", stance=""):
    return {"source": "call", "id": sid, "date": "2026-10-15", "account": account, "account_id": account_id, "speaker": "X",
            "speaker_title": "Y", "side": "customer", "text": text, "url": "https://gong/x", "theme": theme, "competitors": [],
            "stance": stance, "confidence": conf}


def _move(person, company, dom, old="Acme Robotics", decision="Card", age=20, **kw):
    return dict({"Row id": "r_" + person.split()[0].lower(), "Person": person, "Case study": old, "Story": "https://ex/story",
                 "Level": "Champion", "Same person?": "Confirmed", "Moved?": "Yes", "New company": company, "New domain": dom,
                 "Move age (days)": age, "Decision": decision, "Reason": "Confirmed champion, recent move, account resolved.",
                 "Title on page": "Head of Travel", "Now": f"Director @ {company}", "LinkedIn": ""}, **kw)


def _clay():
    spec = importlib.util.spec_from_file_location("cf", os.path.join(ROOT, "scripts", "clay_formula.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def clay_play(row, formula):
    """Evaluate the generated Clay formula on one clay_rows() row (our own formula, so eval is safe here)."""
    parts = formula.split(" : ")
    for part in parts[:-1]:
        cond, play = part.rsplit(" ? ", 1)
        py = re.sub(r"\{\{([^}]+)\}\}", lambda m: repr(str(row[m.group(1)])), cond)
        if eval(py.replace("&&", " and ").replace("||", " or ").replace("Number(", "float(")):  # noqa: S307
            return play.strip('"')
    return parts[-1].strip('"')


# ---- 1. join by CRM id ------------------------------------------------------------------------------------------
def test_two_records_named_northwind_join_on_the_crm_id_not_the_name():
    accs = [_acc("Northwind", "northwind.com", crm="001A"), _acc("Northwind", "northwind.co.uk", crm="001B", owner="UK owner")]
    lost = []
    cards = build_cards(accs, [], [_f("Northwind", "expansion", account_id="001B"), _f("Northwind", "expansion", sid="S2")], TODAY,
                        unmatched=lost)
    assert [(c["crm_id"], c["owner"]) for c in cards] == [("001B", "UK owner")], cards
    assert [f["id"] for f in lost] == ["S2"], lost          # the name alone fits two records: reported, not dropped silently

def test_match_account_order_is_crm_id_then_domain_then_name():
    accs = [_acc("Globex Media", "globex.com", crm="001000000000001AAA"), _acc("Globex Studios", "globexstudios.com", crm="001B")]
    assert match_account(accs, "Globex Studios", "globexstudios.com", "001000000000001")["account"] == "Globex Media"  # 15 vs 18 chars
    assert match_account(accs, "Globex Media", "globexstudios.com")["account"] == "Globex Studios"       # domain beats name
    assert match_account(accs, "Globex Media")["account"] == "Globex Media"                              # name-only callers still work
    assert match_account(accs, "Globex Media", account_id="999") is None                                  # unknown id: unmatched

def test_champion_row_crm_id_picks_the_right_record_and_held_findings_are_not_unmatched():
    accs = [_acc("Northwind", "", "prospect", crm="001A"), _acc("Northwind", "", "prospect", crm="001B", owner="B")]
    lost = []
    cards = build_cards(accs, [_move("Dana Reyes", "Northwind", "", **{"New account id": "001B"})],
                        [_f("Nobody Inc", "pricing_friction"), dict(_f("Northwind", "held"), sid="S9")], TODAY, unmatched=lost)
    assert [(c["crm_id"], c["play"]) for c in cards] == [("001B", "P1")], cards
    assert [f["account"] for f in lost] == ["Nobody Inc"], lost


# ---- 2a. blocked companies: competitors and partners ------------------------------------------------------------
def test_champion_who_joined_a_competitor_gets_no_card_and_a_note():
    accs = [_acc("Acme Robotics", "acme.com", crm="001ACME")]
    notes = []
    blocked = {"domains": {"ledgerly.com": "competitor"}, "names": {"Tidewater Partners": "partner"}}
    cards = build_cards(accs, [_move("Dana Reyes", "Ledgerly", "www.ledgerly.com"), _move("Ben Ortiz", "Tidewater Partners LLC", "tidewaterp.io")],
                        [], TODAY, blocked=blocked, notes=notes)
    assert {c["account"] for c in cards} == {"Acme Robotics"}, [c["account"] for c in cards]
    assert notes == ["Dana Reyes joined Ledgerly (competitor): no outreach", "Ben Ortiz joined Tidewater Partners LLC (partner): no outreach"]
    acme = cards[0]
    assert acme["play"] == "P9" and any("moved to Ledgerly (competitor)" in w for w in acme["why_now"]), acme["why_now"]


# ---- 2b. subsidiaries and sister domains ------------------------------------------------------------------------
def test_champion_who_joined_a_customer_subsidiary_is_expansion_side():
    accs = [_acc("Initech", "initech.com", crm="001I", owner="Riley"), _acc("Initech Labs", "initechlabs.com", "prospect", crm="001L",
                                                                           owner="", parent="001I")]
    c = next(c for c in build_cards(accs, [_move("Dana Reyes", "Initech Labs", "initechlabs.com")], [], TODAY) if c["account"] == "Initech Labs")
    assert c["play"] == "P2" and c["type"] == "customer" and c["part_of"] == "Initech", c
    assert "part of Initech" in c["why_now"][0] and c["owner"] == "Riley" and not c["owner_routed"], c
    assert c["built_from"]["parent"]["crm_id"] == "001I"          # what a replay needs to rebuild the same card

def test_sister_domain_not_in_the_crm_is_flagged_not_merged_into_the_customer():
    accs = [_acc("Initech", "initech.com", crm="001I")]
    cards = {c["account"]: c for c in build_cards(accs, [_move("Dana Reyes", "Initech Deutschland", "initech.de")], [], TODAY)}
    assert "Initech" not in cards, cards                       # the name alone would have merged it into the customer
    c = cards["Initech Deutschland"]
    assert c["play"] == "P1" and c["status"] == "Check first"
    assert any(k.startswith("Looks like part of Initech: confirm before treating it as a new logo") for k in c["checks"]), c["checks"]
    trusted = build_cards(accs, [_move("Dana Reyes", "Initech Deutschland", "initech.de", **{"New account id": "001I"})], [], TODAY)
    assert [(x["account"], x["play"]) for x in trusted] == [("Initech", "P2")]        # a CRM id from upstream is trusted
    assert registrable("initech.de") == registrable("eu.initech.co.uk") == registrable("https://www.initech.com/x") == "initech"


# ---- 2c. open deals ---------------------------------------------------------------------------------------------
def test_open_deal_changes_the_play_and_shows_as_evidence():
    accs = [_acc("Fabrikam", "fabrikam.com", "prospect", crm="001F", opp="Discovery"), _acc("Contoso", "contoso.com", "prospect", crm="001C"),
            _acc("Woodgrove", "woodgrove.com", crm="001W", opp="Expansion"), _acc("Umbrella", "umbrella.com", crm="001U")]
    rows = [_move("Leo Park", "Fabrikam", "fabrikam.com"), _move("Dana Reyes", "Contoso", "contoso.com")]
    f = [_f("Woodgrove", "expansion", sid="S1"), _f("Umbrella", "expansion", sid="S2")]
    by = {c["account"]: c for c in build_cards(accs, rows, f, TODAY)}
    assert {k: c["play"] for k, c in by.items()} == {"Fabrikam": "P11", "Contoso": "P1", "Woodgrove": "P12", "Umbrella": "P6"}, by
    assert "Open opportunity in the CRM: Discovery." in by["Fabrikam"]["why_now"] and by["Fabrikam"]["inputs"]["open_deal"]
    assert by["Woodgrove"]["why_now"][0].startswith("Expansion") and not by["Umbrella"]["inputs"]["open_deal"]

def test_new_plays_sit_before_the_plays_they_refine_and_old_order_is_kept():
    order = [r["play"] for r in RULES]
    assert order.index("P11") == order.index("P1") - 1 and order.index("P12") == order.index("P6") - 1, order
    assert [p for p in order if p not in ("P11", "P12")] == ["P4", "P5", "P2", "P6", "P9", "P1", "P7", "P8", "P10"]
    assert PLAYS["P11"]["name"] == "Champion inside an open deal: brief the deal owner" and PLAYS["P11"]["for"] == "prospect"
    assert PLAYS["P12"]["name"] == "Expansion already in the pipeline: help close it" and PLAYS["P12"]["for"] == "customer"
    assert all(p.get("lead_with") is not None for p in PLAYS.values()) and "open_deal" in CONTRACT["facts"]
    assert problems(CONTRACT) == [] and problems(dict(CONTRACT, rules=[{"play": "P99", "all": ["nope"]}]))

def test_clay_formula_is_regenerated_and_agrees_with_the_code_on_every_card():
    mod = _clay()
    f = mod.formula()
    doc = open(os.path.join(ROOT, "clay", "surface_formulas.md"), encoding="utf-8").read()
    assert "```js\n" + f + "\n```" in doc and '{{Open deal}} == "Yes" ? "P11"' in f
    accs = [_acc("Fabrikam", "fabrikam.com", "prospect", crm="001F", opp="Discovery"), _acc("Woodgrove", "woodgrove.com", crm="001W", opp="Expansion"),
            _acc("Initech", "initech.com", crm="001I"), _acc("Initech Labs", "initechlabs.com", "prospect", crm="001L", parent="001I"),
            _acc("Globex", "globex.com", crm="001G", renew="2026-12-01")]
    rows = [_move("Leo Park", "Fabrikam", "fabrikam.com"), _move("Dana Reyes", "Initech Labs", "initechlabs.com"),
            _move("Ben Ortiz", "Brand New Co", "brandnew.co")]
    f2 = [_f("Woodgrove", "expansion"), _f("Globex", "pricing_friction", sid="S2")]
    cards = build_cards(accs, rows, f2, TODAY, default_owner="SDR desk")
    rows_out = clay_rows(cards)
    assert {r["Open deal"] for r in rows_out} == {"Yes", "No"}
    assert [clay_play(r, f) for r in rows_out] == [c["play"] for c in cards], [(c["account"], c["play"]) for c in cards]


# ---- 3. owner routing -------------------------------------------------------------------------------------------
def test_card_with_no_account_owner_is_routed_with_a_check():
    accs = [_acc("Acme Robotics", "acme.com", crm="001ACME")]
    c = next(c for c in build_cards(accs, [_move("Dana Reyes", "Brand New Co", "brandnew.co")], [], TODAY, default_owner="SDR desk")
             if c["account"] == "Brand New Co")
    assert c["owner"] == "SDR desk" and c["owner_routed"] and "Routed to SDR desk: no account owner yet." in c["checks"], c
    assert "routed" in card_blocks(dict(c, rank=1))[0]["text"]["text"]
    bare = next(c for c in build_cards(accs, [_move("Dana Reyes", "Brand New Co", "brandnew.co")], [], TODAY) if c["account"] == "Brand New Co")
    assert bare["owner"] == "" and not bare["owner_routed"] and any(k.startswith("No account owner yet") for k in bare["checks"])
    acme = next(c for c in build_cards(accs, [_move("Dana Reyes", "Brand New Co", "brandnew.co")], [], TODAY, default_owner="SDR desk")
                if c["account"] == "Acme Robotics")
    assert acme["owner"] == "O" and not acme["owner_routed"]


# ---- 4. play copy -----------------------------------------------------------------------------------------------
def test_every_play_reads_as_an_instruction_not_a_pitch():
    slogans = ("win on", "not list price", "position the", "teams like theirs", "lead with value", "re-anchor", "how teams")
    for pid, p in PLAYS.items():
        do = p["do"].lower()
        assert not any(s in do for s in slogans), (pid, p["do"])
        assert re.search(r"\b(this week|today|within two weeks|before the renewal date|no outreach)\b", do), (pid, p["do"])
        assert re.search(r"\b(owner|no outreach)\b", do), (pid, p["do"])           # says who does it
        assert len(p["name"]) <= 60 and not p["name"].endswith("play"), (pid, p["name"])


# ---- 5. buttons for every card ----------------------------------------------------------------------------------
def _many(n):
    accs = [_acc(f"Account {i:02d}", f"a{i}.com", crm=f"001X{i:02d}") for i in range(n)]
    return build_cards(accs, [], [_f(f"Account {i:02d}", "expansion", sid=f"S{i}") for i in range(n)], TODAY)

def test_cards_below_the_top_six_come_as_thread_replies_with_their_buttons():
    cards = _many(20)
    digest = digest_blocks(cards, PERIOD)
    assert sum(b["type"] == "actions" for b in digest["blocks"]) == 6
    assert any("+14 more accounts in the thread below" in e["text"] for b in digest["blocks"] if b["type"] == "context" for e in b["elements"])
    msgs = card_messages(cards)
    assert len(msgs) == 2 and all(len(m["blocks"]) <= 50 and m["text"] for m in msgs)
    ids = [b["block_id"] for m in msgs for b in m["blocks"] if b["type"] == "actions"]
    assert ids == [f"pg:{c['id']}" for c in cards[6:]], ids
    assert all(len(b["elements"]) >= 5 for m in msgs for b in m["blocks"] if b["type"] == "actions")
    big = card_messages(_many(30), per_message=40)                 # never over Slack's limit, whatever is asked
    assert all(len(m["blocks"]) <= 50 for m in big) and sum(b["type"] == "actions" for m in big for b in m["blocks"]) == 24
    assert card_messages(cards[:6]) == []


# ---- 6. the review queue ----------------------------------------------------------------------------------------
QUEUE = [
    {"Row id": "r_zed", "Person": "Zed Coe", "Case study": "Initrode", "Level": "Champion", "Decision": "Hold", "Reason": "No profile found under this name."},
    {"Row id": "r_max", "Person": "Max Hale", "Case study": "Acme", "Level": "User", "Decision": "Hold", "Moved?": "Yes", "New company": "C",
     "Move age (days)": 5, "Reason": "Moved, but identity is only Likely: the only Max Hale on file."},
    {"Row id": "r_sam", "Person": "Sam Lee", "Case study": "Globex", "Level": "Champion", "Decision": "Hold", "Reason": "4 people share this name; none is tied to Globex.",
     "candidates": [{"name": "Sam Lee", "title": "VP", "company": "Massive", "key": "Sam Lee @ Massive", "linkedin": "https://li/sam"}]},
    {"Row id": "r_mo", "Person": "Morgan Ellis", "Case study": "Pied Piper", "Level": "Champion", "Decision": "Hold", "Moved?": "Yes",
     "New company": "Bachmanity", "Move age (days)": "40", "Reason": "Moved, but identity is only Likely: the only Morgan Ellis on file."},
    {"Row id": "r_leo", "Person": "Leo Park", "Case study": "Northwind", "Level": "Champion", "Decision": "Review", "Moved?": "Yes",
     "New company": "Fabrikam", "Move age (days)": 159, "Reason": "Confirmed champion move; check: move is 159 days old (over 90)."},
]

def test_review_queue_sorts_review_first_then_champions_then_freshest():
    assert [r["Person"] for r in sort_queue(QUEUE)] == ["Leo Park", "Morgan Ellis", "Sam Lee", "Zed Coe", "Max Hale"]

def test_every_queue_row_can_take_a_pasted_profile_and_candidates_keep_their_buttons():
    blocks = ops_blocks(QUEUE, "RUN", "Ops", ["x"])
    assert [b["type"] for b in blocks[:3]] == ["header", "context", "context"] and (len(blocks) - 3) == 3 * len(QUEUE)
    summary = blocks[1]["elements"][0]["text"]
    assert summary.startswith("5 rows: 1 Review, 4 Hold") and "identity only likely 2" in summary and "no profile 1" in summary, summary
    acts = {b["block_id"]: b["elements"] for b in blocks if b["type"] == "actions"}
    assert len(acts) == len(QUEUE)
    for rid, els in acts.items():
        paste = [e for e in els if e["action_id"] == "paste_profile"]
        assert len(paste) == 1 and paste[0]["value"] == f"RUN:{rid[3:]}" and paste[0]["text"]["text"] == "Paste the right profile", els
    assert [e["action_id"] for e in acts["id:r_sam"]] == ["confirm_match", "reject_match", "paste_profile", "unresolvable"]
    assert acts["id:r_sam"][0]["value"] == "RUN:r_sam|Sam Lee @ Massive" and acts["id:r_sam"][0]["text"]["text"] == "This is #1"
    assert [e["action_id"] for e in acts["id:r_zed"]] == ["paste_profile", "unresolvable"]     # "No profile" rows too

def test_ops_canvas_has_the_whole_queue_as_a_table():
    md = ops_canvas_markdown(QUEUE * 10, "Ops", ["call signals fell"])
    table = [l for l in md.splitlines() if l.startswith("| ") and not l.startswith("| #")]
    assert len(table) == 50 and "50 rows: 10 Review, 40 Hold" in md and "## Run health" in md and "call signals fell" in md
    assert "[profile](https://li/sam)" in md and table[0].startswith("| 1 | Review | Champion | Leo Park |"), table[0]
    assert ops_markdown(QUEUE, "Ops", []).index("Leo Park") < ops_markdown(QUEUE, "Ops", []).index("Zed Coe")


# ---- 7. Slack mentions ------------------------------------------------------------------------------------------
def test_neutralize_turns_every_mention_into_plain_text():
    assert neutralize("<!channel> <!here> <!everyone> <!here|here>") == "@channel @here @everyone @here"
    assert neutralize("ask <@U12AB> or <@U9|ann>, see <#C1|deals> and <#C2>") == "ask @U12AB or @ann, see #deals and #C2"
    assert neutralize("<!subteam^S1|@ops> <!subteam^S2> <!date^1700000000^{date}|Nov 14>") == "@ops @group Nov 14"
    assert neutralize("canvas ![](@U77) ![](#C9) <https://x.com|link>") == "canvas @U77 #C9 <https://x.com|link>"
    assert neutralize("") == "" and neutralize(None) == ""

def test_quotes_cannot_ping_from_the_canvas_the_digest_or_the_ops_queue():
    accs = [_acc("Globex", "globex.com", crm="001G")]
    cards = build_cards(accs, [], [_f("Globex", "expansion", text="<!channel> we want 50 more seats, ask <@U1|cfo>")], TODAY)
    md = canvas_markdown("# brief\n", cards)
    assert "@channel we want 50 more seats, ask @cfo" in md and "<!channel>" not in md and "<@U1" not in md
    text = str(digest_blocks(cards, PERIOD))
    assert "@channel" in text and "<!channel>" not in text and "&lt;!channel&gt;" not in text
    q = [dict(QUEUE[0], Reason="<!here> No profile found | <@U2>", Person="Zed <!everyone>")]
    for out in (ops_markdown(q, "Ops", ["<!channel> alert"]), ops_canvas_markdown(q, "Ops", ["<!channel> alert"])):
        assert "<!" not in out and "<@" not in out and "@here No profile found \\| @U2" in out and "@channel alert" in out, out
    assert "<!" not in str(ops_blocks(q, "R", "Ops", ["<!channel>"]))


# ---- 8. card contract -------------------------------------------------------------------------------------------
def test_cards_keep_every_field_other_code_reads_and_carry_the_new_version():
    accs = [_acc("Initech", "initech.com", crm="001I"), _acc("Fabrikam", "fabrikam.com", "prospect", crm="001F", opp="Discovery")]
    cards = build_cards(accs, [_move("Leo Park", "Fabrikam", "fabrikam.com", old="Initech")], [_f("Initech", "expansion")], TODAY)
    assert cards and all(CARD_FIELDS <= set(c) for c in cards), [CARD_FIELDS - set(c) for c in cards]
    assert SURFACE_VERSION == "1.3" and all(c["surface_version"] == "1.3" and c["plays_version"] == CONTRACT["version"] for c in cards)


# ---- 9. old moves (red team C3) ---------------------------------------------------------------------------------
def test_review_moves_older_than_old_move_days_make_no_card_and_are_handed_back():
    accs = [_acc("Initech", "initech.com", crm="001I")]
    rows = [_move("Ana Silva", "Old News Co", "oldnews.com", old="Hooli", decision="Review", age=383, **{"Joined new company": "2025-09"}),
            _move("Ben Ortiz", "Mid Co", "midco.com", old="Hooli", decision="Review", age=200),
            _move("Cy Moss", "Fresh Co", "fresh.com", old="Hooli", decision="Card", age=20)]
    old = []
    cards = {c["account"]: c for c in build_cards(accs, rows, [], TODAY, old_moves=old)}
    assert set(cards) == {"Mid Co", "Fresh Co"}, set(cards)              # default 365: the 383-day move is not news
    assert [r["Person"] for r in old] == ["Ana Silva"], old
    # the youngest possible age of a month-only date decides: 2025-11 is at most 322 days old, whatever the column says
    young = _move("Di Yu", "Late Month Co", "latemonth.com", old="Initech", decision="Review", age=352, **{"Joined new company": "2025-11"})
    assert [c["account"] for c in build_cards(accs, [young], [], TODAY)] == ["Late Month Co"]
    # an old move counts neither as champion in nor as champion out (the old customer gets no 'champion left' card)
    left = _move("Eve Park", "Elsewhere", "elsewhere.com", old="Initech", decision="Review", age=80)
    old2 = []
    assert build_cards(accs, [left], [], TODAY, old_move_days=60, old_moves=old2) == [] and [r["Person"] for r in old2] == ["Eve Park"]
    assert {c["account"] for c in build_cards(accs, [left], [], TODAY)} == {"Elsewhere", "Initech"}   # under the limit: both cards

def test_the_real_review_rows_give_seven_cards_not_nineteen():
    """The 195 real rows (people named in public case studies) stay out of the repo. Point SURFACE_REAL_ROWS at the
    stored decision rows to run this check; without it the check has nothing to read and returns."""
    import json
    path = os.environ.get("SURFACE_REAL_ROWS", "")
    if not path or not os.path.exists(path):
        return
    rows = json.load(open(path, encoding="utf-8"))
    old = []
    cards = build_cards([], rows, [], TODAY, default_owner="RevOps owner", old_moves=old)
    assert sum(r.get("Decision") == "Review" for r in rows) == 19 and len(cards) == 7 and len(old) == 12, (len(cards), len(old))


# ---- 10. an unknown CRM id never falls back to a name (code review 5) -------------------------------------------
def test_a_crm_id_that_matches_no_account_is_unmatched_not_joined_by_name():
    accs = [{"account": "Northwind Inc", "domain": "northwind.com", "type": "customer", "owner": "Ann (NA)",
             "crm_id": "001NA0000000001AAA", "renewal_date": "2026-12-01"}]
    assert match_account(accs, "Northwind GmbH", account_id="001EU0000000009AAA") is None
    assert match_account(accs, "Northwind Inc", "northwind.com", "001EU0000000009AAA") is None       # not even on the domain
    lost = []
    f = dict(_f("Northwind GmbH", "pricing_friction", text="Your price is 30% above budget", account_id="001EU0000000009AAA"))
    assert build_cards(accs, [], [f], TODAY, unmatched=lost) == [] and [x["account"] for x in lost] == ["Northwind GmbH"]
    # a champion row whose upstream CRM id was not read: surfaced with a check, never merged into the NA record
    row = _move("Dana Reyes", "Northwind GmbH", "northwind.de", **{"New account id": "001EU0000000009AAA"})
    c = next(c for c in build_cards(accs, [row], [], TODAY) if c["account"] == "Northwind GmbH")
    assert c["crm_id"] == "" and any("001EU0000000009AAA was not among the accounts read" in k for k in c["checks"]), c["checks"]
    # accounts with no CRM ids at all (a CSV export): the id cannot be checked, so the name still decides
    assert match_account([_acc("Northwind Inc", "northwind.com")], "Northwind Inc", account_id="001X")["account"] == "Northwind Inc"


# ---- 11. account context on every card ---------------------------------------------------------------------------
def test_every_card_carries_a_short_account_context_line():
    accs = [dict(_acc("Globex", "globex.com", crm="001G", owner="Riley Shaw", opp="Renewal", renew="2026-12-15"),
                 industry="Software Development", employees=1200, country="United States")]
    rows = [_move("Leo Park", "Brand New Co", "brandnew.co", old="Globex",
                  **{"New company industry": "Medical Practices", "New company size": "11-50", "New company country": "Germany"})]
    cards = {c["account"]: c for c in build_cards(accs, rows, [_f("Globex", "pricing_friction")], TODAY, default_owner="SDR desk")}
    g = cards["Globex"]["context"]
    for part in ("Customer", "owner Riley Shaw", "renews 2026-12-15 (in 58 days)", "open opportunity: Renewal", "Software Development",
                 "1,200 employees", "United States"):
        assert part in g, (part, g)
    n = cards["Brand New Co"]["context"]
    assert n.startswith("Not in the CRM yet") and "owner SDR desk (routed)" in n and "Medical Practices" in n and "11 to 50 staff" in n \
        and "Germany" in n and "people data" in n, n
    # parsed from the tracker's own 'New account' text when the separate columns are missing
    bare = _move("Mo Ali", "Plain Co", "plain.co", **{"New account": "Not a known customer; 51 to 200 staff; Technology; Information and Internet; plain.co"})
    p = next(c for c in build_cards([], [bare], [], TODAY))
    assert "Technology; Information and Internet" in p["context"] and "51 to 200 staff" in p["context"], p["context"]
    blocks = card_blocks(dict(cards["Globex"], rank=1))
    assert blocks[1]["type"] == "context" and "1,200 employees" in blocks[1]["elements"][0]["text"], blocks[1]
    assert "**Context:** " + g in canvas_markdown("# b\n", [dict(cards["Globex"], rank=1)])
    assert list_rows([cards["Globex"]])[0]["Context"] == g


# ---- 12. ICP fit: is the new account worth working? -------------------------------------------------------------
ICP = {"industries_include": ["Software"], "industries_exclude": ["Government"], "min_employees": 200, "max_employees": 10000,
       "countries": ["United States", "Canada"]}

def test_icp_fit_halves_priority_and_says_why_and_unknowns_never_fail():
    accs = [dict(_acc("Fit Co", "fit.com", crm="001A"), industry="Software Development", employees="1,200", country="USA"),
            dict(_acc("Tiny Co", "tiny.com", crm="001B"), industry="Software Development", employees=50, country="Canada"),
            dict(_acc("Gov Co", "gov.com", crm="001C"), industry="Government Software", employees=5000, country="United States"),
            _acc("Blank Co", "blank.com", crm="001D")]
    f = [_f(a["account"], "expansion", sid=f"S{i}") for i, a in enumerate(accs)]
    plain = {c["account"]: c for c in build_cards(accs, [], f, TODAY)}
    by = {c["account"]: c for c in build_cards(accs, [], f, TODAY, icp=ICP)}
    assert by["Fit Co"]["priority"] == plain["Fit Co"]["priority"] == 20 and by["Fit Co"]["status"] == "Ready" and by["Fit Co"]["icp"]["fit"] == "in"
    t = by["Tiny Co"]
    assert t["priority"] == 10 and t["tier"] == "Low" and t["status"] == "Check first" and t["icp"]["fit"] == "out", t
    assert "Outside your ICP: 50 employees, under your minimum of 200" in t["checks"] and "outside ICP" in t["rank_reason"], t
    assert any(k.startswith("Outside your ICP: industry Government Software is excluded") for k in by["Gov Co"]["checks"]), by["Gov Co"]["checks"]
    b = by["Blank Co"]
    assert b["status"] == "Ready" and b["priority"] == 20 and b["icp"]["fit"] == "unknown", b
    assert "ICP not checked: no industry, no employee count, no country" in b["rank_reason"], b["rank_reason"]
    assert [r["In ICP"] for r in clay_rows([by[k] for k in ("Fit Co", "Tiny Co", "Blank Co")])] == ["Yes", "No", "Unknown"]
    assert clay_rows([plain["Fit Co"]])[0]["In ICP"] == "Not set" and "ICP" not in plain["Fit Co"]["rank_reason"]
    big = icp_fit(ICP, {"industry": "Software", "employees": 20000, "country": "Canada"})
    assert big["fit"] == "out" and big["why"] == ["20,000 employees, over your maximum of 10,000"], big
    assert icp_fit(ICP, {"industry": "Retail", "employees": 500, "country": "France"})["why"] == [
        "industry Retail is not in your list", "country France is not in your list"]

def test_icp_uses_the_champion_profile_for_a_company_not_in_the_crm():
    rows = [_move("Leo Park", "Small Shop", "smallshop.com", **{"New company industry": "Software Development", "New company size": "11 to 50",
                                                              "New company country": "United States"}),
            _move("Dana Reyes", "Right Size", "rightsize.com", **{"New company industry": "Software Development",
                                                                "New company size": "1,001-5,000", "New company country": "US"})]
    by = {c["account"]: c for c in build_cards([], rows, [], TODAY, icp=ICP, default_owner="SDR desk")}
    s, r = by["Small Shop"], by["Right Size"]
    assert s["icp"]["fit"] == "out" and "Outside your ICP: 11 to 50 staff, under your minimum of 200" in s["checks"], s["checks"]
    assert s["priority"] == 20 and r["priority"] == 40 and r["icp"]["fit"] == "in" and s["rank"] > r["rank"], (s["priority"], r["priority"])


# ---- 13. owner DMs never promise a thread (code review 14) ------------------------------------------------------
def test_an_owner_dm_digest_points_to_the_channel_digest_and_the_list():
    cards = _many(8)
    ctx = lambda d: [e["text"] for b in d["blocks"] if b["type"] == "context" for e in b["elements"]]  # noqa: E731
    dm = ctx(digest_blocks(cards, PERIOD, thread=False))
    assert any(t.startswith("+2 more of your accounts are in the channel digest and the List") for t in dm), dm
    assert not any("thread below" in t for t in dm)
    assert any("+2 more accounts in the thread below" in t for t in ctx(digest_blocks(cards, PERIOD)))      # the channel default


# ---- 14. links: escaped, or dropped when they could break out (code review 13) ----------------------------------
def test_links_are_escaped_and_unsafe_urls_are_dropped():
    c = dict(_many(1)[0], rank=1)
    for bad in ("https://evil.example/x><!channel", "https://x.com/a|b", "https://x.com/a b", "https://x.com/<b"):
        text = str(card_blocks(dict(c, who=dict(c["who"], name="Ann", title="CFO", why="w", linkedin=bad))))
        assert "|LinkedIn>" not in text and "<!channel" not in text, (bad, text)
    ok = str(card_blocks(dict(c, who=dict(c["who"], name="Ann", title="CFO", why="w", linkedin="https://www.linkedin.com/in/ann?a=1&b=2"))))
    assert "<https://www.linkedin.com/in/ann?a=1&amp;b=2|LinkedIn>" in ok, ok
    src = str(card_blocks(dict(c, links=[{"label": "Call", "url": "https://gong/x><!here"}])))
    assert "<!here" not in src and "Sources: none" in src, src
    q = [dict(QUEUE[2], candidates=[dict(QUEUE[2]["candidates"][0], linkedin="https://li/x><!channel")])]
    assert "<!channel" not in str(ops_blocks(q, "RUN", "Ops", [])) and "|profile>" not in str(ops_blocks(q, "RUN", "Ops", []))


# ---- 15. the review queue can be cleared (red team C11) ---------------------------------------------------------
def test_every_queue_row_can_be_parked_candidates_rank_by_score_and_parked_rows_are_listed():
    cands = [{"name": "Sam Lee", "title": t, "company": co, "key": f"Sam Lee @ {co}", "score": sc}
             for t, co, sc in (("VP", "Alpha", 0.2), ("CFO", "Beta", 0.9), ("Analyst", "Gamma", 0.1), ("Head", "Delta", 0.7))]
    q = [dict(QUEUE[2], candidates=cands), dict(QUEUE[0], unresolvable=True), QUEUE[4]]
    blocks = ops_blocks(q, "RUN", "Ops", [])
    acts = {b["block_id"]: b["elements"] for b in blocks if b["type"] == "actions"}
    assert set(acts) == {"id:r_sam", "id:r_leo"}, set(acts)                      # the parked row has no buttons
    for rid, els in acts.items():
        park = [e for e in els if e["action_id"] == "unresolvable"]
        assert len(park) == 1 and park[0]["value"] == f"RUN:{rid[3:]}" and park[0]["text"]["text"] == "Can't resolve", els
    assert [e["value"] for e in acts["id:r_sam"] if e["action_id"] == "confirm_match"] == [
        "RUN:r_sam|Sam Lee @ Beta", "RUN:r_sam|Sam Lee @ Delta", "RUN:r_sam|Sam Lee @ Alpha"]
    sam = next(b["text"]["text"] for b in blocks if b["type"] == "section" and "Sam Lee" in b["text"]["text"])
    assert sam.index("Beta") < sam.index("Delta") < sam.index("Alpha") and "Gamma" not in sam, sam
    assert "1. Sam Lee · CFO @ Beta · score 0.9" in sam, sam
    assert "2 rows: 1 Review, 1 Hold" in queue_summary(q) and "1 parked (can't resolve)" in queue_summary(q), queue_summary(q)
    assert blocks[-1]["type"] == "section" and blocks[-1]["text"]["text"].startswith("*Parked: can't resolve (1)*") \
        and "Zed Coe" in blocks[-1]["text"]["text"], blocks[-1]
    assert (len(blocks) - 4) == 3 * 2                                            # 3 on top, 3 per open row, the parked list last
    for md in (ops_markdown(q, "Ops", []), ops_canvas_markdown(q, "Ops", [])):
        main, parked = md.split("## Parked: can't resolve (1)")
        assert "Zed Coe" not in main and "Zed Coe" in parked and "Sam Lee" in main, md
    assert "Can't resolve" in ops_canvas_markdown(q, "Ops", [])

def test_the_queue_bucket_for_a_move_over_90_days_is_not_called_old_move():
    s = queue_summary([QUEUE[4]])
    assert "move over 90 days 1" in s and "old move" not in s, s


# ---- 16. anonymisation ------------------------------------------------------------------------------------------
def test_no_real_company_name_from_the_target_is_left_in_the_surface_files():
    name = "".join(map(chr, (110, 97, 118, 97, 110)))
    files = ["src/pg_surface/surface.py", "src/pg_surface/slack_out.py", "src/pg_surface/plays.py", "contracts/plays.json",
             "scripts/clay_formula.py", "clay/surface_formulas.md", "tests/test_surface.py"]
    hits = [p for p in files if name in open(os.path.join(ROOT, p), encoding="utf-8").read().lower()]
    assert hits == [], hits


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

"""Focused tests for the GTM brief's word rules, the observer check, findings fields and safe quoting.
Each test is one critique item, with small inline examples. Run without pytest:  python3 tests/test_brief.py
"""
import copy
import os
import sys
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from gtm_briefs import observer  # noqa: E402
from gtm_briefs.brief import build_brief, neutralize, to_markdown  # noqa: E402
from gtm_briefs.extract import BASE, classify, extract  # noqa: E402
from gtm_briefs.lexicon import LEX, LEXICON_VERSION, compile_all  # noqa: E402


def sig(text, side="customer", source="call", **kw):
    return dict({"source": source, "id": "T-1", "date": "2026-10-15", "account": "Acme Robotics", "speaker": "X",
                 "speaker_title": "Y", "side": side, "text": text, "url": "u"}, **kw)


def themes(text, side="customer", source="call", rx=None):
    return classify(sig(text, side, source), rx=rx)["themes"]


@contextmanager
def env(**kv):
    """Set (value) or unset (None) environment variables for one test, then put them back."""
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


# ---- 1. the word list on the customer's own domain (travel and expense) -----------------------------------------
def test_lexicon_version_is_2_4_and_demo_competitors_are_unchanged():
    """2.4 adds price comparisons and roles who left; the demo keeps its invented names, so demo cards are unchanged."""
    assert LEXICON_VERSION == "2.4" and LEX["competitors"] == ["Ledgerly", "TripStack", "Spendwise"]


def test_their_travel_spend_is_not_pricing_friction():
    for text in ["We need to cut travel spend by 20 % this year",
                 "Hotel rates in London are too high for our policy",
                 "Airfare costs keep going up",
                 "Our travel budget is approved for next year. The main issue is approvers never respond",
                 "Flights are cheaper if we book on Tuesdays",
                 "Our travel budget got cut again"]:
        assert "pricing_friction" not in themes(text), text


def test_travel_words_still_count_when_the_pushback_points_at_us():
    for text in ["Your per-seat price is too high when our travel budget is already cut",
                 "Your prices keep going up while airfare is flat",
                 "The licence fee is too expensive next to what we spend on hotels"]:
        assert "pricing_friction" in themes(text), text


def test_buying_pushback_still_counts():
    for text in ["Honestly it's too expensive for a team our size",
                 "That's out of budget for this year",
                 "Procurement wants a discount before they sign",
                 "Can we pay monthly? Cash is tight until the round closes",
                 "I can't justify the cost to my CFO",
                 "Ledgerly quoted us 30 percent less"]:
        assert "pricing_friction" in themes(text), text


def test_price_word_and_friction_cue_must_be_close_together():
    assert "pricing_friction" not in themes("The pricing page was clear. Our main issue is approvals.")
    assert "pricing_friction" not in themes("The cost of moving tools would be one thing, but honestly the real issue for us is adoption")
    assert "pricing_friction" in themes("The per user cost is the issue")


def test_a_month_alone_is_not_a_timing_window():
    assert "timing" not in themes("We hired three new people in March")
    assert "timing" not in themes("We ripped out our old tool back in January")
    assert "timing" not in themes("We may sign up more people later")        # 'may' the verb is not the month
    assert "timing" in themes("We have to have a vendor signed by 31 March")
    assert "timing" in themes("Decision expected end of Nov")
    assert "timing" in themes("Our current contract ends in June")


def test_a_renewal_month_is_timing_from_the_customer_not_from_our_crm_note():
    assert "timing" in themes("Our renewal with you is in November")
    assert "timing" not in themes("Renewal is in November.", side="internal", source="crm")


def test_past_expansion_with_no_plans_for_more_is_not_a_request():
    assert "expansion" not in themes("We expanded into Mexico last year, no plans for more")
    assert "expansion" not in themes("We're not looking to expand right now")
    assert "expansion" not in themes("We don't need more seats")
    assert "expansion" in themes("We expanded into Mexico last year and now want to add the Brazil office")
    assert "expansion" in themes("We'd add the Paris office too")


def test_pushback_on_contract_terms_and_doesnt_make_sense_count_as_pricing():
    """Cause: pushback on commercial terms (contract length) and 'doesn't make sense' next to a price word were missed."""
    for text in ["A two-year contract is a deal-breaker for us",
                 "Multi-year commitment is a non-starter, we do twelve months",
                 "Honestly it doesn't make sense at that price for 30 users",
                 "Not sure the program makes sense with a rebate that low"]:
        assert "pricing_friction" in themes(text), text
    for text in ["Long term, the issue is adoption, not anything else",
                 "The per user model makes sense for us",
                 "We signed a two-year contract with them in 2024"]:
        assert "pricing_friction" not in themes(text), text


def test_price_above_what_they_budgeted_is_pricing():
    """Cause: 'higher than what we budgeted' had a price word but no friction cue the lexicon knew."""
    assert "pricing_friction" in themes("The quote came in well over what we'd budgeted")
    assert "pricing_friction" in themes("That's a lot more than we budgeted for this")
    assert "pricing_friction" not in themes("Onboarding took more time than we planned")


def test_our_teams_read_is_a_guess_not_a_report():
    """Cause: our rep's opinion written as 'my read', 'imo', 'feels like', 'I imagine' counted as a report."""
    for text in ["My read: they won't sign before their board meets on the 12th",
                 "Low churn risk imo, budget is frozen until Q1",
                 "Feels like they'll want more seats next year",
                 "I imagine the other offices will want it too"]:
        assert classify(sig(text, side="internal", source="field"))["confidence"] == "excluded", text
    assert classify(sig("Feels like the rollout is slow", side="customer"))["confidence"] == "direct"  # customer words count


def test_timing_needs_a_purchase_word_next_to_the_date():
    """Cause: dates for the customer's own operations (expense deadlines, travel or hiring freezes) counted as timing;
    procurement, purchase paperwork and 'make a call' were missing as purchase words."""
    for text in ["The expense deadline is the 5th of every month",
                 "Hiring freeze until Q2, so headcount is flat",
                 "Travel freeze for Q4 across the group",
                 "Our procurement lead started in September",
                 "We hired a new head of procurement in March"]:
        assert "timing" not in themes(text), text
    for text in ["Budget is frozen until Q1, then we can talk",
                 "Procurement review kicks off in Feb",
                 "We just need the paperwork back by the 12th",
                 "The PO should be raised before Christmas",
                 "We'll make the call on vendors in October"]:
        assert "timing" in themes(text), text


def test_moving_to_a_higher_tier_is_expansion():
    """Cause: an upgrade asked for as a tier or plan ('outgrown', 'next tier', 'upsell') was missed."""
    for text in ["We've outgrown the starter plan", "What's in the enterprise tier?",
                 "Upsell opp: wants the premium plan", "Can we move up to the next tier?"]:
        assert "expansion" in themes(text), text
    assert "expansion" not in themes("No upsell here, they're happy on what they have")


def test_named_roles_leaving_retiring_or_new_are_people_changes():
    """Cause: a named role leaving, retiring or resigning, a move to another side or team, and 'takes over' were missed."""
    for text in ["Our VP Finance is retiring in the spring", "Their head of travel resigned last week",
                 "She's moving over to the procurement team next month", "Whoever takes over from her will own it",
                 "I'm leaving the company at the end of the year", "Our CFO is new as of last week",
                 "Sponsor (Ana) retiring in Jan, no successor yet"]:
        assert "people_change" in themes(text), text
    for text in ["The travel manager wants new cards for the team", "Our controller's new expense policy starts soon",
                 "This lead is new, came in from the website", "We'll leave the team to sort out logins",
                 "We're retiring our old expense tool"]:
        assert "people_change" not in themes(text), text


def test_using_attending_or_passing_round_our_content_is_engagement():
    """Cause: engagement phrased as using or passing round our content ('used the ROI calculator', 'did that webinar',
    'the report is getting forwarded'), often without 'the' in note shorthand, was missed."""
    for text in ["I read the checklist you put out on VAT", "Your policy guide got forwarded to our whole finance team",
                 "We sat in on your roundtable last month",
                 "Prospect filled in the savings calculator, then read two case studies",
                 "Caught your workshop on approvals, really useful"]:
        assert "engagement" in themes(text), text
    for text in ["We did a video call with Carlos on Monday", "We did some research on vendors last year",
                 "Did breakfast w/ the AP team, nice folks", "The expense report was forwarded to the approver"]:
        assert "engagement" not in themes(text), text


def _real_names_lexicon():
    lex = copy.deepcopy(LEX)
    lex["competitors"] = ["Ramp", "Concur", "Perk", "Center"]
    lex["competitor_rules"] = {"Ramp": {"case_sensitive": True, "exclude": ["ramp up", "ramp-up", "on-ramp"]},
                               "Concur": {"case_sensitive": True, "exclude": ["I concur", "we concur"]},
                               "Perk": {"case_sensitive": True, "exclude": ["perk of"]},
                               "Center": {"case_sensitive": True, "exclude": ["cost center", "call center", "data center"]}}
    return compile_all(lex)


def test_competitor_names_that_are_everyday_words_follow_competitor_rules():
    rx = _real_names_lexicon()
    comps = lambda text: classify(sig(text), rx=rx)["competitors"]  # noqa: E731
    for text in ["We need to ramp up the rollout", "Ramp up takes a while here", "I concur with your CFO",
                 "Card controls are a nice perk", "Perk of the job, I suppose", "Can I slice spend by cost center?",
                 "Our Cost Center codes are a mess", "The call center team travels a lot"]:
        assert comps(text) == [], (text, comps(text))
    assert comps("We're trialling Ramp next week") == ["Ramp"]
    assert comps("Concur quoted us lower") == ["Concur"]
    assert comps("Perk is cheaper per trip") == ["Perk"]
    assert comps("Center sent a proposal on Monday") == ["Center"]
    assert classify(sig("We're trialling Ramp next week"), rx=rx)["stance"] == "risk"
    assert observer.validate("We need to ramp up the rollout", {"competitor": "ramp up"}, rx) == []
    assert observer.validate("We're trialling Ramp next week", {"competitor": "Ramp"}, rx) == ["competitor"]


def test_demo_competitors_have_rules_and_the_lexicon_says_how_to_list_yours():
    rules = LEX["competitor_rules"]
    assert all(c in rules for c in LEX["competitors"]) and "Ramp" in rules["_doc"] and "exclude" in rules["_doc"]


# ---- 2. the observer check --------------------------------------------------------------------------------------
def test_observer_evidence_must_contain_a_cue_for_that_theme():
    v = observer.validate
    assert v("We love the product and the team.", {"pricing_friction": "the product"}) == []
    assert v("Thanks for making the time today.", {"expansion": "the time"}) == []
    assert v("We want 20 more seats next year", {"timing": "20 more seats"}) == []          # a cue, but for expansion
    assert v("We want 20 more seats next year", {"expansion": "20 more seats"}) == ["expansion"]
    assert v("Honestly $12 per user is steep", {"pricing_friction": "$12 per user is steep"}) == ["pricing_friction"]
    assert v("They need everything by Friday the 20th", {"timing": "by Friday the 20th"}) == []        # a date alone: v1.1
    assert v("Procurement needs everything by Friday the 20th", {"timing": "by Friday the 20th"}) == ["timing"]
    assert v("Our new controller owns this now", {"people_change": "Our new controller"}) == ["people_change"]
    assert v("I was on your webinar last week", {"engagement": "your webinar"}) == ["engagement"]
    assert v("Hotel rates in London are too high", {"pricing_friction": "Hotel rates in London are too high"}) == []


def _signals():
    return [sig("Ledgerly quoted us 20% less", id="C1"),                                          # customer: sent
            sig("Our pricing is per seat, so you only pay for users", side="internal", id="R1"),  # rep's own words: never
            sig("I think they'll push on price", side="internal", id="R2"),                       # our guess: never
            sig("To recap, your CFO wants a discount", side="internal", id="R3"),                 # rep repeats customer: sent
            sig("Who's on the line? We need a decision by March", side="unknown", id="U1"),       # unplaced speaker: sent
            sig("Champion says budget is frozen", side="internal", source="crm", id="N1"),        # CRM note: sent
            sig("Omar asked about the per-trip fee", side="internal", source="field", id="F1"),   # field note: sent
            sig("   ", id="E1")]                                                                  # nothing to label


def test_observer_sends_only_turns_the_code_would_count_and_reports_stats():
    prompts = []
    fake = lambda p: (prompts.append(p), '[{"id": "C1", "themes": {"pricing_friction": "20% less"}}]')[1]  # noqa: E731
    with env(OBSERVER_MAX_TURNS=None):
        r = observer.observe_with_stats(_signals(), model=fake)
    sent = [i for i in ("C1", "R1", "R2", "R3", "U1", "N1", "F1", "E1") if f'"id": "{i}"' in prompts[0]]
    assert sent == ["C1", "R3", "U1", "N1", "F1"], sent
    assert r["stats"] == {"sent": 5, "skipped_not_counted": 3, "truncated_by_cap": 0, "malformed_batches": 0, "failed_batches": 0}, r["stats"]
    assert r["observations"] == {"C1": ["pricing_friction"], "R3": [], "U1": [], "N1": [], "F1": []}
    with env(OBSERVER_MAX_TURNS=None):
        assert observer.observe(_signals(), model=fake) == r["observations"]        # observe() keeps its old shape


def test_observer_counts_turns_past_the_cap_and_malformed_replies():
    with env(OBSERVER_MAX_TURNS="2"):
        r = observer.observe_with_stats(_signals(), model=lambda p: "Sorry, I can't help with that.", batch=1)
    assert r["stats"] == {"sent": 2, "skipped_not_counted": 3, "truncated_by_cap": 3, "malformed_batches": 2, "failed_batches": 0}, r["stats"]
    assert r["observations"] == {"C1": [], "R3": []}


def _capture_http(reply):
    import gtm_connectors
    calls, orig = [], gtm_connectors.http_json
    gtm_connectors.http_json = lambda method, url, headers, body=None, **kw: (calls.append((url, headers, body)), reply)[1]
    return calls, lambda: setattr(gtm_connectors, "http_json", orig)


def test_observer_provider_is_swappable_without_a_network_call():
    calls, restore = _capture_http({"choices": [{"message": {"content": '[{"id": "C1", "themes": {}}]'}}]})
    try:
        with env(OBSERVER_PROVIDER="openai", OPENAI_API_KEY="test-key", OBSERVER_MODEL="small-model",
                 OPENAI_BASE_URL="http://localhost:9/v1/", OBSERVER_MAX_TURNS=None):
            assert observer.required_env() == []
            r = observer.observe_with_stats(_signals()[:1])
        url, headers, body = calls[0]
        assert url == "http://localhost:9/v1/chat/completions" and headers["Authorization"] == "Bearer test-key"
        assert body["model"] == "small-model" and body["messages"][0]["content"].startswith("You label short texts")
        assert r["observations"] == {"C1": []} and r["stats"]["malformed_batches"] == 0
    finally:
        restore()
    calls, restore = _capture_http({"content": [{"type": "text", "text": "[]"}]})
    try:
        with env(OBSERVER_PROVIDER=None, ANTHROPIC_API_KEY="test-key", OBSERVER_MAX_TURNS=None):
            observer.observe(_signals()[:1])
        assert calls[0][0] == "https://api.anthropic.com/v1/messages"
    finally:
        restore()
    with env(OBSERVER_PROVIDER="openai", OPENAI_API_KEY=None, OBSERVER_MODEL=None):
        assert observer.required_env() == ["OPENAI_API_KEY", "OBSERVER_MODEL"]
    with env(OBSERVER_PROVIDER="nope"):
        try:
            observer.required_env()
            raise AssertionError("an unknown provider must fail loudly")
        except ValueError:
            pass


def test_observer_prompt_is_v1_1_with_the_same_json_output():
    """v1.1 changes the theme definitions only; the reply format (and so the parser) is the same as v1.0."""
    p = observer.PROMPT
    assert observer.PROMPT_VERSION == "1.1" and "Return only JSON" in p
    assert '[{{"id": "<id>", "themes": {{"<theme>": "<exact words>"}}}}]' in p and p.rstrip().endswith("{texts}")
    assert "Not the customer's own growth" in p and "customer's own operations" in p and "is not engagement yet" in p
    assert observer.build_prompt([{"id": "C1", "text": "hi"}]).endswith('{"id": "C1", "text": "hi"}')


def test_observer_timing_needs_a_purchase_word_not_just_a_date():
    """Cause: the model labels any date as timing (an audit, an internal meeting, our next demo). A date counts only
    when the sentence holding it has a purchase word; a purchase word in another sentence does not lend it one."""
    v = observer.validate
    assert v("The audit finding has to be closed by Q1.", {"timing": "closed by Q1"}) == []
    assert v("Our internal QBR is on the 9th, I'll present the numbers there.", {"timing": "on the 9th"}) == []
    assert v("Next step: demo on 11/02.", {"timing": "demo on 11/02"}) == []
    assert v("We sign contracts every spring. The offsite is in April.", {"timing": "in April"}) == []
    assert v("We'll decide after the pilot ends on the 30th.", {"timing": "after the pilot ends on the 30th"}) == ["timing"]
    assert v("Our contract renews in June", {"timing": "contract renews in June"}) == ["timing"]
    assert v("The expense deadline moved to the 3rd", {"timing": "expense deadline moved to the 3rd"}) == []


def test_observer_expansion_must_point_at_more_of_our_product():
    """Cause: the model labels the customer's own growth (headcount, hiring, a new office) as expansion."""
    v = observer.validate
    assert v("Headcount goes from 300 to 500 next year.", {"expansion": "Headcount goes from 300 to 500"}) == []
    assert v("We hired 40 people last quarter.", {"expansion": "We hired 40 people"}) == []
    assert v("How's the new office in Leeds?", {"expansion": "the new office in Leeds"}) == []
    assert v("We'd want it in the Lisbon office too", {"expansion": "want it in the Lisbon office"}) == ["expansion"]
    assert v("We've outgrown the starter tier", {"expansion": "outgrown the starter tier"}) == ["expansion"]
    assert v("Marketing wants 15 more users", {"expansion": "15 more users"}) == ["expansion"]


def test_observer_content_requests_are_not_engagement():
    """Cause: asking us to send content, or our team sending it, was accepted as engagement; nothing was read yet."""
    v = observer.validate
    assert v("Can you send me the case study?", {"engagement": "send me the case study"}) == []
    assert v("Sent the deck per his request.", {"engagement": "the deck per his request"}) == []
    assert v("I read your blog on per diems", {"engagement": "read your blog"}) == ["engagement"]
    assert v("She used the savings calculator on your site", {"engagement": "used the savings calculator"}) == ["engagement"]


# ---- 3. findings carry the optional signal fields ---------------------------------------------------------------
def test_findings_carry_account_id_and_trackers():
    assert "account_id" in BASE and "trackers" in BASE
    f = extract([sig("Ledgerly quoted us lower", account_id="001ABC", trackers=["Competitors"])])
    assert f and all(x["account_id"] == "001ABC" and x["trackers"] == ["Competitors"] for x in f), f
    g = extract([sig("Ledgerly quoted us lower")])
    assert all(x["account_id"] == "" and x["trackers"] == [] for x in g), g


# ---- 4. Slack mentions inside quotes ----------------------------------------------------------------------------
def test_neutralize_turns_slack_mentions_into_plain_words():
    assert neutralize("<!channel> Omar asked for a discount") == "@channel Omar asked for a discount"
    assert neutralize("<!here> and <!here|here> and <!everyone>") == "@here and @here and @everyone"
    assert neutralize("ping <@U123ABC> and <@U9|sam>") == "ping @U123ABC and @sam"
    assert neutralize("see <#C123|deals> and <#C55>") == "see #deals and #C55"
    assert neutralize("<!subteam^S0614TZR7|@finance> please, <!subteam^S1>") == "@finance please, @group"
    assert neutralize("![](@U123) in a canvas") == "@U123 in a canvas"
    assert neutralize("5 < 6 and a <https://x.com|link>") == "5 < 6 and a <https://x.com|link>"


def test_brief_markdown_never_reposts_a_mention():
    notes = [sig("<!channel> Omar asked if we can match TripStack's per-trip fee", side="internal", source="field", id="F9",
                 speaker="<@U77|sam>"),
             sig("<!here> I think they'll say our price is too high", side="internal", source="crm", id="F10")]
    md = to_markdown(build_brief(extract(notes), ("2026-10-12", "2026-10-18"), ("2026-10-05", "2026-10-11")))
    assert "<!" not in md and "<@" not in md, md
    assert '"@channel Omar asked' in md and "@sam" in md and '"@here I think' in md, md


# ---- 5. a real travel-and-expense market (contracts/lexicon.travel-expense.json, chosen with LEXICON_PATH) -------
TE_PATH = os.path.join(ROOT, "contracts", "lexicon.travel-expense.json")


def _te():
    from gtm_briefs.lexicon import load
    return load(TE_PATH)


def _te_rx():
    return compile_all(_te())


def test_travel_expense_lexicon_lists_a_real_market_with_rules_for_everyday_words():
    """Red team C2: '1.00 competitor precision' came from three invented names. This file lists a real travel-and-expense
    market, with a rule for every name that is also an everyday word or phrase."""
    lex = _te()
    assert {"Concur", "Expensify", "Ramp", "Brex", "Perk", "Amex GBT", "Spotnana", "Emburse", "Coupa", "Center", "Airbase",
            "Pleo", "Spendesk", "Rippling Spend"} <= set(lex["competitors"]), lex["competitors"]
    rules = lex["competitor_rules"]
    for name in ("Ramp", "Concur", "Perk", "Center"):
        assert rules[name]["case_sensitive"] and rules[name]["exclude"], name
    assert {"ramp up", "ramp-up", "on-ramp", "ramping"} <= set(rules["Ramp"]["exclude"])
    assert {"I concur", "we concur", "concur with"} <= set(rules["Concur"]["exclude"])
    assert {"perk of", "a perk", "perks"} <= set(rules["Perk"]["exclude"])
    assert {"cost center", "data center", "call center", "center of"} <= set(rules["Center"]["exclude"])
    assert "Amex" not in lex["competitors"] and "Egencia" in rules["Amex GBT"]["aliases"]
    assert "LEXICON_PATH" in lex["_doc"] and "LEXICON_PATH" in LEX["_doc"]


def test_travel_expense_lexicon_on_the_red_team_probes():
    rx = _te_rx()
    c = lambda text: classify(sig(text), rx=rx)  # noqa: E731
    r = c("We use Concur at the parent company, and they want us to align. So I might be overruled on this.")
    assert r["competitors"] == ["Concur"] and "competitor" in r["themes"] and r["stance"] == "risk", r
    r = c("The FX fees on your card are way higher than Amex.")
    assert r["competitors"] == [] and "competitor" not in r["themes"] and "pricing_friction" in r["themes"], r
    r = c("We need to ramp up usage")
    assert r["themes"] == [] and r["competitors"] == [], r
    r = c("Ramp quoted us lower")
    assert r["competitors"] == ["Ramp"] and r["stance"] == "risk", r
    r = c("Finance wants us on Ramp by Q1, so we have to decide before the renewal.")
    assert r["competitors"] == ["Ramp"] and r["stance"] == "risk" and "timing" in r["themes"], r


def test_travel_expense_everyday_words_are_not_competitors_and_real_mentions_are():
    rx = _te_rx()
    comps = lambda text: classify(sig(text), rx=rx)["competitors"]  # noqa: E731
    for text in ["We need to ramp up usage", "Ramp up takes a while here", "The ramp-up was slow", "Ramp-up was slow",
                 "It's an on-ramp to more modules", "Ramping up the rollout now",
                 "I concur with your CFO", "We concur.", "Concur with that, let's go.", "Concur. Let's push the date.",
                 "Card controls are a nice perk", "Perk of the job, I suppose", "The perks are great", "Perks for staff matter",
                 "Perk up, the numbers are fine",
                 "Can I slice spend by cost center?", "Our Cost Center codes are a mess", "The call center team travels a lot",
                 "Data Center visits are a big chunk of travel", "Center of excellence owns this",
                 "Riverside Medical Center wants a demo", "We fly everyone to the Javits Convention Center",
                 "We use our Amex for everything", "Do you support Amex corporate cards?", "It has to work with our Amex program",
                 "American Express cards only, please"]:
        assert comps(text) == [], (text, comps(text))
    for text, want in [("SAP Concur is the parent's tool", ["Concur"]), ("We use Concur with our Amex cards", ["Concur"]),
                       ("We use Concur.", ["Concur"]),
                       ("We're running a Perk trial", ["Perk"]), ("TravelPerk sent a proposal", ["Perk"]),
                       ("Perk is cheaper per trip", ["Perk"]), ("Center sent a proposal on Monday", ["Center"]),
                       ("We moved our cards to Ramp", ["Ramp"]), ("Egencia handles our bookings", ["Amex GBT"]),
                       ("American Express Global Business Travel quoted us", ["Amex GBT"]),
                       ("Amex GBT is the incumbent", ["Amex GBT"])]:
        assert comps(text) == want, (text, comps(text))
    for name in ("Brex", "Pleo", "Coupa", "Spotnana", "Expensify", "Emburse", "Airbase", "Spendesk", "Rippling Spend"):
        assert comps(f"We're also looking at {name}.") == [name], name
    assert set(comps("Brex and Ramp both pitched us")) == {"Brex", "Ramp"}
    v = observer.validate
    assert v("We need to ramp up usage", {"competitor": "ramp up"}, rx) == []
    assert v("The FX fees on your card are way higher than Amex.", {"competitor": "Amex"}, rx) == []
    assert v("Ramp quoted us lower", {"competitor": "Ramp"}, rx) == ["competitor"]
    assert v("Egencia handles our bookings", {"competitor": "Egencia"}, rx) == ["competitor"]


def test_travel_expense_lexicon_is_the_demo_lexicon_with_only_the_competitors_changed():
    """One set of rules: the market file differs from contracts/lexicon.json only in its competitors (a guard against drift)."""
    te = _te()
    same = lambda d: {k: v for k, v in d.items() if k not in ("_doc", "version", "competitors", "competitor_rules")}  # noqa: E731
    assert same(te) == same(LEX)
    assert te["version"].startswith(LEX["version"] + "-") and te["competitor_rules"]["_doc"] == LEX["competitor_rules"]["_doc"]


def test_lexicon_path_selects_the_travel_expense_file_and_the_default_stays_the_demo():
    import json
    import subprocess
    code = ("import json; from gtm_briefs.lexicon import LEXICON_VERSION, COMPETITORS; "
            "from gtm_briefs.extract import classify, extract; from gtm_briefs.observer import build_prompt; "
            "s = {'source': 'call', 'side': 'customer', 'text': 'Ramp quoted us lower than Ledgerly', 'id': 'a', "
            "'date': '2026-10-15'}; "
            "print(json.dumps([LEXICON_VERSION, COMPETITORS, classify(s)['competitors'], 'Egencia' in build_prompt([]), "
            "sorted({f['lexicon'] for f in extract([s])})]))")
    environ = {k: v for k, v in os.environ.items() if k != "LEXICON_PATH"}
    environ["PYTHONPATH"] = os.path.join(ROOT, "src")
    run = lambda extra: json.loads(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,  # noqa: E731
                                                  check=True, env=dict(environ, **extra)).stdout)
    version, comps, found, alias_in_prompt, stamps = run({})
    assert comps == ["Ledgerly", "TripStack", "Spendwise"] and found == ["Ledgerly"] and not alias_in_prompt
    assert version == LEXICON_VERSION and stamps == [LEXICON_VERSION]
    version, comps, found, alias_in_prompt, stamps = run({"LEXICON_PATH": TE_PATH})
    assert "Concur" in comps and "Ledgerly" not in comps and found == ["Ramp"] and alias_in_prompt
    assert version == _te()["version"] and stamps == [version]


# ---- 6. price comparisons and people who left -------------------------------------------------------------------
def test_a_price_comparison_against_another_offer_is_pricing_friction():
    """Red team probe: 'The FX fees on your card are way higher than Amex' found nothing. A comparison counts when it is
    about our price, fees or card ('your', 'per seat', 'the licence'), not when it favours us or is about their own spend."""
    for text in ["The FX fees on your card are way higher than Amex.",
                 "Your per-trip booking fee is a lot higher than what we pay today",
                 "Your platform is more expensive than Ledgerly",
                 "Honestly your subscription costs more than Spendwise",
                 "Your seat price is double what TripStack charges",
                 "Spendwise is cheaper",
                 "The interest rate on your card is higher than Amex"]:
        assert "pricing_friction" in themes(text), text
    for text in ["Hotel rates in London are higher than last year",
                 "Flights are more expensive than last year",
                 "Ledgerly's fees are higher than yours",
                 "Approval rates on your platform are higher than with our old tool",
                 "Your NPS is higher than Ledgerly's",
                 "Adoption is higher than we expected",
                 "Your platform does more than Ledgerly for the same cost",
                 "Rebates on your card are higher than Amex's"]:
        assert "pricing_friction" not in themes(text), text


def test_a_named_role_who_left_quit_or_moved_on_is_a_people_change():
    """Red team probe: 'Our CFO left last week ...' found nothing. A role word with left / quit / moved on / let go counts;
    a role that 'left a comment' or 'quit using' something does not."""
    for text in ["Our CFO left last week and the new one is reviewing every vendor contract.",
                 "Our CFO left last week", "Their controller quit in August", "The travel manager moved on in the spring",
                 "Our champion left to join a competitor", "Our main contact left, so we're starting over",
                 "The VP who signed with you left in March", "Our admin left two weeks ago",
                 "Their head of finance was let go", "Our champion left for Ledgerly last month"]:
        assert "people_change" in themes(text), text
    for text in ["The CFO left a comment on the proposal", "Our CFO left the decision to me",
                 "The manager left early for the holidays", "Our CEO left a voicemail yesterday",
                 "We left the meeting early", "The director quit using the old tool months ago",
                 "Our VP got fired up about the demo", "Our CFO left for Lisbon this morning",
                 "The head of travel left for the airport", "My boss left me in charge"]:
        assert "people_change" not in themes(text), text


# ---- 7. findings say when the model observer supplied the theme -------------------------------------------------
def test_findings_from_observer_themes_carry_the_observer_prompt_version():
    """A finding that exists only because the observer proposed it (and the code verified it) is stamped
    observer_prompt = PROMPT_VERSION. Lexicon findings and Gong tracker findings are not. Merged the way the
    pipeline merges tracker and observer themes."""
    sigs = [sig("Dana owns the travel program in her new role", id="O1"),        # lexicon misses; observer adds
            sig("Ledgerly quoted us 20% less", id="O2"),                         # lexicon finds it anyway
            sig("Can we talk numbers next week?", id="O3", trackers=["Budget"])]  # Gong tracker only
    fake = lambda p: ('[{"id": "O1", "themes": {"people_change": "in her new role"}},'  # noqa: E731
                      ' {"id": "O2", "themes": {"pricing_friction": "20% less"}}, {"id": "O3", "themes": {}}]')
    observations = {}
    for sid, th in observer.tracker_themes(sigs, LEX["gong_trackers"]).items():
        observations.setdefault(sid, []).extend(th)
    with env(OBSERVER_MAX_TURNS=None):
        got = observer.observe_with_stats(sigs, model=fake)
    for sid, th in got["observations"].items():
        observations.setdefault(sid, []).extend(t for t in th if t not in observations.get(sid, []))
    by = {(f["id"], f["theme"]): f for f in extract(sigs, observations)}
    assert by[("O1", "people_change")]["observer_prompt"] == observer.PROMPT_VERSION == "1.1", by
    assert "observer_prompt" not in by[("O2", "pricing_friction")] and "observer_prompt" not in by[("O3", "pricing_friction")]
    assert all("observer_prompt" not in f for k, f in by.items() if k != ("O1", "people_change"))
    assert [f["observer_prompt"] for f in extract(sigs, got["observations"]) if f["id"] == "O1"] == ["1.1"]
    assert all("observer_prompt" not in f for f in extract(sigs))                  # observer off: shape unchanged


# ---- 8. the eval is described as what it is ---------------------------------------------------------------------
def test_eval_descriptions_say_how_the_sets_were_made():
    """The old set was written and labelled by one LLM agent from a spec the rule author wrote: not 'blind', and no
    person labelled it."""
    banned = ("bl" + "ind", "hand" + "-label", "hand" + " label", "human" + "-label")
    for rel in ("contracts/lexicon.json", "contracts/lexicon.travel-expense.json", "src/gtm_briefs/lexicon.py",
                "tests/test_brief_eval.py"):
        text = open(os.path.join(ROOT, rel), encoding="utf-8").read().lower()
        assert not [b for b in banned if b in text], (rel, [b for b in banned if b in text])
    ev = open(os.path.join(ROOT, "tests", "test_brief_eval.py"), encoding="utf-8").read()
    assert "written and labelled by one LLM agent from a spec the rule author wrote" in ev


def test_a_failed_observer_batch_costs_its_turns_not_the_run():
    calls = {"n": 0}
    def flaky(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("HTTP 529 overloaded")
        return "[]"
    sigs = [{"id": f"C{i}", "source": "call", "side": "customer", "text": "We want more seats."} for i in range(3)]
    r = observer.observe_with_stats(sigs, model=flaky, batch=2)
    assert r["stats"]["failed_batches"] == 1 and r["stats"]["sent"] == 3 and "C2" in r["observations"], r


def test_openai_path_sends_a_token_cap_reasoning_effort_and_retries_without_temperature():
    import gtm_connectors
    sent, orig = [], gtm_connectors.http_json
    def fake(method, url, headers, body=None, **kw):
        sent.append(dict(body))
        if "temperature" in body:
            raise RuntimeError("HTTP 400: 'temperature' is not supported with this model")
        return {"choices": [{"message": {"content": "[]"}}]}
    gtm_connectors.http_json = fake
    try:
        with env(OPENAI_API_KEY="k", OBSERVER_MODEL="m", OPENAI_BASE_URL="https://openrouter.ai/api/v1", OBSERVER_REASONING_EFFORT="low"):
            assert observer.call_openai("p") == "[]"
    finally:
        gtm_connectors.http_json = orig
    assert sent[0]["max_tokens"] == 8000 and sent[0]["reasoning"] == {"effort": "low"} and "temperature" not in sent[1]


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
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

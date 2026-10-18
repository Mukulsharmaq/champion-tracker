"""Tests for the source connectors, the champion definition SQL, the demo fixtures, Slack delivery and the warehouse
history. No network: every API is a fake call function; the warehouse is SQLite.
Run without pytest:  python3 tests/test_connectors.py      (pytest also works)
"""
import contextlib
import csv
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from gtm_connectors import AuthError, gong, salesforce, slack_notes, slack_post, warehouse  # noqa: E402

FX = lambda *p: os.path.join(ROOT, "fixtures", *p)  # noqa: E731
TODAY = "2026-10-18"


@contextlib.contextmanager
def env(**values):
    """Set (or, with None, remove) environment variables for one block, then put everything back."""
    saved = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def patched(module, name, value):
    old = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, old)


def _make_fixtures_module():
    spec = importlib.util.spec_from_file_location("mf", os.path.join(ROOT, "scripts", "make_fixtures.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- 1. account ids end to end ----------------------------------------------------------------------------------
def test_every_source_names_the_account_by_its_salesforce_id():
    accounts = salesforce.load_accounts(FX("salesforce", "accounts.json"))
    ids = {a["account"]: a["crm_id"] for a in accounts}
    calls = gong.load(FX("gong", "calls_extensive.json"), FX("gong", "calls_transcript.json"))
    notes = salesforce.load_notes(FX("salesforce", "tasks.json"))
    field, _ = slack_notes.load(FX("slack", "field_notes_history.json"), FX("slack", "users_list.json"), accounts)
    for source, sigs in (("gong", calls), ("crm", notes), ("slack", field)):
        assert sigs, source
        wrong = [(s["account"], s.get("account_id")) for s in sigs if s.get("account_id") != ids[s["account"]]]
        assert not wrong, (source, wrong)
    objs = [o for c in json.load(open(FX("gong", "calls_extensive.json")))["calls"] for ctx in c["context"] for o in ctx["objects"]]
    assert {o["objectId"] for o in objs} <= set(ids.values())      # the Gong context points at the CRM record, not a name


def test_gong_keeps_the_account_object_id_from_the_call_context():
    ext = {"calls": [{"metaData": {"id": "7", "url": "u", "started": "2026-10-15T10:00:00Z"},
                      "parties": [{"speakerId": "s1", "name": "Ann", "title": "CFO", "affiliation": "External"}],
                      "context": [{"system": "Salesforce", "objects": [{"objectType": "Account", "objectId": "001XYZ",
                                   "fields": [{"name": "Name", "value": "Globex Media"}]}]}]}]}
    tr = {"callTranscripts": [{"callId": "7", "transcript": [{"speakerId": "s1", "sentences": [{"start": 0, "end": 1, "text": "Hi."}]}]}]}
    s = gong.to_signals(ext, tr)[0]
    assert (s["account"], s["account_id"]) == ("Globex Media", "001XYZ")
    ext["calls"][0]["context"] = []
    assert gong.to_signals(ext, tr)[0]["account_id"] == ""


ACCTS = [{"account": a, "crm_id": f"001{i}"} for i, a in enumerate(
    ["Close", "Target", "Hooli", "Initech", "Expense", "Northwind Foods", "Tailspin Toys"])]


def _notes(*texts):
    msgs = [{"user": "U1", "text": t, "ts": f"17921628{i:02d}.000001"} for i, t in enumerate(texts)]
    return slack_notes.to_signals({"messages": msgs}, {"members": []}, ACCTS, "C1", "ex")


def test_field_notes_resolve_by_prefix_and_never_by_an_ordinary_word():
    sigs, skipped = _notes("We are close to a decision on the travel tool.",          # not the account Close
                           "Hitting the target for cards is the priority this quarter.",  # not Target
                           "Expense reports are late again.",                           # common word, 7 letters
                           "Hooli wants a demo",                                         # 5 letters: prefix only
                           "Close: they signed the expansion",                           # prefix decides
                           "Account: Target\nBudget frozen until Q1",                    # form-style prefix
                           "Initech asked about SSO",                                    # 7 letters, not a word
                           "met northwind foods at the summit")                          # two words
    assert [(s["account"], s["account_id"]) for s in sigs] == [("Close", "0010"), ("Target", "0011"),
                                                                ("Initech", "0013"), ("Northwind Foods", "0015")], sigs
    assert len(skipped) == 4 and all(k["reason"] == "No account named" for k in skipped)
    assert sigs[0]["text"] == "they signed the expansion"


def test_field_note_prefix_must_be_the_exact_account_and_replies_keep_the_id():
    sigs, skipped = _notes("Tailspin: Omar asked about fees")        # a partial name is not an exact prefix
    assert not sigs and skipped
    sigs, skipped = _notes("tailspin toys, inc.: Omar asked about fees")   # case, punctuation, legal suffix ignored
    assert [s["account_id"] for s in sigs] == ["0016"] and not skipped
    hist = {"messages": [{"user": "U1", "text": "Initech: met the CFO", "ts": "1792162800.000001", "reply_count": 1},
                         {"user": "U1", "text": "she wants a discount", "ts": "1792162900.000001", "thread_ts": "1792162800.000001"}]}
    sigs, _ = slack_notes.to_signals(hist, {"members": []}, ACCTS, "C1", "ex")
    assert [s["account_id"] for s in sigs] == ["0013", "0013"]


# ---- 2. Salesforce ----------------------------------------------------------------------------------------------
def test_salesforce_accounts_carry_owner_and_parent_ids_and_notes_carry_account_id():
    assert "Owner.Id" in salesforce.ACCOUNT_SOQL and "ParentId" in salesforce.ACCOUNT_SOQL
    assert "AccountId," in salesforce.NOTES_SOQL
    resp = {"records": [{"Id": "001A", "Name": "Acme", "Type": "Customer", "ParentId": "001P",
                         "Owner": {"Id": "005O", "Name": "Jordan", "Email": "j@x.com"}}]}
    a = salesforce.to_accounts(resp)[0]
    assert (a["owner_id"], a["parent_id"], a["crm_id"]) == ("005O", "001P", "001A")
    fx = salesforce.load_accounts(FX("salesforce", "accounts.json"))
    assert all(x["owner_id"].startswith("005") for x in fx)
    n = salesforce.to_note_signals({"records": [{"Id": "00T1", "Description": "x", "AccountId": "001A",
                                                 "Account": {"Name": "Acme"}, "Owner": {}}]})[0]
    assert n["account_id"] == "001A"
    assert "contracts/sources.yaml" not in salesforce.__doc__ and "SF_ACCOUNT_WHERE" in salesforce.__doc__


class FakeSalesforce:
    """Records every request. `fail` is a list of (path fragment, status) consumed in order."""

    def __init__(self, fail=()):
        self.calls, self.fail, self.tokens = [], list(fail), 0

    def __call__(self, method, url, headers, body=None, tries=4, form=False):
        self.calls.append((method, url, headers.get("Authorization")))
        if url.endswith("/services/oauth2/token"):
            self.tokens += 1
            return {"access_token": f"tok{self.tokens}", "instance_url": "https://org.my.salesforce.com"}
        for i, (frag, code) in enumerate(self.fail):
            if frag in url:
                del self.fail[i]
                raise AuthError(f"{url}: auth failed ({code})", code)
        if "COUNT%28%29" in url:
            return {"totalSize": 4321, "done": True, "records": []}
        if url.endswith("/describe"):
            return {"fields": [{"name": n} for n in ("Renewal_Date__c", "Pipeline_Play__c", "Pipeline_Play_Status__c",
                                                    "Pipeline_Play_Note__c", "Pipeline_Surface_Version__c")]
                    + [{"name": "Type", "picklistValues": [{"value": "Customer"}, {"value": "Prospect"}]}]}
        if "/query" in url:
            return {"records": [{"Id": "001A", "Name": "Acme", "Type": "Customer"}], "done": True}
        return {"id": "a0X1"}


CC = dict(SF_INSTANCE_URL="https://org.my.salesforce.com", SF_CLIENT_ID="id", SF_CLIENT_SECRET="secret",
          SF_ACCESS_TOKEN=None, SF_ACCOUNT_WHERE=None)


def test_salesforce_401_drops_the_cached_token_and_retries_once():
    salesforce._TOKEN.clear()
    fake = FakeSalesforce(fail=[("/query", 401)])
    with env(**CC), patched(salesforce, "http_json", fake):
        assert [a["account"] for a in salesforce.fetch_accounts()] == ["Acme"]
        assert fake.tokens == 2 and fake.calls[-1][2] == "Bearer tok2"
        fake.fail = [("/sobjects/Account/001A", 401)]                  # write-back after a long idle week
        salesforce.write_back({"object": "Account", "match_on": {"Id": "001A"}, "fields": {"X": 1}}, dry_run=False)
        fake.fail = [("/sobjects/Task/", 401)]
        assert salesforce.create_record({"object": "Task", "fields": {}}, dry_run=False)["id"] == "a0X1"
        assert fake.tokens == 4
        fake.fail = [("/query", 401), ("/query", 401)]                  # still 401 after a fresh token: stop
        try:
            salesforce.fetch_accounts()
            raise AssertionError("expected AuthError")
        except AuthError as e:
            assert e.code == 401
        fake.fail, before = [("/query", 403)], fake.tokens               # 403 is permissions: never retried
        try:
            salesforce.fetch_accounts()
            raise AssertionError("expected AuthError")
        except AuthError as e:
            assert e.code == 403 and fake.tokens == before
    salesforce._TOKEN.clear()


def test_salesforce_401_with_a_supplied_token_fails_at_once():
    salesforce._TOKEN.clear()
    fake = FakeSalesforce(fail=[("/query", 401)])
    with env(SF_INSTANCE_URL="https://org.my.salesforce.com", SF_ACCESS_TOKEN="t", SF_CLIENT_ID=None,
             SF_CLIENT_SECRET=None), patched(salesforce, "http_json", fake):
        try:
            salesforce.fetch_accounts()
            raise AssertionError("expected AuthError")
        except AuthError:
            pass
    assert fake.tokens == 0 and len(fake.calls) == 1


def test_salesforce_account_filter_and_preflight_never_pull_all_accounts():
    salesforce._TOKEN.clear()
    fake = FakeSalesforce()
    with env(**dict(CC, SF_ACCOUNT_WHERE="Owner.UserRole.Name LIKE 'NA%'")), patched(salesforce, "http_json", fake):
        salesforce.fetch_accounts()
        q = [u for _, u, _ in fake.calls if "/query" in u][-1]
        assert "Type%20%21%3D%20null%20AND%20%28Owner.UserRole.Name%20LIKE%20%27NA%25%27%29" in q, q
        fake.calls.clear()
        info = salesforce.preflight()
        urls = [u for _, u, _ in fake.calls if "oauth2" not in u]
        assert info["accounts"] == 4321 and info["missing_account_fields"] == [] and info["customer_types_found"] == ["Customer"]
        assert len(urls) == 2 and urls[0].endswith("/describe") and "SELECT%20COUNT%28%29%20FROM%20Account" in urls[1]
        assert not any("SELECT%20Id" in u for u in urls)              # no full account read in the preflight
    salesforce._TOKEN.clear()


# ---- 3. Gong ----------------------------------------------------------------------------------------------------
def test_gong_count_calls_pages_the_list_only_and_404_means_zero():
    bodies = []

    def fake(method, url, headers, body=None, **kw):
        bodies.append(body)
        if body.get("cursor") is None:
            return {"records": {"cursor": "p2"}, "calls": [{"metaData": {"id": i}} for i in range(100)]}
        return {"records": {}, "calls": [{"metaData": {"id": i}} for i in range(7)]}

    with env(GONG_ACCESS_KEY="k", GONG_ACCESS_KEY_SECRET="s", GONG_BASE_URL="https://api.gong.example/"):
        with patched(gong, "http_json", fake):
            assert gong.count_calls("2026-10-16T00:00:00Z", "2026-10-19T00:00:00Z") == 107
        assert len(bodies) == 2 and all(set(b) <= {"filter", "cursor"} for b in bodies)   # no context/parties/content

        def none(method, url, headers, body=None, **kw):
            raise RuntimeError(f"{url}: HTTP 404: b'No calls found'")

        with patched(gong, "http_json", none):
            assert gong.count_calls("a", "b") == 0


# ---- 4. champion definition --------------------------------------------------------------------------------------
SCHEMA = """
create table crm_account (id text primary key, name text, website_domain text);
create table crm_contact (id text primary key, first_name text, last_name text, full_name text, title text, linkedin_url text, account_id text);
create table crm_opportunity (id text primary key, account_id text, is_won integer, close_date text);
create table crm_opportunity_contact_role (opportunity_id text, contact_id text, role text);
create table product_users (id text primary key, contact_id text, account_id text, is_admin integer);
create table product_events (user_id text, event_at text, is_test_workspace integer, is_internal_user integer);
insert into crm_account values ('A1', 'Acme', 'acme.com'), ('A2', 'Other', 'other.com');
"""


def _warehouse(people, deals=()):
    """people: (id, role or None, close date, [(admin, day, test, internal, account)]).
    deals: extra won deals (opportunity id, account, close date, contact id, role)."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for pid, role, closed, uses in people:
        con.execute("insert into crm_contact values (?, ?, ?, ?, 't', null, 'A1')", (pid, "F" + pid, "L" + pid, f"F{pid} L{pid}"))
        if role:
            con.execute("insert into crm_opportunity values (?, 'A1', 1, ?)", ("O" + pid, closed))
            con.execute("insert into crm_opportunity_contact_role values (?, ?, ?)", ("O" + pid, pid, role))
        for n, (admin, day, test, internal, acct) in enumerate(uses):
            uid = f"U{pid}{n}"
            con.execute("insert into product_users values (?, ?, ?, ?)", (uid, pid, acct, admin))
            con.execute("insert into product_events values (?, ?, ?, ?)", (uid, day, test, internal))
    for oid, acct, closed, pid, role in deals:
        con.execute("insert into crm_opportunity values (?, ?, 1, ?)", (oid, acct, closed))
        con.execute("insert into crm_opportunity_contact_role values (?, ?, ?)", (oid, pid, role))
    return {r["contact_id"]: dict(r) for r in con.execute(warehouse._sql("champion_definition.sqlite.sql"), {"today": TODAY})}


def test_a_won_deal_role_is_a_champion_only_with_recent_product_use():
    got = _warehouse([
        ("rolesuse", "Decision Maker", "2024-03-01", [(0, "2026-09-01", 0, 0, "A1")]),
        ("roleonly", "Decision Maker", "2024-03-01", []),                         # sat on the deal, never used it
        ("oldeal", "Champion", "2023-01-15", [(0, "2026-09-01", 0, 0, "A1")]),    # deal over 3 years old
        ("staleuse", "Evaluator", "2024-03-01", [(0, "2025-09-01", 0, 0, "A1")]),  # use over 365 days ago
        ("elsewhere", "Evaluator", "2024-03-01", [(0, "2026-09-01", 0, 0, "A2")]),  # use at another customer
        ("testonly", "Evaluator", "2024-03-01", [(0, "2026-09-01", 1, 0, "A1")]),  # test workspace is not use
        ("nullflag", "Champion", "2024-03-01", [(0, "2026-09-01", None, None, "A1")]),  # NULL flags = real use
        ("admin", None, None, [(1, "2026-06-01", 0, 0, "A1")]),
        ("oldadmin", None, None, [(1, "2026-03-01", 0, 0, "A1")]),                  # 231 days: a user
        ("vendor", None, None, [(1, "2026-10-01", 0, 1, "A1")]),                    # internal: dropped
        ("plainuser", None, None, [(0, "2026-10-01", 0, 0, "A1")]),                 # no role, no admin seat: not listed
    ])
    lvl = {k: (v["level"], v["evidence"]) for k, v in got.items()}
    assert lvl["rolesuse"][0] == "champion" and lvl["nullflag"][0] == "champion"
    assert lvl["admin"] == ("user", "product admin, no decision or ownership record")     # a seat alone (fix 4)
    assert lvl["roleonly"] == ("user", "role only, no product use")
    assert lvl["testonly"] == ("user", "role only, no product use") and lvl["elsewhere"][1] == "role only, no product use"
    assert lvl["oldeal"] == ("user", "won deal closed over 3 years ago")
    assert lvl["staleuse"] == ("user", "role, but no product use in the last 365 days")
    assert lvl["oldadmin"] == ("user", "admin, but no admin activity in the last 180 days")
    assert "vendor" not in got and "plainuser" not in got
    r = got["rolesuse"]
    assert (r["first_name"], r["last_name"], r["last_activity"], r["evidence_at"], r["evidence_type"]) == \
        ("Frolesuse", "Lrolesuse", "2026-09-01", "2026-09-01", "Decision Maker")
    assert got["roleonly"]["evidence_at"] == "2024-03-01" and got["roleonly"]["last_activity"] is None


def test_an_admin_seat_never_makes_a_champion_without_a_won_deal_role():
    """Red team C10: an active admin seat made anyone a champion, contradicting 'a title alone never makes a champion'."""
    got = _warehouse([
        ("seat", None, None, [(1, "2026-10-01", 0, 0, "A1")]),                              # active admin, no role
        ("seatrole", "Champion", "2024-03-01", [(1, "2026-10-01", 0, 0, "A1")]),            # admin + a recent won-deal role
        ("seatold", "Decision Maker", "2022-01-10", [(1, "2026-10-01", 0, 0, "A1")]),       # admin + a role on an old deal
    ])
    lvl = {k: (v["level"], v["evidence"], v["evidence_type"]) for k, v in got.items()}
    assert lvl["seat"] == ("user", "product admin, no decision or ownership record", "Admin"), lvl["seat"]
    assert lvl["seatrole"] == ("champion", "won-deal role and product use in the last 365 days", "Champion"), lvl["seatrole"]
    assert lvl["seatold"] == ("champion", "product admin active in the last 180 days, with a won-deal role", "Decision Maker")
    seed = {s["Row id"]: s for s in warehouse.to_seed(list(got.values()))}
    assert seed["seat"]["Action tag"] == "uses" and seed["seat"]["Evidence (verbatim)"].endswith("no decision or ownership record")
    assert seed["seatrole"]["Action tag"] == "owns_program" and seed["seatold"]["Action tag"] == "chose"
    assert warehouse.TAG_BY_EVIDENCE["Admin"] == "uses"
    legacy = warehouse.to_seed([{"contact_id": "X", "first_name": "A", "last_name": "B", "customer_name": "Acme",
                                 "customer_domain": "acme.com", "evidence_type": "Admin", "level": "champion", "title": "",
                                 "last_admin_activity": "2026-10-01", "last_activity": "2026-10-01", "evidence_at": "2026-10-01"}])[0]
    assert legacy["Action tag"] == "uses" and legacy["Customer domain"] == "acme.com"     # even if an old query says champion


def test_won_deal_roles_count_only_at_the_contacts_own_account_and_ties_break_by_id():
    """Code review 17: the role and the product use could come from different accounts, and two deals closed on the
    same day gave a random contact role."""
    got = _warehouse([("away", None, None, [(0, "2026-09-01", 0, 0, "A1")]),
                      ("tie", None, None, [(0, "2026-09-01", 0, 0, "A1")])],
                     deals=[("O9away", "A2", "2024-03-01", "away", "Decision Maker"),       # a deal at another account
                            ("O2tie", "A1", "2025-05-05", "tie", "Evaluator"),               # inserted first
                            ("O1tie", "A1", "2025-05-05", "tie", "Decision Maker")])         # same day, lower id: wins
    assert "away" not in got, got.get("away")
    assert got["tie"]["evidence_type"] == "Decision Maker" and got["tie"]["level"] == "champion", got["tie"]
    for path in (os.path.join(ROOT, "sql", "champion_definition.sql"), os.path.join(ROOT, "sql", "champion_definition.sqlite.sql")):
        text = open(path, encoding="utf-8").read()
        assert "o.account_id = c.account_id" in text and "order by o.close_date desc, o.id" in text, path
        assert re.search(r"product[._]users\.account_id must be the CRM account id", text, re.I), path
        assert re.search(r"--\s*join \S*product_account_to_crm m on m\.product_account_id = u\.account_id", text), path


def _evidence_strings(path):
    return sorted(set(re.findall(r"then '([^']+)'|else '([a-z][^']+)'", open(path).read())))


def test_both_champion_sql_files_say_the_same_thing():
    sf, lite = os.path.join(ROOT, "sql", "champion_definition.sql"), os.path.join(ROOT, "sql", "champion_definition.sqlite.sql")
    assert _evidence_strings(sf) == _evidence_strings(lite) and len(_evidence_strings(sf)) >= 7
    a, b = open(sf).read(), open(lite).read()
    for snow, sqlite in (("dateadd(year, -3, current_date)", "date(:today, '-3 years')"),
                         ("dateadd(day, -365, current_date)", "date(:today, '-365 days')"),
                         ("dateadd(day, -180, current_date)", "date(:today, '-180 days')")):
        assert snow in a and sqlite in b, (snow, sqlite)
    for col in ("first_name", "last_name", "last_activity", "evidence_at", "level", "evidence_type"):
        assert col in a and col in b, col


# ---- 5. the demo fixtures agree with themselves ------------------------------------------------------------------
def test_demo_movers_stop_using_the_product_before_they_leave_and_levels_hold():
    mf = _make_fixtures_module()
    left = mf.leave_dates(FX("candidates.json"))
    assert left[("Dana Reyes", "Acme Robotics")] == "2026-08-31" and left[("Leo Park", "Northwind Foods")] == "2026-05-01"
    assert ("Rita Shah", "Stark Freight") not in left and ("Elena Cruz", "Wayne Logistics") not in left   # rename, acquisition
    seed = {s["Name"]: s for s in warehouse.champions_sqlite(FX("warehouse", "demo_warehouse.sql"), TODAY)}
    customer = {s["Name"]: s["Customer"] for s in seed.values()}
    late = {n: (seed[n]["Last activity"], d) for (n, c), d in left.items()
            if n in seed and customer[n] == c and seed[n]["Last activity"] and seed[n]["Last activity"] >= d}
    assert not late, late
    with open(FX("champions.csv"), encoding="utf-8") as f:
        from champion_tracker.config import CHAMPION_TAGS
        want = {r["Name"]: r["Action tag"] in CHAMPION_TAGS for r in csv.DictReader(f) if not r["Exclude reason"]}
    got = {n: s["Action tag"] not in ("uses", "quoted", "none") for n, s in seed.items() if n in want}
    assert got == want, {n: (want[n], got.get(n)) for n in want if got.get(n) != want[n]}
    assert not {"Quinn Tester", "Noor Haddad", "Pat Vendor"} & set(seed)
    owen = seed["Owen Marsh"]                                           # the new trap: role only, no product use
    assert owen["Action tag"] == "uses" and owen["Evidence (verbatim)"].endswith("role only, no product use")
    assert owen["Last activity"] == "" and owen["Evidence date"] == "2024-03-01"
    max_ = seed["Max Hale"]                                             # the admin-only trap: an active seat, no role
    assert max_["Action tag"] == "uses" and max_["Evidence (verbatim)"].endswith("product admin, no decision or ownership record")
    assert max_["Last activity"] >= "2026-04-21", max_                  # active in the last 180 days: a seat, not a decision
    for name in ("Dana Reyes", "Leo Park", "Priya Nair", "Ben Ortiz", "Sam Lee"):   # champions: a role plus product use
        assert "contact role on a won opportunity" in seed[name]["Evidence (verbatim)"], seed[name]


def test_owen_the_role_only_mover_gets_no_card():
    from datetime import date
    from champion_tracker.adapters.fixtures import load_lookups
    from champion_tracker.rules import run as run_rules
    lookups, aliases, known = load_lookups(FX("candidates.json"))
    seed = [s for s in warehouse.champions_sqlite(FX("warehouse", "demo_warehouse.sql"), TODAY) if s["Name"] == "Owen Marsh"]
    assert run_rules(seed, lookups, date(2026, 10, 18), aliases, known)[0]["Decision"] == "No action"
    as_old_sql = [dict(seed[0], **{"Action tag": "chose"})]            # what 'any won-deal role = champion' gave
    assert run_rules(as_old_sql, lookups, date(2026, 10, 18), aliases, known)[0]["Decision"] == "Card"


def test_regenerated_fixtures_match_the_committed_files():
    mf = _make_fixtures_module()
    rels = [("gong", "calls_extensive.json"), ("gong", "calls_transcript.json"), ("salesforce", "accounts.json"),
            ("salesforce", "tasks.json"), ("warehouse", "demo_warehouse.sql"), ("slack", "field_notes_history.json"),
            ("slack", "users_list.json")]
    with tempfile.TemporaryDirectory() as d:
        with contextlib.redirect_stdout(open(os.devnull, "w")):
            mf.main(d)
        for r in rels:
            assert open(os.path.join(d, *r), "rb").read() == open(FX(*r), "rb").read(), r


def test_seed_uses_crm_first_and_last_names_and_parses_full_names_safely():
    cases = {"Dr. Jane Smith": ("Jane", "Smith"), "Reyes, Dana": ("Dana", "Reyes"), "Mary Ann Smith": ("Mary Ann", "Smith"),
             "Maria de la Cruz": ("Maria", "de la Cruz"), "John Smith Jr.": ("John", "Smith"),
             "Smith, Ms. Jane, PhD": ("Jane", "Smith"), "Cher": ("Cher", ""), "  Leo   Park ": ("Leo", "Park")}
    for full, want in cases.items():
        assert warehouse.split_name(full) == want, (full, warehouse.split_name(full))
    row = {"CONTACT_ID": "003X", "FIRST_NAME": "Leo", "LAST_NAME": "van Park", "FULL_NAME": "Mr. Leo van Park",
           "TITLE": "VP", "CUSTOMER_NAME": "Acme", "CUSTOMER_DOMAIN": "acme.com", "EVIDENCE_TYPE": "Admin",
           "LEVEL": "champion", "LAST_ADMIN_ACTIVITY": "2026-09-01 10:00:00", "LAST_ACTIVITY": "2026-09-02 08:00:00",
           "EVIDENCE_AT": "2026-09-02"}                               # Snowflake: UPPERCASE keys, timestamps
    s = warehouse.to_seed([row])[0]
    assert (s["Name"], s["First"], s["Last"]) == ("Leo van Park", "Leo", "van Park")
    assert (s["Last activity"], s["Evidence date"]) == ("2026-09-02", "2026-09-02")
    assert s["Evidence (verbatim)"] == "Product admin; last admin activity 2026-09-01"
    old = warehouse.to_seed([dict(row, CONTACT_ID="003Y", FIRST_NAME=None, LAST_NAME=None, FULL_NAME="Park, Leo")])[0]
    assert (old["First"], old["Last"], old["Name"]) == ("Leo", "Park", "Leo Park")


# ---- 6. Slack delivery -------------------------------------------------------------------------------------------
DIGEST = {"text": "Pipeline", "blocks": [{"type": "header"}, {"type": "section"}, {"type": "section"}]}
ROWS = [{"Card id": f"card{i}", "Account": f"A{i}", "Play": "P1", "Why now": "w", "Contact": "c", "Owner": "o",
         "Status": "Ready", "Priority": i, "Source": ""} for i in range(3)]


def _slack(info=None, fail_on=None):
    calls = []

    def fake(method, body, get=False):
        calls.append((method, body))
        if method == fail_on:
            raise RuntimeError(f"{method}: internal_error")
        if method == "conversations.info":
            return {"ok": True, "channel": dict({"id": body["channel"]}, **(info or {}))}
        if method == "canvases.create":
            return {"ok": True, "canvas_id": "F_C"}
        if method == "slackLists.create":
            return {"ok": True, "list_id": "F_L", "list_metadata": {"schema": [dict(c, id="Col_" + c["key"]) for c in slack_post.LIST_SCHEMA]}}
        if method == "slackLists.items.create":
            return {"ok": True, "item": {"id": f"Rec{len(calls)}"}}
        if method == "chat.postMessage":
            return {"ok": True, "ts": "111.1" if "thread_ts" not in body else "111.2"}
        return {"ok": True}
    return fake, calls


def test_state_is_saved_right_after_each_create_so_a_failed_post_never_duplicates():
    fake, calls = _slack(fail_on="chat.postMessage")
    with tempfile.TemporaryDirectory() as d:
        try:
            slack_post.deliver("C1", DIGEST, "t", "md", dry_run=False, out_dir=d, next_steps=ROWS, call=fake)
            raise AssertionError("expected the digest post to fail")
        except RuntimeError:
            pass
        state = json.load(open(os.path.join(d, "slack_state.json")))
        assert state["canvas_id"] == "F_C" and state["list_id"] == "F_L" and len(state["rows"]) == 3
        fake2, calls2 = _slack()
        slack_post.deliver("C1", DIGEST, "t", "md", dry_run=False, out_dir=d, next_steps=ROWS, call=fake2)
        methods = [m for m, _ in calls2]
        assert "canvases.create" not in methods and "slackLists.create" not in methods
        assert methods.count("slackLists.items.update") == 3 and "slackLists.items.create" not in methods


def test_a_slack_connect_channel_is_refused_unless_allowed():
    for info in ({"is_ext_shared": True}, {"is_shared": True}, {"is_pending_ext_shared": True}):
        fake, calls = _slack(info=info)
        with tempfile.TemporaryDirectory() as d:
            try:
                slack_post.deliver("C1", DIGEST, "t", "md", dry_run=False, out_dir=d, next_steps=ROWS, call=fake)
                raise AssertionError(f"expected a refusal for {info}")
            except slack_post.SharedChannelError as e:
                assert "another organisation" in str(e)
        assert [m for m, _ in calls] == ["conversations.info"], calls        # nothing written anywhere
    fake, calls = _slack(info={"is_shared": True, "is_org_shared": True})  # same Enterprise Grid org: fine
    with tempfile.TemporaryDirectory() as d:
        r = slack_post.deliver("C1", DIGEST, "t", "md", dry_run=False, out_dir=d, call=fake)
        assert r["message_ts"] == "111.1"
        fake, calls = _slack(info={"is_ext_shared": True})
        r = slack_post.deliver("C1", DIGEST, "t", "md", dry_run=False, out_dir=d, call=fake, allow_shared=True)
        assert r["message_ts"] == "111.1"
    fake, calls = _slack(fail_on="conversations.info")                    # cannot check: do not post
    with tempfile.TemporaryDirectory() as d:
        try:
            slack_post.deliver("C1", DIGEST, "t", "md", dry_run=False, out_dir=d, call=fake)
            raise AssertionError("expected a refusal")
        except slack_post.SharedChannelError as e:
            assert "channels:read" in str(e)
    def ops_shared(method, body, get=False):                              # the review queue's channel counts too
        if method == "conversations.info" and body["channel"] == "C_OPS":
            return {"ok": True, "channel": {"is_ext_shared": True}}
        return _slack()[0](method, body, get)
    with tempfile.TemporaryDirectory() as d:
        try:
            slack_post.deliver("C1", DIGEST, "t", "md", dry_run=False, out_dir=d, ops=[{"type": "section"}],
                               ops_to="C_OPS", call=ops_shared)
            raise AssertionError("expected a refusal")
        except slack_post.SharedChannelError:
            pass


def test_thread_messages_are_replies_under_the_digest_and_dry_run_logs_every_request():
    fake, calls = _slack()
    replies = [{"text": "Champion evidence", "blocks": [{"type": "section"}]}, "Run health: all sources fresh"]
    with tempfile.TemporaryDirectory() as d:
        r = slack_post.deliver("C1", DIGEST, "t", "md", dry_run=False, out_dir=d, call=fake, thread_messages=replies)
        posts = [b for m, b in calls if m == "chat.postMessage"]
        assert r["thread_replies"] == 2 and [p.get("thread_ts") for p in posts] == [None, "111.1", "111.1"]
        assert posts[1]["blocks"] == [{"type": "section"}] and posts[2]["text"] == "Run health: all sources fresh"
    with tempfile.TemporaryDirectory() as d:
        slack_post.deliver("C1", DIGEST, "t", "md", dry_run=True, out_dir=d, next_steps=ROWS, thread_messages=replies)
        reqs = json.load(open(os.path.join(d, "slack_requests.json")))
        methods = [x["method"] for x in reqs]
        assert methods[0] == "conversations.info" and methods.count("chat.postMessage") == 3
        assert [x["body"].get("thread_ts") for x in reqs if x["method"] == "chat.postMessage"] == [None, "<ts>", "<ts>"]
        assert not os.path.exists(os.path.join(d, "slack_state.json"))   # a dry run never writes state


# ---- 7. warehouse history ----------------------------------------------------------------------------------------
DEC = [{"Row id": "r1"}, {"Row id": "r2"}]
FIND = [{"id": "f1", "theme": "pricing"}]
CARD = [{"id": "c1"}, {"id": "c2"}]
E1 = {"at": "2026-10-10T10:00", "user": "U1", "action": "snooze"}
E2 = {"at": "2026-10-17T09:00", "user": "U2", "action": "run_play"}


class Spy:
    """A sqlite connection that counts single inserts vs batched ones, and can fail on one table."""

    def __init__(self, con, fail_table=None):
        self.con, self.fail_table, self.single_inserts, self.batches = con, fail_table, 0, 0

    def cursor(self):
        spy, cur = self, self.con.cursor()

        class C:
            def execute(self, sql, params=()):
                if sql.lstrip().lower().startswith("insert"):
                    spy.single_inserts += 1
                return cur.execute(sql, params)

            def executemany(self, sql, seq):
                spy.batches += 1
                if spy.fail_table and spy.fail_table in sql:
                    raise sqlite3.OperationalError("disk I/O error")
                return cur.executemany(sql, seq)

            def fetchall(self):
                return cur.fetchall()
        return C()

    @property
    def in_transaction(self):
        return self.con.in_transaction

    def commit(self):
        self.con.commit()

    def rollback(self):
        self.con.rollback()


def _count(con, t, run=None):
    q = f"select count(*) from {t}" + (" where run_id = ?" if run else "")
    return con.execute(q, (run,) if run else ()).fetchone()[0]


def test_feedback_history_is_append_only_and_survives_a_lost_local_log():
    con = sqlite3.connect(":memory:")
    warehouse.write_run(con, "R1", "2026-10-11", DEC, FIND, CARD, [E1], {"n": 1})
    n = warehouse.write_run(con, "R2", "2026-10-18", DEC, FIND, CARD, [], {"n": 2})     # laptop log lost
    assert _count(con, "pg_feedback") == 1 and n["pg_feedback"] == 0
    n = warehouse.write_run(con, "R3", "2026-10-25", DEC, FIND, CARD, [E1, E2, E2], {"n": 3})
    assert _count(con, "pg_feedback") == 2 and n["pg_feedback"] == 1                    # only the new click
    warehouse.write_run(con, "R3", "2026-10-25", DEC[:1], FIND, CARD, [E1, E2], {"n": 3})  # rerun of R3
    assert _count(con, "pg_decisions", "R3") == 1 and _count(con, "pg_decisions", "R1") == 2
    assert _count(con, "pg_runs") == 3 and _count(con, "pg_feedback") == 2


def test_write_run_batches_inserts_and_is_all_or_nothing():
    con = sqlite3.connect(":memory:")
    spy = Spy(con)
    warehouse.write_run(spy, "R1", "2026-10-18", DEC, FIND, CARD, [E1], {"n": 1})
    assert spy.single_inserts == 0 and spy.batches == 5                 # one executemany per table, no row-by-row
    broken = Spy(con, fail_table="pg_cards")
    try:
        warehouse.write_run(broken, "R1", "2026-10-18", DEC + [{"Row id": "r3"}], FIND, CARD, [E1, E2], {"n": 2})
        raise AssertionError("expected the write to fail")
    except sqlite3.OperationalError:
        pass
    assert not con.in_transaction
    assert _count(con, "pg_decisions", "R1") == 2 and _count(con, "pg_cards", "R1") == 2   # the old run is intact
    assert _count(con, "pg_feedback") == 1



def test_two_clicks_in_one_second_on_different_cards_both_land_and_a_purge_reaches_the_warehouse():
    """Code review 10: the key at|user|action (to the second) dropped a second click on another card, and pg_feedback
    kept a note purge() had blanked for ever."""
    con = sqlite3.connect(":memory:")
    a = {"at": "2025-01-02T10:00:00+00:00", "user": "U1", "action": "wrong_play", "note": "Jane is on medical leave", "card_id": "acct_A"}
    b = {"at": "2025-01-02T10:00:00+00:00", "user": "U1", "action": "wrong_play", "note": "", "card_id": "acct_B"}
    n = warehouse.write_run(con, "R1", "2025-01-03", [], [], [], [a, b], {})
    assert n["pg_feedback"] == 2 and _count(con, "pg_feedback") == 2, n
    purged = dict(a, note="", note_purged_on="2026-01-05")
    n = warehouse.write_run(con, "R2", "2026-01-05", [], [], [], [purged, b], {})
    notes = {json.loads(p)["card_id"]: (json.loads(p)["note"], run) for p, run in con.execute("select payload, run_id from pg_feedback")}
    assert n["pg_feedback"] == 1 and notes == {"acct_A": ("", "R1"), "acct_B": ("", "R1")}, (n, notes)
    c = {"at": "2025-01-02T10:00:00.123456+00:00", "user": "U1", "action": "snooze", "row_id": "r1"}
    d = dict(c, at="2025-01-02T10:00:00.654321+00:00")                       # same second, different microsecond
    e = {"uuid": "9f1c", "at": "2025-01-02T10:00:00+00:00", "user": "U1", "action": "snooze", "row_id": "r1"}
    warehouse.write_run(con, "R3", "2026-01-06", [], [], [], [c, d, e, e], {})
    assert _count(con, "pg_feedback") == 5 and warehouse.feedback_key(e) == "id:9f1c"
    assert warehouse.feedback_key(c) != warehouse.feedback_key(d) and "acct_A" in warehouse.feedback_key(a)


def test_feedback_written_under_the_old_key_moves_to_the_new_key():
    con = sqlite3.connect(":memory:")
    con.execute("create table pg_feedback (run_id text, run_date text, item_key text, payload text)")
    old = {"at": "2026-10-10T10:00:00+00:00", "user": "U1", "action": "snooze", "card_id": "acct_A", "note": "back in Q1"}
    con.execute("insert into pg_feedback values ('R0', '2026-10-10', ?, ?)", (f"{old['at']}|U1|snooze", json.dumps(old)))
    con.commit()
    n = warehouse.write_run(con, "R1", "2026-10-18", [], [], [], [dict(old, note="")], {})
    rows = con.execute("select item_key, payload from pg_feedback").fetchall()
    assert len(rows) == 1 and rows[0][0] == warehouse.feedback_key(old) and json.loads(rows[0][1])["note"] == "", rows
    assert n["pg_feedback"] == 1


def test_snowflake_feedback_is_merged_on_its_key():
    class Cur:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=()):
            self.sql.append(sql)

        def executemany(self, sql, seq):
            self.sql.append((sql, list(seq)))

        def fetchall(self):
            return [("acct_A|||U1|snooze|", json.dumps({"card_id": "acct_A", "user": "U1", "action": "snooze", "note": "x"}))]

    class Con:
        cur = Cur()

        def cursor(self):
            return self.cur
    con = Con()
    ev = {"card_id": "acct_A", "user": "U1", "action": "snooze", "note": ""}
    n = warehouse.write_run(con, "R1", "2026-10-18", [], [], [], [ev], {}, "snowflake")
    merges = [x for x in con.cur.sql if isinstance(x, tuple) and x[0].startswith("merge into pg_feedback")]
    assert n["pg_feedback"] == 1 and len(merges) == 1 and "on t.item_key = s.item_key" in merges[0][0], con.cur.sql
    assert "when matched then update set t.payload = s.payload" in merges[0][0] and merges[0][1][0][2] == "acct_A|||U1|snooze|"
    assert not any(isinstance(x, tuple) and x[0].startswith("insert into pg_feedback") for x in con.cur.sql)


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

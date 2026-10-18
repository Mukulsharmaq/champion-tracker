"""Build the demo source data in each API's real response shape from three readable CSVs:
  fixtures/briefs/accounts.csv   -> fixtures/salesforce/accounts.json        (SOQL response)
  fixtures/briefs/signals.csv    -> fixtures/gong/calls_extensive.json        (POST /v2/calls/extensive)
                                    fixtures/gong/calls_transcript.json       (POST /v2/calls/transcript)
                                    fixtures/salesforce/tasks.json            (SOQL response, rep notes)
                                    fixtures/slack/field_notes_history.json   (conversations.history)
                                    fixtures/slack/users_list.json            (users.list)
  fixtures/champions.csv         -> fixtures/warehouse/demo_warehouse.sql     (CRM + product-usage tables)
  + fixtures/candidates.json        (work history: nobody's product use is dated after they left the customer)

Every call also carries filler turns (greetings, logistics) so the parser and the lexicon must ignore noise, and the
warehouse carries traps the champion SQL must drop (a test-workspace admin, a lost deal, a vendor's internal user)
or demote to a 'user', not a champion (a won-deal role with no product use; an active admin seat with no won-deal role:
Max Hale, the admin-only trap). Every champion in champions.csv gets a won-deal contact role on a deal at their own
account plus product use (program owners as admins, deal-makers as users), so the SQL yields the same champions and
levels as the CSV.
Every source names an account by the same Salesforce Account Id (Gong context objectId, Task AccountId).
Run:  python3 scripts/make_fixtures.py        (deterministic; tests check the round trip)
"""
import csv
import json
import os
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = lambda *p: os.path.join(ROOT, "fixtures", *p)  # noqa: E731
OUT = {"root": os.path.join(ROOT, "fixtures")}
F = lambda *p: os.path.join(OUT["root"], *p)  # noqa: E731
OWNER_TITLE = {"Jordan Blake": "Account Manager", "Riley Shaw": "Account Manager", "Taylor Brooks": "Account Executive",
               "Alex Kim": "Account Executive", "Sam Ito": "SDR"}
OPENERS = ["Thanks for making the time today.", "Good to see you again. Shall we start with where things are?",
           "Thanks for joining. I have a few questions from last time."]
CLOSERS = ["That's helpful. I'll send a recap by email after this.", "Got it. Let's pick this up on Thursday.",
           "Makes sense. I'll share notes with the team."]


def read(name):
    with open(SRC(*name.split("/")), encoding="utf-8") as f:
        return list(csv.DictReader(f))


def dump(obj, *path):
    os.makedirs(os.path.dirname(F(*path)), exist_ok=True)
    with open(F(*path), "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def ts(day, hour=15):
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00+00:00").timestamp()


def leave_dates(path):
    """(name, customer) -> the day the person left the customer, from the work history in candidates.json. A job that
    goes on under an alias (a rename) or a 'new' job starting the same month as the customer job (an acquisition) is
    not a leave."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = {}
    for lk in data["lookups"]:
        names = {n.lower() for n in [lk["customer"]] + data.get("aliases", {}).get(lk["customer"], [])}
        for cand in lk.get("candidates") or []:
            exp = cand.get("experience") or []
            at = [e for e in exp if (e.get("company") or "").lower() in names]
            if not at or any(not e.get("end") for e in at):
                continue                                       # never there, or still there
            starts = {(e.get("start") or "")[:7] for e in at}
            if any(e not in at and (e.get("start") or "")[:7] not in starts for e in exp):
                out[(lk["name"], lk["customer"])] = max(e["end"] for e in at)
    return out


def main(out_root: str = ""):
    OUT["root"] = out_root or os.path.join(ROOT, "fixtures")
    accounts = {a["account"]: a for a in read("briefs/accounts.csv")}
    signals = read("briefs/signals.csv")

    # Salesforce accounts (the Id here is the account id every other source uses)
    recs, acct_id = [], {}
    owner_id = {o: f"005DEMO0000000{n:03d}" for n, o in enumerate(sorted({a["owner"] for a in accounts.values()}), 1)}
    for i, a in enumerate(accounts.values(), 1):
        acct_id[a["account"]] = f"001DEMO0000000{i:03d}"
        opp = None
        if a["open_opportunity"]:
            is_type = a["open_opportunity"] in ("Renewal", "Expansion")
            opp = {"totalSize": 1, "done": True, "records": [{"attributes": {"type": "Opportunity"},
                   "StageName": "Negotiation" if is_type else a["open_opportunity"],
                   "Type": a["open_opportunity"] if is_type else None}]}
        recs.append({"attributes": {"type": "Account", "url": f"/services/data/v61.0/sobjects/Account/001DEMO0000000{i:03d}"},
                     "Id": f"001DEMO0000000{i:03d}", "Name": a["account"], "Website": a["domain"],
                     "Type": "Customer" if a["type"] == "customer" else "Prospect",
                     "ParentId": None, "Owner": {"attributes": {"type": "User"}, "Id": owner_id[a["owner"]], "Name": a["owner"]},
                     "Renewal_Date__c": a["renewal_date"] or None, "Opportunities": opp})
    dump({"totalSize": len(recs), "done": True, "records": recs}, "salesforce", "accounts.json")

    # Gong calls: one call per call signal, with filler turns around the signal turn
    calls, transcripts = [], []
    for k, s in enumerate(x for x in signals if x["source"] == "call"):
        call_id = s["id"].split("-")[1]
        acct = accounts[s["account"]]
        rep = acct["owner"]
        start_ms = int(s["url"].split("t=")[1]) * 1000 if "t=" in s["url"] else 60000
        speaker = {"speakerId": "sp-2", "name": s["speaker"], "title": s["speaker_title"],
                   "affiliation": "External" if s["side"] == "customer" else "Internal"}
        parties = [{"speakerId": "sp-1", "name": rep, "title": OWNER_TITLE[rep], "affiliation": "Internal"}]
        if s["speaker"] != rep:
            parties.append(speaker)
        else:
            speaker = parties[0]
        if s["side"] != "customer":   # an internal turn still happens on a customer call
            parties.append({"speakerId": "sp-3", "name": "Jamie Fox", "title": "Operations Lead", "affiliation": "External"})
        calls.append({"metaData": {"id": call_id, "url": f"https://gong.example.com/call?id={call_id}",
                                   "title": f"{s['account']} <> weekly sync", "started": f"{s['date']}T15:00:00Z",
                                   "duration": 1800, "system": "Zoom", "direction": "Conference"},
                      "parties": parties,
                      "context": [{"system": "Salesforce", "objects": [{"objectType": "Account", "objectId": acct_id[s["account"]],
                                   "fields": [{"name": "Name", "value": s["account"]}, {"name": "Website", "value": acct["domain"]}]}]}]})
        turns = [{"speakerId": "sp-1", "topic": None, "sentences": [{"start": 5000, "end": 9000, "text": OPENERS[k % 3]}]},
                 {"speakerId": speaker["speakerId"], "topic": None,
                  "sentences": [{"start": start_ms, "end": start_ms + 9000, "text": s["text"]}]},
                 {"speakerId": "sp-3" if s["side"] != "customer" else "sp-1", "topic": None,
                  "sentences": [{"start": start_ms + 20000, "end": start_ms + 26000, "text": CLOSERS[k % 3]}]}]
        transcripts.append({"callId": call_id, "transcript": turns})
    dump({"requestId": "demo", "records": {"totalRecords": len(calls), "currentPageSize": len(calls), "currentPageNumber": 0},
          "calls": calls}, "gong", "calls_extensive.json")
    dump({"requestId": "demo", "records": {"totalRecords": len(transcripts), "currentPageSize": len(transcripts),
          "currentPageNumber": 0}, "callTranscripts": transcripts}, "gong", "calls_transcript.json")

    # Salesforce tasks (rep notes)
    tasks = []
    for s in (x for x in signals if x["source"] == "crm"):
        tid = "00TDEMO00000000" + s["id"].split("-")[1]
        tasks.append({"attributes": {"type": "Task", "url": f"/services/data/v61.0/sobjects/Task/{tid}"}, "Id": tid,
                      "Description": s["text"], "ActivityDate": s["date"], "AccountId": acct_id[s["account"]],
                      "Account": {"attributes": {"type": "Account"}, "Name": s["account"]},
                      "Owner": {"attributes": {"type": "Name"}, "Name": s["speaker"], "Title": s["speaker_title"]}})
    dump({"totalSize": len(tasks), "done": True, "records": tasks}, "salesforce", "tasks.json")

    # Slack field notes (with noise the connector must skip)
    msgs = [{"type": "message", "subtype": "channel_join", "user": "U0SAMITO", "text": "<@U0SAMITO> has joined the channel",
             "ts": f"{ts('2026-10-05', 9):.6f}"},
            {"type": "message", "user": "U0RILEY", "text": "Reminder: pipeline review moves to Thursday this week.",
             "ts": f"{ts('2026-10-14', 10):.6f}"}]
    for n, s in enumerate(x for x in signals if x["source"] == "field"):
        msgs.append({"type": "message", "user": "U0SAMITO", "text": f"{s['account']}: {s['text']}",
                     "ts": f"{ts(s['date'], 15) + n:.6f}"})
    msgs.sort(key=lambda m: float(m["ts"]), reverse=True)        # Slack returns newest first
    dump({"ok": True, "messages": msgs, "has_more": False, "response_metadata": {"next_cursor": ""}}, "slack", "field_notes_history.json")
    dump({"ok": True, "members": [
        {"id": "U0SAMITO", "name": "sam.ito", "real_name": "Sam Ito", "profile": {"real_name": "Sam Ito", "title": "SDR"}},
        {"id": "U0RILEY", "name": "riley.shaw", "real_name": "Riley Shaw", "profile": {"real_name": "Riley Shaw", "title": "Account Manager"}}],
        "response_metadata": {"next_cursor": ""}}, "slack", "users_list.json")

    # Demo warehouse (CRM + product usage), built from the champion fixtures
    champs = read("champions.csv")
    left = leave_dates(SRC("candidates.json"))
    doms = {}
    for c in champs:
        doms.setdefault(c["Customer"], c["Slug"] + ".com")
    sql = ["-- Demo warehouse generated by scripts/make_fixtures.py. Invented people and companies.",
           "create table crm_account (id text primary key, name text, website_domain text);",
           "create table crm_contact (id text primary key, first_name text, last_name text, full_name text, title text, "
           "linkedin_url text, account_id text);",
           "create table crm_opportunity (id text primary key, account_id text, is_won integer, close_date text);",
           "create table crm_opportunity_contact_role (opportunity_id text, contact_id text, role text);",
           "create table product_users (id text primary key, contact_id text, account_id text, is_admin integer);",
           "create table product_events (user_id text, event_at text, is_test_workspace integer, is_internal_user integer);"]
    q = lambda v: "null" if v is None else "'" + str(v).replace("'", "''") + "'"  # noqa: E731
    acc_ids = {}
    for i, (name, dom) in enumerate(doms.items(), 1):
        acc_ids[name] = f"001W{i:03d}"
        sql.append(f"insert into crm_account values ({q(acc_ids[name])}, {q(name)}, {q(dom)});")
    role_by_tag = {"chose": "Decision Maker", "rolled_out": "Champion", "business_case": "Economic Buyer",
                   "initiated_review": "Evaluator", "owns_program": "Champion", "_lost_deal": "Champion",
                   "_role_only": "Decision Maker"}
    admin_tags = {"owns_program", "quoted", "_test_workspace", "_vendor"}   # program owners run it; the rest are traps
    extra = [  # traps: the SQL must drop the first two and call the third a user, not a champion
        {"Name": "Quinn Tester", "First": "Quinn", "Last": "Tester", "Title on page": "IT Admin", "Customer": "Globex Media",
         "Action tag": "_test_workspace"},
        {"Name": "Noor Haddad", "First": "Noor", "Last": "Haddad", "Title on page": "Finance Manager", "Customer": "Initech",
         "Action tag": "_lost_deal"},
        {"Name": "Owen Marsh", "First": "Owen", "Last": "Marsh", "Title on page": "Procurement Manager", "Customer": "Initrode",
         "Action tag": "_role_only"}]   # sat on the won deal, never used the product (and has since moved)
    for i, c in enumerate(champs + extra, 1):
        cid, acc = f"003W{i:03d}", acc_ids[c["Customer"]]
        sql.append(f"insert into crm_contact values ({q(cid)}, {q(c['First'])}, {q(c['Last'])}, {q(c['Name'])}, "
                   f"{q(c['Title on page'])}, null, {q(acc)});")
        tag = "_vendor" if c.get("Exclude reason") else c["Action tag"]
        deal = tag in role_by_tag
        if deal:
            oid = f"006W{i:03d}"
            sql.append(f"insert into crm_opportunity values ({q(oid)}, {q(acc)}, {0 if tag == '_lost_deal' else 1}, '2024-03-01');")
            sql.append(f"insert into crm_opportunity_contact_role values ({q(oid)}, {q(cid)}, {q(role_by_tag[tag])});")
        if tag in ("_lost_deal", "_role_only"):
            continue                                  # no product use at all
        uid = f"USR{i:03d}"                           # program owners and the traps are admins; deal-makers are users
        sql.append(f"insert into product_users values ({q(uid)}, {q(cid)}, {q(acc)}, {1 if tag in admin_tags else 0});")
        # Max Hale (quoted) is the admin-only trap: an active admin seat (2026-09-30) and no won-deal role -> a user.
        day = {"owns_program": "2026-09-30", "quoted": "2026-09-30", "_test_workspace": "2026-10-01",
               "_vendor": "2026-10-02"}.get(tag, "2026-09-30")
        gone = left.get((c["Name"], c["Customer"]))
        if gone:                                      # last used a few days before they left, never after
            day = min(day, (date.fromisoformat(gone) - timedelta(days=3)).isoformat())
        sql.append(f"insert into product_events values ({q(uid)}, {q(day)}, {1 if tag == '_test_workspace' else 0}, "
                   f"{1 if tag == '_vendor' else 0});")
    os.makedirs(F("warehouse"), exist_ok=True)
    with open(F("warehouse", "demo_warehouse.sql"), "w", encoding="utf-8") as f:
        f.write("\n".join(sql) + "\n")
    print(f"accounts {len(recs)} · calls {len(calls)} · tasks {len(tasks)} · slack {len(msgs)} · contacts {len(champs) + len(extra)}")


if __name__ == "__main__":
    main()

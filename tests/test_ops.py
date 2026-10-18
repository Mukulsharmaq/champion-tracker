"""Operations fixes from the verification round (2026-10-18): partial Slack posts, owner collisions, purge races,
observer failures, profile keys, the review canvas share, state files and the run lock, the Salesforce token,
pasted-profile caching, the system's own Tasks, snooze and cache timing at a weekly cadence, expected answers on
corrections, queue buttons across runs and parked rows, preflight, retries of writes, the demo-post guard, the
observer prompt stamp and account context. Each test failed before its fix.
Run without pytest:  python3 tests/test_ops.py      (pytest also works)
"""
import contextlib
import csv
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import gtm_connectors  # noqa: E402
from champion_tracker import feedback  # noqa: E402
from champion_tracker.rules import run as run_rules  # noqa: E402
from gtm_briefs import observer as observer_mod  # noqa: E402
from gtm_briefs.extract import extract  # noqa: E402
from gtm_connectors import MissingConfig, gong, salesforce, slack_post, warehouse  # noqa: E402
from pg_system import check, feedback_loop, pipeline, state  # noqa: E402
from pg_system.feedback_loop import allowed_ids, handle_click, profile_url, replay_case  # noqa: E402

TODAY = date(2026, 10, 18)


def _patched(obj, **attrs):
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    return lambda: [setattr(obj, k, v) for k, v in saved.items()]


@contextlib.contextmanager
def _env(**kw):
    saved = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _cfg(d, **kw):
    return dict({"review_owner": {"name": "RevOps", "slack_id": "U_OPS"}, "regression_file": os.path.join(d, "reg.jsonl"),
                 "feedback_log": os.path.join(d, "fb", "feedback.jsonl")}, **kw)


def _latest(d):
    return open(os.path.join(d, "replay", "LATEST")).read()


def _slack_fake(posted, fail=None, dm="D_OPS"):
    """A Slack Web API stand-in. fail(method, body) -> an error string to raise, or None."""
    def fake(method, body, get=False):
        if fail:
            err = fail(method, body)
            if err:
                raise RuntimeError(f"{method}: {err}")
        if method == "conversations.info":
            return {"ok": True, "channel": {"id": body["channel"]}}
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": dm}}
        if method == "chat.postMessage":
            posted.append(body)
            return {"ok": True, "ts": f"{len(posted)}.0"}
        if method == "canvases.create":
            return {"ok": True, "canvas_id": "F_OPS" if "channel_id" not in body else "F_BRIEF"}
        if method == "slackLists.create":
            return {"ok": True, "list_id": "F_L", "list_metadata": {"schema": [dict(c, id="Col" + c["key"]) for c in slack_post.LIST_SCHEMA]}}
        if method == "slackLists.items.create":
            return {"ok": True, "item": {"id": f"R{len(posted)}"}}
        if method == "users.lookupByEmail":
            raise RuntimeError("users.lookupByEmail: users_not_found")
        return {"ok": True, "file": {"permalink": ""}}
    return fake


# ---- 1. a Slack failure after the digest keeps the posted digest's buttons live (code review 1) ------------------
def test_a_failure_after_the_digest_is_posted_still_moves_latest_and_keeps_the_buttons_working():
    posted = []
    restore = _patched(slack_post, _call=_slack_fake(posted))
    try:
        with tempfile.TemporaryDirectory() as d, _env(SLACK_BOT_TOKEN="xoxb-test"):
            cfg = _cfg(d, channel="C123", owner_slack_ids={"Riley Shaw": "U_RILEY"})
            pipeline.run("demo", date(2026, 10, 11), d, post_slack=True, cfg=cfg)
            slack_post._call = _slack_fake(posted, fail=lambda m, b: "ratelimited" if m == "chat.postMessage" and b.get("thread_ts") else None)
            posted.clear()
            s = pipeline.run("demo", TODAY, d, post_slack=True, cfg=cfg)          # used to raise and leave LATEST behind
            assert _latest(d) == s["run_id"] and s["replay"]["latest_updated"], s["replay"]
            assert any(e.startswith("thread reply 1: ") and "ratelimited" in e for e in s["slack"]["errors"]), s["slack"]
            assert any(a.startswith("Slack: ") for a in s["alerts"])
            sent_to = [p["channel"] for p in posted]
            assert sent_to.count("U_OPS") == 2, sent_to                          # the ops queue and the QA sample still went
            digest = next(p for p in posted if p["channel"] == "C123" and not p.get("thread_ts"))
            val = next(e["value"] for b in digest["blocks"] if b["type"] == "actions" for e in b["elements"]
                       if e["action_id"] == "snooze")
            res = handle_click("snooze", val, "U_RILEY", os.path.join(d, "replay"), cfg, log_path=cfg["feedback_log"])
            assert res["ok"], res
            hist = json.load(open(os.path.join(d, "history", f"cards-{s['run_id']}.json")))
            assert hist["dry_run"] is False and hist["posted"] is True
            # the digest itself fails: nothing is live, LATEST stays, and the backtest is told nobody saw the cards
            slack_post._call = _slack_fake(posted, fail=lambda m, b: "internal_error" if m == "chat.postMessage" else None)
            try:
                pipeline.run("demo", date(2026, 10, 25), d, post_slack=True, cfg=cfg)
                raise AssertionError("a failed digest must raise")
            except RuntimeError as e:
                assert "internal_error" in str(e)
            assert _latest(d) == s["run_id"]
            failed = [json.load(open(os.path.join(d, "history", f))) for f in os.listdir(os.path.join(d, "history"))
                      if f.startswith("cards-2026-10-25")]
            assert failed and failed[0]["dry_run"] is True and failed[0]["posted"] is False, failed
    finally:
        restore()


# ---- 2. two Salesforce owners with one name (code review 2) -----------------------------------------------------
def test_two_owners_with_the_same_name_never_act_on_each_other_s_accounts():
    accounts = [{"account": "A Corp", "owner": "John Smith", "owner_id": "005A", "owner_email": "john.a@x.com"},
                {"account": "B Corp", "owner": "John Smith", "owner_id": "005B", "owner_email": "john.b@x.com"},
                {"account": "C Corp", "owner": "Ann Lee", "owner_id": "005C", "owner_email": "ann@x.com"}]
    by_mail = {"john.a@x.com": "U_JOHN_A", "john.b@x.com": "U_JOHN_B", "ann@x.com": "U_ANN"}
    restore = _patched(slack_post, _call=lambda m, b, g=False: {"ok": True, "user": {"id": by_mail[b["email"]]}})
    try:
        with tempfile.TemporaryDirectory() as d:
            ids = pipeline._owner_ids(accounts, {"owner_slack_ids": {"005A": "U_JOHN_A", "005B": "U_JOHN_B"}}, d, live_slack=True)
            assert "John Smith" not in ids and ids["005C"] == "U_ANN", ids                  # learned by owner id
            assert "John Smith" not in json.load(open(os.path.join(d, "owner_slack_ids.json")))
            ids2 = pipeline._owner_ids(accounts, {}, d, live_slack=True)                     # nothing configured
            assert ids2 == {"005A": "U_JOHN_A", "005B": "U_JOHN_B", "005C": "U_ANN"}, ids2
    finally:
        restore()
    card_b = {"owner": "John Smith", "owner_id": "005B", "owner_name_unique": False}
    both = {"005A": "U_JOHN_A", "005B": "U_JOHN_B", "John Smith": "U_JOHN_A"}       # an old file with a name key
    assert allowed_ids("do_play", {"kind": "card", "card": card_b}, {"owner_slack_ids": both}) == {"U_JOHN_B"}
    assert pipeline.card_slack_id(card_b, both, {}) == "U_JOHN_B"
    by_name_only = {"John Smith": "U_JOHN_A"}                                        # ambiguous: nobody, not the wrong John
    assert allowed_ids("do_play", {"kind": "card", "card": card_b}, {"owner_slack_ids": by_name_only}) == set()
    assert pipeline.card_slack_id(card_b, by_name_only, {}) == ""
    unique = {"owner": "Riley Shaw", "owner_id": "005R", "owner_name_unique": True}
    assert allowed_ids("snooze", {"kind": "card", "card": unique}, {"owner_slack_ids": {"Riley Shaw": "U_R"}}) == {"U_R"}
    assert pipeline.owner_names_with_several_ids(accounts) == {"John Smith"}


# ---- 4. a click saved while the run purges old notes is never lost (code review 6) ------------------------------
def test_a_click_appended_during_a_purge_waits_for_the_lock_and_is_kept():
    with tempfile.TemporaryDirectory() as d:
        log = os.path.join(d, "fb.jsonl")
        with open(log, "w") as f:
            f.write(json.dumps({"at": "2024-01-01T00:00:00+00:00", "action": "snooze", "note": "old words about Jane"}) + "\n")
        reading, real_replace = threading.Event(), os.replace

        def slow_replace(src, dst):
            if os.path.abspath(dst) == os.path.abspath(log):
                reading.set()                    # the purge has read the log and is about to swap it in
                time.sleep(0.4)
            real_replace(src, dst)

        def click():
            reading.wait(5)
            feedback.record("do_play", {"Row id": "acct_1"}, user="U_OWNER", log_path=log, extra={"allowed": True, "card_id": "acct_1"})

        t = threading.Thread(target=click)
        os.replace = slow_replace
        try:
            t.start()
            assert state.purge(log, 365, today=TODAY) == 1
        finally:
            os.replace = real_replace
            t.join(5)
        events = state.read_log(log)
        assert [e["action"] for e in events] == ["snooze", "do_play"] and events[0]["note"] == "", events


def test_the_purge_also_blanks_old_notes_in_the_test_cases_and_the_crm_log():
    with tempfile.TemporaryDirectory() as d:
        log, reg, crm = (os.path.join(d, n) for n in ("f.jsonl", "reg.jsonl", "crm_write_back.jsonl"))
        open(log, "w").close()
        with open(reg, "w") as f:
            f.write(json.dumps({"at": "2024-02-01T10:00:00+00:00", "action": "wrong_play", "why": "Jane is on medical leave",
                                "replay": {"today": "2024-02-01"}}) + "\n")
            f.write(json.dumps({"action": "wrong_person", "why": "old case, dated by its replay", "replay": {"today": "2024-03-01"}}) + "\n")
            f.write(json.dumps({"at": "2026-10-01T10:00:00+00:00", "action": "wrong_play", "why": "recent: kept"}) + "\n")
        with open(crm, "w") as f:
            f.write(json.dumps({"object": "Contact", "fields": {"Champion_Move_Note__c": "left for Rivalco", "Champion_Move_Status__c": "x"},
                                "logged_at": "2024-01-05T00:00:00+00:00"}) + "\n")
            f.write(json.dumps({"object": "Account", "fields": {"Pipeline_Play_Note__c": "no date: kept"}}) + "\n")
        assert state.purge(log, 365, today=TODAY, also=[reg, crm]) == 3
        cases = [json.loads(x) for x in open(reg)]
        assert [c["why"] for c in cases] == ["", "", "recent: kept"] and cases[0]["note_purged_on"] == "2026-10-18"
        lines = [json.loads(x) for x in open(crm)]
        assert lines[0]["fields"] == {"Champion_Move_Note__c": "", "Champion_Move_Status__c": "x"}
        assert lines[1]["fields"]["Pipeline_Play_Note__c"] == "no date: kept"
    with tempfile.TemporaryDirectory() as d:                 # dry-run write-back lines are dated for the purge
        salesforce.write_back({"object": "Contact", "match_on": {"Id": "003X"}, "fields": {"Champion_Move_Note__c": "n"}},
                              dry_run=True, log_path=os.path.join(d, "crm.jsonl"))
        assert date.fromisoformat(json.loads(open(os.path.join(d, "crm.jsonl")).read())["logged_at"][:10])


# ---- 5 + 21. the optional observer failing never stops the run; its prompt version is stamped ---------------------
def test_an_observer_failure_runs_on_without_it_and_the_prompt_version_is_on_the_summary():
    def boom(signals):
        raise RuntimeError("https://api.anthropic.com/v1/messages: failed after 4 tries: HTTP Error 529: Overloaded")
    with tempfile.TemporaryDirectory() as d:
        restore = _patched(observer_mod, required_env=lambda: [], observe_with_stats=boom)
        try:
            s = pipeline.run("demo", TODAY, d, cfg=_cfg(d, observer={"enabled": True}))
        finally:
            restore()
        assert s["observer"]["enabled"] and "529" in s["observer"]["failed"], s["observer"]
        assert s["observer"]["prompt_version"] == observer_mod.PROMPT_VERSION
        assert any(a.startswith("Observer failed") for a in s["alerts"]), s["alerts"]
        assert os.path.exists(os.path.join(d, "summary.json")) and os.path.exists(os.path.join(d, "slack_digest.json"))
        assert s["cards"] == 10
    with tempfile.TemporaryDirectory() as d:
        ok = {"observations": {}, "stats": {"sent": 1, "skipped_not_counted": 0, "truncated_by_cap": 0, "malformed_batches": 0}}
        restore = _patched(observer_mod, required_env=lambda: [], observe_with_stats=lambda signals: ok)
        try:
            s = pipeline.run("demo", TODAY, d, cfg=_cfg(d, observer={"enabled": True}))
        finally:
            restore()
        assert s["observer"]["prompt_version"] == observer_mod.PROMPT_VERSION and s["observer"]["sent"] == 1


# ---- 6. a profile pasted after "None of these" is confirmed (code review 8) --------------------------------------
def test_a_profile_pasted_after_none_of_these_is_confirmed_not_still_rejected():
    raw = "https://linkedin.com/in/Jane-Roe/"          # the provider's spelling
    ev = [{"row_id": "003A", "action": "reject_match", "profile": raw, "allowed": True},
          {"row_id": "003A", "action": "paste_profile", "profile": "https://www.linkedin.com/in/jane-roe", "allowed": True}]
    assert state.overrides(ev) == {"003A": {"reject": [], "confirm_url": "https://www.linkedin.com/in/jane-roe"}}
    seed = [{"Name": "Jane Roe", "First": "Jane", "Last": "Roe", "Customer": "Acme", "Slug": "acme", "Row id": "003A",
             "Title on page": "VP Ops", "Action tag": "chose", "Evidence (verbatim)": "Jane chose us", "Story URL": "https://x/s"}]
    cand = {"full_name": "Jane Roe", "linkedin_url": raw, "current_company_name": "Globex", "current_company_website": "globex.com",
            "current_title": "COO", "experience": [{"company": "Acme", "website": "acme.com", "start": "2019-01", "end": "2026-03"},
                                                   {"company": "Globex", "website": "globex.com", "start": "2026-08", "end": None}]}
    lookups = {("Jane Roe", "acme"): {"customer": "Acme", "ok": True, "candidates": [cand]}}
    lk, ov, changed = pipeline.apply_pasted_profiles(seed, lookups, state.overrides(ev))
    assert changed == ["003A"] and len(lk[("Jane Roe", "acme")]["candidates"]) == 1        # matched, not added twice
    r = run_rules(seed, lk, TODAY, overrides=ov)[0]
    assert r["Same person?"] == "Confirmed" and r["Decision"] != "Hold", (r["Decision"], r["Reason"])
    # a reject after the paste wins again; case and trailing slashes never make two keys
    ev2 = ev + [{"row_id": "003A", "action": "wrong_person", "profile": "https://www.linkedin.com/in/JANE-ROE/", "allowed": True}]
    assert state.overrides(ev2)["003A"] == {"reject": ["https://www.linkedin.com/in/jane-roe"]}
    assert state.profile_key("http://uk.linkedin.com/in/J%C3%A9r%C3%B4me?trk=x") == "https://www.linkedin.com/in/jérôme"


# ---- 7. the review canvas is shared through the DM channel, retried until it works (code review 9) ---------------
def test_the_review_canvas_is_shared_through_the_dm_channel_and_retried_every_run_until_it_works():
    calls, fail_share = [], [True]

    def fake(method, body, get=False):
        calls.append((method, body))
        if method == "conversations.info":
            return {"ok": True, "channel": {}}
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "D_REVIEW"}}
        if method == "canvases.create":
            return {"ok": True, "canvas_id": "F_OPS" if "channel_id" not in body else "F_BRIEF"}
        if method == "canvases.access.set":
            if body.get("user_ids") or fail_share[0]:
                raise RuntimeError("canvases.access.set: canvas_not_shared_with_user")
        return {"ok": True, "ts": "1.0", "file": {"permalink": ""}}

    ops = [{"type": "header"}, {"type": "context", "elements": [{"type": "mrkdwn", "text": "queue"}]}, {"type": "context", "elements": []}]
    with tempfile.TemporaryDirectory() as d:
        r = slack_post.deliver("C1", {"text": "d", "blocks": [{"type": "divider"}] * 3}, "t", "md", dry_run=False, out_dir=d,
                               ops=ops, ops_to="U_REVIEW", call=fake, ops_canvas_md="# queue")
        shares = [b for m, b in calls if m == "canvases.access.set"]
        assert shares and shares[0]["channel_ids"] == ["D_REVIEW"] and "user_ids" not in shares[0], shares
        assert ("conversations.open", {"users": "U_REVIEW"}) in calls
        opsmsg = next(b for m, b in calls if m == "chat.postMessage" and b["channel"] == "U_REVIEW")
        assert "not shared with you yet" in json.dumps(opsmsg) and len(opsmsg["blocks"]) == 3     # said in the ops message
        assert any(e.startswith("ops canvas share:") for e in r["errors"]) and not r["ops_canvas_shared"]
        assert json.load(open(os.path.join(d, "slack_state.json")))["ops_canvas_shared"] is False
        calls.clear()
        fail_share[0] = False
        r2 = slack_post.deliver("C1", {"text": "d", "blocks": [{"type": "divider"}] * 3}, "t", "md", dry_run=False, out_dir=d,
                                ops=ops, ops_to="U_REVIEW", call=fake, ops_canvas_md="# queue")
        assert [m for m, _ in calls].count("canvases.access.set") == 1 and r2["ops_canvas_shared"] and not r2["errors"]
        assert [m for m, _ in calls].count("canvases.create") == 0                       # edited in place, not made again
        calls.clear()
        slack_post.deliver("C1", {"text": "d", "blocks": [{"type": "divider"}] * 3}, "t", "md", dry_run=False, out_dir=d,
                           ops=ops, ops_to="U_REVIEW", call=fake, ops_canvas_md="# queue")
        assert [m for m, _ in calls].count("canvases.access.set") == 0                   # shared: never asked again


# ---- 8. state files: written whole, a corrupt one is a clear error, one run at a time (code review 11) ------------
def test_state_files_are_replaced_whole_and_a_corrupt_one_says_how_to_fix_it():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "slack_state.json")
        with open(p, "w") as f:
            json.dump({"canvas_id": "OLD"}, f)
        real = os.replace

        def crash(src, dst):
            raise OSError("disk full")
        os.replace = crash
        try:
            try:
                slack_post._save_state(d, {"canvas_id": "NEW", "list_id": "L"})
            except OSError:
                pass
        finally:
            os.replace = real
        assert json.load(open(p)) == {"canvas_id": "OLD"} and os.listdir(d) == ["slack_state.json"]
        with open(p, "w") as f:
            f.write('{"canvas_id": "F1", "list_id": "F2", "columns": {"sta')
        for fn in (lambda: slack_post.deliver("C1", {"text": "d", "blocks": []}, "t", "md", dry_run=False, out_dir=d,
                                              call=lambda *a, **k: {"ok": True}),
                   lambda: slack_post.set_list_status(d, "acct_1", "working")):
            try:
                fn()
                raise AssertionError("a corrupt state file must stop with a clear error")
            except RuntimeError as e:
                assert "not valid JSON" in str(e) and "move it away" in str(e), e


def test_only_one_run_at_a_time_per_out_folder():
    import fcntl
    with tempfile.TemporaryDirectory() as d:
        holder = open(os.path.join(d, ".run.lock"), "a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)              # the Slack app's run is going
        try:
            try:
                pipeline.run("demo", TODAY, d, cfg=_cfg(d))
                raise AssertionError("a second run must not start")
            except pipeline.RunLocked as e:
                assert ".run.lock" in str(e)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                assert pipeline.main(["--out", d]) == pipeline.EXIT_LOCKED
            assert "Not run: another pipeline run is using" in out.getvalue()
            assert not os.path.exists(os.path.join(d, "summary.json"))
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
        assert pipeline.run("demo", TODAY, d, cfg=_cfg(d))["cards"] == 10                # free again


# ---- 9. the Salesforce token under concurrent clicks (code review 12) ---------------------------------------------
class _ClearedMidway(dict):
    """Looks non-empty at a truthiness check while another thread has already cleared it."""
    def __len__(self):
        return max(1, super().__len__())


def test_a_token_cleared_by_another_thread_never_crashes_a_call_and_only_the_failed_token_is_dropped():
    tokens = []

    def fake(method, url, headers, body=None, tries=4, form=False):
        if url.endswith("/services/oauth2/token"):
            tokens.append(1)
            return {"access_token": f"tok{len(tokens)}", "instance_url": "https://org.my.salesforce.com"}
        if headers["Authorization"] == "Bearer tok1" and url.endswith("/stale"):
            salesforce._TOKEN.update(token="tok9")               # another thread refreshed meanwhile
            raise gtm_connectors.AuthError(f"{url}: auth failed (401)", 401)
        return {"ok": True, "auth": headers["Authorization"]}

    env = dict(SF_INSTANCE_URL="https://org.my.salesforce.com", SF_CLIENT_ID="id", SF_CLIENT_SECRET="s", SF_ACCESS_TOKEN=None)
    saved_token = salesforce._TOKEN
    restore = _patched(salesforce, http_json=fake)
    try:
        with _env(**env):
            salesforce._TOKEN = _ClearedMidway()
            base, h = salesforce._auth()                         # used to raise KeyError 'base'
            assert base == "https://org.my.salesforce.com" and h["Authorization"] == "Bearer tok1"
            salesforce._TOKEN = {}
            tokens.clear()
            r = salesforce._send("GET", "/stale")
            assert r["auth"] == "Bearer tok9" and len(tokens) == 1, (r, tokens)    # tok9 kept: no second fetch
    finally:
        restore()
        salesforce._TOKEN = saved_token
        salesforce._TOKEN.clear()


def test_any_crm_error_is_reported_on_the_card_and_never_escapes_the_click():
    replay = {"kind": "card", "card": {"id": "acct_1", "owner": "Ann", "crm_id": "001A", "play": "P4", "status": "Ready",
              "who": {"name": ""}, "account": "A", "headline": "h", "why_now": ["w"], "built_from": {"finding_ids": [], "row_ids": []}}}

    def broken(update, dry_run=True, log_path=""):
        raise KeyError("base")
    with tempfile.TemporaryDirectory() as d:
        r1 = _patched(feedback_loop, load_replay=lambda root, value, action="": replay)
        r2 = _patched(salesforce, write_back=broken)
        try:
            res = handle_click("snooze", "r:acct_1", "U_ANN", d, {"owner_slack_ids": {"Ann": "U_ANN"}, "write_back": "live"},
                               log_path=os.path.join(d, "f.jsonl"))
        finally:
            r1()
            r2()
        assert res["ok"] and "CRM not updated: 'base'" in res["message"] and len(state.read_log(os.path.join(d, "f.jsonl"))) == 1


# ---- 10. pasted profile links (code review 13) -------------------------------------------------------------------
def test_profile_links_must_be_a_profile_and_carry_no_slack_markup():
    for bad in ("https://www.linkedin.com/company/acme", "https://www.linkedin.com/search/results/people/?keywords=jane",
                "https://www.linkedin.com/in/", "https://www.linkedin.com/in/%20", "https://evil.example/x><!channel>",
                "https://evil.example/a>b", "https://x.com/a|b", "<https://www.linkedin.com/in/jane>", "http://evil.example/x",
                "evil.example/in/jane"):
        assert profile_url(bad) == "", bad
    assert profile_url("https://www.linkedin.com/in/Jane-Roe") == profile_url("linkedin.com/in/jane-roe/") == \
        "https://www.linkedin.com/in/jane-roe"
    assert profile_url("https://de.linkedin.com/in/J%C3%B6rg-M/details/experience/") == "https://www.linkedin.com/in/jörg-m"
    assert profile_url("https://lnkd.in/abc123") == "https://lnkd.in/abc123"           # another https link is fine


# ---- 11. a pasted profile is fetched once, then cached (code review 16) -------------------------------------------
def test_pasted_profiles_are_cached_by_url_and_survive_the_lookup_cache_rewrite():
    calls = []

    def fetch(url):
        calls.append(url)
        return {"full_name": "Zed Coe", "linkedin_url": url, "current_company_name": "Newco"}
    with tempfile.TemporaryDirectory() as d:
        get = pipeline.cached_profile_fetch(fetch, d, TODAY, {})
        assert get("https://www.linkedin.com/in/Zed-Coe/")["current_company_name"] == "Newco" and len(calls) == 1
        pipeline.lookup_people([{"Name": "P0", "Slug": "s", "Customer": "Acme"}], TODAY, d, {},
                               lookup=lambda p: {"name": p["Name"], "slug": "s", "ok": True, "candidates": []})
        stats = {}
        get = pipeline.cached_profile_fetch(fetch, d, TODAY + timedelta(days=7), {}, stats)
        assert get("https://www.linkedin.com/in/zed-coe")["full_name"] == "Zed Coe" and len(calls) == 1 and stats["from_cache"] == 1
        get = pipeline.cached_profile_fetch(fetch, d, TODAY + timedelta(days=9), {})         # past people_cache_days: again
        get("https://www.linkedin.com/in/zed-coe")
        assert len(calls) == 2
        none = pipeline.cached_profile_fetch(lambda u: calls.append(u), d, TODAY, {})        # unknown profile: not cached
        none("https://www.linkedin.com/in/nobody")
        none("https://www.linkedin.com/in/nobody")
        assert calls[-2:] == ["https://www.linkedin.com/in/nobody"] * 2


# ---- 12. .gitignore (code review 18) -----------------------------------------------------------------------------
def test_gitignore_keeps_the_test_data_and_ignores_a_filled_config():
    lines = [ln.strip() for ln in open(os.path.join(ROOT, ".gitignore")) if ln.strip() and not ln.startswith("#")]
    assert "data/" in lines and "!tests/data/" in lines and lines.index("!tests/data/") > lines.index("data/"), lines
    assert "config.json" in lines and "config.example.json" not in lines
    for f in ("regression_sample.jsonl", "indep_test_labels.csv"):
        assert os.path.exists(os.path.join(ROOT, "tests", "data", f)), f


# ---- 13. the system never reads its own Tasks back (red team C5) --------------------------------------------------
def test_the_system_never_reads_its_own_tasks_as_rep_notes():
    with tempfile.TemporaryDirectory() as d:
        s = pipeline.run("demo", TODAY, d, cfg=_cfg(d))
        cards = [c for c in json.load(open(os.path.join(d, "surface.json"))) if c.get("crm_id")]
    assert s["cards"] == 10 and cards
    recs = []
    for i, c in enumerate(cards):
        t = feedback_loop.play_task(c, "2026-10-18")["fields"]
        assert t["Subject"].startswith("[GTM Signal] ")
        recs.append({"Id": f"00T{i:015d}", "Subject": t["Subject"], "Description": t["Description"], "ActivityDate": t["ActivityDate"],
                     "AccountId": c["crm_id"], "Account": {"Name": c["account"]}, "Owner": {"Name": "GTM Integration User"}})
    assert salesforce.to_note_signals({"records": recs}) == [] and extract(salesforce.to_note_signals({"records": recs}), {}) == []
    rep = dict(recs[0], Subject="Call with CFO", Id="00TREP")
    assert len(salesforce.to_note_signals({"records": [rep]})) == 1                  # a rep's own Task still counts
    with _env(SF_INTEGRATION_USER_ID=None):
        q = salesforce.notes_soql("2026-10-05", "2026-10-18")
        assert "ActivityDate >= 2026-10-05 AND ActivityDate <= 2026-10-18" in q and "NOT Subject LIKE '[GTM Signal]%'" in q
        assert "OwnerId" not in q and "TaskSubtype != 'Email'" in q and "Subject," in q
    with _env(SF_INTEGRATION_USER_ID="005000000000001AAA"):
        q = salesforce.notes_soql("2026-10-05", "2026-10-18")
        assert "OwnerId != '005000000000001AAA' AND CreatedById != '005000000000001AAA'" in q, q
    with _env(SF_INTEGRATION_USER_ID="x' OR Id != null OR Id='"):
        try:
            salesforce.notes_soql("2026-10-05")
            raise AssertionError("a bad user id must not reach the query")
        except MissingConfig:
            pass
    try:
        salesforce.notes_soql("2026-10-05 OR 1=1")
        raise AssertionError("a bad date must not reach the query")
    except ValueError:
        pass


# ---- 14. snooze at the weekly cadence (red team C6) --------------------------------------------------------------
def test_snooze_and_not_now_hide_the_card_on_the_next_weekly_runs():
    card = {"id": "acct_1", "account": "Globex", "built_from": {"finding_ids": ["F1"], "row_ids": []}}
    monday = "2026-10-19T09:30:00+00:00"                     # clicked on digest day
    runs = [date(2026, 10, 26), date(2026, 11, 2), date(2026, 11, 9)]
    seen = {}
    for act in ("snooze", "not_now"):
        ev = [{"card_id": "acct_1", "allowed": True, "action": act, "at": monday, "user": "U1"}]
        seen[act] = ["shown" if state.apply([dict(card)], ev, day)[0] else "hidden" for day in runs]
    assert seen == {"snooze": ["hidden", "shown", "shown"], "not_now": ["hidden", "hidden", "shown"]}, seen
    _, hidden = state.apply([dict(card)], [{"card_id": "acct_1", "allowed": True, "action": "snooze", "at": monday, "user": "U1"}], runs[0])
    assert hidden[0]["held_back"].endswith("until 2026-10-27"), hidden


# ---- 15. the people cache hits on a weekly run (red team C7) -----------------------------------------------------
def test_last_weeks_people_lookups_are_reused_on_this_weeks_run():
    calls = []

    def fake(p):
        calls.append(p["Name"])
        return {"name": p["Name"], "slug": p["Slug"], "customer": "Acme", "ok": True, "candidates": []}
    seed = [{"Name": f"Person {i}", "Slug": "acme", "Customer": "Acme"} for i in range(20)]
    with tempfile.TemporaryDirectory() as d:
        counts = []
        for day in (date(2026, 10, 19), date(2026, 10, 26), date(2026, 11, 2)):    # every Monday
            calls.clear()
            _, info = pipeline.lookup_people(seed, day, d, {}, lookup=fake)
            counts.append((len(calls), info["from_cache"]))
        assert counts == [(20, 0), (0, 20), (20, 0)], counts           # day 7 reused; day 14 (8+ days old) looked up
    assert pipeline.DEFAULTS["people_cache_days"] == 8


# ---- 16. a correction passes when the answer is right, not merely different (red team C14) ------------------------
def test_a_correction_with_the_right_answer_passes_only_on_that_answer():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, owner_slack_ids={"Alex Kim": "U_ALEX"})
        pipeline.run("demo", TODAY, d, cfg=cfg)
        cards = json.load(open(os.path.join(d, "surface.json")))
        t = next(c for c in cards if c["account"] == "Tailspin Toys")
        res = handle_click("wrong_play", f"{_latest(d)}:{t['id']}", "U_ALEX", os.path.join(d, "replay"), cfg,
                           note="They signed: a customer now", log_path=cfg["feedback_log"], expected="P5")
        assert res["event"]["expected_play"] == "P5" and "right answer: P5" in res["message"]
        case = feedback_loop.check_regressions(cfg["regression_file"])[0]
        assert case["expected_play"] == "P5" and case["result"] == {"now": "P7", "passes": False}
        fixed = json.loads(json.dumps(case))
        fixed["replay"]["account"]["type"] = "customer"                   # the play changes ...
        got = replay_case(fixed)["now"]
        assert got != "P7" and replay_case(dict(fixed, expected_play=got))["passes"]
        assert not replay_case(dict(fixed, expected_play="P1"))["passes"]  # ... but not to the one the seller asked for
        assert replay_case(dict(fixed, expected_play=None))["passes"]      # without an answer: any change passes (old rule)
        bad = handle_click("wrong_play", f"{_latest(d)}:{t['id']}", "U_ALEX", os.path.join(d, "replay"), cfg,
                           log_path=cfg["feedback_log"], expected="P99")
        assert "expected_play" not in bad["event"]                        # only plays in contracts/plays.json
        # QA: the right decision
        row = next(f for f in os.listdir(os.path.join(d, "replay", _latest(d))) if f.startswith("champion_"))
        rid = row[len("champion_"):-5]
        q = handle_click("qa_wrong", f"{_latest(d)}:{rid}", "U_OPS", os.path.join(d, "replay"), cfg,
                         log_path=cfg["feedback_log"], expected="Watch")
        qcase = feedback_loop.check_regressions(cfg["regression_file"])[-1]
        assert q["event"]["expected_decision"] == "Watch" and qcase["expected_decision"] == "Watch"
        assert qcase["result"]["passes"] is (qcase["result"]["now"] == "Watch")
    m = feedback_loop.correction_modal("wrong_play", "R:acct_1", "C1", "1.0", "pg:acct_1", thread_ts="0.5")
    sel = next(b for b in m["blocks"] if b["block_id"] == "expected")
    vals = [o["value"] for o in sel["element"]["options"]]
    from pg_surface.plays import PLAYS
    assert sel["optional"] and sel["element"]["type"] == "static_select" and set(vals) == set(PLAYS) | {"none"}
    assert all(len(o["text"]["text"]) <= 75 for o in sel["element"]["options"])
    assert json.loads(m["private_metadata"])["thread_ts"] == "0.5"
    qa = feedback_loop.correction_modal("qa_wrong", "R:r1", "D1", "1.0", "qa:r1")
    assert [o["value"] for o in next(b for b in qa["blocks"] if b["block_id"] == "expected")["element"]["options"]] == \
        ["Card", "Review", "Hold", "Watch", "No action"]
    assert not any(b["block_id"] == "expected" for b in feedback_loop.correction_modal("wrong_account", "R:a", "C", "1", "b")["blocks"])


# ---- 17. queue buttons work across runs; rows can be parked; candidates ranked (red team C11) ---------------------
def test_queue_buttons_work_on_last_weeks_message_and_a_parked_row_leaves_the_buttons():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, owner_slack_ids={"Riley Shaw": "U_RILEY"})
        s1 = pipeline.run("demo", TODAY, d, cfg=cfg)
        rp = os.path.join(d, "replay")
        sam = next(r for r in csv.DictReader(open(os.path.join(d, "review_queue.csv"))) if r["Person"] == "Sam Lee")["Row id"]
        globex = next(c for c in json.load(open(os.path.join(d, "surface.json"))) if c["account"] == "Globex Media")["id"]
        s2 = pipeline.run("demo", TODAY + timedelta(days=7), d, cfg=cfg)          # a newer run replaced the messages
        assert s2["run_id"] != s1["run_id"] and _latest(d) == s2["run_id"]
        seller = handle_click("snooze", f"{s1['run_id']}:{globex}", "U_RILEY", rp, cfg, log_path=cfg["feedback_log"])
        assert not seller["ok"] and "older run" in seller["message"]               # seller cards: latest run only
        ok = handle_click("unresolvable", f"{s1['run_id']}:{sam}", "U_OPS", rp, cfg, log_path=cfg["feedback_log"])
        assert ok["ok"] and ok["allowed"] and ok["event"]["applied_to_run"] == s2["run_id"], ok
        assert ok["write_back"]["skipped"].startswith("parked") and "Can't resolve" in ok["message"]
        assert not handle_click("unresolvable", f"{s2['run_id']}:{sam}", "U_RILEY", rp, cfg, log_path=cfg["feedback_log"])["allowed"]
        assert not handle_click("confirm_match", f"{s1['run_id']}:no_such_row|x", "U_OPS", rp, cfg, log_path=cfg["feedback_log"])["ok"]
        assert not handle_click("confirm_match", f"../{s1['run_id']}:{sam}|x", "U_OPS", rp, cfg, log_path=cfg["feedback_log"])["ok"]
        s3 = pipeline.run("demo", TODAY + timedelta(days=14), d, cfg=cfg)
        queue = {r["Person"]: r for r in csv.DictReader(open(os.path.join(d, "review_queue.csv")))}
        assert queue["Sam Lee"]["Parked"] == "Yes" and s3["queue_parked"] == 1
        ops = next(r["body"] for r in json.load(open(os.path.join(d, "slack_requests.json")))
                   if r["method"] == "chat.postMessage" and r["body"]["text"] == "Champion review queue")
        assert f"id:{sam}" not in {b.get("block_id") for b in ops["blocks"] if b["type"] == "actions"}
        assert s3["metrics"]["queue_age_days"]["by_row"].get(sam) is None              # parked rows do not age the queue
        handle_click("paste_profile", f"{s3['run_id']}:{sam}", "U_OPS", rp, cfg, note="https://www.linkedin.com/in/sam-lee-x",
                     log_path=cfg["feedback_log"])
        assert state.parked(state.read_log(cfg["feedback_log"])) == {}                  # a later decision un-parks it


def test_review_candidates_carry_a_score_and_the_likeliest_comes_first():
    seed = {"Row id": "r1", "Name": "Sam Lee", "First": "Sam", "Last": "Lee", "Slug": "acme", "Customer": "Acme Robotics",
            "Title on page": "Head of Travel"}
    cands = [{"full_name": "Sam Lee", "current_title": "Nurse", "current_company_name": "City Hospital", "linkedin_url": "u1"},
             {"full_name": "Sam Lee", "current_title": "Travel Manager", "current_company_name": "Contoso", "linkedin_url": "u2",
              "headline": "Travel manager | ex-Acme Robotics", "current_location": "Toronto, Canada",
              "experience": [{"company": "Acme Robotics", "start": "2019-01", "end": "2025-02"}]},
             {"full_name": "Sam Lee", "current_title": "Head of Travel", "current_company_name": "Initech", "linkedin_url": "u3"}]
    got = pipeline._candidates({"Row id": "r1"}, {"r1": seed}, {("Sam Lee", "acme"): {"ok": True, "candidates": cands}},
                               countries={"Acme Robotics": "Canada"})
    assert [c["linkedin"] for c in got] == ["u2", "u3", "u1"], got
    assert got[0]["score"] == 1.0 and got[1]["score"] == 0.2 and got[2]["score"] == 0.0, [c["score"] for c in got]


# ---- 18. preflight exercises the people source and only explains the SQL (red team C16) ---------------------------
def test_preflight_runs_one_real_people_lookup_and_explains_the_sql_instead_of_running_it():
    only_people = {"sources": {"warehouse": False, "people": True, "gong": False, "salesforce": False, "field_notes": False},
                   "champion_seed_csv": "x.csv"}
    with _env(PEOPLE_SEARCH_URL="https://p/search", PEOPLE_DATA_TOKEN="t", PEOPLE_MATCH_URL=None):
        rep = check.preflight(only_people)
        assert rep["sources"]["people"]["exercised"] is False and check.NOT_EXERCISED in rep["warnings"]
        assert check.NO_MATCH_URL in rep["warnings"]
        from champion_tracker.adapters import people_data
        seen = []
        restore = _patched(people_data, lookup_record=lambda p: seen.append(p) or {"ok": True, "source": "search", "candidates": [{}, {}]})
        try:
            rep = check.preflight(dict(only_people, preflight_person={"first": "Dana", "last": "Reyes", "company": "Acme"}))
        finally:
            restore()
        assert seen[0]["Name"] == "Dana Reyes" and seen[0]["Customer"] == "Acme"
        assert rep["sources"]["people"]["exercised"] and rep["sources"]["people"]["candidates"] == 2 and rep["ok"], rep
        restore = _patched(people_data, lookup_record=lambda p: {"ok": False, "error": "auth failed (401)"})
        try:
            rep = check.preflight(dict(only_people, preflight_person={"first": "Dana", "last": "Reyes", "company": "Acme"}))
        finally:
            restore()
        assert not rep["ok"] and "auth failed" in rep["sources"]["people"]["error"]
    executed = []

    class Cur:
        def execute(self, sql):
            executed.append(sql)

        def fetchall(self):
            return [("GlobalStats",), ("1:0 -> Result",)]

    class Con:
        def cursor(self):
            return Cur()

        def close(self):
            pass
    restore = _patched(warehouse, connect=lambda: (None, Con()))
    try:
        rep = check.preflight({"sources": {"warehouse": True, "people": False, "gong": False, "salesforce": False,
                                           "field_notes": False}})
    finally:
        restore()
    assert executed[0].startswith("EXPLAIN USING TEXT ") and "count(*)" not in executed[0].lower()[:40], executed
    assert not executed[0].rstrip().endswith(";") and rep["sources"]["warehouse"]["sql_valid"]


# ---- 19. writes are never retried when they may have gone through (red team C17) -----------------------------------
def test_posts_are_not_retried_on_timeouts_or_5xx_but_reads_and_429s_are():
    attempts = []

    def opener(errors):
        def urlopen(req, timeout=60):
            attempts.append(req.get_method())
            e = errors.pop(0) if errors else None
            if e == 503:
                raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b"busy"))
            if e == 429:
                raise urllib.error.HTTPError(req.full_url, 429, "slow down", {"Retry-After": "0"}, io.BytesIO(b""))
            if e == "timeout":
                raise socket.timeout("timed out")

            class R:
                def read(self):
                    return b'{"ok": true}'

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return R()
        return urlopen
    import urllib.request
    restore = _patched(urllib.request, urlopen=opener([]))
    r2 = _patched(time, sleep=lambda s: None)
    try:
        for err in (503, "timeout"):
            attempts.clear()
            urllib.request.urlopen = opener([err])
            try:
                gtm_connectors.http_json("POST", "https://slack.com/api/chat.postMessage", {}, {"text": "digest"})
                raise AssertionError("a POST that may have gone through must not be retried")
            except RuntimeError as e:
                assert "not retried" in str(e) and attempts == ["POST"], (err, attempts)
        attempts.clear()
        urllib.request.urlopen = opener([429, 429])
        assert gtm_connectors.http_json("POST", "https://x/api", {}, {"a": 1}) == {"ok": True} and len(attempts) == 3
        for err in (503, "timeout"):
            attempts.clear()
            urllib.request.urlopen = opener([err])
            assert gtm_connectors.http_json("GET", "https://x/api", {}) == {"ok": True} and attempts == ["GET", "GET"], err
            attempts.clear()
            urllib.request.urlopen = opener([err])
            assert gtm_connectors.http_json("POST", "https://x/v2/calls/extensive", {}, {}, idempotent=True) == {"ok": True}
            assert len(attempts) == 2
    finally:
        restore()
        r2()
    seen = []
    restore = _patched(gong, http_json=lambda method, url, headers, body=None, **kw: seen.append(kw) or {"records": {}, "calls": []})
    try:
        with _env(GONG_ACCESS_KEY="k", GONG_ACCESS_KEY_SECRET="s", GONG_BASE_URL="https://g"):
            gong.count_calls("a", "b")
            gong.fetch("a", "b")
    finally:
        restore()
    assert seen and all(kw.get("idempotent") is True for kw in seen), seen
    sent = []
    restore = _patched(slack_post, http_json=lambda method, url, headers, body=None, **kw: sent.append((url, kw)) or {"ok": True})
    try:
        with _env(SLACK_BOT_TOKEN="xoxb-test"):
            slack_post._call("chat.postMessage", {"channel": "C"})
            slack_post._call("canvases.edit", {"canvas_id": "F"})
    finally:
        restore()
    assert [kw.get("idempotent") for _, kw in sent] == [False, True], sent


# ---- 20. the CLI never posts the demo unless asked to, on purpose (red team C18) -----------------------------------
def test_the_cli_refuses_to_post_the_demo_without_allow_demo_post():
    def no_slack(*a, **k):
        raise AssertionError("must not reach Slack")
    restore = _patched(slack_post, _call=no_slack)
    try:
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                assert pipeline.main(["--post-slack", "--out", d, "--channel", "C_REAL"]) == 2
            assert "--allow-demo-post" in out.getvalue() and os.listdir(d) == [], out.getvalue()
    finally:
        restore()
    posted = []
    with tempfile.TemporaryDirectory() as d, _env(SLACK_BOT_TOKEN="xoxb-test"):
        restore = _patched(slack_post, _call=_slack_fake(posted))
        r2 = _patched(pipeline, load_config=lambda out_dir="": _cfg(d))          # never the repo's feedback/ folder
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                assert pipeline.main(["--post-slack", "--allow-demo-post", "--out", d, "--channel", "C_TEST"]) == 0
            assert posted and posted[0]["channel"] == "C_TEST"
        finally:
            restore()
            r2()


# ---- 22. account context, ICP, old moves from the surface, owner DMs without a thread ------------------------------
def test_accounts_carry_industry_size_country_and_the_pipeline_passes_icp_and_old_moves():
    resp = {"records": [{"Id": "001A", "Name": "Acme", "Type": "Customer", "Industry": "Software", "NumberOfEmployees": 1200,
                         "BillingCountry": "Canada", "Owner": {"Id": "005O", "Name": "Jo"}}]}
    a = salesforce.to_accounts(resp)[0]
    assert (a["industry"], a["employees"], a["country"]) == ("Software", 1200, "Canada")
    assert "Industry, NumberOfEmployees, BillingCountry" in salesforce.ACCOUNT_SOQL
    assert salesforce.to_accounts({"records": [{"Id": "1", "Name": "B"}]})[0]["employees"] is None
    from pg_surface import surface
    seen = {}
    real = surface.build_cards

    def spy(*a, **kw):
        seen.update(kw)
        cards = real(*a, **kw)
        if kw.get("old_moves") is not None:
            kw["old_moves"].append({"Row id": "ROW_OLD", "Person": "Olga Old", "Case study": "Initech",
                                    "New company": "Elsewhere", "Move age (days)": 400, "Reason": "old move", "Decision": "Review"})
        return cards
    digests = []
    real_digest = pipeline.digest_blocks

    def spy_digest(*a, **kw):
        digests.append(kw)
        return real_digest(*a, **kw)
    icp = {"_note": "x", "industries_include": ["Software"], "min_employees": None, "countries": []}
    restore = _patched(pipeline, build_cards=spy, digest_blocks=spy_digest)
    try:
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(d, icp=icp, old_move_days=200, digest_to="owners",
                       owner_slack_ids={"Riley Shaw": "U_RILEY", "Alex Kim": "U_ALEX", "Jordan Blake": "U_J", "Taylor Brooks": "U_T"})
            s = pipeline.run("demo", TODAY, d, cfg=cfg)
            assert seen["icp"] == {"industries_include": ["Software"]} and seen["old_move_days"] == 200, seen
            ops_md = open(os.path.join(d, "ops.md")).read()
            assert "| Olga Old | Initech | Elsewhere | 400 |" in ops_md and s["old_moves"] == 1, ops_md.split("## Old moves")[1][:400]
            dms = [r["body"] for r in json.load(open(os.path.join(d, "slack_requests.json")))
                   if r["method"] == "chat.postMessage" and r["body"]["channel"] in ("U_RILEY", "U_ALEX", "U_J", "U_T")]
            assert dms and not any("thread below" in json.dumps(m) for m in dms)
            assert [kw.get("thread") for kw in digests].count(False) == len(dms) and digests[0].get("thread") is None
        with tempfile.TemporaryDirectory() as d:
            seen.clear()
            pipeline.run("demo", TODAY, d, cfg=_cfg(d, icp={"industries_include": [], "min_employees": None}))
            assert seen.get("icp") is None                 # an empty ICP means no ICP check
    finally:
        restore()


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

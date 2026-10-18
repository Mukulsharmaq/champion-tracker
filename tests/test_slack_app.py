"""Tests for the Slack app (slack_app/app.py), its manifest and the packaging. A fake slack_bolt is injected, so no
tokens, sockets or threads are used; the one real child process stops at the missing-config check, before any network.
Run without pytest:  python3 tests/test_slack_app.py      (pytest also works)
"""
import atexit
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from pg_system import feedback_loop  # noqa: E402


# ---- a fake slack_bolt -----------------------------------------------------------------------------------------
class FakeClient:
    """Records every Slack Web API call; conversations_history answers from self.history."""

    def __init__(self):
        self.calls, self.history = [], {}

    def __getattr__(self, name):
        def call(**kw):
            self.calls.append((name, kw))
            if name == "conversations_history":
                msg = self.history.get((kw["channel"], kw["latest"]))
                return {"ok": True, "messages": [msg] if msg else []}
            return {"ok": True}
        return call

    def named(self, name):
        return [kw for n, kw in self.calls if n == name]


class FakeApp:
    """Registers handlers the way Bolt's decorators do, so the tests can call them directly."""

    def __init__(self, token=None, **_):
        self.token, self.client = token, FakeClient()
        self.actions, self.views, self.commands = [], {}, {}

    def action(self, matcher):
        def register(fn):
            self.actions.append((matcher, fn))
            return fn
        return register

    def view(self, callback_id):
        def register(fn):
            self.views[callback_id] = fn
            return fn
        return register

    def command(self, name):
        def register(fn):
            self.commands[name] = fn
            return fn
        return register

    def handler_for(self, action_id):
        for m, fn in self.actions:
            if (m.match(action_id) if hasattr(m, "match") else m == action_id):
                return fn
        return None


class FakeSocketModeHandler:
    def __init__(self, app, token):
        self.app, self.token = app, token

    def start(self):
        raise AssertionError("tests never open a socket")


def _install_fake_bolt():
    bolt = types.ModuleType("slack_bolt")
    adapter = types.ModuleType("slack_bolt.adapter")
    socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
    bolt.App, socket_mode.SocketModeHandler = FakeApp, FakeSocketModeHandler
    bolt.adapter, adapter.socket_mode = adapter, socket_mode
    sys.modules.update({"slack_bolt": bolt, "slack_bolt.adapter": adapter, "slack_bolt.adapter.socket_mode": socket_mode})


TMP = tempfile.mkdtemp(prefix="slack_app_test_")
atexit.register(shutil.rmtree, TMP, True)
os.environ.update(SLACK_BOT_TOKEN="xoxb-test", SLACK_APP_TOKEN="xapp-test", PIPELINE_OUT=os.path.join(TMP, "out"))
os.environ.pop("PIPELINE_MODE", None)
_install_fake_bolt()
_spec = importlib.util.spec_from_file_location("gtm_slack_app", os.path.join(ROOT, "slack_app", "app.py"))
APP = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(APP)

CFG = {"channel": "C_PIPE", "pipeline_admins": ["U_ADMIN"], "review_owner": {"name": "Rev Ops", "slack_id": "U_REVIEW"},
       "schedule": {"weekday": "MON", "hour_utc": 7}, "feedback_log": os.path.join(TMP, "feedback", "feedback.jsonl"),
       "regression_file": os.path.join(TMP, "feedback", "regression_cases.jsonl")}     # never the repo's feedback/
MONDAY_9 = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)          # 2026-10-19 is a Monday


@contextlib.contextmanager
def fresh(cfg=None, mode=None):
    """A clean out/ folder, config, PIPELINE_MODE and Slack client for one test; puts everything back after."""
    out = tempfile.mkdtemp(dir=TMP)
    names = ("OUT", "load_config", "handle_click", "run_pipeline_process", "_background")
    saved = {n: getattr(APP, n) for n in names}
    saved_client, saved_mode = APP.app.client, os.environ.get("PIPELINE_MODE")
    saved_list = APP.slack_post.set_list_status
    APP.OUT, APP.app.client = out, FakeClient()
    APP.load_config = lambda out_dir="": json.loads(json.dumps(cfg if cfg is not None else CFG))
    if mode is None:
        os.environ.pop("PIPELINE_MODE", None)
    else:
        os.environ["PIPELINE_MODE"] = mode
    try:
        yield out
    finally:
        for n, v in saved.items():
            setattr(APP, n, v)
        APP.app.client, APP.slack_post.set_list_status = saved_client, saved_list
        os.environ.pop("PIPELINE_MODE", None)
        if saved_mode is not None:
            os.environ["PIPELINE_MODE"] = saved_mode


def _slash(user="U_ADMIN", channel="C_PIPE"):
    acks, client = [], FakeClient()
    APP.app.commands["/pipeline"](ack=lambda *a, **k: acks.append(a[0] if a else k.get("text")),
                                  command={"user_id": user, "channel_id": channel, "text": ""}, client=client)
    return acks, client


def _click_body(action_id, value, block_id, user, channel="C_PIPE", ts="1.0", blocks=None):
    blocks = blocks if blocks is not None else [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Globex Media* · P4 Renewal save"}},
        {"type": "actions", "block_id": block_id, "elements": [{"type": "button", "action_id": action_id, "value": value}]}]
    return {"actions": [{"action_id": action_id, "value": value, "block_id": block_id}], "user": {"id": user},
            "channel": {"id": channel}, "trigger_id": "T1", "message": {"ts": ts, "text": "digest", "blocks": blocks}}


def _clicks_seen():
    seen = []

    def fake(action, value, user, replay_root, cfg, note="", log_path="", **_):
        seen.append({"action": action, "value": value, "user": user, "replay_root": replay_root, "note": note,
                     "log_path": log_path})
        return dict(fake.result)
    fake.result = {"ok": True, "allowed": True, "message": "✔ done", "event": {"card_id": "c1"}}
    return seen, fake


# ---- 1. demo data never reaches the channel ---------------------------------------------------------------------
def test_demo_mode_refuses_to_post_from_the_slash_command():
    for mode in (None, "demo", "Demo "):
        with fresh(mode=mode):
            runs = []
            APP.run_pipeline_process = lambda *a, **k: runs.append(a) or {}
            APP._background = lambda fn: fn()
            acks, client = _slash()
            assert runs == [] and "PIPELINE_MODE" in acks[0] and "live" in acks[0], (mode, acks)
            assert client.calls == [] and not APP.RUN_LOCK.locked()


def test_demo_mode_skips_the_weekly_run_logs_it_and_tells_the_review_owner_once():
    with fresh(mode="demo") as out:
        launched, err = [], io.StringIO()
        with contextlib.redirect_stderr(err):
            first = APP.scheduler_tick(MONDAY_9, launch=launched.append)
            again = APP.scheduler_tick(MONDAY_9 + timedelta(minutes=1), launch=launched.append)
        assert first == again == "refused" and launched == []
        dms = APP.app.client.named("chat_postMessage")
        assert len(dms) == 1 and dms[0]["channel"] == "U_REVIEW" and "PIPELINE_MODE" in dms[0]["text"], dms
        assert "PIPELINE_MODE is not 'live'" in err.getvalue()
        with open(os.path.join(out, "schedule_state.json"), encoding="utf-8") as f:
            assert json.load(f).get("last_run_date", "") != "2026-10-19"       # a skipped week is not a run
        os.environ["PIPELINE_MODE"] = "live"                                  # fixed and restarted the same day
        assert APP.scheduler_tick(MONDAY_9 + timedelta(hours=2), launch=launched.append) == "started" and len(launched) == 1


# ---- 4. /pipeline: who and where ------------------------------------------------------------------------------
def test_pipeline_refuses_when_no_admins_are_set_and_says_who_can_set_them():
    with fresh(dict(CFG, pipeline_admins=[]), mode="live"):
        APP.run_pipeline_process = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run"))
        acks, _ = _slash()
        assert "pipeline_admins" in acks[0] and "Rev Ops" in acks[0], acks


def test_pipeline_refuses_a_person_not_in_the_admin_list():
    with fresh(mode="live"):
        APP.run_pipeline_process = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run"))
        acks, _ = _slash(user="U_SELLER")
        assert "pipeline_admins" in acks[0] and not APP.RUN_LOCK.locked(), acks


def test_pipeline_refuses_outside_the_configured_channel():
    with fresh(mode="live"):
        APP.run_pipeline_process = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run"))
        acks, _ = _slash(channel="C_RANDOM")
        assert "C_PIPE" in acks[0] and "pipeline channel" in acks[0], acks
    with fresh(dict(CFG, channel=""), mode="live"):
        acks, _ = _slash(channel="C_RANDOM")
        assert "channel is not set" in acks[0], acks


def test_pipeline_runs_once_reports_back_and_never_overlaps_another_run():
    with fresh(mode="live"):
        runs = []
        APP.run_pipeline_process = lambda channel="", *a, **k: runs.append(channel) or {"cards": 3, "cards_ready": 2,
                                                                                         "alerts": ["x"]}
        APP._background = lambda fn: fn()
        acks, client = _slash()
        assert runs == ["C_PIPE"] and acks[0].startswith("Running")
        assert client.named("chat_postEphemeral")[0]["text"] == "Posted: 3 accounts (2 ready). Alerts: 1."
        assert not APP.RUN_LOCK.locked()
        APP.RUN_LOCK.acquire()                       # the weekly run is going
        try:
            acks, _ = _slash()
        finally:
            APP.RUN_LOCK.release()
        assert "already going" in acks[0] and runs == ["C_PIPE"], acks


def test_a_failed_pipeline_run_reports_its_last_lines_and_frees_the_lock():
    with fresh(mode="live"):
        def boom(*a, **k):
            raise APP.PipelineFailed("the run exited with code 1", "Traceback (most recent call last):\nKeyError: 'Id'")
        APP.run_pipeline_process, APP._background = boom, (lambda fn: fn())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _, client = _slash()
        text = client.named("chat_postEphemeral")[0]["text"]
        assert "exited with code 1" in text and "KeyError: 'Id'" in text and not APP.RUN_LOCK.locked(), text


# ---- clicks -----------------------------------------------------------------------------------------------------
def test_an_allowed_click_closes_the_card_for_everyone_and_moves_the_list_row():
    with fresh() as out:
        seen, fake = _clicks_seen()
        listed = []
        APP.handle_click = fake
        APP.slack_post.set_list_status = lambda out_dir, card_id, status: listed.append((out_dir, card_id, status))
        client = FakeClient()
        APP.app.handler_for("do_play")(ack=lambda *a, **k: None, body=_click_body("do_play", "R1:c1", "pg:c1", "U_OWNER"),
                                       client=client)
        upd = client.named("chat_update")
        assert len(upd) == 1 and upd[0]["channel"] == "C_PIPE" and upd[0]["ts"] == "1.0"
        assert upd[0]["blocks"][1] == {"type": "context", "block_id": "pg:c1", "elements": [{"type": "mrkdwn", "text": "✔ done"}]}
        assert listed == [(out, "c1", "working")] and client.named("chat_postEphemeral") == []
        assert seen[0]["action"] == "do_play" and seen[0]["replay_root"] == os.path.join(out, "replay")
        assert seen[0]["log_path"] == CFG["feedback_log"]           # the same log the pipeline reads


def test_a_click_by_someone_else_is_a_private_note_and_the_buttons_stay():
    with fresh():
        seen, fake = _clicks_seen()
        fake.result = {"ok": True, "allowed": False, "message": "logged as a comment", "event": {"card_id": "c1"}}
        APP.handle_click = fake
        APP.slack_post.set_list_status = lambda *a: (_ for _ in ()).throw(AssertionError("must not move the row"))
        client = FakeClient()
        APP.app.handler_for("snooze")(ack=lambda *a, **k: None, body=_click_body("snooze", "R1:c1", "pg:c1", "U_SELLER"),
                                      client=client)
        assert client.named("chat_update") == []
        eph = client.named("chat_postEphemeral")
        assert eph == [{"channel": "C_PIPE", "user": "U_SELLER", "text": "logged as a comment"}], eph


def test_a_click_on_an_old_digest_is_refused_by_the_real_handler():
    with fresh():                                          # empty out/: no replay/LATEST, so every run id is stale
        client = FakeClient()
        APP.app.handler_for("accept")(ack=lambda *a, **k: None, body=_click_body("accept", "OLD:c1", "pg:c1", "U_OWNER"),
                                      client=client)
        assert client.named("chat_update") == [] and "older run" in client.named("chat_postEphemeral")[0]["text"]


def test_identity_and_qa_buttons_are_handled_like_clicks():
    # QA "Wrong" opens the correction form (what is wrong, and optionally the right decision); "Can't resolve" is a click
    for a in ("confirm_match", "reject_match", "unresolvable", "qa_ok", "accept", "do_play", "snooze", "dismiss", "not_now"):
        assert APP.app.handler_for(a) is APP.on_click, a
    for a in ("wrong_person", "wrong_play", "wrong_account", "qa_wrong"):
        assert APP.app.handler_for(a) is APP.on_correction, a
    assert APP.app.handler_for("paste_profile") is APP.on_paste_profile
    assert set(APP.app.views) == {"correction", "paste_profile"} and set(APP.app.commands) == {"/pipeline"}
    with fresh():
        seen, fake = _clicks_seen()
        fake.result = {"ok": True, "allowed": True, "message": "✔ This is them", "event": {}}
        APP.handle_click = fake
        client = FakeClient()
        body = _click_body("confirm_match", "R1:ROW7|li:jane", "id:ROW7", "U_REVIEW", channel="D_OWNER")
        APP.app.handler_for("confirm_match")(ack=lambda *a, **k: None, body=body, client=client)
        assert seen[0]["value"] == "R1:ROW7|li:jane"
        assert client.named("chat_update")[0]["blocks"][1]["block_id"] == "id:ROW7"


def test_paste_profile_opens_the_form_and_saving_records_the_url_and_closes_the_row():
    with fresh():
        ops = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Jane Doe* · Hold"}},
               {"type": "actions", "block_id": "id:ROW7", "elements": [{"type": "button", "action_id": "paste_profile"}]}]
        client = FakeClient()
        body = _click_body("paste_profile", "R1:ROW7", "id:ROW7", "U_REVIEW", channel="D_OWNER", ts="2.0", blocks=ops)
        APP.app.handler_for("paste_profile")(ack=lambda *a, **k: None, body=body, client=client)
        opened = client.named("views_open")
        assert len(opened) == 1 and opened[0]["trigger_id"] == "T1"
        view = opened[0]["view"]
        assert view["callback_id"] == "paste_profile"
        url_block = next(b for b in view["blocks"] if b.get("block_id") == "url")
        assert url_block["element"]["action_id"] == "text"

        seen, fake = _clicks_seen()
        fake.result = {"ok": True, "allowed": True, "message": "✔ profile saved", "event": {}}
        APP.handle_click = fake
        client.history[("D_OWNER", "2.0")] = {"ts": "2.0", "text": "queue", "blocks": ops}
        acks = []
        bad = dict(view, state={"values": {"url": {"text": {"type": "plain_text_input", "value": "jane doe"}}}})
        APP.app.views["paste_profile"](ack=lambda *a, **k: acks.append(k), body={"user": {"id": "U_REVIEW"}}, view=bad,
                                       client=client)
        assert acks[0].get("response_action") == "errors" and "url" in acks[0]["errors"] and seen == []
        url = "https://www.linkedin.com/in/jane-doe"
        good = dict(view, state={"values": {"url": {"text": {"type": "plain_text_input", "value": f" {url} "}}}})
        APP.app.views["paste_profile"](ack=lambda *a, **k: acks.append(k), body={"user": {"id": "U_REVIEW"}}, view=good,
                                       client=client)
        assert acks[-1] == {} and [(s["action"], s["value"], s["user"], s["note"]) for s in seen] == [
            ("paste_profile", "R1:ROW7", "U_REVIEW", url)]
        upd = client.named("chat_update")
        assert upd[0]["channel"] == "D_OWNER" and upd[0]["ts"] == "2.0"
        assert upd[0]["blocks"][1] == {"type": "context", "block_id": "id:ROW7",
                                       "elements": [{"type": "mrkdwn", "text": "✔ profile saved"}]}
        bare = dict(view, state={"values": {"url": {"text": {"value": "linkedin.com/in/jane-doe"}}}})
        APP.app.views["paste_profile"](ack=lambda *a, **k: acks.append(k), body={"user": {"id": "U_REVIEW"}}, view=bare,
                                       client=client)                  # the form accepts what handle_click accepts
        assert acks[-1] == {} and seen[-1]["note"] == "linkedin.com/in/jane-doe"


def test_a_pasted_profile_reaches_the_real_handler_with_the_url():
    """End to end through the real handle_click: the URL is the note and the queue row's block closes."""
    with fresh() as out:
        rdir = os.path.join(out, "replay", "RUN1")
        os.makedirs(rdir)
        with open(os.path.join(out, "replay", "LATEST"), "w", encoding="utf-8") as f:
            f.write("RUN1")
        row = {"Row id": "ROW7", "Person": "Jane Doe", "Case study": "Acme", "Decision": "Hold", "Reason": "same name",
               "Rule version": "test"}
        with open(os.path.join(rdir, "champion_ROW7.json"), "w", encoding="utf-8") as f:
            json.dump({"kind": "champion", "today": "2026-10-18", "seed": {}, "lookup": None, "row": row}, f)
        assert APP.feedback_loop is feedback_loop                     # the app opens the real form, not a copy
        view = feedback_loop.paste_modal("RUN1:ROW7", "D_OWNER", "2.0", "id:ROW7")
        ops = [{"type": "actions", "block_id": "id:ROW7", "elements": [{"type": "button", "action_id": "paste_profile"}]}]
        client = FakeClient()
        client.history[("D_OWNER", "2.0")] = {"ts": "2.0", "text": "queue", "blocks": ops}
        url = "https://www.linkedin.com/in/jane-doe"
        APP.app.views["paste_profile"](ack=lambda *a, **k: None, body={"user": {"id": "U_REVIEW"}},
                                       view=dict(view, state={"values": {"url": {"text": {"value": url}}}}), client=client)
        with open(CFG["feedback_log"], encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        assert events[-1]["action"] == "paste_profile" and url in json.dumps(events[-1]), events[-1]
        upd = client.named("chat_update")
        assert upd and upd[0]["blocks"][0]["type"] == "context" and upd[0]["blocks"][0]["block_id"] == "id:ROW7", client.calls


class SlackLikeClient(FakeClient):
    """conversations.history returns top-level messages only (newest with ts <= latest); conversations.replies returns
    the thread's parent first, then the replies inside oldest..latest. That is how Slack answers."""

    def __init__(self, messages):
        super().__init__()
        self.messages = messages

    def conversations_history(self, channel, latest, inclusive=True, limit=1, **kw):
        self.calls.append(("conversations_history", {"channel": channel, "latest": latest}))
        top = [m for m in self.messages if m.get("thread_ts") in (None, m["ts"])]
        hits = sorted((m for m in top if float(m["ts"]) <= float(latest)), key=lambda m: -float(m["ts"]))
        return {"ok": True, "messages": hits[:limit]}

    def conversations_replies(self, channel, ts, oldest=None, latest=None, inclusive=True, limit=10, **kw):
        self.calls.append(("conversations_replies", {"channel": channel, "ts": ts, "oldest": oldest, "latest": latest}))
        parent = [m for m in self.messages if m["ts"] == ts]
        rest = [m for m in self.messages if m.get("thread_ts") == ts and m["ts"] != ts
                and (oldest is None or float(m["ts"]) >= float(oldest)) and (latest is None or float(m["ts"]) <= float(latest))]
        return {"ok": True, "messages": (parent + rest)[:limit]}


def test_a_form_opened_from_a_thread_reply_card_updates_that_reply_not_the_digest():
    digest = {"ts": "100.0", "thread_ts": "100.0", "text": "Pipeline: 9 accounts",
              "blocks": [{"type": "actions", "block_id": "pg:acct_TOP1", "elements": []}]}
    reply = {"ts": "101.0", "thread_ts": "100.0", "text": "More accounts (7 to 9)",
             "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "*Northwind*"}},
                        {"type": "actions", "block_id": "pg:acct_N7", "elements": [{"type": "button", "action_id": "wrong_play"}]}]}
    with fresh():
        client = SlackLikeClient([digest, reply])
        body = _click_body("wrong_play", "R1:acct_N7", "pg:acct_N7", "U_OWNER", ts="101.0", blocks=reply["blocks"])
        body["message"]["thread_ts"] = "100.0"
        APP.app.handler_for("wrong_play")(ack=lambda *a, **k: None, body=body, client=client)
        view = client.named("views_open")[0]["view"]
        meta = json.loads(view["private_metadata"])
        assert meta["thread_ts"] == "100.0" and meta["ts"] == "101.0", meta
        seen, fake = _clicks_seen()
        fake.result = {"ok": True, "allowed": True, "message": "✖ Wrong play", "event": {"card_id": "acct_N7"}}
        APP.handle_click = fake
        APP.slack_post.set_list_status = lambda *a: None
        APP.app.views["correction"](ack=lambda *a, **k: None, body={"user": {"id": "U_OWNER"}}, client=client,
                                    view=dict(view, state={"values": {"note": {"text": {"value": "a customer now"}}}}))
        upd = client.named("chat_update")
        assert len(upd) == 1 and upd[0]["ts"] == "101.0" and upd[0]["text"] == "More accounts (7 to 9)", upd
        assert [b.get("block_id") for b in upd[0]["blocks"]] == [None, "pg:acct_N7"] and upd[0]["blocks"][1]["type"] == "context"
        assert client.named("conversations_replies")[0]["ts"] == "100.0"
        # a top-level card still reads the channel history; a message that is not this ts is never overwritten
        client2 = SlackLikeClient([digest])
        APP.app.views["correction"](ack=lambda *a, **k: None, body={"user": {"id": "U_OWNER"}}, client=client2,
                                    view=dict(view, private_metadata=json.dumps(dict(meta, thread_ts="")),
                                              state={"values": {"note": {"text": {"value": "x"}}}}))
        assert client2.named("chat_update") == [] and client2.named("chat_postEphemeral"), client2.calls


def test_qa_wrong_opens_the_form_and_the_right_decision_reaches_the_test_case():
    with fresh():
        client = FakeClient()
        body = _click_body("qa_wrong", "R1:ROW7", "qa:ROW7", "U_REVIEW", channel="D_OWNER", ts="3.0")
        APP.app.handler_for("qa_wrong")(ack=lambda *a, **k: None, body=body, client=client)
        view = client.named("views_open")[0]["view"]
        assert view["callback_id"] == "correction"
        sel = next(b for b in view["blocks"] if b["block_id"] == "expected")
        assert [o["value"] for o in sel["element"]["options"]] == ["Card", "Review", "Hold", "Watch", "No action"]
        assert next(b for b in view["blocks"] if b["block_id"] == "note")["optional"] is True
        seen = []

        def fake(action, value, user, replay_root, cfg, note="", log_path="", expected="", **_):
            seen.append((action, value, note, expected))
            return {"ok": True, "allowed": True, "message": "✖ Wrong (QA)", "event": {}}
        APP.handle_click = fake
        state = {"values": {"note": {"text": {"value": None}},
                            "expected": {"choice": {"type": "static_select", "selected_option": {"value": "Watch"}}}}}
        client.history[("D_OWNER", "3.0")] = {"ts": "3.0", "text": "QA", "blocks": body["message"]["blocks"]}
        APP.app.views["correction"](ack=lambda *a, **k: None, body={"user": {"id": "U_REVIEW"}}, view=dict(view, state=state),
                                    client=client)
        assert seen == [("qa_wrong", "R1:ROW7", "", "Watch")], seen
        state["values"]["expected"]["choice"]["selected_option"] = None          # nothing picked: no expected answer
        APP.app.views["correction"](ack=lambda *a, **k: None, body={"user": {"id": "U_REVIEW"}}, view=dict(view, state=state),
                                    client=client)
        assert seen[-1][3] == ""


def test_a_list_row_that_cannot_be_moved_never_breaks_the_click():
    with fresh() as out:
        seen, fake = _clicks_seen()
        APP.handle_click = fake

        def broken(out_dir, card_id, status):
            raise ValueError("slack_state.json: Expecting value: line 1 column 1")      # not a RuntimeError
        APP.slack_post.set_list_status = broken
        client = FakeClient()
        with contextlib.redirect_stderr(io.StringIO()):
            APP.app.handler_for("do_play")(ack=lambda *a, **k: None, body=_click_body("do_play", "R1:c1", "pg:c1", "U_OWNER"),
                                           client=client)
        assert len(client.named("chat_update")) == 1


# ---- 3. the weekly run ------------------------------------------------------------------------------------------
def test_should_run_is_a_pure_weekly_decision():
    sch = {"weekday": "MON", "hour_utc": 7}
    at = lambda d, h, m=0: datetime(2026, 10, d, h, m, tzinfo=timezone.utc)  # noqa: E731
    assert not APP.should_run(at(19, 6, 59), sch, "")
    assert APP.should_run(at(19, 7), sch, "")
    assert APP.should_run(at(19, 22), sch, "2026-10-12")          # started late on the day: catch up once
    assert not APP.should_run(at(19, 22), sch, "2026-10-19")      # that day already ran
    assert not APP.should_run(at(20, 8), sch, "")                 # the next day: no surprise digest
    assert not APP.should_run(at(19, 9), {}, "")
    assert APP.should_run(at(19, 9), {"weekday": "monday", "hour_utc": "7"}, None)
    assert not APP.should_run(at(19, 9), {"weekday": "XYZ", "hour_utc": 7}, "")
    assert not APP.should_run(at(19, 9), {"weekday": "MON", "hour_utc": "seven"}, "")
    ist = timezone(timedelta(hours=5, minutes=30))                 # judged in UTC whatever the clock's zone
    assert not APP.should_run(datetime(2026, 10, 19, 12, 0, tzinfo=ist), {"weekday": "MON", "hour_utc": 7}, "")


def test_the_scheduler_catches_up_once_and_the_state_file_stops_a_second_run():
    with fresh(mode="live") as out:
        launched = []
        assert APP.scheduler_tick(MONDAY_9, launch=launched.append) == "started"      # app started 2 h late
        state_path = os.path.join(out, "schedule_state.json")
        with open(state_path, encoding="utf-8") as f:
            assert json.load(f)["last_run_date"] == "2026-10-19"                      # written before the run
        later = MONDAY_9 + timedelta(minutes=1)
        assert APP.scheduler_tick(later, launch=launched.append) == "idle" and len(launched) == 1
        with open(os.path.join(out, "heartbeat.json"), encoding="utf-8") as f:
            hb = json.load(f)
        assert hb["ok"] and hb["last_tick"] == "idle" and hb["at"] == later.isoformat() and hb["at_epoch"] == later.timestamp()

        runs = []
        APP.run_pipeline_process = lambda channel="", out="", *a, **k: runs.append((channel, out)) or {"cards": 4,
                                                                                                      "cards_ready": 4}
        launched[0]()                                                                  # the run itself
        with open(state_path, encoding="utf-8") as f:
            st = json.load(f)
        assert runs == [("C_PIPE", out)] and st["result"].startswith("ok") and st["last_run_date"] == "2026-10-19"
        os.remove(state_path)                                                          # the file is the only memory
        assert APP.scheduler_tick(later, launch=launched.append) == "started"


def test_a_failed_weekly_run_alerts_the_review_owner_and_is_not_retried_that_day():
    with fresh(mode="live") as out:
        def boom(*a, **k):
            raise APP.PipelineFailed("the run exited with code 1", "ValueError: contracts/plays.json: rule 3 has no play")
        APP.run_pipeline_process = boom
        launched, err = [], io.StringIO()
        with contextlib.redirect_stderr(err):
            APP.scheduler_tick(MONDAY_9, launch=launched.append)
            launched[0]()
        dm = APP.app.client.named("chat_postMessage")
        assert len(dm) == 1 and dm[0]["channel"] == "U_REVIEW"
        assert "contracts/plays.json" in dm[0]["text"] and "--check" in dm[0]["text"] and "/pipeline" in dm[0]["text"]
        with open(os.path.join(out, "schedule_state.json"), encoding="utf-8") as f:
            assert json.load(f)["result"].startswith("failed")
        assert APP.scheduler_tick(MONDAY_9 + timedelta(hours=1), launch=launched.append) == "idle" and len(launched) == 1


def test_a_broken_config_keeps_the_heartbeat_going_and_marks_it_not_ok():
    with fresh() as out:
        def broken(out_dir=""):
            raise ValueError("config.json: Expecting ',' delimiter")
        APP.load_config = broken
        with contextlib.redirect_stderr(io.StringIO()):
            assert APP.scheduler_tick(MONDAY_9, launch=lambda fn: None) == "error"
        with open(os.path.join(out, "heartbeat.json"), encoding="utf-8") as f:
            hb = json.load(f)
        assert hb["ok"] is False and "delimiter" in hb["error"]


# ---- 2. each run is a fresh process -----------------------------------------------------------------------------
def test_each_run_is_a_fresh_live_process_and_a_failure_brings_back_only_the_last_lines():
    calls = []

    def failing(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 1, stdout="\n".join(f"line {i}" for i in range(40)),
                                           stderr="Traceback (most recent call last):\nValueError: contracts/plays.json: bad rule")
    try:
        APP.run_pipeline_process("C_PIPE", out="/srv/gtm/out", runner=failing)
        raise AssertionError("a failed run must raise")
    except APP.PipelineFailed as e:
        assert "exited with code 1" in str(e) and "ValueError: contracts/plays.json: bad rule" in e.tail
        assert "line 0\n" not in e.tail and len(e.tail.splitlines()) <= APP.TAIL_LINES
    cmd, kw = calls[0]
    assert cmd == [sys.executable, "-m", "pg_system", "--mode", "live", "--post-slack", "--out", "/srv/gtm/out",
                   "--channel", "C_PIPE"], cmd
    assert kw["cwd"] == ROOT and kw["env"]["PYTHONPATH"].split(os.pathsep)[0] == os.path.join(ROOT, "src")
    assert kw["timeout"] == APP.RUN_TIMEOUT_S and kw["capture_output"]

    ok = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"cards": 5, "cards_ready": 3}, indent=2),  # noqa: E731
                                                       stderr="")
    assert APP.run_pipeline_process("", out="/x", runner=ok) == {"cards": 5, "cards_ready": 3}
    assert "--channel" not in APP.pipeline_command("", "/x")

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"], output="fetching gong calls")
    try:
        APP.run_pipeline_process("", out="/x", timeout=5, runner=slow)
        raise AssertionError("a run past its timeout must raise")
    except APP.PipelineFailed as e:
        assert "longer than 5 s" in e.reason and "fetching gong calls" in e.tail


def test_a_real_child_process_failure_comes_back_as_a_message_and_the_app_lives_on():
    out = tempfile.mkdtemp(dir=TMP)
    token = os.environ.pop("SLACK_BOT_TOKEN")        # no token: the child stops at the config check, before any network
    try:
        APP.run_pipeline_process("C_PIPE", out=out, timeout=120)
        raise AssertionError("a run with no Slack token must fail")
    except APP.PipelineFailed as e:
        assert "exited with code 2" in e.reason and "Not run:" in e.tail and "SLACK_BOT_TOKEN" in e.tail, str(e)
    finally:
        os.environ["SLACK_BOT_TOKEN"] = token
    assert not os.path.exists(os.path.join(out, "replay"))


# ---- 6, 7. manifest and packaging ---------------------------------------------------------------------------------
def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


def test_manifest_names_the_app_and_asks_for_every_scope_the_code_calls():
    text = _read("slack_app", "manifest.yaml")
    assert re.search(r"^display_information:\n  name: GTM Signal System$", text, re.M)
    assert re.search(r"^    display_name: GTM Signal System$", text, re.M)
    scopes = set(re.findall(r"^      - ([a-z.:_]+)", text.split("bot:", 1)[1], re.M))
    need = {"chat:write", "im:write", "im:history", "canvases:write", "lists:write", "lists:read", "files:read",
            "commands", "channels:history", "groups:history", "channels:read", "groups:read", "users:read",
            "users:read.email"}
    assert need <= scopes, need - scopes
    assert "socket_mode_enabled: true" in text and "command: /pipeline" in text


def test_pip_install_builds_a_named_package_with_the_live_extra_and_a_command():
    text = _read("pyproject.toml")
    for line in ('requires = ["setuptools>=61"]', 'name = "gtm-signal-system"', 'version = "2.0.0"',
                 'requires-python = ">=3.9"', 'where = ["src"]', 'gtm-signals = "pg_system.pipeline:main"',
                 'live = ["slack_bolt>=1.18", "snowflake-connector-python>=3.8.1"]'):
        assert line in text, line
    assert re.search(r'^description = ".+"$', text, re.M)
    from pg_system.pipeline import main
    assert callable(main)
    pins = [ln.strip() for ln in _read("requirements-live.txt").splitlines() if ln.strip() and not ln.startswith("#")]
    assert pins == ["slack_bolt>=1.18", "snowflake-connector-python>=3.8.1"], pins
    for pkg in ("champion_tracker", "gtm_briefs", "gtm_connectors", "pg_surface", "pg_system", "champion_tracker/adapters"):
        assert os.path.exists(os.path.join(ROOT, "src", pkg, "__init__.py")), pkg


def test_the_image_installs_the_live_extra_runs_the_app_and_bakes_in_no_secrets():
    text = _read("Dockerfile")
    assert re.search(r"^FROM python:3\.11-slim$", text, re.M) and 'pip install --no-cache-dir ".[live]"' in text
    assert 'CMD ["python3", "slack_app/app.py"]' in text and "PYTHONPATH=/app/src" in text
    copies = [ln for ln in text.splitlines() if ln.startswith(("COPY", "ADD"))]
    assert copies and not any(re.match(r"^(COPY|ADD) \.\s", ln) or ".env" in ln or "config.json " in ln for ln in copies), copies
    assert not re.search(r"(TOKEN|SECRET|PASSWORD|KEY)\s*=", text), "no credentials in the image"
    for d in ("src", "contracts", "sql", "slack_app"):
        assert re.search(rf"^COPY {d} \./{d}$", text, re.M), d


def test_a_bad_token_at_start_up_leaves_a_heartbeat_that_says_why():
    """With the real slack_bolt, App() used to call auth.test at import and crash before any heartbeat was written."""
    import json as _json, tempfile as _tf
    app_mod = APP
    class Bad:
        def auth_test(self):
            raise RuntimeError("invalid_auth")
    class Good:
        def auth_test(self):
            return {"ok": True}
    with _tf.TemporaryDirectory() as d:
        assert app_mod.check_token(Bad(), d) is False
        beat = _json.load(open(os.path.join(d, "heartbeat.json")))
        assert beat["ok"] is False and "invalid_auth" in beat["error"] and "SLACK_BOT_TOKEN" in beat["error"]
        assert app_mod.check_token(Good(), d) is True


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

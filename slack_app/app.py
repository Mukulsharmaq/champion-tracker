"""Slack app (Socket Mode, no public URL needed). One process runs the buttons, /pipeline and the weekly run.
Each pipeline run is its own child process (python3 -m pg_system --mode live --post-slack), so an edit to
contracts/plays.json or contracts/lexicon.json counts from the next run without a restart, and a crash inside a run
never takes the buttons down. One run at a time: /pipeline and the schedule share a lock.

  Buttons on the digest      -> pg_system.feedback_loop (log, owner-only CRM write-back + Task, List row status)
  Wrong person / play / account, QA Wrong -> a short form asks what is wrong (and, for a play or a QA row, optionally
                                the right answer); the answer becomes a test case. A form opened from a card in a
                                thread reply reads that reply back (conversations.replies), never the digest above it
  Review queue (to the review owner) -> "This is #n" / "None of these" / "Paste the right profile" / "Can't resolve"
                                decide identity; these and the QA buttons keep working on last week's message while
                                the row is still in the latest run
  QA "Looks right"           -> logged like any other click
  /pipeline                  -> a live run posted to the configured channel (config.json -> pipeline_admins only)
  config.json -> schedule    -> e.g. {"weekday": "MON", "hour_utc": 7}: the weekly live run, posted to the channel

Nothing is posted unless PIPELINE_MODE=live. Demo mode invents accounts, and its buttons would work in the sellers'
channel, so /pipeline says no and the weekly run is skipped with a DM to the review owner.

State the app writes next to the pipeline's (PIPELINE_OUT, default out/):
  schedule_state.json  the date of the last scheduled run, written before the run starts: a restart never posts twice,
                       and an app started late on the scheduled day catches up once
  heartbeat.json       rewritten every minute (at, at_epoch, ok, last_tick, last_run_date, last_result): alert when stale

Setup: api.slack.com/apps > Create New App > From a manifest > slack_app/manifest.yaml; install; create an
app-level token with connections:write; invite the bot to the digest channel and the field-notes channel; then:
  pip install ".[live]"           (or: pip install -r requirements-live.txt)
  export SLACK_BOT_TOKEN=xoxb-...  SLACK_APP_TOKEN=xapp-...  PIPELINE_MODE=live
  PYTHONPATH=src python3 slack_app/app.py
Operating it: docs/runbook.md.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from slack_bolt import App  # noqa: E402
from slack_bolt.adapter.socket_mode import SocketModeHandler  # noqa: E402

from gtm_connectors import slack_post  # noqa: E402
from pg_system import feedback_loop  # noqa: E402
from pg_system.feedback_loop import correction_modal, handle_click, replace_actions  # noqa: E402
from pg_system.pipeline import load_config  # noqa: E402

# Token checked in check_token() at start-up (not at import), so a bad token leaves a heartbeat that says why.
app = App(token=os.environ["SLACK_BOT_TOKEN"], token_verification_enabled=False)
OUT = os.path.abspath(os.environ.get("PIPELINE_OUT", os.path.join(ROOT, "out")))
DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
CLICKS = ("accept", "do_play", "snooze", "dismiss", "not_now", "confirm_match", "reject_match", "unresolvable", "qa_ok")
CORRECTION_CLICKS = ("wrong_person", "wrong_play", "wrong_account", "qa_wrong")
LIST_STATUS = {"do_play": "working", "accept": "working", "snooze": "not_this_week", "not_now": "not_this_week",
               "dismiss": "not_this_week", "wrong_account": "not_this_week"}
RUN_LOCK = threading.Lock()                 # never two runs at once: /pipeline and the schedule share it (and each
                                            # run also holds out/.run.lock, so a manual CLI run cannot overlap either)
RUN_TIMEOUT_S = int(os.environ.get("PIPELINE_TIMEOUT_S", "3600"))
TAIL_LINES = 12

DEMO_REFUSAL = ("Not run: PIPELINE_MODE is not `live` on the app server. Demo mode invents accounts and its buttons "
                "would work, so the app never posts it to Slack. Set PIPELINE_MODE=live once "
                "`python3 -m pg_system --check` passes, then restart the app. To look at the demo, run "
                "`PYTHONPATH=src python3 -m pg_system --out /tmp/demo-out` (files only).")
DEMO_SCHEDULE_ALERT = ("The weekly pipeline run was skipped: PIPELINE_MODE is not `live` on the app server, and demo mode "
                       "would have posted invented accounts with working buttons to the sellers' channel. Set "
                       "PIPELINE_MODE=live once `python3 -m pg_system --check` passes and restart the app: it catches up "
                       "once today.")


def _log(msg: str):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", file=sys.stderr, flush=True)


def live_mode() -> bool:
    return os.environ.get("PIPELINE_MODE", "").strip().lower() == "live"


def _replay() -> str:
    return os.path.join(OUT, "replay")


def _log_path(cfg: Dict) -> str:
    """The same feedback log the pipeline reads (config.json -> feedback_log), so every click reaches the next run."""
    p = cfg.get("feedback_log") or "feedback/feedback.jsonl"
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def _background(fn: Callable):
    threading.Thread(target=fn, daemon=True).start()


def _read_json(path: str) -> Dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, obj: Dict):
    """Write to a temp file, then rename: a crash mid-write never leaves half a state file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    os.replace(tmp, path)


def _alert_owner(cfg: Dict, text: str) -> bool:
    """A problem with the weekly run must reach a person, not only a log line."""
    owner = (cfg.get("review_owner") or {}).get("slack_id")
    if not owner:
        _log("no review_owner.slack_id in config.json, so this alert reached the log only: " + text)
        return False
    try:
        app.client.chat_postMessage(channel=owner, text=text)
        return True
    except Exception as e:  # noqa: BLE001  (a failed DM must not stop the scheduler)
        _log(f"could not DM the review owner: {e}")
        return False


# ---- one pipeline run = one child process -----------------------------------------------------------------------
class PipelineFailed(RuntimeError):
    """A run that exited non-zero or ran out of time; carries the last lines it printed."""

    def __init__(self, reason: str, tail: str = ""):
        super().__init__(reason + (f"\n{tail}" if tail else ""))
        self.reason, self.tail = reason, tail


def _tail(*texts, lines: int = TAIL_LINES, chars: int = 1500) -> str:
    joined = "\n".join(t.decode("utf-8", "replace") if isinstance(t, bytes) else (t or "") for t in texts)
    kept = [line for line in joined.splitlines() if line.strip()][-lines:]
    return "\n".join(kept)[-chars:]


def pipeline_command(channel: str = "", out: str = "") -> list:
    cmd = [sys.executable, "-m", "pg_system", "--mode", "live", "--post-slack", "--out", out or OUT]
    return cmd + (["--channel", channel] if channel else [])


def _summary(stdout: str, out: str) -> Dict:
    text = stdout or ""
    start = text.find("{")
    if start >= 0:
        try:
            return json.JSONDecoder().raw_decode(text, start)[0]
        except ValueError:
            pass
    return _read_json(os.path.join(out, "summary.json"))


def run_pipeline_process(channel: str = "", out: str = "", timeout: Optional[int] = None,
                         runner: Callable = subprocess.run) -> Dict:
    """One live run in a fresh Python process: contract edits count without a restart, and a crash stays in the child.
    Returns the run summary; raises PipelineFailed with the last lines of output when the run fails."""
    out = out or OUT
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    env["PYTHONPATH"] = os.pathsep.join([SRC] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    try:
        p = runner(pipeline_command(channel, out), cwd=ROOT, env=env, capture_output=True, text=True,
                   timeout=timeout or RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise PipelineFailed(f"the run took longer than {e.timeout:.0f} s and was stopped", _tail(e.stdout, e.stderr))
    if p.returncode != 0:
        raise PipelineFailed(f"the run exited with code {p.returncode}", _tail(p.stdout, p.stderr))
    return _summary(p.stdout, out)


def _posted(s: Dict) -> str:
    return f"Posted: {s.get('cards', '?')} accounts ({s.get('cards_ready', '?')} ready). Alerts: {len(s.get('alerts') or [])}."


def _failed(e: Exception) -> str:
    if isinstance(e, PipelineFailed):
        return f"Run failed: {e.reason}." + (f"\n```{e.tail}```" if e.tail else "")
    return f"Run failed: {e}"


# ---- clicks ------------------------------------------------------------------------------------------------------
def _show(client, channel, ts, user, blocks, text, block_id, res, action):
    """Only an allowed click closes the card for everyone; anyone else gets a private note and the buttons stay."""
    if res.get("ok") and res.get("allowed"):
        client.chat_update(channel=channel, ts=ts, text=text, blocks=replace_actions(blocks, block_id, res["message"]))
        card_id = (res.get("event") or {}).get("card_id")
        if card_id and action in LIST_STATUS:
            try:
                slack_post.set_list_status(OUT, card_id, LIST_STATUS[action])
            except Exception as e:  # noqa: BLE001  (the click is saved and the card closed; the List row can wait)
                _log(f"could not move the List row for {card_id}: {e}")
    else:
        client.chat_postEphemeral(channel=channel, user=user, text=res.get("message") or "Not saved.")


def _thread_ts(body: Dict) -> str:
    """The thread a clicked card sits in ('' for a top-level message)."""
    return (body.get("message") or {}).get("thread_ts") or (body.get("container") or {}).get("thread_ts") or ""


def _message(client, channel: str, ts: str, thread_ts: str = "") -> Optional[Dict]:
    """The card a form was opened from (channel or DM: needs channels/groups/im:history). A card in a thread reply is
    read with conversations.replies (conversations.history returns top-level messages only, so it would hand back the
    digest and the update would overwrite the reply with it). Only the message with exactly this ts is returned."""
    try:
        if thread_ts and thread_ts != ts:
            msgs = client.conversations_replies(channel=channel, ts=thread_ts, oldest=ts, latest=ts, inclusive=True,
                                                limit=2)["messages"]
        else:
            msgs = client.conversations_history(channel=channel, latest=ts, inclusive=True, limit=1)["messages"]
        return next((m for m in msgs or [] if str(m.get("ts")) == str(ts)), None)
    except Exception as e:  # noqa: BLE001  (the click is saved either way; say so instead of failing)
        _log(f"could not read message {channel}/{ts}: {e}")
        return None


def _show_saved(client, channel, ts, user, block_id, res, action, thread_ts: str = ""):
    msg = _message(client, channel, ts, thread_ts)
    if msg:
        _show(client, channel, ts, user, msg.get("blocks") or [], msg.get("text", ""), block_id, res, action)
    else:
        client.chat_postEphemeral(channel=channel, user=user,
                                  text=(res.get("message") or "Not saved.") + " (the card itself could not be updated)")


def _identity_block(value: str) -> str:
    """'<run id>:<row id>[|profile]' -> 'id:<row id>', the review-queue row's actions block."""
    return "id:" + (value or "").partition(":")[2].partition("|")[0]


@app.action(re.compile(r"^(" + "|".join(CLICKS) + r")$"))
def on_click(ack, body, client):
    ack()
    a = body["actions"][0]
    cfg = load_config(OUT)
    res = handle_click(a["action_id"], a["value"], body["user"]["id"], _replay(), cfg, log_path=_log_path(cfg))
    msg = body["message"]
    _show(client, body["channel"]["id"], msg["ts"], body["user"]["id"], msg["blocks"], msg.get("text", ""),
          a["block_id"], res, a["action_id"])


@app.action(re.compile(r"^(" + "|".join(CORRECTION_CLICKS) + r")$"))
def on_correction(ack, body, client):
    ack()
    a = body["actions"][0]
    client.views_open(trigger_id=body["trigger_id"], view=correction_modal(
        a["action_id"], a["value"], body["channel"]["id"], body["message"]["ts"], a["block_id"], _thread_ts(body)))


def _form_value(view: Dict, block: str, action: str) -> Dict:
    return (((view.get("state") or {}).get("values") or {}).get(block) or {}).get(action) or {}


@app.view("correction")
def on_correction_saved(ack, body, view, client):
    ack()
    meta = json.loads(view["private_metadata"])
    note = _form_value(view, "note", "text").get("value") or ""
    expected = (_form_value(view, "expected", "choice").get("selected_option") or {}).get("value") or ""
    cfg = load_config(OUT)
    res = handle_click(meta["action"], meta["value"], body["user"]["id"], _replay(), cfg, note=note,
                       log_path=_log_path(cfg), expected=expected)
    _show_saved(client, meta["channel"], meta["ts"], body["user"]["id"], meta["block_id"], res, meta["action"],
                meta.get("thread_ts") or "")


@app.action("paste_profile")
def on_paste_profile(ack, body, client):
    ack()
    a = body["actions"][0]
    client.views_open(trigger_id=body["trigger_id"], view=feedback_loop.paste_modal(
        a["value"], body["channel"]["id"], body["message"]["ts"], a["block_id"], _thread_ts(body)))


@app.view("paste_profile")
def on_paste_profile_saved(ack, body, view, client):
    """The review owner pasted the right profile: saved as that champion's identity, then the queue row closes."""
    url = (((view.get("state") or {}).get("values") or {}).get("url") or {}).get("text") or {}
    url = (url.get("value") or "").strip()
    if not feedback_loop.profile_url(url):          # the same check handle_click applies; the error shows in the form
        ack(response_action="errors", errors={"url": "That is not a profile link. Paste the person's profile URL, "
                                                     "e.g. https://www.linkedin.com/in/their-name"})
        return
    ack()
    meta = json.loads(view.get("private_metadata") or "{}")
    value = meta.get("value", "")
    cfg = load_config(OUT)
    res = handle_click("paste_profile", value, body["user"]["id"], _replay(), cfg, note=url, log_path=_log_path(cfg))
    _show_saved(client, meta.get("channel", ""), meta.get("ts", ""), body["user"]["id"],
                meta.get("block_id") or _identity_block(value), res, "paste_profile", meta.get("thread_ts") or "")


# ---- /pipeline -----------------------------------------------------------------------------------------------------
def pipeline_refusal(cfg: Dict, channel_id: str, user_id: str) -> str:
    """Why /pipeline must not run for this person in this channel, or '' when it may."""
    admins = set(cfg.get("pipeline_admins") or [])
    configured = cfg.get("channel") or ""
    if not admins:
        who = (cfg.get("review_owner") or {}).get("name") or "the review owner"
        return ("/pipeline is off: config.json -> pipeline_admins is empty. Ask " + who + " or whoever runs the app "
                "server to add Slack user ids there (it counts from the next command, no restart).")
    if user_id not in admins:
        return "Only the people listed in config.json -> pipeline_admins can run /pipeline."
    if not configured:
        return "config.json -> channel is not set, so there is no pipeline channel to post to yet."
    if channel_id != configured:
        return f"/pipeline runs only in the pipeline channel (<#{configured}>), where the digest goes."
    if not live_mode():
        return DEMO_REFUSAL
    return ""


@app.command("/pipeline")
def on_pipeline(ack, command, client):
    why = pipeline_refusal(load_config(OUT), command["channel_id"], command["user_id"])
    if why:
        ack(why)
        return
    if not RUN_LOCK.acquire(blocking=False):
        ack("A run is already going (the weekly run or another /pipeline). Its digest will appear here when it ends.")
        return
    ack("Running the pipeline in a fresh process. The digest, the brief and the List will appear here.")
    channel, user = command["channel_id"], command["user_id"]

    def work():
        try:
            text = _posted(run_pipeline_process(channel))
        except Exception as e:  # noqa: BLE001  (report any failure to the person who asked)
            _log(f"/pipeline run failed: {e}")
            text = _failed(e)
        finally:
            RUN_LOCK.release()
        client.chat_postEphemeral(channel=channel, user=user, text=text)

    try:
        _background(work)
    except Exception:
        RUN_LOCK.release()
        raise


# ---- the weekly run ------------------------------------------------------------------------------------------------
def should_run(now: datetime, schedule: Dict, last_run_date: str) -> bool:
    """The weekly decision, pure: on the scheduled weekday, at or after the hour (UTC), once per date. An app started
    late that day catches up once; a date already recorded in schedule_state.json never runs again."""
    if not schedule:
        return False
    day = str(schedule.get("weekday", "MON")).strip().upper()[:3]
    try:
        hour = int(schedule.get("hour_utc", 7))
    except (TypeError, ValueError):
        return False
    if day not in DAYS:
        return False
    if now.tzinfo:
        now = now.astimezone(timezone.utc)
    return DAYS[now.weekday()] == day and now.hour >= hour and (last_run_date or "") != now.date().isoformat()


def _scheduled_run(channel: str, out: str, day: str):
    with RUN_LOCK:                       # waits for a /pipeline run that is still going
        try:
            result = "ok: " + _posted(run_pipeline_process(channel, out))
        except Exception as e:  # noqa: BLE001  (a failed weekly run must reach a person, not just a log)
            result = "failed: " + (e.reason if isinstance(e, PipelineFailed) else str(e))
            _log(f"scheduled run failed: {e}")
            _alert_owner(load_config(out), f"The weekly pipeline run failed ({day}). " + _failed(e) +
                         "\nRun `PYTHONPATH=src python3 -m pg_system --check` on the app server, fix what it names, then "
                         "re-run with /pipeline in the pipeline channel. The schedule does not retry today.")
    path = os.path.join(out, "schedule_state.json")
    _write_json(path, dict(_read_json(path), result=result[:500],
                           finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds")))


def scheduler_tick(now: Optional[datetime] = None, out: str = "", launch: Optional[Callable] = None) -> str:
    """One minute of the weekly loop: decide with should_run, start at most one run, write the heartbeat.
    Returns what it did: 'started', 'refused' (not live), 'idle', 'no schedule' or 'error'."""
    now = now or datetime.now(timezone.utc)
    out = out or OUT
    launch = launch or _background
    path = os.path.join(out, "schedule_state.json")
    state = _read_json(path)
    did, error = "idle", ""
    try:
        cfg = load_config(out)
        sch = cfg.get("schedule") or {}
        today = now.date().isoformat()
        if not sch:
            did = "no schedule"
        elif should_run(now, sch, state.get("last_run_date", "")):
            if not live_mode():
                did = "refused"
                if state.get("refused_on") != today:         # one alert a day, not one a minute
                    state["refused_on"] = today
                    _write_json(path, state)
                    _log("weekly run skipped: PIPELINE_MODE is not 'live'")
                    _alert_owner(cfg, DEMO_SCHEDULE_ALERT)
            else:
                state.update(last_run_date=today, started_at=now.isoformat(timespec="seconds"), result="running")
                _write_json(path, state)        # recorded before the run: a restart mid-run never posts twice
                channel = cfg.get("channel", "")
                launch(lambda: _scheduled_run(channel, out, today))
                did = "started"
    except Exception as e:  # noqa: BLE001  (the loop must keep beating; the heartbeat carries the error)
        did, error = "error", str(e)[:300]
        _log(f"scheduler error: {error}")
    try:
        _write_json(os.path.join(out, "heartbeat.json"), {
            "at": now.isoformat(timespec="seconds"), "at_epoch": now.timestamp(), "pid": os.getpid(), "ok": not error,
            "error": error, "last_tick": did, "pipeline_mode": os.environ.get("PIPELINE_MODE", ""),
            "last_run_date": state.get("last_run_date", ""), "last_result": state.get("result", "")})
    except OSError as e:
        _log(f"could not write the heartbeat: {e}")
    return did


def scheduler():
    """config.json -> schedule {"weekday": "MON", "hour_utc": 7}: one live run a week, posted to the channel."""
    while True:
        try:
            scheduler_tick()
        except Exception as e:  # noqa: BLE001
            _log(f"scheduler tick failed: {e}")
        time.sleep(60)


def check_token(client, out: str = "") -> bool:
    """auth.test once at start-up. On failure, write a heartbeat with ok=false and the error, and return False,
    so the container's health check and whoever reads heartbeat.json see the reason instead of a restart loop."""
    try:
        client.auth_test()
        return True
    except Exception as e:  # noqa: BLE001
        err = f"Slack token check failed: {str(e)[:200]}. Set a valid SLACK_BOT_TOKEN (xoxb-...)."
        _log(err)
        now = datetime.now(timezone.utc)
        try:
            _write_json(os.path.join(out or OUT, "heartbeat.json"), {"at": now.isoformat(timespec="seconds"),
                        "at_epoch": now.timestamp(), "pid": os.getpid(), "ok": False, "error": err, "last_tick": "start"})
        except OSError:
            pass
        return False


if __name__ == "__main__":
    if not check_token(app.client):
        sys.exit(2)
    if not live_mode():
        _log("PIPELINE_MODE is not 'live': buttons work, but /pipeline and the weekly run refuse to post.")
    threading.Thread(target=scheduler, daemon=True).start()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()

"""Deliver to Slack. Slack shows what the code decided; it never decides.

  canvas    the GTM brief + next steps, created as a tab in the channel (canvases.create with channel_id), then
            replaced in place every run (canvases.edit, operation replace) so the channel keeps one brief
  List      next steps, one row per account (slackLists.create, shared to the channel with slackLists.access.set),
            then updated in place every run (slackLists.items.update / items.create): status, why now, play
  digest    the ranked cards with buttons (chat.postMessage), with links to the canvas and the List
  ops       the champion review queue and run health, sent to the review owner (not to sellers)

  threads   optional follow-ups (thread_messages) posted as replies under the digest: the accounts past the top 6,
            each with its own buttons (pg_surface.slack_out.card_messages)
  review    optional extra messages for the review owner (review_messages), after the ops queue: the weekly QA sample
  ops canvas  optional (ops_canvas_md): the whole review queue, the old moves and the metrics as one canvas shared
            with the review owner, replaced in place on later runs. A user is reached through their DM channel
            (conversations.open -> D..., then canvases.access.set with channel_ids); a channel id is shared directly.
            ops_canvas_shared in the state file stays false until the share works, so every run retries it, and a
            failure is said in the review owner's ops message (the canvas is handled just before that message)

dry_run=True writes the exact requests to out/slack_requests.json instead of calling Slack.
Live needs SLACK_BOT_TOKEN with chat:write, canvases:write, lists:write, lists:read, files:read, and channels:read +
groups:read (conversations.info); the bot must be in the channel. Canvases and Lists need a paid Slack plan: if
either call fails, the digest still posts and says so.
Slack Connect: before anything is written, conversations.info is read for the channel (and a channel ops_to). A
channel shared with another organisation is refused (the brief, the List and the review queue name customers and
people) unless deliver(..., allow_shared=True). If the check itself fails, nothing is posted either.
State (canvas id, List id, row ids per account) is kept in out/slack_state.json between runs, saved right after each
create, so a failure later in the run never makes next week create a second canvas or List. It is written whole
(temp file + rename), so the Slack app never reads half of it; a state file that does not parse stops the run with a
RuntimeError that says how to fix it.
Once the digest is posted, every later step (thread replies, owner DMs, the review canvas, the ops queue, the QA
sample) is tried on its own: a failure is added to result["errors"] and the rest still goes out, and the caller
keeps the posted digest's buttons live (message_ts is set).
Retries: chat.postMessage and every create are never retried on a timeout or 5xx (a second digest or List row);
edits, shares and reads are (IDEMPOTENT below).
"""
import json
import os
from typing import Callable, Dict, List

from . import dump_json_atomic, http_json, need

API = "https://slack.com/api/"
IDEMPOTENT = {"canvases.edit", "canvases.access.set", "slackLists.items.update", "slackLists.access.set", "chat.update",
              "conversations.open", "conversations.info", "users.lookupByEmail", "files.info", "auth.test"}

STATUS_CHOICES = [{"value": "ready", "label": "Ready", "color": "green"},
                  {"value": "check_first", "label": "Check first", "color": "yellow"},
                  {"value": "working", "label": "Working", "color": "blue"},
                  {"value": "not_this_week", "label": "Not this week", "color": "gray"}]
LIST_SCHEMA = [{"key": "account", "name": "Account", "type": "text", "is_primary_column": True},
               {"key": "play", "name": "Play", "type": "text"},
               {"key": "why_now", "name": "Why now", "type": "text"},
               {"key": "contact", "name": "Contact", "type": "text"},
               {"key": "owner", "name": "Owner", "type": "user"},
               {"key": "owner_name", "name": "Owner (CRM)", "type": "text"},
               {"key": "status", "name": "Status", "type": "select",
                "options": {"format": "single_select", "choices": STATUS_CHOICES}},
               {"key": "priority", "name": "Priority", "type": "number", "options": {"precision": 0}},
               {"key": "source", "name": "Source", "type": "link"}]


def _call(method: str, body: Dict, get: bool = False) -> Dict:
    env = need("SLACK_BOT_TOKEN")
    h = {"Authorization": f"Bearer {env['SLACK_BOT_TOKEN']}"}
    if get:
        from urllib.parse import urlencode
        r = http_json("GET", API + method + "?" + urlencode(body), h)
    else:
        r = http_json("POST", API + method, h, body, idempotent=method in IDEMPOTENT)
    if not r.get("ok"):
        raise RuntimeError(f"{method}: {r.get('error')} {r.get('detail') or r.get('response_metadata') or ''}".strip())
    return r


def _rt(text: str) -> List[Dict]:
    return [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": text or "-"}]}]}]


def _status_value(row: Dict) -> str:
    return {"Ready": "ready", "Check first": "check_first", "Working": "working"}.get(row["Status"], "check_first")


def row_fields(r: Dict, col: Dict[str, str]) -> List[Dict]:
    """One next-steps row (slack_out.list_rows) -> List cells keyed by column id."""
    f = [{"column_id": col[k], "rich_text": _rt(r[name])} for k, name in
         (("account", "Account"), ("play", "Play"), ("why_now", "Why now"), ("contact", "Contact"), ("owner_name", "Owner"))]
    if r.get("Owner Slack id"):              # a people column, so each rep can filter the List to their accounts
        f.append({"column_id": col["owner"], "user": [r["Owner Slack id"]]})
    f.append({"column_id": col["status"], "select": [_status_value(r)]})
    f.append({"column_id": col["priority"], "number": [r["Priority"]]})
    if r.get("Source"):
        f.append({"column_id": col["source"], "link": [{"original_url": r["Source"], "display_as_url": False, "display_name": "Source"}]})
    return f


def list_items(rows: List[Dict], col: Dict[str, str]) -> List[List[Dict]]:
    return [row_fields(r, col) for r in rows]


def _load_state(out_dir: str) -> Dict:
    p = os.path.join(out_dir, "slack_state.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            state = json.load(f)
    except ValueError as e:
        raise RuntimeError(f"{p} is not valid JSON ({e}): it was cut off or edited by hand. Restore it from a backup, or "
                           "move it away to start again (the next run then creates a new canvas and List; archive "
                           "the old ones by hand).") from e
    if not isinstance(state, dict):
        raise RuntimeError(f"{p} does not hold a JSON object; restore it or move it away (see above).")
    return state


def _save_state(out_dir: str, state: Dict):
    dump_json_atomic(os.path.join(out_dir, "slack_state.json"), state)


class SharedChannelError(RuntimeError):
    """The channel is shared with another organisation (Slack Connect), or that could not be checked."""


def shared_with_another_org(ch: Dict) -> bool:
    """conversations.info channel -> True when people outside this organisation can read it. is_shared without
    is_org_shared means another org (is_org_shared alone is a workspace of the same Enterprise Grid org)."""
    return bool(ch.get("is_ext_shared") or ch.get("is_pending_ext_shared")
                or (ch.get("is_shared") and not ch.get("is_org_shared")))


def _permalink(call: Callable, file_id: str) -> str:
    try:
        return call("files.info", {"file": file_id}, True)["file"].get("permalink", "")
    except (RuntimeError, KeyError):
        return ""


def deliver(channel: str, digest: Dict, canvas_title: str, canvas_md: str, dry_run: bool = True, out_dir: str = "out",
            next_steps: List[Dict] = (), ops: List[Dict] = (), ops_to: str = "", owner_digests: Dict[str, Dict] = None,
            call: Callable = None, thread_messages: List = (), allow_shared: bool = False,
            review_messages: List = (), ops_canvas_md: str = "", ops_canvas_title: str = "Champion review queue") -> Dict:
    """Post everything. `call` is injectable for tests (defaults to the Slack Web API). thread_messages: payloads
    ({"text", "blocks"} or plain text) posted as replies in the digest's thread. review_messages: payloads posted to
    ops_to after the ops queue. ops_canvas_md: markdown for the review owner's canvas (needs ops_to)."""
    next_steps = list(next_steps)
    state = _load_state(out_dir) if not dry_run else {}
    log: List[Dict] = []

    def do(method: str, body: Dict, get: bool = False) -> Dict:
        log.append({"method": method, "body": body})
        if dry_run:
            ch = {"id": body.get("channel", channel)} if method == "conversations.info" else \
                {"id": f"<DM channel with {body.get('users')}>"} if method == "conversations.open" else channel
            return {"ok": True, "canvas_id": "<canvas id>", "list_id": "<list id>", "ts": "<ts>", "channel": ch,
                    "list_metadata": {"schema": [dict(c, id=f"<column id for {c['key']}>") for c in LIST_SCHEMA]},
                    "items": [], "file": {"permalink": ""}, "item": {"id": "<row id>"}}
        return (call or _call)(method, body, get)

    def saved():                             # after every create: the new id must survive a failure later on
        if not dry_run:
            _save_state(out_dir, state)

    result = {"dry_run": dry_run, "errors": []}
    # 0. Slack Connect: never post customer names and people into a channel another organisation can read.
    for ch_id in [channel] + ([ops_to] if ops_to[:1] in ("C", "G") and ops_to != channel else []):
        try:
            info = do("conversations.info", {"channel": ch_id}, True).get("channel") or {}
        except RuntimeError as e:
            raise SharedChannelError(f"Not posted: could not check whether {ch_id} is shared with another "
                                     f"organisation ({e}). Add channels:read and groups:read to the bot.") from e
        if shared_with_another_org(info) and not allow_shared:
            raise SharedChannelError(f"Not posted: {ch_id} is shared with another organisation (Slack Connect). "
                                     "Use an internal channel, or pass allow_shared=True on purpose.")
    # 1. Canvas: one per channel, replaced in place on later runs.
    canvas_id = state.get("canvas_id")
    try:
        if canvas_id:
            do("canvases.edit", {"canvas_id": canvas_id, "changes": [{"operation": "replace",
                                 "document_content": {"type": "markdown", "markdown": canvas_md}}]})
            do("canvases.edit", {"canvas_id": canvas_id, "changes": [{"operation": "rename",
                                 "title_content": {"type": "markdown", "markdown": canvas_title}}]})
        else:
            canvas_id = do("canvases.create", {"title": canvas_title, "channel_id": channel,
                                               "document_content": {"type": "markdown", "markdown": canvas_md}})["canvas_id"]
            state["canvas_id"] = canvas_id
            saved()
    except RuntimeError as e:
        result["errors"].append(f"canvas: {e}")
        canvas_id = None

    # 2. List: one per channel, rows updated in place (keyed by card id).
    list_id = state.get("list_id")
    if next_steps:
        try:
            if not list_id:
                created = do("slackLists.create", {"name": "Next steps from the pipeline", "schema": LIST_SCHEMA})
                list_id = created["list_id"]
                state["list_id"] = list_id
                state["columns"] = {c["key"]: c["id"] for c in created["list_metadata"]["schema"]}
                state["rows"] = {}
                saved()
                do("slackLists.access.set", {"list_id": list_id, "access_level": "write", "channel_ids": [channel]})
            col, rows = state["columns"], state.setdefault("rows", {})
            seen = set()
            for r in next_steps:
                key = r["Card id"]
                seen.add(key)
                if key in rows:
                    cells = [dict(c, row_id=rows[key]) for c in row_fields(r, col)]
                    do("slackLists.items.update", {"list_id": list_id, "cells": cells})
                else:
                    made = do("slackLists.items.create", {"list_id": list_id, "initial_fields": row_fields(r, col)})
                    rows[key] = (made.get("item") or {}).get("id", "")
                    saved()
            gone = [rid for key, rid in rows.items() if key not in seen and rid]
            for i in range(0, len(gone), 100):     # off this week's list: keep the row (history), mark it
                do("slackLists.items.update", {"list_id": list_id, "cells": [
                    {"row_id": rid, "column_id": col["status"], "select": ["not_this_week"]} for rid in gone[i:i + 100]]})
        except (RuntimeError, KeyError) as e:
            result["errors"].append(f"list: {e}")
            list_id = None

    # 3. Digest, linking to the canvas and the List.
    links = []
    if canvas_id:
        url = "" if dry_run else _permalink(call or _call, canvas_id)
        links.append(f"<{url}|GTM brief (canvas)>" if url else "GTM brief: the canvas tab in this channel")
    if list_id:
        url = "" if dry_run else _permalink(call or _call, list_id)
        links.append(f"<{url}|Next steps (List)>" if url else "Next steps: the List shared to this channel")
    if result["errors"]:
        links.append("Not posted: " + "; ".join(result["errors"])[:300])
    blocks = list(digest["blocks"])
    if links:
        blocks.insert(2, {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(links)}]})
    msg = do("chat.postMessage", {"channel": channel, "text": digest["text"], "blocks": blocks[:50], "unfurl_links": False})
    result.update(canvas_id=canvas_id, list_id=list_id, message_ts=msg.get("ts"))

    # From here on the digest is live: each step is tried on its own, so one failure (a rate limit on a thread reply)
    # never stops the rest, and the caller still moves replay/LATEST to this run (the digest's buttons must work).
    def step(name: str, fn: Callable):
        try:
            fn()
        except Exception as e:  # noqa: BLE001  (a failed follow-up must not hide the posted digest)
            result["errors"].append(f"{name}: {e}")

    def post(body: Dict, payload) -> Dict:
        payload = {"text": payload} if isinstance(payload, str) else dict(payload)
        body = dict(body, text=payload.get("text") or "", unfurl_links=False)
        if payload.get("blocks"):
            body["blocks"] = payload["blocks"][:50]
        return do("chat.postMessage", body)

    result["thread_replies"] = 0
    for n, payload in enumerate(thread_messages, 1):   # follow-ups under the digest: the channel shows one message a week
        def reply(payload=payload):
            post({"channel": channel, "thread_ts": msg.get("ts")}, payload)
            result["thread_replies"] += 1
        step(f"thread reply {n}", reply)

    # 4. Each owner's own accounts by DM (optional).
    for user, d in (owner_digests or {}).items():
        step(f"DM to {user}", lambda user=user, d=d: do("chat.postMessage", {
            "channel": user, "text": d["text"], "blocks": d["blocks"][:50], "unfurl_links": False}))

    # 5. The review owner's canvas (before the ops queue, so a failed share is said in that message): one canvas,
    #    replaced in place, shared with that person through their DM channel (or with their channel), retried every
    #    run until the share works.
    ops_canvas_id, canvas_note = state.get("ops_canvas_id"), ""
    if ops_canvas_md and ops_to:
        doc = {"type": "markdown", "markdown": ops_canvas_md}
        try:
            if ops_canvas_id:
                do("canvases.edit", {"canvas_id": ops_canvas_id, "changes": [{"operation": "replace", "document_content": doc}]})
            else:
                ops_canvas_id = do("canvases.create", {"title": ops_canvas_title, "document_content": doc})["canvas_id"]
                state.update(ops_canvas_id=ops_canvas_id, ops_canvas_shared=False)
                saved()
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"ops canvas: {e}")
            canvas_note = f"The review canvas could not be updated: {e}"
            ops_canvas_id = None
        if ops_canvas_id and not state.get("ops_canvas_shared"):
            try:
                if ops_to[:1] in ("C", "G", "D"):
                    target = ops_to
                else:                            # a user: share through their DM channel with the bot
                    target = (do("conversations.open", {"users": ops_to}).get("channel") or {}).get("id") or ""
                    if not target:
                        raise RuntimeError(f"conversations.open gave no DM channel for {ops_to}")
                do("canvases.access.set", {"canvas_id": ops_canvas_id, "access_level": "write", "channel_ids": [target]})
                state["ops_canvas_shared"] = True
                saved()
            except Exception as e:  # noqa: BLE001
                state["ops_canvas_shared"] = False
                result["errors"].append(f"ops canvas share: {e}")
                canvas_note = (f"The review canvas is not shared with you yet ({e}); the next run tries again. Until "
                               "then the queue is in this message only.")
    result["ops_canvas_id"] = ops_canvas_id
    result["ops_canvas_shared"] = bool(state.get("ops_canvas_shared")) if ops_canvas_id else False

    # 6. The ops queue and the QA sample to the review owner.
    if ops and ops_to:
        ops = list(ops)
        if canvas_note:                          # into the top context block: rows stay 3 blocks, split at 48
            line = {"type": "mrkdwn", "text": canvas_note[:2900]}
            if len(ops) > 1 and ops[1].get("type") == "context" and len(ops[1].get("elements") or []) < 10:
                ops[1] = dict(ops[1], elements=list(ops[1]["elements"]) + [line])
            else:
                ops.insert(min(1, len(ops)), {"type": "context", "elements": [line]})
        for i in range(0, len(ops), 48):
            step("ops queue", lambda i=i: do("chat.postMessage", {"channel": ops_to, "text": "Champion review queue",
                                                                  "blocks": ops[i:i + 48], "unfurl_links": False}))
    if ops_to:
        for n, payload in enumerate(review_messages, 1):
            step(f"review message {n}", lambda payload=payload: post({"channel": ops_to}, payload))

    if dry_run:
        dump_json_atomic(os.path.join(out_dir, "slack_requests.json"), log)
    else:
        _save_state(out_dir, state)
    result["requests"] = len(log)
    return result


def update_message(channel: str, ts: str, text: str, blocks: List[Dict]) -> Dict:
    return _call("chat.update", {"channel": channel, "ts": ts, "text": text, "blocks": blocks})


def set_list_status(out_dir: str, card_id: str, status: str) -> Dict:
    """After a click: move that account's List row to Working / Not this week."""
    state = _load_state(out_dir)
    rid = (state.get("rows") or {}).get(card_id)
    if not (state.get("list_id") and rid):
        return {"skipped": "no List row for this card"}
    return _call("slackLists.items.update", {"list_id": state["list_id"], "cells": [
        {"row_id": rid, "column_id": state["columns"]["status"], "select": [status]}]})

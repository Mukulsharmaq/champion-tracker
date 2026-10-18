"""What happens after someone clicks a button in Slack.

  1. Log it (feedback/feedback.jsonl) with the rule or surface version.
  2. Permission (README "Permissions"):
       identity actions (This is #n, None of these, Paste the right profile, Can't resolve, Wrong person, and the
         weekly QA labels) -> only the review owner's Slack id (config.json -> review_owner.slack_id)
       account-card actions (Run this play, Snooze, Not now, Wrong account, Wrong play)
         -> only that account owner's Slack id. A card with a Salesforce owner id (005...) is decided by that id:
            config.json -> owner_slack_ids[<owner id>]; the owner's NAME counts only when no id entry exists and the
            run found no other owner with that name (the card's owner_name_unique), so two reps called John Smith
            never act on each other's accounts. For a card routed to the default owner (no account owner yet), that
            person
       every other champion-row action -> the review owner
     Anyone else's click is logged as a comment: no CRM write, and the next run ignores it.
  3. Write back to the CRM by record id (dry run until config.json says "write_back": "live"). Any error there is
     reported on the card ("CRM not updated: ...") and never loses the click, which is logged first.
  4. A correction (wrong person / wrong play / wrong account / QA "Wrong") becomes a regression case that carries the
     exact inputs behind the card, so it can be replayed against the next rule change (tests/test_regressions.py).
     The form can also say what was right (the right play, or the right decision for a QA row): the case then passes
     only when the rules give that answer, not merely a different one.

Buttons carry '<run id>:<target>'. Seller-card buttons count only on the latest run (replay/LATEST): older inputs
are stale. Review-queue and QA buttons keep working on an older run's message while the row is still in the latest
run: the click is applied to the latest run's inputs for that row.
"""
import json
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import unquote, urlsplit

from champion_tracker import feedback
from champion_tracker.rules import run as run_rules
from gtm_connectors import salesforce
from pg_surface.surface import build_cards
from .state import profile_key

ROOT = os.environ.get("GTM_HOME") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORRECTIONS = {"wrong_person", "wrong_play", "wrong_account", "qa_wrong"}
IDENTITY = {"confirm_match", "reject_match", "paste_profile", "unresolvable", "wrong_person", "qa_ok", "qa_wrong"}
QA = {"qa_ok", "qa_wrong"}
NO_WRITE = {"qa_ok": "QA label: nothing to write to the CRM", "qa_wrong": "QA label: nothing to write to the CRM",
            "unresolvable": "parked queue row: nothing to write to the CRM"}
LABELS = {"accept": "Work this account", "do_play": "Run this play", "snooze": "Snooze", "dismiss": "Dismiss",
          "not_now": "Not now", "wrong_person": "Wrong person", "wrong_play": "Wrong play", "wrong_account": "Wrong account",
          "confirm_match": "This is them", "reject_match": "None of these", "paste_profile": "Paste the right profile",
          "unresolvable": "Can't resolve", "qa_ok": "Looks right", "qa_wrong": "Wrong (QA)"}
STATUS = {"accept": "Working", "do_play": "Working", "snooze": "Snoozed", "dismiss": "Dismissed", "not_now": "Snoozed",
          "wrong_person": "Identity rejected", "wrong_account": "Account rejected", "wrong_play": "Play rejected",
          "confirm_match": "Identity confirmed", "reject_match": "Identity rejected", "paste_profile": "Identity confirmed"}
DECISIONS = ["Card", "Review", "Hold", "Watch", "No action"]
NO_CARD = "none"
MAX_URL = 300
BAD_URL_CHARS = re.compile(r"[\s<>|]")


def _safe_part(p: str) -> bool:
    """A run id or target from a button value names a file under replay/: never a path out of it."""
    return bool(p) and not any(ch in p for ch in ("/", "\\", "\0")) and not p.startswith(".")


def _plays() -> Dict[str, str]:
    """Play code -> name, from contracts/plays.json, in P1, P2, ... order."""
    from pg_surface.plays import PLAYS
    return {k: PLAYS[k]["name"] for k in sorted(PLAYS, key=lambda k: (len(k), k))}


def _play_code(play: str) -> str:
    """'P2', 'P2 Champion joined a customer' or a champion row's play name -> 'P2'."""
    p = (play or "").strip()
    m = re.match(r"^(P\d+)\b", p)
    if m:
        return m.group(1)
    from champion_tracker import config as C
    names = {v: k for k, v in _plays().items()}
    names.update({v: k for k, v in C.PLAYS.items()})
    return names.get(p, p)


def load_replay(replay_root: str, value: str, action: str = "") -> Optional[Dict]:
    """value = '<run id>:<card or row id>[|<profile url>]'. A click on the latest run (replay/LATEST) loads that run's
    inputs. A click from an older run counts only for review-queue and QA actions (IDENTITY), and only while the row
    is still in the latest run: it is applied to the latest run's inputs for that row. A QA label whose row left the
    latest run is applied to its own run's inputs while that folder is kept (it labels that run's decision)."""
    run, _, target = (value or "").partition(":")
    target, _, profile = target.partition("|")
    latest_path = os.path.join(replay_root, "LATEST")
    if not os.path.exists(latest_path) or not _safe_part(target) or not _safe_part(run):
        return None
    with open(latest_path, encoding="utf-8") as f:
        latest = f.read().strip()
    if run == latest:
        tries = [(run, f"card_{target}.json"), (run, f"champion_{target}.json")]
    elif action in IDENTITY and latest:
        tries = [(latest, f"champion_{target}.json")] + ([(run, f"champion_{target}.json")] if action in QA else [])
    else:
        return None
    for folder, name in tries:
        p = os.path.join(replay_root, folder, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return dict(json.load(f), profile=profile, replay_run=folder, clicked_run=run)
    return None


def _path(p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def profile_url(text: str) -> str:
    """A pasted profile link -> one clean URL, or '' when it is not one.
    Accepted: a linkedin.com profile, https://(www.|xx.)linkedin.com/in/<slug>[/...] (http or no scheme is fine), returned
    normalised (https://www.linkedin.com/in/<slug>, slug unquoted and lower case: one key per person); or any other
    https URL. Rejected: LinkedIn pages that are not a profile (company, search, a bare /in/), plain http outside
    LinkedIn, no host, longer than 300 characters, and whitespace, '<', '>' or '|' anywhere (Slack would read them
    as markup: '<https://x><!channel|...>' becomes an @channel)."""
    s = (text or "").strip()
    if not s or len(s) > MAX_URL or BAD_URL_CHARS.search(s):
        return ""
    if not re.match(r"^https?://", s, re.I):
        if not re.match(r"^([a-z0-9-]+\.)?linkedin\.com/", s, re.I):
            return ""
        s = "https://" + s
    try:
        parts = urlsplit(s)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or "." not in host:
        return ""
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        m = re.match(r"^/in/([^/?#]+)/?", parts.path or "")
        if not m:
            return ""
        slug = unquote(m.group(1)).strip().lower()
        if not slug or BAD_URL_CHARS.search(slug) or "/" in slug:
            return ""
        return profile_key(f"https://www.linkedin.com/in/{slug}")
    return s if scheme == "https" else ""


def card_row(card: Dict) -> Dict:
    return {"Row id": card["id"], "Person": card["who"].get("name") or "", "Case study": card["account"],
            "Decision": f"{card['play']} ({card['status']})", "Reason": card.get("headline") or (card["why_now"] or [""])[0],
            "Rule version": card.get("surface_version", "1.0")}


def account_write_back(event: Dict, crm_id: str, play: str) -> Dict:
    if not crm_id:
        raise ValueError("write-back needs the CRM account id; this card's account is not in the CRM")
    return {"object": "Account", "match_on": {"Id": crm_id}, "fields": {
        "Pipeline_Play__c": play, "Pipeline_Play_Status__c": STATUS[event["action"]],
        "Pipeline_Play_Note__c": event["note"], "Pipeline_Surface_Version__c": event["rule_version"]}}


def play_task(card: Dict, today: str) -> Dict:
    """'Run this play' also creates a Salesforce Task on the account, so the play lives where the rep works. The
    subject starts with salesforce.TASK_MARKER ("[GTM Signal]"): the notes query skips those Tasks, so the system
    never reads its own write-back as a rep's note next week."""
    from datetime import date, timedelta
    due = (date.fromisoformat(today[:10]) + timedelta(days=7)).isoformat()
    body = "\n".join(card["why_now"]) + "\n\nDo: " + card["play_do"] + "\n\nSources:\n" + \
        "\n".join(f"{l['label']}: {l['url']}" for l in card["links"] if l.get("url"))
    return {"object": "Task", "fields": {"WhatId": card["crm_id"],
            "Subject": f"{salesforce.TASK_MARKER} {card['play']} {card['play_name']}"[:255],
            "Description": body[:32000], "ActivityDate": due, "Status": "Not Started",
            "Priority": "High" if card.get("tier") == "High" else "Normal"}}


def card_owner_slack_id(card: Dict, owners: Dict[str, str], owner_id: str = "") -> str:
    """The account owner's Slack id for a card: by Salesforce owner id when the card has one and it is configured;
    by name only when there is no id entry and the name is unique among the run's owners (or the card has no id)."""
    owner_id = owner_id or card.get("owner_id") or ""
    sid = owners.get(owner_id) if owner_id else ""
    if not sid and card.get("owner") and (not owner_id or card.get("owner_name_unique", True)):
        sid = owners.get(card["owner"]) or ""
    return sid or ""


def allowed_ids(action: str, replay: Dict, cfg: Dict) -> set:
    """The Slack ids whose click on this target changes anything (see the module docstring)."""
    review_owner = cfg.get("review_owner") or {}
    review = review_owner.get("slack_id") or ""
    if action in IDENTITY or replay.get("kind") != "card":
        return {review} - {"", None}
    owners = cfg.get("owner_slack_ids") or {}
    card = replay["card"]
    ids = {card_owner_slack_id(card, owners, card.get("owner_id") or replay.get("owner_id") or "")}
    if card.get("owner_routed"):            # no account owner yet: the person the card was routed to
        routed_to_review = card.get("owner") == review_owner.get("name") or not cfg.get("default_owner")
        if routed_to_review:
            ids.add(review)
    return ids - {"", None}


def _expected(action: str, expected: str) -> Dict:
    """The right answer picked in the correction form, checked against the allowed values."""
    e = (expected or "").strip()
    if not e:
        return {}
    if action == "wrong_play" and (e in _plays() or e == NO_CARD):
        return {"expected_play": e}
    if action == "qa_wrong" and e in DECISIONS:
        return {"expected_decision": e}
    return {}


def handle_click(action: str, value: str, user_id: str, replay_root: str, cfg: Dict, note: str = "",
                 log_path: str = "feedback/feedback.jsonl", at: Optional[str] = None, expected: str = "") -> Dict:
    replay = load_replay(replay_root, value, action)
    if not replay:
        return {"ok": False, "allowed": False,
                "message": "This card is from an older run. Use the card in the latest digest."}
    is_card = replay["kind"] == "card"
    pasted = ""
    if action == "paste_profile":
        if is_card:
            return {"ok": False, "allowed": False, "message": "Paste a profile on a review-queue row, not on an account card."}
        pasted = profile_url(note)
        if not pasted:
            return {"ok": False, "allowed": False, "message": "That is not a profile link. Paste the person's profile URL "
                    "(https://www.linkedin.com/in/... is best; any https:// link works). Nothing was saved."}
    row = card_row(replay["card"]) if is_card else replay["row"]

    allowed = user_id in allowed_ids(action, replay, cfg)
    extra = {"allowed": allowed, **_expected(action, expected)}
    if replay.get("clicked_run") and replay.get("replay_run") and replay["clicked_run"] != replay["replay_run"]:
        extra.update(clicked_run=replay["clicked_run"], applied_to_run=replay["replay_run"])
    if is_card:
        b = replay["card"]["built_from"]
        extra.update(card_id=replay["card"]["id"], play=replay["card"]["play"], evidence=b["finding_ids"] + b["row_ids"])
    elif action == "unresolvable":
        extra.update(profile="")
    else:
        extra.update(profile=pasted or replay.get("profile") or row.get("LinkedIn", ""))
    event = feedback.record(action, row, user=user_id, note=pasted or note, log_path=log_path, at=at, extra=extra)

    case = None                      # saved before any write-back, so a CRM failure never loses a correction
    if action in CORRECTIONS:
        case = dict(feedback.regression_case(event) or {}, status="open", action=action, replay=replay,
                    rejected_play=replay["card"]["play"] if is_card else row.get("Play", ""))
        feedback.append_line(_path(cfg.get("regression_file") or "feedback/regression_cases.jsonl"), case)

    wb = {}
    if allowed and action in NO_WRITE:
        wb = {"skipped": NO_WRITE[action]}
        crm = ("row parked: it leaves the review queue's buttons from the next run (any later identity decision "
               "brings it back)") if action == "unresolvable" else "label saved for the weekly QA"
    elif allowed:
        dry = cfg.get("write_back") != "live"
        log = os.path.join(os.path.dirname(log_path) or ".", "crm_write_back.jsonl")
        try:
            update = (account_write_back(event, replay["card"].get("crm_id") or replay.get("account", {}).get("crm_id", ""),
                                         replay["card"]["play"]) if is_card else feedback.crm_write_back(event))
            wb = salesforce.write_back(update, dry_run=dry, log_path=log)
            if is_card and action == "do_play":
                wb["task"] = salesforce.create_record(play_task(replay["card"], event["at"]), dry_run=dry, log_path=log)
            crm = "CRM updated" + (" and a Task created" if wb.get("task") else "") + (" (dry run)" if dry else "")
        except Exception as e:  # noqa: BLE001  (any CRM failure is reported on the card; the click is already logged)
            msg = str(e) or type(e).__name__
            wb, crm = {"skipped": msg}, f"CRM not updated: {msg}"
    elif is_card and action not in IDENTITY:
        wb, crm = {"skipped": "not the account owner"}, "logged as a comment (only the account owner updates the CRM)"
    else:
        wb, crm = {"skipped": "not the review owner"}, "logged as a comment (only the review owner decides identity)"

    when = (at or datetime.now(timezone.utc).isoformat(timespec="minutes"))[11:16]
    msg = f"{'✖' if action in CORRECTIONS else '✔'} <@{user_id}> chose *{LABELS[action]}* at {when} UTC · {crm}"
    if pasted and allowed:
        msg += f" · profile {pasted} is used for this champion from the next run"
    if extra.get("expected_play"):
        msg += " · right answer: " + ("no card" if extra["expected_play"] == NO_CARD else extra["expected_play"])
    if extra.get("expected_decision"):
        msg += f" · right answer: {extra['expected_decision']}"
    if case:
        msg += " · saved as a test case for the next rule change"
    if note and not pasted:
        msg += f" · note: {note[:300]}"
    return {"ok": True, "allowed": allowed, "event": event, "write_back": wb, "regression_case": bool(case), "message": msg}


def replace_actions(blocks: List[Dict], block_id: str, message: str) -> List[Dict]:
    """Swap the clicked card's buttons for a line saying who did what, so the channel sees it is handled."""
    out = []
    for b in blocks:
        if b.get("type") == "actions" and b.get("block_id") == block_id:
            out.append({"type": "context", "block_id": block_id, "elements": [{"type": "mrkdwn", "text": message}]})
        else:
            out.append(b)
    return out


def _option(text: str, value: str) -> Dict:
    return {"text": {"type": "plain_text", "text": text[:75]}, "value": value[:150]}


def expected_options(action: str) -> List[Dict]:
    """The choices for 'what was right' in the correction form: every play in contracts/plays.json plus 'No card'
    for Wrong play; the five decisions for a QA 'Wrong'. Empty for the other corrections."""
    if action == "wrong_play":
        return [_option(f"{k} {name}", k) for k, name in _plays().items()] + [_option("No card: nothing to do here", NO_CARD)]
    if action == "qa_wrong":
        return [_option(d, d) for d in DECISIONS]
    return []


def correction_modal(action: str, target_id: str, channel: str, ts: str, block_id: str, thread_ts: str = "") -> Dict:
    """The form behind Wrong person / Wrong play / Wrong account / QA Wrong. thread_ts (set when the card is a thread
    reply) lets the app read that reply back after saving, instead of the digest it hangs under."""
    blocks = [{"type": "input", "block_id": "note", "optional": action == "qa_wrong",
               "label": {"type": "plain_text", "text": "What is wrong? (becomes a test case)"},
               "element": {"type": "plain_text_input", "action_id": "text", "multiline": True, "max_length": 500}}]
    opts = expected_options(action)
    if opts:
        blocks.append({"type": "input", "block_id": "expected", "optional": True,
                       "label": {"type": "plain_text", "text": "Right play (optional)" if action == "wrong_play"
                                 else "Right decision (optional)"},
                       "hint": {"type": "plain_text", "text": "With an answer here, the test case passes only when the "
                                "rules give exactly this answer."},
                       "element": {"type": "static_select", "action_id": "choice", "options": opts,
                                   "placeholder": {"type": "plain_text", "text": "Pick one"}}})
    return {"type": "modal", "callback_id": "correction",
            "private_metadata": json.dumps({"action": action, "value": target_id, "channel": channel, "ts": ts,
                                            "block_id": block_id, "thread_ts": thread_ts or ""}),
            "title": {"type": "plain_text", "text": LABELS[action]}, "submit": {"type": "plain_text", "text": "Save"},
            "blocks": blocks}


def paste_modal(value: str, channel: str, ts: str, block_id: str, thread_ts: str = "") -> Dict:
    """The form behind 'Paste the right profile'. On submit the app calls
    handle_click("paste_profile", value, user, replay_root, cfg, note=<the URL>)."""
    return {"type": "modal", "callback_id": "paste_profile",
            "private_metadata": json.dumps({"action": "paste_profile", "value": value, "channel": channel, "ts": ts,
                                            "block_id": block_id, "thread_ts": thread_ts or ""}),
            "title": {"type": "plain_text", "text": "Paste the right profile"}, "submit": {"type": "plain_text", "text": "Save"},
            "blocks": [{"type": "input", "block_id": "url", "label": {"type": "plain_text", "text": "Profile URL"},
                        "hint": {"type": "plain_text", "text": "The person's own profile, e.g. https://www.linkedin.com/in/their-name. "
                                 "It is confirmed for this champion on every later run."},
                        "element": {"type": "plain_text_input", "action_id": "text", "max_length": MAX_URL,
                                    "placeholder": {"type": "plain_text", "text": "https://www.linkedin.com/in/..."}}}]}


# ---- replaying corrections against the current rules -----------------------------------------------------------
def replay_case(case: Dict) -> Dict:
    """Run the current rules on the inputs a correction was made on. passes=True means the mistake is now fixed:
    the answer equals the one the corrector picked (expected_play / expected_decision) when they picked one, else it
    is no longer the answer they called wrong."""
    r = case["replay"]
    from datetime import date
    today = date.fromisoformat(r["today"])
    if r["kind"] == "champion":
        seed = r["seed"]
        from champion_tracker.rules import row_id
        over = {row_id(seed): r["override"]} if r.get("override") else None
        rows = run_rules([seed], {(seed["Name"], seed["Slug"]): r["lookup"]} if r.get("lookup") else {}, today,
                         r.get("aliases") or {}, set(r.get("known") or []), overrides=over)
        now = rows[0]["Decision"]
        if case["action"] == "wrong_play":     # fixed when the play changed, or the row no longer reaches a seller
            got = _play_code(rows[0].get("Play", "")) if now in ("Card", "Review") else NO_CARD
            if case.get("expected_play"):
                return {"now": f"{now} {rows[0].get('Play', '')}".strip(), "passes": got == case["expected_play"]}
            return {"now": f"{now} {rows[0].get('Play', '')}".strip(),
                    "passes": now not in ("Card", "Review") or rows[0].get("Play", "") != case.get("rejected_play")}
        if case["action"] == "qa_wrong" or case.get("expect_not"):   # fixed when the decision is no longer that one
            if case.get("expected_decision"):
                return {"now": now, "passes": now == case["expected_decision"]}
            return {"now": now, "passes": now != (case.get("expect_not") or r.get("decision"))}
        bad = {"Card"} if case.get("expect_at_most") == "Review" else {"Card", "Review"}
        return {"now": now, "passes": now not in bad}
    accounts = [r["account"]] + ([r["parent"]] if r.get("parent") else [])   # a subsidiary replays with its parent
    cards = build_cards(accounts, r["champion_rows"], r["findings"], today)
    card = next((c for c in cards if c["account"] == r["account"]["account"]), None)
    if case["action"] == "wrong_account":
        return {"now": card["play"] if card else "no card", "passes": card is None}
    if case.get("expected_play"):
        return {"now": card["play"] if card else "no card",
                "passes": (card["play"] if card else NO_CARD) == case["expected_play"]}
    return {"now": card["play"] if card else "no card", "passes": card is None or card["play"] != case.get("rejected_play")}


def check_regressions(path: str) -> List[Dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                case = json.loads(line)
                out.append(dict(case, **{"result": replay_case(case)}))
    return out


def broken_fixes(cases: List[Dict]) -> List[Dict]:
    """Cases marked fixed that fail again: CI fails on these (--check-regressions exits 1)."""
    return [c for c in cases if c.get("status") == "fixed" and not c["result"]["passes"]]

"""Slack field-notes channel -> signals.

Reads conversations.history ({"ok", "messages": [{"user", "text", "ts", "subtype"?}], "has_more",
"response_metadata": {"next_cursor"}}) and users.list ({"members": [{"id", "real_name", "profile": {"title"}}]}).

Convention for reps: start the note with the account name, e.g. "Tailspin Toys: Omar asked if we can match ...",
or (Workflow Builder forms) put it on an "Account: Tailspin Toys" line. That prefix is matched first, by exact
normalised name (case, punctuation and legal suffixes ignored). Only without a usable prefix is the account looked
for inside the text (whole words, longest name first), and only for names that cannot be ordinary English: two or
more words, or one word of 6+ letters that is not a common word ("we are close to a decision" is not the account
Close; "the target for cards" is not Target). Notes that name no account are skipped and listed, never guessed.
Each signal carries "account_id" = the matched account's CRM id. Thread replies are read too (a note's details often
sit in the thread) and inherit their note's account; posts from Workflow Builder forms are kept when their bot id is
in config.json -> field_notes_bot_ids.

Demo:  load("fixtures/slack/field_notes_history.json", "fixtures/slack/users_list.json", accounts)
Live:  fetch(channel_id, oldest_ts, accounts)   needs SLACK_BOT_TOKEN (scopes: channels:history, users:read)
"""
import html
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from champion_tracker.matching import company_tokens
from . import http_json, load_json, need

SKIP_SUBTYPES = {"channel_join", "channel_leave", "bot_message", "channel_topic", "channel_purpose"}
# Single-word account names that are also everyday words: never matched inside free text, only by the prefix.
COMMON_WORDS = set("""
access action active advance agenda allied amazon anchor answer around avenue balance beacon better beyond bridge
budget button camera canvas capital carbon center chance change circle closer closing common compass connect
contract corner credit custom decide decision deliver demand detail digital direct during effect element energy
engine enough entire equity expand expense family figure finance follow forward friday future garden global golden
ground growth harbor health honest horizon impact income insight island launch legacy letter liberty linear little
living manage market matter meeting method middle mobile moment monday mosaic motion nature normal notion number
office online orange origin output pacific people period person pillar pioneer planet pledge pocket policy portal
pretty pricing proper public quarter rather really reason record reform region relief remote renewal report result
review rocket safety sample school screen second secure select signal silver simple simply social source spring
square stable status steady sterling strike strong studio summit supply switch target theory thrive ticket toward
travel unique united update useful valley vector venture vision wealth window wonder yellow zenith
""".split())


def _exact(accounts: List[Dict], name: str) -> Optional[Dict]:
    """The one account whose normalised name equals `name`; None when none or several do."""
    key = company_tokens(name)
    hits = [a for a in accounts if key and company_tokens(a["account"]) == key]
    return hits[0] if len(hits) == 1 else None


def _safe_in_text(account: str) -> bool:
    """Only names that cannot be ordinary English are looked for inside a note: 2+ words, or 6+ letters and not a
    common word."""
    words = re.findall(r"[A-Za-z0-9&']+", account)
    if len(words) >= 2:
        return True
    return len(words) == 1 and len(words[0]) >= 6 and words[0].lower() not in COMMON_WORDS


def _resolve(text: str, accounts: List[Dict]) -> Tuple[Optional[Dict], str]:
    """(account, note text). The prefix decides first; a whole-text name match is the fallback."""
    form = re.search(r"(?im)^\s*account\s*:\s*(.+?)\s*$", text)
    if form:
        hit = _exact(accounts, form.group(1))
        if hit:
            return hit, text.strip()
    m = re.match(r"^\s*([^:\n]{2,60}):\s+(.*)$", text, re.S)
    if m:
        hit = _exact(accounts, m.group(1))
        if hit:
            return hit, m.group(2).strip()
    for a in sorted(accounts, key=lambda a: -len(a["account"])):
        if _safe_in_text(a["account"]) and re.search(r"\b" + re.escape(a["account"]) + r"\b", text, re.I):
            return a, text.strip()
    return None, text.strip()


def permalink(workspace: str, channel: str, ts: str) -> str:
    return f"https://{workspace}.slack.com/archives/{channel}/p{ts.replace('.', '')}"


def to_signals(history: Dict, users: Dict, accounts: List[Dict], channel: str, workspace: str,
               bot_ids: List[str] = ()) -> Tuple[List[Dict], List[Dict]]:
    people = {u["id"]: u for u in users.get("members", [])}
    out, skipped, thread_account = [], [], {}
    for m in sorted(history.get("messages", []), key=lambda m: float(m.get("ts") or 0)):   # parents before replies
        workflow = m.get("subtype") == "bot_message" and m.get("bot_id") in set(bot_ids)
        if (m.get("subtype") in SKIP_SUBTYPES and not workflow) or not (m.get("text") or "").strip():
            continue
        acct, text = _resolve(html.unescape(m["text"]), accounts)
        if not acct and m.get("thread_ts") and m.get("thread_ts") != m.get("ts"):
            acct = thread_account.get(m["thread_ts"])             # a reply belongs to its note's account
        if acct:
            thread_account[m["ts"]] = acct
        if not acct:
            skipped.append({"ts": m.get("ts"), "text": html.unescape(m["text"]), "reason": "No account named"})
            continue
        u = people.get(m.get("user"), {})
        day = datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc).date().isoformat()
        out.append({"source": "field", "id": f"FN-{m['ts']}", "date": day, "account": acct["account"],
                    "account_id": acct.get("crm_id") or "",
                    "speaker": u.get("real_name") or (u.get("profile") or {}).get("real_name") or m.get("user", ""),
                    "speaker_title": (u.get("profile") or {}).get("title") or "", "side": "internal", "text": text,
                    "url": permalink(workspace, channel, m["ts"])})
    return sorted(out, key=lambda s: s["date"]), skipped


def load(history_path: str, users_path: str, accounts: List[Dict], channel: str = "C0FIELDNOTES",
         workspace: str = "example") -> Tuple[List[Dict], List[Dict]]:
    return to_signals(load_json(history_path), load_json(users_path), accounts, channel, workspace)


def fetch(channel: str, oldest_ts: str, accounts: List[Dict], bot_ids: List[str] = ()) -> Tuple[List[Dict], List[Dict]]:
    env = need("SLACK_BOT_TOKEN")
    h = {"Authorization": f"Bearer {env['SLACK_BOT_TOKEN']}"}
    msgs, cursor = [], ""
    while True:
        r = http_json("GET", f"https://slack.com/api/conversations.history?channel={channel}&oldest={oldest_ts}"
                             f"&limit=200&cursor={cursor}", h)
        if not r.get("ok"):
            raise RuntimeError(f"conversations.history: {r.get('error')}")
        msgs += r.get("messages", [])
        cursor = (r.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    for m in [m for m in msgs if m.get("reply_count")]:          # thread replies (history returns top level only)
        r = http_json("GET", f"https://slack.com/api/conversations.replies?channel={channel}&ts={m['ts']}&limit=200", h)
        msgs += [x for x in r.get("messages", []) if x.get("ts") != m["ts"]]
    members, cursor = [], ""
    while True:
        r = http_json("GET", f"https://slack.com/api/users.list?limit=200&cursor={cursor}", h)
        if not r.get("ok"):
            raise RuntimeError(f"users.list: {r.get('error')}")
        members += r.get("members", [])
        cursor = (r.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    team = http_json("GET", "https://slack.com/api/auth.test", h)
    workspace = (team.get("url") or "https://example.slack.com/").split("//")[1].split(".")[0]
    return to_signals({"messages": msgs}, {"members": members}, accounts, channel, workspace, bot_ids)

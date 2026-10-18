"""Slack outputs for the surface. Slack delivers; it never decides.
  digest_blocks(cards)        one message: the top accounts with why now, who, play and buttons to act or correct
                              (thread=False for an owner's DM: no thread is posted there, so it points to the channel)
  card_messages(cards)        the rest of the accounts, with the same buttons, as thread replies under the digest
  list_rows(cards)            next steps as rows for a Slack List (or a CSV import)
  canvas_markdown(...)        the GTM brief plus the next steps, for the sellers' canvas
  ops_blocks / ops_markdown / ops_canvas_markdown   the champion review queue and run health, for the review owner
Text that came from a source (quotes, names, reasons) goes through neutralize(), so a quote can never ping a channel.
A link is written only when its URL cannot break out of Slack's <url|label> form (no <, >, | or whitespace).
"""
import re
from collections import Counter
from typing import Dict, List

from champion_tracker.config import RECENT_DAYS

BUTTONS = [("do_play", "Run this play", "primary"), ("snooze", "Snooze 1 week", None), ("not_now", "Not now", None),
           ("wrong_account", "Wrong account", "danger"), ("wrong_play", "Wrong play", None)]
SLACK_MAX_BLOCKS = 50
# Review-queue reasons, for the counts on top of the queue (first match wins; the rest is "other").
QUEUE_REASONS = [("same name", r"share this name"), ("no profile", r"No profile"), ("lookup failed", r"Lookup failed"),
                 ("records disagree", r"Records disagree|disagree on where"), ("employer unknown", r"employer unknown|No current employer"),
                 ("rename or acquisition", r"rename|acquisition"), ("identity only likely", r"only Likely"),
                 ("evidence missing", r"evidence sentence"), ("unknown action tag", r"action tag"),
                 ("old job still open", r"still open"), (f"move over {RECENT_DAYS} days", r"days old"),
                 ("move date unknown", r"move date unknown"),
                 ("small or unknown size", r"is small|size unknown"), ("no domain", r"no domain")]
_MENTION = re.compile(r"<([!@#])([^<>|]*)(?:\|([^<>]*))?>")
_CANVAS_MENTION = re.compile(r"!\[[^\]]*\]\(\s*([@#][^)\s]*)\s*\)")


def _short(text: str, n: int = 220) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def esc(text: str) -> str:
    """Slack mrkdwn needs &, < and > escaped in anything that came from a source (names, quotes)."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _mention(m) -> str:
    kind, body, label = m.group(1), m.group(2), (m.group(3) or "").strip()
    if kind == "!":
        if body in ("channel", "here", "everyone"):
            return "@" + body
        if body.startswith("subteam^"):
            return "@" + (label.lstrip("@") or "group")
        return label or body.split("^")[0]          # <!date^...|fallback> -> its fallback text
    return kind + (label.lstrip(kind) or body)


def neutralize(text: str) -> str:
    """Slack mention syntax -> plain text: <!channel> -> @channel, <@U1|ann> -> @ann, <#C1|deals> -> #deals,
    <!subteam^S1|@ops> -> @ops, and the canvas form ![](@U1) -> @U1. A quote must never notify anyone."""
    return _CANVAS_MENTION.sub(lambda m: m.group(1), _MENTION.sub(_mention, text or ""))


def _safe(text) -> str:
    """For mrkdwn: neutralize mentions, then escape."""
    return esc(neutralize(str(text or "")))


def _cell(text) -> str:
    """For a markdown table cell or a link label: no mentions, no column breaks, no stray link brackets."""
    return neutralize(str(text or "")).replace("|", "\\|").replace("\n", " ").replace("[", "(").replace("]", ")")


def _url(u: str) -> str:
    return (u or "").replace(" ", "%20").replace("|", "%7C").replace(")", "%29")


_UNSAFE_URL = re.compile(r"[<>|\s]")


def _link(url, label: str) -> str:
    """A Slack mrkdwn link <url|label>, or '' when the URL is empty or could break out of the link (<, >, | or
    whitespace: 'https://x/><!channel' would otherwise become an @channel). The URL is escaped (& -> &amp;)."""
    u = str(url or "").strip()
    if not u or _UNSAFE_URL.search(u):
        return ""
    return f"<{esc(u)}|{label}>"


def card_blocks(c: Dict) -> List[Dict]:
    who = c["who"]
    who_line = f"{_safe(who['name'])} · {_safe(who['title'])} ({_safe(who['why'])})" if who.get("name") else _safe(who["why"])
    linkedin = _link(who.get("linkedin"), "LinkedIn")
    if linkedin:
        who_line += f" · {linkedin}"
    status = "✅ Ready" if c["status"] == "Ready" else "⚠️ Check first: " + _safe("; ".join(c["checks"]))
    if c.get("working"):
        status = f"🔵 {_safe(c['working'])} · " + status
    why = "\n".join(f"• {_safe(_short(w))}" for w in c["why_now"][:4])
    links = " · ".join(x for x in (_link(l.get("url"), _safe(l["label"])) for l in c["links"][:4]) if x)
    run = c.get("button_value", "").partition(":")[0]
    buttons = [dict({"type": "button", "action_id": a, "value": c.get("button_value", c["id"]), "text": {"type": "plain_text", "text": label}},
                    **({"style": s} if s else {})) for a, label, s in BUTTONS]
    if who.get("row_id"):         # the contact is a matched champion: let the rep say it is the wrong person
        buttons.append({"type": "button", "action_id": "wrong_person", "style": "danger", "text": {"type": "plain_text", "text": "Wrong person"},
                        "value": f"{run}:{who['row_id']}" if run else who["row_id"]})
    kind = c["type"] + (f" (part of {_safe(c['part_of'])})" if c.get("part_of") else "")
    owner = (_safe(c["owner"]) or "unassigned") + (" (routed: no account owner yet)" if c.get("owner_routed") else "")
    head = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{c['rank']}. {_safe(c['account'])}* · {c['tier']} · {kind} · owner {owner}"}}]
    if c.get("context"):          # the account in one line: type, owner, renewal, open opportunity, industry, size, country
        head.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _short(_safe(c["context"]), 2900)}]})
    return head + [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Why now*\n{why}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Contact*\n{who_line}"},
            {"type": "mrkdwn", "text": _short(f"*Play {c['play']}*\n{c['play_name']}", 1900)}]},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _short(f"{status} · Sources: {links or 'none'}", 2900)},
                                         {"type": "mrkdwn", "text": f"Ranked on: {c['rank_reason']}"}]},
        {"type": "actions", "block_id": f"pg:{c['id']}", "elements": buttons},
        {"type": "divider"},
    ]


def digest_blocks(cards: List[Dict], period: tuple, top: int = 6, brief_url: str = "", footer: List[str] = (),
                  thread: bool = True) -> Dict:
    """The top `top` cards with their buttons. thread=True (the channel digest): the rest follow as thread replies
    (card_messages). thread=False (an owner's DM, where no thread is posted): the line says where the rest are."""
    brief = _link(brief_url, "GTM brief")
    head = [{"type": "header", "text": {"type": "plain_text", "text": f"Pipeline: accounts to work ({period[0]} to {period[1]})"}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text":
                f"{len(cards)} accounts with a reason to act · top {min(top, len(cards))} below · "
                f"{sum(1 for c in cards if c['status'] == 'Ready')} ready, {sum(1 for c in cards if c['status'] != 'Ready')} need a check"
                + (f" · {brief}" if brief else "")}]},
            {"type": "divider"}]
    blocks = head + [b for c in cards[:top] for b in card_blocks(c)]
    if len(cards) > top:
        lead = (f"+{len(cards) - top} more accounts in the thread below, each with its buttons: " if thread else
                f"+{len(cards) - top} more of your accounts are in the channel digest and the List, each with its buttons there: ")
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
            _short(lead + ", ".join(_safe(c["account"]) for c in cards[top:]), 2900)}]})
    if footer:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _short(" · ".join(footer), 2900)}]})
    return {"text": f"Pipeline: {len(cards)} accounts to work", "blocks": blocks[:SLACK_MAX_BLOCKS]}


def card_messages(cards: List[Dict], start: int = 6, per_message: int = 7) -> List[Dict]:
    """Every card after the digest's top `start`, with its full card and buttons, as messages to post as thread
    replies under the digest (the caller posts them with thread_ts). Without this only the top 6 accounts had one-click
    actions. Each payload stays within Slack's 50-block limit, whatever per_message is."""
    rest, msgs, i = cards[start:], [], 0
    while i < len(rest):
        chunk, blocks = [], []
        while i < len(rest) and len(chunk) < max(1, per_message):
            cb = card_blocks(rest[i])
            if chunk and 1 + len(blocks) + len(cb) > SLACK_MAX_BLOCKS:
                break
            chunk.append(rest[i])
            blocks += cb
            i += 1
        first, last = start + i - len(chunk) + 1, start + i
        text = f"More accounts to work ({first} to {last} of {len(cards)})"
        msgs.append({"text": text, "blocks": ([{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}] + blocks)[:SLACK_MAX_BLOCKS]})
    return msgs


def list_rows(cards: List[Dict]) -> List[Dict]:
    return [{"Card id": c["id"], "Account": c["account"], "Play": f"{c['play']} {c['play_name']}",
             "Why now": _short(neutralize(c.get("headline") or ""), 240), "Context": neutralize(c.get("context") or ""),
             "Contact": neutralize(c["who"].get("name") or c["who"]["why"]), "Owner": c["owner"],
             "Status": "Working" if c.get("working") else c["status"],
             "Priority": c["priority"], "Tier": c["tier"], "Source": next((l["url"] for l in c["links"] if l.get("url")), "")} for c in cards]


def canvas_markdown(brief_md: str, cards: List[Dict], held_back: List[Dict] = ()) -> str:
    """The sellers' canvas: the brief, then the next steps and each card's evidence. No ops content here."""
    rows = ["| # | Account | Tier | Play | Contact | Owner | Status |", "|---|---|---|---|---|---|---|"]
    for c in cards:
        contact = c["who"].get("name") or c["who"]["why"]
        status = "Working" if c.get("working") else c["status"]
        owner = (c["owner"] or "unassigned") + (" (routed)" if c.get("owner_routed") else "")
        rows.append(f"| {c['rank']} | {_cell(c['account'])} | {c['tier']} | {c['play']} {c['play_name']} | {_cell(contact)} | {_cell(owner)} | {status} |")
    out = brief_md + "\n## Next steps (from the pipeline surface)\n" + "\n".join(rows) + "\n"
    for c in cards:
        out += f"\n### {c['rank']}. {neutralize(c['account'])}: {c['play']} {c['play_name']}\n"
        if c.get("context"):
            out += f"**Context:** {neutralize(c['context'])}\n\n"
        out += f"**Do:** {c['play_do']}\n\n" + "".join(f"- {neutralize(w)}\n" for w in c["why_now"])
        out += "".join(f"- Check first: {neutralize(k)}\n" for k in c["checks"])
        out += f"- Ranked on: {c['rank_reason']}\n"
        out += "\nSources: " + " · ".join(f"[{_cell(l['label'])}]({_url(l['url'])})" for l in c["links"] if l.get("url")) + "\n"
    if held_back:
        out += "\n## Held back by a seller this week\n" + "".join(f"- {neutralize(c['account'])}: {neutralize(c['held_back'])}\n" for c in held_back)
    return out


# ---- the champion review queue (for the review owner, never for sellers) ------------------------------------------
def _age(r: Dict) -> int:
    a = r.get("Move age (days)")
    a = int(a) if isinstance(a, str) and a.strip().isdigit() else a
    return a if isinstance(a, int) else 10 ** 6


def sort_queue(queue: List[Dict]) -> List[Dict]:
    """Review rows first (one look clears them), then Hold rows; champions before users; freshest move first."""
    return sorted(queue, key=lambda r: (r.get("Decision") != "Review", r.get("Level") != "Champion", _age(r), r.get("Person", "")))


def _split(queue: List[Dict]):
    """(open rows, parked rows): a row the review owner marked "Can't resolve" carries unresolvable=True and is parked:
    listed, counted, never asked again (no buttons)."""
    q = sort_queue(queue)
    return [r for r in q if not r.get("unresolvable")], [r for r in q if r.get("unresolvable")]


def _score(c: Dict):
    s = c.get("score")
    return float(s) if isinstance(s, (int, float)) and not isinstance(s, bool) else None


def ranked_candidates(r: Dict) -> List[Dict]:
    """The row's candidate profiles, most likely first when the pipeline scored them ("score"); unscored ones keep
    their order after the scored ones."""
    cands = list(r.get("candidates") or [])
    if not any(_score(c) is not None for c in cands):
        return cands
    return sorted(cands, key=lambda c: (_score(c) is None, -(_score(c) or 0)))


def _score_text(c: Dict) -> str:
    s = _score(c)
    return "" if s is None else f" · score {round(s, 2):g}"


def queue_reason(r: Dict) -> str:
    reason = r.get("Reason") or ""
    return next((name for name, rx in QUEUE_REASONS if re.search(rx, reason, re.I)), "other")


def queue_summary(queue: List[Dict]) -> str:
    """'7 rows: 1 Review, 6 Hold · same name 1 · no profile 1 · ... · 2 parked (can't resolve)': what the open queue is
    made of, before the rows; parked rows are counted apart."""
    q, parked = _split(queue)
    decisions = Counter(r.get("Decision", "") for r in q)
    reasons = sorted(Counter(queue_reason(r) for r in q).items(), key=lambda kv: (-kv[1], kv[0]))
    return (f"{len(q)} rows: {decisions.get('Review', 0)} Review, {decisions.get('Hold', 0)} Hold"
            + "".join(f" · {k} {v}" for k, v in reasons) + (f" · {len(parked)} parked (can't resolve)" if parked else ""))


def _parked_markdown(parked: List[Dict]) -> str:
    if not parked:
        return ""
    out = (f"\n## Parked: can't resolve ({len(parked)})\n\nMarked Can't resolve by the review owner: not asked again.\n\n"
           "| Decision | Person | Was at | Reason | Row id |\n|---|---|---|---|---|\n")
    return out + "".join(f"| {r.get('Decision', '')} | {_cell(r.get('Person'))} | {_cell(r.get('Case study'))} | "
                         f"{_cell(r.get('Reason'))} | {_cell(r.get('Row id'))} |\n" for r in parked)


def ops_markdown(queue: List[Dict], owner: str, alerts: List[str]) -> str:
    q, parked = _split(queue)
    out = f"# Champion review queue ({len(q)} rows, owner: {owner})\n\n{queue_summary(queue)}\n\n"
    out += "| Decision | Level | Person | Was at | Reason |\n|---|---|---|---|---|\n"
    out += "".join(f"| {r['Decision']} | {r.get('Level', '')} | {_cell(r['Person'])} | {_cell(r['Case study'])} | {_cell(r['Reason'])} |\n" for r in q)
    out += _parked_markdown(parked)
    out += "\n## Run health\n" + ("".join(f"- {neutralize(a)}\n" for a in alerts) or "- No alerts.\n")
    return out


def ops_blocks(queue: List[Dict], run_id: str, owner: str, alerts: List[str]) -> List[Dict]:
    """For the review owner: each queued champion with the candidate profiles and one-click identity decisions.
    'This is #n' confirms that profile on every later run; 'None of these' rejects the profiles shown; 'Paste the right
    profile' (every row, including rows with no profile at all) opens a form for the URL the owner found; 'Can't
    resolve' (every row) parks it so it stops coming back. Candidates are shown most likely first (their "score").
    Three blocks on top and three per open row, so 48-block messages split between rows, never inside one; parked rows
    come last as one list with no buttons."""
    q, parked = _split(queue)
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"Champion review queue: {len(q)} rows"}},
              {"type": "context", "elements": [{"type": "mrkdwn", "text": _short(_safe(queue_summary(queue)), 2900)}]},
              {"type": "context", "elements": [{"type": "mrkdwn", "text": _short("Run health: " + _safe(" · ".join(alerts) or "no alerts"), 2900)}]}]
    for r in q:
        cands = ranked_candidates(r)
        text = (f"*{_safe(r['Person'])}* · {_safe(r.get('Level', ''))} · was {_safe(r.get('Title on page', ''))} at {_safe(r['Case study'])}"
                f" · *{r['Decision']}*\n{_safe(r['Reason'])}")
        for i, c in enumerate(cands[:3], 1):
            profile = _link(c.get("linkedin"), "profile")
            text += (f"\n{i}. {_safe(c.get('name'))} · {_safe(c.get('title'))} @ {_safe(c.get('company'))}{_score_text(c)}"
                     + (f" · {profile}" if profile else ""))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _short(text, 2900)}})
        keys = [c["key"] for c in cands[:3] if c.get("key")]
        els = [{"type": "button", "action_id": "confirm_match", "text": {"type": "plain_text", "text": f"This is #{i}"},
                "value": f"{run_id}:{r['Row id']}|{k}"[:2000]} for i, k in enumerate(keys, 1)]
        if keys:
            els.append({"type": "button", "action_id": "reject_match", "style": "danger", "text": {"type": "plain_text", "text": "None of these"},
                        "value": f"{run_id}:{r['Row id']}|{'||'.join(keys)}"[:2000]})
        els.append({"type": "button", "action_id": "paste_profile", "text": {"type": "plain_text", "text": "Paste the right profile"},
                    "value": f"{run_id}:{r['Row id']}"[:2000]})
        els.append({"type": "button", "action_id": "unresolvable", "text": {"type": "plain_text", "text": "Can't resolve"},
                    "value": f"{run_id}:{r['Row id']}"[:2000]})
        blocks.append({"type": "actions", "block_id": f"id:{r['Row id']}", "elements": els})
        blocks.append({"type": "divider"})
    if parked:
        shown = parked[:20]
        text = f"*Parked: can't resolve ({len(parked)})*\n" + "\n".join(
            f"• {_safe(r.get('Person'))} · was at {_safe(r.get('Case study'))} · {r.get('Decision', '')}" for r in shown)
        if len(parked) > len(shown):
            text += f"\n… and {len(parked) - len(shown)} more in the review canvas"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _short(text, 2900)}})
    return blocks


def ops_canvas_markdown(queue: List[Dict], owner: str, alerts: List[str]) -> str:
    """The whole review queue as one table for the review owner's canvas (the message carries the buttons)."""
    q, parked = _split(queue)
    out = f"# Champion review queue ({len(q)} rows, owner: {_cell(owner)})\n\n{queue_summary(queue)}\n\n"
    out += ("Clear a row from the review message: This is #n, None of these, Paste the right profile, or Can't resolve "
            "(parks the row so it stops coming back).\n\n")
    out += ("| # | Decision | Level | Person | Was | Champion at | Moved to | Move age (days) | Reason | Candidates | Row id |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n")
    for i, r in enumerate(q, 1):
        person = f"[{_cell(r['Person'])}]({_url(r['Story'])})" if r.get("Story") else _cell(r["Person"])
        cands = "; ".join(f"{j}. {_cell(c.get('name'))} · {_cell(c.get('title'))} @ {_cell(c.get('company'))}{_score_text(c)}"
                          + (f" ([profile]({_url(c['linkedin'])}))" if c.get("linkedin") else "")
                          for j, c in enumerate(ranked_candidates(r), 1))
        moved = r.get("New company", "") if r.get("Moved?") == "Yes" else ""
        out += (f"| {i} | {r.get('Decision', '')} | {_cell(r.get('Level'))} | {person} | {_cell(r.get('Title on page'))} | "
                f"{_cell(r.get('Case study'))} | {_cell(moved)} | {_cell(r.get('Move age (days)', ''))} | {_cell(r.get('Reason'))} | "
                f"{cands or 'none'} | {_cell(r.get('Row id'))} |\n")
    out += _parked_markdown(parked)
    out += "\n## Run health\n" + ("".join(f"- {neutralize(a)}\n" for a in alerts) or "- No alerts.\n")
    return out


def clay_rows(cards: List[Dict]) -> List[Dict]:
    """The surface as a flat table for a Clay import: the play inputs, the play the code chose, and the evidence.
    clay/surface_formulas.md re-computes the play inside Clay from the input columns (free formula columns).
    'In ICP' is Yes / No / Unknown (the ICP's fields are unknown) / Not set (no ICP given); it moves priority, not play."""
    return [{"Account": c["account"], "Type": c["type"], "Owner": c["owner"], "Near renewal": "Yes" if c["inputs"]["near_renewal"] else "No",
             "Renewal days": "" if c["renewal_days"] is None else c["renewal_days"],
             "Champion in": c["inputs"]["champion_in"], "Champion out": c["inputs"]["champion_out"],
             "Competitor risk": c["inputs"]["competitor_risk"], "Pricing": c["inputs"]["pricing"],
             "Expansion": c["inputs"]["expansion"], "Timing": c["inputs"]["timing"],
             "Open deal": "Yes" if c["inputs"].get("open_deal") else "No",
             "In ICP": {"in": "Yes", "out": "No", "unknown": "Unknown"}.get((c.get("icp") or {}).get("fit"), "Not set"),
             "Play": c["play"],
             "Play name": c["play_name"], "Status": c["status"], "Priority": c["priority"], "Tier": c["tier"],
             "Contact": c["who"].get("name") or c["who"]["why"], "Why now": " | ".join(c["why_now"]),
             "Check first": " | ".join(c["checks"]), "Sources": " | ".join(l["url"] for l in c["links"] if l.get("url"))}
            for c in cards]

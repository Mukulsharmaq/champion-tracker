"""The seller card: who to contact, why now (with evidence), what to do, and buttons to act on or correct it.
Slack is the delivery layer, not the decision layer: the card only shows what rules.py decided.
Output is Slack Block Kit JSON. Posting it needs a Slack app token (not included); the JSON can be pasted into
Slack's Block Kit Builder to preview."""
import json
from typing import Dict, List

ACTIONS = [  # (action_id, label, style)
    ("accept", "Work this account", "primary"),
    ("snooze", "Snooze 30 days", None),
    ("dismiss", "Dismiss", None),
    ("wrong_person", "Wrong person", "danger"),
    ("wrong_play", "Wrong play", None),
]


def build_card(row: Dict) -> Dict:
    """Block Kit message for a Card or Review row. Review rows show what the human must check first."""
    if row.get("Decision") not in ("Card", "Review"):
        raise ValueError("Only Card and Review rows become seller cards")
    review = row["Decision"] == "Review"
    title = ("Check before acting: " if review else "") + f"Champion moved: {row['Person']}"
    blocks: List[Dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Who*\n{row['Person']}"},
            {"type": "mrkdwn", "text": f"*Now*\n{row.get('Now', '')}"},
            {"type": "mrkdwn", "text": f"*Was*\n{row.get('Title on page', '')}, {row['Case study']}"},
            {"type": "mrkdwn", "text": f"*Why now*\nJoined {row.get('Joined new company') or 'date unknown'}"
                                       f" ({row.get('Move age (days)', '?')} days ago)"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"*Evidence they were a champion*\n>{row.get('Evidence (verbatim)', '')}\n<{row.get('Story', '')}|Source>"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Play*: {row.get('Play', '')}\n*Account*: {row.get('New account', '')}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"Same person: {row.get('Same person?', '')} ({row.get('Tied by', '')}) · {row.get('Reason', '')} · rules v{row.get('Rule version', '')}"}]},
        {"type": "actions", "block_id": f"champion:{row.get('Row id', '')}", "elements": [
            dict({"type": "button", "action_id": a, "value": str(row.get("Row id", "")), "text": {"type": "plain_text", "text": label}},
                 **({"style": style} if style else {}))
            for a, label, style in ACTIONS]},
    ]
    if row.get("LinkedIn"):
        blocks[1]["fields"][0]["text"] += f"\n<{row['LinkedIn']}|LinkedIn>"
    return {"text": title, "blocks": blocks}


def to_json(row: Dict) -> str:
    return json.dumps(build_card(row), indent=2, ensure_ascii=False)

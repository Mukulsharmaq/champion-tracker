"""Feedback loop: what happens when the seller acts on or corrects a card.
Every action is appended to a log with the rule version. 'wrong_person' and 'wrong_play' become regression cases,
so the next rule change is tested against every mistake a seller has already caught."""
import json
import os
from datetime import datetime, timezone
from typing import Dict, Optional

from .config import RULE_VERSION

VALID = {"accept", "snooze", "dismiss", "wrong_person", "wrong_play"}


def record(action: str, row: Dict, user: str, note: str = "", log_path: str = "feedback/feedback.jsonl",
           at: Optional[str] = None) -> Dict:
    if action not in VALID:
        raise ValueError(f"unknown action {action!r}; expected one of {sorted(VALID)}")
    event = {"at": at or datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": action, "user": user,
             "note": note, "row_id": row.get("Row id"), "person": row.get("Person"), "case_study": row.get("Case study"),
             "decision": row.get("Decision"), "reason": row.get("Reason"), "rule_version": row.get("Rule version", RULE_VERSION)}
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def regression_case(event: Dict) -> Optional[Dict]:
    """A seller correction, turned into a test case the rules must handle next time."""
    if event["action"] not in ("wrong_person", "wrong_play"):
        return None
    expect = "Hold" if event["action"] == "wrong_person" else "Review"
    return {"row_id": event["row_id"], "person": event["person"], "case_study": event["case_study"], "was": event["decision"],
            "expect_at_most": expect, "why": event["note"] or event["action"], "from_rule_version": event["rule_version"]}


def crm_write_back(event: Dict) -> Dict:
    """Shape of the write-back to the record of truth (CRM field update), keyed on the row id. Wire to your CRM client."""
    status = {"accept": "Working", "snooze": "Snoozed", "dismiss": "Dismissed",
              "wrong_person": "Identity rejected", "wrong_play": "Play rejected"}[event["action"]]
    if not event.get("row_id"):
        raise ValueError("write-back needs a row id (CRM contact id); never match a contact by name")
    return {"object": "Contact", "match_on": {"Id": event["row_id"]}, "fields": {
        "Champion_Move_Status__c": status, "Champion_Move_Note__c": event["note"],
        "Champion_Move_Rule_Version__c": event["rule_version"]}}

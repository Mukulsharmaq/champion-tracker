"""Feedback loop: what happens when the seller acts on or corrects a card.
Every action is appended to a log with the rule version. 'wrong_person', 'wrong_play', 'wrong_account' and the QA
label 'qa_wrong' become regression cases, so the next rule change is tested against every mistake a person has
already caught.

Two processes touch the log: the Slack app appends clicks and the weekly run rewrites it to blank old notes
(pg_system.state.purge). Both take an exclusive lock on '<log>.lock' (locked() below), so a click saved while the
run purges is never lost. The lock is a separate file because the purge replaces the log file itself."""
import contextlib
import json
import os
from datetime import datetime, timezone
from typing import Dict, Iterator, Optional

from .config import RULE_VERSION

try:                                    # POSIX; on Windows the lock is a no-op (one process at a time there)
    import fcntl
except ImportError:                     # pragma: no cover
    fcntl = None

VALID = {"accept", "snooze", "dismiss", "wrong_person", "wrong_play",          # champion cards
         "do_play", "wrong_account", "not_now",                                 # PG surface cards
         "confirm_match", "reject_match", "paste_profile", "unresolvable",      # review queue (identity)
         "qa_ok", "qa_wrong"}                                                   # weekly QA sample (review owner)
CORRECTION_ACTIONS = ("wrong_person", "wrong_play", "wrong_account", "qa_wrong")
STATUS = {"accept": "Working", "do_play": "Working", "snooze": "Snoozed", "dismiss": "Dismissed", "not_now": "Snoozed",
          "wrong_person": "Identity rejected", "wrong_account": "Account rejected", "wrong_play": "Play rejected",
          "confirm_match": "Identity confirmed", "reject_match": "Identity rejected",
          "paste_profile": "Identity confirmed"}       # the reviewer named the right profile
NO_CRM = {"qa_ok", "qa_wrong", "unresolvable"}         # about our rules and our queue, not the contact


@contextlib.contextmanager
def locked(path: str) -> Iterator[None]:
    """Hold an exclusive lock on '<path>.lock' for the block (waits for the other holder). Every append to and every
    rewrite of a shared log goes through this."""
    if fcntl is None:
        yield
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path + ".lock", "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def append_line(path: str, obj: Dict):
    """One JSON line appended under the log's lock."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, default=str) + "\n"
    with locked(path):
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)


def record(action: str, row: Dict, user: str, note: str = "", log_path: str = "feedback/feedback.jsonl",
           at: Optional[str] = None, extra: Optional[Dict] = None) -> Dict:
    if action not in VALID:
        raise ValueError(f"unknown action {action!r}; expected one of {sorted(VALID)}")
    event = {"at": at or datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": action, "user": user,
             "note": note, "row_id": row.get("Row id"), "person": row.get("Person"), "case_study": row.get("Case study"),
             "decision": row.get("Decision"), "reason": row.get("Reason"), "rule_version": row.get("Rule version", RULE_VERSION),
             **(extra or {})}
    append_line(log_path, event)
    return event


def regression_case(event: Dict) -> Optional[Dict]:
    """A correction, turned into a test case the rules must handle next time. A QA 'Wrong' on a row nobody was sent
    (Watch, No action, Hold) is fixed when the decision changes from the one the reviewer called wrong, or, when the
    reviewer picked the right decision (expected_decision), when it equals that. A 'Wrong play' with the right play
    picked (expected_play, or "none" for no card) is fixed only when the play equals it. `at` dates the case, so the
    retention purge can blank its note."""
    if event["action"] not in CORRECTION_ACTIONS:
        return None
    case = {"row_id": event["row_id"], "person": event["person"], "case_study": event["case_study"], "was": event["decision"],
            "why": event["note"] or event["action"], "from_rule_version": event["rule_version"], "at": event.get("at", "")}
    if event["action"] == "qa_wrong":
        case["expect_not"] = event["decision"]
    else:
        case["expect_at_most"] = "Hold" if event["action"] in ("wrong_person", "wrong_account") else "Review"
    for k in ("expected_play", "expected_decision"):
        if event.get(k):
            case[k] = event[k]
    return case


def crm_write_back(event: Dict) -> Dict:
    """Shape of the write-back to the record of truth (CRM field update), keyed on the row id. Wire to your CRM client.
    QA labels (qa_ok, qa_wrong) and 'Can't resolve' (unresolvable) have no write-back: they raise ValueError, which
    callers report as 'CRM not updated'."""
    if event["action"] in NO_CRM:
        raise ValueError("a QA label or a parked queue row is about our rules, not the contact: nothing to write to the CRM")
    status = STATUS[event["action"]]
    if not event.get("row_id"):
        raise ValueError("write-back needs a row id (CRM contact id); never match a contact by name")
    return {"object": "Contact", "match_on": {"Id": event["row_id"]}, "fields": {
        "Champion_Move_Status__c": status, "Champion_Move_Note__c": event["note"],
        "Champion_Move_Rule_Version__c": event["rule_version"]}}

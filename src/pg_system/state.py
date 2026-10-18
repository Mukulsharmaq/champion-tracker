"""What earlier clicks mean for this run. The feedback log is the memory:

  Snooze           the card stays out of the digest for 7 days, counted so a click on digest day (Monday) is still
                   hidden on next Monday's run: hidden while today < click day + 7 + 1, back on the run after
  Not now          14 days, the same way (hidden on the next two weekly runs)
  Dismiss          out until new evidence arrives (a finding or champion row the dismissed card did not have)
  Wrong account    same as dismiss
  Run this play    the card stays, marked "Working since <date>"
  Wrong person / None of these   that profile is rejected for that champion on every later run
  This is them     that profile is confirmed for that champion on every later run
  Paste the right profile        the pasted URL is confirmed for that champion on every later run (the pipeline adds
                                 that profile to the lookup when the provider does not have it)
  Can't resolve    the review owner parks the row: it stays on the review canvas but leaves the queue's buttons
                   until a later identity decision on it (parked())
  Looks right / Wrong (QA)       labels on the weekly QA sample; they change no decision

The latest identity decision on a row wins. Only events with allowed=True (the review owner for identity, the
account owner for cards) change anything; everyone else's clicks are kept as comments.
Profile keys are compared normalised (matching.normalize_profile_url): https://linkedin.com/in/Jane-Roe/ and
https://www.linkedin.com/in/jane-roe are one person, so a profile pasted after "None of these" is not still rejected.
Retention: purge() blanks the free-text note on events older than retention_days, so a seller's words about a
named person are not kept for ever; the action itself stays (metrics and overrides need it). It also blanks the
notes in the regression cases and the CRM write-back log it is given. Every rewrite holds the log's lock
(champion_tracker.feedback.locked), the same lock every click append takes, so no click is lost to a purge.
"""
import json
import os
import re
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

from champion_tracker.feedback import locked

try:                                     # added by the matching builder; the local copy below is the same rule
    from champion_tracker.matching import normalize_profile_url as _normalize_profile_url
except ImportError:                      # pragma: no cover
    _normalize_profile_url = None

HIDE_DAYS = {"snooze": 7, "not_now": 14}
GRACE_DAYS = 1                           # a click on run day must still hide the card on the run `days` later
UNTIL_NEW_EVIDENCE = {"dismiss", "wrong_account"}
IDENTITY_DECISIONS = {"confirm_match", "reject_match", "paste_profile", "wrong_person", "unresolvable"}
NOTE_FIELDS = re.compile(r"(^note$|^why$|Note__c$)")


def _local_normalize(url: str) -> str:
    """https://www.linkedin.com/in/<slug, unquoted, lower case> for a LinkedIn profile URL; '' when it is not one."""
    s = (url or "").strip()
    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    try:
        parts = urlsplit(s)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    m = re.match(r"^/in/([^/?#]+)", parts.path or "")
    if (host == "linkedin.com" or host.endswith(".linkedin.com")) and m:
        slug = unquote(m.group(1)).strip().lower()
        return f"https://www.linkedin.com/in/{slug}" if slug else ""
    return ""


def profile_key(key: str) -> str:
    """One key per person, the same one rules.candidate_key gives a candidate: matching.normalize_profile_url (a
    LinkedIn profile -> https://www.linkedin.com/in/<slug>; other URLs without query, fragment or trailing slash;
    'Name @ Company' trimmed). Without that function, the local LinkedIn rule; other keys as written."""
    k = (key or "").strip()
    if not k:
        return ""
    if _normalize_profile_url is not None:
        try:
            return _normalize_profile_url(k) or k
        except Exception:  # noqa: BLE001  (a bad key must not stop the run; the local rule decides)
            pass
    if re.search(r"linkedin\.com/in/", k, re.I):
        return _local_normalize(k) or k
    return k


def read_log(path: str, bad: Optional[List[int]] = None) -> List[Dict]:
    """Every parsable event, in order. A corrupt line (a half-written append, a hand edit) is skipped instead of
    stopping the weekly run; its line number goes to `bad` so run health can say how many were lost."""
    if not os.path.exists(path):
        return []
    events = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                e = None
            if isinstance(e, dict):
                events.append(e)
            elif bad is not None:
                bad.append(n)
    return events


def overrides(events: List[Dict]) -> Dict[str, Dict]:
    """Identity decisions made by people, applied by champion_tracker.rules.run on every later run.
    {row id: {"reject": [keys], "confirm": key} or {"reject": [...], "confirm_url": pasted url}}, every key normalised
    with profile_key(). The pipeline turns a confirm_url into a confirm on the candidate it adds for that URL, and
    matches each key to the candidate's own key (pipeline.apply_pasted_profiles)."""
    out: Dict[str, Dict] = {}
    for e in events:
        rid, url = e.get("row_id"), e.get("profile")
        if not rid or not url or not e.get("allowed"):      # only people allowed to decide identity
            continue
        action = e.get("action")
        if action not in ("confirm_match", "paste_profile", "wrong_person", "reject_match"):
            continue
        o = out.setdefault(rid, {"reject": []})
        if action == "confirm_match":
            key = profile_key(url)
            o["confirm"] = key
            o.pop("confirm_url", None)
            o["reject"] = [u for u in o["reject"] if u != key]
        elif action == "paste_profile":
            key = profile_key(url)
            o["confirm_url"] = key
            o.pop("confirm", None)
            o["reject"] = [u for u in o["reject"] if u != key]
        else:
            for u in url.split("||"):
                key = profile_key(u)
                if not key:
                    continue
                if key not in o["reject"]:
                    o["reject"].append(key)
                if o.get("confirm") == key:
                    o.pop("confirm")
                if o.get("confirm_url") == key:
                    o.pop("confirm_url")
    return out


def parked(events: List[Dict]) -> Dict[str, Dict]:
    """Row ids the review owner marked "Can't resolve", when that is still the latest identity decision on the row
    (a later This is them / None of these / Paste / Wrong person un-parks it). {row id: the event}."""
    last: Dict[str, Dict] = {}
    for e in events:
        if e.get("row_id") and e.get("allowed") and e.get("action") in IDENTITY_DECISIONS:
            last[e["row_id"]] = e
    return {rid: e for rid, e in last.items() if e["action"] == "unresolvable"}


def hidden_until(action: str, at: date) -> date:
    """The first day a snoozed card shows again. A weekly digest goes out every 7 days, so a snooze clicked on run
    day must still hide the card on the next run (day 7): it shows from day 8, i.e. on the run after."""
    return at + timedelta(days=HIDE_DAYS[action] + GRACE_DAYS)


def apply(cards: List[Dict], events: List[Dict], today: date) -> Tuple[List[Dict], List[Dict]]:
    """Cards -> (shown, held back). Only events from the account owner (allowed=True) change what is shown."""
    last: Dict[str, Dict] = {}
    for e in events:
        if e.get("card_id") and e.get("allowed"):
            last[e["card_id"]] = e
    shown, hidden = [], []
    for c in cards:
        e = last.get(c["id"])
        if not e:
            shown.append(c)
            continue
        at = date.fromisoformat(e["at"][:10])
        evidence = set(c["built_from"]["finding_ids"]) | set(c["built_from"]["row_ids"])
        if e["action"] in HIDE_DAYS and today < hidden_until(e["action"], at):
            hidden.append(dict(c, held_back=f"{e['action'].replace('_', ' ')} by {e['user']} until "
                                            f"{hidden_until(e['action'], at).isoformat()}"))
        elif e["action"] in UNTIL_NEW_EVIDENCE and evidence <= set(e.get("evidence") or []):
            hidden.append(dict(c, held_back=f"{e['action'].replace('_', ' ')} by {e['user']} on {at}; returns with new evidence"))
        else:
            if e["action"] == "do_play":
                c = dict(c, working=f"Working since {at} ({e['user']})")
            shown.append(c)
    for i, c in enumerate(shown, 1):
        c["rank"] = i
    return shown, hidden


def _entry_date(e: Dict) -> str:
    """The date an entry was written: 'at' (events, cases, write-back lines), else the replay's day (older cases)."""
    at = e.get("at") or e.get("logged_at") or ((e.get("replay") or {}).get("today") if isinstance(e.get("replay"), dict) else "")
    return str(at or "")[:10]


def _blank_notes(obj) -> int:
    """Blank every note field inside one entry (note, why, *Note__c, at any depth). Returns how many were blanked."""
    n = 0
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str) and v and NOTE_FIELDS.search(k) and v != obj.get("action"):   # "why": "wrong_play" is no note
                obj[k] = ""
                n += 1
            elif isinstance(v, (dict, list)):
                n += _blank_notes(v)
    elif isinstance(obj, list):
        for v in obj:
            n += _blank_notes(v)
    return n


def _purge_file(path: str, cutoff: str, today: str, whole_entry: bool) -> int:
    """Rewrite one JSON-lines file under its lock, blanking notes on entries dated before `cutoff`. whole_entry=False
    (the feedback log) blanks only the top-level 'note'; True (cases, write-back log) blanks every note field."""
    if not os.path.exists(path):
        return 0
    with locked(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        out, blanked = [], 0
        for line in lines:
            try:
                e = json.loads(line) if line.strip() else None
            except ValueError:
                e = None
            if isinstance(e, dict) and _entry_date(e) and _entry_date(e) < cutoff:
                if whole_entry:
                    n = _blank_notes(e)
                elif e.get("note"):
                    e["note"], n = "", 1
                else:
                    n = 0
                if n:
                    e["note_purged_on"] = today
                    blanked += n if whole_entry else 1
                    line = json.dumps(e, ensure_ascii=False, default=str) + "\n"
            out.append(line if line.endswith("\n") else line + "\n")
        if blanked:
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(out)
            os.replace(tmp, path)
    return blanked


def purge(log_path: str, retention_days: int = 365, today: Optional[date] = None, also: Iterable[str] = ()) -> int:
    """Blank the note text on events older than retention_days and return how many were blanked. Each file is
    rewritten atomically (a temp file, then a rename) while holding its lock, so a crash mid-way never leaves half a
    log and a click appended meanwhile waits instead of being lost. Lines that do not parse are kept as they are:
    they are counted in run health for a person to fix, never silently dropped.
    also: more JSON-lines files whose entries carry notes (feedback/regression_cases.jsonl, crm_write_back.jsonl);
    every note field in their old entries is blanked too. An entry with no date is left alone.
    today defaults to the real date: a demo run pinned to a future day must not wipe live notes."""
    if retention_days is None or int(retention_days) <= 0:
        return 0
    today = today or date.today()
    cutoff = (today - timedelta(days=int(retention_days))).isoformat()
    n = _purge_file(log_path, cutoff, today.isoformat(), whole_entry=False)
    for p in also or ():
        if p and os.path.abspath(p) != os.path.abspath(log_path):
            n += _purge_file(p, cutoff, today.isoformat(), whole_entry=True)
    return n

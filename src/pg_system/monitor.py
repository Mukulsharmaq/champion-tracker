"""Run health, compared with the previous run. A sudden jump in a hold reason usually means a source changed
(a people-data provider, a CRM field, a Gong workspace), not that the world did.

Besides the sources, run health says what the run could not do on its own: searches that hit the result cap,
findings that matched no CRM account, champions who joined a competitor or partner, observer batches that were cut
or came back unreadable, an observer that failed (the run goes on without it), card owners whose clicks cannot change
anything (no Slack id), and feedback-log lines that could not be read."""
import re
from collections import Counter
from typing import Dict, Iterable, List, Optional

HOLD_BUCKETS = [("search capped", r"^Search hit the \d+-result cap"), ("same name", r"share this name"),
                ("no profile", r"No profile"), ("lookup failed", r"Lookup failed"),
                ("records disagree", r"Records disagree"), ("employer unknown", r"current employer unknown"),
                ("rename or acquisition", r"rename|acquisition"), ("identity only likely", r"only Likely")]


def hold_reasons(rows: List[Dict]) -> Dict[str, int]:
    c = Counter()
    for r in rows:
        if r.get("Decision") != "Hold":
            continue
        reason = r.get("Reason") or ""
        c[next((b for b, rx in HOLD_BUCKETS if re.search(rx, reason, re.I)), "other")] += 1
    return dict(c)


def capped_lookups(lookups: Optional[Dict]) -> int:
    """Lookup records whose same-name search came back full (capped=True): the list may be missing the right person."""
    return sum(1 for j in (lookups or {}).values() if isinstance(j, dict) and j.get("capped"))


def summary(rows: List[Dict], signals: List[Dict], findings: List[Dict], cards: List[Dict], skipped: List[Dict],
            lookups: Optional[Dict] = None, unmatched: Optional[List[Dict]] = None, notes: Iterable[str] = (),
            observer: Optional[Dict] = None, owners_without_slack: Iterable[str] = (), corrupt_log_lines: int = 0) -> Dict:
    unmatched = unmatched or []
    return {"decisions": dict(Counter(r["Decision"] for r in rows)), "hold_reasons": hold_reasons(rows),
            "signals_by_source": dict(Counter(s["source"] for s in signals)),
            "findings_by_theme": dict(Counter(f["theme"] for f in findings)),
            "cards": len(cards), "cards_ready": sum(1 for c in cards if c["status"] == "Ready"),
            "field_notes_skipped": len(skipped),
            "calls_without_account": len({s["id"].rsplit("-", 1)[0] for s in signals if s["source"] == "call" and not s.get("account")}),
            "lookups_capped": capped_lookups(lookups),
            "findings_unmatched": len(unmatched),
            "unmatched_accounts": sorted({f.get("account") or "(no account named)" for f in unmatched}),
            "blocked_moves": list(notes or []), "observer": observer or {"enabled": False},
            "owners_without_slack_id": sorted(set(owners_without_slack or [])),
            "feedback_log_corrupt_lines": int(corrupt_log_lines or 0)}


def alerts(cur: Dict, prev: Optional[Dict], jump: float = 0.5, min_abs: int = 3) -> List[str]:
    out = []
    for src in ("call", "crm", "field"):
        if not cur["signals_by_source"].get(src):
            out.append(f"No {src} signals this run: check that connector before trusting the brief.")
    if cur["hold_reasons"].get("lookup failed"):
        out.append(f"{cur['hold_reasons']['lookup failed']} people-data lookups failed: rerun them.")
    if cur.get("lookups_capped"):
        held = cur["hold_reasons"].get("search capped", 0)
        out.append(f"{cur['lookups_capped']} people-data searches hit the result cap ({held} held because of it): "
                   "the right profile may be past the cut. Clear them from the review queue or search with the company.")
    if cur.get("calls_without_account"):
        out.append(f"{cur['calls_without_account']} call(s) had no CRM account linked in Gong: their quotes are in the brief, not on any card.")
    if cur.get("findings_unmatched"):
        names = cur.get("unmatched_accounts") or []
        out.append(f"{cur['findings_unmatched']} finding(s) matched no CRM account and are on no card: "
                   + ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
                   + ". Fix the account name or link in the source.")
    if cur["field_notes_skipped"]:
        out.append(f"{cur['field_notes_skipped']} field note(s) named no account and were skipped (listed in skipped_notes.json).")
    for note in cur.get("blocked_moves") or []:
        out.append(f"Blocked move: {note}.")
    obs = cur.get("observer") or {}
    if obs.get("failed"):
        out.append(f"Observer failed, so this brief has lexicon and Gong-tracker themes only: {obs['failed']}. "
                   "Check the provider (OBSERVER_PROVIDER, OBSERVER_MODEL, its key) before the next run.")
    if obs.get("skipped"):
        out.append(f"Observer is on but did not run: {obs['skipped']}.")
    if obs.get("truncated_by_cap"):
        out.append(f"Observer: {obs['truncated_by_cap']} turn(s) past OBSERVER_MAX_TURNS were not read; raise the cap "
                   "or the brief misses what only the observer catches.")
    if obs.get("malformed_batches"):
        out.append(f"Observer: {obs['malformed_batches']} batch(es) came back unreadable; their turns got no observer themes.")
    if cur.get("owners_without_slack_id"):
        names = cur["owners_without_slack_id"]
        out.append(f"{len(names)} card owner(s) have no Slack id, so their clicks cannot change anything: "
                   + ", ".join(names[:5]) + ". Add them to config.json -> owner_slack_ids (by name or Salesforce owner id).")
    if cur.get("feedback_log_corrupt_lines"):
        out.append(f"{cur['feedback_log_corrupt_lines']} line(s) in the feedback log could not be read and were skipped: "
                   "those clicks are not applied. Fix or remove them.")
    if prev:
        for b, n in cur["hold_reasons"].items():
            was = prev.get("hold_reasons", {}).get(b, 0)
            if n - was >= min_abs and (was == 0 or (n - was) / was >= jump):
                out.append(f"Hold reason '{b}' jumped from {was} to {n}: check the source before clearing the queue.")
        for src, n in prev.get("signals_by_source", {}).items():
            now = cur["signals_by_source"].get(src, 0)
            if n >= 10 and now < n * (1 - jump):
                out.append(f"{src} signals fell from {n} to {now}: a connector may be failing quietly.")
    return out

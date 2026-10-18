"""Run everything and write every output. Demo mode uses the fixtures (no keys); live mode reads the real sources.

  PYTHONPATH=src python3 -m pg_system                                           # demo, Slack requests written to out/
  PYTHONPATH=src python3 -m pg_system --check                                   # live preflight: every source, no writes
  PYTHONPATH=src python3 -m pg_system --mode live                               # live dry run, writes files only
  PYTHONPATH=src python3 -m pg_system --mode live --post-slack                  # live, posts to Slack
  PYTHONPATH=src python3 -m pg_system --post-slack --allow-demo-post --channel C_TEST   # the demo, to a TEST channel
  PYTHONPATH=src python3 -m pg_system --check-regressions                       # replay corrections; exit 1 if a fix broke

--post-slack in demo mode is refused unless --allow-demo-post is given: the demo invents accounts and its buttons
work, so it must only ever reach a test channel.

Outputs (out/): decisions.csv, champion_cards.json, findings.json, brief.md, brief.json, surface.json, next_steps.csv,
clay_surface.csv, slack_digest.json, canvas.md, ops.md (the review owner's canvas: queue, run health, old moves,
metrics, QA sample), review_queue.csv, skipped_notes.json, slack_requests.json, summary.json, people_cache.jsonl
(live), history/<date>.json, history/cards-<run id>.json (never pruned: the backtest reads them), history/first_seen.json
and history/queue_first_seen.json (time to detect, queue age), replay/<run id>/ (the inputs behind each card and queue
row, for corrections), replay/LATEST. Every JSON state file is written whole (temp file + rename).

replay/LATEST names the run whose buttons are live. It moves whenever this run's digest reached Slack (even if a
later step such as a thread reply failed: those buttons are on screen and must work) or in a demo run without
--post-slack. A live dry run writes its replay folder and leaves LATEST alone, so the digest already in Slack keeps
working. history/cards-<run id>.json says dry_run=true when nothing reached Slack, so the backtest skips it.

One run at a time per out/ folder: the run holds an exclusive lock on out/.run.lock, so a manual CLI run and the
Slack app's weekly run can never write the same files at once (the second one stops with a clear message, exit 3).
"""
import argparse
import csv
import hashlib
import inspect
import json
import os
import random
import re
import shutil
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Callable, Dict, List, Optional, Tuple

from champion_tracker.adapters.fixtures import load_lookups, load_seed
from champion_tracker.cli import COLUMNS
from champion_tracker.matching import date_range, domain, mentions_company, same_customer, seed_name_matches
from champion_tracker.rules import candidate_key, row_id, run as run_rules
from champion_tracker.slack_card import build_card
from gtm_connectors import MissingConfig, dump_json_atomic, gong, salesforce, slack_notes, slack_post, warehouse, write_atomic
from gtm_briefs.brief import build_brief, to_markdown
from gtm_briefs.extract import extract
from pg_surface.slack_out import (canvas_markdown, card_messages, clay_rows, digest_blocks, esc, list_rows, neutralize,
                                  ops_blocks, ops_canvas_markdown)
from pg_surface.surface import build_cards
from . import monitor, state
from .feedback_loop import card_owner_slack_id

try:                                     # POSIX; elsewhere the run lock is a no-op
    import fcntl
except ImportError:                      # pragma: no cover
    fcntl = None

ROOT = os.environ.get("GTM_HOME") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FX = lambda *p: os.path.join(ROOT, "fixtures", *p)  # noqa: E731
DEMO_TODAY = date(2026, 10, 18)   # the demo data covers 2026-10-05 to 2026-10-18
DEFAULT_SOURCES = {"warehouse": True, "people": True, "gong": True, "salesforce": True, "field_notes": True}
# people_cache_days 8: weekly runs are exactly 7 days apart, so last week's lookups are still fresh this week.
DEFAULTS = {"people_workers": 8, "people_cache_days": 8, "old_move_days": 365, "qa_sample_size": 5, "keep_runs": 52,
            "retention_days": 365}
SELLER_ACTIONS = {"accept", "do_play", "snooze", "not_now", "dismiss", "wrong_account", "wrong_play", "wrong_person"}
QA_POOL = ("Watch", "No action", "Hold")
DAILY = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
EXIT_LOCKED = 3


class RunLocked(RuntimeError):
    """Another pipeline run holds out/.run.lock."""


def _p(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def _cfg(cfg: Dict, key: str) -> int:
    v = cfg.get(key)
    return int(DEFAULTS[key] if v is None or v == "" else v)


def load_config(out_dir: str = "") -> Dict:
    cfg = {}
    for name in ("config.json", "config.example.json"):
        if os.path.exists(_p(name)):
            with open(_p(name), encoding="utf-8") as f:
                cfg = json.load(f)
            break
    found = os.path.join(out_dir or _p("out"), "owner_slack_ids.json")     # filled by users.lookupByEmail
    if os.path.exists(found):
        try:
            with open(found, encoding="utf-8") as f:
                learned = json.load(f)
        except ValueError as e:
            raise RuntimeError(f"{found} is not valid JSON ({e}); delete it: the next run that posts to Slack finds "
                               "the owners' Slack ids again") from e
        cfg["owner_slack_ids"] = {**learned, **{k: v for k, v in (cfg.get("owner_slack_ids") or {}).items() if v}}
    return cfg


def periods(today: date, days: int) -> Tuple[Tuple[str, str], Tuple[str, str]]:
    end, start = today, today - timedelta(days=days - 1)
    return (start.isoformat(), end.isoformat()), ((start - timedelta(days=days)).isoformat(), (start - timedelta(days=1)).isoformat())


def required_env(cfg: Dict, post_slack: bool) -> List[str]:
    """Every variable live mode needs for the sources switched on in config.json -> sources (and the observer)."""
    src = {**DEFAULT_SOURCES, **(cfg.get("sources") or {})}
    env = os.environ.get
    missing = []
    if src["warehouse"]:
        missing += [n for n in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_DATABASE") if not env(n)]
        if not (env("SNOWFLAKE_PRIVATE_KEY_FILE") or env("SNOWFLAKE_PASSWORD")):
            missing.append("SNOWFLAKE_PRIVATE_KEY_FILE (key-pair) or SNOWFLAKE_PASSWORD")
    elif not cfg.get("champion_seed_csv"):
        missing.append("config.json: champion_seed_csv (warehouse is off)")
    if src["people"] and cfg.get("people_source", "api") == "api":
        missing += [n for n in ("PEOPLE_SEARCH_URL", "PEOPLE_DATA_TOKEN") if not env(n)]
    elif src["people"] and not cfg.get("clay_csv_path"):
        missing.append("config.json: clay_csv_path (people_source is clay_csv)")
    if src["gong"]:
        missing += [n for n in ("GONG_ACCESS_KEY", "GONG_ACCESS_KEY_SECRET", "GONG_BASE_URL") if not env(n)]
    if src["salesforce"]:
        if not env("SF_INSTANCE_URL"):
            missing.append("SF_INSTANCE_URL")
        if not ((env("SF_CLIENT_ID") and env("SF_CLIENT_SECRET")) or env("SF_ACCESS_TOKEN")):
            missing.append("SF_CLIENT_ID + SF_CLIENT_SECRET (or SF_ACCESS_TOKEN)")
    if (src["field_notes"] or post_slack) and not env("SLACK_BOT_TOKEN"):
        missing.append("SLACK_BOT_TOKEN")
    if src["field_notes"] and not cfg.get("field_notes_channel"):
        missing.append("config.json: field_notes_channel")
    if post_slack and not cfg.get("channel"):
        missing.append("config.json: channel (or --channel)")
    if (cfg.get("observer") or {}).get("enabled"):
        from gtm_briefs import observer
        try:
            missing += observer.required_env()
        except ValueError as e:              # an OBSERVER_PROVIDER this code does not know
            missing.append(str(e))
    return missing


# ---- people lookups: parallel, cached ----------------------------------------------------------------------------
def _cache_age(e: Dict, today: date) -> Optional[int]:
    try:
        return (today - date.fromisoformat(str(e.get("fetched_at"))[:10])).days
    except ValueError:
        return None


def _write_cache(path: str, entries: List[Dict]):
    write_atomic(path, "".join(json.dumps(e, ensure_ascii=False, default=str) + "\n" for e in entries))


def lookup_people(seed: List[Dict], today: date, out: str, cfg: Dict, lookup: Callable = None) -> Tuple[Dict, Dict]:
    """(Name, Slug) -> lookup record for every seed row. A record fetched people_cache_days ago or less (default 8,
    so a weekly run reuses last week's) is reused from out/people_cache.jsonl; the rest are looked up people_workers
    at a time. A failed lookup (ok=False) is never cached, so the next run retries it instead of holding the person
    for a week. Profiles fetched by URL for pasted profiles (cached_profile_fetch) share the file and are kept."""
    if lookup is None:
        from champion_tracker.adapters import people_data
        lookup = people_data.lookup_record
    path = os.path.join(out, "people_cache.jsonl")
    max_age = _cfg(cfg, "people_cache_days")
    cache: Dict[Tuple[str, str], Dict] = {}
    by_url: Dict[str, Dict] = {}
    for e in state.read_log(path):
        age = _cache_age(e, today)
        if age is None or not 0 <= age <= max_age:
            continue
        if e.get("url"):                                  # a pasted profile fetched by URL
            if e["url"] not in by_url or e["fetched_at"] > by_url[e["url"]]["fetched_at"]:
                by_url[e["url"]] = e
            continue
        key, rec = (e.get("name"), e.get("slug")), e.get("record")
        if isinstance(rec, dict) and rec.get("ok") and (key not in cache or e["fetched_at"] > cache[key]["fetched_at"]):
            cache[key] = e
    lookups, todo, seen = {}, [], set()
    for p in seed:
        key = (p["Name"], p.get("Slug", ""))
        if key in seen:
            continue
        seen.add(key)
        if key in cache:
            lookups[key] = cache[key]["record"]
        else:
            todo.append((key, p))

    def one(item):
        key, p = item
        try:
            return key, lookup(p)
        except Exception as e:  # noqa: BLE001  (one bad person must not stop the run; the rules Hold them: retry)
            return key, {"name": p["Name"], "slug": p.get("Slug", ""), "customer": p.get("Customer", ""), "ok": False,
                         "error": f"lookup crashed: {e}"[:300], "candidates": []}

    workers = max(1, _cfg(cfg, "people_workers"))
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for key, rec in ex.map(one, todo):
                lookups[key] = rec
    keep = dict(cache)
    for key, _ in todo:
        if lookups[key].get("ok"):
            keep[key] = {"name": key[0], "slug": key[1], "fetched_at": today.isoformat(), "record": lookups[key]}
    os.makedirs(out, exist_ok=True)
    _write_cache(path, [keep[k] for k in sorted(keep, key=lambda k: (str(k[0]), str(k[1])))]
                 + [by_url[u] for u in sorted(by_url)])
    return lookups, {"from_cache": len(seen) - len(todo), "looked_up": len(todo), "workers": workers,
                     "failed": sum(1 for k, _ in todo if not lookups[k].get("ok"))}


def cached_profile_fetch(fetch: Callable, out: str, today: date, cfg: Dict, stats: Optional[Dict] = None) -> Callable:
    """Wrap fetch(url) (people_data.fetch_profile, a paid call) with out/people_cache.jsonl, keyed by the normalised
    URL and kept people_cache_days like the lookups: a pasted profile is fetched once, not on every run. A None
    (unknown profile or a failed call) is not cached, so it is tried again next run."""
    path = os.path.join(out, "people_cache.jsonl")
    max_age = _cfg(cfg, "people_cache_days")
    stats = stats if stats is not None else {}
    stats.setdefault("from_cache", 0)
    stats.setdefault("fetched", 0)

    def get(url: str):
        key = state.profile_key(url)
        entries = state.read_log(path)
        hit = None
        for e in entries:
            age = _cache_age(e, today)
            if e.get("url") == key and age is not None and 0 <= age <= max_age and isinstance(e.get("profile"), dict):
                if hit is None or e["fetched_at"] > hit["fetched_at"]:
                    hit = e
        if hit:
            stats["from_cache"] += 1
            return dict(hit["profile"])
        got = fetch(url)
        stats["fetched"] += 1
        if isinstance(got, dict):
            kept = [e for e in entries if e.get("url") != key]
            _write_cache(path, kept + [{"url": key, "fetched_at": today.isoformat(), "profile": got}])
        return got
    return get


def apply_pasted_profiles(seed: List[Dict], lookups: Dict, overrides: Dict, fetch: Callable = None):
    """Rows where the review owner pasted the right profile (overrides[row id]["confirm_url"]): that profile joins the
    row's lookup candidates (once: matched on the normalised profile key, so the provider's raw spelling and the
    pasted link are one person) and is confirmed by its key. fetch(url) -> a candidate with data, or None; without
    data a bare candidate (the seed's name + the URL) keeps the reviewer's choice, and the row then says what is
    missing (e.g. no current employer on file). A fetched profile under another name (a maiden name) takes the seed's
    name, since the reviewer said it is this person. Every reject and confirm key is the candidate's own key
    (rules.candidate_key) when a candidate matches it normalised. Returns (lookups, overrides, row ids changed)."""
    lookups = dict(lookups)
    overrides = {k: dict(v) for k, v in (overrides or {}).items()}
    by_id = {row_id(p): p for p in seed}
    changed = []
    for rid, o in overrides.items():
        p = by_id.get(rid)
        if not p:
            continue
        key = (p["Name"], p.get("Slug", ""))
        url = o.get("confirm_url")
        if url:
            want = state.profile_key(url)
            rec = dict(lookups.get(key) or {"name": p["Name"], "slug": p.get("Slug", ""), "customer": p.get("Customer", "")})
            cands = list(rec.get("candidates") or [])
            hit = next((c for c in cands if c.get("linkedin_url") and state.profile_key(c["linkedin_url"]) == want), None)
            if hit is None:
                got = fetch(url) if fetch else None
                if got and not seed_name_matches(got, p):
                    got = dict(got, full_name=p["Name"], name_on_profile=got.get("full_name", ""))
                hit = dict(got or {"full_name": p["Name"]})
                hit.setdefault("linkedin_url", url)
                hit["source"] = "pasted by the review owner" + ("" if got else " (no profile data)")
                cands.append(hit)
            rec.update(candidates=cands, ok=True)
            lookups[key] = rec
            o["confirm"] = candidate_key(hit)
            o.pop("confirm_url")
            changed.append(rid)
        own = {state.profile_key(candidate_key(c)): candidate_key(c) for c in (lookups.get(key) or {}).get("candidates") or []}
        if o.get("reject"):
            o["reject"] = [own.get(state.profile_key(u), u) for u in o["reject"]]
        if o.get("confirm"):
            o["confirm"] = own.get(state.profile_key(o["confirm"]), o["confirm"])
    return lookups, overrides, changed


def gather_demo(today: date, cfg: Dict, info: Dict):
    seed = warehouse.champions_sqlite(FX("warehouse", "demo_warehouse.sql"), today.isoformat())
    lookups, aliases, known = load_lookups(FX("candidates.json"))
    accounts = salesforce.load_accounts(FX("salesforce", "accounts.json"))
    notes, skipped = slack_notes.load(FX("slack", "field_notes_history.json"), FX("slack", "users_list.json"), accounts)
    signals = (gong.load(FX("gong", "calls_extensive.json"), FX("gong", "calls_transcript.json"))
               + salesforce.load_notes(FX("salesforce", "tasks.json")) + notes)
    return seed, lookups, {**aliases, **(cfg.get("aliases") or {})}, known, accounts, signals, skipped


def gather_live(today: date, prev_start: str, cfg: Dict, post_slack: bool, out: str = "out", info: Dict = None):
    missing = required_env(cfg, post_slack)
    if missing:
        raise MissingConfig("Live mode needs: " + ", ".join(missing) + ". Demo mode needs nothing: drop --mode live.")
    info = info if info is not None else {}
    src = {**DEFAULT_SOURCES, **(cfg.get("sources") or {})}
    seed = warehouse.champions_snowflake() if src["warehouse"] else load_seed(_p(cfg["champion_seed_csv"]))
    lookups = {}
    if src["people"]:
        if cfg.get("people_source", "api") == "clay_csv":
            from champion_tracker.adapters import clay_export
            lookups = clay_export.lookups_from_csv(_p(cfg["clay_csv_path"]), seed, cfg.get("clay_columns"))
        else:
            lookups, info["people_lookups"] = lookup_people(seed, today, out, cfg)
    accounts = salesforce.fetch_accounts() if src["salesforce"] else []
    signals, skipped = [], []
    if src["gong"]:
        nxt = (today + timedelta(days=1)).isoformat()            # Gong's toDateTime is exclusive
        signals += gong.fetch(f"{prev_start}T00:00:00Z", f"{nxt}T00:00:00Z")
    if src["salesforce"]:
        signals += salesforce.fetch_notes(prev_start, today.isoformat())      # never a Task dated in the future
    if src["field_notes"]:
        oldest = datetime.fromisoformat(prev_start).replace(tzinfo=timezone.utc).timestamp()
        notes, skipped = slack_notes.fetch(cfg["field_notes_channel"], f"{oldest:.0f}", accounts,
                                           bot_ids=cfg.get("field_notes_bot_ids") or [])
        signals += notes
    return (seed, lookups, cfg.get("aliases") or {}, set(cfg.get("known_customer_domains") or []), accounts, signals, skipped)


def _write_csv(path: str, rows: List[Dict], cols: List[str]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def _dump(path: str, obj):
    dump_json_atomic(path, obj)


def _load(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ---- the review queue's candidates, most likely first -------------------------------------------------------------
_STOP = {"of", "and", "the", "for", "at", "in", "a", "an", "&", "to", "head", "senior", "sr", "vp", "director", "manager",
         "lead", "chief", "officer", "global", "group"}


def _title_words(t: str) -> set:
    return {w for w in re.findall(r"[a-z]+", (t or "").lower()) if len(w) > 2 and w not in _STOP}


def candidate_score(c: Dict, p: Dict, country: str = "", aliases: Optional[Dict] = None) -> float:
    """How likely a same-name profile is the champion, 0 to 1, for ordering the review queue (never a decision):
    the customer in their work history (or current job) 0.4, their headline naming the customer 0.2, title words
    shared with the title in the evidence up to 0.2, the customer's country in their location 0.2."""
    cust = p.get("Customer", "")
    jobs = [c.get("current_company_name")] + [e.get("company") for e in c.get("experience") or []]
    s = 0.4 if cust and any(same_customer(j, cust, aliases) for j in jobs if j) else 0.0
    if cust and mentions_company(c.get("headline"), cust):
        s += 0.2
    want = _title_words(p.get("Title on page", ""))
    if want:
        titles = " ".join([c.get("current_title") or "", c.get("headline") or ""]
                          + [e.get("title") or "" for e in c.get("experience") or []])
        s += 0.2 * min(1.0, len(want & _title_words(titles)) / min(len(want), 2))
    loc = (c.get("current_location") or c.get("location") or "").lower()
    if country and loc and country.lower() in loc:
        s += 0.2
    return round(s, 2)


def _candidates(r: Dict, seed_by_id: Dict, lookups: Dict, countries: Optional[Dict[str, str]] = None,
                aliases: Optional[Dict] = None) -> List[Dict]:
    """The row's same-name candidates for the review owner, each with its likelihood score, most likely first."""
    p = seed_by_id.get(r["Row id"])
    j = lookups.get((p["Name"], p["Slug"])) if p else None
    country = (countries or {}).get((p or {}).get("Customer", ""), "")
    out = []
    for c in (j or {}).get("candidates") or []:
        if p and seed_name_matches(c, p):          # first, last and the seed's 'Name variants' (maiden names)
            out.append({"name": c.get("full_name", ""), "title": c.get("current_title", ""), "key": candidate_key(c),
                        "company": c.get("current_company_name", ""), "linkedin": c.get("linkedin_url", ""),
                        "score": candidate_score(c, p, country, aliases)})
    out.sort(key=lambda c: -c["score"])            # stable: equal scores keep the provider's order
    return out


# ---- owners: Slack ids by Salesforce owner id ---------------------------------------------------------------------
def owner_names_with_several_ids(accounts: List[Dict]) -> set:
    """Owner names shared by two or more Salesforce owner ids (two reps called John Smith)."""
    ids = defaultdict(set)
    for a in accounts:
        if a.get("owner") and a.get("owner_id"):
            ids[a["owner"]].add(a["owner_id"])
    return {n for n, s in ids.items() if len(s) > 1}


def _owner_ids(accounts: List[Dict], cfg: Dict, out: str, live_slack: bool) -> Dict[str, str]:
    """Salesforce owner id (or owner name) -> Slack id: config first, then users.lookupByEmail on the CRM owner's email
    (saved for the app, keyed by the owner id). A name key is never added for a name that two owner ids share, and an
    owner already configured by id, or by a name only they have, is not looked up."""
    ids = {k: v for k, v in (cfg.get("owner_slack_ids") or {}).items() if v}
    if live_slack:
        shared = owner_names_with_several_ids(accounts)
        learned = {}
        for a in accounts:
            name, oid = a.get("owner") or "", a.get("owner_id") or ""
            key = oid or (name if name not in shared else "")
            if not key or not a.get("owner_email") or key in ids or key in learned:
                continue
            if oid and name and name not in shared and name in ids:     # configured by a name only this owner has
                continue
            try:
                learned[key] = slack_post._call("users.lookupByEmail", {"email": a["owner_email"]}, True)["user"]["id"]
            except (RuntimeError, KeyError):
                pass
        ids.update(learned)
        saved = _load(os.path.join(out, "owner_slack_ids.json"), {})
        saved = {k: v for k, v in (saved if isinstance(saved, dict) else {}).items() if k not in shared}
        _dump(os.path.join(out, "owner_slack_ids.json"), {**saved, **{k: v for k, v in ids.items() if k not in shared}})
    return ids


def card_slack_id(card: Dict, owner_ids: Dict[str, str], cfg: Dict) -> str:
    """Whose click counts on this card: by Salesforce owner id when the card has one (the owner's name only when no id
    entry exists and no other owner has that name: card["owner_name_unique"]); a card routed to the review owner (no
    default_owner set) counts the review owner's id. Mirrors feedback_loop.allowed_ids."""
    review = cfg.get("review_owner") or {}
    sid = card_owner_slack_id(card, owner_ids)
    if not sid and card.get("owner_routed") and (card.get("owner") == review.get("name") or not cfg.get("default_owner")):
        sid = review.get("slack_id") or ""
    return sid


def _move_age(r: Dict) -> Optional[int]:
    a = r.get("Move age (days)")
    if isinstance(a, int):
        return a
    return int(a) if isinstance(a, str) and a.strip().isdigit() else None


def _safe(text) -> str:
    return esc(neutralize(str(text or "")))


def _cell(text) -> str:
    return neutralize(str(text or "")).replace("|", "\\|").replace("\n", " ")


def qa_sample(rows: List[Dict], run_id: str, size: int) -> List[Dict]:
    """The weekly QA sample: `size` rows nobody was sent (Watch, No action, Hold), picked at random but the same for
    the same run id, so a rerun of a run never reshuffles what the review owner was asked about."""
    pool = sorted((r for r in rows if r.get("Decision") in QA_POOL), key=lambda r: str(r.get("Row id")))
    if size <= 0 or not pool:
        return []
    return random.Random(run_id).sample(pool, min(size, len(pool)))


def qa_message(sample: List[Dict], run_id: str) -> Dict:
    """One message to the review owner: each sampled row with "Looks right" / "Wrong". Wrong opens a form (what is
    wrong, and optionally the right decision) and saves a test case."""
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"Weekly QA sample: {len(sample)} rows nobody was sent"}},
              {"type": "context", "elements": [{"type": "mrkdwn", "text": "Picked at random from Watch, No action and Hold. "
                                                "Is each decision right? *Wrong* saves the row as a test case for the next rule change."}]}]
    for r in sample:
        text = (f"*{_safe(r.get('Person'))}* · {_safe(r.get('Level'))} · was {_safe(r.get('Title on page'))} at "
                f"{_safe(r.get('Case study'))} · *{r.get('Decision')}*\n{_safe(r.get('Reason'))}")
        if r.get("Now"):
            text += f"\nNow: {_safe(r['Now'])}"
        if r.get("Evidence (verbatim)"):
            text += f"\nEvidence: {_safe(r['Evidence (verbatim)'])[:400]}"
        val = f"{run_id}:{r['Row id']}"[:2000]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}})
        blocks.append({"type": "actions", "block_id": f"qa:{r['Row id']}", "elements": [
            {"type": "button", "action_id": "qa_ok", "style": "primary", "text": {"type": "plain_text", "text": "Looks right"}, "value": val},
            {"type": "button", "action_id": "qa_wrong", "style": "danger", "text": {"type": "plain_text", "text": "Wrong"}, "value": val}]})
    return {"text": "Weekly QA sample", "blocks": blocks[:50]}


def old_moves_markdown(old: List[Dict], days: int) -> str:
    out = (f"\n## Old moves ({len(old)}): confirmed moves over {days} days old\n\n"
           "Not sent one by one and on no seller's card: a move this old is rarely news. Clear them here when there is "
           "time.\n\n")
    if not old:
        return out + "None this run.\n"
    out += "| # | Person | Champion at | Moved to | Move age (days) | Reason | Row id |\n|---|---|---|---|---|---|---|\n"
    for i, r in enumerate(sorted(old, key=lambda r: -(_move_age(r) or 0)), 1):
        out += (f"| {i} | {_cell(r.get('Person'))} | {_cell(r.get('Case study'))} | {_cell(r.get('New company'))} | "
                f"{_move_age(r) or ''} | {_cell(r.get('Reason'))} | {_cell(r.get('Row id'))} |\n")
    return out


def _pct(x) -> str:
    return "n/a" if x is None else f"{round(100 * x)} %"


def _stats(by_row: Dict[str, int]) -> Dict:
    vals = list(by_row.values())
    return {"rows": len(vals), "median": median(vals) if vals else None, "max": max(vals) if vals else None, "by_row": by_row}


def metrics(events: List[Dict], queue_ids: List[str], detected: List[Dict], today: date, hist: str) -> Dict:
    """Seller clicks and how the queue is doing. Rates are over every click on a seller card (whoever clicked; a
    non-owner's click is still a judgement). Snooze rate counts Snooze and Not now. Queue age: days each row id has
    sat in the review queue (history/queue_first_seen.json; a row that leaves the queue starts again). Time to detect:
    first run that saw a Card or Review row minus the day they joined (latest possible day of a month-only date, so the
    lag is never overstated; history/first_seen.json keeps the first sighting for ever)."""
    clicks = Counter(e.get("action") for e in events)
    seller = sum(n for a, n in clicks.items() if a in SELLER_ACTIONS)

    def rate(*acts):
        return round(sum(clicks.get(a, 0) for a in acts) / seller, 3) if seller else None

    q_path, f_path = os.path.join(hist, "queue_first_seen.json"), os.path.join(hist, "first_seen.json")
    q_old, first = _load(q_path, {}), _load(f_path, {})
    q_new = {rid: min(q_old.get(rid) or today.isoformat(), today.isoformat()) for rid in queue_ids}
    _dump(q_path, q_new)
    detect = {}
    for r in detected:
        rid = r["Row id"]
        first[rid] = min(first.get(rid) or today.isoformat(), today.isoformat())
        rng = date_range(r.get("Joined new company"))
        seen = date.fromisoformat(first[rid])
        if rng and rng[0] <= seen:
            detect[rid] = (seen - min(rng[1], seen)).days
    _dump(f_path, first)
    return {"clicks_by_action": dict(sorted(clicks.items())), "seller_clicks": seller,
            "acceptance_rate": rate("do_play", "accept"), "wrong_person_rate": rate("wrong_person"),
            "wrong_play_rate": rate("wrong_play"), "wrong_account_rate": rate("wrong_account"),
            "snooze_rate": rate("snooze", "not_now"),
            "queue_age_days": _stats({rid: (today - date.fromisoformat(d)).days for rid, d in q_new.items()}),
            "time_to_detect_days": _stats(detect)}


def metrics_line(m: Dict) -> str:
    q, t = m["queue_age_days"], m["time_to_detect_days"]
    return (f"Metrics: {m['seller_clicks']} seller clicks · accepted {_pct(m['acceptance_rate'])} · wrong person "
            f"{_pct(m['wrong_person_rate'])} · wrong play {_pct(m['wrong_play_rate'])} · wrong account "
            f"{_pct(m['wrong_account_rate'])} · snoozed {_pct(m['snooze_rate'])} · queue age median "
            f"{q['median'] if q['rows'] else 'n/a'} days (max {q['max'] if q['rows'] else 'n/a'}) · time to detect median "
            f"{t['median'] if t['rows'] else 'n/a'} days")


def _add_context(block: Dict, text: str):
    """Add a line to a context block (up to 10 elements), so the ops message keeps 3 blocks on top and splits at rows."""
    if block.get("type") == "context" and len(block.get("elements", [])) < 10:
        block["elements"].append({"type": "mrkdwn", "text": text[:2900]})


def icp_from_config(cfg: Dict) -> Optional[Dict]:
    """config.json -> icp with its notes and empty fields dropped; None when nothing is set (no ICP check at all)."""
    icp = {k: v for k, v in (cfg.get("icp") or {}).items() if not str(k).startswith("_") and v not in (None, "", [], {})}
    return icp or None


def _accepts(fn: Callable, name: str) -> bool:
    """Does fn take this keyword? (The surface's newer parameters are passed only where they exist.)"""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


@contextmanager
def run_lock(out: str):
    """Hold out/.run.lock (exclusive, non-blocking) for one run. Raises RunLocked when another run holds it."""
    os.makedirs(out, exist_ok=True)
    if fcntl is None:
        yield
        return
    path = os.path.join(out, ".run.lock")
    f = open(path, "a+")
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.seek(0)
            held = f.read().strip()
            raise RunLocked(f"another pipeline run is using {out} (it holds {path}"
                            + (f"; {held}" if held else "") + "). Wait for it to finish; nothing was run or posted.")
        f.seek(0)
        f.truncate()
        f.write(f"pid {os.getpid()} since {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
        f.flush()
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def run(mode: str = "demo", today: date = None, out: str = "out", post_slack: bool = False, channel: str = "",
        cfg: Dict = None) -> Dict:
    """One whole run, under out/.run.lock (see the module docstring)."""
    with run_lock(out):
        return _run(mode, today, out, post_slack, channel, cfg)


def _run(mode: str, today: Optional[date], out: str, post_slack: bool, channel: str, cfg: Optional[Dict]) -> Dict:
    cfg = cfg if cfg is not None else load_config(out)
    today = today or (DEMO_TODAY if mode == "demo" else date.today())
    period, previous = periods(today, int(cfg.get("period_days", 7)))
    if channel:
        cfg = dict(cfg, channel=channel)
    info: Dict = {}
    src = {**DEFAULT_SOURCES, **(cfg.get("sources") or {})}
    gathered = gather_demo(today, cfg, info) if mode == "demo" else gather_live(today, previous[0], cfg, post_slack, out, info)
    seed, lookups, aliases, known, accounts, signals, skipped = gathered
    known = set(known) | {domain(a["domain"]) for a in accounts if a["type"] == "customer" and a.get("domain")}
    hist = os.path.join(out, "history")
    os.makedirs(hist, exist_ok=True)

    log_path = _p(cfg.get("feedback_log") or "feedback/feedback.jsonl")
    also = [_p(cfg.get("regression_file") or "feedback/regression_cases.jsonl"),
            os.path.join(os.path.dirname(log_path) or ".", "crm_write_back.jsonl")]
    purged = state.purge(log_path, _cfg(cfg, "retention_days"), also=also)   # real date: a pinned demo day never wipes notes
    bad_lines: List[int] = []
    events = state.read_log(log_path, bad_lines)
    fetch, fetch_stats = None, {}
    if mode == "live" and src["people"] and cfg.get("people_source", "api") == "api" and os.environ.get("PEOPLE_PROFILE_URL"):
        from champion_tracker.adapters import people_data
        fetch = cached_profile_fetch(people_data.fetch_profile, out, today, cfg, fetch_stats)
    lookups, ov, pasted = apply_pasted_profiles(seed, lookups, state.overrides(events), fetch)
    rows = run_rules(seed, lookups, today, aliases, known, overrides=ov)
    parked = state.parked(events)
    for r in rows:
        if r["Row id"] in parked:
            r["unresolvable"] = True

    from gtm_briefs.lexicon import LEX
    from gtm_briefs import observer
    observations, obs_stats = {}, {"enabled": False}
    for sid, th in observer.tracker_themes(signals, LEX.get("gong_trackers") or {}).items():
        observations.setdefault(sid, []).extend(th)
    if (cfg.get("observer") or {}).get("enabled"):
        prompt = getattr(observer, "PROMPT_VERSION", "")
        try:
            missing = observer.required_env()
        except ValueError as e:
            missing = [str(e)]
        if missing:            # live mode refuses earlier (required_env); demo runs on without it and says so
            obs_stats = {"enabled": True, "skipped": "missing " + ", ".join(missing), "prompt_version": prompt}
        else:
            try:
                got = observer.observe_with_stats(signals)
            except Exception as e:  # noqa: BLE001  (the observer is optional: the run goes on with the lexicon alone)
                got = None
                obs_stats = {"enabled": True, "failed": f"{type(e).__name__}: {e}"[:300], "prompt_version": prompt}
            if got is not None:
                obs_stats = {"enabled": True, **got["stats"], "prompt_version": prompt}
                for sid, th in got["observations"].items():
                    observations.setdefault(sid, []).extend(t for t in th if t not in observations.get(sid, []))
    findings = extract(signals, observations)
    brief = build_brief(findings, period, previous)
    brief_md = to_markdown(brief)
    in_period = [f for f in findings if period[0] <= f["date"] <= period[1]]
    review = cfg.get("review_owner") or {}
    owner = review.get("name") or "the review owner"
    default_owner = cfg.get("default_owner") or review.get("name") or ""
    old_days = _cfg(cfg, "old_move_days")
    unmatched, blocked_notes, carded_old = [], [], []
    extra = {k: v for k, v in (("icp", icp_from_config(cfg)), ("old_move_days", old_days), ("old_moves", carded_old))
             if _accepts(build_cards, k)}
    all_cards = build_cards(accounts, rows, in_period, today, unmatched=unmatched, blocked=cfg.get("blocked"),
                            notes=blocked_notes, default_owner=default_owner, **extra)
    shared_names = owner_names_with_several_ids(accounts)
    for c in all_cards:                 # the Salesforce owner id, so owner_slack_ids can be keyed by it
        acc, parent = c["built_from"]["account"], c["built_from"].get("parent") or {}
        c["owner_id"] = "" if c.get("owner_routed") else (acc.get("owner_id") or (parent.get("owner_id", "") if not acc.get("owner") else ""))
        c["owner_name_unique"] = c.get("owner") not in shared_names
    cards, held_back = state.apply(all_cards, events, today)

    seed_by_id = {row_id(s): s for s in seed}
    in_queue = [r for r in rows if r["Decision"] in ("Hold", "Review")]
    old_moves = [r for r in in_queue if r["Decision"] == "Review" and (_move_age(r) or 0) > old_days]
    old_ids = {r["Row id"] for r in old_moves}
    for r in carded_old:                # moves build_cards kept off the sellers' cards for their age
        if r.get("Row id") not in old_ids:
            old_moves.append(r)
            old_ids.add(r.get("Row id"))
    countries = {a["account"]: a.get("country") or "" for a in accounts if a.get("type") == "customer"}
    queue = [dict(r, **{"Queue owner": owner, "candidates": _candidates(r, seed_by_id, lookups, countries, aliases)})
             for r in in_queue if r["Row id"] not in old_ids]

    owner_ids = _owner_ids(accounts, cfg, out, post_slack)
    sid_by_card = {c["id"]: card_slack_id(c, owner_ids, cfg) for c in all_cards}
    no_slack = [c["owner"] for c in all_cards if c.get("owner") and not sid_by_card[c["id"]]]

    prev_summary = None
    daily = sorted(p for p in os.listdir(hist) if DAILY.match(p) and p[:10] < today.isoformat())
    if daily:
        prev_summary = _load(os.path.join(hist, daily[-1]), None)
    summ = monitor.summary(rows, signals, findings, cards, skipped, lookups=lookups, unmatched=unmatched,
                           notes=blocked_notes, observer=obs_stats, owners_without_slack=no_slack,
                           corrupt_log_lines=len(bad_lines))
    summ.update({"mode": mode, "today": today.isoformat(), "period": period, "previous": previous,
                 "held_back": len(held_back), "alerts": monitor.alerts(summ, prev_summary),
                 "old_moves": len(old_moves), "profiles_pasted": len(pasted), "feedback_notes_purged": purged,
                 "queue_parked": sum(1 for r in queue if r.get("unresolvable"))})
    if info.get("people_lookups"):
        summ["people_lookups"] = info["people_lookups"]
    if fetch_stats:
        summ["pasted_profile_fetches"] = fetch_stats
    summ["metrics"] = metrics(events, [r["Row id"] for r in queue if not r.get("unresolvable")],
                              [r for r in rows if r["Decision"] in ("Card", "Review")], today, hist)

    # A run id on every button: a click on an older digest is refused instead of acting on stale inputs.
    run_id = today.isoformat() + "-" + hashlib.sha1(json.dumps([(c["id"], c["play"], c["why_now"]) for c in cards],
                                                              sort_keys=True).encode()).hexdigest()[:8]
    summ["run_id"] = run_id
    for c in cards:
        c["button_value"] = f"{run_id}:{c['id']}"
    title = f"GTM brief and pipeline · {period[0]} to {period[1]}"
    footer = f"Champion review queue ({len(queue)}) went to {owner}." + \
        (f" {len(old_moves)} old move(s) are in the review canvas." if old_moves else "")
    digest = digest_blocks(cards, period, footer=[footer])
    thread = card_messages(cards, start=6, per_message=7)       # every account past the top 6 keeps its buttons
    canvas = canvas_markdown(brief_md, cards, held_back)
    ops = ops_blocks(queue, run_id, owner, summ["alerts"])
    if len(ops) > 2:
        if old_moves:
            _add_context(ops[1], f"Old moves (over {old_days} days): {len(old_moves)}, listed in the review canvas, not one by one.")
        _add_context(ops[2], metrics_line(summ["metrics"]))
    sample = qa_sample(rows, run_id, _cfg(cfg, "qa_sample_size"))
    ops_md = (ops_canvas_markdown(queue, owner, summ["alerts"]) + old_moves_markdown(old_moves, old_days)
              + "\n## Metrics\n" + metrics_line(summ["metrics"]).replace("Metrics: ", "", 1) + "\n"
              + (f"\n## Weekly QA sample ({len(sample)})\n" + "".join(
                  f"- {_cell(r['Person'])} ({_cell(r['Case study'])}): {r['Decision']} · {_cell(r['Reason'])}\n" for r in sample)
                 if sample else ""))
    owner_digests = {}
    if cfg.get("digest_to") in ("owners", "both"):
        mine = defaultdict(list)
        for c in cards:
            if sid_by_card.get(c["id"]):
                mine[sid_by_card[c["id"]]].append(c)
        dm = {"thread": False} if _accepts(digest_blocks, "thread") else {}   # no thread in a DM: say where the rest are
        owner_digests = {uid: digest_blocks(cs, period, **dm) for uid, cs in mine.items()}

    champ_cards = [build_card(r) for r in rows if r["Decision"] in ("Card", "Review")]
    for card in champ_cards:
        for b in card["blocks"]:
            for e in b.get("elements", []) if b["type"] == "actions" else []:
                e["value"] = f"{run_id}:{e['value']}"
    _write_csv(os.path.join(out, "decisions.csv"), [dict(r, **{"Champion at": r.get("Case study", "")}) for r in rows],
               ["Row id"] + [("Champion at" if c == "Case study" else c) for c in COLUMNS])
    _dump(os.path.join(out, "champion_cards.json"), champ_cards)
    _dump(os.path.join(out, "findings.json"), findings)
    _dump(os.path.join(out, "brief.json"), brief)
    write_atomic(os.path.join(out, "brief.md"), brief_md)
    _dump(os.path.join(out, "surface.json"), cards)
    lr = [dict(r, **{"Owner Slack id": sid_by_card.get(r["Card id"], "")}) for r in list_rows(cards)]
    _write_csv(os.path.join(out, "next_steps.csv"), lr, list(lr[0].keys()) if lr else ["Account"])
    cr = clay_rows(cards)
    _write_csv(os.path.join(out, "clay_surface.csv"), cr, list(cr[0].keys()) if cr else ["Account"])
    _write_csv(os.path.join(out, "review_queue.csv"),
               [dict(r, **{"Champion at": r["Case study"], "Parked": "Yes" if r.get("unresolvable") else ""}) for r in queue],
               ["Queue owner", "Decision", "Person", "Champion at", "Reason", "Story", "Row id", "Parked"])
    _dump(os.path.join(out, "slack_digest.json"), digest)
    write_atomic(os.path.join(out, "canvas.md"), canvas)
    write_atomic(os.path.join(out, "ops.md"), ops_md)
    _dump(os.path.join(out, "skipped_notes.json"), skipped)

    # Replay inputs: what each card and queue row was built from, so a correction becomes a runnable test case.
    rdir = os.path.join(out, "replay", run_id)
    os.makedirs(rdir, exist_ok=True)
    sample_ids = {r["Row id"] for r in sample}
    for r in rows:
        if r["Decision"] in ("Card", "Review", "Hold") or r["Row id"] in sample_ids:
            p = seed_by_id.get(r["Row id"])
            if p:
                _dump(os.path.join(rdir, f"champion_{r['Row id']}.json"), {"kind": "champion", "today": today.isoformat(),
                      "seed": p, "lookup": lookups.get((p["Name"], p["Slug"])), "aliases": aliases, "known": sorted(known),
                      "override": ov.get(r["Row id"]), "decision": r["Decision"], "row": r})
    for c in cards:
        used = c["built_from"]
        _dump(os.path.join(rdir, f"card_{c['id']}.json"), {"kind": "card", "today": today.isoformat(), "card": c,
              "account": used["account"], "parent": used.get("parent"), "owner_id": c.get("owner_id", ""),
              "champion_rows": [r for r in rows if r.get("Row id") in used["row_ids"]],
              "findings": [f for f in in_period if f["id"] in used["finding_ids"]]})

    latest_path = os.path.join(out, "replay", "LATEST")
    latest_before = open(latest_path, encoding="utf-8").read().strip() if os.path.exists(latest_path) else ""
    review_id = review.get("slack_id") or ""
    failure = None
    try:
        summ["slack"] = slack_post.deliver(
            cfg.get("channel") or "C_DEMO", digest, title, canvas, dry_run=not post_slack, out_dir=out, next_steps=lr,
            ops=ops, ops_to=review_id or ("U_REVIEW_OWNER" if not post_slack else ""), owner_digests=owner_digests,
            thread_messages=thread, allow_shared=bool(cfg.get("allow_shared_channel")),
            review_messages=[qa_message(sample, run_id)] if sample else [], ops_canvas_md=ops_md)
    except RuntimeError as e:            # SharedChannelError, a bad state file, or the digest post failed: nothing new is live
        failure = e
        summ["slack"] = {"dry_run": not post_slack, "errors": [str(e)], "posted": False}
    posted = bool(post_slack and failure is None and summ["slack"].get("message_ts"))
    summ["slack"]["posted"] = posted
    if summ["slack"].get("errors") and failure is None:
        summ["alerts"].append("Slack: " + "; ".join(summ["slack"]["errors"])[:600])
    update_latest = posted or (mode == "demo" and not post_slack and failure is None)
    if update_latest:
        write_atomic(latest_path, run_id)
    summ["replay"] = {"run_id": run_id, "latest_updated": update_latest,
                      "latest": run_id if update_latest else latest_before}
    # nobody saw these cards unless the digest reached Slack (a demo run without --post-slack stands in for delivery)
    dry = not posted and (post_slack or mode == "live")
    _dump(os.path.join(hist, f"cards-{run_id}.json"), {      # never pruned: scripts/backtest.py reads every run
        "run_id": run_id, "run_date": today.isoformat(), "mode": mode, "dry_run": dry, "posted": posted,
        "cards": [{"account": c["account"], "crm_id": c.get("crm_id", ""), "play": c["play"], "tier": c["tier"],
                   "status": c["status"], "run_date": today.isoformat()} for c in cards]})
    keep = _cfg(cfg, "keep_runs")      # corrections carry their own copy of the inputs, so old runs can go
    live = {run_id, summ["replay"]["latest"]}
    runs = sorted(d for d in os.listdir(os.path.join(out, "replay")) if os.path.isdir(os.path.join(out, "replay", d)))
    for d in runs[:-keep] if keep > 0 else []:
        if d not in live:              # never the run whose buttons are in Slack
            shutil.rmtree(os.path.join(out, "replay", d), ignore_errors=True)

    if cfg.get("write_to_warehouse") and mode == "live" and failure is None:
        _mod, con = warehouse.connect()
        try:
            summ["warehouse_rows"] = warehouse.write_run(con, run_id, today.isoformat(), rows, findings, cards, events, summ, "snowflake")
        finally:
            con.close()
    _dump(os.path.join(out, "summary.json"), summ)
    _dump(os.path.join(hist, f"{today.isoformat()}.json"), summ)
    if failure is not None:
        raise failure
    return summ


DEMO_POST_REFUSAL = ("Not run: --post-slack in demo mode would post invented accounts with working buttons. Use "
                     "--mode live, or add --allow-demo-post with --channel <a test channel> to post the demo on purpose.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["demo", "live"], default="demo")
    ap.add_argument("--today", default="", help="YYYY-MM-DD (default: today; the demo pins 2026-10-18)")
    ap.add_argument("--out", default="out")
    ap.add_argument("--post-slack", action="store_true", help="post to Slack (otherwise writes the requests to out/)")
    ap.add_argument("--allow-demo-post", action="store_true",
                    help="with --post-slack in demo mode: post the invented demo on purpose (a test channel only)")
    ap.add_argument("--channel", default="", help="Slack channel id (or set channel in config.json)")
    ap.add_argument("--check", action="store_true", help="live preflight: test every switched-on source, write nothing")
    ap.add_argument("--check-regressions", action="store_true",
                    help="replay saved corrections against today's rules; exit 1 if a case marked fixed fails again")
    ap.add_argument("--regression-file", default="", help="default: $REGRESSION_FILE, else config.json -> regression_file")
    a = ap.parse_args(argv)
    if a.check:
        from .check import preflight
        report = preflight(load_config(a.out))
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    if a.check_regressions:
        from .feedback_loop import broken_fixes, check_regressions
        path = a.regression_file or os.environ.get("REGRESSION_FILE") or \
            _p(load_config(a.out).get("regression_file") or "feedback/regression_cases.jsonl")
        cases = check_regressions(path)
        for c in cases:
            print(f"{c.get('status', 'open'):5} {c['action']:13} {c.get('person') or c.get('case_study')}: now {c['result']['now']}"
                  f" -> {'FIXED (mark status fixed)' if c['result']['passes'] and c.get('status') != 'fixed' else 'ok' if c['result']['passes'] else 'still wrong'}")
        broken = broken_fixes(cases)
        print(f"{len(cases)} saved corrections" + (f"; {len(broken)} marked fixed now FAIL again" if broken else ""))
        return 1 if broken else 0
    if a.post_slack and a.mode == "demo" and not a.allow_demo_post:
        print(DEMO_POST_REFUSAL)
        return 2
    try:
        s = run(a.mode, date.fromisoformat(a.today) if a.today else None, a.out, a.post_slack, a.channel)
    except MissingConfig as e:
        print(f"Not run: {e}")
        return 2
    except RunLocked as e:
        print(f"Not run: {e}")
        return EXIT_LOCKED
    print(json.dumps({k: s[k] for k in ("mode", "today", "run_id", "decisions", "cards", "cards_ready", "held_back",
                                        "findings_by_theme", "alerts", "slack")}, indent=2))
    if not s["replay"]["latest_updated"]:
        print(f"Dry run: wrote replay/{s['run_id']}; replay/LATEST left at {s['replay']['latest'] or '(none yet)'}, "
              "so the buttons on the digest already in Slack keep working.")
    return 0

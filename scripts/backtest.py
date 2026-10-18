"""Does a card lead to pipeline? Join every past card to opportunities created afterwards, and report the hit rate
per play and per tier, next to accounts that got no card.

  PYTHONPATH=src python3 scripts/backtest.py --out out --opps opps.csv --days 60 [--accounts accounts.csv]

Cards come from out/history/cards-<run id>.json (written every run, never pruned, so the backtest can go back a
year), with out/replay/<run>/card_*.json as a fallback for runs older than that file. Runs whose digest never reached
Slack are skipped (dry_run=true: live dry runs, and posts that failed before the digest went out): nobody saw those
cards (--include-dry-runs to count them). The same card on the same day counts once.

opps.csv columns: account, created_date (YYYY-MM-DD); optional account_id (the Salesforce Account Id: the join uses
it when present, because two records can share a name), type and amount - e.g. a Salesforce report export.
--exclude-types (default "Renewal") drops opportunities of those types: renewals are created automatically ahead of
every renewal date, so without this a renewal-save card (P4) looks perfect.
Use it to set the weights in src/pg_surface/surface.py from your own outcomes instead of judgment.
"""
import argparse
import csv
import glob
import json
import os
from collections import defaultdict
from datetime import date, timedelta


def _id(x) -> str:
    """Salesforce 15- and 18-character forms of one Id compare on the first 15."""
    s = str(x or "").strip()
    return s[:15] if len(s) == 18 and s.isalnum() else s


def load_cards(out: str, include_dry_runs: bool = False):
    cards, seen_runs, seen = [], set(), set()

    def add(c, run_date):
        key = (_id(c.get("crm_id")) or (c.get("account") or "").strip().lower(), c.get("play"), run_date)
        if key in seen:
            return
        seen.add(key)
        cards.append({"account": c["account"], "crm_id": c.get("crm_id", ""), "play": c["play"], "tier": c.get("tier", ""),
                      "status": c.get("status", ""), "run_date": run_date})

    for p in sorted(glob.glob(os.path.join(out, "history", "cards-*.json"))):
        with open(p, encoding="utf-8") as f:
            r = json.load(f)
        seen_runs.add(r.get("run_id") or os.path.basename(p)[len("cards-"):-5])
        if r.get("dry_run") and not include_dry_runs:
            continue
        for c in r.get("cards") or []:
            add(c, c.get("run_date") or r["run_date"])
    for p in sorted(glob.glob(os.path.join(out, "replay", "*", "card_*.json"))):
        if os.path.basename(os.path.dirname(p)) in seen_runs:
            continue
        with open(p, encoding="utf-8") as f:
            r = json.load(f)
        add(r["card"], r["today"])
    return cards


def backtest(cards, opps, days: int, all_accounts=(), exclude_types=("Renewal",)):
    """cards: [{account, crm_id?, play, tier, run_date}]. opps: [{account, created_date, account_id?, type?}].
    all_accounts: names, or {account, account_id} rows, for the no-card baseline."""
    drop = {t.strip().lower() for t in exclude_types or () if t.strip()}
    kept = [o for o in opps if (o.get("type") or "").strip().lower() not in drop]
    by_id, by_name = defaultdict(list), defaultdict(list)
    for o in kept:
        d = date.fromisoformat(o["created_date"][:10])
        if _id(o.get("account_id")):
            by_id[_id(o["account_id"])].append(d)
        by_name[(o.get("account") or "").strip().lower()].append(d)
    use_ids = any(_id(o.get("account_id")) for o in kept)

    def hit(name, crm_id, start):
        s = date.fromisoformat(start)
        found = by_id.get(_id(crm_id), []) if use_ids and _id(crm_id) else by_name.get((name or "").strip().lower(), [])
        return any(s < d <= s + timedelta(days=days) for d in found)

    groups = defaultdict(lambda: [0, 0])
    for c in cards:
        for key in (f"play {c['play']}", f"tier {c['tier']}", "all cards"):
            groups[key][0] += 1
            groups[key][1] += hit(c["account"], c.get("crm_id"), c["run_date"])
    carded = {(_id(c.get("crm_id")) or c["account"].strip().lower(), c["run_date"]) for c in cards}
    runs = sorted({c["run_date"] for c in cards})
    for acc in all_accounts:
        name, aid = (acc, "") if isinstance(acc, str) else (acc.get("account", ""), acc.get("account_id", ""))
        for run in runs:
            if (_id(aid) or name.strip().lower(), run) not in carded and (name.strip().lower(), run) not in carded:
                groups["no card"][0] += 1
                groups["no card"][1] += hit(name, aid, run)
    out = {k: {"cards": n, "with_new_opportunity": h, "rate": round(h / n, 3) if n else 0.0} for k, (n, h) in sorted(groups.items())}
    out["_opportunities"] = {"used": len(kept), "excluded_by_type": len(opps) - len(kept),
                             "excluded_types": sorted(drop), "joined_on": "account_id" if use_ids else "account name"}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out")
    ap.add_argument("--opps", required=True)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--accounts", default="", help="optional CSV with an 'account' (and optional 'account_id') column: "
                                                   "every account, for the no-card baseline")
    ap.add_argument("--exclude-types", default="Renewal", help="comma list of opportunity types to drop ('' keeps all)")
    ap.add_argument("--include-dry-runs", action="store_true", help="also count cards from live dry runs")
    a = ap.parse_args()
    with open(a.opps, encoding="utf-8-sig") as f:
        opps = list(csv.DictReader(f))
    accs = []
    if a.accounts:
        with open(a.accounts, encoding="utf-8-sig") as f:
            accs = [{"account": r.get("account", ""), "account_id": r.get("account_id", "")} for r in csv.DictReader(f)]
    print(json.dumps(backtest(load_cards(a.out, a.include_dry_runs), opps, a.days, accs, a.exclude_types.split(",")), indent=2))

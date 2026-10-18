"""Run the tracker on a seed + candidates file and write decisions, seller cards and a summary.

  python3 -m champion_tracker --seed fixtures/champions.csv --lookups fixtures/candidates.json --today 2026-10-18 --out out
"""
import argparse
import csv
import json
import os
from collections import Counter
from datetime import date

from .adapters.fixtures import load_lookups, load_seed
from .rules import run
from .slack_card import build_card

COLUMNS = ["Decision", "Person", "Case study", "Level", "Now", "Same person?", "Tied by", "Candidates (same name)",
           "Moved?", "Left on", "Joined new company", "Move age (days)", "New account", "Play", "Reason",
           "Evidence (verbatim)", "Title on page", "Action tag", "LinkedIn", "Location", "Story", "Rule version"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", required=True)
    ap.add_argument("--lookups", required=True)
    ap.add_argument("--today", default=date.today().isoformat(), help="YYYY-MM-DD (pin it for reproducible runs)")
    ap.add_argument("--out", default="out")
    a = ap.parse_args(argv)

    seed = load_seed(a.seed)
    lookups, aliases, known = load_lookups(a.lookups)
    rows = run(seed, lookups, date.fromisoformat(a.today), aliases, known)

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "decisions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in COLUMNS})
    cards = [build_card(r) for r in rows if r["Decision"] in ("Card", "Review")]
    with open(os.path.join(a.out, "cards.json"), "w", encoding="utf-8") as f:
        json.dump(cards, f, indent=2, ensure_ascii=False)
    counts = Counter(r["Decision"] for r in rows)
    summary = {"today": a.today, "people": len(rows), "decisions": dict(counts), "cards": len(cards)}
    with open(os.path.join(a.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

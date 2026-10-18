"""Print the Clay formula for 'Play (Clay rule)', generated from contracts/plays.json, so Clay and the code use
the same ordered rules. Column names are the ones in out/clay_surface.csv.
  PYTHONPATH=src python3 scripts/clay_formula.py            print it
  PYTHONPATH=src python3 scripts/clay_formula.py --write    also put it in clay/surface_formulas.md (after a plays change)"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from pg_surface.plays import RULES  # noqa: E402

DOC = os.path.join(ROOT, "clay", "surface_formulas.md")
FACT = {"customer": '{{Type}} == "customer"', "prospect": '{{Type}} != "customer"', "near_renewal": '{{Near renewal}} == "Yes"',
        "pricing": "Number({{Pricing}}) > 0", "competitor_risk": "Number({{Competitor risk}}) > 0",
        "champion_in": "Number({{Champion in}}) > 0", "champion_out": "Number({{Champion out}}) > 0",
        "expansion": "Number({{Expansion}}) > 0", "timing": "Number({{Timing}}) > 0", "open_deal": '{{Open deal}} == "Yes"'}


def formula() -> str:
    parts = []
    for r in RULES:
        cond = [FACT[f] for f in r.get("all", [])]
        if r.get("any"):
            cond.append("(" + " || ".join(FACT[f] for f in r["any"]) + ")")
        parts.append(f'{" && ".join(cond)} ? "{r["play"]}"')
    return " : ".join(parts) + ' : "no card"'


def write_doc(path: str = DOC) -> None:
    """Replace the code block under 'Play (Clay rule)' with the current formula; the rest of the doc is untouched."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    new, n = re.subn(r"(\*\*Play \(Clay rule\)\*\*[^\n]*\n```js\n)[^\n]*(\n```)", lambda m: m.group(1) + formula() + m.group(2), text, count=1)
    if n != 1:
        raise SystemExit(f"no 'Play (Clay rule)' block found in {path}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)


if __name__ == "__main__":
    print(formula())
    if "--write" in sys.argv[1:]:
        write_doc()

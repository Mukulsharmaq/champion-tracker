"""Approved plays and the ordered rules that pick one, loaded from contracts/plays.json (the contract is the code)."""
import json
import os
from typing import Dict, List, Optional

HOME = os.environ.get("GTM_HOME") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATH = os.path.join(HOME, "contracts", "plays.json")   # GTM_HOME: the folder holding contracts/ (needed for a wheel install)
with open(PATH, encoding="utf-8") as _f:
    CONTRACT = json.load(_f)
PLAYS: Dict[str, Dict] = CONTRACT["plays"]
RULES = CONTRACT["rules"]
VERSION = CONTRACT["version"]


def problems(contract: Dict) -> List[str]:
    """What is wrong with a plays contract. Checked at import so a bad edit fails the run, not a seller's card."""
    out = []
    for r in contract["rules"]:
        if r["play"] not in contract["plays"]:
            out.append(f"rule names unknown play {r['play']}")
        out += [f"rule {r['play']} uses undefined fact '{f}'" for f in r.get("all", []) + r.get("any", []) if f not in contract["facts"]]
    out += [f"play {k} has no '{field}'" for k, p in contract["plays"].items() for field in ("name", "do", "lead_with") if field not in p]
    return out


if problems(CONTRACT):
    raise ValueError("contracts/plays.json: " + "; ".join(problems(CONTRACT)))


def pick_play(facts: Dict[str, bool]) -> Optional[str]:
    """First rule whose 'all' facts are true and (if given) at least one 'any' fact is true. None = no card."""
    for r in RULES:
        if all(facts.get(f) for f in r.get("all", [])) and (not r.get("any") or any(facts.get(f) for f in r["any"])):
            return r["play"]
    return None

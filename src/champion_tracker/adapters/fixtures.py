"""Load a seed CSV and a candidates JSON (the public-data / fixture mode). No network, no keys."""
import csv
import json
from typing import Dict, List, Set, Tuple

REQUIRED_SEED = ["Slug", "Name", "First", "Last", "Customer", "Action tag"]


def load_seed(path: str) -> List[dict]:
    with open(path, encoding="utf-8-sig") as f:          # utf-8-sig: spreadsheets often add a BOM
        rows = list(csv.DictReader(f))
    missing = [h for h in REQUIRED_SEED if rows and h not in rows[0]]
    if missing:
        raise ValueError(f"seed file is missing columns: {missing}")
    return rows


def load_lookups(path: str) -> Tuple[Dict[Tuple[str, str], dict], Dict[str, List[str]], Set[str]]:
    """JSON {"lookups": [{"name", "slug", "customer", "ok", "candidates": [...]}], "aliases": {...},
    "known_customer_domains": [...]} -> (lookups by (name, slug), aliases, known customer domains)."""
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    lookups = {(r["name"], r["slug"]): r for r in data["lookups"]}
    return lookups, data.get("aliases", {}), set(data.get("known_customer_domains", []))

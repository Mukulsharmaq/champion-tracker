"""People-data adapter (live mode). Builds the candidates list the rules expect: EVERY same-name profile with its
work history, never a single best guess. Provider-neutral; the example client targets a REST people search with
experience history (GetLeads-style). Swap the client for Clay, ZoomInfo or your warehouse — the rules don't change.

Two-step lookup (from the demo run):
  1. match by name + company (fast, batches of 100) -> people still at the customer resolve here
  2. for misses only: name search 50 deep, with work history (common names need depth; 10 deep wrongly holds them)
"""
import json
import os
import time
import urllib.error
import urllib.request
from typing import Dict, List

SEARCH_URL = os.environ.get("PEOPLE_SEARCH_URL", "")          # e.g. https://<provider>/api/v1/contacts/search
MATCH_URL = os.environ.get("PEOPLE_MATCH_URL", "")            # e.g. https://<provider>/api/v1/enrich/from-person
TOKEN = os.environ.get("PEOPLE_DATA_TOKEN", "")               # never commit keys

COLUMNS = ["full_name", "first_name", "last_name", "current_title", "current_employer", "current_employer_website",
           "linkedin_url", "role_start_date", "latest_company_change_at", "contact_location", "linkedin_headline",
           "last_refresh_at", "contact_updated_at", "employee_count_range", "main_industry", "experience_json"]


# Provider field names -> the keys rules.py reads (providers differ; normalise once, here).
KEY_MAP = {"current_employer": "current_company_name", "current_employer_website": "current_company_website",
           "linkedin_headline": "headline", "contact_location": "current_location", "contact_updated_at": "updated_at"}


class LookupError_(RuntimeError):
    """Provider failure. Callers record the person as ok=False so the rules Hold with 'Lookup failed; retry'."""


def _post(url: str, body: dict, tries: int = 4) -> dict:
    if not (url and TOKEN):
        raise RuntimeError("Set PEOPLE_SEARCH_URL / PEOPLE_MATCH_URL and PEOPLE_DATA_TOKEN to use live mode.")
    data = json.dumps(body).encode()
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data, {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=90))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise LookupError_(f"auth failed ({e.code})") from e
            last = e
        except Exception as e:          # timeouts, 5xx, bad JSON: retry with backoff
            last = e
        time.sleep(4 * (i + 1))
    raise LookupError_(f"lookup failed after {tries} tries: {last}")


def normalise(c: dict) -> dict:
    for src, dst in KEY_MAP.items():
        if src in c and dst not in c:
            c[dst] = c.pop(src)
    return c


def _slim_experience(raw) -> List[Dict]:
    jobs = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
    return [{"company": (j.get("company") or {}).get("name"), "website": (j.get("company") or {}).get("website"),
             "title": j.get("title"), "job_type": j.get("job_type"), "start": j.get("start_date"),
             "end": j.get("end_date"), "is_current": j.get("is_current")} for j in jobs]


def search_candidates(first: str, last: str, depth: int = 50) -> List[Dict]:
    r = _post(SEARCH_URL, {"first_name": first, "last_name": last, "limit": depth, "columns": COLUMNS})
    out = []
    for c in r.get("contacts", []) or []:
        c["experience"] = _slim_experience(c.pop("experience_json", None))
        out.append(normalise(c))
    return out


def lookup_record(person: dict, depth: int = 50) -> dict:
    """One person -> the lookup record rules.run expects. Provider failures become ok=False (Hold: retry)."""
    try:
        cands = search_candidates(person["First"], person["Last"], depth)
        return {"name": person["Name"], "slug": person["Slug"], "customer": person["Customer"], "ok": True, "candidates": cands}
    except LookupError_ as e:
        return {"name": person["Name"], "slug": person["Slug"], "customer": person["Customer"], "ok": False,
                "error": str(e), "candidates": []}

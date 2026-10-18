"""Clay as the people-data source. In Clay: a table of your champions with an "Enrich person" (or job-change)
column that returns each person's current job and work history. Export it as CSV (or send rows with an HTTP API
column to a file this reads), then point config.json at it:

  "sources": {"people": true}, "people_source": "clay_csv", "clay_csv_path": "exports/champions_clay.csv",
  "clay_columns": { ...override any of the defaults below... }

Every row becomes one candidate profile for that champion (match on the seed's Name + Customer, or on Row id when
the export carries it). Work history can come as a JSON column (list of {company, website, title, start, end}).
Clay's column names depend on the enrichment you pick, so they are mapped here, not assumed.
"""
import csv
import json
from typing import Dict, List, Optional

from ..matching import normalize_size_band

DEFAULT_COLUMNS = {
    "row_id": "Row id", "name": "Full Name", "customer": "Customer",
    "current_company": "Current Company", "current_company_website": "Current Company Domain",
    "current_title": "Current Title", "linkedin_url": "LinkedIn Profile", "headline": "Headline",
    "experience_json": "Experience", "employee_count_range": "Company Size", "updated_at": "Enriched At",
    "latest_company_change_at": "Job Start Date",
    # optional: the new account's ICP fields on a move row (left empty when the export has no such column)
    "main_industry": "Company Industry", "current_location": "Location", "company_country": "Company Country",
}


def _experience(raw: str) -> List[Dict]:
    try:
        jobs = json.loads(raw) if raw else []
    except ValueError:
        return []
    out = []
    for j in jobs if isinstance(jobs, list) else []:
        comp = j.get("company")
        name = comp.get("name") if isinstance(comp, dict) else comp
        site = (comp.get("website") if isinstance(comp, dict) else None) or j.get("website") or j.get("company_domain")
        out.append({"company": name, "website": site, "title": j.get("title"), "job_type": j.get("job_type"),
                    "start": j.get("start") or j.get("start_date"), "end": j.get("end") or j.get("end_date"),
                    "is_current": j.get("is_current")})
    return out


def lookups_from_csv(path: str, seed: List[Dict], columns: Optional[Dict] = None) -> Dict:
    col = {**DEFAULT_COLUMNS, **(columns or {})}
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    by_id = {p.get("Row id"): p for p in seed if p.get("Row id")}
    by_name = {(p["Name"].strip().lower(), p["Customer"].strip().lower()): p for p in seed}
    out: Dict = {(p["Name"], p["Slug"]): {"name": p["Name"], "slug": p["Slug"], "customer": p["Customer"], "ok": True,
                                         "source": "clay", "candidates": []} for p in seed}
    for r in rows:
        g = lambda k: (r.get(col.get(k) or "") or "").strip()  # noqa: E731
        p = by_id.get(g("row_id")) or by_name.get((g("name").lower(), g("customer").lower()))
        if not p:
            continue
        cur = g("current_company")
        out[(p["Name"], p["Slug"])]["candidates"].append({
            "full_name": g("name") or p["Name"], "current_company_name": cur, "current_company_website": g("current_company_website"),
            "current_title": g("current_title"), "linkedin_url": g("linkedin_url"), "headline": g("headline"),
            "experience": _experience(g("experience_json")), "employee_count_range": normalize_size_band(g("employee_count_range")),
            "updated_at": g("updated_at"), "latest_company_change_at": g("latest_company_change_at"),
            "main_industry": g("main_industry"), "current_location": g("current_location"),
            "company_country": g("company_country")})
    return out

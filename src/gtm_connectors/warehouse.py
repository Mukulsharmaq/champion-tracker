"""Champion definition in the warehouse -> champion seed rows for the tracker.

The definition is SQL (sql/champion_definition.sql for Snowflake, sql/champion_definition.sqlite.sql for the demo):
a champion decided AND still uses the product: a won-deal contact role (decision maker, champion, economic buyer,
evaluator) on a deal at their own account closed in the last 3 years, plus product use in the last 365 days; or an
active product admin (last 180 days) who also holds such a role. An admin seat alone is a 'user' ("product admin, no
decision or ownership record"): a title or a seat never makes a champion. Someone who only sat on the deal is a 'user'
("role only, no product use"). Test workspaces and internal users never count as usage. Each output row becomes one
seed row with the CRM contact id as the row id, so seller feedback writes back to the right contact; the seed carries
"Last activity" (latest product use at the customer), "Evidence date" and "Customer domain" (the CRM website) for the
rules.

Demo:  champions_sqlite("fixtures/warehouse/demo_warehouse.sql", today)
Live:  champions_snowflake()   needs SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_WAREHOUSE, SNOWFLAKE_DATABASE and
       key-pair auth (SNOWFLAKE_PRIVATE_KEY_FILE + SNOWFLAKE_PRIVATE_KEY_PASSPHRASE; Snowflake is retiring password
       sign-in for service users) or SNOWFLAKE_PASSWORD. Optional: SNOWFLAKE_SCHEMA, SNOWFLAKE_ROLE.
       pip install snowflake-connector-python
"""
import os
import re
import sqlite3
from typing import Dict, List, Tuple

from . import need

ROOT = os.environ.get("GTM_HOME") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# A won-deal contact role is the decision / ownership record. An admin seat alone is product use, not a decision:
# it maps to 'uses' whatever level a row claims, so a title or a seat never makes a champion.
TAG_BY_EVIDENCE = {"Decision Maker": "chose", "Economic Buyer": "business_case", "Champion": "owns_program",
                   "Evaluator": "initiated_review", "Admin": "uses"}
CRM_URL = os.environ.get("SF_INSTANCE_URL", "https://example.my.salesforce.com").rstrip("/")


def _sql(name: str) -> str:
    with open(os.path.join(ROOT, "sql", name), encoding="utf-8") as f:
        return f.read()


HONORIFICS = {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "dame", "rev", "fr"}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "mba", "cpa", "esq", "pmp"}
PARTICLES = {"de", "da", "di", "del", "della", "der", "den", "du", "la", "le", "van", "von", "bin", "ibn", "al", "st"}


def _bare(token: str) -> str:
    return re.sub(r"[.\s]", "", token).lower()


def split_name(full: str) -> Tuple[str, str]:
    """(first, last) from a full name, used only when the CRM has no FirstName/LastName. Never a split on the first
    space: honorifics and suffixes are dropped ('Dr. Jane Smith, PhD'), 'Last, First' is read as such, a middle name
    stays with the first name ('Mary Ann Smith') and surname particles stay with the last ('Maria de la Cruz')."""
    parts = [p.strip() for p in " ".join((full or "").split()).split(",")]
    parts = [p for p in parts if p and _bare(p) not in SUFFIXES]
    if len(parts) >= 2:                                   # 'Reyes, Dana' -> last first
        last, first = parts[0], " ".join(t for t in parts[1].split() if _bare(t) not in HONORIFICS)
        return first, last
    toks = (parts[0] if parts else "").split()
    while toks and _bare(toks[0]) in HONORIFICS:
        toks = toks[1:]
    while len(toks) > 1 and _bare(toks[-1]) in SUFFIXES:
        toks = toks[:-1]
    if len(toks) < 2:
        return (toks[0] if toks else ""), ""
    i = len(toks) - 1
    while i > 1 and toks[i - 1].lower() in PARTICLES:
        i -= 1
    return " ".join(toks[:i]), " ".join(toks[i:])


def _day(v) -> str:
    return str(v)[:10] if v else ""


def to_seed(rows: List[Dict]) -> List[Dict]:
    seed, seen = [], set()
    for r in rows:
        r = {k.lower(): v for k, v in r.items()}          # Snowflake returns UPPERCASE column names
        if r["contact_id"] in seen:
            continue
        seen.add(r["contact_id"])
        full = " ".join((r.get("full_name") or "").split())
        first, last = (r.get("first_name") or "").strip(), (r.get("last_name") or "").strip()
        if not (first and last):                          # CRM FirstName/LastName first; parse only what is missing
            pf, pl = split_name(full)
            first, last = first or pf, last or pl
        name = " ".join(x for x in (first, last) if x) or full
        champion = (r["level"] or "").lower() == "champion"
        tag = TAG_BY_EVIDENCE.get(r["evidence_type"], "owns_program") if champion else "uses"
        used = _day(r.get("last_activity"))
        if r["evidence_type"] == "Admin":
            ev = f"Product admin; last admin activity {_day(r.get('last_admin_activity')) or _day(r['evidence_at'])}"
            ev += f"; {r['evidence']}" if r.get("evidence") else ""
        else:
            ev = (f"{r['evidence_type']} contact role on a won opportunity "
                  f"(closed {_day(r.get('deal_closed')) or _day(r['evidence_at'])})")
            ev += f"; last product use {used}" if champion else f"; {r.get('evidence') or 'no recent product use'}"
        seed.append({"Row id": r["contact_id"], "Slug": (r["customer_domain"] or "").split(".")[0],
                     "Story URL": f"{CRM_URL}/lightning/r/Contact/{r['contact_id']}/view", "Customer": r["customer_name"],
                     "Name": name, "First": first, "Last": last, "Title on page": r["title"] or "",
                     "Employer on page": r["customer_name"], "Action tag": tag, "Evidence (verbatim)": ev,
                     "Exclude reason": "", "LinkedIn": r.get("linkedin_url") or "",
                     "Last activity": used, "Evidence date": _day(r.get("evidence_at")),
                     "Customer domain": r.get("customer_domain") or ""})
    return seed


def champions_sqlite(warehouse_sql_path: str, today: str) -> List[Dict]:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    with open(warehouse_sql_path, encoding="utf-8") as f:
        con.executescript(f.read())
    rows = [dict(r) for r in con.execute(_sql("champion_definition.sqlite.sql"), {"today": today})]
    con.close()
    return to_seed(rows)


def connect():
    env = need("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_DATABASE")
    try:
        import snowflake.connector  # type: ignore
    except ImportError as e:
        raise RuntimeError("pip install snowflake-connector-python to read the warehouse live") from e
    kw = dict(account=env["SNOWFLAKE_ACCOUNT"], user=env["SNOWFLAKE_USER"], warehouse=env["SNOWFLAKE_WAREHOUSE"],
              database=env["SNOWFLAKE_DATABASE"], schema=os.environ.get("SNOWFLAKE_SCHEMA"), role=os.environ.get("SNOWFLAKE_ROLE"))
    if os.environ.get("SNOWFLAKE_PRIVATE_KEY_FILE"):
        kw.update(authenticator="SNOWFLAKE_JWT", private_key_file=os.environ["SNOWFLAKE_PRIVATE_KEY_FILE"])
        if os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"):
            major = int(getattr(snowflake.connector, "__version__", "0").split(".")[0] or 0)
            kw["private_key_password" if major >= 5 else "private_key_file_pwd"] = os.environ["SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"]
    else:
        kw["password"] = need("SNOWFLAKE_PASSWORD")["SNOWFLAKE_PASSWORD"]
    return snowflake.connector, snowflake.connector.connect(**{k: v for k, v in kw.items() if v is not None})


def champions_snowflake() -> List[Dict]:
    snowflake_connector, con = connect()
    try:
        cur = con.cursor(snowflake_connector.DictCursor)
        cur.execute(_sql("champion_definition.sql"))
        return to_seed(cur.fetchall())
    finally:
        con.close()


# ---- writing each run back to the warehouse (facts and history live there, not on one machine's disk) ----------
RUN_TABLES = ["pg_decisions", "pg_findings", "pg_cards", "pg_feedback", "pg_runs"]


PER_RUN_TABLES = ["pg_decisions", "pg_findings", "pg_cards", "pg_runs"]


def feedback_key(e: Dict) -> str:
    """The warehouse key of one click: its own id when the event has one (uuid / id / event_id), else the card or row
    it was on, the event time as stored (to the microsecond when the log has it), the user, the action and the profile.
    Never a field purge() blanks (the note), so a purged event lands on the same key and overwrites its payload."""
    own = e.get("uuid") or e.get("event_id") or e.get("id")
    if own:
        return f"id:{own}"
    return "|".join(str(e.get(k) or "") for k in ("card_id", "row_id", "at", "user", "action", "profile"))


def _legacy_feedback_key(e: Dict) -> str:
    return f"{e.get('at')}|{e.get('user')}|{e.get('action')}"   # the key before 2026-10-18 (one per second per action)


def write_run(con, run_id: str, run_date: str, rows: List[Dict], findings: List[Dict], cards: List[Dict],
              events: List[Dict], summary: Dict, dialect: str = "sqlite") -> Dict:
    """One row per decision / finding / card / feedback event, keyed by run id, payload as JSON (VARIANT in
    Snowflake). The per-run tables are idempotent: rows for this run id are replaced. pg_feedback keeps every click
    for ever, one row per feedback_key (card or row, time, user, action): an event is upserted (MERGE in Snowflake,
    INSERT ... ON CONFLICT in SQLite) and never deleted, so a lost local log never erases click history, two clicks
    in the same second on different cards both land, and a note blanked by purge() overwrites the stored payload
    (the purge reaches the warehouse). Everything happens in one transaction (all or nothing), with one batched
    statement per table. Returns the rows written per table (for pg_feedback: new or changed events only)."""
    import json as _j
    cur = con.cursor()
    typ = "variant" if dialect == "snowflake" else "text"
    ph = "%s" if dialect == "snowflake" else "?"
    val = f"parse_json({ph})" if dialect == "snowflake" else ph
    for t in RUN_TABLES:          # DDL first: in Snowflake it would commit an open transaction
        cur.execute(f"create table if not exists {t} (run_id text, run_date text, item_key text, payload {typ})")
    if dialect != "snowflake":    # SQLite upserts need a unique key (Snowflake MERGE does not)
        cur.execute("create unique index if not exists pg_feedback_item_key on pg_feedback (item_key)")
    if dialect == "snowflake":
        cur.execute("begin")
    elif not getattr(con, "in_transaction", False):
        cur.execute("begin")
    try:
        for t in PER_RUN_TABLES:
            cur.execute(f"delete from {t} where run_id = {ph}", (run_id,))
        cur.execute("select item_key, payload from pg_feedback")
        stored = {}
        for k, payload in cur.fetchall():
            try:
                stored[k] = payload if isinstance(payload, dict) else _j.loads(payload)
            except (TypeError, ValueError):
                stored[k] = None
        changed, legacy, seen = [], [], set()
        for e in events:
            k = feedback_key(e)
            if k in seen:
                continue
            seen.add(k)
            body = _j.loads(_j.dumps(e, default=str, ensure_ascii=False))
            if k not in stored:
                old = _legacy_feedback_key(e)
                same = stored.get(old)
                if isinstance(same, dict) and all(same.get(f) == body.get(f) for f in ("card_id", "row_id", "at", "user", "action")):
                    legacy.append(old)          # the same click under the old key: move it to the new key
                    stored.pop(old)
            if stored.get(k) != body:
                stored[k] = body
                changed.append((k, e))
        if legacy:
            cur.executemany(f"delete from pg_feedback where item_key = {ph}", [(k,) for k in legacy])
        batches = {"pg_decisions": [(r.get("Row id", ""), r) for r in rows],
                   "pg_findings": [(f"{f['id']}|{f['theme']}", f) for f in findings],
                   "pg_cards": [(c["id"], c) for c in cards],
                   "pg_feedback": changed,
                   "pg_runs": [(run_id, summary)]}
        if dialect == "snowflake":
            upsert = ("merge into pg_feedback t using (select %s as run_id, %s as run_date, %s as item_key, "
                      "parse_json(%s) as payload) s on t.item_key = s.item_key "
                      "when matched then update set t.payload = s.payload "
                      "when not matched then insert (run_id, run_date, item_key, payload) "
                      "values (s.run_id, s.run_date, s.item_key, s.payload)")
        else:
            upsert = ("insert into pg_feedback (run_id, run_date, item_key, payload) values (?, ?, ?, ?) "
                      "on conflict(item_key) do update set payload = excluded.payload")
        counts = {}
        for t, items in batches.items():
            if items:
                sql = upsert if t == "pg_feedback" else \
                    f"insert into {t} (run_id, run_date, item_key, payload) select {ph}, {ph}, {ph}, {val}"
                cur.executemany(sql, [(run_id, run_date, k, _j.dumps(p, default=str, ensure_ascii=False)) for k, p in items])
            counts[t] = len(items)
        if dialect == "snowflake":
            cur.execute("commit")
        else:
            con.commit()
    except Exception:
        if dialect == "snowflake":
            cur.execute("rollback")
        else:
            con.rollback()
        raise
    return counts

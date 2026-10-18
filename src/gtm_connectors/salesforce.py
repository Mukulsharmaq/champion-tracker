"""Salesforce -> accounts and CRM-note signals; seller feedback -> CRM write-back.

Reads SOQL query responses ({"totalSize", "done", "nextRecordsUrl", "records": [...]}) for:
  Account  Id, Name, Website, Type, ParentId, Owner.Id, Owner.Name, Owner.Email, Industry, NumberOfEmployees,
           BillingCountry, <renewal date field>, open opportunity (sub-query)   (industry, employees and country are
           account context and ICP fit on the card)
  Task     Id, Subject, Description, ActivityDate, AccountId, Account.Name, Owner.Name, Owner.Title   (rep notes =
           'reported' signals; "account_id" = AccountId, so notes join accounts on the CRM id, not the name)
           The system's own Tasks never come back as rep notes: their subject starts with TASK_MARKER ("[GTM Signal]",
           set by pg_system.feedback_loop.play_task) and the query skips them; with SF_INTEGRATION_USER_ID set it
           also skips every Task that user owns or created. Only Tasks dated up to today are read (a play's Task is
           due in a week: it is not news from the future).
Field names and values that differ per org are environment variables (see .env.example): SF_RENEWAL_FIELD,
SF_CUSTOMER_TYPES, SF_NOTE_EXCLUDE_SUBTYPES, SF_ACCOUNT_WHERE. Which sources are on, and write-back live vs dry run,
are in config.json.

Demo:  load_accounts("fixtures/salesforce/accounts.json"), load_notes("fixtures/salesforce/tasks.json")
Live:  fetch_accounts(), fetch_notes(since), count_accounts()   needs SF_INSTANCE_URL and either
       SF_CLIENT_ID + SF_CLIENT_SECRET (OAuth client-credentials flow on a connected app with a run-as user) or
       SF_ACCESS_TOKEN (fine for a one-off run).
       The client-credentials token is cached for the life of the process (the Slack app runs for weeks): when
       Salesforce answers 401 (session expired or revoked), the token is dropped, a new one fetched and the call
       retried once. A second 401, or any 403 (missing permission), fails at once.
       Optional: SF_API_VERSION, SF_RENEWAL_FIELD, SF_CUSTOMER_TYPES (comma list of Account.Type values that mean
       customer), SF_NOTE_EXCLUDE_SUBTYPES (default Email,ListEmail: logged emails are not rep notes),
       SF_ACCOUNT_WHERE (an extra SOQL condition that narrows the accounts read, e.g. "Owner.UserRole.Name LIKE 'NA%'"),
       SF_INTEGRATION_USER_ID (the run-as user's Id, 005...: its Tasks are never read back as notes).
Tokens are shared by the Slack app's click threads: one lock guards fetching and refreshing, each call uses its own
snapshot, and a 401 drops the token only if it is still the one that failed (another thread may have refreshed it).
"""
import os
import re
import threading
import urllib.parse
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from . import AuthError, MissingConfig, http_json, load_json, need

RENEWAL_FIELD = os.environ.get("SF_RENEWAL_FIELD", "Renewal_Date__c")
CUSTOMER_TYPES = {t.strip() for t in os.environ.get("SF_CUSTOMER_TYPES", "Customer,Customer - Direct,Customer - Channel").split(",")}
EXCLUDE_SUBTYPES = [t.strip() for t in os.environ.get("SF_NOTE_EXCLUDE_SUBTYPES", "Email,ListEmail").split(",") if t.strip()]
_TOKEN: Dict[str, str] = {}
_TOKEN_LOCK = threading.Lock()
TASK_MARKER = "[GTM Signal]"            # subject prefix of every Task this system creates (feedback_loop.play_task)

ACCOUNT_WHERE = "Type != null"
ACCOUNT_SOQL = ("SELECT Id, Name, Website, Type, ParentId, Owner.Id, Owner.Name, Owner.Email, Industry, "
                "NumberOfEmployees, BillingCountry, {renewal}, "
                "(SELECT StageName, Type FROM Opportunities WHERE IsClosed = false ORDER BY CloseDate LIMIT 1) "
                "FROM Account WHERE {where}")
COUNT_SOQL = "SELECT COUNT() FROM Account WHERE {where}"
NOTES_SOQL = ("SELECT Id, Subject, Description, ActivityDate, AccountId, Account.Name, Owner.Name, Owner.Title FROM Task "
              "WHERE ActivityDate >= {since} AND ActivityDate <= {until} AND AccountId != null "
              "AND (Subject = null OR (NOT Subject LIKE '" + TASK_MARKER + "%')){integration}{subtypes}")
# Description is a long text area: not filterable. {integration}: the run-as user's own Tasks (SF_INTEGRATION_USER_ID).


def _instance() -> str:
    return os.environ.get("SF_INSTANCE_URL", "https://example.my.salesforce.com").rstrip("/")


def _version() -> str:
    return os.environ.get("SF_API_VERSION", "v61.0")


def account_where() -> str:
    """The accounts filter: every typed account, narrowed by SF_ACCOUNT_WHERE when it is set."""
    extra = os.environ.get("SF_ACCOUNT_WHERE", "").strip()
    return f"{ACCOUNT_WHERE} AND ({extra})" if extra else ACCOUNT_WHERE


def _employees(v) -> Optional[int]:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def to_accounts(resp: Dict) -> List[Dict]:
    out = []
    for r in resp.get("records", []):
        opp = ((r.get("Opportunities") or {}).get("records") or [{}])[0]
        owner = r.get("Owner") or {}
        out.append({"account": r.get("Name") or "", "domain": r.get("Website") or "",
                    "industry": r.get("Industry") or "", "employees": _employees(r.get("NumberOfEmployees")),
                    "country": r.get("BillingCountry") or "",
                    "type": "customer" if (r.get("Type") or "") in CUSTOMER_TYPES else "prospect",
                    "owner": owner.get("Name") or "", "owner_email": owner.get("Email") or "",
                    "owner_id": owner.get("Id") or "", "parent_id": r.get("ParentId") or "",
                    "renewal_date": r.get(RENEWAL_FIELD) or "",
                    "open_opportunity": opp.get("Type") or opp.get("StageName") or "", "crm_id": r.get("Id") or ""})
    return out


def to_note_signals(resp: Dict) -> List[Dict]:
    out = []
    for r in resp.get("records", []):
        if not (r.get("Description") or "").strip():
            continue
        if (r.get("Subject") or "").startswith(TASK_MARKER):   # our own write-back (the query skips it too)
            continue
        owner = r.get("Owner") or {}
        out.append({"source": "crm", "id": f"CRM-{r['Id']}", "date": (r.get("ActivityDate") or "")[:10],
                    "account": (r.get("Account") or {}).get("Name") or "", "account_id": r.get("AccountId") or "",
                    "speaker": owner.get("Name") or "",
                    "speaker_title": owner.get("Title") or "", "side": "internal", "text": (r.get("Description") or "").strip(),
                    "url": f"{_instance()}/lightning/r/Task/{r['Id']}/view"})
    return out


def load_accounts(path: str) -> List[Dict]:
    return to_accounts(load_json(path))


def load_notes(path: str) -> List[Dict]:
    return to_note_signals(load_json(path))


def _client_credentials() -> bool:
    return bool(os.environ.get("SF_CLIENT_ID") and os.environ.get("SF_CLIENT_SECRET"))


def _auth() -> Tuple[str, Dict[str, str]]:
    """(instance url, headers). Client-credentials token fetched once and cached (under _TOKEN_LOCK, so two click
    threads never see a half-cleared token); else a supplied access token. Returns a snapshot, never a live view."""
    if _client_credentials():
        with _TOKEN_LOCK:
            token, base = _TOKEN.get("token"), _TOKEN.get("base")
            if not (token and base):
                env = need("SF_INSTANCE_URL", "SF_CLIENT_ID", "SF_CLIENT_SECRET")
                r = http_json("POST", f"{env['SF_INSTANCE_URL'].rstrip('/')}/services/oauth2/token", {},
                              {"grant_type": "client_credentials", "client_id": env["SF_CLIENT_ID"],
                               "client_secret": env["SF_CLIENT_SECRET"]}, form=True)
                token, base = r["access_token"], (r.get("instance_url") or env["SF_INSTANCE_URL"]).rstrip("/")
                _TOKEN.clear()
                _TOKEN.update(token=token, base=base)
        return base, {"Authorization": f"Bearer {token}"}
    if not (os.environ.get("SF_INSTANCE_URL") and os.environ.get("SF_ACCESS_TOKEN")):
        raise MissingConfig("Set these environment variables for live mode: SF_INSTANCE_URL and SF_CLIENT_ID + "
                            "SF_CLIENT_SECRET (client-credentials flow), or SF_ACCESS_TOKEN for a one-off run")
    return os.environ["SF_INSTANCE_URL"].rstrip("/"), {"Authorization": f"Bearer {os.environ['SF_ACCESS_TOKEN']}"}


def _send(method: str, path: str, body: Optional[dict] = None) -> Dict:
    """One authorised call to a path under the instance URL. A 401 with client credentials means the cached token
    expired while the process kept running: drop it, fetch a new one, retry once. The base URL is re-read after the
    refresh, because a new token can come with a new instance_url."""
    for attempt in (1, 2):
        base, h = _auth()
        try:
            return http_json(method, f"{base}{path}", h, body)
        except AuthError as e:
            if e.code != 401 or attempt == 2 or not _client_credentials():
                raise
            _drop_token(h["Authorization"][len("Bearer "):])
    raise AssertionError("unreachable")


def _drop_token(failed: str):
    """Forget the cached token only if it is still the one that got the 401: another thread may already hold a
    fresh one, and clearing that would make every thread fetch again."""
    with _TOKEN_LOCK:
        if _TOKEN.get("token") == failed:
            _TOKEN.clear()


def _query(soql: str) -> Dict:
    path = f"/services/data/{_version()}/query?q={urllib.parse.quote(soql)}"
    records = []
    while path:
        r = _send("GET", path)
        records += r.get("records", [])
        path = r.get("nextRecordsUrl")       # relative, so a token refresh mid-way keeps paging
    return {"records": records, "totalSize": len(records), "done": True}


def count_accounts() -> int:
    """How many accounts fetch_accounts() would read (SELECT COUNT(): one row-less call, whatever the org's size)."""
    soql = COUNT_SOQL.format(where=account_where())
    return int(_send("GET", f"/services/data/{_version()}/query?q={urllib.parse.quote(soql)}").get("totalSize") or 0)


def preflight() -> Dict:
    """Live check before the first run: the token works, the fields the queries need exist, and how many accounts the
    run would read. One describe call and one COUNT(); it never pulls the accounts themselves."""
    fields = _send("GET", f"/services/data/{_version()}/sobjects/Account/describe")["fields"]
    acc = {f["name"] for f in fields}
    types = {v["value"] for f in fields if f["name"] == "Type" for v in f.get("picklistValues", [])}
    need_fields = {RENEWAL_FIELD, "Pipeline_Play__c", "Pipeline_Play_Status__c", "Pipeline_Play_Note__c", "Pipeline_Surface_Version__c"}
    return {"missing_account_fields": sorted(need_fields - acc), "customer_types_found": sorted(CUSTOMER_TYPES & types),
            "account_types_in_org": sorted(types), "accounts": count_accounts(), "account_filter": account_where()}


def fetch_accounts() -> List[Dict]:
    return to_accounts(_query(ACCOUNT_SOQL.format(renewal=RENEWAL_FIELD, where=account_where())))


def integration_filter() -> str:
    """' AND OwnerId != X AND CreatedById != X' for SF_INTEGRATION_USER_ID (checked to be a Salesforce Id)."""
    uid = os.environ.get("SF_INTEGRATION_USER_ID", "").strip()
    if not uid:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9]{15}([A-Za-z0-9]{3})?", uid):
        raise MissingConfig(f"SF_INTEGRATION_USER_ID={uid!r} is not a Salesforce user Id (15 or 18 letters and digits)")
    return f" AND OwnerId != '{uid}' AND CreatedById != '{uid}'"


def notes_soql(since: str, until: Optional[str] = None) -> str:
    """The rep-notes query: Tasks dated since..until (default today), not the system's own (TASK_MARKER subject,
    SF_INTEGRATION_USER_ID), not logged emails (SF_NOTE_EXCLUDE_SUBTYPES)."""
    for d in (since, until):
        if d is not None:
            date.fromisoformat(str(d))                   # a date literal (YYYY-MM-DD), never free text in the query
    sub = "".join(f" AND TaskSubtype != '{t}'" for t in EXCLUDE_SUBTYPES)
    return NOTES_SOQL.format(since=since, until=until or date.today().isoformat(), integration=integration_filter(),
                             subtypes=sub)


def fetch_notes(since: str, until: Optional[str] = None) -> List[Dict]:
    return to_note_signals(_query(notes_soql(since, until)))


def _log(entry: Dict, log_path: str):
    """Append one line to the write-back log, dated (logged_at) so the retention purge can blank its notes, under the
    log's lock (the purge rewrites the file)."""
    from champion_tracker.feedback import append_line
    append_line(log_path, dict(entry, logged_at=datetime.now(timezone.utc).isoformat(timespec="seconds")))


def write_back(update: Dict, dry_run: bool = True, log_path: str = "out/crm_write_back.jsonl") -> Dict:
    """Apply one feedback.crm_write_back() result. Dry run appends it to a file; live PATCHes the record by Id."""
    if dry_run:
        _log(update, log_path)
        return {"dry_run": True, **update}
    rid = update["match_on"]["Id"]
    _send("PATCH", f"/services/data/{_version()}/sobjects/{update['object']}/{rid}", update["fields"])
    return {"dry_run": False, **update}


def create_record(rec: Dict, dry_run: bool = True, log_path: str = "out/crm_write_back.jsonl") -> Dict:
    """Create one record (e.g. the play's Task). Dry run appends it to the write-back log."""
    if dry_run:
        _log({"create": rec}, log_path)
        return {"dry_run": True, **rec}
    r = _send("POST", f"/services/data/{_version()}/sobjects/{rec['object']}/", rec["fields"])
    return {"dry_run": False, "id": r.get("id"), **rec}

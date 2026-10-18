"""People-data adapter (live mode). Builds the candidates list the rules expect: EVERY same-name profile with its
work history, never a single best guess. Provider-neutral; the example client targets a REST people search with
experience history (GetLeads-style). Swap the client for Clay, ZoomInfo or your warehouse; the rules don't change.

Lookup (from the demo run):
  1. match by name + company (fast, batches of 100) -> people still at the customer resolve here
  2. for misses only: name search 50 deep, with work history (common names need depth; 10 deep wrongly holds them)
  3. if that search came back full (exactly 50) with nobody tied to the customer, the list was cut off: every
     same-name profile is pulled (up to PEOPLE_DEEP_LIMIT, default 5000, through PEOPLE_EXPORT_URL when the provider
     has a bulk/export endpoint) with its work history, and only the profiles tied to the customer are kept. Most
     people APIs cannot filter on past employers (a company filter means the CURRENT employer, which a mover has
     left), so this full pull is the fix; on the real run it resolved 24 of 37 capped people. If the provider does
     filter on work history, PEOPLE_PAST_COMPANY_FIELD names that field and it is sent as well.
Every record carries "depth" (the search depth asked for), "capped" (the first list was cut off), "full_search"
(every same-name profile was read) and "searched" (how many) on a successful lookup; a failed lookup carries
"depth" and "capped" only.
Rate: one shared token bucket (PEOPLE_RATE_PER_SEC, default 5 calls a second) for every worker thread. Retries: only
408, 429, 5xx and network errors, waiting what Retry-After says when the provider sends it; any other 4xx fails at
once (retrying a bad request only burns quota).
"""
import email.utils
import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional

SEARCH_URL = os.environ.get("PEOPLE_SEARCH_URL", "")          # e.g. https://<provider>/api/v1/contacts/search
MATCH_URL = os.environ.get("PEOPLE_MATCH_URL", "")            # e.g. https://<provider>/api/v1/enrich/from-person
TOKEN = os.environ.get("PEOPLE_DATA_TOKEN", "")               # never commit keys
# The provider's work-history filter: the customer is sent here as a PAST employer when the plain search was cut off.
PAST_COMPANY_FIELD = os.environ.get("PEOPLE_PAST_COMPANY_FIELD", "")      # only if your provider filters on work history
DEEP_LIMIT = int(os.environ.get("PEOPLE_DEEP_LIMIT", "5000"))             # the full same-name pull for capped searches
DEEP_URL = os.environ.get("PEOPLE_EXPORT_URL", "")                        # optional bulk/export endpoint for that pull
RETRY_STATUS = {408, 429}            # plus every 5xx
MAX_RETRY_WAIT_S = 120               # never sleep longer than this on one Retry-After
# LinkedIn work history is month-precise; this provider pads it to the 1st ("2026-07-01"; 9,185 of 9,186 starts on
# the real run). Restore the month so a move is aged from the end of its month, not the 1st. "0" turns this off.
MONTH_PADDED = os.environ.get("PEOPLE_DATES_MONTH_PADDED", "1") != "0"
DATE_FIELDS = ("role_start_date", "latest_company_change_at", "current_role_start_date")

COLUMNS = ["full_name", "first_name", "last_name", "current_title", "current_employer", "current_employer_website",
           "linkedin_url", "role_start_date", "latest_company_change_at", "contact_location", "linkedin_headline",
           "last_refresh_at", "contact_updated_at", "employee_count_range", "main_industry", "experience_json"]


# Provider field names -> the keys rules.py reads (providers differ; normalise once, here).
KEY_MAP = {"current_employer": "current_company_name", "current_employer_website": "current_company_website",
           "linkedin_headline": "headline", "contact_location": "current_location", "contact_updated_at": "updated_at"}


class LookupError_(RuntimeError):
    """Provider failure. Callers record the person as ok=False so the rules Hold with 'Lookup failed; retry'."""


class RateLimiter:
    """A token bucket shared by every worker thread: at most `rate` calls a second on average, bursts up to `rate`.
    rate <= 0 turns it off. clock and sleep are injectable for tests."""

    def __init__(self, rate: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.rate, self.clock, self.sleep = float(rate), clock, sleep
        self.capacity = max(1.0, self.rate)
        self.tokens, self.stamp = self.capacity, clock()
        self.lock = threading.Lock()

    def acquire(self) -> float:
        """Take one token, waiting for it when the bucket is empty. Returns the seconds waited."""
        if self.rate <= 0:
            return 0.0
        waited = 0.0
        while True:
            with self.lock:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
                self.stamp = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return waited
                wait = (1 - self.tokens) / self.rate
            self.sleep(wait)
            waited += wait


def _rate_from_env() -> float:
    try:
        return float(os.environ.get("PEOPLE_RATE_PER_SEC", "5"))
    except ValueError:
        return 5.0


LIMITER = RateLimiter(_rate_from_env())
_sleep = time.sleep          # patched in tests


def _retry_after(e: urllib.error.HTTPError) -> Optional[float]:
    """Seconds the provider asked us to wait (Retry-After as seconds or an HTTP date), capped; None when absent."""
    raw = (e.headers.get("Retry-After") if e.headers else None) or ""
    raw = raw.strip()
    if not raw:
        return None
    try:
        secs = float(raw)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(raw)
            secs = when.timestamp() - time.time()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    return max(0.0, min(float(MAX_RETRY_WAIT_S), secs))


def _post(url: str, body: dict, tries: int = 4) -> dict:
    """POST JSON with the shared rate limit. Retries only 408, 429, 5xx and network errors (Retry-After first, else
    4, 8, 12 s); 401/403 and every other 4xx fail at once."""
    if not (url and TOKEN):
        raise RuntimeError("Set PEOPLE_SEARCH_URL / PEOPLE_MATCH_URL and PEOPLE_DATA_TOKEN to use live mode.")
    data = json.dumps(body).encode()
    last: Optional[BaseException] = None
    for i in range(tries):
        wait = 4.0 * (i + 1)
        LIMITER.acquire()
        try:
            req = urllib.request.Request(url, data, {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=90))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise LookupError_(f"auth failed ({e.code})") from e
            if e.code not in RETRY_STATUS and e.code < 500:
                raise LookupError_(f"provider refused the request ({e.code}); not retried") from e
            last = e
            asked = _retry_after(e)
            wait = wait if asked is None else asked
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:   # network, timeout, bad JSON
            last = e
        if i < tries - 1:
            _sleep(wait)
    raise LookupError_(f"lookup failed after {tries} tries: {last}")


def month_only(d):
    """'2026-07-01' -> '2026-07' when the provider pads month-only dates to the 1st; anything else unchanged."""
    if MONTH_PADDED and isinstance(d, str):
        m = re.match(r"^(\d{4}-\d{2})-01(?:$|T00:00(?::00)?(?:\.0+)?(?:Z|[+-]00:?00)?$)", d.strip())
        if m:
            return m.group(1)
    return d


def normalise(c: dict) -> dict:
    for src, dst in KEY_MAP.items():
        if src in c and dst not in c:
            c[dst] = c.pop(src)
    for k in DATE_FIELDS:
        if k in c:
            c[k] = month_only(c[k])
    return c


def _slim_experience(raw) -> List[Dict]:
    jobs = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
    return [{"company": (j.get("company") or {}).get("name"), "website": (j.get("company") or {}).get("website"),
             "title": j.get("title"), "job_type": j.get("job_type"), "start": month_only(j.get("start_date")),
             "end": month_only(j.get("end_date")), "is_current": j.get("is_current")} for j in jobs]


def search_candidates(first: str, last: str, depth: int = 50, company: Optional[str] = None,
                      past_company: Optional[str] = None, url: str = "") -> List[Dict]:
    """Every same-name profile, up to depth. company: an optional company_name filter (most providers read it as the
    CURRENT employer). past_company: an optional work-history filter, sent as PAST_COMPANY_FIELD; this is the one that
    can find a champion who left. Both are used only when the plain search came back full."""
    body = {"first_name": first, "last_name": last, "limit": depth, "columns": COLUMNS}
    if company:
        body["company_name"] = company
    if past_company and PAST_COMPANY_FIELD:
        body[PAST_COMPANY_FIELD] = past_company
    r = _post(url or SEARCH_URL, body)
    out = []
    for c in r.get("contacts", []) or []:
        c["experience"] = _slim_experience(c.pop("experience_json", None))
        out.append(normalise(c))
    return out


def _profile_key(c: Dict) -> str:
    return c.get("linkedin_url") or f"{(c.get('full_name') or '').lower()} @ {(c.get('current_company_name') or '').lower()}"


def _merge(*lists: List[Dict]) -> List[Dict]:
    """One entry per profile (LinkedIn URL, else name @ company), first seen wins."""
    seen, out = set(), []
    for c in (c for lst in lists for c in lst):
        k = _profile_key(c)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def match_person(first: str, last: str, company: str) -> List[Dict]:
    """Step 1: match on name + company (fast; resolves people still at the customer or tied to it by history)."""
    if not MATCH_URL:
        return []
    r = _post(MATCH_URL, {"first_name": first, "last_name": last, "company_name": company, "columns": COLUMNS})
    found = r.get("contacts") or ([r["contact"]] if r.get("contact") else [])
    out = []
    for c in found:
        c["experience"] = _slim_experience(c.pop("experience_json", None))
        out.append(normalise(c))
    return out


def fetch_profile(url: str) -> Optional[Dict]:
    """One profile by its URL (the review owner pasted it): a normalised candidate, or None when PEOPLE_PROFILE_URL
    is not set, the provider does not know the profile, or the call fails. The pipeline then keeps the reviewer's
    choice as a bare candidate (name + URL), and the row says what is still missing."""
    endpoint = os.environ.get("PEOPLE_PROFILE_URL", "")
    if not (endpoint and url):
        return None
    try:
        r = _post(endpoint, {"linkedin_url": url, "columns": COLUMNS})
    except (LookupError_, RuntimeError):
        return None
    found = r.get("contact") or next(iter(r.get("contacts") or []), None)
    if not isinstance(found, dict) or not (found.get("full_name") or found.get("first_name")):
        return None
    found["experience"] = _slim_experience(found.pop("experience_json", None))
    found.setdefault("linkedin_url", url)
    return normalise(found)


def _tied_to(c: Dict, company: str) -> bool:
    """Tied to the customer by name (same_company) or by a website named for it (ryder.com for Ryder)."""
    from ..matching import domain_is_named_for, same_company
    jobs = [(c.get("current_company_name"), c.get("current_company_website"))]
    jobs += [(e.get("company"), e.get("website")) for e in c.get("experience") or []]
    return any(same_company(n, company) or domain_is_named_for(w, company) for n, w in jobs)


def lookup_record(person: dict, depth: int = 50) -> dict:
    """One person -> the lookup record rules.run expects. Provider failures become ok=False (Hold: retry).
    Name + company first; only when that finds nobody tied to the customer, every same-name profile 50 deep
    (10 deep silently held people with common names: the hardest failure in docs/failures_fixed.md).
    A search that returns exactly `depth` profiles was cut off: if none is tied to the customer, every same-name
    profile is pulled (up to PEOPLE_DEEP_LIMIT, through PEOPLE_EXPORT_URL when set) with its work history and tied
    locally by the rules. Most people APIs cannot filter on past employers (a company filter means the CURRENT
    employer, which a mover has left), so the full pull is the fix; PEOPLE_PAST_COMPANY_FIELD adds a work-history
    filter only for providers that support one. On the real run this resolved 24 of 37 capped people.
    Every record carries depth, capped, full_search and searched."""
    first, last, cust = person.get("First", ""), person.get("Last", ""), person.get("Customer", "")
    source, capped, full, searched = "match", False, False, 0
    try:
        cands = [c for c in match_person(first, last, cust) if _tied_to(c, cust)]
        if not cands:
            cands, source = search_candidates(first, last, depth), "search"
            capped = len(cands) >= depth
            if capped and not any(_tied_to(c, cust) for c in cands):
                deep = search_candidates(first, last, DEEP_LIMIT, url=DEEP_URL or SEARCH_URL)
                past = search_candidates(first, last, depth, past_company=cust) if PAST_COMPANY_FIELD else []
                full, searched = len(deep) < DEEP_LIMIT, len(deep)
                cands = _merge(cands, [c for c in deep + past if _tied_to(c, cust)])
                source = "search+full_pull"
        return {"name": person["Name"], "slug": person["Slug"], "customer": cust, "ok": True, "source": source,
                "capped": capped, "depth": depth, "full_search": full, "searched": searched, "candidates": cands}
    except LookupError_ as e:
        return {"name": person["Name"], "slug": person["Slug"], "customer": cust, "ok": False,
                "source": source, "capped": capped, "depth": depth, "error": str(e), "candidates": []}

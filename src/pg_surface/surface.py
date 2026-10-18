"""The pipeline-generation surface: one card per account that says which account, why now (with evidence), who to
contact and which approved play to run. Inputs come from three places and are joined by CRM id, then domain, then name:

  accounts         CRM accounts (type, owner, renewal date, open opportunity, crm_id, optional parent_id, and the
                   optional firmographics industry, employees, country)
  champion rows    decisions from champion_tracker.rules.run (moves in, moves out, champions still there)
  findings         GTM-brief findings for the period (gtm_briefs.extract), optionally with the CRM "account_id"

The code decides the play from ordered rules; every card lists the evidence and what still needs a check. Each card
also records exactly which champion rows and findings it was built from, so a seller's correction can be replayed.
Every card carries a one-line account context (type, owner, renewal, open opportunity, industry, size, country) and,
when an ICP is given, whether the account fits it (outside the ICP: half the priority and a check, never dropped).
A CRM id that matches no account read is never joined by domain or name: the record exists but was not read.
Review moves older than old_move_days (365) make no card: a move that old is rarely news (the caller lists them).
"""
import re
from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional

from champion_tracker.config import RECENT_DAYS
from champion_tracker.matching import company_tokens, date_range, days_since, domain, normalize_size_band, same_company
from .plays import PLAYS, RULES, VERSION as PLAYS_VERSION, pick_play

SURFACE_VERSION = "1.3"
OLD_MOVE_DAYS = 365          # config old_move_days: a Review move older than this makes no card
ICP_FACTOR = 0.5             # an account outside the ICP keeps its card at half the priority, with a check
# Firmographics a champion row can carry for the new company (people data), used when the CRM has none.
ROW_FIRMO_KEYS = {"industry": "New company industry", "size": "New company size", "country": "New company country"}
_COUNTRY_ALIASES = {"us": "united states", "usa": "united states", "u.s.": "united states", "u.s.a.": "united states",
                    "united states of america": "united states", "america": "united states",
                    "uk": "united kingdom", "u.k.": "united kingdom", "gb": "united kingdom", "great britain": "united kingdom",
                    "england": "united kingdom", "uae": "united arab emirates", "deutschland": "germany"}
RENEWAL_WINDOW_DAYS = 120
WEIGHTS = {"champion_in_card": 40, "champion_in_review": 20, "champion_out": 25, "competitor_risk_customer": 30,
           "competitor_risk_prospect": 15, "pricing_customer_renewal": 30, "pricing_customer": 15, "pricing_prospect": 10,
           "expansion_customer": 20, "timing": 10}
REPORTED_FACTOR = 0.6
THEME_ORDER = ["competitor", "pricing_friction", "expansion", "timing", "people_change", "engagement"]
THEME_LABELS = {"competitor": "Competitor", "pricing_friction": "Pricing", "expansion": "Expansion", "timing": "Timing",
                "people_change": "People change", "engagement": "Engagement"}
SOURCE_LABELS = {"call": "Call", "crm": "CRM note", "field": "Field note"}
TAGS = {"direct": "", "reported": " (reported by our team)", "unverified": " (speaker not identified)"}
# Where a champion row carries CRM ids (set upstream when the tracker resolved them): the new employer, the old one.
NEW_ID_KEYS = ("New account id", "account_id")
OLD_ID_KEYS = ("Case study account id", "Old account id")
_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "ac", "edu", "ne", "or", "gob"}
OPEN_DEAL_PLAYS = {r["play"] for r in RULES if "open_deal" in r.get("all", [])}


def account_key(acc: Dict) -> str:
    """One key per CRM record: the CRM id, else the domain, else the name. Two records on one domain stay apart."""
    return acc.get("crm_id") or domain(acc.get("domain")) or (acc.get("account") or "").strip().lower()


def _id_key(x) -> str:
    """Salesforce hands out 15- and 18-character forms of one Id (Gong uses 18, some exports 15): compare on 15."""
    s = str(x or "").strip()
    return s[:15] if len(s) == 18 and s.isalnum() else s


def registrable(dom: str) -> str:
    """The name part of a domain without its public suffix: initech.com, initech.de, eu.initech.co.uk -> 'initech'."""
    parts = [p for p in domain(dom).split(".") if p]
    if len(parts) < 2:
        return parts[0] if parts else ""
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2]


def id_unread(accounts: List[Dict], account_id: str = "") -> bool:
    """True when a CRM id is given, the accounts carry CRM ids, and none of them is this one: the record exists in the
    CRM but was not read (SF_ACCOUNT_WHERE narrowed the accounts, or it is another region's record)."""
    aid = _id_key(account_id)
    return bool(aid) and any(a.get("crm_id") for a in accounts) and not any(_id_key(a.get("crm_id")) == aid for a in accounts)


def match_account(accounts: List[Dict], name: str = "", dom: str = "", account_id: str = "") -> Optional[Dict]:
    """CRM id first (Gong and Salesforce hand it over, and two records can share a name). Then domain. Then an exact
    (normalised) name. A looser name match counts only when exactly one account fits.
    An id that matches no account is unmatched (None), never joined by domain or name: a Gong call on an EMEA record
    must not land on the NA record that shares its name. Only when no account carries a CRM id at all (a CSV export
    without ids) can the id not be checked, and the domain and name decide."""
    aid = _id_key(account_id)
    if aid:
        hits = [a for a in accounts if _id_key(a.get("crm_id")) == aid]
        if hits:
            return hits[0]
        if any(a.get("crm_id") for a in accounts):
            return None
    d = domain(dom)
    if d:
        hits = [a for a in accounts if domain(a.get("domain")) == d]
        if len(hits) == 1:
            return hits[0]
        if hits and name:
            exact = [a for a in hits if company_tokens(a["account"]) == company_tokens(name)]
            if len(exact) == 1:
                return exact[0]
    if not name:
        return None
    exact = [a for a in accounts if company_tokens(a["account"]) == company_tokens(name) and company_tokens(name)]
    if len(exact) == 1:
        return exact[0]
    loose = [a for a in accounts if same_company(a["account"], name)]
    return loose[0] if len(loose) == 1 else None


def blocked_reason(blocked: Optional[Dict], name: str = "", dom: str = "") -> str:
    """Why a company is off limits (competitor, partner), or ''. Domain (or a subdomain of it) first, then the name."""
    if not blocked:
        return ""
    d = domain(dom)
    for k, why in (blocked.get("domains") or {}).items():
        k = domain(k)
        if d and k and (d == k or d.endswith("." + k)):
            return why or "blocked"
    for k, why in (blocked.get("names") or {}).items():
        if name and same_company(name, k):
            return why or "blocked"
    return ""


def _row_id(r: Dict, keys) -> str:
    return next((str(r[k]).strip() for k in keys if str(r.get(k) or "").strip()), "")


def youngest_move_age(r: Dict, today: date) -> Optional[int]:
    """The youngest the move can be: from 'Joined new company' at its latest possible day (a month-only '2025-11' is at
    most 322 days old on 2026-10-18), else the row's 'Move age (days)'. None when unknown."""
    rng = date_range(r.get("Joined new company"))
    if rng and rng[0] <= today:
        return (today - min(rng[1], today)).days
    age = r.get("Move age (days)")
    if isinstance(age, int):
        return age
    return int(age) if isinstance(age, str) and age.strip().isdigit() else None


def _left_recently(r: Dict) -> bool:
    age = r.get("Move age (days)")
    age = int(age) if isinstance(age, str) and age.strip().isdigit() else age
    return isinstance(age, int) and age <= RECENT_DAYS and "still open" not in (r.get("Reason") or "")


def _sister_customers(accounts: List[Dict], d: str) -> List[Dict]:
    """Customers on another domain with the same registrable name (initech.de vs initech.com): maybe one family."""
    if not d:
        return []
    return [a for a in accounts if a.get("type") == "customer" and domain(a.get("domain")) and domain(a.get("domain")) != d
            and registrable(a.get("domain")) == registrable(d)]


def build_cards(accounts: List[Dict], champion_rows: List[Dict], findings: List[Dict], today: date,
                unmatched: Optional[List[Dict]] = None, blocked: Optional[Dict] = None, notes: Optional[List[str]] = None,
                default_owner: str = "", old_move_days: int = OLD_MOVE_DAYS, icp: Optional[Dict] = None,
                old_moves: Optional[List[Dict]] = None) -> List[Dict]:
    """One card per account with a reason to act, ranked.
      unmatched      a list that receives every finding that matched no account, so the caller can count and alert
      blocked        {"domains": {domain: reason}, "names": {company: reason}}: competitors and partners. A champion
                     who joined one gets no card; `notes` (a list) receives a plain line saying so
      default_owner  who gets a card with no account owner (a champion at a company not in the CRM yet)
      old_move_days  a Review move older than this (its youngest possible age) counts neither as champion in nor as
                     champion out: no card. `old_moves` (a list) receives each such row, for the review canvas table.
                     They do not go into `notes`, whose every line becomes a run-health alert.
      icp            {"industries_include": [...], "industries_exclude": [...], "min_employees": n, "max_employees": n,
                     "countries": [...]}, every key optional. Outside it: priority x 0.5 and a check (see icp_fit)"""
    accounts = [dict(a) for a in accounts]
    by_id = {_id_key(a["crm_id"]): a for a in accounts if a.get("crm_id")}
    groups = defaultdict(lambda: {"in": [], "out": [], "still": [], "signals": []})

    for r in champion_rows:
        if r.get("Level") != "Champion" or r.get("Same person?") != "Confirmed":
            continue
        if r.get("Moved?") == "Yes" and r.get("Decision") in ("Card", "Review"):
            age = youngest_move_age(r, today)
            if r.get("Decision") == "Review" and old_move_days is not None and age is not None and age > old_move_days:
                if old_moves is not None:
                    old_moves.append(r)
                continue          # rarely news: listed with the old moves, never a "Check first" card for a seller
            new_id = _row_id(r, NEW_ID_KEYS)
            unread = id_unread(accounts, new_id)
            acc = match_account(accounts, r.get("New company", ""), r.get("New domain", ""), new_id)
            why_blocked = blocked_reason(blocked, r.get("New company", ""), r.get("New domain", "")) or \
                (blocked_reason(blocked, acc.get("account", ""), acc.get("domain", "")) if acc else "")
            old = match_account(accounts, r.get("Case study", ""), account_id=_row_id(r, OLD_ID_KEYS))
            if old and old.get("type") == "customer" and _left_recently(r):
                groups[account_key(old)]["out"].append(dict(r, **{"Joined (blocked)": why_blocked}) if why_blocked else r)
            if why_blocked:   # a competitor or partner: never a "congratulate them" card
                if notes is not None:
                    where = r.get("New company") or (acc or {}).get("account") or domain(r.get("New domain"))
                    notes.append(f"{r['Person']} joined {where} ({why_blocked}): no outreach")
                continue
            d = domain(r.get("New domain"))
            sisters = _sister_customers(accounts, d)
            by_crm_id = bool(acc and new_id and _id_key(acc.get("crm_id")) == _id_key(new_id))
            if acc is not None and any(acc is x for x in sisters) and not by_crm_id:
                acc = None    # matched only by name to a sister domain: initech.de is not the initech.com record
            if not acc:   # a champion moved to a company that is not in the CRM yet: surface it anyway
                acc = {"account": r.get("New company", ""), "domain": r.get("New domain", ""), "type": "prospect",
                       "owner": "", "not_in_crm": True}
                if unread:     # the upstream id names a CRM record this run did not read: never another record
                    acc["crm_id_unread"] = new_id
                if sisters:
                    acc.update(looks_like=sisters[0]["account"], looks_like_domain=domain(sisters[0].get("domain")))
                accounts.append(acc)
            groups[account_key(acc)]["in"].append(r)
        elif r.get("Moved?") == "No" and r.get("Decision") == "Watch":
            acc = match_account(accounts, r.get("Case study", ""), r.get("New domain", ""), _row_id(r, OLD_ID_KEYS))
            if acc:
                groups[account_key(acc)]["still"].append(r)

    for f in findings:
        if f.get("theme") in (None, "", "held"):
            continue
        acc = match_account(accounts, f.get("account", ""), account_id=f.get("account_id", ""))
        if acc:
            groups[account_key(acc)]["signals"].append(f)
        elif unmatched is not None:
            unmatched.append(f)

    cards, seen_ids = [], set()
    for acc in accounts:
        g = groups.get(account_key(acc))
        if not g or not (g["in"] or g["out"] or g["signals"]):
            continue
        parent = by_id.get(_id_key(acc.get("parent_id")))
        parent = parent if parent is not None and parent is not acc and parent.get("type") == "customer" else None
        card = _card(acc, g, today, parent, default_owner, icp)
        if card is None:
            continue
        while card["id"] in seen_ids:          # two records without a CRM id on one domain
            card["id"] += "-2"
        seen_ids.add(card["id"])
        cards.append(card)
    cards.sort(key=lambda c: (-c["priority"], c["account"]))
    for i, c in enumerate(cards, 1):
        c["rank"] = i
    return cards


def _has(sigs: List[Dict], theme: str, stance: str = "") -> List[Dict]:
    return [s for s in sigs if s["theme"] == theme and (not stance or s.get("stance") == stance)]


def _w(sigs: List[Dict], weight: int) -> float:
    if not sigs:
        return 0
    return weight if any(s["confidence"] == "direct" for s in sigs) else weight * REPORTED_FACTOR


def label(s: Dict) -> str:
    """Human label for a source link: 'Call 05:12, Hannah Cole, 2026-10-13'."""
    at = f" {s['at']}" if s.get("at") else ""
    return f"{SOURCE_LABELS.get(s.get('source'), 'Source')}{at}, {s.get('speaker') or 'unknown'}, {s.get('date')}"


def _by_signal(sigs: List[Dict]):
    """Findings -> one group per source signal (a quote is shown once, with all its themes).
    The customer's own words come first, newest first; our team's reports follow."""
    groups = defaultdict(list)
    for s in sigs:
        groups[s["id"]].append(s)
    return sorted(groups.items(), key=lambda kv: (kv[1][0]["confidence"] == "direct", kv[1][0]["date"], kv[0]), reverse=True)


def _corroboration(r: Dict, sigs: List[Dict]) -> Optional[Dict]:
    """A moved champion who speaks, or is named, in this period's signals at the new account. A first name alone
    counts only on a people-change signal that also names their old company ("Ben joined us from Globex")."""
    name = (r.get("Person") or "").strip().lower()
    if not name:
        return None
    first = name.split(" ")[0]
    old = [t for t in company_tokens(r.get("Case study")) if len(t) > 2]
    for s in sorted(sigs, key=lambda s: s["date"], reverse=True):
        text = (s.get("text") or "").lower()
        if (s.get("speaker") or "").strip().lower() == name or name in text:
            return s
        if (s["theme"] == "people_change" and re.search(r"\b" + re.escape(first) + r"\b", text)
                and any(re.search(r"\b" + re.escape(t) + r"\b", text) for t in old)):
            return s
    return None


def _short_quote(text: str, n: int = 90) -> str:
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def tier(score: float) -> str:
    return "High" if score >= 60 else "Medium" if score >= 30 else "Low"


# ---- account context and ICP fit ------------------------------------------------------------------------------------
def employees_number(x) -> Optional[int]:
    """1200, '1,200', '1001 to 5000', '1,001-5,000', '10001+' -> a number (a band gives its lower bound). Else None."""
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)):
        return int(x) if x > 0 else None
    nums = re.findall(r"\d+", normalize_size_band(str(x)).replace(",", ""))
    return int(nums[0]) if nums and int(nums[0]) > 0 else None


def _parse_new_account(text: str) -> Dict[str, str]:
    """The tracker's 'New account' text, 'Not a known customer; 11 to 50 staff; Software Development; acme.com', back
    into size and industry, for rows written before the separate columns existed (an industry may hold '; ')."""
    parts = str(text or "").split("; ")
    if len(parts) < 4 or not parts[0].startswith("Not a known customer"):
        return {}
    size = parts[1][:-len(" staff")] if parts[1].endswith(" staff") else ""
    industry = "; ".join(parts[2:-1])
    return {"size": "" if size == "size unknown" else size, "industry": "" if industry == "industry unknown" else industry}


def firmographics(acc: Dict, rows: List[Dict] = ()) -> Dict:
    """Industry, employees and country: the CRM account first, field by field, then the first champion row that joined
    it (the people data's view of the new company). 'source' says where they came from."""
    out = {"industry": str(acc.get("industry") or "").strip(), "employees": employees_number(acc.get("employees")),
           "employees_text": "", "country": str(acc.get("country") or "").strip(), "source": ""}
    if any((out["industry"], out["employees"], out["country"])):
        out["source"] = "crm"
    if out["employees"]:
        raw = str(acc.get("employees")).replace(",", "").strip()
        exact = not isinstance(acc.get("employees"), str) or raw.isdigit()
        out["employees_text"] = f"{out['employees']:,} employees" if exact else f"{normalize_size_band(acc['employees'])} employees"
    from_rows = False
    for r in rows:
        parsed = _parse_new_account(r.get("New account"))
        ind = str(r.get(ROW_FIRMO_KEYS["industry"]) or parsed.get("industry") or "").strip()
        size = normalize_size_band(r.get(ROW_FIRMO_KEYS["size"]) or parsed.get("size") or "")
        country = str(r.get(ROW_FIRMO_KEYS["country"]) or "").strip()
        if not out["industry"] and ind:
            out["industry"], from_rows = ind, True
        if not out["employees"] and employees_number(size):
            out["employees"], out["employees_text"], from_rows = employees_number(size), f"{size} staff", True
        if not out["country"] and country:
            out["country"], from_rows = country, True
    if from_rows:
        out["source"] = "crm + people data" if out["source"] else "people data"
    return out


def _country(c: str) -> str:
    c = re.sub(r"\s+", " ", str(c or "").strip().lower())
    return _COUNTRY_ALIASES.get(c, c)


def _industry_hit(term: str, industry: str) -> bool:
    t = re.sub(r"\s+", " ", str(term or "").strip().lower().replace("&", "and"))
    i = re.sub(r"\s+", " ", industry.lower().replace("&", "and"))
    return bool(t) and bool(re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", i))


def icp_fit(icp: Optional[Dict], firmo: Dict) -> Dict:
    """Is the account worth working? {"fit": "in" | "out" | "unknown" | "", "why": [reasons it is outside],
    "not_checked": [fields the ICP uses that are unknown]}. A field that is unknown never fails the check; "" = no ICP.
    Employees from a size band use its lower bound ('51 to 200' is 51 against a minimum of 200); the reason shows the band."""
    if not icp:
        return {"fit": "", "why": [], "not_checked": []}
    why, unchecked, checked = [], [], 0
    inc, exc = [t for t in icp.get("industries_include") or [] if t], [t for t in icp.get("industries_exclude") or [] if t]
    ind = str(firmo.get("industry") or "").strip()
    if inc or exc:
        if not ind:
            unchecked.append("no industry")
        else:
            checked += 1
            if any(_industry_hit(t, ind) for t in exc):
                why.append(f"industry {ind} is excluded")
            elif inc and not any(_industry_hit(t, ind) for t in inc):
                why.append(f"industry {ind} is not in your list")
    lo, hi = icp.get("min_employees"), icp.get("max_employees")
    if lo is not None or hi is not None:
        n = firmo.get("employees")
        if not n:
            unchecked.append("no employee count")
        else:
            checked += 1
            size = firmo.get("employees_text") or f"{n:,} employees"      # a band is shown as the band
            if lo is not None and n < lo:
                why.append(f"{size}, under your minimum of {lo:,}")
            elif hi is not None and n > hi:
                why.append(f"{size}, over your maximum of {hi:,}")
    countries = [c for c in icp.get("countries") or [] if c]
    if countries:
        c = str(firmo.get("country") or "").strip()
        if not c:
            unchecked.append("no country")
        else:
            checked += 1
            if _country(c) not in {_country(x) for x in countries}:
                why.append(f"country {c} is not in your list")
    return {"fit": "out" if why else "in" if checked else "unknown", "why": why, "not_checked": unchecked}


def context_line(acc: Dict, owner: str, routed: bool, renewal_days: Optional[int], parent: Optional[Dict], family: bool,
                 firmo: Dict) -> str:
    """'Customer · owner Riley Shaw · renews 2026-12-15 (in 58 days) · open opportunity: Renewal · Software · 1,200
    employees · United States': what a seller needs to know about the account before reading the evidence."""
    if acc.get("crm_id_unread"):
        kind = f"In the CRM ({acc['crm_id_unread']}) but not read this run"
    elif acc.get("not_in_crm"):
        kind = "Not in the CRM yet"
    elif family:
        kind = f"{(acc.get('type') or 'prospect').capitalize()} in the CRM, part of customer {parent['account']}"
    else:
        kind = (acc.get("type") or "type unknown").capitalize()
    parts = [kind, (f"owner {owner}" + (" (routed)" if routed else "")) if owner else "no owner"]
    if acc.get("renewal_date"):
        rd = str(acc["renewal_date"])[:10]
        if renewal_days is None:
            parts.append(f"renewal date {rd}")
        elif renewal_days >= 0:
            parts.append(f"renews {rd} (in {renewal_days} days)")
        else:
            parts.append(f"renewal date {rd} passed {-renewal_days} days ago")
    opp = str(acc.get("open_opportunity") or "").strip()
    stage = str(acc.get("open_opportunity_stage") or "").strip()
    if opp or stage:
        parts.append("open opportunity: " + (opp or stage) + (f" ({stage})" if opp and stage and stage != opp else ""))
    firm = [x for x in (firmo.get("industry"), firmo.get("employees_text"), firmo.get("country")) if x]
    if firm:
        parts.append(" · ".join(firm) + (" (people data)" if firmo.get("source") == "people data" else
                                         " (partly people data)" if firmo.get("source") == "crm + people data" else ""))
    return " · ".join(parts)


def _card(acc: Dict, g: Dict, today: date, parent: Optional[Dict] = None, default_owner: str = "",
          icp: Optional[Dict] = None) -> Optional[Dict]:
    family = parent is not None and bool(g["in"])     # a champion joined a subsidiary of a customer: expansion side
    customer = acc.get("type") == "customer" or family
    open_deal = bool(str(acc.get("open_opportunity") or "").strip())
    sigs = g["signals"]
    since = days_since(acc.get("renewal_date"), today) if acc.get("renewal_date") else None
    renewal_days = -since if since is not None else None
    in_cards = [r for r in g["in"] if r["Decision"] == "Card"]
    in_review = [r for r in g["in"] if r["Decision"] == "Review"]
    comp_risk, pricing = _has(sigs, "competitor", "risk"), _has(sigs, "pricing_friction")
    expansion, timing = _has(sigs, "expansion"), _has(sigs, "timing")
    near_renewal = customer and renewal_days is not None and 0 <= renewal_days <= RENEWAL_WINDOW_DAYS

    parts = [("champion joined", WEIGHTS["champion_in_card"] * bool(in_cards)),
             ("champion joined (needs a check)", WEIGHTS["champion_in_review"] * bool(in_review and not in_cards)),
             ("champion left", WEIGHTS["champion_out"] if customer and g["out"] else 0),
             ("competitor risk", _w(comp_risk, WEIGHTS["competitor_risk_customer" if customer else "competitor_risk_prospect"])),
             ("pricing friction" + (" near renewal" if near_renewal else ""),
              _w(pricing, WEIGHTS["pricing_customer_renewal"] if near_renewal else WEIGHTS["pricing_customer"] if customer else WEIGHTS["pricing_prospect"])),
             ("expansion request", _w(expansion, WEIGHTS["expansion_customer"]) if customer else 0),
             ("timing window", _w(timing, WEIGHTS["timing"]))]
    score = sum(v for _, v in parts)

    facts = {"customer": customer, "prospect": not customer, "near_renewal": bool(near_renewal), "pricing": bool(pricing),
             "competitor_risk": bool(comp_risk), "champion_in": bool(g["in"]), "champion_out": bool(g["out"]),
             "expansion": bool(expansion), "timing": bool(timing), "open_deal": open_deal}
    play = pick_play(facts)
    if play is None:
        return None       # nothing actionable (e.g. only a competitor win, or a prospect's expansion talk)

    lines, links, row_ids, finding_ids = [], [], [], []     # lines = (kinds, text)
    for r in g["in"]:
        seen = _corroboration(r, sigs)
        lines.append(({"champion_in"}, f"Champion joined: {r['Person']} (was {r.get('Title on page', '')} at {r['Case study']}) is now "
                     f"{r.get('Now', '')}, joined {r.get('Move age (days)', '?')} days ago."
                     + (f" {acc['account']} is part of {parent['account']}, a customer." if family else "")
                     + (f" Seen here this week ({label(seen)})." if seen else "")))
        links.append({"label": f"{r['Person']}: champion evidence", "url": r.get("Story", "")})
        row_ids.append(r.get("Row id", ""))
    for r in g["out"]:
        went = r.get("New company", "") + (f" ({r['Joined (blocked)']})" if r.get("Joined (blocked)") else "")
        lines.append(({"champion_out"}, f"Champion left: {r['Person']} ({r.get('Title on page', '')}) moved to "
                      f"{went} {r.get('Move age (days)', '?')} days ago."))
        row_ids.append(r.get("Row id", ""))
    if near_renewal:
        lines.append(({"renewal"}, f"Renewal in {renewal_days} days ({acc['renewal_date']})."))
    if open_deal and play in OPEN_DEAL_PLAYS:
        lines.append(({"open_deal"}, f"Open opportunity in the CRM: {acc['open_opportunity']}."))
    for sid, group in _by_signal(sigs):
        s0 = group[0]
        kinds = {x["theme"] if x["theme"] != "pricing_friction" else "pricing" for x in group}
        themes = " + ".join(THEME_LABELS[t] for t in THEME_ORDER if any(x["theme"] == t for x in group))
        lines.append((kinds, f"{themes}: \"{s0['text']}\" ({s0['speaker']}, {s0['speaker_title']}){TAGS.get(s0['confidence'], '')}"))
        links.append({"label": label(s0), "url": s0["url"]})
        finding_ids.append(sid)
    lead = PLAYS[play].get("lead_with", [])
    rank = lambda kinds: min((lead.index(k) for k in kinds if k in lead), default=len(lead))  # noqa: E731
    lines.sort(key=lambda kl: rank(kl[0]))                  # the signal that triggered the play comes first
    why = [t for _, t in lines]
    headline = " · ".join(_short_quote(t).rstrip(".") for k, t in lines[:2] if rank(k) < len(lead)) or _short_quote(why[0])

    owner = acc.get("owner") or (parent.get("owner", "") if family else "")
    routed = not owner and bool(default_owner)
    owner = owner or default_owner
    who = _who(g, owner)
    checks = []
    for r in in_review:
        seen = _corroboration(r, sigs)
        checks.append(f"{r['Person']}: {r['Reason'].replace('Confirmed champion move; check: ', '')}"
                      + (f" Note: seen at {acc['account']} this week ({label(seen)})." if seen else ""))
    if acc.get("crm_id_unread"):
        checks.append(f"CRM account {acc['crm_id_unread']} was not among the accounts read (filtered out, or another "
                      "region's record): find its owner before outreach.")
    elif acc.get("not_in_crm") and acc.get("looks_like"):
        checks.append(f"Looks like part of {acc['looks_like']}: confirm before treating it as a new logo "
                      f"({domain(acc.get('domain'))} is not in the CRM; {acc['looks_like']} is {acc.get('looks_like_domain', '')}).")
    elif acc.get("not_in_crm"):
        checks.append("Account is not in the CRM yet: create it before outreach.")
    if sigs and all(s["confidence"] != "direct" for s in sigs) and not g["in"] and not g["out"]:
        checks.append("Only reported by our team so far: confirm with the customer.")
    if not who.get("name"):
        checks.append("No one to contact yet: " + who["why"][0].lower() + who["why"][1:] + ".")
    if routed:
        checks.append(f"Routed to {owner}: no account owner yet.")
    elif not owner:
        checks.append("No account owner yet: name one before outreach.")
    firmo = firmographics(acc, g["in"])
    fit = icp_fit(icp, firmo)
    rank_reason = " · ".join(f"{n} {round(v)}" for n, v in sorted(parts, key=lambda p: -p[1]) if v)
    if fit["fit"] == "out":
        score *= ICP_FACTOR
        checks.append("Outside your ICP: " + "; ".join(fit["why"]))
        rank_reason += f" · outside ICP x{ICP_FACTOR}"
    elif fit["fit"] == "in":
        rank_reason += " · in ICP"
    if fit["not_checked"]:
        rank_reason += " · ICP not checked: " + ", ".join(fit["not_checked"])
    status = "Check first" if checks else "Ready"
    cid = acc.get("crm_id") or domain(acc.get("domain")) or re.sub(r"[^a-z0-9]+", "-", acc["account"].lower()).strip("-")
    return {"id": "acct_" + cid, "account": acc["account"], "domain": domain(acc.get("domain")),
            "type": "customer" if customer else acc.get("type", ""), "part_of": parent["account"] if family else "",
            "owner": owner, "owner_routed": routed, "crm_id": acc.get("crm_id", ""), "renewal_days": renewal_days,
            "priority": round(score), "tier": tier(score), "rank_reason": rank_reason.lstrip(" ·"),
            "context": context_line(acc, owner, routed, renewal_days, parent, family, firmo),
            "icp": dict(fit, industry=firmo["industry"], employees=firmo["employees"], country=firmo["country"],
                        source=firmo["source"]),
            "surface_version": SURFACE_VERSION, "plays_version": PLAYS_VERSION, "play": play,
            "play_name": PLAYS[play]["name"], "play_do": PLAYS[play]["do"], "status": status, "checks": checks,
            "headline": headline, "why_now": why, "who": who, "links": links,
            "inputs": {"champion_in": len(g["in"]), "champion_out": len(g["out"]), "competitor_risk": len(comp_risk),
                       "pricing": len(pricing), "expansion": len(expansion), "timing": len(timing),
                       "near_renewal": bool(near_renewal), "open_deal": open_deal},
            "built_from": {"account": acc, "parent": parent if family else None, "row_ids": row_ids, "finding_ids": finding_ids},
            "signals": {t: len(_has(sigs, t)) for t in ("competitor", "pricing_friction", "expansion", "timing")}}


def _who(g: Dict, owner: str) -> Dict:
    if g["in"]:
        r = g["in"][0]
        return {"name": r["Person"], "title": r.get("Now", ""), "why": "Former champion, now here", "linkedin": r.get("LinkedIn", ""),
                "row_id": r.get("Row id", "")}
    if g["still"]:
        r = g["still"][0]
        return {"name": r["Person"], "title": r.get("Title on page", ""), "why": "Champion still at the account", "linkedin": r.get("LinkedIn", "")}
    direct = sorted([s for s in g["signals"] if s["confidence"] == "direct"], key=lambda s: s["date"], reverse=True)
    if direct:
        s = direct[0]
        return {"name": s["speaker"], "title": s["speaker_title"], "why": "Said it on the latest call", "linkedin": ""}
    if g["out"]:
        return {"name": "", "title": "", "why": f"No owner since {g['out'][0]['Person']} left: name one", "linkedin": ""}
    return {"name": "", "title": "", "why": f"No customer contact on record: ask {owner or 'the account owner'}", "linkedin": ""}

"""The champion-tracker decision. Data comes in; code decides; every row carries a plain-words reason.

Inputs per person (see fixtures/):
  seed row     Name, First, Last, Customer, Title on page, Action tag, Evidence (verbatim), Story URL, Exclude reason,
               Slug, optional Row id (e.g. the CRM contact id)
  candidates   every same-name profile from the people-data provider, each with current employer, website,
               title, headline, dates, company size and work history (experience: company, website, start, end, job_type)

Decisions: Card (send to the rep) · Review (confirmed champion move, something to check) · Hold (identity or move
uncertain) · Watch (champion still at the customer) · No action (user, not a champion) · Excluded.
"""
import hashlib
from collections import Counter, defaultdict
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

from . import config as C
from .matching import (company_tokens, days_since, domain, is_open, mentions_company, name_matches, parse_date,
                       same_company, same_customer, words4)

Lookup = Dict[str, object]


def row_id(p: dict) -> str:
    """Stable id for a row: the seed's own id (e.g. CRM contact id) when present, else a hash of source + name.
    Feedback, cards and CRM write-back key on this, never on a person's name."""
    if p.get("Row id"):
        return str(p["Row id"])
    return "r_" + hashlib.sha1(f"{p.get('Story URL', '')}|{p.get('Name', '')}".encode("utf-8")).hexdigest()[:12]


def learn_customer_domains(lookups: Iterable[Lookup]) -> Dict[str, set]:
    """Customer -> domains, learned from profiles whose employer name matches the customer (never guessed by name)."""
    counts: Dict[str, Counter] = defaultdict(Counter)
    for j in lookups:
        cust = j["customer"]
        for c in j.get("candidates") or []:
            if same_company(c.get("current_company_name"), cust) and c.get("current_company_website"):
                counts[cust][domain(c["current_company_website"])] += 1
            for e in c.get("experience") or []:
                if same_company(e.get("company"), cust) and e.get("website"):
                    counts[cust][domain(e["website"])] += 1
    return {k: {d for d in v if d} for k, v in counts.items()}


def run(seed: List[dict], lookups: Dict[Tuple[str, str], Lookup], today: date,
        aliases: Optional[dict] = None, known_customer_domains: Optional[set] = None) -> List[dict]:
    """seed: list of seed rows. lookups: {(Name, Slug): {"customer", "ok", "candidates": [...]}}.
    aliases: {customer: [parent / acquired names]}. known_customer_domains: extra domains of known customers."""
    aliases = aliases or {}
    cdom = learn_customer_domains(lookups.values())
    known_domains = set().union(*cdom.values()) if cdom else set()
    known_domains |= set(known_customer_domains or ())
    customers = {p["Customer"] for p in seed}

    def tied(c: dict, cust: str) -> List[str]:
        doms = cdom.get(cust, set())
        why = []
        if same_customer(c.get("current_company_name"), cust, aliases) or domain(c.get("current_company_website")) in doms:
            why.append("current employer")
        if any(same_customer(e.get("company"), cust, aliases) or domain(e.get("website")) in doms
               for e in c.get("experience") or []):
            why.append("work history")
        if mentions_company(c.get("headline"), cust):
            why.append("headline")
        return why

    def is_known_customer(name: str, website: str) -> bool:
        d = domain(website)   # domain beats name; the name is used only when there is no domain at all
        return (d in known_domains) if d else any(same_company(name, cu) for cu in customers)

    rows = [decide(p, lookups.get((p["Name"], p.get("Slug", ""))), today, aliases, cdom, tied, is_known_customer)
            for p in seed]
    rows.sort(key=lambda r: (C.DECISION_ORDER[r["Decision"]],
                             r["Move age (days)"] if isinstance(r.get("Move age (days)"), int) else 99999,
                             r["Case study"]))
    return rows


def _employer_key(c: dict) -> str:
    return domain(c.get("current_company_website")) or " ".join(company_tokens(c.get("current_company_name")))


def _tenure_start(cur_exp: dict, at_cur: List[dict]) -> Optional[str]:
    """Start of continuous tenure at the current company: promotions extend it (a stint ending within ~2 months of the
    next one's start), a real gap resets it (a returning employee is aged from the current stint)."""
    start = parse_date(cur_exp.get("start"))
    if not start:
        return None
    earlier = sorted((e for e in at_cur if e is not cur_exp and parse_date(e.get("start")) and parse_date(e.get("start")) < start),
                     key=lambda e: parse_date(e.get("start")), reverse=True)
    for e in earlier:
        end = parse_date(e.get("end")) if not is_open(e.get("end")) else start
        if end and (start - end).days <= 62:
            start = parse_date(e.get("start"))
        else:
            break
    return start.isoformat()


def decide(p: dict, j: Optional[Lookup], today: date, aliases: dict, cdom: Dict[str, set], tied, is_known_customer) -> dict:
    cust = p["Customer"]
    tag = (p.get("Action tag") or "").strip().lower()
    level = "Champion" if tag in C.CHAMPION_TAGS else "User"
    base = {"Row id": row_id(p), "Person": p["Name"], "Case study": cust, "Story": p.get("Story URL", ""), "Level": level,
            "Action tag": tag, "Evidence (verbatim)": p.get("Evidence (verbatim)", ""),
            "Title on page": p.get("Title on page", ""), "Rule version": C.RULE_VERSION}

    def out(decision: str, reason: str, **kw) -> dict:
        return {**base, **kw, "Decision": decision, "Reason": reason}

    if p.get("Exclude reason"):
        return out("Excluded", p["Exclude reason"], Level="n/a")
    if tag not in C.CHAMPION_TAGS | C.USER_TAGS:
        return out("Hold", f"Unknown action tag '{p.get('Action tag')}'; fix the extraction before deciding.")
    if not j or not j.get("ok"):
        return out("Hold", "Lookup failed (provider error or timeout); retry.", **{"Same person?": "Hold"})

    # 1. Same person? Only same-name profiles count, and only those tied to the case-study company.
    cands = [c for c in (j.get("candidates") or []) if name_matches(c, p["First"], p["Last"])]
    n = len(cands)
    tied_c = [(c, tied(c, cust)) for c in cands]
    tied_c = [(c, w) for c, w in tied_c if w]
    if len(tied_c) > 1:   # duplicate profiles of one person: keep if they agree on the current employer (domain first)
        keys = {_employer_key(c) for c, _ in tied_c} - {""}
        names = [c.get("current_company_name") for c, _ in tied_c if c.get("current_company_name")]
        agree = len(keys) <= 1 or all(same_company(a, names[0]) for a in names)
        if agree:
            tied_c = [max(tied_c, key=lambda cw: (bool(cw[0].get("current_company_name")), str(cw[0].get("updated_at") or "")))]
        else:
            return out("Hold", f"{len(tied_c)} profiles are tied to {cust} and disagree on where they work now.",
                       **{"Same person?": "Hold", "Candidates (same name)": n})
    if not tied_c:
        if n == 0:
            return out("Hold", "No profile found under this name.", **{"Same person?": "Hold", "Candidates (same name)": 0})
        if n > 1:
            return out("Hold", f"{n} people share this name; none is tied to {cust}.",
                       **{"Same person?": "Hold", "Candidates (same name)": n})
        c, why, identity = cands[0], [], "Likely"
    else:
        (c, why), identity = tied_c[0], "Confirmed"

    exp = c.get("experience") or []
    cur, site = c.get("current_company_name") or "", c.get("current_company_website") or ""
    info = {"Same person?": identity, "Candidates (same name)": n, "Tied by": ", ".join(why),
            "Now": f"{c.get('current_title') or ''} @ {cur}".strip(" @"), "LinkedIn": c.get("linkedin_url", ""),
            "Location": c.get("current_location", "")}

    # 2. Records disagree? A stale profile whose title contradicts the evidence cannot confirm anything.
    upd = days_since(c.get("last_refresh_at") or c.get("updated_at"), today)
    if (identity == "Confirmed" and upd and upd > C.PROFILE_STALE_DAYS and same_company(cur, cust)
            and not (words4(p.get("Title on page")) & words4(c.get("current_title")))):
        return out("Hold", f"Records disagree: evidence says '{p.get('Title on page')}', profile says "
                           f"'{c.get('current_title')}', refreshed {upd} days ago.", **{**info, "Same person?": "Hold"})

    # 3. Moved? (never from fields that can lag a move, such as an org/email domain)
    doms = cdom.get(cust, set())
    cust_jobs = [e for e in exp if same_customer(e.get("company"), cust, aliases) or domain(e.get("website")) in doms]
    if not cur:
        return out("Hold", "No current employer on file.", **info, **{"Moved?": "Unknown"})
    if same_customer(cur, cust, aliases) or domain(site) in doms:
        return out("Watch" if level == "Champion" else "No action", f"Still at {cust}.", **info,
                   **{"Moved?": "No", "Play": C.PLAYS["P3"] if level == "Champion" else ""})
    open_cust = any(is_open(e.get("end")) for e in cust_jobs)
    at_cur = [e for e in exp if (domain(site) and domain(e.get("website")) == domain(site)) or same_company(e.get("company"), cur)]
    cur_exp = (next((e for e in at_cur if is_open(e.get("end"))), None) or next(iter(at_cur), None)
               or next((e for e in exp if e.get("is_current")), {}))
    if not is_open(cur_exp.get("end")):
        return out("Hold", f"Profile's latest job ({cur}) ended {str(cur_exp['end'])[:10]}; current employer unknown.",
                   **info, **{"Moved?": "Unknown"})
    if cur_exp.get("start") and str(cur_exp["start"])[:7] in {str(e.get("start"))[:7] for e in cust_jobs if e.get("start")}:
        return out("Hold", f"'{cur}' job starts the same month as the {cust} job ({str(cur_exp['start'])[:7]}): "
                           "looks like a rename or acquisition, not a move.", **info, **{"Moved?": "Unclear"})
    side = (C.SIDE_ROLE.search(c.get("current_title") or "") or C.SELF_EMPLOYED.search(cur)
            or cur_exp.get("job_type") in C.SIDE_JOB_TYPES)
    if side and open_cust:
        return out("Watch" if level == "Champion" else "No action",
                   f"New 'current' role is a side seat ({c.get('current_title')}); {cust} job still open. Not a move.",
                   **info, **{"Moved?": "No", "Play": C.PLAYS["P3"] if level == "Champion" else ""})
    if side:
        return out("Hold", f"New 'current' role is a side seat ({c.get('current_title')}); real current employer unknown.",
                   **info, **{"Moved?": "Unclear"})

    # 4. The move: age counts from joining the new company (the "why now"), never from leaving the old one.
    left = max((str(e.get("end"))[:10] for e in cust_jobs if not is_open(e.get("end"))), default=None)
    joined = _tenure_start(cur_exp, at_cur)
    mdays = days_since(joined or c.get("latest_company_change_at") or c.get("role_start_date")
                       or c.get("current_role_start_date"), today)
    existing = is_known_customer(cur, site)
    size = c.get("employee_count_range") or ""
    account = (f"Existing customer ({cur} · {domain(site) or 'no domain'})" if existing else
               f"Not a known customer; {size or 'size unknown'} staff; {c.get('main_industry') or 'industry unknown'}; "
               f"{domain(site) or 'no domain'}")
    info.update({"Moved?": "Yes", "Left on": left or "", "Joined new company": joined or "",
                 "Move age (days)": "" if mdays is None else mdays, "New account": account})
    if identity == "Likely":
        return out("Hold", f"Moved, but identity is only Likely: the only {p['Name']} on file, no record ties them to {cust}.", **info)
    if level != "Champion":
        return out("No action", "Moved, but the evidence only shows them as a user.", **info)
    if not (p.get("Evidence (verbatim)") or "").strip() or not (p.get("Story URL") or "").strip():
        return out("Hold", "Champion move, but the evidence sentence or its source link is missing.", **info)

    # 5. Stop rule: what still needs a human before a card goes to a seller.
    checks = []
    if open_cust:
        checks.append(f"{cust} job still open on the profile")
    if mdays is None:
        checks.append("move date unknown")
    elif mdays > C.RECENT_DAYS:
        checks.append(f"move is {mdays} days old (over {C.RECENT_DAYS})")
    if not existing and not size:
        checks.append("new company size unknown")
    elif size in C.SMALL_COMPANY_BANDS:
        checks.append(f"new company is small ({size} staff)")
    if not domain(site):
        checks.append("new company has no domain on file")
    play = C.PLAYS["P2"] if existing else C.PLAYS["P1"]
    if checks:
        return out("Review", "Confirmed champion move; check: " + "; ".join(checks) + ".", **info, Play=play)
    return out("Card", "Confirmed champion, recent move, account resolved.", **info, Play=play)

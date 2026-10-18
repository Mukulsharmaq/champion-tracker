"""The champion-tracker decision. Data comes in; code decides; every row carries a plain-words reason.

Inputs per person (see fixtures/):
  seed row     Name, First, Last, Customer, Title on page, Action tag, Evidence (verbatim), Story URL, Exclude reason,
               Slug; optional: Row id (e.g. the CRM contact id), Name variants (maiden / married names, ';' or ','),
               Evidence date (YYYY-MM-DD or YYYY: when the evidence was true), Last activity (last product activity at
               the customer, from the warehouse), Customer domain (the customer's own website, e.g. from the CRM)
  candidates   every same-name profile from the people-data provider, each with current employer, website,
               title, headline, dates, company size and work history (experience: company, website, start, end, job_type)
  lookup       {"customer", "ok", "candidates", optional "capped": the same-name search returned its full depth}

Decisions: Card (send to the rep) · Review (confirmed champion move, something to check) · Hold (identity or move
uncertain) · Watch (champion still at the customer) · No action (user, not a champion) · Excluded.
Rows for a move also carry the new account's ICP fields: New company size (normalised band), New company industry,
New company country (empty when the profile does not say).
"""
import re
import hashlib
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from . import config as C
from .matching import (company_tokens, date_range, days_since, domain, domain_is_named_for, is_open, is_self_employed,
                       is_side_role, mentions_company, normalize_profile_url, normalize_size_band, parse_date,
                       same_company, same_customer, seed_name_matches, words4)

Lookup = Dict[str, object]


def candidate_key(c: dict) -> str:
    """How a person who checked by hand points at one profile: its URL (normalised by normalize_profile_url, so the
    provider's raw spelling and a pasted link are one key), else name @ current company."""
    url = (c.get("linkedin_url") or "").strip()
    return normalize_profile_url(url) if url else f"{c.get('full_name', '')} @ {c.get('current_company_name') or ''}"


def normalize_override(o: Optional[dict]) -> dict:
    """An identity override with every profile key normalised (normalize_profile_url), so a reject stored as a raw
    provider URL and a confirm made from a pasted link compare equal."""
    o = dict(o or {})
    o["reject"] = [normalize_profile_url(u) for u in (o.get("reject") or []) if u]
    for k in ("confirm", "confirm_url"):
        if o.get(k):
            o[k] = normalize_profile_url(o[k])
    return o


def row_id(p: dict) -> str:
    """Stable id for a row: the seed's own id (e.g. CRM contact id) when present, else a hash of source + name.
    Feedback, cards and CRM write-back key on this, never on a person's name."""
    if p.get("Row id"):
        return str(p["Row id"])
    return "r_" + hashlib.sha1(f"{p.get('Story URL', '')}|{p.get('Name', '')}".encode("utf-8")).hexdigest()[:12]


def learn_customer_domains(lookups: Iterable[Lookup], aliases: Optional[dict] = None,
                           own_domains: Optional[Dict[str, set]] = None, known_domains: Optional[set] = None) -> Dict[str, set]:
    """Customer -> domains, learned only from jobs whose company name IS the customer (equal after removing legal
    suffixes, or a listed alias). Never from a looser match: a namesake at 'Acme Realty' must not teach acmerealty.com
    as Acme's domain, or a champion who later joins Acme Realty reads as 'existing customer'.
    A website merely named for the customer (ryder.com for 'Ryder System, Inc.') counts only when it is the customer's
    own known domain (own_domains: the CRM website; known_domains: known customer domains), or, when the customer has
    no own domain and no job under its exact name has a website, as the only evidence there is. So a champion who moves
    from Ramp (ramp.com) to Ramp Network (ramp.network) never teaches ramp.network as Ramp's domain.
    The customer's own domains are always included."""
    aliases, own, known = aliases or {}, own_domains or {}, set(known_domains or ())
    exact: Dict[str, Counter] = defaultdict(Counter)
    named: Dict[str, Counter] = defaultdict(Counter)
    for j in lookups:
        cust = j["customer"]
        names = [cust] + list(aliases.get(cust, []))
        jobs = [(c.get("current_company_name"), c.get("current_company_website")) for c in j.get("candidates") or []]
        jobs += [(e.get("company"), e.get("website")) for c in j.get("candidates") or [] for e in c.get("experience") or []]
        for name, site in jobs:
            d = domain(site)
            if not d:
                continue
            if same_customer(name, cust, aliases):
                exact[cust][d] += 1
            elif any(domain_is_named_for(site, n) for n in names):
                named[cust][d] += 1
    out: Dict[str, set] = defaultdict(set)
    for cust, doms in own.items():
        out[cust] |= {domain(d) for d in doms if domain(d)}
    for cust, c in exact.items():
        out[cust] |= set(c)
    for cust, c in named.items():
        mine = out.get(cust, set()) | known
        fallback = not own.get(cust) and not exact.get(cust)
        out[cust] |= {d for d in c if d in mine or fallback}
    return {k: v for k, v in out.items() if v}


def _fits_evidence(e: dict, ev) -> bool:
    """A job at the customer ties a profile to the champion only if it overlaps the evidence date (with a grace year
    after the job ended, since stories are published late). Without an evidence date every job counts.
    A namesake who interned at the customer in 2012-14 must not confirm a 2023 champion."""
    if not ev:
        return True
    ev_first, ev_last, _ = ev
    start = date_range(e.get("start"))
    if start and start[0] > ev_last:
        return False
    if not is_open(e.get("end")):
        end = date_range(e.get("end"))
        if end and end[1] + timedelta(days=C.EVIDENCE_GRACE_DAYS) < ev_first:
            return False
    return True


def run(seed: List[dict], lookups: Dict[Tuple[str, str], Lookup], today: date,
        aliases: Optional[dict] = None, known_customer_domains: Optional[set] = None,
        overrides: Optional[Dict[str, dict]] = None) -> List[dict]:
    """seed: list of seed rows. lookups: {(Name, Slug): {"customer", "ok", "candidates": [...]}}.
    aliases: {customer: [parent / acquired names]}. known_customer_domains: extra domains of known customers.
    overrides: {row id: {"confirm": profile url} or {"reject": [profile urls]}}, from people who checked by hand
    (the review queue's "This is them" / "None of these", a seller's "Wrong person"). Profile URLs are compared after
    normalize_profile_url, whatever spelling the caller stored."""
    overrides = {k: normalize_override(v) for k, v in (overrides or {}).items()}
    aliases = aliases or {}
    own: Dict[str, set] = defaultdict(set)
    for p in seed:
        if domain(p.get("Customer domain")):
            own[p["Customer"]].add(domain(p["Customer domain"]))
    cdom = learn_customer_domains(lookups.values(), aliases, own, known_customer_domains)
    known_domains = set().union(*cdom.values()) if cdom else set()
    known_domains |= set(known_customer_domains or ())
    customers = {p["Customer"] for p in seed}

    def tied(c: dict, cust: str, evidence=None) -> List[str]:
        doms = cdom.get(cust, set())
        why = []
        if same_customer(c.get("current_company_name"), cust, aliases) or domain(c.get("current_company_website")) in doms:
            why.append("current employer")
        if any((same_customer(e.get("company"), cust, aliases) or domain(e.get("website")) in doms) and _fits_evidence(e, evidence)
               for e in c.get("experience") or []):
            why.append("work history")
        if mentions_company(c.get("headline"), cust):
            why.append("headline")
        return why

    def is_known_customer(name: str, website: str) -> bool:
        d = domain(website)   # domain beats name; the name is used only when there is no domain at all
        return (d in known_domains) if d else any(same_company(name, cu) for cu in customers)

    rows = [decide(p, lookups.get((p["Name"], p.get("Slug", ""))), today, aliases, cdom, tied, is_known_customer,
                   overrides.get(row_id(p)))
            for p in seed]
    rows.sort(key=lambda r: (C.DECISION_ORDER[r["Decision"]],
                             r["Move age (days)"] if isinstance(r.get("Move age (days)"), int) else 99999,
                             r["Case study"]))
    return rows


def icp_fields(c: dict) -> dict:
    """The new account's ICP fields, from the profile when it has them (empty otherwise): size as one normalised band,
    industry, and country (the provider's country field, else the last part of the person's location)."""
    loc = str(c.get("current_location") or "").strip()
    country = (c.get("company_country") or c.get("country") or (loc.split(",")[-1] if loc else "") or "")
    return {"New company size": normalize_size_band(c.get("employee_count_range")),
            "New company industry": str(c.get("main_industry") or c.get("industry") or "").strip(),
            "New company country": str(country).strip()}


def _member_id(url) -> Optional[str]:
    """The id LinkedIn appends to a vanity slug ('/in/rob-chapman-4a2548232'): kept when the slug is renamed."""
    m = re.search(r"/in/[^/?#]*?-([0-9a-f]{7,})/?(?:[?#].*)?$", str(url or "").lower())
    return m.group(1) if m else None


def _employer_key(c: dict) -> str:
    return domain(c.get("current_company_website")) or " ".join(company_tokens(c.get("current_company_name")))


def _tenure_start(cur_exp: dict, at_cur: List[dict]) -> Optional[str]:
    """Start of continuous tenure at the current company: promotions extend it (a stint ending within ~2 months of the
    next one's start), a real gap resets it (a returning employee is aged from the current stint).
    Returned as written ('2026-07' stays month-only), so the move age never pretends to know the day."""
    start, first = parse_date(cur_exp.get("start")), cur_exp
    if not start:
        return None
    earlier = sorted((e for e in at_cur if e is not cur_exp and parse_date(e.get("start")) and parse_date(e.get("start")) < start),
                     key=lambda e: parse_date(e.get("start")), reverse=True)
    for e in earlier:
        end = parse_date(e.get("end")) if not is_open(e.get("end")) else start
        if end and (start - end).days <= 62:
            start, first = parse_date(e.get("start")), e
        else:
            break
    return str(first.get("start")).strip()[:10]


def decide(p: dict, j: Optional[Lookup], today: date, aliases: dict, cdom: Dict[str, set], tied, is_known_customer,
           override: Optional[dict] = None) -> dict:
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
    cands = [c for c in (j.get("candidates") or []) if seed_name_matches(c, p)]
    evidence = date_range(p.get("Evidence date"))
    override = normalize_override(override)
    rejected = set(override.get("reject") or [])
    if override.get("confirm") and override["confirm"] in rejected and any(candidate_key(c) == override["confirm"] for c in cands):
        return out("Hold", "Reviewer decisions disagree: the same profile is both confirmed and rejected for this row; "
                           "check by hand.", **{"Same person?": "Hold", "Candidates (same name)": len(cands)})
    cands = [c for c in cands if candidate_key(c) not in rejected]
    n = len(cands)
    confirmed = [c for c in cands if override.get("confirm") and candidate_key(c) == override["confirm"]]
    tied_c = ([(confirmed[0], ["confirmed by a reviewer"])] if confirmed else
              [(c, w) for c, w in ((c, tied(c, cust, evidence)) for c in cands) if w])
    if len(tied_c) > 1:   # duplicate profiles of one person: keep if they agree on the current employer (domain first)
        keys = {_employer_key(c) for c, _ in tied_c} - {""}
        names = [c.get("current_company_name") for c, _ in tied_c if c.get("current_company_name")]
        ids = {_member_id(c.get("linkedin_url")) for c, _ in tied_c}
        same_member = len(ids) == 1 and None not in ids   # one LinkedIn member under an old and a new URL slug
        agree = same_member or len(keys) <= 1 or all(same_company(a, names[0]) for a in names)
        if same_member:   # the freshest copy wins: the old slug's copy stopped being refreshed
            tied_c = [max(tied_c, key=lambda cw: str(cw[0].get("last_refresh_at") or cw[0].get("updated_at") or ""))]
        if agree:
            tied_c = [max(tied_c, key=lambda cw: (bool(cw[0].get("current_company_name")), str(cw[0].get("updated_at") or "")))]
        else:
            return out("Hold", f"{len(tied_c)} profiles are tied to {cust} and disagree on where they work now.",
                       **{"Same person?": "Hold", "Candidates (same name)": n})
    if not tied_c:
        if j.get("capped") and j.get("full_search"):   # every same-name profile was read: nothing is further down
            return out("Hold", f"A full search of {j.get('searched', n)} same-name profiles found none tied to {cust}: "
                               "check by hand, or paste the right profile.",
                       **{"Same person?": "Hold", "Candidates (same name)": n})
        if j.get("capped"):   # the search was cut off: "N share this name" would be a false statement of fact
            depth = j.get("depth") or C.SEARCH_DEPTH
            found = (f"{n} profiles with this name came back and none of those is tied to {cust}" if n else
                     f"none of those has this exact name or is tied to {cust}")
            return out("Hold", f"Search hit the {depth}-result cap: {found}; the right profile may be further down "
                               "and the full same-name pull did not run. Re-run the lookup, or check by hand.",
                       **{"Same person?": "Hold", "Candidates (same name)": n})
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
    at_cust_now = same_customer(cur, cust, aliases) or domain(site) in cdom.get(cust, set())
    if (identity == "Confirmed" and upd and upd > C.PROFILE_STALE_DAYS and at_cust_now
            and not (words4(p.get("Title on page")) & words4(c.get("current_title")))):
        return out("Hold", f"Records disagree: evidence says '{p.get('Title on page')}', profile says "
                           f"'{c.get('current_title')}', refreshed {upd} days ago.", **{**info, "Same person?": "Hold"})

    # 3. Moved? (never from fields that can lag a move, such as an org/email domain)
    doms = cdom.get(cust, set())
    cust_jobs = [e for e in exp if same_customer(e.get("company"), cust, aliases) or domain(e.get("website")) in doms]
    if not cur:
        return out("Hold", "No current employer on file.", **info, **{"Moved?": "Unknown"})
    if at_cust_now and cust_jobs and not any(is_open(e.get("end")) for e in cust_jobs):
        ended = max(str(e.get("end"))[:10] for e in cust_jobs if e.get("end"))
        return out("Hold", f"Profile still names {cust} as the current employer, but the {cust} job ended {ended}; "
                           "current employer unknown.", **info, **{"Moved?": "Unknown"})
    if at_cust_now:
        info.update({"New company": cur, "New domain": domain(site)})
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
    side = is_side_role(c.get("current_title"), cur_exp.get("job_type")) or is_self_employed(cur, c.get("current_title"))
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
    start_src = (joined or c.get("latest_company_change_at") or c.get("role_start_date")
                 or c.get("current_role_start_date"))
    rng = date_range(start_src)
    future = bool(rng and rng[0] > today)
    # A month-only start ('2026-07') is aged from its most recent possible day, never from the 1st.
    mdays = None if not rng or future else (today - min(rng[1], today)).days
    mdays_max = None if not rng or future else (today - rng[0]).days
    existing = is_known_customer(cur, site)
    size = normalize_size_band(c.get("employee_count_range"))
    account = (f"Existing customer ({cur} · {domain(site) or 'no domain'})" if existing else
               f"Not a known customer; {size or 'size unknown'} staff; {c.get('main_industry') or 'industry unknown'}; "
               f"{domain(site) or 'no domain'}")
    info.update({"Moved?": "Yes", "New company": cur, "New domain": domain(site), "Left on": left or "", "Joined new company": joined or "",
                 "Move age (days)": "" if mdays is None else mdays, "New account": account, **icp_fields(c)})
    if identity == "Likely":
        return out("Hold", f"Moved, but identity is only Likely: the only {p['Name']} on file, no record ties them to {cust}.", **info)
    if level != "Champion":
        return out("No action", "Moved, but the evidence only shows them as a user.", **info)
    if not (p.get("Evidence (verbatim)") or "").strip() or not (p.get("Story URL") or "").strip():
        return out("Hold", "Champion move, but the evidence sentence or its source link is missing.", **info)
    if future:
        return out("Hold", f"New job's start date is in the future ({start_src} at {cur}); the profile may be wrong or "
                           "the move has not happened yet.", **info)
    act, left_rng = date_range(p.get("Last activity")), date_range(left)
    grace = timedelta(days=C.ACTIVITY_AFTER_LEAVING_GRACE_DAYS)       # a last expense report after the last day is normal
    if act and left_rng and not open_cust and act[0] > left_rng[1] + grace:   # used the product long after they "left"
        return out("Hold", f"Still active in the product at {cust} on {p.get('Last activity')}, more than "
                           f"{C.ACTIVITY_AFTER_LEAVING_GRACE_DAYS} days after the profile says they left ({left}); "
                           "records disagree.", **info)

    # 5. Stop rule: what still needs a human before a card goes to a seller.
    checks = []
    if open_cust:
        checks.append(f"{cust} job still open on the profile")
    if mdays is None:
        checks.append("move date unknown")
    elif mdays > C.RECENT_DAYS:
        checks.append(f"move is {mdays} days old (over {C.RECENT_DAYS})")
    elif mdays_max > C.RECENT_DAYS:   # fresh only if the start fell late in its month (or year)
        checks.append(f"start date known only to the {rng[2]} ({start_src}): the move is {mdays} to {mdays_max} days "
                      f"old, so it may be over {C.RECENT_DAYS}")
    if not existing and not size:
        checks.append("new company size unknown")
    elif size in C.SMALL_COMPANY_BANDS:
        checks.append(f"new company is small ({size} staff)")
    if not domain(site):
        checks.append("new company has no domain on file")
    if C.FRACTIONAL_OR_INTERIM.search(c.get("current_title") or ""):
        checks.append("fractional or interim role")
    play = C.PLAYS["P2"] if existing else C.PLAYS["P1"]
    if checks:
        return out("Review", "Confirmed champion move; check: " + "; ".join(checks) + ".", **info, Play=play)
    return out("Card", "Confirmed champion, recent move, account resolved.", **info, Play=play)

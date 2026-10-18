"""Name, company and date matching. Each rule here exists because real data broke without it."""
import re
import unicodedata
from datetime import date
from typing import List, Optional

from .config import GENERIC_COMPANY_WORDS


def company_tokens(name: Optional[str]) -> List[str]:
    t = re.findall(r"[a-z0-9]+", (name or "").lower().replace("&", " and "))
    return [x for x in t if x not in GENERIC_COMPANY_WORDS]


def same_company(a: Optional[str], b: Optional[str]) -> bool:
    """Whole-name comparison after dropping legal/generic words.
    'Acme' ~ 'Acme Robotics' (a 4+ letter leading name), but never 'Ace' ~ 'AceRealty'."""
    ta, tb = company_tokens(a), company_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(small[0]) >= 4 and big[:len(small)] == small


def same_customer(name: Optional[str], customer: str, aliases: Optional[dict] = None) -> bool:
    """Same company, counting listed parent / acquired-company names (e.g. an acquired brand's old name)."""
    aliases = aliases or {}
    return same_company(name, customer) or any(same_company(name, a) for a in aliases.get(customer, []))


def domain(url: Optional[str]) -> str:
    u = (url or "").lower().strip()
    u = re.sub(r"^https?://", "", u).split("/")[0]
    return u[4:] if u.startswith("www.") else u


def parse_date(d) -> Optional[date]:
    """YYYY-MM-DD, YYYY-MM or YYYY (day/month default to 1). Unknown, placeholder or malformed -> None."""
    s = str(d or "").strip()[:10]
    if not s or s.startswith("1970"):
        return None
    m = re.match(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$", s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1))
    except ValueError:
        return None


def days_since(d, today: date) -> Optional[int]:
    """Whole days from a date to today; None when the date is unknown or malformed (never crashes a run)."""
    p = parse_date(d)
    return (today - p).days if p else None


def is_open(end) -> bool:
    """An experience entry is open (still current) when its end is empty or says present/current."""
    return str(end or "").strip().lower() in ("", "none", "null", "present", "current")


def _plain(x: Optional[str]) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", x or "") if not unicodedata.combining(ch)).lower()


def name_matches(candidate: dict, first: str, last: str) -> bool:
    """Exact last name (accent/case-insensitive, whole token) + first name equal or a 3+ letter prefix either way.
    Rob ~ Robert passes; a substring hit like 'Jo de Vries' -> 'Joanna Devrieson' fails."""
    full_name = candidate.get("full_name") or f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}"
    full = re.findall(r"[a-z]+", _plain(full_name))
    f, l = re.findall(r"[a-z]+", _plain(first)), re.findall(r"[a-z]+", _plain(last))
    if not full or not f or not l:
        return False
    if l[-1] not in full[1:] and full[-1] != l[-1]:
        return False
    a, b = full[0], f[0]
    return a == b or (min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)))


def words4(text: Optional[str]) -> set:
    return set(re.findall(r"[a-z]{4,}", (text or "").lower()))


def mentions_company(headline: Optional[str], customer: str) -> bool:
    """Headline names the company: the whole name as a phrase ('Head of Ops | Acme Robotics'), or its leading name
    right after a marker ('ex-Acme', 'formerly Acme', '@Acme', 'at Acme'). A single ordinary word never ties
    ('Head of Travel at Contoso' must not tie to 'Travel Leaders Group')."""
    toks = company_tokens(customer)
    h = (headline or "").lower()
    if not toks or not h:
        return False
    if len(toks) >= 2 and re.search(r"\b" + r"\W+".join(map(re.escape, toks)) + r"\b", h):
        return True
    lead = toks[0]
    marker = r"(?:\bex[- ]|\bformer(?:ly)?\s+(?:at\s+)?|\bpreviously\s+(?:at\s+)?|@\s*" + (r"|\bat\s+" if len(toks) == 1 else "") + r")"
    return len(lead) >= 3 and re.search(marker + re.escape(lead) + r"\b", h) is not None

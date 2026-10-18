"""Name, company and date matching. Each rule here exists because real data broke without it."""
import calendar
import re
import unicodedata
from datetime import date
from typing import Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

from .config import (ADVISOR, ADVISOR_BOARD_CONTEXT, COMPANY_DESCRIPTORS, GENERIC_HEADS, GEO_REGIONS, GEO_WORDS,
                     LEGAL_SUFFIXES, NAME_CONNECTORS, SELF_EMPLOYED_COMPANY, SELF_EMPLOYED_TITLE, SHORT_HEAD_LEN,
                     SIDE_ADVISOR_WORDS, SIDE_JOB_TYPES, SIDE_ROLE)

# Letters NFKD does not decompose: 'Søren' = 'Soren', 'Łukasz' = 'Lukasz', 'Strauß' = 'Strauss'.
_FOLD = str.maketrans({"ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ß": "ss", "æ": "ae", "Æ": "AE",
                       "œ": "oe", "Œ": "OE", "þ": "th", "Þ": "Th", "ð": "d", "Ð": "D", "ı": "i"})


def _fold(x: Optional[str]) -> str:
    """Accents removed (NFKD, then combining marks dropped), case kept: 'Pérez' -> 'Perez'."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", (x or "").translate(_FOLD)) if not unicodedata.combining(ch))


def _plain(x: Optional[str]) -> str:
    return _fold(x).lower()


# ---- companies ----------------------------------------------------------------------------------------------------
def _strip_legal(t: List[str]) -> List[str]:
    while len(t) > 1 and (t[-1] in LEGAL_SUFFIXES or t[-1] in NAME_CONNECTORS):
        t = t[:-1]
    return t


def _geo_tail(t: List[str]) -> int:
    """Where a trailing run of geography words starts (len(t) when there is none, or when it follows 'of')."""
    k = len(t)
    while k > 1 and t[k - 1] in GEO_WORDS:
        k -= 1
    return len(t) if k < len(t) and t[k - 1] in NAME_CONNECTORS else k


def _weak_head(t: List[str]) -> bool:
    """One word that cannot name a company alone: generic ('air', 'union', 'virgin') or 5 letters or fewer."""
    return len(t) == 1 and (len(t[0]) <= SHORT_HEAD_LEN or t[0] in GENERIC_HEADS)


def company_tokens(name: Optional[str]) -> List[str]:
    """The company's own name as tokens: punctuation, a leading 'the', a web suffix ('Pendo.io'), and trailing legal
    suffixes and geography removed. Geography counts as generic only after the company's own name and never after
    'of' ('Contoso North America Inc' -> contoso, 'Bank of America' -> bank of america, 'US Bank' -> us bank,
    'Hertha BSC GmbH & Co. KGaA' -> hertha bsc). Geography is kept when removing it would leave one generic or short
    word: 'Air Canada' -> air canada (not air, which would equal Air India), 'Union Pacific' -> union pacific."""
    s = re.sub(r"\.(?:com|io|ai|co|net|org|app)\s*$", "", _plain(name).strip())
    t = re.findall(r"[a-z0-9]+", s.replace("&", " and "))
    if len(t) > 1 and t[0] == "the":
        t = t[1:]
    t = _strip_legal(t)
    while True:
        k = _geo_tail(t)
        if k == len(t):
            return t
        rest = _strip_legal(t[:k])
        if _weak_head(rest):
            return t
        t = rest


def _region(tail: List[str]) -> str:
    g = " ".join(tail)
    return GEO_REGIONS.get(g, g)


def same_company(a: Optional[str], b: Optional[str]) -> bool:
    """Same company when the names are equal after company_tokens (spacing aside: 'SurveyMonkey' == 'Survey Monkey').
    A shorter name matches a longer one only when the extra words are legal suffixes or trailing geography (removed
    above), or generic descriptors after a name of two or more words ('Luck Grove' == 'Luck Grove Telecom').
    So 'Delta' != 'Delta Dental', 'American Express' != 'American Express GBT', 'Travel Leaders Group' !=
    'Travel Leaders Network', 'Bank of America' != 'US Bank', 'Air Canada' != 'Air India', 'Union Pacific' != 'Union'.
    A short head keeps its geography, which then has to name the same region: 'Acme USA' == 'Acme North America',
    'Canon USA' != 'Canon Europe'; a generic head never matches across geography ('Virgin America' != 'Virgin USA').
    Parent / acquired names go in the aliases."""
    ta, tb = company_tokens(a), company_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb or "".join(ta) == "".join(tb):
        return True
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(small) >= 2 and big[:len(small)] == small and all(x in COMPANY_DESCRIPTORS for x in big[len(small):]):
        return True
    ka, kb = _geo_tail(ta), _geo_tail(tb)
    if 0 < ka < len(ta) and 0 < kb < len(tb) and ta[:ka] == tb[:kb] and not (ka == 1 and ta[0] in GENERIC_HEADS):
        return _region(ta[ka:]) == _region(tb[kb:])
    return False


def domain_is_named_for(url: Optional[str], name: Optional[str]) -> bool:
    """The website's own name IS the company's whole name: ryder.com for 'Ryder', pendo.io for 'Pendo', nulo.com for
    'Nulo'. Domain beats name, so this ties 'Ryder System, Inc.' to Ryder without any loose name rule;
    acmerealty.com is never Acme's."""
    label = re.sub(r"[^a-z0-9]", "", domain(url).split(".")[0]) if domain(url) else ""
    return bool(label) and label == "".join(company_tokens(name))


def same_customer(name: Optional[str], customer: str, aliases: Optional[dict] = None) -> bool:
    """Same company, counting listed parent / acquired-company names (e.g. an acquired brand's old name)."""
    aliases = aliases or {}
    return same_company(name, customer) or any(same_company(name, a) for a in aliases.get(customer, []))


def domain(url: Optional[str]) -> str:
    u = (url or "").lower().strip()
    u = re.sub(r"^https?://", "", u).split("/")[0]
    return u[4:] if u.startswith("www.") else u


def normalize_profile_url(url: Optional[str]) -> str:
    """One spelling per profile, so a reject stored as the provider's raw URL and a pasted link compare equal.
    linkedin.com/in/ URLs (any subdomain, scheme or none, query, fragment, trailing slash, slug case or %-escapes)
    -> https://www.linkedin.com/in/<lowercased, unquoted slug>. Other URLs: scheme and host lowercased, query,
    fragment and trailing slash dropped. Anything that is not a URL (a 'name @ company' key) is returned trimmed."""
    s = (url or "").strip()
    if not s:
        return ""
    if not re.match(r"^[a-z][a-z0-9+.-]*://", s, re.I):
        if not re.match(r"^(?:www\.)?(?:[a-z0-9-]+\.)*linkedin\.com/|^www\.", s, re.I):
            return s
        s = "https://" + s
    try:
        parts = urlsplit(s)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return s
    if not host:
        return s
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        m = re.match(r"^/in/([^/?#]+)", parts.path or "")
        slug = unquote(m.group(1)).strip().lower() if m else ""
        if slug:
            return f"https://www.linkedin.com/in/{slug}"
    netloc = host + (f":{port}" if port else "")
    return f"{parts.scheme.lower()}://{netloc}{(parts.path or '').rstrip('/')}"


def normalize_size_band(size: Optional[str]) -> str:
    """One spelling for a company-size band, so the small-company check sees every provider's format:
    '11-50', '11 - 50 employees', '11 to 50' -> '11 to 50'; '10,001+' -> '10001+'. Anything else is returned trimmed."""
    s = str(size or "").strip()
    nums = re.findall(r"\d+", s.replace(",", ""))
    if len(nums) == 2 and re.search(r"\d\s*(?:-|–|to)\s*\d", s.replace(",", ""), re.I):
        return f"{int(nums[0])} to {int(nums[1])}"
    if len(nums) == 1 and "+" in s:
        return f"{int(nums[0])}+"
    return s


# ---- side seats -----------------------------------------------------------------------------------------------------
def is_side_job_type(job_type: Optional[str]) -> bool:
    """The profile's own job type says side seat: freelance, volunteer, advisor, board."""
    return re.sub(r"[\s_-]+", " ", str(job_type or "").strip().lower()) in SIDE_JOB_TYPES


def is_side_role(title: Optional[str], job_type: Optional[str] = None) -> bool:
    """A board or advisor seat, judged on the title (and the job type when the profile has one).
    Side seats: 'Chair, Regional Board', 'Advisory Board Member', 'Board Advisor', 'Advisor', 'Strategic Advisor',
    'Advisor to the CEO', 'Investor & Advisor', and trustee / volunteer / mentor / ambassador as the role itself.
    Jobs: 'Director, Advisory Services', 'Financial Advisor', 'Senior Advisor', 'Principal Advisor',
    'Volunteer Coordinator Manager', 'Brand Ambassador Program Manager', 'Trustee Services Officer'."""
    t = title or ""
    if is_side_job_type(job_type) or SIDE_ROLE.search(t) or ADVISOR_BOARD_CONTEXT.search(t):
        return True
    return any((m.group(1) or "").strip().lower() in SIDE_ADVISOR_WORDS | {""} for m in ADVISOR.finditer(t))


def is_self_employed(company: Optional[str], title: Optional[str] = "") -> bool:
    """The company or the title IS self-employment ('Self-employed', 'Freelance', 'Independent consultant'),
    never a company that merely contains the word ('Independent Bank')."""
    return bool(SELF_EMPLOYED_COMPANY.fullmatch(company or "") or SELF_EMPLOYED_TITLE.match(title or ""))


# ---- dates ----------------------------------------------------------------------------------------------------------
_DATE = re.compile(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$")


def date_range(d) -> Optional[Tuple[date, date, str]]:
    """(earliest possible day, latest possible day, precision) for YYYY-MM-DD, YYYY-MM or YYYY.
    '2026-07' is any day of July: precision is kept so a move age is never overstated. Unknown or malformed -> None."""
    s = str(d or "").strip()[:10]
    if not s or s.startswith("1970"):
        return None
    m = _DATE.match(s)
    if not m:
        return None
    y, mo, dd = int(m.group(1)), m.group(2), m.group(3)
    try:
        if dd:
            x = date(y, int(mo), int(dd))
            return x, x, "day"
        if mo:
            return date(y, int(mo), 1), date(y, int(mo), calendar.monthrange(y, int(mo))[1]), "month"
        return date(y, 1, 1), date(y, 12, 31), "year"
    except ValueError:
        return None


def parse_date(d) -> Optional[date]:
    """Earliest possible day of YYYY-MM-DD, YYYY-MM or YYYY. Unknown, placeholder or malformed -> None.
    Use date_range when the precision matters (move age)."""
    r = date_range(d)
    return r[0] if r else None


def days_since(d, today: date) -> Optional[int]:
    """Whole days from a date (its earliest possible day) to today; None when unknown or malformed (never crashes)."""
    p = parse_date(d)
    return (today - p).days if p else None


def is_open(end) -> bool:
    """An experience entry is open (still current) when its end is empty or says present/current."""
    return str(end or "").strip().lower() in ("", "none", "null", "present", "current")


# ---- names ----------------------------------------------------------------------------------------------------------
HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "mx", "prof", "professor", "sir", "dame"}
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "mba", "cpa", "esq"}
# Dotted post-nominals at the end of a name ('Ph.D.', 'M.D.') would otherwise read as a last-name initial 'd'.
_POSTNOMINAL = re.compile(r"[\s,]+\(?(?:ph\.?\s?d|m\.\s?d|m\.\s?b\.\s?a|c\.\s?p\.\s?a|j\.\s?d)\.?\)?\s*$")

# Formal name -> common English nicknames. Matching goes both ways; two nicknames match when they share a formal name
# (Bill ~ Will), but two formal names never match through a shared nickname (Christopher !~ Christina via Chris).
NICKNAMES = {
    "william": "bill billy will willy liam", "robert": "bob bobby rob robbie bert", "richard": "rick ricky dick rich",
    "james": "jim jimmy jamie", "john": "jack johnny jon", "jonathan": "jon jonny", "michael": "mike mikey mick",
    "elizabeth": "liz lizzie beth betty eliza libby", "katherine": "kate katie kathy kat kay",
    "catherine": "kate katie cathy cat", "kathryn": "kate katie kathy", "margaret": "maggie meg peggy greta",
    "thomas": "tom tommy", "christopher": "chris kit", "christine": "chris tina", "christina": "chris tina",
    "joseph": "joe joey", "daniel": "dan danny", "david": "dave davy", "edward": "ed eddie ted ned",
    "charles": "charlie chuck chas", "anthony": "tony", "andrew": "andy drew", "alexander": "alex xander sasha",
    "alexandra": "alex sasha lexi", "benjamin": "ben benny", "matthew": "matt", "nicholas": "nick nicky",
    "patrick": "pat paddy", "patricia": "pat patty trish tricia", "stephen": "steve stevie", "steven": "steve stevie",
    "samuel": "sam sammy", "samantha": "sam sammy", "susan": "sue susie suzy", "rebecca": "becky becca",
    "deborah": "deb debbie", "jennifer": "jen jenny jenn", "jessica": "jess jessie", "kimberly": "kim",
    "gregory": "greg", "timothy": "tim timmy", "kenneth": "ken kenny", "ronald": "ron ronnie", "donald": "don donny",
    "gerald": "gerry jerry", "lawrence": "larry", "raymond": "ray", "frederick": "fred freddie", "albert": "al bert",
    "henry": "hank harry", "harold": "harry hal", "peter": "pete", "philip": "phil", "phillip": "phil",
    "douglas": "doug", "jeffrey": "jeff", "bradford": "brad", "bradley": "brad", "zachary": "zach zack",
    "joshua": "josh", "nathaniel": "nate nat", "nathan": "nate nat", "abigail": "abby abbie", "victoria": "vicky tori",
    "cynthia": "cindy", "theodore": "ted theo", "leonard": "leo len lenny", "eugene": "gene", "francis": "frank fran",
    "frances": "fran frankie", "sandra": "sandy", "judith": "judy", "mary": "molly polly", "sarah": "sally sadie",
    "barbara": "barb barbie", "pamela": "pam", "beverly": "bev", "dorothy": "dot dottie", "valerie": "val",
    "allison": "ali allie", "kassandra": "kassy", "cassandra": "cassie cass", "gabriel": "gabe", "isabella": "bella izzy",
}
# One name, several Latin spellings: every spelling in a group matches every other (Mohammed = Muhammad = Mohamed).
TRANSLITERATIONS = [
    "mohammed mohamed muhammad mohammad muhammed mohamad muhamad mohammod mohd",
    "ahmed ahmad ahmet", "yusuf yousef youssef yousuf yosef", "hussein hussain husain hossein husein",
    "alexander aleksandr aleksander alexandr", "sergey sergei serguei", "dmitry dmitri dmitriy", "yuri yury iurii",
    "mikhail michail", "andrei andrey", "aleksei alexei alexey", "olga olha", "katarzyna katharzyna",
]
_FORMALS = {}
for _formal, _nicks in NICKNAMES.items():
    _FORMALS.setdefault(_formal, set()).add(_formal)
    for _n in _nicks.split():
        _FORMALS.setdefault(_n, set()).add(_formal)
for _group in TRANSLITERATIONS:
    _spellings = _group.split()
    for _n in _spellings:
        _FORMALS.setdefault(_n, set()).add(_spellings[0])
        _FORMALS[_n] |= _FORMALS.get(_spellings[0], set())
# Particles inside a surname: never the 'first surname' of a compound ('de la Cruz' is Cruz, not De).
SURNAME_PARTICLES = {"de", "da", "di", "del", "della", "der", "den", "du", "la", "le", "van", "von", "bin", "ibn",
                     "al", "el", "st", "y", "dos", "das", "do", "e", "ten", "ter", "mac", "mc", "o"}


def _tokens(x: Optional[str]) -> List[str]:
    s = _plain(x).strip()
    while _POSTNOMINAL.search(s):
        s = _POSTNOMINAL.sub("", s)
    return re.findall(r"[a-z]+", s)


def _strip_titles(t: List[str]) -> List[str]:
    while len(t) > 1 and t[0] in HONORIFICS:
        t = t[1:]
    while len(t) > 1 and t[-1] in NAME_SUFFIXES:
        t = t[:-1]
    return t


def first_names_match(a: str, b: str) -> bool:
    """Equal, a 3+ letter prefix either way (Rob ~ Robert), or a listed nickname (Bill ~ William, Bob ~ Robert)."""
    if a == b or (min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a))):
        return True
    return bool(_FORMALS.get(a, set()) & _FORMALS.get(b, set()))


def _last_names_match(full: List[str], l: List[str]) -> bool:
    """The seed's last name is in the profile's name after its first word, compared without accents, apostrophes,
    hyphens or spaces ('O'Brien' = 'OBrien', 'Mc Donald' = 'McDonald', 'Al-Rashid' = 'Al Rashid'). A compound surname
    matches its first surname alone ('Pérez Rodríguez' = 'Pérez', 'Smith-Jones' = 'Smith'), never a particle ('de')."""
    if l[-1] in full[1:] or full[-1] == l[-1]:
        return True
    squashed = "".join(l)
    if any("".join(full[i:]) == squashed for i in range(1, len(full))):
        return True
    core = [x for x in l if x not in SURNAME_PARTICLES]
    return len(full) >= 2 and len(core) >= 2 and full[-1] == core[0]


def _match_tokens(full: List[str], f: List[str], l: List[str]) -> bool:
    if not full or not f or not l:
        return False
    if not _last_names_match(full, l):
        return False
    return first_names_match(full[0], f[0])


def _unicode_key(x: str) -> str:
    return "".join(re.findall(r"\w+", unicodedata.normalize("NFKC", x or "").casefold()))


def split_variants(raw: Optional[str]) -> List[str]:
    """The seed's optional 'Name variants' column: alternative names separated by ';' or ','."""
    return [v.strip() for v in re.split(r"[;,]", raw or "") if v.strip()]


def name_matches(candidate: dict, first: str, last: str, variants: Optional[Iterable[str]] = None) -> bool:
    """Exact last name (accent/case-insensitive, whole token; apostrophes, hyphens and spaces ignored; a compound
    surname matches its first surname) + first name equal, a 3+ letter prefix either way, a listed nickname or a
    transliteration (Mohammed = Muhammad). Honorifics (Dr, Ms, Prof) and suffixes (Jr, PhD) are ignored, and
    family-name-first order ('Zhao Kevin' = Kevin Zhao) matches. variants: alternative full names (maiden / married
    names) or, when one word, alternative first names. Non-Latin names match on the exact full name.
    A substring hit like 'Jo de Vries' -> 'Joanna Devrieson' still fails."""
    full_name = candidate.get("full_name") or f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}"
    full = _strip_titles(_tokens(full_name))
    f, l = _strip_titles(_tokens(first)), _strip_titles(_tokens(last))
    seeds = [(f, l)]
    for v in variants or []:
        vt = _strip_titles(_tokens(v))
        if len(vt) == 1:
            seeds.append((vt, l))
        elif len(vt) > 1:
            seeds.append((vt[:1], vt[-1:]))
    orders = [full] + ([full[::-1]] if len(full) == 2 else [])   # 'Zhao Kevin' = Kevin Zhao (two-word names only)
    if any(_match_tokens(o, sf, sl) for o in orders for sf, sl in seeds):
        return True
    # Non-Latin script (or anything the Latin tokens cannot see): exact full name, either order.
    if not full or not f or not l:
        cand_key = _unicode_key(full_name)
        wanted = {_unicode_key(f"{first} {last}"), _unicode_key(f"{last} {first}")} | {_unicode_key(v) for v in variants or []}
        return bool(cand_key) and cand_key in wanted - {""}
    return False


def seed_name_matches(candidate: dict, seed_row: dict) -> bool:
    """name_matches with everything the seed row knows: First, Last and the optional 'Name variants' column."""
    return name_matches(candidate, seed_row.get("First", ""), seed_row.get("Last", ""),
                        split_variants(seed_row.get("Name variants")))


# ---- headlines ------------------------------------------------------------------------------------------------------
def words4(text: Optional[str]) -> set:
    return set(re.findall(r"[a-z]{4,}", (text or "").lower()))


def mentions_company(headline: Optional[str], customer: str) -> bool:
    """Headline names the company: the whole name as a phrase ('Head of Ops | Acme Robotics'), or its leading name
    right after a marker ('ex-Acme', 'formerly Acme', '@Acme', 'at Acme'). A single ordinary word never ties
    ('Head of Travel at Contoso' must not tie to 'Travel Leaders Group'), and a generic head never ties alone
    ('Senior Manager at Air India' and 'ex-Air India' do not tie to Air Canada; '@Union Pacific' does not tie to
    Union)."""
    toks = company_tokens(customer)
    h = _fold(headline)
    if not toks or not h.strip():
        return False
    if len(toks) >= 2 and re.search(r"\b" + r"\W+".join(map(re.escape, toks)) + r"\b", h.replace("&", "and"), re.I):
        return True
    lead = toks[0]
    if len(lead) < 3 or lead in GENERIC_HEADS:
        return False
    marker = r"(?:\bex[- ]|\bformer(?:ly)?\s+(?:at\s+)?|\bpreviously\s+(?:at\s+)?|@\s*" + (r"|\bat\s+" if len(toks) == 1 else "") + r")"
    return re.search(marker + re.escape(lead) + r"\b", h, re.I) is not None

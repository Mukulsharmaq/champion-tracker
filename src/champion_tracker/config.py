"""Thresholds and vocabularies. Mirrors contracts/*.yaml (the human-readable contract); tests check they agree."""
import re

RULE_VERSION = "1.2"

RECENT_DAYS = 90            # contracts/context_contract.yaml -> freshness.move_recent_days
PROFILE_STALE_DAYS = 365    # contracts/context_contract.yaml -> freshness.profile_stale_days
EVIDENCE_GRACE_DAYS = 365   # contracts/context_contract.yaml -> freshness.evidence_grace_days
SEARCH_DEPTH = 50           # same-name search depth; a search that returns exactly this many was cut off
# Product use at the old customer after the profile's leaving date holds the row only when it is more than this many
# days after leaving: a final expense report filed after the last day is normal for an expense product.
ACTIVITY_AFTER_LEAVING_GRACE_DAYS = 60

CHAMPION_TAGS = {"initiated_review", "chose", "ran_rfp", "rolled_out", "owns_program", "business_case",
                 "deployed_elsewhere"}
USER_TAGS = {"uses", "quoted", "none"}

# A "current job" that is really a side seat. Not a move while the old job is still open.
# Matched on the TITLE only, and only for board / advisor roles: "Director, Advisory Services" is a real job.
# Trustee / volunteer / mentor / ambassador count only as the role noun itself, never as the head of a staff job
# ("Volunteer Coordinator Manager", "Brand Ambassador Program Manager", "Trustee Services Officer" are jobs).
_STAFF_NOUNS = (r"coordinator|manager|management|program|programme|programs|services|officer|lead|director|specialist"
                r"|administrator|operations|engagement|relations|recruiter|recruitment|team|office")
SIDE_ROLE = re.compile(
    r"\bboard\s+(?:member|director|chair\w*|observer|trustee|advis[oe]r|of\s+(?:directors|trustees|governors|advis[oe]rs))\b"
    r"|\b(?:chair\w*|vice[- ]chair\w*|member|trustee)\b[^|]*\bboard\b"
    r"|\bmember\s+of\s+the\s+board\b|\badvisory\s+(?:board|council|committee)\b"
    r"|\b(?:trustee|volunteer|mentor|ambassador)s?\b(?![\s,&/-]+(?:" + _STAFF_NOUNS + r")\b)", re.I)
ADVISOR = re.compile(r"\b(\w+\s+)?advis[oe]r\b", re.I)
# An advisor title is a side seat only with board context, or when nothing before "advisor" makes it a job:
# "Advisor", "Strategic Advisor", "Startup Advisor", "Board Advisor", "Advisor to the CEO", "Investor and Advisor".
# Any other word before it is a job ladder or an occupation: "Senior Advisor" and "Principal Advisor" (consultancies),
# "Financial Advisor" (banks), "Policy Advisor".
ADVISOR_BOARD_CONTEXT = re.compile(
    r"\badvis[oe]r\s+to\s+(?:the\s+)?(?:board|ceo|cfo|coo|cto|founders?|founding\s+team|executive\s+team|leadership\s+team)\b"
    r"|\binvestor\s*(?:and|&|/|,)\s*advis[oe]r\b|\badvis[oe]r\s*(?:and|&|/|,)\s*investor\b"
    r"|\bboard\s+advis[oe]r\b|\badvisory\s+board\b", re.I)
SIDE_ADVISOR_WORDS = {"strategic", "startup", "independent", "angel", "external", "honorary", "volunteer", "board",
                      "investor", "founding"}
# Kept for compatibility: occupations before "advisor" (never a side seat, now the default for any non-side word).
ADVISOR_JOB_WORDS = {"financial", "wealth", "investment", "tax", "legal", "insurance", "mortgage", "benefits", "client",
                     "customer", "sales", "travel", "career", "academic", "admissions", "enrollment", "retirement",
                     "pension", "credit", "solutions", "security", "compliance", "trust", "technical", "lending"}
# A fractional or interim seat is a real move, but a seller should know before acting on it: Review.
FRACTIONAL_OR_INTERIM = re.compile(r"\b(?:fractional|interim)\b", re.I)
# Self-employment only when the company or the title IS self-employment, never a company that contains the word
# ("Independent Bank" is a bank).
SELF_EMPLOYED_COMPANY = re.compile(
    r"\s*(?:self[- ]?employed|freelanc(?:e|er|ing)|independent|sole\s+(?:proprietor|trader))"
    r"(?:\s+(?:consultant|consulting|contractor|professional))?\s*[.,!]*\s*", re.I)
SELF_EMPLOYED_TITLE = re.compile(r"\s*(?:self[- ]?employed|freelanc(?:e|er)|independent\s+(?:consultant|contractor))\b", re.I)
SIDE_JOB_TYPES = {"freelance", "volunteer", "advisor", "adviser", "advisory", "board", "board_member", "board member"}

# Company names are compared after removing legal suffixes and trailing geography ("Acme USA, LLC" ==
# "Acme North America Inc"). Nothing else is dropped: "Delta" != "Delta Dental", "Bank of America" != "US Bank".
# The website settles the rest: ryder.com is Ryder's, so "Ryder System, Inc." at ryder.com is Ryder.
LEGAL_SUFFIXES = {"inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "corp", "corporation", "company", "co",
                  "plc", "sa", "ag", "gmbh", "kg", "kgaa", "se", "bv", "nv", "pty", "pte", "srl", "spa", "sas", "sarl",
                  "oy", "oyj", "ab", "as", "asa", "aps", "kk", "ltda", "bhd", "sdn", "group", "holdings", "holding"}
# Generic ONLY when trailing after the company's own name, and not after "of" ("Bank of America" keeps it).
GEO_WORDS = {"usa", "us", "na", "north", "south", "america", "americas", "uk", "emea", "apac", "latam", "europe", "eu",
             "global", "international", "worldwide", "canada", "australia", "india", "asia", "pacific"}
NAME_CONNECTORS = {"of", "and", "for", "the", "de", "du", "la", "del", "der"}
# Generic heads: never strip trailing geography if that would leave one of these alone, or any single word of 5 letters
# or fewer ("Air Canada" != "Air India", "Virgin Australia" != "Virgin America", "Union Pacific" != "Union"). The
# website decides those cases. A headline never ties on one of these alone ("ex-Air India" is not ex-Air Canada).
GENERIC_HEADS = {"air", "bank", "union", "virgin", "first", "national", "royal", "united", "general", "global",
                 "american", "pacific", "delta", "southern", "northern", "western", "eastern", "capital", "metro", "city",
                 "state"}
SHORT_HEAD_LEN = 5
# Geography that names the same region: "Acme USA" == "Acme North America" when the head is not generic.
GEO_REGIONS = {"usa": "na", "us": "na", "na": "na", "america": "na", "americas": "na", "north america": "na",
               "uk": "uk", "eu": "europe", "europe": "europe", "emea": "emea", "apac": "apac", "asia pacific": "apac",
               "latam": "latam", "south america": "latam"}
# Generic descriptors: a name of two or more words still matches when the other name only adds these
# ("Luck Grove" == "Luck Grove Telecom"). Never for a one-word name: "Delta" != "Delta Systems".
COMPANY_DESCRIPTORS = {"technologies", "technology", "tech", "telecom", "telecommunications", "communications",
                       "systems", "system", "solutions", "services", "software", "labs", "industries", "enterprises"}
GENERIC_COMPANY_WORDS = LEGAL_SUFFIXES | GEO_WORDS   # legacy name: words dropped only when trailing (see company_tokens)

SMALL_COMPANY_BANDS = {"0 to 1", "1 to 10", "2 to 10", "11 to 50", "51 to 200"}

PLAYS = {                    # P1 and P2 use the names in contracts/plays.json, so a row and its card say the same play
    "P1": "Champion joined a prospect",
    "P2": "Champion joined a customer",
    "P3": "Champion still at customer: reference / watch",
}

DECISION_ORDER = {"Card": 0, "Review": 1, "Hold": 2, "Watch": 3, "No action": 4, "Excluded": 5}

"""Thresholds and vocabularies. Mirrors contracts/*.yaml (the human-readable contract); tests check they agree."""
import re

RULE_VERSION = "1.0"

RECENT_DAYS = 90            # contracts/context_contract.yaml -> freshness.move_recent_days
PROFILE_STALE_DAYS = 365    # contracts/context_contract.yaml -> freshness.profile_stale_days

CHAMPION_TAGS = {"initiated_review", "chose", "ran_rfp", "rolled_out", "owns_program", "business_case",
                 "deployed_elsewhere"}
USER_TAGS = {"uses", "quoted", "none"}

# A "current job" that is really a side seat. Not a move while the old job is still open.
SIDE_ROLE = re.compile(r"\b(board|chair|chairman|chairwoman|advisor|adviser|advisory|volunteer|mentor|trustee|ambassador)\b", re.I)
SELF_EMPLOYED = re.compile(r"\b(self[- ]employed|freelance|independent|consultant)\b", re.I)
SIDE_JOB_TYPES = {"freelance", "volunteer"}

# Words ignored when comparing company names ("Acme USA, LLC" == "Acme North America").
GENERIC_COMPANY_WORDS = {
    "inc", "llc", "ltd", "limited", "corp", "corporation", "company", "co", "group", "holdings", "holding", "plc",
    "sa", "ag", "gmbh", "bv", "nv", "the", "usa", "us", "na", "north", "america", "americas", "technologies",
    "technology", "tech", "worldwide", "international", "global", "services", "labs", "and", "of", "uk", "emea"}

SMALL_COMPANY_BANDS = {"1 to 10", "2 to 10", "11 to 50", "51 to 200"}

PLAYS = {
    "P1": "Champion moved: intro play",
    "P2": "Champion moved to an existing customer: expansion play",
    "P3": "Champion still at customer: reference / watch",
}

DECISION_ORDER = {"Card": 0, "Review": 1, "Hold": 2, "Watch": 3, "No action": 4, "Excluded": 5}

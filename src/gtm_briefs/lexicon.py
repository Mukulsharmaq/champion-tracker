"""What counts as each theme, loaded at runtime from contracts/lexicon.json, or from the file LEXICON_PATH names (for
example contracts/lexicon.travel-expense.json: the same rules with a real travel-and-expense market's competitors). A
change is a reviewed data change, measured by tests/test_brief_eval.py on two LLM-written synthetic sets: the old set,
written and labelled by one LLM agent from a spec the rule author wrote, and an independent set written by one agent
and labelled by another. Neither is real call text.

Themes: pricing_friction · competitor · expansion · timing · people_change · engagement
"""
import json
import os
import re
from itertools import chain
from typing import Dict, Iterable, List, Optional

HOME = os.environ.get("GTM_HOME") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATH = os.path.join(HOME, "contracts", "lexicon.json")   # GTM_HOME: the folder holding contracts/ (needed for a wheel install)


def load(path: str = "") -> Dict:
    with open(path or os.environ.get("LEXICON_PATH") or PATH, encoding="utf-8") as f:
        return json.load(f)


LEX = load()
LEXICON_VERSION = LEX["version"]
COMPETITORS = LEX["competitors"]

# A capital letter at the start of a sentence (or of the text) is not a sign of a name.
_SENTENCE_START = r"(?:^|(?<=[.!?;:]\s)|(?<=[\n\"(“]))"


def _exclude_rx(phrase: str, match_case: bool) -> "re.Pattern":
    if not match_case:
        return re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", re.I)
    capital = phrase[:1].upper() + phrase[1:]
    return re.compile(r"(?<!\w)(?:" + re.escape(phrase) + "|" + _SENTENCE_START + re.escape(capital) + r")(?!\w)")


class Competitor:
    """One listed competitor: a whole-word match of its name (optionally case-sensitive) or of any alias (any case),
    ignoring a match that sits inside an excluded phrase. Real names are often everyday words ('ramp up' is not Ramp,
    'cost center' is not Center). Excluded phrases match in any case, or, with exclude_match_case, only as written
    (a capital first letter allowed at the start of a sentence): 'Concur with that.' is the verb, while
    'we use Concur with our cards' is the product."""

    def __init__(self, name: str, case_sensitive: bool = False, exclude: Iterable[str] = (), aliases: Iterable[str] = (),
                 exclude_match_case: bool = False):
        self.name = name
        word = lambda w, flags: re.compile(r"\b" + re.escape(w) + r"(?:'s)?\b", flags)  # noqa: E731
        self.rx = word(name, 0 if case_sensitive else re.I)
        self.aliases = [word(a, re.I) for a in aliases]
        self.exclude = [_exclude_rx(x, exclude_match_case) for x in exclude]

    def search(self, text: str) -> Optional["re.Match"]:
        text = text or ""
        spans = [m.span() for x in self.exclude for m in x.finditer(text)]
        for m in chain(self.rx.finditer(text), *(a.finditer(text) for a in self.aliases)):
            if not any(a <= m.start() and m.end() <= b for a, b in spans):
                return m
        return None


def competitor(name: str, rule: Dict = None) -> Competitor:
    """A Competitor from a contracts/lexicon.json -> competitor_rules entry (case_sensitive, exclude, aliases,
    exclude_match_case; all optional)."""
    rule = rule or {}
    return Competitor(name, bool(rule.get("case_sensitive")), rule.get("exclude") or (), rule.get("aliases") or (),
                      bool(rule.get("exclude_match_case")))


def competitor_labels(lex: Dict = None) -> List[str]:
    """Competitor names for the observer prompt, with any aliases: 'Amex GBT (also: Egencia, ...)'."""
    lex = lex or LEX
    rules = lex.get("competitor_rules") or {}
    out = []
    for c in lex["competitors"]:
        aliases = (rules.get(c) or {}).get("aliases") or []
        out.append(f"{c} (also: {', '.join(aliases)})" if aliases else c)
    return out


def _cue(word: str) -> str:
    """'licen*' matches any word starting licen; 'fee' only the whole word; symbols ($, %) match as they are."""
    stem = word.endswith("*")
    w = word.rstrip("*").lower()
    left = r"(?<![a-z0-9])" if w[:1].isalnum() else ""
    right = "" if stem or not w[-1:].isalnum() else r"(?![a-z0-9])"
    return left + re.escape(w) + right


def compile_all(lex: Dict = None) -> Dict:
    """Compile a lexicon dict (default: the loaded lexicon). Tests and other teams pass their own dict."""
    lex = lex or LEX
    I = re.I  # noqa: E741
    opt = lambda p: re.compile(p, I) if p else None  # noqa: E731
    rules = lex.get("competitor_rules") or {}
    tw, en, pr = lex["timing_window"], lex["expansion_negated"], lex["pricing"]
    return {
        "price_terms": re.compile(pr["terms"], I),
        "price_friction": re.compile(pr["friction"], I),
        "price_within": int(pr.get("within_words", 6)),
        "price_standalone": [re.compile(p, I) for p in pr["standalone"]],
        "price_comparison": opt(pr.get("comparison")),
        "price_comparison_priced": opt(pr.get("comparison_priced")),
        "not_price": opt(pr.get("not_price")),
        "negated_pricing": [re.compile(p, I) for p in pr["negated"]],
        "anchors_us": re.compile(pr["anchors_us"], I),
        "travel_spend": re.compile(pr["travel_spend"], I),
        "themes": {k: [re.compile(p, I) for p in v] for k, v in lex["themes"].items()},
        "timing_when": [re.compile(p, I) for p in tw["when"]],
        "timing_context": re.compile(tw["context"], I),
        "timing_customer_context": re.compile(tw["customer_context"], I),
        "timing_within": int(tw.get("within_words", 6)),
        "exp_neg_before": re.compile(en["before_hit"], I),
        "exp_past": re.compile(en["past"], I),
        "exp_no_plans": re.compile(en["no_plans"], I),
        "competitors": {c: competitor(c, rules.get(c)) for c in lex["competitors"]},
        "strong_win": [re.compile(p, I) for p in lex["competitor_stance"]["strong_win"]],
        "risk": [re.compile(p, I) for p in lex["competitor_stance"]["risk"]],
        "weak_win": [re.compile(p, I) for p in lex["competitor_stance"]["weak_win"]],
        "speculation": re.compile(lex["speculation"], I),
        "reporting": re.compile(lex["reporting"], I),
        "call_report": re.compile(lex["call_report"], I),
        "observer_cues": {t: re.compile("|".join(_cue(w) for w in words), I)
                          for t, words in (lex.get("observer_cues") or {}).items() if not t.startswith("_") and words},
        "observer_not": {t: re.compile(p, I)
                         for t, p in (lex.get("observer_not") or {}).items() if not t.startswith("_") and p},
    }

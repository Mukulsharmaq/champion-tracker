"""Turn raw signals (call turns, CRM notes, field notes) into findings. The code decides; every finding keeps the
exact words, who said them and the link back to the source.

Signal row: source (call|crm|field), id, date (YYYY-MM-DD), account, speaker, speaker_title, side (customer|internal|
            unknown), text, url, at (time in the call, optional), account_id (CRM id, optional), trackers (Gong tracker
            names hit on this turn, optional). Every finding carries all of these through.

Confidence (stop rule for briefs):
  direct      the customer said it on a call or in writing               -> counts
  reported    a CRM or field note, or our rep on a call repeating what     -> counts, labelled 'reported by our team'
              the customer told them ("as you said, your CFO wants...")
  unverified  a call speaker the recorder could not place (dial-in)       -> counts, labelled 'speaker not identified'
  excluded    our own people guessing ("I think they'll push on price")    -> never counts, listed under Held
  ignored     our rep's own words on a call ("our pricing is per seat")    -> not evidence, dropped

What counts as a theme (contracts/lexicon.json, v2.4; LEXICON_PATH can name another file with the same keys):
  pricing_friction  a standalone phrase ("more than we budgeted"), or a price or terms word ("a three-year term") and a
                    friction cue ("non-starter", "doesn't make sense") within a few words in one sentence, or a
                    comparison that puts our price above another offer ("the FX fees on your card are way higher than
                    Amex", "more expensive than X") and points at us; never a sentence about the customer's own travel
                    spend unless it points at us ("your price", "per seat")
  timing            a named window (contract end, RFP, notice period), or a month / quarter / date with a purchase word
                    next to it (decision, contract, signing, procurement, paperwork, go-live). A date for the customer's
                    own operations is not timing ("we hired three people in March", "the expense deadline is the 5th",
                    "travel freeze for Q4")
  expansion         a request for more of the product (seats, modules, a higher tier); past expansion with "no plans for
                    more" is history, not a request
  people_change     a role that left, quit, moved on, retired or resigned ("our CFO left last week"), someone new,
                    a takeover or a re-org; not a role that "left a comment"
  competitor        a listed name or alias, matched by contracts/lexicon.json -> competitor_rules
  excluded          our team's guesses include opinion markers ("my read", "imo", "feels like")

A finding whose theme exists only because the model observer proposed it (and the code verified it) carries
"observer_prompt": the observer prompt version. Lexicon and Gong tracker findings do not.
"""
import re
from bisect import bisect_right
from typing import Dict, List

from .lexicon import LEXICON_VERSION, compile_all

_RX = compile_all()
QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})
BASE = ("source", "id", "date", "account", "speaker", "speaker_title", "side", "text", "url", "at", "account_id", "trackers")
COUNTED = ("direct", "reported", "unverified")
_SENTENCE = re.compile(r"(?<=[.!?;])\s+")
_WORD = re.compile(r"\S+")


class ObserverTheme(str):
    """A theme the model observer proposed and observer.validate accepted. It compares, sorts and serialises as the plain
    theme name, and carries the prompt version that produced it so the finding can say so (observer_prompt)."""
    prompt = ""

    def __new__(cls, theme: str, prompt: str):
        s = super().__new__(cls, theme)
        s.prompt = prompt
        return s


class _Masked:
    """A pattern whose matches inside a masked span are dropped ('approval rates' holds 'rates', not a price)."""

    def __init__(self, rx, mask):
        self.rx, self.mask = rx, mask

    def finditer(self, s: str):
        bad = [m.span() for m in self.mask.finditer(s)] if self.mask is not None else []
        return (m for m in self.rx.finditer(s) if not any(a <= m.start() < b for a, b in bad))


def _confidence(text: str, side: str, source: str, rx: Dict = None):
    rx = rx or _RX
    if side == "customer":
        return "direct", ""
    if rx["speculation"].search(text):
        return "excluded", "Our own team guessing, not the customer's words."
    if source == "call":
        if side == "unknown":
            return "unverified", ""
        if rx["call_report"].search(text):
            return "reported", ""
        return "ignored", ""
    return "reported", ""               # CRM and field notes are our team's reports by nature


def would_count(sig: Dict) -> bool:
    """True when the code would count this signal's words (customer turns, unplaced call speakers, CRM and field
    notes, our rep repeating the customer). The observer only sees these: a model never labels our rep's own words."""
    text = (sig.get("text") or "").translate(QUOTES)
    side = (sig.get("side") or "").strip().lower()
    source = (sig.get("source") or "").strip().lower()
    return bool(text.strip()) and _confidence(text, side, source)[0] in COUNTED


def _sentences(text: str) -> List[str]:
    return [s for s in _SENTENCE.split(text) if s.strip()]


def _near(sentence: str, a: List, b: List, n: int) -> bool:
    """A match of any pattern in a and any pattern in b at most n words apart in this sentence."""
    starts = [m.start() for m in _WORD.finditer(sentence)]
    at = lambda pos: bisect_right(starts, pos) - 1  # noqa: E731  (index of the word holding this character)
    spans = lambda pats: [(at(m.start()), at(max(m.start(), m.end() - 1))) for p in pats for m in p.finditer(sentence)]  # noqa: E731
    return any(max(0, b0 - a1, a0 - b1) <= n for a0, a1 in spans(a) for b0, b1 in spans(b))


def _comparison(s: str, rx: Dict) -> bool:
    """Our price compared unfavourably with another offer, in one sentence that points at us (anchors_us): a comparison
    that names price itself ("more expensive than", "costs more than"), or one such as "higher than" / "double what"
    within a few words of a price word ("the FX fees on your card are way higher than Amex"). "Higher than yours"
    favours us and is not matched; "approval rates" is not a price (not_price)."""
    if not rx["anchors_us"].search(s):
        return False
    if rx.get("price_comparison_priced") is not None and rx["price_comparison_priced"].search(s):
        return True
    return rx.get("price_comparison") is not None and \
        _near(s, [_Masked(rx["price_terms"], rx.get("not_price"))], [rx["price_comparison"]], rx["price_within"])


def _pricing(text: str, rx: Dict) -> bool:
    """Pushback on OUR price: a standalone phrase, a price word close to a friction cue, or a comparison that puts our
    price above another offer, in one sentence. A sentence about the customer's own travel spend ("cut travel spend",
    "hotel rates in London") counts only if it points at us."""
    for s in _sentences(text):
        hit = any(p.search(s) for p in rx["price_standalone"]) or \
            _near(s, [rx["price_terms"]], [rx["price_friction"]], rx["price_within"]) or _comparison(s, rx)
        if hit and not (rx["travel_spend"].search(s) and not rx["anchors_us"].search(s)):
            return True
    return False


def _timing(text: str, side: str, rx: Dict) -> bool:
    """A named window, or a month / quarter / date next to a decision, contract, deadline or go-live word. Renewal
    words count as that context only in the customer's own words (the CRM already holds the renewal date)."""
    if any(p.search(text) for p in rx["themes"]["timing"]):
        return True
    context = [rx["timing_context"]] + ([rx["timing_customer_context"]] if side == "customer" else [])
    return any(_near(s, rx["timing_when"], context, rx["timing_within"]) for s in _sentences(text))


def _expansion(text: str, rx: Dict) -> bool:
    """A request for more. A hit right after a negation ("not looking to expand") is not one, and neither is past
    expansion when the text says there are no plans for more ("We expanded into Mexico last year, no plans for more")."""
    no_plans = rx["exp_no_plans"].search(text)
    for p in rx["themes"]["expansion"]:
        for m in p.finditer(text):
            if rx["exp_neg_before"].search(text[:m.start()]):
                continue
            if no_plans and rx["exp_past"].search(m.group(0)):
                continue
            return True
    return False


def classify(sig: Dict, observed: List[str] = (), rx: Dict = None) -> Dict:
    """One signal -> {themes, competitors, stance, confidence, held_reason, negated_pricing, observer_prompts}.
    observed: themes the optional model observer proposed and observer.validate accepted, and Gong tracker themes
    (added to the lexicon's). observer_prompts: {theme: prompt version} for themes only the observer supplied.
    rx: a compiled lexicon (lexicon.compile_all(your_dict)); default: the loaded lexicon."""
    rx = rx or _RX
    text = (sig.get("text") or "").translate(QUOTES)
    side = (sig.get("side") or "").strip().lower()
    source = (sig.get("source") or "").strip().lower()
    confidence, held = _confidence(text, side, source, rx)

    themes = []
    negated = any(p.search(text) for p in rx["negated_pricing"])
    standalone = any(p.search(text) for p in rx["price_standalone"])
    if _pricing(text, rx) and (standalone or not negated):
        themes.append("pricing_friction")
    for t, pats in rx["themes"].items():
        if t == "renewal_date":
            continue
        if t == "timing":
            hit = _timing(text, side, rx)
        elif t == "expansion":
            hit = _expansion(text, rx)
        else:
            hit = any(p.search(text) for p in pats)
        if hit:
            themes.append(t)
    if side == "customer" and "timing" not in themes and any(p.search(text) for p in rx["themes"]["renewal_date"]):
        themes.append("timing")          # a renewal date counts when the customer raises it; the CRM already has it
    added = []
    for t in observed:
        if t not in themes and t not in added and t != "competitor":
            added.append(t)
    themes += [str(t) for t in added]
    observer_prompts = {str(t): t.prompt for t in added if isinstance(t, ObserverTheme)}

    comps = [c for c, p in rx["competitors"].items() if p.search(text)]
    stance = ""
    if comps:
        themes.append("competitor")
        hit = lambda k: any(p.search(text) for p in rx[k])  # noqa: E731
        stance = "win" if hit("strong_win") else "risk" if hit("risk") else "win" if hit("weak_win") else "mention"
    if negated and not held and "pricing_friction" not in themes:
        held = "Mentions price only to say it is not a problem."
    return {"themes": sorted(set(themes)), "competitors": comps, "stance": stance, "confidence": confidence,
            "held_reason": held, "negated_pricing": negated, "observer_prompts": observer_prompts}


def extract(signals: List[Dict], observations: Dict[str, List[str]] = None) -> List[Dict]:
    """All signals -> findings (one per signal per theme). Held items are kept separately with their reason.
    observations: optional {signal id: [themes]} from gtm_briefs.observer (already validated by the code) and Gong
    trackers. A finding whose theme only the observer supplied is stamped observer_prompt (the prompt version)."""
    out = []
    for s in signals:
        c = classify(s, (observations or {}).get(s.get("id"), []))
        if c["confidence"] == "ignored":
            continue
        base = {k: s.get(k, [] if k == "trackers" else "") for k in BASE}
        if c["confidence"] == "excluded" or (c["negated_pricing"] and not c["themes"]):
            if c["themes"] or c["negated_pricing"] or c["competitors"]:
                out.append({**base, "theme": "held", "competitors": c["competitors"], "stance": c["stance"],
                            "confidence": c["confidence"], "held_reason": c["held_reason"], "lexicon": LEXICON_VERSION})
            continue
        for t in c["themes"]:
            f = {**base, "theme": t, "competitors": c["competitors"] if t == "competitor" else [],
                 "stance": c["stance"] if t == "competitor" else "", "confidence": c["confidence"],
                 "held_reason": "", "lexicon": LEXICON_VERSION}
            if t in c["observer_prompts"]:
                f["observer_prompt"] = c["observer_prompts"][t]
            out.append(f)
    return out

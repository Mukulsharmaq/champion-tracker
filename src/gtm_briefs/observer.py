"""Optional model observer for the brief (off by default). The model observes; the code decides.

The lexicon misses phrasing it has never seen (tests/test_brief_eval.py: on the old set's 80-row holdout, lexicon v2.2
recall was 0.50 engagement, 0.60 people change, 0.67 pricing friction; on independent dev text it is lower still).
The observer reads each turn it is sent and proposes themes WITH the exact words that show each one. The code keeps a
proposed theme only if:
  - the quoted evidence (3 characters or more) appears word for word in the turn, ignoring case, spacing and curly
    quotes (no paraphrase, no invention),
  - the evidence contains a cue word for THAT theme (contracts/lexicon.json -> observer_cues), so "the product" is not
    pricing evidence and "the time" is not expansion evidence. Timing cues are purchase words (contract, renewal, RFP,
    procurement, decide, sign, budget cycle, go-live, notice, proposal, quote); evidence that only names a month,
    quarter or date counts when the sentence holding it has one ("close the audit finding by Q1" does not). Expansion
    cues point at more of our product (seats, users, licences, modules, tier, rollout, add, expand), not the
    customer's own growth ("headcount going from 450 to 600", "we hired 60 people"),
  - the evidence does not match that theme's refusal pattern (observer_not): asking us to send content, or our team
    sending it, is not engagement yet,
  - for 'competitor', the evidence and the turn contain a name or alias from contracts/lexicon.json -> competitors
    (competitor_rules apply; the prompt lists the names with their aliases),
  - for 'pricing_friction', the turn is not a "price is not the problem" turn (unless it also has a standalone pushback
    phrase such as "discount"), and not about the customer's own travel spend unless it points at us (the lexicon's
    travel_spend and anchors_us patterns, checked on the whole turn).
An accepted 'competitor' adds nothing by itself: extract.classify takes competitor names only from the lexicon.
Only turns the code would count are sent (extract.would_count: customer turns, unplaced call speakers, CRM and field
notes, our rep repeating the customer); our rep's own call turns, our team's guesses and empty turns are never sent.
Who said it, and whether it counts, stays in extract.py, never in the model. Each accepted theme carries PROMPT_VERSION
(extract.ObserverTheme), so a finding that exists only because of the observer is stamped "observer_prompt".

Turn on: config.json -> "observer": {"enabled": true}. Provider: OBSERVER_PROVIDER=anthropic (default; needs
ANTHROPIC_API_KEY, OBSERVER_MODEL optional, default claude-haiku-5-5) or openai (any OpenAI-compatible chat completions
API at OPENAI_BASE_URL, default https://api.openai.com/v1; needs OPENAI_API_KEY and OBSERVER_MODEL). OBSERVER_MAX_TURNS
caps the turns sent in one run (default 2,000). observe_with_stats() also reports what was sent, skipped and capped.
"""
import json
import os
import re
from typing import Callable, Dict, List

from .extract import _SENTENCE, QUOTES, ObserverTheme, would_count
from .lexicon import compile_all, competitor_labels

PROMPT_VERSION = "1.1"
THEMES = ["pricing_friction", "competitor", "expansion", "timing", "people_change", "engagement"]
PROMPT = """You label short texts from a B2B sales team: customer turns from sales calls, and our reps' CRM and Slack notes.
For each text, list the themes that are present as a signal the sales team should act on, and for each theme copy the
exact words from the text that show it (a short span, copied character for character).

Themes:
- pricing_friction: the buyer pushes back on our price, fees or commercial terms (too expensive, more than they
  budgeted for us, wants a discount, a contract length or payment terms they will not accept, a worse rate than they
  get elsewhere). Not a neutral question about price or discounts, not "price isn't the issue", and not the customer's
  own spending (their travel budget or expense cuts).
- competitor: a named competitor is mentioned. Competitors: {competitors}. Only these names.
- expansion: the buyer wants more of our product: more seats, users or licences, a higher tier, another module, or a
  rollout to more teams, offices, entities or regions. Not the customer's own growth (hiring, headcount, a new office)
  unless they want our product there too.
- timing: a window for a buying decision about our product: contract end, renewal, notice period, RFP, proposal or
  quote date, procurement or signing date, budget cycle, go-live date, "decide by", "locked in until". Not a date for
  the customer's own operations (audits, expense deadlines, internal meetings, policy rewrites, hiring or travel
  freezes), and not the date of our next call or demo.
- people_change: someone joined, left, retired, was promoted or moved to another role; a new owner or contact; a
  re-org of who owns this. Not someone being brought onto a call.
- engagement: the buyer engaged with our content or events (attended or watched our webinar or event; read,
  downloaded or used our guide, template, calculator or report; opened our deck or email; replied to our newsletter;
  liked or shared our post). Asking us to send something, or our team sending it, is not engagement yet, and another
  company's conference is not our event.
A text can have several themes or none. Do not judge who is speaking; just label the text.

Return only JSON: a list with one object per input, in the same order:
[{{"id": "<id>", "themes": {{"<theme>": "<exact words>"}}}}]

Texts:
{texts}"""
_RX = compile_all()


def build_prompt(items: List[Dict]) -> str:
    texts = "\n".join(json.dumps({"id": i["id"], "text": i["text"]}, ensure_ascii=False) for i in items)
    return PROMPT.format(competitors=", ".join(competitor_labels()), texts=texts)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").translate(QUOTES)).strip().lower()


def _around(t: str, ev: str) -> str:
    """The sentences of the normalised turn t that hold the normalised evidence ev."""
    i = t.find(ev)
    j = i + len(ev)
    cuts = [m.span() for m in _SENTENCE.finditer(t)]
    starts, ends = [0] + [b for _, b in cuts], [a for a, _ in cuts] + [len(t)]
    return " ".join(t[a:b] for a, b in zip(starts, ends) if a < j and b > i)


def _has_cue(theme: str, evidence: str, rx: Dict, turn: str = "") -> bool:
    """The quoted words must themselves name the theme with a cue word. Timing evidence that only names a month,
    quarter or date also counts when the sentence holding it has a purchase word: a date alone is not a buying window
    ("an audit finding we need to close by Q1", "our internal QBR is on the 9th")."""
    if theme == "competitor":
        return any(c.search(evidence) for c in rx["competitors"].values())
    cue = rx["observer_cues"].get(theme)
    if cue is not None and cue.search(_norm(evidence)):
        return True
    if theme != "timing":
        return False
    if any(p.search(evidence) for p in rx["themes"]["timing"]):
        return True
    return cue is not None and any(p.search(evidence) for p in rx["timing_when"]) and \
        bool(cue.search(_around(_norm(turn), _norm(evidence))))


def validate(text: str, proposed: Dict[str, str], rx: Dict = None) -> List[str]:
    """Proposed {theme: evidence} -> the themes the code accepts (see the module docstring for every check)."""
    rx = rx or _RX
    raw = (text or "").translate(QUOTES)
    t = _norm(raw)
    keep = []
    for theme, ev in (proposed or {}).items():
        if theme not in THEMES or not isinstance(ev, str) or len(_norm(ev)) < 3 or _norm(ev) not in t:
            continue
        ev = ev.translate(QUOTES)
        if not _has_cue(theme, ev, rx, raw):
            continue
        refuse = rx.get("observer_not", {}).get(theme)
        if refuse is not None and refuse.search(ev):
            continue
        if theme == "competitor" and not any(c.search(raw) for c in rx["competitors"].values()):
            continue
        if theme == "pricing_friction":
            if any(p.search(raw) for p in rx["negated_pricing"]) and not any(p.search(raw) for p in rx["price_standalone"]):
                continue
            if rx["travel_spend"].search(raw) and not rx["anchors_us"].search(raw):
                continue
        keep.append(theme)
    return keep


def tracker_themes(signals: List[Dict], mapping: Dict[str, str]) -> Dict[str, List[str]]:
    """Gong tracker hits on a turn -> themes, by contracts/lexicon.json -> gong_trackers. Competitor trackers are
    only a hint: the competitor theme still needs a listed name in the words."""
    out: Dict[str, List[str]] = {}
    for s in signals:
        th = [mapping[t] for t in s.get("trackers") or [] if mapping.get(t) in THEMES]
        if th:
            out[s["id"]] = sorted(set(th))
    return out


def call_anthropic(prompt: str) -> str:
    from gtm_connectors import http_json, need
    env = need("ANTHROPIC_API_KEY")
    r = http_json("POST", "https://api.anthropic.com/v1/messages",
                  {"x-api-key": env["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
                  {"model": os.environ.get("OBSERVER_MODEL", "claude-haiku-5-5"), "max_tokens": 8000, "temperature": 0,
                   "messages": [{"role": "user", "content": prompt}]}, idempotent=True)   # a read: safe to retry
    return "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")


def call_openai(prompt: str) -> str:
    """OpenAI, or any gateway or local server that speaks the same chat completions API, at OPENAI_BASE_URL."""
    from gtm_connectors import http_json, need
    env = need("OPENAI_API_KEY", "OBSERVER_MODEL")
    base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    body = {"model": env["OBSERVER_MODEL"], "max_tokens": 8000, "messages": [{"role": "user", "content": prompt}]}
    if os.environ.get("OBSERVER_TEMPERATURE", "0") != "":
        body["temperature"] = float(os.environ.get("OBSERVER_TEMPERATURE", "0"))   # reasoning models may ignore it
    effort = os.environ.get("OBSERVER_REASONING_EFFORT")                          # e.g. low: fewer hidden tokens
    if effort:
        body.update({"reasoning": {"effort": effort}} if "openrouter.ai" in base else {"reasoning_effort": effort})
    try:
        r = http_json("POST", base + "/chat/completions", {"Authorization": "Bearer " + env["OPENAI_API_KEY"]},
                      body, idempotent=True)
    except RuntimeError as e:                       # a model that rejects temperature: ask again without it
        if "temperature" not in str(e).lower() or "temperature" not in body:
            raise
        body.pop("temperature")
        r = http_json("POST", base + "/chat/completions", {"Authorization": "Bearer " + env["OPENAI_API_KEY"]},
                      body, idempotent=True)
    choice = (r.get("choices") or [{}])[0]
    return (choice.get("message") or {}).get("content") or ""


PROVIDERS = {"anthropic": call_anthropic, "openai": call_openai}
PROVIDER_ENV = {"anthropic": ["ANTHROPIC_API_KEY"], "openai": ["OPENAI_API_KEY", "OBSERVER_MODEL"]}


def provider_name() -> str:
    name = (os.environ.get("OBSERVER_PROVIDER") or "anthropic").strip().lower()
    if name not in PROVIDERS:
        raise ValueError(f"OBSERVER_PROVIDER={name!r}: use one of {', '.join(PROVIDERS)}")
    return name


def required_env() -> List[str]:
    """The environment variables the configured provider still needs (for preflight; empty when ready)."""
    return [n for n in PROVIDER_ENV[provider_name()] if not os.environ.get(n)]


def observe_with_stats(signals: List[Dict], model: Callable[[str], str] = None, batch: int = 40) -> Dict:
    """signals -> {"observations": {signal id: [accepted themes]}, "stats": {sent, skipped_not_counted,
    truncated_by_cap, malformed_batches}}. Only turns the code would count are sent; the rest are skipped, and
    eligible turns past OBSERVER_MAX_TURNS are not sent. A batch whose reply holds no JSON list counts as malformed
    and its turns get no themes. Accepted themes are extract.ObserverTheme strings carrying PROMPT_VERSION."""
    cap = int(os.environ.get("OBSERVER_MAX_TURNS", "2000"))
    eligible = [{"id": s["id"], "text": s["text"]} for s in signals if would_count(s)]
    items = eligible[:cap]
    stats = {"sent": len(items), "skipped_not_counted": len(signals) - len(eligible),
             "truncated_by_cap": len(eligible) - len(items), "malformed_batches": 0, "failed_batches": 0}
    out: Dict[str, List[str]] = {}
    call = model or (PROVIDERS[provider_name()] if items else None)
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        try:
            raw = call(build_prompt(chunk)) or ""
        except Exception:  # noqa: BLE001  (one failed batch costs its turns, not the whole run)
            stats["failed_batches"] += 1
            continue
        m = re.search(r"\[.*\]", raw, re.S)
        try:
            parsed = json.loads(m.group(0)) if m else None
        except ValueError:
            parsed = None
        if not isinstance(parsed, list):
            stats["malformed_batches"] += 1
            parsed = []
        by_id = {p.get("id"): p.get("themes") or {} for p in parsed if isinstance(p, dict)}
        for it in chunk:
            th = by_id.get(it["id"], {})
            accepted = validate(it["text"], th if isinstance(th, dict) else {})
            out[it["id"]] = [ObserverTheme(t, PROMPT_VERSION) for t in accepted]
    return {"observations": out, "stats": stats}


def observe(signals: List[Dict], model: Callable[[str], str] = None, batch: int = 40) -> Dict[str, List[str]]:
    """signals -> {signal id: [accepted themes]} for the turns that were sent (see observe_with_stats)."""
    return observe_with_stats(signals, model, batch)["observations"]

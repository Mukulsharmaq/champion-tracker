"""The GTM brief: what changed, who said it, and the link back to the source so the team can follow up.

build_brief(findings, period=(start, end), previous=(start, end)) -> dict
to_markdown(brief) -> Slack Canvas / doc markdown (every quote passed through neutralize)
neutralize(text) -> the text with Slack mentions turned into plain words
"""
import re
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

from .lexicon import LEXICON_VERSION

THEME_TITLES = {"pricing_friction": "Pricing friction", "competitor": "Competitor mentions", "expansion": "Expansion requests",
                "timing": "Timing windows", "people_change": "People changes", "engagement": "Engagement"}
ORDER = ["competitor", "pricing_friction", "expansion", "timing", "people_change", "engagement"]


_MENTION = re.compile(r"<([!@#])([^<>|]*)(?:\|([^<>]*))?>")
_CANVAS_MENTION = re.compile(r"!\[\]\(([@#])([^()\s]+)\)")


def _plain(m: "re.Match") -> str:
    kind, target, label = m.group(1), m.group(2), (m.group(3) or "").lstrip("@#")
    if kind == "!":
        if target.startswith("subteam^"):
            return "@" + (label or "group")
        if target in ("channel", "here", "everyone"):
            return "@" + target
        return label or "@" + target          # <!date^...|fallback> and anything else: the readable part
    return ("@" if kind == "@" else "#") + (label or target)


def neutralize(text: str) -> str:
    """A quote re-posted raw could ping a whole channel: a field note starting "<!channel>" would notify everyone again
    when the brief is posted. Slack mentions (<!channel>, <!here>, <!everyone>, <@U123>, <#C123|name>,
    <!subteam^S1|@team>) and canvas mentions (![](@U123)) become plain words such as "@channel"."""
    return _CANVAS_MENTION.sub(lambda m: m.group(1) + m.group(2), _MENTION.sub(_plain, text or ""))


def mark_repeats(findings: List[Dict]) -> List[Dict]:
    """Our team's note that repeats what the customer already said (same account, theme and competitor) is shown,
    but counted once: the customer's words are the count, the note is corroboration."""
    key = lambda f: (f["account"], f["theme"], tuple(sorted(f.get("competitors") or [])))  # noqa: E731
    direct = {key(f) for f in findings if f["confidence"] == "direct"}
    return [dict(f, repeat=f["confidence"] != "direct" and key(f) in direct) for f in findings]


def _in(d: str, period: Tuple[str, str]) -> bool:
    return period[0] <= (d or "")[:10] <= period[1]


def build_brief(findings: List[Dict], period: Tuple[str, str], previous: Tuple[str, str]) -> Dict:
    now = mark_repeats([f for f in findings if _in(f["date"], period)])
    before = mark_repeats([f for f in findings if _in(f["date"], previous)])
    themes = {}
    for t in ORDER:
        items = [f for f in now if f["theme"] == t]
        cur = [f for f in items if not f["repeat"]]
        prev = [f for f in before if f["theme"] == t and not f["repeat"]]
        cur_acc, prev_acc = sorted({f["account"] for f in cur}), sorted({f["account"] for f in prev})
        themes[t] = {"signals": len(cur), "signals_before": len(prev), "accounts": cur_acc, "accounts_before": prev_acc,
                     "new_accounts": [a for a in cur_acc if a not in prev_acc],
                     "repeats": len(items) - len(cur),
                     "items": sorted(items, key=lambda f: (f["account"], f["repeat"], f["date"], f["id"]))}
    comp_now = Counter(c for f in now if f["theme"] == "competitor" and not f["repeat"] for c in f["competitors"])
    comp_before = Counter(c for f in before if f["theme"] == "competitor" and not f["repeat"] for c in f["competitors"])
    comp_accounts = defaultdict(set)
    comp_stance = defaultdict(Counter)
    for f in now:
        if f["theme"] == "competitor" and not f["repeat"]:
            for c in f["competitors"]:
                comp_accounts[c].add(f["account"])
                comp_stance[c][f["stance"]] += 1
    competitors = [{"name": c, "mentions": n, "mentions_before": comp_before.get(c, 0), "accounts": sorted(comp_accounts[c]),
                    "stance": dict(comp_stance[c]), "new": comp_before.get(c, 0) == 0} for c, n in comp_now.most_common()]
    competitors += [{"name": c, "mentions": 0, "mentions_before": n, "accounts": [], "stance": {}, "new": False}
                    for c, n in comp_before.most_common() if c not in comp_now]     # gone quiet: say so
    brief = {"period": period, "previous": previous, "lexicon": LEXICON_VERSION, "themes": themes, "competitors": competitors,
             "held": [f for f in now if f["theme"] == "held"]}
    brief["what_changed"] = what_changed(brief)
    return brief


def _n(k: int, word: str) -> str:
    return f"{k} {word}" + ("" if k == 1 else "s")


def _accounts(xs: List[str]) -> str:
    return ", ".join(x or "(no CRM account linked)" for x in xs) if xs else "none"


def what_changed(b: Dict) -> List[str]:
    lines = []
    for c in b["competitors"]:
        if not c["mentions"]:
            lines.append(f"{c['name']}: no mentions this period (was {c['mentions_before']}).")
            continue
        st = {k: v for k, v in c["stance"].items() if k}
        stance = (f"all {st and next(iter(st))}" if len(st) == 1 else ", ".join(f"{v} {k}" for k, v in sorted(st.items()))) or "mention"
        tag = "new: none last period" if c["new"] else f"was {c['mentions_before']}"
        lines.append(f"{c['name']}: {_n(c['mentions'], 'mention')} at {_n(len(c['accounts']), 'account')} ({tag}): "
                     f"{_accounts(c['accounts'])}; stance: {stance}.")
    for t in ["pricing_friction", "expansion", "timing"]:
        th = b["themes"][t]
        if not th["signals"] and not th["signals_before"]:
            continue
        arrow = "up" if th["signals"] > th["signals_before"] else "down" if th["signals"] < th["signals_before"] else "flat"
        new = f"; new: {_accounts(th['new_accounts'])}" if th["new_accounts"] else ""
        lines.append(f"{THEME_TITLES[t]}: {_n(th['signals'], 'signal')} at {_n(len(th['accounts']), 'account')}, "
                     f"{arrow} from {th['signals_before']}{new}.")
    pc = b["themes"]["people_change"]
    if pc["signals"]:
        lines.append(f"People changes: {_n(pc['signals'], 'signal')} at {_accounts(pc['accounts'])} "
                     f"(cross-checked against the champion tracker).")
    en = b["themes"]["engagement"]
    if en["signals"]:
        lines.append(f"Engagement: {_n(en['signals'], 'signal')} at {_accounts(en['accounts'])}.")
    reps = sum(th["repeats"] for th in b["themes"].values())
    if reps:
        lines.append(f"Counted once: {_n(reps, 'note')} from our team repeated what a customer already said.")
    if b["held"]:
        lines.append(f"Held: {_n(len(b['held']), 'item')} did not count (reasons listed at the end).")
    return lines


def to_markdown(b: Dict, title: str = "GTM brief") -> str:
    p0, p1 = b["period"]
    out = [f"# {title}: {p0} to {p1}", "",
           f"Compared with {b['previous'][0]} to {b['previous'][1]}. Every item links to its source. Lexicon v{b['lexicon']}.", "",
           "## What changed"]
    out += [f"- {line}" for line in b["what_changed"]] + [""]
    for t in ORDER:
        th = b["themes"][t]
        if not th["items"]:
            continue
        out.append(f"## {THEME_TITLES[t]} ({th['signals']})")
        for f in th["items"]:
            who = f"{f['speaker']}, {f['speaker_title']} ({f['account'] or 'no CRM account linked'})"
            label = {"direct": "", "unverified": " · speaker not identified"}.get(f["confidence"], " · reported by our team")
            label += " · repeats the customer's words, counted once" if f.get("repeat") else ""
            comp = f" · {', '.join(f['competitors'])} ({f['stance']})" if f.get("competitors") else ""
            out.append(f"- \"{f['text']}\" · {who}{label}{comp} · {f['date']} · [source]({f['url']})")
        out.append("")
    if b["held"]:
        out.append("## Held (did not count)")
        for f in b["held"]:
            out.append(f"- \"{f['text']}\" · {f['speaker']} ({f['account']}): {f['held_reason']} · [source]({f['url']})")
        out.append("")
    return neutralize("\n".join(out))          # every quote, name and account came from a source

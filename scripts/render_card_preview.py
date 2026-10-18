"""Render a Block Kit card JSON as a Slack-like HTML preview (for the README image).
  python3 scripts/render_card_preview.py examples/slack_card_card.json examples/slack_card_preview.html
Then screenshot it with any headless Chromium, e.g.:
  <chrome> --headless --screenshot=examples/slack_card_preview.png --window-size=720,700 file://$PWD/examples/slack_card_preview.html
"""
import html
import json
import re
import sys


def md(text: str) -> str:
    t = html.escape(text)
    t = re.sub(r"&lt;(https?://[^|&]+)\|([^&]+)&gt;", r'<a href="\1">\2</a>', t)
    t = re.sub(r"\*([^*]+)\*", r"<b>\1</b>", t)
    t = re.sub(r"(^|\n)&gt;(.*)", r'\1<span class="quote">\2</span>', t)
    return t.replace("\n", "<br>")


def render(card: dict) -> str:
    parts = []
    for b in card["blocks"]:
        if b["type"] == "header":
            parts.append(f'<div class="hdr">{html.escape(b["text"]["text"])}</div>')
        elif b["type"] == "section" and "fields" in b:
            parts.append('<div class="grid">' + "".join(f'<div>{md(f["text"])}</div>' for f in b["fields"]) + "</div>")
        elif b["type"] == "section":
            parts.append(f'<div class="sec">{md(b["text"]["text"])}</div>')
        elif b["type"] == "context":
            parts.append('<div class="ctx">' + " ".join(md(e["text"]) for e in b["elements"]) + "</div>")
        elif b["type"] == "actions":
            parts.append('<div class="btns">' + "".join(
                f'<span class="btn {e.get("style", "")}">{html.escape(e["text"]["text"])}</span>' for e in b["elements"]) + "</div>")
    return f"""<!doctype html><meta charset="utf-8"><style>
body{{font:15px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;background:#fff;color:#1d1c1d;margin:0;padding:20px}}
.msg{{display:flex;gap:10px;max-width:680px}} .av{{width:36px;height:36px;border-radius:6px;background:#135CFB;color:#fff;
display:flex;align-items:center;justify-content:center;font-weight:700}} .who{{font-weight:800}} .app{{background:#e8e8e8;font-size:11px;
padding:1px 4px;border-radius:3px;margin-left:4px;color:#555}} .hdr{{font-weight:800;font-size:17px;margin:6px 0}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px 18px;margin:6px 0}} .sec{{margin:8px 0}} .quote{{border-left:4px solid #ddd;padding-left:8px;display:block;color:#333}}
.ctx{{color:#616061;font-size:12.5px;margin:8px 0}} a{{color:#1264a3;text-decoration:none}}
.btns{{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}} .btn{{border:1px solid #ccc;border-radius:4px;padding:5px 12px;font-weight:700;font-size:13px}}
.primary{{background:#007a5a;color:#fff;border-color:#007a5a}} .danger{{color:#e01e5a;border-color:#e01e5a}}
</style><div class="msg"><div class="av">CT</div><div><div><span class="who">Champion Tracker</span><span class="app">APP</span></div>
{''.join(parts)}</div></div>"""


if __name__ == "__main__":
    card = json.load(open(sys.argv[1], encoding="utf-8"))
    open(sys.argv[2], "w", encoding="utf-8").write(render(card))

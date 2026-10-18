"""Render out/canvas.md as a Slack-canvas-like HTML page (for screenshots and the README).
  python3 scripts/render_canvas_preview.py out/canvas.md examples/canvas_preview.html
Handles what the canvas uses: #/##/### headings, bullets, tables, **bold**, [links](url) and "quotes"."""
import html
import re
import sys


def inline(t: str) -> str:
    t = html.escape(t, quote=False)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    return t


def render(md: str, title: str) -> str:
    out, in_list, table = [], False, []

    def flush():
        nonlocal in_list, table
        if in_list:
            out.append("</ul>")
            in_list = False
        if table:
            head, *rows = [r for r in table if not re.match(r"^\|[-| ]+\|$", r)]
            cells = lambda r: [c.strip() for c in r.strip("|").split("|")]  # noqa: E731
            out.append("<table><tr>" + "".join(f"<th>{inline(c)}</th>" for c in cells(head)) + "</tr>"
                       + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells(r)) + "</tr>" for r in rows) + "</table>")
            table = []

    for line in md.splitlines():
        if line.startswith("|"):
            if in_list:
                out.append("</ul>")
                in_list = False
            table.append(line)
            continue
        if table:
            flush()
        m = re.match(r"^(#{1,3}) (.*)", line)
        if m:
            flush()
            out.append(f"<h{len(m.group(1))}>{inline(m.group(2))}</h{len(m.group(1))}>")
        elif line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(line[2:])}</li>")
        elif line.strip():
            flush()
            out.append(f"<p>{inline(line)}</p>")
        else:
            flush()
    flush()
    return f"""<!doctype html><meta charset="utf-8"><title>{html.escape(title)}</title><style>
body{{margin:0;background:#f8f8f8;font:15px/1.55 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1d1c1d}}
.bar{{background:#fff;border-bottom:1px solid #e2e2e2;padding:10px 24px;font-weight:700;font-size:14px}}
.bar span{{color:#616061;font-weight:400;margin-left:8px}}
.doc{{max-width:860px;margin:0 auto;background:#fff;padding:36px 56px 60px;min-height:100vh}}
h1{{font-size:28px;margin:0 0 8px}} h2{{font-size:20px;margin:28px 0 8px;border-top:1px solid #eee;padding-top:18px}} h3{{font-size:16px;margin:20px 0 6px}}
p{{margin:6px 0}} ul{{margin:6px 0 6px 20px;padding:0}} li{{margin:3px 0}} a{{color:#1264a3;text-decoration:none}}
table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:13.5px}} th,td{{border:1px solid #e2e2e2;padding:6px 8px;text-align:left;vertical-align:top}} th{{background:#f6f6f6}}
</style><div class="bar">Canvas<span>{html.escape(title)}</span></div><div class="doc">{''.join(out)}</div>"""


if __name__ == "__main__":
    md = open(sys.argv[1], encoding="utf-8").read()
    title = sys.argv[3] if len(sys.argv) > 3 else "GTM brief and pipeline"
    open(sys.argv[2], "w", encoding="utf-8").write(render(md, title))

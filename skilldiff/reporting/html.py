import html
import re
from pathlib import Path
from typing import Any

_INLINE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\)|_(.+?)_(?!\w)")

_CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--soft:#f6f8fa;
--good:#1a7f37;--bad:#cf222e;--tip:#1a7f37;--warning:#9a6700;--note:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;
--line:#3d444d;--soft:#151b23;--good:#3fb950;--bad:#f85149;--tip:#3fb950;
--warning:#d29922;--note:#4493f8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:1100px;margin:0 auto;padding:32px 24px 64px}
h1{font-size:28px;margin:0 0 4px}h2{font-size:20px;margin:36px 0 12px;
padding-bottom:6px;border-bottom:1px solid var(--line)}
.date{color:var(--muted);margin:0 0 24px}
.callout{border-left:4px solid var(--note);background:var(--soft);padding:12px 16px;
margin:16px 0;border-radius:6px}
.callout.tip{border-color:var(--tip)}.callout.warning{border-color:var(--warning)}
.callout-title{font-weight:600;margin:0 0 6px}.callout p{margin:4px 0}
.callout ul{margin:4px 0;padding-left:20px}
.table-wrap{overflow-x:auto;margin:8px 0 16px}
table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{text-align:left;color:var(--muted);font-weight:600;background:var(--soft)}
td.r,th.r{text-align:right}.good{color:var(--good);font-weight:600}
.bad{color:var(--bad);font-weight:600}
code{font:13px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--soft);
padding:1px 5px;border-radius:4px}
a{color:var(--note)}li{margin:4px 0}
"""


def _inline_html(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _INLINE.finditer(text):
        out.append(html.escape(text[pos : m.start()]))
        bold, code, link_text, link_url, italic = m.groups()
        if bold is not None:
            out.append(f"<strong>{_inline_html(bold)}</strong>")
        elif code is not None:
            out.append(f"<code>{html.escape(code)}</code>")
        elif link_text is not None:
            href = html.escape(link_url, quote=True)
            out.append(f'<a href="{href}">{html.escape(link_text)}</a>')
        else:
            out.append(f"<em>{_inline_html(italic)}</em>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def build_html_report(results: dict[str, Any], run_root: Path | None = None) -> str:
    import skilldiff.reporter as reporter

    title, blocks = reporter.build_report_blocks(results, run_root)
    return _render_html(title, blocks, str(results.get("timestamp", "")))


def _render_html(title: str, blocks: list[tuple], date: str) -> str:
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><main>",
        f"<h1>{html.escape(title)}</h1>",
        f'<p class="date">{html.escape(date)}</p>',
    ]
    for block in blocks:
        kind = block[0]
        if kind == "h":
            parts.append(f"<h{block[1]}>{_inline_html(block[2])}</h{block[1]}>")
        elif kind == "p":
            parts.append(f"<p>{_inline_html(block[1])}</p>")
        elif kind == "ul":
            items = "".join(f"<li>{_inline_html(i)}</li>" for i in block[1])
            parts.append(f"<ul>{items}</ul>")
        elif kind == "table":
            headers, rows, align = block[1], block[2], block[3]
            align = align or ["l"] + ["r"] * (len(headers) - 1)
            align = align + ["l"] * (len(headers) - len(align))
            head = "".join(f'<th class="{a}">{html.escape(h)}</th>' for h, a in zip(headers, align))
            body_rows = []
            for row in rows:
                cells = []
                for cell, a in zip(row, align):
                    text, tone = cell if isinstance(cell, tuple) else (cell, None)
                    cls = " ".join(c for c in (a, tone or "") if c)
                    cells.append(f'<td class="{cls}">{_inline_html(text)}</td>')
                body_rows.append("<tr>" + "".join(cells) + "</tr>")
            parts.append(
                f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
                f"<tbody>{''.join(body_rows)}</tbody></table></div>"
            )
        elif kind == "callout":
            _, ctype, ctitle, body = block
            if len(body) > 1 and ctype == "warning":
                inner = "<ul>" + "".join(f"<li>{_inline_html(b)}</li>" for b in body) + "</ul>"
            else:
                inner = "".join(f"<p>{_inline_html(b)}</p>" for b in body)
            parts.append(
                f'<div class="callout {ctype}"><p class="callout-title">'
                f"{html.escape(ctitle)}</p>{inner}</div>"
            )
    parts.append("</main></body></html>")
    return "\n".join(parts)

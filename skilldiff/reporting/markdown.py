import json
from pathlib import Path
from typing import Any

from skilldiff.reporting.table import _md_table


def _render_markdown(title: str, blocks: list[tuple], flavor: str, date: str) -> str:
    lines: list[str] = []
    if flavor == "quarto":
        lines.extend(
            [
                "---",
                f"title: {json.dumps(title)}",
                f"date: {json.dumps(date)}",
                "format:",
                "  html:",
                "    theme: cosmo",
                "    toc: true",
                "    embed-resources: true",
                "---",
                "",
            ]
        )
    else:
        lines.extend([f"# {title}", "", f"_{date}_", ""])

    for block in blocks:
        kind = block[0]
        if kind == "h":
            lines.extend(["#" * block[1] + " " + block[2], ""])
        elif kind == "p":
            lines.extend([block[1], ""])
        elif kind == "ul":
            lines.extend([f"- {item}" for item in block[1]] + [""])
        elif kind == "table":
            lines.extend(_md_table(block[1], block[2], block[3]) + [""])
        elif kind == "callout":
            _, ctype, ctitle, body = block
            bullets = len(body) > 1 and ctype == "warning"
            if flavor == "quarto":
                lines.append(f"::: {{.callout-{ctype}}}")
                lines.append(f"## {ctitle}")
                lines.extend([f"- {b}" for b in body] if bullets else body)
                lines.extend([":::", ""])
            else:
                gh = {"tip": "TIP", "warning": "WARNING", "note": "NOTE"}.get(ctype, "NOTE")
                lines.append(f"> [!{gh}]")
                lines.append(f"> **{ctitle}**")
                for b in body:
                    lines.append(f"> - {b}" if bullets else f"> {b}")
                    if not bullets:
                        lines.append(">")
                if lines[-1] == ">":
                    lines.pop()
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_quarto_report(results: dict[str, Any], run_root: Path | None = None) -> str:
    import skilldiff.reporter as reporter

    title, blocks = reporter.build_report_blocks(results, run_root)
    return _render_markdown(title, blocks, "quarto", str(results.get("timestamp", "")))


def build_markdown_report(results: dict[str, Any], run_root: Path | None = None) -> str:
    import skilldiff.reporter as reporter

    title, blocks = reporter.build_report_blocks(results, run_root)
    return _render_markdown(title, blocks, "gfm", str(results.get("timestamp", "")))

import re
from typing import Optional

from skilldiff.reporting.types import Cell


def _cell_text(cell: Cell) -> str:
    return cell[0] if isinstance(cell, tuple) else cell


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _md_table(headers: list[str], rows: list[list[Cell]], align: Optional[list[str]]) -> list[str]:
    align = align or ["l"] + ["r"] * (len(headers) - 1)
    if len(align) < len(headers):
        align = align + ["l"] * (len(headers) - len(align))
    sep = ["---:" if a == "r" else ":---" for a in align[: len(headers)]]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(sep) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_markdown_cell(_cell_text(c)) for c in row) + " |")
    return lines


def _tone(value: float, higher_is_better: bool, eps: float = 1e-9) -> Optional[str]:
    if abs(value) <= eps:
        return None
    return "good" if (value > 0) == higher_is_better else "bad"


def _strip_inline(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text).replace("`", "")

from pathlib import Path
from typing import Any

from skilldiff.persistence import atomic_write
from skilldiff.reporting.html import _render_html
from skilldiff.reporting.markdown import _render_markdown
from skilldiff.telemetry import redact_secrets


def create_reports(results: dict[str, Any], run_root: Path) -> dict[str, Path]:
    import skilldiff.reporter as reporter

    title, blocks = reporter.build_report_blocks(results, run_root)
    date = str(results.get("timestamp", ""))
    reports = {
        "html": ("report.html", _render_html(title, blocks, date)),
        "md": ("report.md", _render_markdown(title, blocks, "gfm", date)),
        "qmd": ("report.qmd", _render_markdown(title, blocks, "quarto", date)),
    }
    paths: dict[str, Path] = {}
    for key, (filename, content) in reports.items():
        paths[key] = run_root / filename
        atomic_write(paths[key], redact_secrets(content))
    return paths


def create_quarto_report(results: dict[str, Any], run_root: Path) -> tuple[Path, Path | None]:
    paths = create_reports(results, run_root)
    return paths["qmd"], paths["html"]

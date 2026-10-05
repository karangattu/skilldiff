"""Keep the shipped skilldiff skill consistent with the CLI it teaches.

AGENTS.md: when a skill specifies a format that code also generates, keep both
consistent and add a meaningful regression check for that contract. The skill
documents the Evaluation results table, the verdict strings, and the CLI
commands; this module pins all three to their sources of truth in the code.
"""

import argparse
import re
from pathlib import Path

from skilldiff import cli, reporter
from skilldiff.linter import lint_skill

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "skilldiff"
SKILL_MD = SKILL_DIR / "SKILL.md"
DOC_PATHS = [SKILL_MD, *sorted((SKILL_DIR / "references").glob("*.md"))]
ALL_DOCS = "\n".join(p.read_text(encoding="utf-8") for p in DOC_PATHS)


def _documented_evaluation_headers() -> list[str]:
    for line in ALL_DOCS.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and "App" in stripped and "Arm" in stripped:
            cells = [c.strip().strip("*").strip() for c in stripped.strip("|").split("|")]
            if "Score" in cells:
                return cells
    raise AssertionError("no Evaluation results table header found in the skill docs")


def test_evaluation_table_columns_match_renderer():
    assert _documented_evaluation_headers() == reporter._EVALUATION_HEADERS


def test_verdict_strings_documented():
    for verdict in reporter._RECOMMENDATION_KIND:
        assert verdict in ALL_DOCS


def test_documented_commands_exist():
    parser = cli.build_parser()
    sub_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    known = set(sub_action.choices)
    code_spans = re.findall(r"```[^\n]*\n(.*?)```", ALL_DOCS, re.DOTALL)
    code_spans += re.findall(r"`([^`\n]+)`", ALL_DOCS)
    mentioned = {
        m.group(1)
        for span in code_spans
        for m in re.finditer(r"(?<![\w/.-])skilldiff ([a-z][a-z-]*)", span)
    }
    assert mentioned, "expected the skill to document at least one CLI command"
    unknown = mentioned - known
    assert not unknown, f"skill documents unknown commands: {sorted(unknown)}"


def test_shipped_skill_passes_own_linter():
    result = lint_skill(SKILL_DIR)
    assert result.valid, result.errors
    assert not result.warnings, result.warnings


def test_skill_md_points_to_reporting_reference():
    assert "references/reporting.md" in SKILL_MD.read_text(encoding="utf-8")

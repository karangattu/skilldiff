from pathlib import Path

import yaml

from skilldiff.linter import lint_prompt_coverage


def _skill(tmp_path: Path, description: str) -> Path:
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: demo\n"
        f"description: {description}\n"
        "---\n\n"
        "Use the diagnostics workflow.\n"
    )
    return skill


def _tasks(tmp_path: Path, prompts: list[str]) -> Path:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    for index, prompt in enumerate(prompts, start=1):
        (tasks / f"t{index}.yaml").write_text(
            yaml.safe_dump({"id": f"t{index}", "prompt": prompt})
        )
    return tasks


def test_prompt_coverage_flags_symptom_prompts_with_no_trigger_overlap(tmp_path):
    skill = _skill(
        tmp_path,
        "House style for changelog entries and release notes.",
    )
    tasks = _tasks(
        tmp_path,
        [
            "The app shows a blank total and the right card does not update.",
            "A stale list keeps old rows after I click Add.",
        ],
    )
    report = lint_prompt_coverage(skill, tasks)
    assert report["stats"]["tasks"] == 2
    assert report["stats"]["uncovered"] == 2
    assert len(report["warnings"]) == 2
    assert "possible trigger gap" in report["warnings"][0]


def test_prompt_coverage_accepts_matching_language(tmp_path):
    skill = _skill(
        tmp_path,
        "Diagnose broken Shiny apps with stale reactive state and blank module totals.",
    )
    tasks = _tasks(
        tmp_path,
        ["The module total is blank and the reactive state stays stale."],
    )
    report = lint_prompt_coverage(skill, tasks)
    assert report["stats"]["uncovered"] == 0
    assert report["tasks"][0]["coverage"] > 0

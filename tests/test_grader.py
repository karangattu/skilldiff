from pathlib import Path

from skilldiff.config import GraderConfig
from skilldiff.grader import Grader, sanitize_text


def test_sanitize_text():
    text = "Using skill code-review with sonnet in treatment mode."
    sanitized = sanitize_text(text, ["code-review", "sonnet", "treatment"])
    assert "code-review" not in sanitized
    assert "sonnet" not in sanitized
    assert "treatment" not in sanitized
    assert "[REDACTED]" in sanitized


def test_grade_pair_exit_code(tmp_path: Path):
    ctrl_ws = tmp_path / "ctrl"
    treat_ws = tmp_path / "treat"
    ctrl_ws.mkdir()
    treat_ws.mkdir()

    cfg = GraderConfig(type="command", command="exit 0")
    grader = Grader(cfg, skill_name="test-skill", model_name="sonnet")

    grade_c, grade_t = grader.grade_pair(
        control_ws=ctrl_ws,
        treatment_ws=treat_ws,
        control_response="resp c",
        treatment_response="resp t",
        control_diff="",
        treatment_diff="",
        control_transcript="",
        treatment_transcript="",
    )

    assert grade_c.score == 1.0
    assert grade_c.success is True
    assert grade_t.score == 1.0
    assert grade_t.success is True
    assert {grade_c.label, grade_t.label} == {"candidate-A", "candidate-B"}


def test_grade_pair_json_output(tmp_path: Path):
    ctrl_ws = tmp_path / "ctrl"
    treat_ws = tmp_path / "treat"
    ctrl_ws.mkdir()
    treat_ws.mkdir()

    cfg = GraderConfig(type="command", command="echo '{\"score\": 0.85, \"success\": true}'")
    grader = Grader(cfg, skill_name="test-skill", model_name="sonnet")

    grade_c, grade_t = grader.grade_pair(
        control_ws=ctrl_ws,
        treatment_ws=treat_ws,
        control_response="resp c",
        treatment_response="resp t",
        control_diff="",
        treatment_diff="",
        control_transcript="",
        treatment_transcript="",
    )

    assert grade_c.score == 0.85
    assert grade_c.success is True
    assert grade_t.score == 0.85

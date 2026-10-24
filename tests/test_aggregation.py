from pathlib import Path

from skilldiff.aggregation import (
    _arm_labels,
    _run_summary,
    _run_warnings,
    _settings_summary,
    aggregate_baseline_comparisons,
    check_contamination,
)
from skilldiff.config import ClaudeConfig, ExperimentConfig


def test_arm_labels_presets():
    cfg_default = ExperimentConfig(name="exp", skill=None, models=["m"], tasks_patterns=[])
    assert _arm_labels(cfg_default, None) == {"control": "Control", "treatment": "Skill"}

    cfg_ab = ExperimentConfig(
        name="ab", skill=None, models=["m"], tasks_patterns=[], skill_a=Path("a"), skill_b=Path("b")
    )
    assert _arm_labels(cfg_ab, None) == {"control": "Skill A", "treatment": "Skill B"}

    cfg_comp = ExperimentConfig(
        name="comp", skill=None, models=["m"], tasks_patterns=[], preset="compression"
    )
    assert _arm_labels(cfg_comp, None) == {"control": "Original", "treatment": "Minified"}

    assert _arm_labels(cfg_default, {"mode": "correctness"}) == {
        "control": "Base (untouched)",
        "treatment": "PR (untouched)",
    }
    assert _arm_labels(cfg_default, {"mode": "agent"}) == {
        "control": "Without PR",
        "treatment": "With PR",
    }


def test_run_summary_formatting():
    assert _run_summary({"status": "timeout"}) == "TIMEOUT"
    assert (
        _run_summary({"status": "ok", "grade_status": "graded", "score": 0.85, "duration": 12.4})
        == "85% in 12s"
    )
    assert (
        _run_summary({"status": "ok", "grade_status": "ungraded", "duration": 5.0})
        == "ungraded in 5s"
    )
    assert (
        _run_summary({
            "status": "ok",
            "grade_status": "graded",
            "score": 1.0,
            "duration": 2.0,
            "arm": "treatment",
            "skill_invoked": False,
        })
        == "100% in 2s (skill unused)"
    )


def test_settings_summary():
    cfg = ExperimentConfig(
        name="test",
        skill=None,
        models=["m"],
        tasks_patterns=[],
        timeout_seconds=300,
        parallel=2,
        claude=ClaudeConfig(effort="low"),
    )
    settings = _settings_summary(cfg)
    assert settings["harness"] == "claude"
    assert settings["timeout_seconds"] == 300
    assert settings["parallel"] == 2
    assert settings["effort"] == "low"


def test_baseline_comparisons_use_the_primary_repetition_default():
    baseline = [{"model": "m", "task_id": "t", "score": 0.0, "status": "ok"}]
    control = [{"model": "m", "task_id": "t", "repetition": 1, "score": 1.0, "status": "ok"}]
    treatment = [{"model": "m", "task_id": "t", "repetition": 1, "score": 1.0, "status": "ok"}]
    comparisons, _ = aggregate_baseline_comparisons(baseline, control, treatment)
    assert comparisons["baseline_vs_a"]["pairs"] == 1
    assert comparisons["baseline_vs_b"]["pairs"] == 1


def test_check_contamination():
    valid, warnings = check_contamination(
        control_runs=[{"skill_available": True}],
        baseline_runs=[],
        comparison=None,
        is_skill_comparison=False,
    )
    assert not valid
    assert any("control run(s) had access to the skill" in w for w in warnings)

    valid_base, base_warnings = check_contamination(
        control_runs=[],
        baseline_runs=[{"skill_invoked": True}],
        comparison=None,
        is_skill_comparison=True,
    )
    assert not valid_base
    assert any("baseline run(s) loaded a skill revision" in w for w in base_warnings)


def test_experiment_reexports_aggregation():
    import skilldiff.experiment as exp

    assert exp._arm_labels is _arm_labels
    assert exp._run_summary is _run_summary
    assert exp._settings_summary is _settings_summary
    assert exp._run_warnings is _run_warnings

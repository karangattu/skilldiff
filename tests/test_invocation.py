"""Explicit vs implicit skill invocation.

``implicit`` leaves the prompt alone so adoption measures discovery.
``explicit`` names the skill so adoption measures obedience. The gap
between the two is what the SKILL.md description is worth.
"""

from pathlib import Path
from typing import Any

import pytest

from skilldiff.config import load_experiment, load_task
from skilldiff.invocation import (
    build_instruction,
    instruction_for_arm,
    skill_reference,
    template_names_skill,
)
from skilldiff.runner import AgentRunner

# --------------------------------------------------------------------- helpers


def test_default_instruction_names_the_skill():
    assert build_instruction(["shiny-doctor"]) == (
        "Use the `shiny-doctor` skill for this task."
    )


def test_default_instruction_handles_a_skill_pack():
    assert build_instruction(["a", "b"]) == "Use the skills `a`, `b` for this task."


def test_custom_template_substitutes_the_skill():
    assert build_instruction(["a"], "Read {skill} before you start.") == (
        "Read `a` before you start."
    )


def test_custom_template_without_the_placeholder_keeps_its_own_wording():
    assert build_instruction(["a"], "Use the available skill.") == (
        "Use the available skill."
    )
    assert template_names_skill("Use the available skill.") is False
    assert template_names_skill(None) is True
    assert template_names_skill("Use {skill}.") is True


def test_skill_reference_dedupes_and_quotes():
    assert skill_reference(["a", "a", "b"]) == "`a`, `b`"


def test_no_skill_means_no_instruction():
    assert instruction_for_arm("explicit", None, None) == ""
    assert instruction_for_arm("explicit", None, []) == ""


def test_implicit_mode_never_adds_an_instruction():
    assert instruction_for_arm("implicit", None, ["a"]) == ""


def test_task_setting_overrides_the_experiment_setting():
    assert instruction_for_arm("implicit", "explicit", ["a"]) == (
        "Use the `a` skill for this task."
    )
    assert instruction_for_arm("explicit", "implicit", ["a"]) == ""


# ---------------------------------------------------------------------- config


def _write_skill(root: Path, name: str) -> Path:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Helps with {name}.\n---\n\nBody.\n"
    )
    return skill_dir


def _write_task(root: Path, name: str, body: str = "") -> Path:
    task_path = root / f"{name}.yaml"
    task_path.write_text(f"id: {name}\nprompt: Do the thing.\n{body}")
    return task_path


def _write_config(root: Path, body: str) -> Path:
    config_path = root / "skilldiff.yaml"
    config_path.write_text(f"name: t\nmodels:\n  - claude-sonnet-5-5\n{body}")
    return config_path


def test_experiment_defaults_to_implicit(tmp_path: Path):
    _write_skill(tmp_path, "my-skill")
    _write_task(tmp_path, "t1")
    _write_config(tmp_path, "skill: ./skills/my-skill\ntasks:\n  - ./t*.yaml\n")
    config, tasks = load_experiment(tmp_path / "skilldiff.yaml")
    assert config.skill_invocation == "implicit"
    assert config.skill_invocation_prompt is None
    assert tasks[0].skill_invocation is None


def test_experiment_reads_the_invocation_mode(tmp_path: Path):
    _write_skill(tmp_path, "my-skill")
    _write_task(tmp_path, "t1")
    _write_config(
        tmp_path,
        "skill: ./skills/my-skill\nskill_invocation: explicit\ntasks:\n  - ./t*.yaml\n",
    )
    config, _ = load_experiment(tmp_path / "skilldiff.yaml")
    assert config.skill_invocation == "explicit"


def test_experiment_rejects_an_unknown_invocation_mode(tmp_path: Path):
    _write_skill(tmp_path, "my-skill")
    _write_task(tmp_path, "t1")
    _write_config(
        tmp_path,
        "skill: ./skills/my-skill\nskill_invocation: sometimes\ntasks:\n  - ./t*.yaml\n",
    )
    with pytest.raises(ValueError, match="skill_invocation must be"):
        load_experiment(tmp_path / "skilldiff.yaml")


def test_task_reads_its_own_invocation_mode(tmp_path: Path):
    task_path = _write_task(tmp_path, "t1", "skill_invocation: explicit\n")
    task = load_task(task_path)
    assert task.skill_invocation == "explicit"


def test_task_rejects_an_unknown_invocation_mode(tmp_path: Path):
    task_path = _write_task(tmp_path, "t1", "skill_invocation: maybe\n")
    with pytest.raises(ValueError, match="skill_invocation must be"):
        load_task(task_path)


def test_revision_runs_refuse_to_name_different_skills(tmp_path: Path):
    _write_skill(tmp_path, "skill-a")
    _write_skill(tmp_path, "skill-b")
    _write_task(tmp_path, "t1")
    _write_config(
        tmp_path,
        "skill_a: ./skills/skill-a\nskill_b: ./skills/skill-b\n"
        "skill_invocation: explicit\ntasks:\n  - ./t*.yaml\n",
    )
    with pytest.raises(ValueError, match="called differently"):
        load_experiment(tmp_path / "skilldiff.yaml")


def test_revision_runs_allow_a_template_that_does_not_name_the_skill(tmp_path: Path):
    _write_skill(tmp_path, "skill-a")
    _write_skill(tmp_path, "skill-b")
    _write_task(tmp_path, "t1")
    _write_config(
        tmp_path,
        "skill_a: ./skills/skill-a\nskill_b: ./skills/skill-b\n"
        'skill_invocation: explicit\nskill_invocation_prompt: "Use the available skill."\n'
        "tasks:\n  - ./t*.yaml\n",
    )
    config, _ = load_experiment(tmp_path / "skilldiff.yaml")
    assert config.skill_invocation == "explicit"


def test_task_level_explicit_also_blocks_a_revision_run(tmp_path: Path):
    _write_skill(tmp_path, "skill-a")
    _write_skill(tmp_path, "skill-b")
    _write_task(tmp_path, "t1", "skill_invocation: explicit\n")
    _write_config(
        tmp_path,
        "skill_a: ./skills/skill-a\nskill_b: ./skills/skill-b\ntasks:\n  - ./t*.yaml\n",
    )
    with pytest.raises(ValueError, match="called differently"):
        load_experiment(tmp_path / "skilldiff.yaml")


# ------------------------------------------------------------------ end to end


def _run_experiment(
    root: Path,
    monkeypatch,
    task_override: str | None = None,
    category: str = "intended",
    prompts: list[str] | None = None,
    **overrides,
) -> dict[str, Any]:
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setenv("SKILLDIFF_MOCK_RESPONSE", "Mocked")
    from skilldiff.config import ClaudeConfig, ExperimentConfig, GraderConfig, TaskConfig
    from skilldiff.experiment import ExperimentRunner
    from skilldiff.runner import AgentRunner

    root.mkdir(parents=True, exist_ok=True)
    skill_dir = root / "skills" / "my-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Helps.\n---\n\nBody.\n"
    )
    task_file = root / "task.yaml"
    task_file.write_text(f"id: t1\nprompt: Review the code\ncategory: {category}\n")
    task = TaskConfig(
        id="t1",
        prompt="Review the code",
        prompts=prompts or [],
        category=category,
        grader=GraderConfig(type="command", command="exit 0"),
        source_path=task_file,
        skill_invocation=task_override,
    )
    exp_cfg = ExperimentConfig(
        name="invocation-test",
        skill=skill_dir,
        models=["haiku"],
        tasks_patterns=["task.yaml"],
        runs=1,
        claude=ClaudeConfig(),
        **overrides,
    )
    runner = ExperimentRunner(
        exp_cfg, [task], output_dir=root / "runs", agent_runner=AgentRunner()
    )
    return runner.run()


def test_implicit_run_never_names_the_skill(tmp_path: Path, monkeypatch):
    results = _run_experiment(tmp_path, monkeypatch)
    treatment_prompt = results["runs"]["treatment"][0]["prompt"]
    control_prompt = results["runs"]["control"][0]["prompt"]
    assert "my-skill" not in treatment_prompt
    assert "my-skill" not in control_prompt
    assert results["skill_invocation"] == "implicit"
    assert results["settings"]["skill_invocation"] == "implicit"


def test_explicit_run_names_the_skill_in_the_skill_arm_only(tmp_path: Path, monkeypatch):
    results = _run_experiment(tmp_path, monkeypatch, skill_invocation="explicit")
    treatment_prompt = results["runs"]["treatment"][0]["prompt"]
    control_prompt = results["runs"]["control"][0]["prompt"]
    assert treatment_prompt.startswith("Use the `my-skill` skill for this task.")
    assert "Review the code" in treatment_prompt
    # The control arm has no skill, so it is never told to use one.
    assert "my-skill" not in control_prompt
    assert results["skill_invocation"] == "explicit"
    assert results["settings"]["skill_invocation"] == "explicit"


def test_explicit_run_labels_the_arm_and_the_report(tmp_path: Path, monkeypatch):
    results = _run_experiment(tmp_path, monkeypatch, skill_invocation="explicit")
    assert results["arm_labels"]["treatment"] == "Skill (named)"
    report_md = (Path(results["run_dir"]) / "report.md").read_text(encoding="utf-8")
    assert "Skill (named)" in report_md
    assert "prompt named the skill" in report_md


def test_explicit_run_changes_the_adoption_advice(tmp_path: Path, monkeypatch):
    from skilldiff.adoption import adoption_actions, adoption_summary

    results = _run_experiment(tmp_path, monkeypatch, skill_invocation="explicit")
    runs_by_arm = {
        "control": results["runs"]["control"],
        "treatment": results["runs"]["treatment"],
    }
    summary = adoption_summary(results, runs_by_arm)
    assert summary["invocation"] == "explicit"
    actions = adoption_actions(results, summary)
    for action in actions:
        assert "SKILL.md description" not in action["advice"]


def test_a_task_can_override_the_experiment_mode(tmp_path: Path, monkeypatch):
    from skilldiff.adoption import adoption_actions, adoption_summary
    from skilldiff.diagnose import diagnose_run

    monkeypatch.setenv("SKILLDIFF_MOCK_SKILL_INVOKED", "0")
    results = _run_experiment(
        tmp_path, monkeypatch, skill_invocation="implicit", task_override="explicit"
    )
    treatment_prompt = results["runs"]["treatment"][0]["prompt"]
    control_prompt = results["runs"]["control"][0]["prompt"]
    assert treatment_prompt.startswith("Use the `my-skill` skill for this task.")
    assert "my-skill" not in control_prompt
    # The experiment-level label still names the experiment's own mode.
    assert results["skill_invocation"] == "implicit"
    assert results["task_details"][0]["skill_invocation"] == "explicit"

    runs_by_arm = {
        "control": results["runs"]["control"],
        "treatment": results["runs"]["treatment"],
    }
    summary = adoption_summary(results, runs_by_arm)
    actions = adoption_actions(results, summary)
    desc_action = next(a for a in actions if a["kind"] == "description")
    assert "The prompt named" in desc_action["advice"]
    assert "SKILL.md description" not in desc_action["advice"]

    diag = diagnose_run(Path(results["run_dir"]))
    rec = next(r for r in diag["recommendations"] if r.startswith("Adoption:"))
    assert "The prompt named the skill" in rec
    assert "Refine the SKILL.md description" not in rec


def test_compare_reports_the_invocation_mode(tmp_path: Path, monkeypatch):
    from skilldiff.compare import compare_results, format_comparison

    implicit = _run_experiment(tmp_path / "a", monkeypatch)
    explicit = _run_experiment(tmp_path / "b", monkeypatch, skill_invocation="explicit")
    comp = compare_results(implicit, explicit)
    assert comp["invocation"] == {
        "a": "implicit",
        "b": "explicit",
        "a_overrides": {},
        "b_overrides": {},
    }
    text = format_comparison(comp)
    assert "Skill invocation: implicit → explicit" in text
    assert "mixes obedience with discovery" in text


class _RecordingRunner(AgentRunner):
    """Captures the prompt and the skill names each arm was run with."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    def terminate_all(self) -> None:
        pass

    def preflight(self, cwd, config, expected, runtime_probe=None):  # type: ignore[override]
        return {"ok": True}

    def run(self, prompt, cwd, model, config, **kwargs):  # type: ignore[override]
        self.calls.append(
            {"prompt": prompt, "skill_names": kwargs.get("skill_names"), "cwd": cwd}
        )
        from skilldiff.runner import RunResult

        text = prompt if isinstance(prompt, str) else prompt[0]
        return RunResult(
            prompt=text,
            response="ok",
            transcript="t",
            duration=1.0,
            cost=0.0,
            input_tokens=1,
            output_tokens=1,
            tool_calls=1,
            exit_code=0,
        )


def test_the_control_arm_still_watches_for_the_skill_it_does_not_have(
    tmp_path: Path, monkeypatch
):
    """Detection needs the skill name in every arm, or contamination goes unseen."""
    from skilldiff.config import ClaudeConfig, ExperimentConfig, GraderConfig, TaskConfig
    from skilldiff.experiment import ExperimentRunner

    skill_dir = tmp_path / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Helps.\n---\n\nBody.\n"
    )
    task_file = tmp_path / "task.yaml"
    task_file.write_text("id: t1\nprompt: Review the code\n")
    task = TaskConfig(
        id="t1",
        prompt="Review the code",
        grader=GraderConfig(type="command", command="exit 0"),
        source_path=task_file,
    )
    cfg = ExperimentConfig(
        name="detection",
        skill=skill_dir,
        models=["haiku"],
        tasks_patterns=["task.yaml"],
        runs=1,
        claude=ClaudeConfig(),
        skill_invocation="explicit",
    )
    agent_runner = _RecordingRunner()
    ExperimentRunner(
        cfg, [task], output_dir=tmp_path / "runs", agent_runner=agent_runner
    ).run()

    assert len(agent_runner.calls) == 2
    for call in agent_runner.calls:
        # Both arms are watched for the target skill, so a control arm that
        # loads it is still caught.
        assert call["skill_names"] == ["my-skill"]
    control = next(c for c in agent_runner.calls if "my-skill" not in str(c["prompt"]))
    assert "my-skill" not in str(control["prompt"])


def test_multi_turn_prompts_instruction_on_turn_one_only(tmp_path: Path):
    from skilldiff.config import ClaudeConfig, ExperimentConfig, GraderConfig, TaskConfig
    from skilldiff.experiment import ExperimentRunner

    skill_dir = tmp_path / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Helps.\n---\n\nBody.\n"
    )
    task_file = tmp_path / "task.yaml"
    task_file.write_text("id: t1\nprompts:\n  - turn 1\n  - turn 2\n  - turn 3\n")
    task = TaskConfig(
        id="t1",
        prompt="turn 1",
        prompts=["turn 1", "turn 2", "turn 3"],
        grader=GraderConfig(type="command", command="exit 0"),
        source_path=task_file,
    )
    cfg = ExperimentConfig(
        name="multi-turn-instruction",
        skill=skill_dir,
        models=["haiku"],
        tasks_patterns=["task.yaml"],
        runs=1,
        claude=ClaudeConfig(),
        skill_invocation="explicit",
    )
    agent_runner = _RecordingRunner()
    ExperimentRunner(
        cfg, [task], output_dir=tmp_path / "runs", agent_runner=agent_runner
    ).run()

    treatment = next(c for c in agent_runner.calls if "my-skill" in str(c["prompt"]))
    assert isinstance(treatment["prompt"], list)
    assert treatment["prompt"][0].startswith("Use the `my-skill` skill for this task.\n\nturn 1")
    assert treatment["prompt"][1] == "turn 2"
    assert treatment["prompt"][2] == "turn 3"
    assert "my-skill" not in treatment["prompt"][1]
    assert "my-skill" not in treatment["prompt"][2]


def test_pr_run_rejects_task_level_explicit_invocation(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _write_task(tmp_path, "t1", "skill_invocation: explicit\n")
    _write_config(
        tmp_path,
        "pr:\n  repo: ./repo\n  base: main\n  head: feature\ntasks:\n  - ./t1.yaml\n",
    )
    with pytest.raises(ValueError, match="skill_invocation 'explicit' does not apply to PR runs"):
        load_experiment(tmp_path / "skilldiff.yaml")


def test_pr_run_rejects_top_level_explicit_invocation(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _write_task(tmp_path, "t1")
    _write_config(
        tmp_path,
        "skill_invocation: explicit\n"
        "pr:\n"
        "  repo: ./repo\n"
        "  base: main\n"
        "  head: feature\n"
        "tasks:\n"
        "  - ./t1.yaml\n",
    )
    with pytest.raises(ValueError, match="skill_invocation 'explicit' does not apply to PR runs"):
        load_experiment(tmp_path / "skilldiff.yaml")


def test_all_tasks_explicit_helper():
    from skilldiff.adoption import all_tasks_explicit, task_invocation_mode

    results = {
        "skill_invocation": "implicit",
        "task_details": [
            {"id": "t1", "skill_invocation": "explicit"},
            {"id": "t2", "skill_invocation": "implicit"},
            {"id": "t3"},
        ],
    }
    assert all_tasks_explicit(results, []) is False
    assert task_invocation_mode(results, "t1") == "explicit"
    assert task_invocation_mode(results, "t2") == "implicit"
    assert task_invocation_mode(results, "t3") == "implicit"
    assert all_tasks_explicit(results, ["t1"]) is True
    assert all_tasks_explicit(results, ["t1", "t2"]) is False
    assert all_tasks_explicit(results, ["t2"]) is False


def test_over_triggered_diagnostics_respects_task_explicit(tmp_path: Path, monkeypatch):
    from skilldiff.adoption import adoption_actions, adoption_summary
    from skilldiff.diagnose import diagnose_run

    monkeypatch.setenv("SKILLDIFF_MOCK_SKILL_INVOKED", "1")
    results = _run_experiment(
        tmp_path,
        monkeypatch,
        skill_invocation="implicit",
        task_override="explicit",
        category="irrelevant",
    )
    runs_by_arm = {
        "control": results["runs"]["control"],
        "treatment": results["runs"]["treatment"],
    }
    summary = adoption_summary(results, runs_by_arm)
    actions = adoption_actions(results, summary)
    scope_action = next(a for a in actions if a["kind"] == "scope")
    assert "cannot measure over-triggering" in scope_action["advice"]
    assert "Narrow the description" not in scope_action["advice"]

    diag = diagnose_run(Path(results["run_dir"]))
    rec = next(r for r in diag["recommendations"] if r.startswith("Over-triggering:"))
    assert "cannot measure over-triggering" in rec
    assert "Add scoping boundaries" not in rec


def test_compare_warns_when_task_overrides_differ(tmp_path: Path, monkeypatch):
    from skilldiff.compare import compare_results, format_comparison

    a = _run_experiment(tmp_path / "a", monkeypatch, skill_invocation="implicit")
    b = _run_experiment(
        tmp_path / "b", monkeypatch, skill_invocation="implicit", task_override="explicit"
    )
    comp = compare_results(a, b)
    assert comp["invocation"]["a"] == "implicit"
    assert comp["invocation"]["b"] == "implicit"
    assert comp["invocation"]["a_overrides"] == {}
    assert comp["invocation"]["b_overrides"] == {"t1": "explicit"}

    text = format_comparison(comp)
    assert "Skill invocation: implicit → implicit" in text
    assert "The prompt named the skill in different tasks across the two runs" in text
    assert "mixes obedience with discovery" in text


def test_regrade_preserves_skill_invocation_prompt(tmp_path: Path, monkeypatch):
    import json

    from skilldiff.config import ClaudeConfig, ExperimentConfig, GraderConfig, TaskConfig
    from skilldiff.experiment import ExperimentRunner
    from skilldiff.runner import AgentRunner

    skill_dir = tmp_path / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Helps.\n---\n\nBody.\n"
    )
    task_file = tmp_path / "task.yaml"
    task_file.write_text("id: t1\nprompt: Review the code\n")
    task = TaskConfig(
        id="t1",
        prompt="Review the code",
        grader=GraderConfig(type="command", command="exit 0"),
        source_path=task_file,
    )
    cfg = ExperimentConfig(
        name="regrade-test",
        skill=skill_dir,
        models=["haiku"],
        tasks_patterns=["task.yaml"],
        runs=1,
        claude=ClaudeConfig(),
        skill_invocation="explicit",
        skill_invocation_prompt="Use the custom prompt template.",
    )
    runner = ExperimentRunner(
        cfg, [task], output_dir=tmp_path / "runs", agent_runner=AgentRunner()
    )
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setenv("SKILLDIFF_MOCK_RESPONSE", "Mocked")
    results = runner.run()
    run_dir = Path(results["run_dir"])
    assert results.get("skill_invocation_prompt") == "Use the custom prompt template."

    regrade_summary = runner.regrade(run_dir, dry_run=False)
    assert regrade_summary is not None
    regraded_results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert regraded_results.get("skill_invocation_prompt") == "Use the custom prompt template."

import json
from pathlib import Path

from skilldiff.config import ClaudeConfig, ExperimentConfig, GraderConfig, TaskConfig
from skilldiff.experiment import ExperimentRunner
from skilldiff.runner import AgentRunner


def test_experiment_runner_end_to_end(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setenv("SKILLDIFF_MOCK_RESPONSE", "Found 2 defects")
    monkeypatch.setenv("SKILLDIFF_MOCK_COST", "0.25")
    monkeypatch.setenv("SKILLDIFF_MOCK_DURATION", "10.0")

    skill_dir = tmp_path / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# My Skill")

    task_file = tmp_path / "task.yaml"
    task_file.write_text("id: t1\n")
    task = TaskConfig(
        id="t1",
        prompt="Review auth",
        grader=GraderConfig(type="command", command="exit 0"),
        source_path=task_file,
    )

    exp_cfg = ExperimentConfig(
        name="test-run",
        skill=skill_dir,
        models=["haiku"],
        tasks_patterns=["task.yaml"],
        runs=2,
        claude=ClaudeConfig(),
    )

    runs_out = tmp_path / "runs"
    runner = ExperimentRunner(
        exp_cfg, [task], output_dir=runs_out, agent_runner=AgentRunner()
    )
    results = runner.run()

    assert results["name"] == "test-run"
    assert results["runs_per_arm"] == 2
    assert "haiku" in results["by_model"]
    assert results["by_model"]["haiku"]["by_task"]["t1"]["control"]["total_count"] == 2
    assert "runs" in results
    assert "runs" in results["by_model"]["haiku"]
    assert len(results["runs"]["control"]) == 2

    run_dir = Path(results["run_dir"])
    assert (run_dir / "experiment.json").exists()
    assert (run_dir / "results.json").exists()
    assert (run_dir / "report.qmd").exists()
    assert (run_dir / "haiku" / "t1" / "control" / "001" / "run.json").exists()
    assert (run_dir / "haiku" / "t1" / "control" / "001" / "diff.patch").exists()
    assert (run_dir / "haiku" / "t1" / "treatment" / "001" / "run.json").exists()

    with open(run_dir / "haiku" / "t1" / "treatment" / "001" / "run.json") as f:
        run_data = json.load(f)
        assert run_data["score"] == 1.0
        assert run_data["cost"] == 0.25


def _setup(tmp_path: Path, runs: int = 2, **cfg_kwargs):
    skill_dir = tmp_path / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: my-skill\ndescription: d\n---\n")
    fixture = tmp_path / "fixture"
    (fixture / ".claude" / "skills" / "my-skill").mkdir(parents=True)
    (fixture / ".claude" / "skills" / "my-skill" / "SKILL.md").write_text("stale copy")
    (fixture / "app.py").write_text("print('hi')\n")
    task_file = tmp_path / "task.yaml"
    task_file.write_text("id: t1\n")
    task = TaskConfig(id="t1", prompt="Do it", repo="fixture", source_path=task_file)
    cfg = ExperimentConfig(
        name="exp", skill=skill_dir, models=["haiku"], tasks_patterns=[], runs=runs,
        **cfg_kwargs,
    )
    return cfg, task


def test_experiment_cleans_control_and_reports_warnings(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    # The mock reports the skill as used whenever a skills dir exists, so the stale
    # copy in the fixture must be removed from control for this to come out clean.
    cfg, task = _setup(tmp_path)
    messages: list[str] = []
    results = ExperimentRunner(
        cfg, [task], output_dir=tmp_path / "runs", progress=messages.append
    ).run()

    control = results["runs"]["control"]
    assert all(r["skill_invoked"] is False for r in control)
    assert all(r["removed_from_fixture"] for r in control)
    assert {r["run_order"] for r in control} <= {1, 2}
    assert any("already contained the skill" in w for w in results["warnings"])
    assert any("No grader is configured" in w for w in results["warnings"])
    assert results["overall"]["paired"]["pairs"] == 2
    assert results["by_model"]["haiku"]["skill"]["skill_used_count"] == 2
    assert len([m for m in messages if m.startswith("[")]) == 2
    run_dir = Path(results["run_dir"])
    for name in ("report.html", "report.md", "report.qmd", "results.json"):
        assert (run_dir / name).exists()


def test_experiment_parallel_and_skill_pack(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    pack = tmp_path / "pack"
    for name in ("alpha", "beta"):
        (pack / name).mkdir(parents=True)
        (pack / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n")
    monkeypatch.setenv(
        "SKILLDIFF_MOCK_SCRIPT", "ls .claude/skills 2>/dev/null > installed.txt || true"
    )
    task_file = tmp_path / "task.yaml"
    task_file.write_text("id: t1\n")
    task = TaskConfig(
        id="t1",
        prompt="x",
        source_path=task_file,
        grader=GraderConfig(command="grep -q alpha installed.txt && grep -q beta installed.txt"),
    )
    cfg = ExperimentConfig(
        name="pack", skill=pack, models=["m"], tasks_patterns=[], runs=4, parallel=3
    )
    assert cfg.skill_names == ["alpha", "beta"]
    results = ExperimentRunner(cfg, [task], output_dir=tmp_path / "runs").run()
    assert [r["repetition"] for r in results["runs"]["treatment"]] == [1, 2, 3, 4]
    assert all(r["success"] for r in results["runs"]["treatment"])
    assert not any(r["success"] for r in results["runs"]["control"])

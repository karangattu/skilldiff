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

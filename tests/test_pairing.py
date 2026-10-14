import json
from pathlib import Path

from skilldiff.pairing import (
    _Pair,
    balanced_arm_order,
    balanced_three_order,
    postprocess_grade,
    safe_path_component,
    save_run_artifacts,
)


def test_safe_path_component():
    assert safe_path_component("claude-3.5-sonnet") == "claude-3.5-sonnet"
    assert safe_path_component("task/with/slashes") == "task_with_slashes"
    assert safe_path_component("__invalid__") == "invalid"
    assert safe_path_component("") == "item"


def test_balanced_arm_order_rotation():
    order1 = balanced_arm_order("model", "task", 1, 42)
    order2 = balanced_arm_order("model", "task", 2, 42)
    assert set(order1) == {"control", "treatment"}
    assert set(order2) == {"control", "treatment"}
    assert order1 != order2


def test_balanced_three_order_rotation():
    order1 = balanced_three_order("model", "task", 1, 42)
    order2 = balanced_three_order("model", "task", 2, 42)
    order3 = balanced_three_order("model", "task", 3, 42)
    assert set(order1) == {"control", "treatment", "baseline"}
    assert len(order1) == 3
    # First arm should rotate across repetitions
    first_arms = {order1[0], order2[0], order3[0]}
    assert len(first_arms) == 3


def test_postprocess_grade_harness_error():
    rec = {
        "status": "error",
        "failure_kind": "harness_error",
        "score": 1.0,
        "success": True,
        "grade_status": "graded",
    }
    postprocess_grade(rec)
    assert rec["score"] is None
    assert rec["partial_score"] == 1.0
    assert rec["success"] is False
    assert rec["grade_status"] == "ungraded"
    assert rec["analysis_excluded"] is True


def test_postprocess_grade_agent_failure_policy():
    rec = {
        "status": "error",
        "failure_kind": "timeout",
        "score": 0.8,
        "success": True,
        "grade_status": "graded",
    }
    postprocess_grade(rec, {"agent_failure": "zero"})
    assert rec["partial_score"] == 0.8
    assert rec["score"] == 0.0

    rec2 = {
        "status": "error",
        "failure_kind": "timeout",
        "score": 0.8,
        "success": True,
        "grade_status": "graded",
    }
    postprocess_grade(rec2, {"agent_failure": "exclude"})
    assert rec2["analysis_excluded"] is True


def test_save_run_artifacts(tmp_path: Path):
    arm_dir = tmp_path / "arm"
    arm_dir.mkdir()
    record = {"status": "ok", "score": 1.0}
    save_run_artifacts(arm_dir, record, "transcript content", "diff content")

    assert (arm_dir / "transcript.txt").read_text() == "transcript content"
    assert (arm_dir / "diff.patch").read_text() == "diff content"
    assert (arm_dir / "run.json").exists()
    saved_record = json.loads((arm_dir / "run.json").read_text())
    assert "artifact_hashes" in saved_record
    assert "transcript.txt" in saved_record["artifact_hashes"]
    assert "diff.patch" in saved_record["artifact_hashes"]


def test_experiment_reexports_pairing():
    import skilldiff.experiment as exp

    assert exp._Pair is _Pair
    assert exp.balanced_arm_order is balanced_arm_order
    assert exp.balanced_three_order is balanced_three_order
    assert exp.safe_path_component is safe_path_component


def test_looks_like_rate_limit():
    from skilldiff.runner import looks_like_rate_limit

    assert looks_like_rate_limit("Rate limit exceeded")
    assert looks_like_rate_limit("Error 429 Too Many Requests")
    assert looks_like_rate_limit("Resource exhausted by quota")
    assert looks_like_rate_limit("Server is overloaded, try again later")
    assert not looks_like_rate_limit("Syntax error in line 4")
    assert not looks_like_rate_limit("Success")


def test_arm_parallel_experiment_runner(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setenv("SKILLDIFF_MOCK_RESPONSE", "Mocked")
    from skilldiff.config import ClaudeConfig, ExperimentConfig, GraderConfig, TaskConfig
    from skilldiff.experiment import ExperimentRunner
    from skilldiff.runner import AgentRunner

    skill_dir = tmp_path / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# My Skill")

    task_file = tmp_path / "task.yaml"
    task_file.write_text("id: t1\n")
    task = TaskConfig(
        id="t1",
        prompt="Review code",
        grader=GraderConfig(type="command", command="exit 0"),
        source_path=task_file,
    )

    exp_cfg = ExperimentConfig(
        name="test-arm-parallel",
        skill=skill_dir,
        models=["haiku"],
        tasks_patterns=["task.yaml"],
        runs=1,
        arm_parallel=True,
        claude=ClaudeConfig(),
    )

    runs_out = tmp_path / "runs"
    runner = ExperimentRunner(exp_cfg, [task], output_dir=runs_out, agent_runner=AgentRunner())
    results = runner.run()

    assert results["name"] == "test-arm-parallel"
    assert results["settings"].get("arm_parallel") is True
    assert len(results["runs"]["control"]) == 1
    assert len(results["runs"]["treatment"]) == 1
    assert results["runs"]["control"][0]["status"] == "ok"


def test_run_pair_rate_limit_retry(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    from skilldiff.config import ClaudeConfig, ExperimentConfig, GraderConfig, TaskConfig
    from skilldiff.experiment import ExperimentRunner
    from skilldiff.pairing import _Pair, run_pair
    from skilldiff.runner import AgentRunner, RunResult

    skill_dir = tmp_path / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# My Skill")

    task = TaskConfig(
        id="t1",
        prompt="Review code",
        grader=GraderConfig(type="command", command="exit 0"),
        source_path=tmp_path / "task.yaml",
    )

    exp_cfg = ExperimentConfig(
        name="test-retry",
        skill=skill_dir,
        models=["haiku"],
        tasks_patterns=["task.yaml"],
        runs=1,
        arm_parallel=True,
        claude=ClaudeConfig(),
    )

    from skilldiff.snapshots import grader_inputs

    runner = ExperimentRunner(
        exp_cfg, [task], output_dir=tmp_path / "runs", agent_runner=AgentRunner()
    )
    runner._grader_inputs = grader_inputs(runner.tasks)

    calls = 0

    def mock_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return RunResult(
                prompt="p",
                response="",
                transcript="429 Too Many Requests",
                duration=0.1,
                cost=0.0,
                input_tokens=0,
                output_tokens=0,
                tool_calls=0,
                exit_code=1,
                error="rate_limit: 429",
                status="error",
                failure_kind="rate_limit",
            )
        return RunResult(
            prompt="p",
            response="OK",
            transcript="OK",
            duration=0.1,
            cost=0.01,
            input_tokens=10,
            output_tokens=5,
            tool_calls=1,
            exit_code=0,
            status="ok",
        )

    runner.agent_runner.run = mock_run
    pair = _Pair(model="haiku", task=task, repetition=1)
    ctrl, treat = run_pair(runner, pair, tmp_path / "run_root")

    assert calls >= 2
    assert len(runner._retry_history) >= 1
    assert runner._retry_history[0]["status"] == "rate_limit"


def test_save_run_artifacts_redacts_secrets(tmp_path: Path, monkeypatch):
    from skilldiff.pairing import save_run_artifacts

    monkeypatch.setenv("MY_CUSTOM_SECRET", "super-secret-pass-99")
    arm_dir = tmp_path / "arm"
    arm_dir.mkdir()
    record = {
        "status": "error",
        "error": "Failed with token sk-ant-api03-abcdef1234567890123456 and super-secret-pass-99",
        "transcript": "Prompt leaked Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz",
    }
    raw_transcript = "Traceback: sk-proj-12345678901234567890 super-secret-pass-99"
    save_run_artifacts(arm_dir, record, raw_transcript, "diff")

    clean_t = (arm_dir / "transcript.txt").read_text()
    assert "sk-proj-" not in clean_t
    assert "super-secret-pass-99" not in clean_t
    assert "[REDACTED]" in clean_t

    import json

    saved_rec = json.loads((arm_dir / "run.json").read_text())
    assert "sk-ant-api03-" not in saved_rec["error"]
    assert "super-secret-pass-99" not in saved_rec["error"]
    assert "[REDACTED]" in saved_rec["error"]
    assert "Bearer eyJ" not in saved_rec["transcript"]
    assert "[REDACTED]" in saved_rec["transcript"]


def test_run_pair_guarantees_workspace_cleanup(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    from skilldiff.config import ClaudeConfig, ExperimentConfig, GraderConfig, TaskConfig
    from skilldiff.experiment import ExperimentRunner
    from skilldiff.pairing import _Pair, run_pair
    from skilldiff.runner import AgentRunner

    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# S")

    task = TaskConfig(
        id="t_cleanup",
        prompt="do it",
        grader=GraderConfig(type="command", command="exit 0"),
        source_path=tmp_path / "task.yaml",
    )
    cfg = ExperimentConfig(
        name="cleanup-test",
        skill=skill_dir,
        models=["m"],
        tasks_patterns=["*.yaml"],
        runs=1,
        claude=ClaudeConfig(),
    )
    runner = ExperimentRunner(
        cfg, [task], output_dir=tmp_path / "runs", agent_runner=AgentRunner()
    )

    created_workspaces = []

    def mock_run(prompt, cwd, *args, **kwargs):
        created_workspaces.append(cwd)
        raise RuntimeError("simulated failure during agent execution")

    runner.agent_runner.run = mock_run
    pair = _Pair(model="m", task=task, repetition=1)

    import pytest

    with pytest.raises(RuntimeError, match="simulated failure"):
        run_pair(runner, pair, tmp_path / "run_root")

    assert len(created_workspaces) >= 1
    for ws_root in created_workspaces:
        assert not ws_root.exists()


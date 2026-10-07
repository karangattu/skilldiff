import json
import sys
from pathlib import Path

import pytest

from skilldiff.config import (
    ExperimentConfig,
    GraderConfig,
    load_experiment,
    load_task,
)
from skilldiff.diagnose import diagnose_run
from skilldiff.grader import Grader, check_blast_radius
from skilldiff.linter import lint_skill
from skilldiff.profiler import compute_context_tax, measure_skill_footprint
from skilldiff.runner import AgentRunner


def test_check_blast_radius_allowed_and_forbidden():
    err = check_blast_radius(
        ["src/app.py", "src/utils.py"],
        allowed_paths=["src/*"],
        forbidden_paths=["*.key"],
    )
    assert err is None

    err = check_blast_radius(
        ["src/app.py", "secrets.key"],
        allowed_paths=["src/*"],
        forbidden_paths=[],
    )
    assert err is not None
    assert "outside allowed paths" in err

    err = check_blast_radius(
        ["src/app.py", "src/secrets.key"],
        allowed_paths=["src/*"],
        forbidden_paths=["*.key", "src/*.key"],
    )
    assert err is not None
    assert "forbidden path" in err


def test_grader_blast_radius_enforcement(tmp_path: Path):
    ctrl_ws = tmp_path / "ctrl"
    treat_ws = tmp_path / "treat"
    ctrl_ws.mkdir()
    treat_ws.mkdir()

    cfg = GraderConfig(type="command", command="exit 0")
    grader = Grader(
        cfg,
        skill_name="test-skill",
        model_name="sonnet",
        allowed_paths=["src/*"],
    )

    grade_c, grade_t = grader.grade_pair(
        control_ws=ctrl_ws,
        treatment_ws=treat_ws,
        control_response="resp c",
        treatment_response="resp t",
        control_diff="",
        treatment_diff="",
        control_transcript="",
        treatment_transcript="",
        control_changed_files=["src/main.py"],
        treatment_changed_files=["outside.txt"],
    )

    assert grade_c.success is True
    assert grade_t.success is False
    assert "outside allowed paths" in grade_t.feedback


def test_grader_llm_rubric_evaluation(tmp_path: Path):
    ctrl_ws = tmp_path / "ctrl"
    treat_ws = tmp_path / "treat"
    ctrl_ws.mkdir()
    treat_ws.mkdir()

    cmd = 'echo \'{"score": 0.85, "success": true, "feedback": "Meets rubric"}\''
    cfg = GraderConfig(type="llm", rubric="Verify correct solution", command=cmd)
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
    assert grade_t.success is True

    cfg_fallback = GraderConfig(type="llm", rubric="Verify fallback")
    grader_fallback = Grader(cfg_fallback, skill_name="test-skill", model_name="sonnet")
    grade_c_fb, _ = grader_fallback.grade_pair(
        control_ws=ctrl_ws,
        treatment_ws=treat_ws,
        control_response="resp c",
        treatment_response="resp t",
        control_diff="",
        treatment_diff="",
        control_transcript="",
        treatment_transcript="",
    )
    assert grade_c_fb.score is None
    assert grade_c_fb.grade_status == "error"
    assert "judge command" in grade_c_fb.feedback


def test_task_config_extensions(tmp_path: Path):
    task_file = tmp_path / "task.yaml"
    task_file.write_text(
        "prompt: Single prompt\n"
        "prompts:\n"
        "  - 'Turn 1'\n"
        "  - 'Turn 2'\n"
        "allowed_paths:\n"
        "  - 'src/**'\n"
        "forbidden_paths:\n"
        "  - '.env'\n"
        "grader:\n"
        "  type: llm\n"
        "  command: python judge.py\n"
        "  rubric: 'Check quality'\n"
    )
    task = load_task(task_file)
    assert task.prompts == ["Turn 1", "Turn 2"]
    assert task.allowed_paths == ["src/**"]
    assert task.forbidden_paths == [".env"]
    assert task.grader.type == "llm"
    assert task.grader.rubric == "Check quality"


def test_experiment_config_extensions(tmp_path: Path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "t1.yaml").write_text("prompt: hello\n")

    exp_file = tmp_path / "experiment.yaml"
    exp_file.write_text(
        "name: test-exp\n"
        "skill: ./skill\n"
        "models:\n"
        "  - sonnet\n"
        "tasks:\n"
        "  - tasks/*.yaml\n"
        "isolation: docker\n"
        "container_image: python:3.11-slim\n"
    )
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: skill\ndescription: demo skill here for test\n---\n"
    )

    exp, tasks = load_experiment(exp_file)
    assert exp.isolation == "docker"
    assert exp.container_image == "python:3.11-slim"
    assert len(tasks) == 1


def test_harnesses_key_is_rejected(tmp_path: Path):
    """`harnesses:` never drove the runner; refuse it instead of ignoring it."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "t1.yaml").write_text("prompt: hello\n")

    exp_file = tmp_path / "experiment.yaml"
    exp_file.write_text(
        "name: test-exp\n"
        "skill: ./skill\n"
        "models:\n"
        "  - sonnet\n"
        "tasks:\n"
        "  - tasks/*.yaml\n"
        "harnesses:\n"
        "  - claude\n"
        "  - codex\n"
    )
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: skill\ndescription: demo skill here for test\n---\n"
    )

    with pytest.raises(ValueError, match="harnesses is not supported"):
        load_experiment(exp_file)


def test_runner_multi_turn(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SKILLDIFF_MOCK_RUNNER", raising=False)
    agent = tmp_path / "agent"
    agent.write_text(
        f"#!{sys.executable}\n"
        "import json,pathlib\n"
        "state=pathlib.Path('turn.txt')\n"
        "turn=int(state.read_text())+1 if state.exists() else 1\n"
        "state.write_text(str(turn))\n"
        "if turn==1:\n"
        " print(json.dumps({'type':'assistant','message':{'content':[{"
        "'type':'tool_use','name':'Skill','input':{'skill':'my-skill'}}]}}))\n"
        "print(json.dumps({'type':'result','result':'Created' if turn==1 else 'Modified',"
        "'total_cost_usd':0.01 if turn==1 else 0.02,'num_turns':1,"
        "'usage':{'input_tokens':100 if turn==1 else 150,"
        "'output_tokens':50 if turn==1 else 80}}))\n",
        encoding="utf-8",
    )
    agent.chmod(0o755)
    config = ExperimentConfig(name="test", skill=None, models=["sonnet"], tasks_patterns=[])
    config.claude.bin_path = str(agent)
    result = AgentRunner().run(
        ["Step 1: Create file", "Step 2: Modify file"], tmp_path, "test-model", config,
        skill_names=["my-skill"],
    )
    assert result.status == "ok"
    assert result.skill_invoked is True
    assert result.cost == 0.03
    assert result.input_tokens == 250
    assert result.output_tokens == 130
    assert result.num_turns == 2
    assert result.response == "Modified"
    assert "TURN 1" in result.transcript
    assert "TURN 2" in result.transcript


def test_measure_skill_footprint(tmp_path: Path):
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: my-skill\ndescription: Test skill description.\n---\nBody content."
    )

    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "helper.py").write_text("print('hello world')\n")

    footprint = measure_skill_footprint(skill_dir)
    assert footprint["file_count"] == 2
    assert footprint["estimated_tokens"] > 0
    assert footprint["total_bytes"] > 0


def test_compute_context_tax(tmp_path: Path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: s\ndescription: d\n---\n" + ("word " * 100)
    )

    footprint = measure_skill_footprint(skill_dir)
    tax = compute_context_tax(footprint, median_turns=3.0, run_count=4)
    assert tax["static_tokens"] > 0
    assert tax["median_turns"] == 3.0
    assert tax["session_tax_tokens"] == tax["static_tokens"] * 3
    assert tax["total_experiment_tokens"] == tax["session_tax_tokens"] * 4


def test_lint_skill(tmp_path: Path):
    valid_skill = tmp_path / "valid-skill"
    valid_skill.mkdir()
    (valid_skill / "SKILL.md").write_text(
        "---\nname: valid-skill\ndescription: Comprehensive valid description for test.\n"
        "---\n# Heading\nContent here.\n"
    )
    res = lint_skill(valid_skill)
    assert res.valid is True
    assert len(res.errors) == 0

    invalid_skill = tmp_path / "invalid-skill"
    invalid_skill.mkdir()
    (invalid_skill / "SKILL.md").write_text(
        "---\ndescription: Missing name\n---\n```python\nunclosed code block"
    )
    res_inv = lint_skill(invalid_skill)
    assert res_inv.valid is False
    assert any("name" in e.lower() for e in res_inv.errors)
    assert any("code fence" in e.lower() for e in res_inv.errors)


def test_diagnose_run(tmp_path: Path):
    report_data = {
        "metrics": {
            "control": {"success_rate": 0.9, "total_input_tokens": 1000},
            "treatment": {"success_rate": 0.4, "total_input_tokens": 3000},
        },
        "runs": {
            "control": [
                {
                    "task_id": "task_1",
                    "model": "sonnet",
                    "repetition": 1,
                    "success": True,
                    "score": 1.0,
                    "input_tokens": 1000,
                }
            ],
            "treatment": [
                {
                    "task_id": "task_1",
                    "model": "sonnet",
                    "repetition": 1,
                    "task_category": "intended",
                    "skill_invoked": False,
                    "success": False,
                    "score": 0.0,
                    "input_tokens": 3000,
                    "feedback": "Blast radius violation: modified forbidden path 'test.key'",
                }
            ],
        },
        "context_tax": {
            "static_skill_tokens": 6000,
            "delta_input_tokens": 2000,
        },
    }
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps(report_data))

    diag = diagnose_run(results_path)
    assert len(diag["under_triggered"]) == 1
    assert len(diag["blast_violations"]) == 1
    assert len(diag["regressions"]) == 1
    assert len(diag["token_bloat_tasks"]) == 1
    assert len(diag["recommendations"]) > 0


def test_diagnose_ignores_partial_agent_failure_comparisons(tmp_path: Path):
    failed = {
        "task_id": "task_1", "model": "m", "repetition": 1,
        "status": "error", "score": 1.0, "input_tokens": 100,
    }
    completed = {
        "task_id": "task_1", "model": "m", "repetition": 1,
        "status": "ok", "score": 0.0, "input_tokens": 500,
    }
    path = tmp_path / "results.json"
    path.write_text(json.dumps({
        "failure_policy": {"agent_failure": "exclude"},
        "runs": {"control": [failed], "treatment": [completed]},
    }))
    diag = diagnose_run(path)
    assert diag["regressions"] == []
    assert diag["token_bloat_tasks"] == []


def test_cli_lint_and_diagnose(tmp_path: Path, capsys):
    from skilldiff.cli import cmd_diagnose, cmd_lint

    class Args:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: Comprehensive description for testing linter.\n"
        "---\nBody here.\n"
    )

    ret_lint = cmd_lint(Args(skill_dir=str(skill_dir), json=False))
    assert ret_lint == 0
    captured = capsys.readouterr()
    assert "passes all lint checks" in captured.out

    res_dir = tmp_path / "run_dir"
    res_dir.mkdir()
    (res_dir / "results.json").write_text(json.dumps({
        "metrics": {"control": {}, "treatment": {}},
        "runs": {"control": [], "treatment": []},
    }))

    ret_diag = cmd_diagnose(Args(run_dir=str(res_dir), config=None, json=False))
    assert ret_diag == 0
    captured_diag = capsys.readouterr()
    assert "Diagnosis for" in captured_diag.out

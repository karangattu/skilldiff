import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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
from skilldiff.runner import AgentRunner, RunResult


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
    assert grade_c_fb.score == 1.0
    assert "Rubric: Verify fallback" in grade_c_fb.feedback


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
        "harnesses:\n"
        "  - claude\n"
        "  - codex\n"
        "isolation: docker\n"
        "container_image: python:3.11-slim\n"
    )
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: skill\ndescription: demo skill here for test\n---\n"
    )

    exp, tasks = load_experiment(exp_file)
    assert exp.harnesses == ["claude", "codex"]
    assert exp.isolation == "docker"
    assert exp.container_image == "python:3.11-slim"
    assert len(tasks) == 1


def test_runner_multi_turn(tmp_path: Path):
    runner = AgentRunner()
    prompts = ["Step 1: Create file", "Step 2: Modify file"]

    with patch.object(runner, "run") as mock_run:
        mock_run.side_effect = [
            RunResult(
                prompt="Step 1: Create file",
                response="Created file",
                transcript="turn 1 transcript",
                duration=1.2,
                cost=0.01,
                input_tokens=100,
                output_tokens=50,
                tool_calls=1,
                exit_code=0,
                status="ok",
                skill_invoked=True,
            ),
            RunResult(
                prompt="Step 2: Modify file",
                response="Modified file",
                transcript="turn 2 transcript",
                duration=1.8,
                cost=0.02,
                input_tokens=150,
                output_tokens=80,
                tool_calls=1,
                exit_code=0,
                status="ok",
                skill_invoked=False,
            ),
        ]

        exp_config = ExperimentConfig(
            name="test",
            skill=tmp_path,
            models=["sonnet"],
            tasks_patterns=["*.yaml"],
        )
        res = runner._run_multi_turn(
            prompts=prompts,
            cwd=tmp_path,
            model="test-model",
            config=exp_config,
        )

        assert res.status == "ok"
        assert res.skill_invoked is True
        assert res.cost == 0.03
        assert res.input_tokens == 250
        assert res.output_tokens == 130
        assert "TURN 1" in res.transcript
        assert "TURN 2" in res.transcript


def test_runner_container_isolation(tmp_path: Path):
    runner = AgentRunner()
    cmd = ["echo", "hello"]

    with patch("shutil.which", return_value="/usr/local/bin/docker"), patch(
        "subprocess.Popen"
    ) as mock_popen, patch("skilldiff.runner._kill_group"):
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        runner._exec(
            cmd,
            cwd=tmp_path,
            env={},
            timeout=10.0,
            isolation="docker",
            container_image="custom-image:1.0",
        )

        called_cmd = mock_popen.call_args[0][0]
        assert called_cmd[0] == "docker"
        assert called_cmd[1] == "run"
        assert "custom-image:1.0" in called_cmd
        assert "echo" in called_cmd


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

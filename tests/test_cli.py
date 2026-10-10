import json
from pathlib import Path

import yaml

from skilldiff.cli import _format_results, cmd_check, cmd_init, cmd_results, cmd_run


class Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_terminal_summary_uses_matched_eligible_pairs():
    def run(arm, rep, score, duration, status="ok"):
        return {
            "arm": arm, "model": "m", "task_id": "t", "repetition": rep,
            "status": status, "score": score, "success": score == 1.0,
            "duration": duration, "input_tokens": 100, "output_tokens": 50,
        }

    results = {
        "name": "partial", "models": ["m"], "tasks_count": 1, "runs_per_arm": 2,
        "by_model": {"m": {
            "control": {"task_score": 1.0, "median_time": 10.0},
            "skill": {"task_score": 0.5, "median_time": 55.0},
        }},
        "runs": {
            "control": [run("control", 1, 1.0, 10.0), run("control", 2, 1.0, 500.0, "error")],
            "treatment": [run("treatment", 1, 1.0, 10.0), run("treatment", 2, 1.0, 100.0)],
        },
        "failure_policy": {"agent_failure": "exclude"},
    }
    output = _format_results(results)
    score_line = next(line for line in output.splitlines() if line.startswith("Task score"))
    time_line = next(line for line in output.splitlines() if line.startswith("Time (median)"))
    success_line = next(line for line in output.splitlines() if line.startswith("Success"))

    assert score_line.count("100%") == 2
    assert "0 pp" in score_line
    assert time_line.count("10s") >= 2
    assert "55s" not in time_line
    assert success_line.count("1/1") == 2
    assert " 0 " in success_line
    assert "| t | Skill | 100% | 10s |" in output


def test_cli_init_and_overwrite(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    args = Args(force=False)
    ret = cmd_init(args)
    assert ret == 0
    assert (tmp_path / "skilldiff.yaml").exists()
    assert (tmp_path / "tasks" / "changelog-entry.yaml").exists()
    assert (tmp_path / "skills" / "changelog-style" / "SKILL.md").exists()
    assert (tmp_path / "fixtures" / "changelog" / "CHANGELOG.md").exists()
    assert (tmp_path / "graders" / "changelog_entry.py").exists()
    generated_config = yaml.safe_load((tmp_path / "skilldiff.yaml").read_text())
    assert generated_config["claude"]["auth"] == "subscription"
    assert generated_config["claude"]["permission_mode"] == "acceptEdits"
    assert generated_config["claude"]["allowed_tools"] == []
    assert generated_config["claude"]["isolate"] is True

    ret_no_force = cmd_init(args)
    assert ret_no_force == 1

    args_force = Args(force=True)
    ret_force = cmd_init(args_force)
    assert ret_force == 0


def test_cli_run_and_results(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")

    cmd_init(Args(force=False))

    cfg_file = tmp_path / "skilldiff.yaml"
    cfg_file.write_text(
        "name: test-exp\nskill: ./skills/changelog-style\n"
        "models:\n  - sonnet\ntasks:\n  - ./tasks/*.yaml\nruns: 1\n"
    )

    ret_run = cmd_run(Args(config="skilldiff.yaml", runs=1))
    assert ret_run == 0

    captured = capsys.readouterr()
    assert "test-exp" in captured.out
    assert "Task score" in captured.out
    assert "[1/1] sonnet · changelog-entry · run 1" in captured.out
    assert "Report:" in captured.out
    assert "report.html" in captured.out
    assert "| App | Arm | Score | Time | Input | Cached input |" in captured.out
    assert "| changelog-entry | Control |" in captured.out
    assert "| changelog-entry | Skill |" in captured.out

    ret_results = cmd_results(Args(run_dir=None, json=False))
    assert ret_results == 0
    captured_res = capsys.readouterr()
    assert "test-exp" in captured_res.out
    assert "Report:" in captured_res.out
    assert "| App | Arm | Score | Time | Input | Cached input |" in captured_res.out

    ret_json = cmd_results(Args(run_dir=None, json=True))
    assert ret_json == 0
    captured_json = capsys.readouterr()
    parsed = json.loads(captured_json.out)
    assert parsed["name"] == "test-exp"


def test_cli_init_with_existing_skill(tmp_path: Path, monkeypatch):
    skill = tmp_path / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: d\n---\n")
    exp = tmp_path / "eval"

    assert cmd_init(Args(force=False, skill=str(skill), dir=str(exp), harness="codex")) == 0
    cfg = yaml.safe_load((exp / "skilldiff.yaml").read_text())
    assert cfg["harness"] == "codex"
    assert (exp / cfg["skill"]).resolve() == skill.resolve()
    assert (exp / "tasks" / "my-first-task.yaml").exists()
    assert not (exp / "skills").exists()


def test_cli_init_rejects_missing_skill(tmp_path: Path):
    assert cmd_init(Args(force=False, skill=str(tmp_path / "nope"), dir=str(tmp_path))) == 1


def test_cli_check_demo(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cmd_init(Args(force=False))
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho 9.9.9\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(fake))

    code = cmd_check(Args(config="skilldiff.yaml", no_grade=False))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "untouched fixture scores 0%" in out
    assert "12 agent sessions" not in out
    assert "6 agent sessions per full run" in out


def test_cli_check_rejects_existing_binary_that_cannot_run(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cmd_init(Args(force=False))
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho 'sandbox denied' >&2\nexit 77\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(fake))

    code = cmd_check(Args(config="skilldiff.yaml", no_grade=True))
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL  claude CLI failed" in out
    assert "sandbox denied" in out
    assert "ok    claude CLI:" not in out


def test_cli_check_flags_grader_that_always_passes(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cmd_init(Args(force=False))
    task = tmp_path / "tasks" / "changelog-entry.yaml"
    task.write_text(task.read_text().replace(
        'python3 "$SKILLDIFF_TASK_DIR/../graders/changelog_entry.py"', "exit 0"
    ))
    monkeypatch.setenv("CLAUDE_BIN", "/bin/echo")
    cmd_check(Args(config="skilldiff.yaml", no_grade=False))
    assert "already scores 100%" in capsys.readouterr().out


def test_cli_check_and_run_reject_unknown_config_key(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cmd_init(Args(force=False))
    cfg_file = tmp_path / "skilldiff.yaml"
    cfg_file.write_text(
        cfg_file.read_text() + "timeouts_seconds: 60\n"
    )

    code = cmd_check(Args(config="skilldiff.yaml", no_grade=False))
    out = capsys.readouterr().out
    assert code == 1, out
    assert "Unknown key" in out and "did you mean 'timeout_seconds'" in out

    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    code = cmd_run(Args(config="skilldiff.yaml", runs=1))
    err = capsys.readouterr().err
    assert code == 1, err
    assert "Configuration error: Unknown key" in err


def test_format_results_source_size_signed_pct():
    from skilldiff.cli import _format_results

    res = {
        "name": "test-exp",
        "harness": "claude",
        "models": ["m"],
        "tasks": ["t"],
        "tasks_count": 1,
        "runs_per_arm": 1,
        "preset": "compression",
        "skill_comparison": {
            "skill_a": "skills/a",
            "skill_b": "skills/b",
            "source_bytes_a": 160176,
            "source_bytes_b": 172934,
        },
        "summary": {
            "control": {"task_score": 0.5},
            "skill": {"task_score": 0.5},
            "paired": {},
        },
    }
    out = _format_results(res)
    assert "Source size: 160176 → 172934 bytes (+8.0%)" in out
    assert "reduction" not in out


def test_cli_init_warns_placeholder_grader(tmp_path: Path, capsys):
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: test\n---\n")
    code = cmd_init(Args(force=False, skill=str(skill), dir=str(tmp_path / "exp")))
    assert code == 0
    out = capsys.readouterr().out
    assert (
        "Warning: graders/my_first_task.py is a placeholder template that fails until replaced."
    ) in out
    grader_text = (tmp_path / "exp" / "graders" / "my_first_task.py").read_text()
    assert "False,  # TODO: replace with real checks" in grader_text


def test_cli_check_warns_placeholder_grader_and_python_domains(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: test\n---\n")
    cmd_init(Args(force=False, skill=str(skill), dir="."))
    (tmp_path / "fixtures" / "my-project" / "main.py").write_text("print('hello')\n")
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho 9.9.9\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(fake))

    cmd_check(Args(config="skilldiff.yaml", no_grade=True))
    out = capsys.readouterr().out
    assert "is a placeholder template that fails until replaced" in out
    assert "allowed_domains is empty; add 'pypi.org' and 'files.pythonhosted.org'" in out

def test_run_split_filters_before_sessions_and_empty_selection_fails(tmp_path, monkeypatch, capsys):
    from skilldiff.cli import build_parser

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    assert cmd_init(Args(force=False)) == 0
    task_path = tmp_path / "tasks/changelog-entry.yaml"
    heldout = yaml.safe_load(task_path.read_text())
    heldout.update(id="heldout-secret", split="held-out")
    (tmp_path / "tasks/heldout.yaml").write_text(yaml.safe_dump(heldout))
    parser = build_parser()
    args = parser.parse_args(["run", "--runs", "1", "--split", "dev"])
    assert cmd_run(args) == 0
    result_files = list((tmp_path / "runs").glob("*/results.json"))
    assert len(result_files) == 1
    results = json.loads(result_files[0].read_text())
    assert results["tasks"] == ["changelog-entry"]
    assert "heldout-secret" not in capsys.readouterr().out
    args = parser.parse_args(["run", "--split", "dev", "-t", "heldout-secret"])
    assert cmd_run(args) == 1
    assert "No tasks match" in capsys.readouterr().err
    assert len(list((tmp_path / "runs").glob("*/results.json"))) == 1
    args = parser.parse_args(["run", "--runs", "1", "--split", "held-out"])
    assert cmd_run(args) == 0
    result_files = list((tmp_path / "runs").glob("*/results.json"))
    assert len(result_files) == 2
    selected = [json.loads(path.read_text())["tasks"] for path in result_files]
    assert ["heldout-secret"] in selected

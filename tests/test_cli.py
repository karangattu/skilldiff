import json
from pathlib import Path

import yaml

from skilldiff.cli import cmd_check, cmd_init, cmd_results, cmd_run


class Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


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

    ret_results = cmd_results(Args(run_dir=None, json=False))
    assert ret_results == 0
    captured_res = capsys.readouterr()
    assert "test-exp" in captured_res.out
    assert "Report:" in captured_res.out

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

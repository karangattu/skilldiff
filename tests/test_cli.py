import json
from pathlib import Path

import yaml

from skilldiff.cli import cmd_init, cmd_results, cmd_run


class Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_cli_init_and_overwrite(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    args = Args(force=False)
    ret = cmd_init(args)
    assert ret == 0
    assert (tmp_path / "skilldiff.yaml").exists()
    assert (tmp_path / "tasks" / "review-auth.yaml").exists()
    assert (tmp_path / "skills" / "code-review" / "SKILL.md").exists()
    generated_config = yaml.safe_load((tmp_path / "skilldiff.yaml").read_text())
    assert generated_config["claude"]["auth"] == "subscription"
    assert generated_config["claude"]["permission_mode"] == "acceptEdits"
    assert generated_config["claude"]["allowed_tools"] == []

    ret_no_force = cmd_init(args)
    assert ret_no_force == 1

    args_force = Args(force=True)
    ret_force = cmd_init(args_force)
    assert ret_force == 0


def test_cli_run_and_results(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")

    cmd_init(Args(force=False))

    task_file = tmp_path / "tasks" / "review-auth.yaml"
    task_file.write_text(
        "id: review-auth\nprompt: Review code\n"
        "grader:\n  type: command\n  command: exit 0\n"
    )

    cfg_file = tmp_path / "skilldiff.yaml"
    cfg_file.write_text(
        "name: test-exp\nskill: ./skills/code-review\n"
        "models:\n  - sonnet\ntasks:\n  - ./tasks/*.yaml\nruns: 1\n"
    )

    ret_run = cmd_run(Args(config="skilldiff.yaml", runs=1))
    assert ret_run == 0

    captured = capsys.readouterr()
    assert "test-exp" in captured.out
    assert "Task score" in captured.out
    assert "Quarto report:" in captured.out

    ret_results = cmd_results(Args(run_dir=None, json=False))
    assert ret_results == 0
    captured_res = capsys.readouterr()
    assert "test-exp" in captured_res.out
    assert "Quarto report:" in captured_res.out

    ret_json = cmd_results(Args(run_dir=None, json=True))
    assert ret_json == 0
    captured_json = capsys.readouterr()
    parsed = json.loads(captured_json.out)
    assert parsed["name"] == "test-exp"

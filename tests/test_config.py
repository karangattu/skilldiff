from pathlib import Path

import pytest

from skilldiff.config import load_experiment, load_task


def test_load_task_valid(tmp_path: Path):
    task_file = tmp_path / "task.yaml"
    task_file.write_text(
        "id: task-1\nprompt: Test prompt\nrepo: ./fixture\n"
        "grader:\n  type: command\n  command: python test.py\n"
    )
    task = load_task(task_file)
    assert task.id == "task-1"
    assert task.prompt == "Test prompt"
    assert task.repo == "./fixture"
    assert task.grader is not None
    assert task.grader.command == "python test.py"


def test_load_task_missing_prompt(tmp_path: Path):
    task_file = tmp_path / "invalid.yaml"
    task_file.write_text("id: bad\n")
    with pytest.raises(ValueError, match="prompt"):
        load_task(task_file)


def test_load_experiment_valid(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill")

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "t1.yaml").write_text("id: t1\nprompt: Do work\n")

    exp_file = tmp_path / "skilldiff.yaml"
    exp_file.write_text(
        "name: test-exp\nskill: ./skills/test-skill\n"
        "models:\n  - sonnet\ntasks:\n  - ./tasks/*.yaml\nruns: 2\n"
    )

    exp_cfg, tasks = load_experiment(exp_file)
    assert exp_cfg.name == "test-exp"
    assert exp_cfg.skill == skill_dir.resolve()
    assert exp_cfg.models == ["sonnet"]
    assert exp_cfg.runs == 2
    assert exp_cfg.claude.auth == "subscription"
    assert len(tasks) == 1
    assert tasks[0].id == "t1"


def test_load_experiment_missing_skill_md(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "empty-skill"
    skill_dir.mkdir(parents=True)

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "t1.yaml").write_text("id: t1\nprompt: Do work\n")

    exp_file = tmp_path / "skilldiff.yaml"
    exp_file.write_text(
        "name: test-exp\nskill: ./skills/empty-skill\n"
        "models:\n  - sonnet\ntasks:\n  - ./tasks/*.yaml\n"
    )

    with pytest.raises(FileNotFoundError, match="SKILL.md"):
        load_experiment(exp_file)


def test_load_experiment_no_tasks(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill")

    exp_file = tmp_path / "skilldiff.yaml"
    exp_file.write_text(
        "name: test-exp\nskill: ./skills/test-skill\n"
        "models:\n  - sonnet\ntasks:\n  - ./nonexistent/*.yaml\n"
    )

    with pytest.raises(ValueError, match="No task files matched"):
        load_experiment(exp_file)


def test_load_experiment_reads_claude_permissions(tmp_path: Path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Test")
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "task.yaml").write_text("id: one\nprompt: Do it\n")
    exp_file = tmp_path / "skilldiff.yaml"
    exp_file.write_text(
        "name: test\n"
        "skill: ./skill\n"
        "models:\n  - sonnet\n"
        "tasks:\n  - ./tasks/*.yaml\n"
        "claude:\n"
        "  auth: api_key\n"
        "  permission_mode: acceptEdits\n"
        "  allowed_tools:\n"
        "    - Bash(shiny docs *)\n"
    )

    config, _ = load_experiment(exp_file)

    assert config.claude.auth == "api_key"
    assert config.claude.permission_mode == "acceptEdits"
    assert config.claude.allowed_tools == ["Bash(shiny docs *)"]


def test_load_experiment_rejects_unknown_claude_auth(tmp_path: Path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Test")
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "task.yaml").write_text("id: one\nprompt: Do it\n")
    exp_file = tmp_path / "skilldiff.yaml"
    exp_file.write_text(
        "name: test\n"
        "skill: ./skill\n"
        "models:\n  - sonnet\n"
        "tasks:\n  - ./tasks/*.yaml\n"
        "claude:\n"
        "  auth: surprise\n"
    )

    with pytest.raises(ValueError, match="claude.auth"):
        load_experiment(exp_file)

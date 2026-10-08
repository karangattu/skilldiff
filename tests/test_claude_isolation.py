"""Config rules for macOS isolation under the Claude harness."""

import sys
from pathlib import Path

import pytest

from skilldiff.config import ClaudeConfig, ExperimentConfig, load_experiment
from skilldiff.isolation import macos_command
from skilldiff.preflight import probe_workspace, protected_read_paths
from skilldiff.runner import AgentRunner, ExecResult


def write_experiment(directory: Path, body: str) -> Path:
    tasks = directory / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    skill = directory / "skill"
    skill.mkdir(exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\n", encoding="utf-8")
    (directory / "repo").mkdir(exist_ok=True)
    (tasks / "demo.yaml").write_text(
        "id: demo\n"
        "category: general\n"
        "repo: ../repo\n"
        "prompt: Say hello.\n"
        "grader:\n"
        "  type: command\n"
        "  command: 'true'\n",
        encoding="utf-8",
    )
    path = directory / "skilldiff.yaml"
    path.write_text(
        "name: claude-isolation\n"
        "skill: ./skill\n"
        "models: [claude-haiku-5-5]\n"
        "tasks: ['./tasks/*.yaml']\n"
        f"{body}",
        encoding="utf-8",
    )
    return path


def test_claude_harness_accepts_macos_isolation_with_read_paths(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = write_experiment(
        tmp_path,
        "harness: claude\n"
        "isolation: macos\n"
        "claude:\n"
        "  read_paths: [./runtime]\n",
    )

    config, _ = load_experiment(path)

    assert config.isolation == "macos"
    assert config.harness == "claude"
    assert config.claude.read_paths == [str(runtime.resolve())]


def test_claude_read_paths_require_macos_isolation(tmp_path):
    path = write_experiment(
        tmp_path,
        "harness: claude\n"
        "claude:\n"
        "  read_paths: [./runtime]\n",
    )

    with pytest.raises(ValueError, match="claude.read_paths requires isolation: macos"):
        load_experiment(path)


def test_macos_isolation_rejects_harnesses_without_support(tmp_path):
    path = write_experiment(tmp_path, "harness: opencode\nisolation: macos\n")

    with pytest.raises(ValueError, match="Codex and Claude harnesses only"):
        load_experiment(path)


def test_probe_workspace_passes_claude_read_paths(tmp_path):
    calls = []

    class CaptureRunner(AgentRunner):
        def _exec(self, cmd, cwd, env, timeout, **kwargs):
            calls.append((cmd, kwargs))
            return ExecResult("", "", 0, 0)

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cfg = ExperimentConfig(
        name="claude-probe",
        skill=None,
        models=["claude-haiku-5-5"],
        tasks_patterns=[],
        harness="claude",
        isolation="macos",
        claude=ClaudeConfig(read_paths=[str(runtime)]),
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    probe_workspace(CaptureRunner(), workspace, cfg, [], scratch, runtime_probe="echo ok")
    assert any(call[1].get("read_paths") == [str(runtime)] for call in calls)


def test_protected_read_paths_includes_claude_read_paths(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cfg = ExperimentConfig(
        name="t",
        skill=None,
        models=["m"],
        tasks_patterns=[],
        harness="claude",
        claude=ClaudeConfig(read_paths=[str(runtime)]),
    )
    protected = protected_read_paths(cfg)
    assert str(runtime / ".agents") in protected
    assert str(runtime / ".claude") in protected


def test_macos_command_masks_bundled_claude_skills(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("macOS isolation requires macOS")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    cmd = macos_command(["claude"], workspace, scratch, [str(runtime)], [])
    assert r"\.(agents|codex|claude)" in cmd[2]


def test_macos_command_rejects_missing_and_broad_read_paths(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("macOS isolation requires macOS")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="read_paths root does not exist"):
        macos_command(["claude"], workspace, scratch, [str(missing)], [])
    with pytest.raises(ValueError, match="read_paths root is too broad"):
        macos_command(["claude"], workspace, scratch, [str(Path.home())], [])


def test_macos_command_allows_claude_scratch_and_claude_json(tmp_path, monkeypatch):
    if sys.platform != "darwin":
        pytest.skip("macOS isolation requires macOS")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    (fake_home / ".claude.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    cmd = macos_command(["claude"], workspace, scratch, [], [])
    assert "tmp/claude-" in cmd[2]
    assert str(fake_home / ".claude.json") in cmd[2]

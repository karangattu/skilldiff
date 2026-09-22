import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skilldiff.config import (
    AntigravityConfig,
    ClaudeConfig,
    CodexConfig,
    ExperimentConfig,
    OpenCodeConfig,
    TaskConfig,
    load_experiment,
)
from skilldiff.experiment import ExperimentRunner
from skilldiff.runner import AgentRunner
from skilldiff.workspace import Workspace


@pytest.fixture
def mock_exp_dir(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "skills" / "sample-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: sample-skill\ndescription: Test skill\n---\n# Sample\n",
        encoding="utf-8",
    )

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True)
    task_file = tasks_dir / "task1.yaml"
    task_file.write_text("id: task1\nprompt: Hello test\n", encoding="utf-8")

    return tmp_path


def test_load_experiment_harness_defaults_and_detection(mock_exp_dir: Path):
    exp_file = mock_exp_dir / "exp_default.yaml"
    exp_file.write_text(
        """name: test-exp
skill: ./skills/sample-skill
models:
  - sonnet
tasks:
  - ./tasks/*.yaml
""",
        encoding="utf-8",
    )
    cfg, tasks = load_experiment(exp_file)
    assert cfg.harness == "claude"
    assert len(tasks) == 1

    exp_file_codex = mock_exp_dir / "exp_codex.yaml"
    exp_file_codex.write_text(
        """name: test-codex
skill: ./skills/sample-skill
models:
  - o3-mini
tasks:
  - ./tasks/*.yaml
codex:
  sandbox: workspace-write
""",
        encoding="utf-8",
    )
    cfg_codex, _ = load_experiment(exp_file_codex)
    assert cfg_codex.harness == "codex"
    assert cfg_codex.codex.sandbox == "workspace-write"

    exp_file_opencode = mock_exp_dir / "exp_opencode.yaml"
    exp_file_opencode.write_text(
        """name: test-opencode
skill: ./skills/sample-skill
models:
  - anthropic/claude-3-5-sonnet
tasks:
  - ./tasks/*.yaml
opencode:
  variant: high
""",
        encoding="utf-8",
    )
    cfg_opencode, _ = load_experiment(exp_file_opencode)
    assert cfg_opencode.harness == "opencode"
    assert cfg_opencode.opencode.variant == "high"

    exp_file_antigravity = mock_exp_dir / "exp_agy.yaml"
    exp_file_antigravity.write_text(
        """name: test-agy
skill: ./skills/sample-skill
models:
  - gemini-2.5-pro
tasks:
  - ./tasks/*.yaml
agy:
  dangerously_skip_permissions: true
""",
        encoding="utf-8",
    )
    cfg_agy, _ = load_experiment(exp_file_antigravity)
    assert cfg_agy.harness == "antigravity"


def test_load_experiment_rejects_invalid_harness(mock_exp_dir: Path):
    exp_file = mock_exp_dir / "exp_invalid.yaml"
    exp_file.write_text(
        """name: test-invalid
skill: ./skills/sample-skill
harness: unsupported-agent
models:
  - test-model
tasks:
  - ./tasks/*.yaml
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Invalid harness 'unsupported-agent'"):
        load_experiment(exp_file)


def test_workspace_skill_placement_for_each_harness(tmp_path: Path):
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Skill", encoding="utf-8")

    ws_claude = Workspace(
        root=tmp_path / "ws_claude",
        is_treatment=True,
        skill_dir=skill_dir,
        harness="claude",
    )
    ws_claude.setup()
    assert (ws_claude.root / ".claude" / "skills" / "my-skill" / "SKILL.md").exists()

    ws_codex = Workspace(
        root=tmp_path / "ws_codex",
        is_treatment=True,
        skill_dir=skill_dir,
        harness="codex",
    )
    ws_codex.setup()
    assert (ws_codex.root / ".codex" / "skills" / "my-skill" / "SKILL.md").exists()
    assert (ws_codex.root / ".agents" / "skills" / "my-skill" / "SKILL.md").exists()

    ws_opencode = Workspace(
        root=tmp_path / "ws_opencode",
        is_treatment=True,
        skill_dir=skill_dir,
        harness="opencode",
    )
    ws_opencode.setup()
    assert (ws_opencode.root / ".opencode" / "skills" / "my-skill" / "SKILL.md").exists()
    assert (ws_opencode.root / ".agents" / "skills" / "my-skill" / "SKILL.md").exists()

    ws_agy = Workspace(
        root=tmp_path / "ws_agy",
        is_treatment=True,
        skill_dir=skill_dir,
        harness="antigravity",
    )
    ws_agy.setup()
    assert (ws_agy.root / ".agents" / "skills" / "my-skill" / "SKILL.md").exists()


def test_workspace_diff_excludes_all_harness_directories(tmp_path: Path):
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Skill", encoding="utf-8")

    ws = Workspace(
        root=tmp_path / "ws_diff",
        is_treatment=False,
        skill_dir=skill_dir,
        harness="claude",
    )
    ws.setup()

    (ws.root / ".claude" / "metadata.json").parent.mkdir(parents=True, exist_ok=True)
    (ws.root / ".claude" / "metadata.json").write_text("{}", encoding="utf-8")
    (ws.root / ".codex" / "history.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (ws.root / ".codex" / "history.jsonl").write_text("{}", encoding="utf-8")
    (ws.root / ".opencode" / "session.json").parent.mkdir(parents=True, exist_ok=True)
    (ws.root / ".opencode" / "session.json").write_text("{}", encoding="utf-8")
    (ws.root / ".agents" / "test.txt").parent.mkdir(parents=True, exist_ok=True)
    (ws.root / ".agents" / "test.txt").write_text("agent file", encoding="utf-8")

    (ws.root / "app.py").write_text("print('hello')", encoding="utf-8")

    diff, files = ws.get_diff()
    assert files == ["app.py"]
    assert "print('hello')" in diff
    assert ".claude" not in diff
    assert ".codex" not in diff
    assert ".opencode" not in diff
    assert ".agents" not in diff


def test_agent_runner_dispatches_codex(tmp_path: Path):
    runner = AgentRunner(codex_bin="codex-mock")
    cfg = CodexConfig(
        auth="stored",
        sandbox="workspace-write",
        dangerously_bypass_approvals_and_sandbox=True,
    )

    mock_stdout = (
        '{"type": "tool_call", "name": "bash"}\n'
        '{"type": "message", "content": "Solved issue", '
        '"usage": {"input_tokens": 120, "output_tokens": 45}}\n'
    )

    with patch("subprocess.run") as mock_subproc:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = ""
        mock_subproc.return_value = mock_proc

        res = runner.run(
            prompt="Do task",
            cwd=tmp_path,
            model="o3-mini",
            config=cfg,
        )

        assert res.exit_code == 0
        assert res.response == "Solved issue"
        assert res.input_tokens == 120
        assert res.output_tokens == 45
        assert res.tool_calls == 1

        call_args = mock_subproc.call_args[0][0]
        assert "codex-mock" in call_args
        assert "exec" in call_args
        assert "--dangerously-bypass-approvals-and-sandbox" in call_args
        assert "--json" in call_args


def test_agent_runner_dispatches_opencode(tmp_path: Path):
    runner = AgentRunner(opencode_bin="opencode-mock")
    cfg = OpenCodeConfig(
        dangerously_skip_permissions=True,
        variant="high",
    )

    mock_stdout = (
        '{"type": "step_start", "part": {"tokens": {"input": 80}}}\n'
        '{"type": "message", "part": {"text": "Updated code", "tokens": {"output": 35}}}\n'
    )

    with patch("subprocess.run") as mock_subproc:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = ""
        mock_subproc.return_value = mock_proc

        res = runner.run(
            prompt="Refactor code",
            cwd=tmp_path,
            model="anthropic/claude-3-5-sonnet",
            config=cfg,
        )

        assert res.exit_code == 0
        assert "Updated code" in res.response
        assert res.input_tokens == 80
        assert res.output_tokens == 35

        call_args = mock_subproc.call_args[0][0]
        assert "opencode-mock" in call_args
        assert "run" in call_args
        assert "--format" in call_args
        assert "json" in call_args
        assert "--dangerously-skip-permissions" in call_args
        assert "--variant" in call_args
        assert "high" in call_args


def test_agent_runner_dispatches_antigravity(tmp_path: Path):
    runner = AgentRunner(antigravity_bin="agy-mock")
    cfg = AntigravityConfig(dangerously_skip_permissions=True)

    mock_stdout = json.dumps(
        {
            "result": "Completed successfully",
            "total_cost_usd": 0.04,
            "usage": {"input_tokens": 150, "output_tokens": 60},
            "tool_calls_count": 2,
        }
    )

    with patch("subprocess.run") as mock_subproc:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = ""
        mock_subproc.return_value = mock_proc

        res = runner.run(
            prompt="Plan task",
            cwd=tmp_path,
            model="gemini-2.5-pro",
            config=cfg,
        )

        assert res.exit_code == 0
        assert res.response == "Completed successfully"
        assert res.cost == 0.04
        assert res.input_tokens == 150
        assert res.output_tokens == 60
        assert res.tool_calls == 2

        call_args = mock_subproc.call_args[0][0]
        assert "agy-mock" in call_args
        assert "-p" in call_args
        assert "--dangerously-skip-permissions" in call_args
        assert "--model" in call_args


def test_experiment_runner_end_to_end_codex_mock(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setenv("SKILLDIFF_MOCK_RESPONSE", "Codex mock output")

    skill_dir = tmp_path / "skills" / "mock-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: mock-skill\ndescription: Mock\n---\n", encoding="utf-8"
    )

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "task1.yaml").write_text("id: task1\nprompt: Test prompt\n", encoding="utf-8")

    cfg = ExperimentConfig(
        name="codex-experiment",
        skill=skill_dir,
        models=["o3-mini"],
        tasks_patterns=["./tasks/*.yaml"],
        runs=1,
        harness="codex",
        codex=CodexConfig(),
        claude=ClaudeConfig(),
        opencode=OpenCodeConfig(),
        antigravity=AntigravityConfig(),
    )
    task = TaskConfig(id="task1", prompt="Test prompt", source_path=tasks_dir / "task1.yaml")

    runner = ExperimentRunner(config=cfg, tasks=[task], output_dir=tmp_path / "runs")
    results = runner.run()

    assert results["harness"] == "codex"
    assert results["name"] == "codex-experiment"
    assert "o3-mini" in results["by_model"]
    assert results["by_model"]["o3-mini"]["runs_count"] == 1


def test_opencode_go_subscription_routing(tmp_path: Path):
    runner = AgentRunner(opencode_bin="opencode-mock")
    cfg_go = OpenCodeConfig(service="go")

    with patch("subprocess.run") as mock_subproc:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"type": "message", "part": {"text": "ok"}}\n'
        mock_proc.stderr = ""
        mock_subproc.return_value = mock_proc

        runner.run(
            prompt="Test",
            cwd=tmp_path,
            model="deepseek-v4-pro",
            config=cfg_go,
        )
        call_args = mock_subproc.call_args[0][0]
        assert "-m" in call_args
        model_idx = call_args.index("-m") + 1
        assert call_args[model_idx] == "opencode-go/deepseek-v4-pro"

        runner.run(
            prompt="Test",
            cwd=tmp_path,
            model="opencode/deepseek-v4-pro",
            config=cfg_go,
        )
        call_args = mock_subproc.call_args[0][0]
        model_idx = call_args.index("-m") + 1
        assert call_args[model_idx] == "opencode-go/deepseek-v4-pro"

        runner.run(
            prompt="Test",
            cwd=tmp_path,
            model="anthropic/claude-3-5-sonnet",
            config=cfg_go,
        )
        call_args = mock_subproc.call_args[0][0]
        model_idx = call_args.index("-m") + 1
        assert call_args[model_idx] == "anthropic/claude-3-5-sonnet"

    cfg_zen = OpenCodeConfig(service="zen", provider="opencode")
    with patch("subprocess.run") as mock_subproc:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"type": "message", "part": {"text": "ok"}}\n'
        mock_proc.stderr = ""
        mock_subproc.return_value = mock_proc

        runner.run(
            prompt="Test",
            cwd=tmp_path,
            model="claude-sonnet-5",
            config=cfg_zen,
        )
        call_args = mock_subproc.call_args[0][0]
        model_idx = call_args.index("-m") + 1
        assert call_args[model_idx] == "opencode/claude-sonnet-5"


def test_load_experiment_opencode_service_validation(mock_exp_dir: Path):
    exp_file_invalid = mock_exp_dir / "exp_opencode_invalid.yaml"
    exp_file_invalid.write_text(
        """name: test-invalid-opencode
skill: ./skills/sample-skill
models:
  - deepseek-v4-pro
tasks:
  - ./tasks/*.yaml
opencode:
  service: invalid-tier
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="opencode.service must be 'go'"):
        load_experiment(exp_file_invalid)

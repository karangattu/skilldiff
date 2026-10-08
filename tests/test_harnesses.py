import json
from pathlib import Path
from unittest.mock import patch

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
from skilldiff.runner import AgentRunner, ExecResult
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

    with patch.object(AgentRunner, "_exec") as mock_subproc:
        mock_subproc.return_value = ExecResult(
            stdout=mock_stdout, stderr="", exit_code=0, duration=1.0
        )

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


def test_codex_missing_cache_write_count_is_zero_for_cost_accounting(tmp_path: Path):
    runner = AgentRunner(codex_bin="codex-mock")
    cfg = CodexConfig()
    mock_stdout = json.dumps({
        "type": "turn.completed",
        "usage": {
            "input_tokens": 100,
            "cached_input_tokens": 60,
            "output_tokens": 20,
        },
    })

    with patch.object(AgentRunner, "_exec") as mock_subproc:
        mock_subproc.return_value = ExecResult(
            stdout=mock_stdout, stderr="", exit_code=0, duration=1.0
        )
        res = runner.run("Do task", tmp_path, "gpt-6-luna", cfg)

    assert res.input_tokens == 40
    assert res.cache_read_tokens == 60
    assert res.cache_creation_tokens == 0
    assert res.output_tokens == 20


def test_codex_reports_skill_availability_separately_from_use(tmp_path: Path):
    skill = tmp_path / ".agents" / "skills" / "sample-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: sample-skill\ndescription: sample\n---\n", encoding="utf-8"
    )
    runner = AgentRunner(codex_bin="codex-mock")
    cfg = CodexConfig()
    mock_stdout = "\n".join(
        [
            json.dumps(
                {"type": "thread.started", "thread_id": "t", "skills": ["sample-skill"]}
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "rg --files",
                    },
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 10
            }}),
        ]
    )

    with patch.object(AgentRunner, "_exec") as mock_subproc:
        mock_subproc.return_value = ExecResult(
            stdout=mock_stdout, stderr="", exit_code=0, duration=1.0
        )
        res = runner.run("Do task", tmp_path, "gpt-6-luna", cfg)

    assert res.skill_available is True
    assert res.skill_invoked is False


def test_codex_detects_skill_file_read_as_adoption(tmp_path: Path):
    skill = tmp_path / ".agents" / "skills" / "sample-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: sample-skill\ndescription: sample\n---\n", encoding="utf-8"
    )
    runner = AgentRunner(codex_bin="codex-mock")
    mock_stdout = "\n".join(
        [
            json.dumps(
                {"type": "thread.started", "thread_id": "t", "skills": ["sample-skill"]}
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "cat .agents/skills/sample-skill/SKILL.md",
                    },
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 10
            }}),
        ]
    )

    with patch.object(AgentRunner, "_exec") as mock_subproc:
        mock_subproc.return_value = ExecResult(
            stdout=mock_stdout, stderr="", exit_code=0, duration=1.0
        )
        res = runner.run("Do task", tmp_path, "gpt-6-luna", CodexConfig())

    assert res.skill_available is True
    assert res.skill_invoked is True


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

    with patch.object(AgentRunner, "_exec") as mock_subproc:
        mock_subproc.return_value = ExecResult(
            stdout=mock_stdout, stderr="", exit_code=0, duration=1.0
        )

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
        # OpenCode v2 rejects --dir; skilldiff launches in cwd and pins PWD.
        assert "--dir" not in call_args
        assert str(tmp_path) not in call_args
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

    with patch.object(AgentRunner, "_exec") as mock_subproc:
        mock_subproc.return_value = ExecResult(
            stdout=mock_stdout, stderr="", exit_code=0, duration=1.0
        )

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
        assert "--output-format" in call_args
        assert "json" in call_args
        assert "--add-dir" in call_args
        assert str(tmp_path) in call_args
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

    with patch.object(AgentRunner, "_exec") as mock_subproc:
        mock_subproc.return_value = ExecResult(
            stdout='{"type": "message", "part": {"text": "ok"}}\n',
            stderr="",
            exit_code=0,
            duration=1.0,
        )

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
    with patch.object(AgentRunner, "_exec") as mock_subproc:
        mock_subproc.return_value = ExecResult(
            stdout='{"type": "message", "part": {"text": "ok"}}\n',
            stderr="",
            exit_code=0,
            duration=1.0,
        )

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


def test_antigravity_runner_cache_tokens_and_transcript(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = AgentRunner(antigravity_bin="agy-mock")
    cfg_agy = AntigravityConfig()

    conv_id = "test-conv-1234"
    brain_log_dir = (
        Path.home()
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / conv_id
        / ".system_generated"
        / "logs"
    )
    brain_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = brain_log_dir / "transcript_full.jsonl"
    entry = '{"event": "tool_call", "tool": "view_file", "path": "skills/shiny-doctor/SKILL.md"}\n'
    log_file.write_text(entry, encoding="utf-8")

    try:
        sample_stdout = json.dumps({
            "conversation_id": conv_id,
            "status": "SUCCESS",
            "response": "Done",
            "duration_seconds": 4.5,
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_tokens": 500,
            },
        })

        with patch.object(AgentRunner, "_exec") as mock_subproc:
            mock_subproc.return_value = ExecResult(
                stdout=sample_stdout,
                stderr="",
                exit_code=0,
                duration=4.5,
            )

            res = runner.run(
                prompt="Fix",
                cwd=tmp_path,
                model="gemini-3.8-flash-high",
                config=cfg_agy,
                skill_names=["shiny-doctor"],
            )

            assert res.input_tokens == 1000
            assert res.output_tokens == 200
            assert res.cache_read_tokens == 500
            assert "skills/shiny-doctor/SKILL.md" in res.transcript
            assert res.skill_invoked is True
    finally:
        if log_file.exists():
            log_file.unlink()



def test_antigravity_non_transient_no_retry(tmp_path: Path):
    cfg_agy = AntigravityConfig(dangerously_skip_permissions=True)
    runner = AgentRunner(antigravity_bin="fake-agy")

    with patch.object(AgentRunner, "_exec") as mock_exec, patch("time.sleep"):
        mock_exec.return_value = ExecResult(
            stdout="",
            stderr="invalid flag: --foo",
            exit_code=1,
            duration=1.0,
        )

        res = runner.run(
            prompt="Repair",
            cwd=tmp_path,
            model="gemini-3.8-flash-high",
            config=cfg_agy,
        )

        assert mock_exec.call_count == 1
        assert res.status == "error"


def test_opencode_v2_uses_cwd_and_model_variant(tmp_path: Path):
    """OpenCode v2 rejects --dir and --variant; use cwd and model#variant."""
    runner = AgentRunner(opencode_bin="opencode-mock")
    cfg = OpenCodeConfig(variant="high", isolate=True)

    with patch.object(
        AgentRunner,
        "opencode_run_flags",
        return_value={"--format", "--standalone", "--model", "--dangerously-skip-permissions"},
    ), patch.object(AgentRunner, "_exec") as mock_exec:
        mock_exec.return_value = ExecResult(
            stdout='{"type":"text","part":{"text":"ok"}}\n',
            stderr="",
            exit_code=0,
            duration=1.0,
        )

        res = runner.run(
            prompt="Test",
            cwd=tmp_path,
            model="opencode-go/deepseek-v4.1-flash",
            config=cfg,
        )

        assert res.status == "ok"
        call_args = mock_exec.call_args[0][0]
        assert "--dir" not in call_args
        assert "--variant" not in call_args
        assert "--standalone" in call_args
        model_idx = call_args.index("-m") + 1
        assert call_args[model_idx] == "opencode-go/deepseek-v4.1-flash#high"


def test_opencode_recovers_from_rejected_flag(tmp_path: Path):
    """A single version-drift flag must not kill the run: drop it and retry."""
    runner = AgentRunner(opencode_bin="opencode-mock")
    cfg = OpenCodeConfig(variant="high")

    rejected = ExecResult(
        stdout=(
            '{"type":"error","error":{"message":'
            '"Unrecognized flag: --variant in command opencode run"}}\n'
        ),
        stderr="",
        exit_code=1,
        duration=0.2,
    )
    ok = ExecResult(
        stdout='{"type":"text","part":{"text":"done"}}\n',
        stderr="",
        exit_code=0,
        duration=1.0,
    )

    with patch.object(AgentRunner, "opencode_run_flags", return_value=set()), patch.object(
        AgentRunner, "_exec", side_effect=[rejected, ok]
    ) as mock_exec:
        res = runner.run(prompt="Test", cwd=tmp_path, model="m", config=cfg)

        assert res.status == "ok"
        assert mock_exec.call_count == 2
        retry_args = mock_exec.call_args_list[1][0][0]
        assert "--variant" not in retry_args
        assert retry_args[retry_args.index("-m") + 1] == "opencode-go/m#high"


def test_opencode_error_event_is_harness_failure(tmp_path: Path):
    runner = AgentRunner(opencode_bin="opencode-mock")
    cfg = OpenCodeConfig()

    error = ExecResult(
        stdout='{"type":"error","error":{"message":"Failed to change directory to /x"}}\n',
        stderr="",
        exit_code=0,
        duration=0.5,
    )

    with patch.object(AgentRunner, "opencode_run_flags", return_value=set()), patch.object(
        AgentRunner, "_exec", return_value=error
    ):
        res = runner.run(prompt="Test", cwd=tmp_path, model="m", config=cfg)

        assert res.status == "error"
        assert res.failure_kind == "harness_error"
        assert "Failed to change directory" in res.error


def test_opencode_structured_skill_adoption(tmp_path: Path):
    runner = AgentRunner(opencode_bin="opencode-mock")
    cfg = OpenCodeConfig()
    stdout = (
        json.dumps(
            {
                "type": "tool_use",
                "part": {"type": "tool", "tool": "skill", "input": {"id": "my-skill"}},
            }
        )
        + "\n"
    )

    with patch.object(AgentRunner, "opencode_run_flags", return_value=set()), patch.object(
        AgentRunner,
        "_exec",
        return_value=ExecResult(stdout=stdout, stderr="", exit_code=0, duration=1.0),
    ):
        res = runner.run(
            prompt="p", cwd=tmp_path, model="m", config=cfg, skill_names=["my-skill"]
        )

        assert res.skill_invoked is True


def test_load_experiment_opencode_isolate_default_and_override(mock_exp_dir: Path):
    exp_default = mock_exp_dir / "exp_iso_default.yaml"
    exp_default.write_text(
        "name: t\nskill: ./skills/sample-skill\nmodels: [m]\ntasks: [./tasks/*.yaml]\n",
        encoding="utf-8",
    )
    cfg_default, _ = load_experiment(exp_default)
    assert cfg_default.opencode.isolate is True

    exp_off = mock_exp_dir / "exp_iso_off.yaml"
    exp_off.write_text(
        "name: t\nskill: ./skills/sample-skill\nmodels: [m]\ntasks: [./tasks/*.yaml]\n"
        "opencode:\n  isolate: false\n",
        encoding="utf-8",
    )
    cfg_off, _ = load_experiment(exp_off)
    assert cfg_off.opencode.isolate is False

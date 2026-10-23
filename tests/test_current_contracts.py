"""Regressions for current CLI contracts (no credentials or model calls)."""

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

from skilldiff.config import AntigravityConfig, ClaudeConfig, CodexConfig, OpenCodeConfig
from skilldiff.experiment import find_user_level_installs
from skilldiff.reporter import _total_tokens_opt
from skilldiff.runner import (
    AgentRunner,
    ExecResult,
    detect_opencode_skill_load,
    parse_claude_output,
)
from skilldiff.telemetry import parse_agy, parse_codex, parse_opencode
from skilldiff.workspace import Workspace, find_harness_contamination


@pytest.mark.parametrize("config", [ClaudeConfig(), CodexConfig(), OpenCodeConfig(),
                                   AntigravityConfig()])
@pytest.mark.parametrize("output", ["", "not json", '{"unexpected":true}'])
def test_zero_exit_without_completion_is_harness_error(tmp_path, config, output):
    with patch.object(AgentRunner, "_exec", return_value=ExecResult(output, "", 0, 1)), \
         patch.object(AgentRunner, "cli_flags", return_value={"--ephemeral", "--auto"}):
        result = AgentRunner().run("p", tmp_path, "m", config)
    assert result.status == "error"
    assert result.failure_kind == "harness_error"


def test_codex_failure_keeps_completed_usage(tmp_path):
    output = '\n'.join(map(json.dumps, [
        {"type": "turn.completed", "usage": {"input_tokens": 100,
         "cached_input_tokens": 60, "output_tokens": 20}},
        {"type": "turn.failed", "error": {"message": "provider failed"}},
    ]))
    with patch.object(AgentRunner, "_exec", return_value=ExecResult(output, "", 0, 1)):
        result = AgentRunner().run("p", tmp_path, "m", CodexConfig())
    assert result.status == "error"
    assert result.error is not None and "provider failed" in result.error
    assert result.input_tokens == 40


@pytest.mark.parametrize("tool,status,expected", [
    ("read", "completed", False), ("skill", "error", False),
    ("skill", "running", False), ("skill", "completed", True),
])
def test_opencode_only_successful_skill_load_proves_adoption(tool, status, expected):
    stream = json.dumps({"type": "tool_use", "part": {"tool": tool,
                         "state": {"status": status, "input": {"id": "target"}}}})
    assert detect_opencode_skill_load(stream, ["target"]) is expected


def test_agy_native_global_and_plugin_skills_are_scanned(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    base = tmp_path / ".gemini/antigravity-cli"
    for rel in ("skills/target", "plugins/demo/skills/target"):
        path = base / rel
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text("# target")
    assert len(find_user_level_installs(["target"], "antigravity")) == 2
    warnings = find_harness_contamination(["target"], "antigravity")
    assert any("plugins/demo/skills/target" in item for item in warnings)


@pytest.mark.parametrize("harness,root", [("claude", ".claude"), ("codex", ".agents"),
                                          ("opencode", ".opencode"),
                                          ("antigravity", ".agents")])
def test_treatment_installs_target_once(tmp_path, harness, root):
    skill = tmp_path / "target"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: target\ndescription: test\n---\n")
    ws = Workspace(tmp_path / "ws", True, skill, harness=harness)
    ws.setup()
    assert [str(p.relative_to(ws.root)) for p in ws.root.rglob("SKILL.md")] == [
        f"{root}/skills/target/SKILL.md"]


def test_agy_terminal_cumulative_usage_is_counted_once(tmp_path):
    output = '\n'.join(map(json.dumps, [
        {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE",
         "usage": {"input_tokens": 100, "output_tokens": 20}}},
        {"event": "result", "result": {"status": "SUCCESS", "conversation_id": "one",
         "response": "first", "num_turns": 1,
         "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 60,
                   "total_tokens": 120}}},
        {"event": "result", "result": {"status": "SUCCESS", "conversation_id": "one",
         "response": "second", "num_turns": 2,
         "usage": {"input_tokens": 250, "output_tokens": 50, "cache_read_tokens": 100,
                   "total_tokens": 300}}},
    ]))
    with patch.object(AgentRunner, "_exec", return_value=ExecResult(output, "", 0, 1)):
        result = AgentRunner().run("p", tmp_path, "m", AntigravityConfig())
    assert result.input_tokens == 150
    assert result.cache_read_tokens == 100
    assert result.num_turns == 2
    assert _total_tokens_opt(asdict(result)) == 300
    assert result.session_id == "one"


def test_report_keeps_reported_total_with_unknown_breakdown():
    assert _total_tokens_opt({"input_tokens": None, "output_tokens": None,
                              "reported_total_tokens": 11072}) == 11072


def test_claude_per_turn_usage_and_cumulative_cost_use_different_scopes():
    transcript = (Path(__file__).parent / "fixtures/cli/claude-multiturn.jsonl").read_text()
    result = parse_claude_output(transcript, [])
    assert result["input_tokens"] == 4
    assert result["output_tokens"] == 86
    assert result["cache_creation_tokens"] == 17991
    assert result["cache_read_tokens"] == 16279
    assert result["num_turns"] == 2
    assert result["cost"] == pytest.approx(0.0760878)


def test_failed_followup_preserves_prior_cumulative_cost_and_permission_evidence():
    claude = parse_claude_output('\n'.join(map(json.dumps, [
        {"type": "result", "result": "first", "total_cost_usd": .25,
         "permission_denials": [{"tool_name": "Bash"}]},
        {"type": "result", "subtype": "error_during_execution",
         "permission_denials": []},
    ])), [])
    assert claude["cost"] == .25
    assert claude["permission_denials"] == ["Bash"]
    agy = parse_agy('\n'.join(map(json.dumps, [
        {"event": "result", "result": {"status": "SUCCESS", "cost": .25,
         "num_turns": 1, "usage": {"input_tokens": 100, "cache_read_tokens": 0,
                                   "output_tokens": 10, "total_tokens": 110}}},
        {"event": "result", "result": {"status": "ERROR", "error": "failed"}},
    ])), [])
    assert agy["cost"] == .25
    assert agy["num_turns"] == 1
    assert agy["reported_total_tokens"] == 110


def test_codex_cumulative_snapshots_are_not_added_repeatedly():
    text = (Path(__file__).parent / "fixtures/cli/codex-cumulative.jsonl").read_text()
    result = parse_codex(text, [])
    assert result["input_tokens"] == 21441
    assert result["cache_read_tokens"] == 51456
    assert result["output_tokens"] == 559
    assert result["reported_total_tokens"] == 73456


def test_codex_snapshot_wins_over_turn_usage():
    text = "\n".join(map(json.dumps, [
        {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": {
            "inputTokens": 100, "cachedInputTokens": 10, "outputTokens": 20,
            "totalTokens": 120,
        }}}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}},
         "usage": {"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20}},
    ]))
    result = parse_codex(text, [])
    assert result["input_tokens"] == 90
    assert result["cache_read_tokens"] == 10
    assert result["output_tokens"] == 20
    assert result["reported_total_tokens"] == 120


def test_agy_inconsistent_cache_keeps_reported_total_and_unknown_input():
    text = (Path(__file__).parent / "fixtures/cli/agy-cumulative.jsonl").read_text()
    result = parse_agy(text, [])
    assert result["input_tokens"] is None
    assert result["output_tokens"] == 83
    assert result["reported_total_tokens"] == 20949
    assert result["cache_read_tokens"] == 28922


def test_opencode_current_missing_final_event_retains_partial_evidence():
    text = (Path(__file__).parent / "fixtures/cli/opencode-missing-completion.jsonl").read_text()
    result = parse_opencode(text, [])
    assert result["error"]
    assert result["response"] == "READY"
    assert result["input_tokens"] == 8655


def test_harness_failure_never_becomes_a_valid_zero_score_pair():
    from skilldiff.stats import analysis_run, usable_agent_run

    raw = {"status": "error", "failure_kind": "harness_error", "grade_status": "graded",
           "score": 1, "cost": 0.25}
    result = analysis_run(raw, agent_failure="zero")
    assert not usable_agent_run(result)
    assert result["score"] == 1  # Keep diagnostic evidence intact.
    assert result["cost"] == 0.25


@pytest.mark.parametrize("cfg,output", [
    (ClaudeConfig(), '{"type":"result","usage":[1]}'),
    (CodexConfig(), '{"type":"turn.completed","usage":[1]}'),
    (AntigravityConfig(), '{"event":"result","result":[1]}'),
    (OpenCodeConfig(), '{"type":"step_finish","part":[1]}'),
    (OpenCodeConfig(), '{"type":"tool_use","part":{"state":[1]}}'),
    (ClaudeConfig(), '{"type":"result","result":[1]}'),
    (AntigravityConfig(), '{"event":"result","result":{"status":"SUCCESS",'
     '"response":[1]}}'),
    (CodexConfig(), '{"type":"item.completed","item":{"type":"agent_message",'
     '"text":[1]}}\n{"type":"turn.completed"}'),
])
def test_malformed_event_shapes_return_errors_instead_of_raising(tmp_path, cfg, output):
    with patch.object(AgentRunner, "_exec", return_value=ExecResult(output, "", 0, 1)):
        result = AgentRunner().run("p", tmp_path, "m", cfg)
    assert result.status == "error"
    assert result.failure_kind == "harness_error"


def test_control_discovery_is_a_harness_error_even_without_loading(tmp_path, capable_cli):
    from skilldiff.config import ExperimentConfig

    skill = tmp_path / "target"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: target\ndescription: test\n---\n")
    ws = tmp_path / "workspace"
    ws.mkdir()
    cfg = ExperimentConfig(name="test", skill=skill, models=["m"], tasks_patterns=[])
    output = ('{"type":"system","subtype":"init","skills":["target"]}\n'
              '{"type":"result","result":"done"}')
    with patch.object(AgentRunner, "_exec", return_value=ExecResult(output, "", 0, 1)):
        result = AgentRunner().run("p", ws, "m", cfg)
    assert result.skill_available is True
    assert result.skill_invoked is False
    assert result.failure_kind == "harness_error"
    assert result.status == "error"


def test_native_total_and_session_metadata_survive_reports_and_terminal(tmp_path):
    from skilldiff.cli import _format_results
    from skilldiff.reporter import create_reports
    from skilldiff.stats import paired_comparison

    run = {"task_id": "one", "model": "m", "repetition": 1, "status": "ok", "score": 1,
           "grade_status": "graded", "success": True, "input_tokens": None,
           "cache_read_tokens": None, "cache_creation_tokens": None, "output_tokens": None,
           "reported_total_tokens": 11072, "session_id": "native-session-one",
           "cli": {"version": "native-cli 2.0.0", "isolation": "local"}}
    results = {"name": "native-report", "models": ["m"], "tasks": ["one"],
               "runs_per_arm": 1, "runs": {"control": [run],
               "treatment": [{**run, "session_id": "native-session-two"}]}}
    paths = create_reports(results, tmp_path)
    for path in paths.values():
        text = path.read_text()
        assert "11,072" in text
        assert "native-session-one" in text
        assert "native-cli 2.0.0" in text
        assert "N/A" in text
    assert "11,072" in _format_results(results)
    assert paired_comparison([run], [run])["tokens"]["n"] == 1

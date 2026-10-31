"""Inspect Scout database export, skilldiff scanners, and `skilldiff scan`."""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

inspect_scout = pytest.importorskip("inspect_scout")

from inspect_ai.model import ChatMessageAssistant, ChatMessageTool, ChatMessageUser  # noqa: E402
from inspect_scout import Transcript  # noqa: E402

from skilldiff.cli import cmd_init, cmd_run, cmd_scan  # noqa: E402
from skilldiff.scanners import (  # noqa: E402
    build_scanners,
    format_scan_summary,
    reference_kind,
    scan_summary,
    skill_use,
)
from skilldiff.scout import (  # noqa: E402
    build_inspect_transcript,
    export_run_to_scout_db,
    parse_transcript_messages,
    to_inspect_messages,
)

CLAUDE_STREAM = (
    "STDOUT:\n"
    '{"type": "user", "message": {"role": "user", "content": '
    '[{"type": "text", "text": "Solve task"}]}}\n'
    '{"type": "assistant", "message": {"role": "assistant", "content": ['
    '{"type": "text", "text": "I will read the skill."},'
    '{"type": "tool_use", "id": "call_1", "name": "Skill", "input": {"skill": "pytest-runner"}},'
    '{"type": "tool_use", "id": "call_2", "name": "Read", '
    '"input": {"file_path": "/ws/.claude/skills/pytest-runner/reference.md"}}'
    ']}}\n'
    '{"type": "user", "message": {"role": "user", "content": ['
    '{"type": "tool_result", "tool_use_id": "call_1", "content": "Skill loaded", '
    '"is_error": false},'
    '{"type": "tool_result", "tool_use_id": "call_2", "content": "denied", "is_error": true}'
    ']}}\n'
    '{"type": "assistant", "message": {"role": "assistant", "content": '
    '[{"type": "text", "text": "Finished."}]}}\n'
    "\n\nSTDERR:\n"
)


def _transcript(arm: str = "treatment", stream: str = CLAUDE_STREAM) -> Transcript:
    messages, _ = parse_transcript_messages(stream, {"harness": "claude"})
    scout_obj = {
        "id": f"t-{arm}", "task": "task-1", "model": "claude-sonnet-5-5", "arm": arm,
        "score": 1.0, "success": True, "duration": 2.5, "messages": messages,
        "metadata": {"arm": arm, "harness": "claude", "repetition": 1,
                     "skill_invoked": arm == "treatment", "status": "ok",
                     "task_id": "task-1", "model": "claude-sonnet-5-5",
                     "tokens": {"input": 10, "output": 5}},
    }
    results = {"name": "exp", "harness": "claude", "skill_names": ["pytest-runner"],
               "run_dir": "runs/x", "arm_labels": {"treatment": "Skill"}}
    transcript = build_inspect_transcript(
        scout_obj, results, {"pytest-runner": "# Skill\nRun pytest."}
    )
    assert isinstance(transcript, Transcript)
    return transcript


def test_to_inspect_messages_keeps_tool_calls_results_and_errors():
    messages, _ = parse_transcript_messages(CLAUDE_STREAM, {"harness": "claude"})
    chat = to_inspect_messages(messages)
    assert isinstance(chat[0], ChatMessageUser)
    assistant = chat[1]
    assert isinstance(assistant, ChatMessageAssistant)
    assert [c.function for c in assistant.tool_calls or []] == ["Skill", "Read"]
    tools = [m for m in chat if isinstance(m, ChatMessageTool)]
    assert [m.tool_call_id for m in tools] == ["call_1", "call_2"]
    assert tools[0].error is None
    assert tools[1].error is not None


def test_build_inspect_transcript_fields_and_metadata():
    transcript = _transcript()
    assert transcript.transcript_id == "t-treatment"
    assert transcript.task_set == "exp"
    assert transcript.task_id == "task-1"
    assert transcript.agent == "claude"
    assert transcript.model == "claude-sonnet-5-5"
    assert transcript.total_tokens == 15
    assert transcript.metadata["skill_names"] == ["pytest-runner"]
    assert transcript.metadata["skill_bodies"]["pytest-runner"].startswith("# Skill")
    # Database columns may not be duplicated inside metadata.
    assert "model" not in transcript.metadata and "task_id" not in transcript.metadata


def test_reference_kind_covers_each_harness_tool_family():
    names = ["pytest-runner"]
    assert reference_kind("Skill", {"skill": "pytest-runner"}, names) == "skill"
    assert reference_kind("skill", {"id": "other"}, names) is None
    assert reference_kind("Read", {"file_path": "/w/.claude/skills/pytest-runner/SKILL.md"},
                          names) == "read"
    assert reference_kind("read_file", {"path": ".agents/skills/pytest-runner/SKILL.md"},
                          names) == "read"
    assert reference_kind("command_execution",
                          {"command": "cat .agents/skills/pytest-runner/SKILL.md"},
                          names) == "command"
    assert reference_kind("Write", {"file_path": "skills/pytest-runner/x"}, names) == "unclassified"
    assert reference_kind("Bash", {"command": "pytest"}, names) is None


def _scan(transcript: Transcript) -> Any:
    async def run() -> Any:
        return await skill_use()(transcript)

    return asyncio.run(run())


def test_skill_use_scanner_counts_only_confirmed_loads():
    result = _scan(_transcript())
    assert result.value is True
    assert result.answer == "used"
    assert result.metadata["confirmed"] == 1
    assert result.metadata["failed"] == 1
    assert "[M2] Skill (skill, ok)" in result.explanation
    assert "[M2] Read (read, error)" in result.explanation
    assert len(result.references) == 2
    assert result.metadata["harness_skill_invoked"] is True

    denied_only = CLAUDE_STREAM.replace('"is_error": false', '"is_error": true')
    result = _scan(_transcript(stream=denied_only))
    assert result.value is False and result.answer == "denied"

    no_result = CLAUDE_STREAM.replace('"tool_use_id": "call_1"', '"tool_use_id": "missing"')
    result = _scan(_transcript(stream=no_result))
    assert result.value is False and result.answer == "unmeasured"


def test_export_run_to_scout_db_roundtrip_and_scan_summary(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    cmd_init(Namespace(force=False))
    assert cmd_run(Namespace(config="skilldiff.yaml", runs=1, no_probe=True)) == 0
    run_dir = sorted((tmp_path / "runs").iterdir())[-1]

    export = export_run_to_scout_db(run_dir)
    assert export["count"] == 2
    assert Path(export["output_dir"]) == run_dir / "scout" / "db"
    # Idempotent: re-exporting adds no rows (the summary below still has two).
    export_run_to_scout_db(run_dir)

    capsys.readouterr()
    assert cmd_scan(Namespace(run_dir=str(run_dir), json=True)) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["scanners"] == ["skill_mentions", "skill_use"]
    assert [row["arm"] for row in summary["rows"]] == ["control", "treatment"]
    # The mock treatment run reports skill use without any tool call: Scout disagrees.
    treatment = summary["rows"][1]
    assert treatment["harness_skill_invoked"] is True
    assert treatment["scout_skill_used"] is False
    assert summary["agreement"] == {
        "agree": 1, "disagree": 1, "unmeasured": 0,
        "disagreements": summary["agreement"]["disagreements"],
    }
    assert summary["agreement"]["disagreements"][0]["arm"] == "treatment"
    assert (run_dir / "scout" / "scan-summary.json").is_file()

    table = format_scan_summary(summary)
    assert "| treatment | changelog-entry | claude-sonnet-5-5 | 1 | yes | no | 0 ok |" in table
    assert "disagree on 1" in table
    assert "scout view" in table

    again = scan_summary(summary["scan_location"])
    assert [r["transcript_id"] for r in again["rows"]] == [
        r["transcript_id"] for r in summary["rows"]
    ]


def test_build_scanners_adds_llm_scanners_only_on_request():
    assert set(build_scanners(["demo"])) == {"skill_use", "skill_mentions"}
    assert set(build_scanners([])) == {"skill_use"}
    assert set(build_scanners(["demo"], llm=True)) == {
        "skill_use", "skill_mentions", "skill_influence", "skill_miss_reason"
    }


def test_cli_judge_provider_parses_ids_and_builds_tool_free_configs():
    from inspect_ai.model import ChatMessageSystem

    from skilldiff.inspect_provider import (
        judge_config,
        model_id,
        parse_model_name,
        render_prompt,
    )

    assert model_id("codex", "gpt-6.1-sol") == "skilldiff/codex/gpt-6.1-sol"
    assert parse_model_name("agy/gemini-3.8-flash") == ("antigravity", "gemini-3.8-flash")
    with pytest.raises(ValueError, match="skilldiff/<harness>/<model>"):
        parse_model_name("claude-sonnet-5-5")
    claude = judge_config("claude")
    assert claude.sandbox is False and claude.max_turns == 1
    assert claude.extra_args == ["--tools", ""]
    assert judge_config("codex").sandbox == "read-only"
    assert judge_config("opencode").dangerously_skip_permissions is False
    assert judge_config("antigravity").dangerously_skip_permissions is False
    prompt = render_prompt([ChatMessageSystem(content="Be strict."),
                            ChatMessageUser(content="Judge this.")])
    assert prompt.startswith("SYSTEM INSTRUCTIONS:\nBe strict.")
    assert "USER:\nJudge this." in prompt
    assert "Do not run tools" in prompt


def test_cli_judge_provider_generates_through_the_harness_runner(monkeypatch):
    from inspect_ai.model import get_model

    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setenv("SKILLDIFF_MOCK_RESPONSE", "Looks fine. ANSWER: B")

    async def run() -> Any:
        model = get_model("skilldiff/claude/claude-sonnet-5-5")  # via the inspect_ai entry point
        return await model.generate("Is this fine?")

    output = asyncio.run(run())
    assert output.completion == "Looks fine. ANSWER: B"
    assert output.usage is not None and output.usage.total_tokens == 150


def test_scan_llm_defaults_to_the_runs_signed_in_cli(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setenv("SKILLDIFF_MOCK_RESPONSE", "The work follows part of it. ANSWER: B")
    cmd_init(Namespace(force=False))
    assert cmd_run(Namespace(config="skilldiff.yaml", runs=1, no_probe=True)) == 0
    capsys.readouterr()

    assert cmd_scan(Namespace(config="skilldiff.yaml", llm=True, json=True)) == 0
    captured = capsys.readouterr()
    assert "Model-judged scanners use skilldiff/claude/claude-sonnet-5-5" in captured.err
    summary = json.loads(captured.out)
    assert "skill_influence" in summary["scanners"] and "skill_miss_reason" in summary["scanners"]
    for row in summary["rows"]:
        assert row["scanners"]["skill_influence"]["value"] == "partially_followed"
        assert row["scanners"]["skill_miss_reason"]["value"] == "discovered_but_ignored"
    table = format_scan_summary(summary)
    assert "| Influence | Miss reason |" in table
    assert "partially_followed" in table

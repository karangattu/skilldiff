from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from skilldiff.cli import cmd_export
from skilldiff.pairing import save_run_artifacts
from skilldiff.scout import (
    build_scout_transcript,
    export_run_to_scout,
    parse_transcript_messages,
)


def test_parse_transcript_claude_messages():
    raw_claude = (
        "STDOUT:\n"
        '{"type": "user", "message": {"role": "user", "content": '
        '[{"type": "text", "text": "Solve task"}]}}\n'
        '{"type": "assistant", "message": {"role": "assistant", "content": ['
        '{"type": "text", "text": "I will read the skill."},'
        '{"type": "tool_use", "id": "call_1", "name": "Skill", "input": {"skill": "pytest-runner"}}'
        ']}}\n'
        '{"type": "user", "message": {"role": "user", "content": ['
        '{"type": "tool_result", "tool_use_id": "call_1", "content": "Skill loaded successfully", '
        '"is_error": false}'
        ']}}\n'
        '{"type": "assistant", "message": {"role": "assistant", "content": '
        '[{"type": "text", "text": "Finished."}]}}\n'
        '{"type": "result", "result": "Finished."}\n'
        "\n\nSTDERR:\n"
    )
    messages, events = parse_transcript_messages(raw_claude, {"harness": "claude"})
    assert len(messages) == 4
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Solve task"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "I will read the skill."
    assert len(messages[1]["tool_calls"]) == 1
    assert messages[1]["tool_calls"][0]["function"] == "Skill"
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "call_1"
    assert messages[2]["content"] == "Skill loaded successfully"
    assert messages[2]["error"] is None
    assert messages[3]["role"] == "assistant"
    assert messages[3]["content"] == "Finished."
    assert len(events) >= 4


def test_parse_transcript_codex_messages():
    raw_codex = (
        "STDOUT:\n"
        '{"method": "turn/start", "params": {"input": [{"type": "text", "text": "Fix bug"}]}}\n'
        '{"event": "item.completed", "item": {"type": "agent_message", "text": "Checking"}}\n'
        '{"event": "item.completed", "item": {"id": "cmd_1", "type": "command", '
        '"command": "git status", "output": "clean", "error": null}}\n'
        "\n\nSTDERR:\n"
    )
    messages, events = parse_transcript_messages(raw_codex, {"harness": "codex"})
    assert any(m["role"] == "user" and "Fix bug" in m["content"] for m in messages)
    assert any(m["role"] == "assistant" and "Checking" in m["content"] for m in messages)
    assert any(m["role"] == "tool" and m["tool_call_id"] == "cmd_1" for m in messages)


def test_parse_transcript_agy_messages():
    raw_agy = (
        "STDOUT:\n"
        '{"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE", '
        '"step_index": 1, "tool_info": {"name": "read_file", "parameters": {"path": "SKILL.md"}}, '
        '"output": "Skill content"}}\n'
        '{"event": "result", "result": {"status": "SUCCESS", "response": "Task done"}}\n'
        "\n\nSTDERR:\n"
    )
    messages, events = parse_transcript_messages(raw_agy, {"harness": "antigravity"})
    assert any(m["role"] == "tool" and m["content"] == "Skill content" for m in messages)
    assert any(m["role"] == "assistant" and m["content"] == "Task done" for m in messages)


def test_parse_transcript_opencode_messages():
    raw_opencode = (
        "STDOUT:\n"
        '{"type": "user", "content": "Run tests"}\n'
        '{"type": "assistant", "content": [{"type": "text", "text": "Running"}, '
        '{"type": "tool", "name": "shell", "state": {"input": {"cmd": "pytest"}}}]}\n'
        "\n\nSTDERR:\n"
    )
    messages, events = parse_transcript_messages(raw_opencode, {"harness": "opencode"})
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert len(messages[1]["tool_calls"]) == 1


def test_parse_transcript_fallback_plain_text():
    plain = "STDOUT:\nProcessed task successfully.\n\nSTDERR:\n"
    record = {"prompt": "Solve task", "response": "Processed task successfully."}
    messages, events = parse_transcript_messages(plain, record)
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Solve task"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "Processed task successfully."


def test_build_scout_transcript_redaction():
    record = {
        "task_id": "test_task",
        "model": "claude-sonnet-5-5",
        "arm": "treatment",
        "score": 1.0,
        "success": True,
        "duration": 5.2,
        "input_tokens": 100,
        "output_tokens": 50,
        "skill_invoked": True,
        "harness": "claude",
    }
    raw_transcript = "Agent leaked sk-ant-api03-abcdef1234567890123456 in response."
    diff = "diff --git a/test.txt b/test.txt\n+password=sk-proj-12345678901234567890"

    scout_obj = build_scout_transcript(record, raw_transcript, diff)
    assert scout_obj["source"] == "skilldiff"
    assert scout_obj["task"] == "test_task"
    assert scout_obj["model"] == "claude-sonnet-5-5"
    assert scout_obj["arm"] == "treatment"
    assert scout_obj["score"] == 1.0
    assert scout_obj["metadata"]["skill_invoked"] is True
    assert "sk-ant-api03-" not in json.dumps(scout_obj)
    assert "sk-proj-" not in json.dumps(scout_obj)
    assert "[REDACTED]" in json.dumps(scout_obj)


def test_save_run_artifacts_creates_transcript_json(tmp_path: Path):
    arm_dir = tmp_path / "arm"
    arm_dir.mkdir()
    record = {
        "task_id": "task_1",
        "model": "gpt-6.1",
        "repetition": 1,
        "arm": "treatment",
        "status": "ok",
        "score": 1.0,
    }
    save_run_artifacts(arm_dir, record, "STDOUT:\nDone\n\nSTDERR:\n", "diff content")

    assert (arm_dir / "transcript.txt").exists()
    assert (arm_dir / "diff.patch").exists()
    assert (arm_dir / "transcript.json").exists()

    saved_record = json.loads((arm_dir / "run.json").read_text(encoding="utf-8"))
    assert "transcript.txt" in saved_record["artifact_hashes"]
    assert "diff.patch" in saved_record["artifact_hashes"]
    assert "transcript.json" in saved_record["artifact_hashes"]

    saved_transcript = json.loads((arm_dir / "transcript.json").read_text(encoding="utf-8"))
    assert saved_transcript["id"] == "task_1_gpt-6.1_1_treatment"
    assert saved_transcript["source"] == "skilldiff"
    assert saved_transcript["score"] == 1.0


def test_export_run_to_scout_all_formats(tmp_path: Path):
    run_dir = tmp_path / "run_eval"
    run_dir.mkdir()
    arm_dir = run_dir / "runs" / "t1" / "m1" / "1" / "treatment"
    arm_dir.mkdir(parents=True)

    record = {
        "task_id": "t1",
        "model": "m1",
        "repetition": 1,
        "arm": "treatment",
        "status": "ok",
        "score": 1.0,
        "artifacts": "runs/t1/m1/1/treatment",
    }
    save_run_artifacts(arm_dir, record, "STDOUT:\nAgent response\n\nSTDERR:\n", "")

    results_data = {
        "name": "test_experiment",
        "harness": "claude",
        "models": ["m1"],
        "tasks_count": 1,
        "runs": {"treatment": [record]},
    }
    (run_dir / "results.json").write_text(json.dumps(results_data), encoding="utf-8")

    summary_scout = export_run_to_scout(run_dir, format="scout")
    scout_dir = run_dir / "scout"
    assert scout_dir.is_dir()
    assert (scout_dir / "t1_m1_1_treatment.json").exists()
    assert (scout_dir / "transcripts.jsonl").exists()
    assert (scout_dir / "metadata.json").exists()
    assert summary_scout["count"] == 1

    custom_out = tmp_path / "json_export"
    summary_json = export_run_to_scout(run_dir, output_dir=custom_out, format="json")
    assert (custom_out / "transcripts.json").exists()
    loaded_json = json.loads((custom_out / "transcripts.json").read_text(encoding="utf-8"))
    assert len(loaded_json) == 1
    assert loaded_json[0]["id"] == "t1_m1_1_treatment"
    assert summary_json["count"] == 1

    jsonl_out = tmp_path / "jsonl_export"
    summary_jsonl = export_run_to_scout(run_dir, output_dir=jsonl_out, format="jsonl")
    assert (jsonl_out / "transcripts.jsonl").exists()
    lines = (jsonl_out / "transcripts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert summary_jsonl["count"] == 1


def test_export_invalid_format(tmp_path: Path):
    with pytest.raises(ValueError, match="Unsupported format"):
        export_run_to_scout(tmp_path, format="invalid_format")


def test_cmd_export_cli(tmp_path: Path, capsys):
    run_dir = tmp_path / "eval"
    run_dir.mkdir()
    results_data = {
        "name": "cli_eval",
        "harness": "claude",
        "models": ["m"],
        "tasks_count": 1,
        "runs": {
            "control": [{"task_id": "t", "model": "m", "repetition": 1, "arm": "control"}],
        },
    }
    (run_dir / "results.json").write_text(json.dumps(results_data), encoding="utf-8")

    args = Namespace(run_dir=str(run_dir), format="scout", output_dir=None, config=None)
    ret = cmd_export(args)
    assert ret == 0
    captured = capsys.readouterr()
    assert "Exported 1 transcript(s) for Inspect Scout (scout)" in captured.out

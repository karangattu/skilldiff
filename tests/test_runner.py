import json
import time
from pathlib import Path

from skilldiff.config import ClaudeConfig, ExperimentConfig
from skilldiff.runner import AgentRunner, detect_skill_reference, parse_claude_output


def test_runner_allows_fixture_edits_and_shiny_docs_without_prompts(
    tmp_path: Path, monkeypatch
) -> None:
    """Catch unattended runs that silently deny the tools being evaluated."""
    capture = tmp_path / "args.txt"
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$SKILLDIFF_ARG_CAPTURE"\n'
        "printf '%s\\n' '{\"result\":\"ok\"}'\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("SKILLDIFF_ARG_CAPTURE", str(capture))

    config = ClaudeConfig(
        permission_mode="acceptEdits",
        allowed_tools=["Bash(shiny docs *)"],
    )
    AgentRunner(claude_bin=str(fake_claude)).run(
        "Edit the fixture", tmp_path, "claude-sonnet-5", config
    )

    args = capture.read_text().splitlines()
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    allowed = args[args.index("--allowedTools") + 1]
    assert "Bash(shiny docs *)" in allowed


def test_runner_removes_api_credentials_for_subscription_auth(
    tmp_path: Path, monkeypatch
) -> None:
    capture = tmp_path / "env.txt"
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "${ANTHROPIC_API_KEY-unset}" > "$SKILLDIFF_ENV_CAPTURE"\n'
        'printf "%s\\n" "${ANTHROPIC_AUTH_TOKEN-unset}" >> "$SKILLDIFF_ENV_CAPTURE"\n'
        "printf '%s\\n' '{\"result\":\"ok\"}'\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("SKILLDIFF_ENV_CAPTURE", str(capture))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-api-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-auth-token")

    AgentRunner(claude_bin=str(fake_claude)).run(
        "Edit the fixture", tmp_path, "claude-sonnet-5", ClaudeConfig()
    )

    assert capture.read_text().splitlines() == ["unset", "unset"]


def test_runner_preserves_api_credentials_for_api_key_auth(
    tmp_path: Path, monkeypatch
) -> None:
    capture = tmp_path / "env.txt"
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$ANTHROPIC_API_KEY" > "$SKILLDIFF_ENV_CAPTURE"\n'
        "printf '%s\\n' '{\"result\":\"ok\"}'\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("SKILLDIFF_ENV_CAPTURE", str(capture))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-api-key")

    config = ClaudeConfig(auth="api_key")
    AgentRunner(claude_bin=str(fake_claude)).run(
        "Edit the fixture", tmp_path, "claude-sonnet-5", config
    )

    assert capture.read_text().strip() == "test-api-key"


STREAM = "\n".join(
    json.dumps(e)
    for e in [
        {"type": "system", "subtype": "init", "skills": ["other", "my-skill"]},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Let me check the skill."},
                    {"type": "tool_use", "name": "Skill", "input": {"skill": "my-skill"}},
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}}]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Done.",
            "num_turns": 3,
            "total_cost_usd": 0.12,
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 5000,
                "cache_creation_input_tokens": 700,
                "output_tokens": 300,
            },
        },
    ]
)


def test_parse_claude_stream_json_detects_skill_and_cache_tokens() -> None:
    parsed = parse_claude_output(STREAM, ["my-skill"])
    assert parsed["response"] == "Done."
    assert parsed["skill_available"] is True
    assert parsed["skill_invoked"] is True
    assert parsed["tool_calls"] == 2
    assert parsed["num_turns"] == 3
    assert parsed["cost"] == 0.12
    assert parsed["cache_read_tokens"] == 5000
    assert parsed["cache_creation_tokens"] == 700
    assert parsed["is_error"] is False


def test_parse_claude_detects_skill_read_via_file_path() -> None:
    stream = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/tmp/ws/.claude/skills/my-skill/SKILL.md"},
                    }
                ]
            },
        }
    ) + "\n" + json.dumps({"type": "result", "result": "ok", "subtype": "success"})
    assert parse_claude_output(stream, ["my-skill"])["skill_invoked"] is True
    assert parse_claude_output(stream, ["other-skill"])["skill_invoked"] is False


def test_parse_claude_marks_error_results() -> None:
    stream = json.dumps(
        {"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 30}
    )
    parsed = parse_claude_output(stream, [])
    assert parsed["is_error"] is True
    assert parsed["error"] == "error_max_turns"


def _fake_claude(tmp_path: Path, body: str) -> Path:
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n" + body)
    fake.chmod(0o755)
    return fake


def test_claude_command_flags_and_error_status(tmp_path: Path, monkeypatch) -> None:
    capture = tmp_path / "args.txt"
    monkeypatch.setenv("SKILLDIFF_ARG_CAPTURE", str(capture))
    fake = _fake_claude(
        tmp_path,
        'printf "%s\\n" "$@" > "$SKILLDIFF_ARG_CAPTURE"\n'
        # stdin must be closed so claude -p doesn't wait for piped input
        'if read line; then echo "stdin was open" >&2; fi\n'
        "printf '%s\\n' '{\"type\":\"result\",\"is_error\":true,"
        "\"result\":\"Not logged in\"}'\n",
    )
    res = AgentRunner(claude_bin=str(fake)).run(
        "Do it", tmp_path, "sonnet", ClaudeConfig(max_turns=7), skill_names=["s"]
    )
    args = capture.read_text().splitlines()
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in args
    assert "--no-session-persistence" in args
    assert args[args.index("--setting-sources") + 1] == "project,local"
    assert args[args.index("--max-turns") + 1] == "7"
    assert res.status == "error"
    assert res.exit_code != 0
    assert res.error == "Not logged in"
    assert "stdin was open" not in res.transcript


def test_claude_isolation_can_be_disabled(tmp_path: Path, monkeypatch) -> None:
    capture = tmp_path / "args.txt"
    monkeypatch.setenv("SKILLDIFF_ARG_CAPTURE", str(capture))
    fake = _fake_claude(tmp_path, 'printf "%s\\n" "$@" > "$SKILLDIFF_ARG_CAPTURE"\n')
    AgentRunner(claude_bin=str(fake)).run("x", tmp_path, "sonnet", ClaudeConfig(isolate=False))
    assert "--setting-sources" not in capture.read_text().splitlines()


def test_runner_times_out_and_kills_children(tmp_path: Path) -> None:
    # The background child keeps a copy of stdout open; the run must still return.
    fake = _fake_claude(tmp_path, "sleep 30 &\nsleep 30\n")
    config = ExperimentConfig(
        name="t",
        skill=tmp_path,
        models=["sonnet"],
        tasks_patterns=[],
        timeout_seconds=1,
    )
    config.claude.bin_path = str(fake)
    start = time.perf_counter()
    res = AgentRunner().run("x", tmp_path, "sonnet", config)
    assert time.perf_counter() - start < 10
    assert res.status == "timeout"
    assert res.exit_code != 0


def test_detect_skill_reference() -> None:
    assert detect_skill_reference("cat .agents/skills/my-skill/SKILL.md", ["my-skill"])
    assert not detect_skill_reference("cat .agents/skills/my-skill-2/SKILL.md", ["my-skill"])

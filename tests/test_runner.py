from pathlib import Path

from skilldiff.config import ClaudeConfig
from skilldiff.runner import AgentRunner


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

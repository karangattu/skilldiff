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

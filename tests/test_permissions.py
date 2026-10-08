"""Headless permissions: sessions must neither prompt nor silently lose their tools."""

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from skilldiff.cli import permission_summary
from skilldiff.config import (
    AntigravityConfig,
    ClaudeConfig,
    CodexConfig,
    ExperimentConfig,
    OpenCodeConfig,
    load_experiment,
)
from skilldiff.diagnose import diagnose_run
from skilldiff.experiment import _run_warnings
from skilldiff.preflight import host_sandbox_fix, host_sandbox_problems
from skilldiff.runner import (
    AgentRunner,
    ExecResult,
    antigravity_permission_denials,
    claude_sandbox_settings,
    codex_permission_denials,
    codex_sandbox_args,
    opencode_approval_flag,
    opencode_permission_denials,
    parse_claude_output,
)


def _settings(cmd: list[str]) -> dict:
    return json.loads(cmd[cmd.index("--settings") + 1])


def test_claude_result_records_permission_denials():
    stdout = "\n".join(json.dumps(event) for event in (
        {"type": "system", "subtype": "init", "skills": []},
        {"type": "result", "subtype": "success", "result": "done", "num_turns": 2,
         "permission_denials": [
             {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "pytest"}},
             {"tool_name": "Edit", "tool_use_id": "t2", "tool_input": {}},
         ]},
    ))
    assert parse_claude_output(stdout, [])["permission_denials"] == ["Bash", "Edit"]
    clean = json.dumps({"type": "result", "result": "ok", "permission_denials": []})
    assert parse_claude_output(clean, [])["permission_denials"] == []
    assert parse_claude_output(json.dumps({"type": "result", "result": "ok"}), [])[
        "permission_denials"] is None


def test_claude_runs_bash_in_its_own_sandbox_by_default():
    cmd = AgentRunner(claude_bin="claude").claude_command(
        "hi", "sonnet", ClaudeConfig(), protected_paths=["/graders", "/skill"]
    )
    sandbox = _settings(cmd)["sandbox"]
    assert sandbox["enabled"] and sandbox["autoAllowBashIfSandboxed"]
    assert sandbox["failIfUnavailable"] and sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["filesystem"]["denyRead"] == ["/graders", "/skill"]
    assert "network" not in sandbox
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"

    domains = claude_sandbox_settings(ClaudeConfig(allowed_domains=["pypi.org"]))
    assert domains["sandbox"]["network"] == {"allowedDomains": ["pypi.org"]}
    assert claude_sandbox_settings(ClaudeConfig(sandbox=False)) is None


@pytest.mark.parametrize("isolation", ["macos", "docker", "podman"])
def test_external_isolation_replaces_claude_sandbox(isolation):
    cmd = AgentRunner(claude_bin="claude").claude_command(
        "hi", "sonnet", ClaudeConfig(), isolation=isolation
    )
    assert "--settings" not in cmd
    mode = cmd[cmd.index("--permission-mode") + 1]
    # Claude's seatbelt cannot nest in sandbox-exec; the outer boundary confines it.
    assert mode == ("bypassPermissions" if isolation == "macos" else "acceptEdits")


def test_codex_sandbox_is_bypassed_only_inside_external_isolation():
    assert codex_sandbox_args(CodexConfig()) == ["-s", "workspace-write"]
    assert codex_sandbox_args(CodexConfig(network_access=True)) == [
        "-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"]
    assert codex_sandbox_args(CodexConfig(sandbox="read-only", network_access=True)) == [
        "-s", "read-only"]
    for isolation in ("macos", "docker", "podman"):
        assert codex_sandbox_args(CodexConfig(), isolation) == [
            "--dangerously-bypass-approvals-and-sandbox"]


def test_opencode_uses_the_approval_flag_the_cli_advertises():
    assert opencode_approval_flag({"--auto", "--format"}) == "--auto"
    assert opencode_approval_flag({"--dangerously-skip-permissions"}) == (
        "--dangerously-skip-permissions")
    # Unknown capabilities: try the v1 name, then fall back after a rejection.
    assert opencode_approval_flag(set()) == "--dangerously-skip-permissions"
    assert opencode_approval_flag(set(), {"--dangerously-skip-permissions"}) == "--auto"
    assert opencode_approval_flag({"--format"}) is None


def test_harness_permission_keys_are_validated(tmp_path):
    (tmp_path / "skill").mkdir()
    (tmp_path / "skill" / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "t.yaml").write_text("id: t\nprompt: hi\n")
    path = tmp_path / "skilldiff.yaml"
    base = "name: x\nskill: ./skill\nmodels: [m]\ntasks: ['./tasks/*.yaml']\n"
    path.write_text(base + "claude:\n  sandbox: false\n  allowed_domains: [pypi.org]\n"
                    "codex:\n  network_access: true\n")
    cfg, _ = load_experiment(path)
    assert cfg.claude.sandbox is False and cfg.claude.allowed_domains == ["pypi.org"]
    assert cfg.codex.network_access is True
    for bad, key in (("claude:\n  sandbox: 'yes'\n", "claude.sandbox"),
                     ("claude:\n  allowed_domains: pypi.org\n", "claude.allowed_domains"),
                     ("codex:\n  network_access: 1\n", "codex.network_access")):
        path.write_text(base + bad)
        with pytest.raises(ValueError, match=key):
            load_experiment(path)


def test_check_states_what_sessions_may_do():
    cfg = ExperimentConfig(name="x", skill=None, models=["m"], tasks_patterns=[],
                           harness="claude")
    assert "Bash runs in Claude's sandbox" in permission_summary(cfg)[0][1]
    cfg.claude.sandbox = False
    level, message = permission_summary(cfg)[0]
    assert level == "warn" and "deny Bash" in message
    codex = ExperimentConfig(name="x", skill=None, models=["m"], tasks_patterns=[],
                             harness="codex")
    assert "network off" in permission_summary(codex)[0][1]
    codex.isolation = "macos"
    assert "macos isolation boundary" in permission_summary(codex)[0][1]


def test_denied_tools_are_reported_not_hidden(tmp_path):
    def run(arm, denials):
        return {"arm": arm, "task_id": "t", "model": "m", "repetition": 1, "status": "ok",
                "score": 1.0, "grade_status": "graded", "permission_denials": denials}

    control = [run("control", ["Bash", "Bash"])]
    treatment = [run("treatment", [])]
    warnings = _run_warnings(control, treatment, [])
    assert any("denied tool calls in 1 of 1 control runs (Bash ×2)" in w for w in warnings)
    # The advice covers every harness, not only Claude.
    assert any("codex.sandbox" in w and "OpenCode and Antigravity" in w for w in warnings)
    assert not any("denied tool calls" in w for w in _run_warnings(treatment, treatment, []))

    (tmp_path / "results.json").write_text(json.dumps(
        {"runs": {"control": control, "treatment": treatment}}))
    diag = diagnose_run(tmp_path)
    assert diag["permission_denied"] == [
        {"arm": "control", "task_id": "t", "model": "m", "rep": 1, "tools": ["Bash", "Bash"]}]
    assert any(r.startswith("Permission denials") for r in diag["recommendations"])


def test_agent_sandbox_around_skilldiff_is_detected(tmp_path, monkeypatch):
    for var in ("CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "CLAUDECODE", "OPENCODE",
                "ANTIGRAVITY_CONVERSATION_ID", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert host_sandbox_problems("claude", "local") == []

    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    assert any("network access is disabled" in p for p in host_sandbox_problems("codex", "local"))
    assert "prefix_rule" in host_sandbox_fix()
    # Container sessions keep their state in the image, not on this host.
    assert host_sandbox_problems("codex", "docker") == []


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX non-root")
def test_unwritable_harness_state_is_detected(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    monkeypatch.delenv("CODEX_SANDBOX_NETWORK_DISABLED", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    state = tmp_path / ".claude"
    state.mkdir()
    state.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        problems = host_sandbox_problems("claude", "local")
    finally:
        state.chmod(stat.S_IRWXU)
    assert len(problems) == 1 and f"cannot write {state}" in problems[0]


def test_parent_agent_session_variables_never_reach_child_clis(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_SESSION_ID", "parent")
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "parent")
    monkeypatch.setenv("CLAUDE_CODE_SIMPLE", "1")
    monkeypatch.setenv("KEEP_ME", "yes")
    result = AgentRunner()._exec(["/usr/bin/env"], Path(tmp_path), dict(os.environ), 10)
    assert "KEEP_ME=yes" in result.stdout
    for var in ("OPENCODE_SESSION_ID", "ANTIGRAVITY_CONVERSATION_ID", "CLAUDE_CODE_SIMPLE"):
        assert f"{var}=" not in result.stdout


# Lines recorded from `codex exec --json` stderr (codex-cli 0.160, workspace-write).
CODEX_STDERR = "\n".join((
    "2026-09-26T17:05:10.118Z  WARN codex_skills::interface: ignoring interface.icon_small: "
    "icon path with '..' must resolve under plugin assets/",
    "2026-09-26T17:05:14.392932Z  WARN codex_sandboxing::violation: recorded sandbox "
    "violation: resource=filesystem backend=seatbelt reason=operation_not_permitted "
    "path=unknown",
    "2026-09-26T17:09:02.516475Z ERROR codex_core::tools::router: error=exec_command failed: "
    "CreateProcess { message: \"Rejected(\\\"`/bin/zsh -lc 'rm -rf __pycache__ && git status "
    "--short'` rejected: rm -f style commands are not permitted. Use a safer approach\\\")\" }",
    # Not a permission: the shell binary was missing.
    "2026-09-26T17:09:05.000000Z ERROR codex_core::tools::router: error=exec_command failed: "
    "CreateProcess { message: \"Rejected(\\\"Failed to create unified exec process: No such "
    "file or directory (os error 2)\\\")\" }",
    "2026-09-26T17:09:06.000000Z ERROR codex_core::tools::router: error=apply_patch "
    "verification failed: invalid patch: multiple operations target /w/test_app.py",
))

# `opencode run --format json` (v2.0) without --auto: the stderr notice, then the
# failed tool part carrying the headless rejection message.
OPENCODE_STDOUT = "\n".join(json.dumps(e) for e in (
    {"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}},
    {"type": "tool_use", "sessionID": "ses_1", "part": {
        "type": "tool", "tool": "bash", "callID": "call_1", "state": {
            "status": "error", "input": {"command": "pytest -q"},
            "error": "This non-interactive run cannot ask the user for permission, so the "
                     "request was rejected. Continue without this action."}}},
    # An ordinary tool failure is not a denial.
    {"type": "tool_use", "sessionID": "ses_1", "part": {
        "type": "tool", "tool": "read", "callID": "call_2", "state": {
            "status": "error", "input": {"filePath": "/w/.skilldiff-eval"},
            "error": "File not found: /w/.skilldiff-eval"}}},
    {"type": "tool_use", "sessionID": "ses_1", "part": {
        "type": "tool", "tool": "edit", "callID": "call_3", "state": {
            "status": "error", "input": {},
            "error": "The user has specified a rule which prevents you from using this "
                     "specific tool call."}}},
))
OPENCODE_STDERR = (
    "\x1b[93m\x1b[1m! \x1b[0mpermission requested: bash (pytest -q); auto-rejecting\n"
    "\x1b[93m\x1b[1m! \x1b[0mpermission requested: external_directory (/etc/*); "
    "auto-rejecting\n"
    "\x1b[93m\x1b[1m! \x1b[0mpermission requested: edit (app.py, test_app.py); "
    "auto-rejecting\n"
)


def test_codex_sandbox_and_policy_blocks_are_denials():
    assert codex_permission_denials(CODEX_STDERR) == [
        "shell (sandbox: filesystem)", "shell (exec policy)"]
    assert codex_permission_denials("") == []

    with patch.object(AgentRunner, "_exec", return_value=ExecResult(
            stdout=json.dumps({"type": "turn.completed", "usage": {}}),
            stderr=CODEX_STDERR, exit_code=0, duration=1.0)):
        res = AgentRunner(codex_bin="codex-mock").run("t", Path("."), "m", CodexConfig())
    assert res.permission_denials == ["shell (sandbox: filesystem)", "shell (exec policy)"]


def test_opencode_rejected_permissions_are_denials():
    # Two failed tool parts but three notices: v1 drops interrupted tools, so the
    # longer record wins and nothing is counted twice.
    assert opencode_permission_denials(OPENCODE_STDOUT, OPENCODE_STDERR) == [
        "bash", "external_directory", "edit"]
    assert opencode_permission_denials(OPENCODE_STDOUT, "") == ["bash", "edit"]
    assert opencode_permission_denials(OPENCODE_STDOUT.splitlines()[2], "") == []

    with patch.object(AgentRunner, "opencode_run_flags", return_value={"--auto", "--format"}), \
            patch.object(AgentRunner, "_exec", return_value=ExecResult(
                stdout=OPENCODE_STDOUT, stderr=OPENCODE_STDERR, exit_code=0, duration=1.0)):
        res = AgentRunner(opencode_bin="opencode-mock").run(
            "t", Path("."), "opencode/m", OpenCodeConfig(dangerously_skip_permissions=False))
    assert res.permission_denials == ["bash", "external_directory", "edit"]


def test_antigravity_soft_denials_are_denials():
    data = {"status": "SUCCESS", "response": "done", "denied_actions": [
        {"tool_name": "run_command", "reason": "requires approval"}, {"tool": "write_to_file"}]}
    assert antigravity_permission_denials(data, "") == ["run_command", "write_to_file"]
    # Print-mode notices on stderr (agy 1.3) when the JSON carries no list.
    stderr = (
        "the run_command, write_to_file tool(s) required approval that headless mode cannot "
        "prompt for, so they were auto-denied. Settings allow-rules do not apply; re-run with "
        "--dangerously-skip-permissions to auto-approve all tools.\n"
        "run_command required the command permission `pytest` that headless mode cannot "
        "prompt for, so it was auto-denied. Add an allow-rule under permissions.allow in "
        "settings.json (e.g. \"run_command(pytest)\")\n"
    )
    assert antigravity_permission_denials({"status": "SUCCESS"}, stderr) == [
        "run_command", "write_to_file", "run_command"]
    assert antigravity_permission_denials({"status": "SUCCESS"}, "") == []

    with patch.object(AgentRunner, "_exec", return_value=ExecResult(
            stdout=json.dumps(data), stderr="", exit_code=0, duration=1.0)):
        res = AgentRunner(antigravity_bin="agy-mock").run(
            "t", Path("."), "m", AntigravityConfig(dangerously_skip_permissions=False))
    assert res.permission_denials == ["run_command", "write_to_file"]

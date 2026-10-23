"""Headless permissions: sessions must neither prompt nor silently lose their tools."""

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, cast
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
    _CANARY_COMMANDS,
    _EVAL_CANARY_COMMANDS,
    AgentRunner,
    ExecResult,
    RunResult,
    antigravity_permission_denials,
    claude_sandbox_settings,
    claude_unrecognized_models,
    codex_permission_denials,
    codex_sandbox_args,
    opencode_approval_flag,
    opencode_permission_denials,
    parse_claude_output,
)


def _settings(cmd: list[str]) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(cmd[cmd.index("--settings") + 1]))


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


def test_claude_denial_reason_is_joined_from_permission_denied_event():
    # The result event lists the refused call without its reason; the reason is in
    # the preceding system event, joined by tool_use_id.
    reason = "Quoted text in this command can't be checked before it runs"
    stdout = "\n".join(json.dumps(event) for event in (
        {"type": "system", "subtype": "permission_denied", "tool_name": "Bash",
         "tool_use_id": "t9", "decision_reason": reason},
        {"type": "result", "subtype": "success", "result": "done", "permission_denials": [
            {"tool_name": "Bash", "tool_use_id": "t9", "tool_input": {"command": "x"}},
        ]},
    ))
    assert parse_claude_output(stdout, [])["permission_denials"] == [f"Bash ({reason})"]


def test_claude_result_records_served_models():
    stdout = json.dumps({"type": "result", "result": "ok", "modelUsage": {
        "claude-haiku-5-5": {"outputTokens": 3},
        "claude-haiku-4-5-20251001": {"outputTokens": 1},
    }})
    assert parse_claude_output(stdout, [])["served_models"] == [
        "claude-haiku-4-5-20251001", "claude-haiku-5-5"]
    assert parse_claude_output(json.dumps({"type": "result", "result": "ok"}), [])[
        "served_models"] is None


def test_claude_unrecognized_models_are_read_from_stderr():
    marker = '[claude-code:unrecognized_model] {"model":"claude-haiku-5-5","query_source":"sdk"}'
    assert claude_unrecognized_models(f"noise\n{marker}\n{marker}\n") == ["claude-haiku-5-5"]
    assert claude_unrecognized_models("plain stderr") is None
    assert claude_unrecognized_models("") is None


def _claude_result(**changes):
    base = {
        "prompt": "", "response": "", "transcript": "", "duration": 1.0, "cost": None,
        "input_tokens": None, "output_tokens": None, "tool_calls": 1, "exit_code": 0,
    }
    return RunResult(**(base | changes))  # type: ignore[arg-type]


def _command_transcript(commands, *, denials=None, failed=False):
    events = []
    for index, command in enumerate(commands, start=1):
        tool_id = f"t{index}"
        events.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "Bash",
                            "input": {"command": command},
                        }
                    ]
                },
            }
        )
        events.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "is_error": failed,
                            "content": "failed" if failed else "ok",
                        }
                    ]
                },
            }
        )
    events.append(
        {
            "type": "result",
            "subtype": "success",
            "result": "done",
            "permission_denials": denials or [],
        }
    )
    return "\n".join(json.dumps(event) for event in events)


def _command_result(commands, *, denials=None, failed=False):
    return _claude_result(
        transcript="STDOUT:\n"
        + _command_transcript(commands, denials=denials, failed=failed)
        + "\n\nSTDERR:\n",
        tool_calls=len(commands),
        permission_denials=denials,
    )


def test_auth_probe_reports_when_alias_is_served_as_another_model(monkeypatch):
    served = _claude_result(served_models=["claude-haiku-4-5-20251001"])
    monkeypatch.setattr(AgentRunner, "_run_claude", lambda self, *a, **k: served)
    probe = AgentRunner(claude_bin="claude").probe_claude_auth(
        "claude-haiku-5-5", ClaudeConfig()
    )
    assert probe.ok and probe.mismatch
    assert probe.served_models == ["claude-haiku-4-5-20251001"]


def test_auth_probe_is_silent_when_the_pinned_model_is_served(monkeypatch):
    pinned = _claude_result(served_models=["claude-haiku-5-5"])
    monkeypatch.setattr(AgentRunner, "_run_claude", lambda self, *a, **k: pinned)
    probe = AgentRunner(claude_bin="claude").probe_claude_auth(
        "claude-haiku-5-5", ClaudeConfig()
    )
    assert probe.ok and not probe.mismatch and not probe.unrecognized_models


def test_auth_probe_reports_an_unrecognized_model(monkeypatch):
    flagged = _claude_result(served_models=["claude-haiku-5-5"],
                             unrecognized_models=["claude-haiku-5-5"])
    monkeypatch.setattr(AgentRunner, "_run_claude", lambda self, *a, **k: flagged)
    probe = AgentRunner(claude_bin="claude").probe_claude_auth(
        "claude-haiku-5-5", ClaudeConfig()
    )
    assert probe.ok and probe.unrecognized_models == ["claude-haiku-5-5"]


def test_sandbox_canary_exercises_refused_shapes(monkeypatch):
    # These basic shapes were refused in a real run; the eval-only app shapes
    # are covered separately below.
    prompts = []
    denials = ["Bash (A variable in this command can't be checked before it runs)"]

    def fake_run(self, prompt, cwd, model, cfg, timeout=None, **kwargs):
        prompts.append(prompt)
        return _command_result(list(_CANARY_COMMANDS), denials=denials)

    monkeypatch.setattr(AgentRunner, "_run_claude", fake_run)
    probe = AgentRunner(claude_bin="claude").probe_claude_sandbox_canary(
        "claude-haiku-5-5", ClaudeConfig()
    )
    prompt = prompts[0]
    for shape in ('$(echo canary-out.txt)', "cd . && git --version", "{a,b}"):
        assert shape in prompt
    assert not probe.ok
    assert probe.denials == denials
    assert not probe.inconclusive


def test_sandbox_canary_is_inconclusive_when_commands_are_skipped(monkeypatch):
    monkeypatch.setattr(
        AgentRunner, "_run_claude", lambda self, *a, **k: _claude_result(tool_calls=2))
    probe = AgentRunner(claude_bin="claude").probe_claude_sandbox_canary(
        "claude-haiku-5-5", ClaudeConfig()
    )
    assert not probe.ok and probe.inconclusive and probe.missing


def test_claude_runs_bash_in_its_own_sandbox_by_default(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    cmd = AgentRunner(claude_bin="claude").claude_command(
        "hi",
        "sonnet",
        ClaudeConfig(),
        protected_paths=["/graders", "/skill"],
        temp_dir=scratch,
    )
    settings = _settings(cmd)
    sandbox = settings["sandbox"]
    assert sandbox["enabled"] and sandbox["autoAllowBashIfSandboxed"]
    assert sandbox["failIfUnavailable"] and sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["filesystem"]["denyRead"] == ["/graders", "/skill"]
    assert sandbox["filesystem"]["denyWrite"] == ["/graders", "/skill"]
    assert str(scratch.resolve()) in sandbox["filesystem"]["allowWrite"]
    assert "/tmp" in sandbox["filesystem"]["allowWrite"]
    if Path("/private/tmp").exists():
        assert "/private/tmp" in sandbox["filesystem"]["allowWrite"]
    assert settings["permissions"]["additionalDirectories"] == sandbox["filesystem"]["allowWrite"]
    assert "network" not in sandbox
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"

    domains = claude_sandbox_settings(ClaudeConfig(allowed_domains=["pypi.org"]))
    assert domains is not None
    assert domains["sandbox"]["network"] == {"allowedDomains": ["pypi.org"]}
    assert claude_sandbox_settings(ClaudeConfig(sandbox=False)) is None


@pytest.mark.parametrize("isolation", ["macos", "docker", "podman"])
def test_external_isolation_replaces_claude_sandbox(isolation):
    cmd = AgentRunner(claude_bin="claude").claude_command(
        "hi", "sonnet", ClaudeConfig(), isolation=isolation
    )
    assert "--settings" not in cmd
    mode = cmd[cmd.index("--permission-mode") + 1]
    # Claude's own sandbox cannot nest inside external isolation; the outer boundary confines it.
    assert mode == "bypassPermissions"


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
    assert opencode_approval_flag({"--dangerously-skip-permissions"}) is None
    assert opencode_approval_flag(set()) is None
    assert opencode_approval_flag({"--auto"}, {"--auto"}) is None


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
            stdout=json.dumps({"event": "result", "result": data}),
            stderr="", exit_code=0, duration=1.0)):
        res = AgentRunner(antigravity_bin="agy-mock").run(
            "t", Path("."), "m", AntigravityConfig(dangerously_skip_permissions=False))
    assert res.permission_denials == ["run_command", "write_to_file"]


def test_claude_permission_denial_reasons():
    event = {
        "type": "result",
        "subtype": "success",
        "result": "done",
        "permission_denials": [
            {
                "tool_name": "Bash",
                "reason": "Quoted text in this command can't be checked before it runs",
            },
            {
                "tool_name": "Bash",
                "message": "Parser skipped input between top-level statements",
            },
        ],
    }
    parsed = parse_claude_output(json.dumps(event), [])
    assert parsed["permission_denials"] == [
        "Bash (Quoted text in this command can't be checked before it runs)",
        "Bash (Parser skipped input between top-level statements)",
    ]


def test_probe_claude_sandbox_canary():

    runner = AgentRunner()
    denials = ["Bash (Quoted text in this command can't be checked before it runs)"]
    with patch.object(
        runner,
        "_run_claude",
        return_value=_command_result(list(_CANARY_COMMANDS), denials=denials),
    ):
        probe = runner.probe_claude_sandbox_canary(
            "claude-sonnet-5-5", ClaudeConfig())
    assert probe.ok is False
    assert probe.denials == denials
    assert probe.inconclusive is False


def test_eval_canary_adds_app_shapes(monkeypatch):
    prompts = []

    def fake_run(self, prompt, cwd, model, cfg, timeout=None, **kwargs):
        prompts.append(prompt)
        return _command_result([*_CANARY_COMMANDS, *_EVAL_CANARY_COMMANDS])

    monkeypatch.setattr(AgentRunner, "_run_claude", fake_run)
    probe = AgentRunner(claude_bin="claude").probe_claude_sandbox_canary(
        "claude-sonnet-5-5", ClaudeConfig(profile="eval")
    )
    assert probe.ok
    prompt = prompts[0]
    for shape in ("127.0.0.1", "tracked.txt", "$PID"):
        assert shape in prompt


def test_eval_profile_keeps_the_sandbox_and_adds_localhost(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    settings = claude_sandbox_settings(
        ClaudeConfig(profile="eval"), temp_dir=scratch
    )
    assert settings is not None
    sandbox = settings["sandbox"]
    assert sandbox["enabled"]
    assert sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["network"]["allowLocalBinding"] is True
    assert "localhost" in sandbox["network"]["allowedDomains"]
    cmd = AgentRunner(claude_bin="claude").claude_command(
        "hi", "claude-sonnet-5-5", ClaudeConfig(profile="eval"), temp_dir=scratch
    )
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"


def test_diagnose_reports_denied_patterns_and_asymmetry(tmp_path):
    control = [{
        "status": "ok",
        "task_id": "t1",
        "model": "m",
        "arm": "control",
        "repetition": 1,
        "permission_denials": [
            "Bash (Quoted text in this command can't be checked before it runs)",
            "Bash (Parser skipped input between top-level statements)",
        ],
    }]
    treatment = [{
        "status": "ok",
        "task_id": "t1",
        "model": "m",
        "arm": "treatment",
        "repetition": 1,
        "permission_denials": [],
    }]
    (tmp_path / "results.json").write_text(json.dumps(
        {"runs": {"control": control, "treatment": treatment}}
    ))
    diag = diagnose_run(tmp_path)
    rec = next(r for r in diag["recommendations"] if r.startswith("Permission denials"))
    assert "Parser skipped input between top-level statements" in rec
    assert "Quoted text in this command can't be checked before it runs" in rec
    assert "uneven across arms — confound" in rec



def test_diagnose_reports_model_alias_and_unrecognized_model(tmp_path):
    record = {
        "model": "haiku", "task_id": "t", "repetition": 1, "status": "ok",
        "grade_status": "graded", "score": 1.0, "success": True, "cost": 1.0,
        "duration": 10.0, "input_tokens": 1, "output_tokens": 1,
        "cache_read_tokens": 0, "cache_creation_tokens": 0, "skill_invoked": False,
        "served_models": ["claude-haiku-4-5-20251001"],
        "unrecognized_models": ["claude-haiku-5-5"],
    }
    data = {
        "name": "alias", "models": ["haiku"], "tasks": ["t"], "harness": "claude",
        "failure_policy": {"agent_failure": "exclude"},
        "runs": {"treatment": [record]},
    }
    path = tmp_path / "results.json"
    path.write_text(json.dumps(data))
    report = diagnose_run(path)
    text = " ".join(report["recommendations"])
    assert "Model alias: haiku served as claude-haiku-4-5-20251001" in text
    assert "Unrecognized models: the Claude CLI does not recognize claude-haiku-5-5" in text
    assert report["model_alias_mismatches"][0]["model"] == "haiku"
    assert report["unrecognized_model_runs"][0]["models"] == ["claude-haiku-5-5"]


def test_diagnose_reports_rate_limit_retries(tmp_path):
    runs = {
        "control": [
            {"status": "ok", "task_id": "t", "model": "m", "arm": "control",
             "repetition": 1, "score": 1.0, "grade_status": "graded"}
        ],
        "treatment": [
            {"status": "ok", "task_id": "t", "model": "m", "arm": "treatment",
             "repetition": 1, "score": 1.0, "grade_status": "graded"}
        ],
    }
    (tmp_path / "results.json").write_text(
        json.dumps(
            {
                "runs": runs,
                "retries": [
                    {"model": "m", "task_id": "t", "arm": "treatment",
                     "status": "rate_limit", "cost": 0.01}
                ],
            }
        )
    )
    report = diagnose_run(tmp_path)
    assert report["rate_limit_retries"]["count"] == 1
    assert report["harness_diagnostics"]["retries"]["by_arm"] == {"treatment": 1}
    assert any("Rate-limit retries" in item for item in report["recommendations"])


def test_probe_claude_commands_reports_omissions_and_failures(monkeypatch):
    runner = AgentRunner(claude_bin="claude")
    command = "python3 -c \"print('app')\""
    monkeypatch.setattr(
        AgentRunner,
        "_run_claude",
        lambda self, *a, **k: _command_result([command], failed=True),
    )
    failed = runner.probe_claude_commands(
        "claude-sonnet-5-5", ClaudeConfig(), Path("."), [command]
    )
    assert not failed.ok and failed.failed and not failed.missing

    monkeypatch.setattr(
        AgentRunner, "_run_claude", lambda self, *a, **k: _claude_result(tool_calls=1)
    )
    missing = runner.probe_claude_commands(
        "claude-sonnet-5-5", ClaudeConfig(), Path("."), [command]
    )
    assert not missing.ok and missing.inconclusive


def test_auth_probe_budget_scales_from_the_rate_table(monkeypatch):
    captured = {}

    def fake_run(self, prompt, cwd, model, cfg, timeout=None, **kwargs):
        captured["budget"] = cfg.max_budget_usd
        return _claude_result(served_models=[model])

    monkeypatch.setattr(AgentRunner, "_run_claude", fake_run)
    from skilldiff.pricing import merge_pricing

    pricing, _ = merge_pricing({}, ["claude-sonnet-5-5"], "claude")
    probe = AgentRunner(claude_bin="claude").probe_claude_auth(
        "claude-sonnet-5-5", ClaudeConfig(), pricing=pricing
    )
    assert probe.ok
    assert captured["budget"] > 0.25


def test_check_fails_when_the_canary_is_denied(tmp_path, monkeypatch, capsys, capable_cli):
    from argparse import Namespace

    from skilldiff.cli import cmd_check
    from skilldiff.runner import ClaudeCommandProbe, ClaudeModelProbe

    monkeypatch.setenv("CLAUDE_BIN", sys.executable)
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\n")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "t.yaml").write_text("id: t\nprompt: hi\n")
    (tmp_path / "skilldiff.yaml").write_text(
        "name: t\nskill: ./skill\nmodels: [claude-sonnet-5-5]\n"
        "tasks: [./tasks/*.yaml]\n"
    )
    monkeypatch.setattr(
        AgentRunner,
        "probe_claude_auth",
        lambda self, model, cfg, pricing=None, timeout=90: ClaudeModelProbe(
            configured=model, ok=True, detail="ok", served_models=[model]
        ),
    )
    monkeypatch.setattr(
        AgentRunner,
        "probe_claude_sandbox_canary",
        lambda self, model, cfg, pricing=None, timeout=180: ClaudeCommandProbe(
            ok=False,
            denials=["Bash (Quoted text in this command can't be checked before it runs)"],
        ),
    )
    code = cmd_check(Namespace(config=str(tmp_path / "skilldiff.yaml"), no_grade=True))
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL  claude sandbox canary" in out
    assert "Quoted text" in out


def test_check_fails_when_the_served_model_differs(
    tmp_path, monkeypatch, capsys, capable_cli
):
    from argparse import Namespace

    from skilldiff.cli import cmd_check
    from skilldiff.runner import ClaudeCommandProbe, ClaudeModelProbe

    monkeypatch.setenv("CLAUDE_BIN", sys.executable)
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\n")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "t.yaml").write_text("id: t\nprompt: hi\n")
    (tmp_path / "skilldiff.yaml").write_text(
        "name: t\nskill: ./skill\nmodels: [claude-haiku-5-5]\n"
        "tasks: [./tasks/*.yaml]\n"
    )
    monkeypatch.setattr(
        AgentRunner,
        "probe_claude_auth",
        lambda self, model, cfg, pricing=None, timeout=90: ClaudeModelProbe(
            configured=model,
            ok=True,
            detail="ok",
            served_models=["claude-haiku-4-5-20251001"],
        ),
    )
    monkeypatch.setattr(
        AgentRunner,
        "probe_claude_sandbox_canary",
        lambda self, model, cfg, pricing=None, timeout=180: ClaudeCommandProbe(ok=True),
    )
    code = cmd_check(Namespace(config=str(tmp_path / "skilldiff.yaml"), no_grade=True))
    out = capsys.readouterr().out
    assert code == 1
    assert "configured 'claude-haiku-5-5' was served as claude-haiku-4-5-20251001" in out


def test_claude_assistant_usage_is_recorded_per_request():
    stdout = "\n".join(
        json.dumps(event)
        for event in (
            {
                "type": "assistant",
                "message": {
                    "id": "msg-1",
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 1000,
                        "cache_creation_input_tokens": 500,
                        "output_tokens": 20,
                    },
                    "content": [],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "msg-2",
                    "usage": {
                        "input_tokens": 12,
                        "cache_read_input_tokens": 1500,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 30,
                    },
                    "content": [],
                },
            },
            {"type": "result", "subtype": "success", "result": "done"},
        )
    )
    parsed = parse_claude_output(stdout, [])
    assert parsed["request_usage"] == [
        {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_tokens": 1000,
            "cache_creation_tokens": 500,
        },
        {
            "input_tokens": 12,
            "output_tokens": 30,
            "cache_read_tokens": 1500,
            "cache_creation_tokens": 0,
        },
    ]


def test_eval_profile_rejects_an_unsandboxed_local_config():
    with pytest.raises(ValueError, match="requires claude.sandbox"):
        AgentRunner(claude_bin="claude").claude_command(
            "hi", "claude-sonnet-5-5", ClaudeConfig(profile="eval", sandbox=False)
        )


def test_check_rejects_a_budget_below_the_observed_call_cost(
    tmp_path, monkeypatch, capsys, capable_cli
):
    from argparse import Namespace

    from skilldiff.cli import cmd_check
    from skilldiff.runner import ClaudeCommandProbe, ClaudeModelProbe

    monkeypatch.setenv("CLAUDE_BIN", sys.executable)
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\n")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "t.yaml").write_text("id: t\nprompt: hi\n")
    (tmp_path / "skilldiff.yaml").write_text(
        "name: t\nskill: ./skill\nmodels: [claude-sonnet-5-5]\n"
        "tasks: [./tasks/*.yaml]\nclaude:\n  max_budget_usd: 0.05\n"
    )
    monkeypatch.setattr(
        AgentRunner,
        "probe_claude_auth",
        lambda self, model, cfg, pricing=None, timeout=90: ClaudeModelProbe(
            configured=model, ok=True, detail="ok", served_models=[model], cost=0.02
        ),
    )
    monkeypatch.setattr(
        AgentRunner,
        "probe_claude_sandbox_canary",
        lambda self, model, cfg, pricing=None, timeout=180: ClaudeCommandProbe(ok=True),
    )
    code = cmd_check(Namespace(config=str(tmp_path / "skilldiff.yaml"), no_grade=True))
    out = capsys.readouterr().out
    assert code == 1
    assert "below four times the observed" in out


def test_check_refuses_harness_cost_for_an_unrecognized_model(
    tmp_path, monkeypatch, capsys, capable_cli
):
    from argparse import Namespace

    from skilldiff.cli import cmd_check
    from skilldiff.runner import ClaudeCommandProbe, ClaudeModelProbe

    monkeypatch.setenv("CLAUDE_BIN", sys.executable)
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\n")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "t.yaml").write_text("id: t\nprompt: hi\n")
    (tmp_path / "skilldiff.yaml").write_text(
        "name: t\nskill: ./skill\nmodels: [claude-sonnet-5-5]\n"
        "tasks: [./tasks/*.yaml]\ncost_basis: harness\n"
    )
    monkeypatch.setattr(
        AgentRunner,
        "probe_claude_auth",
        lambda self, model, cfg, pricing=None, timeout=90: ClaudeModelProbe(
            configured=model,
            ok=True,
            detail="ok",
            served_models=[model],
            unrecognized_models=[model],
        ),
    )
    monkeypatch.setattr(
        AgentRunner,
        "probe_claude_sandbox_canary",
        lambda self, model, cfg, pricing=None, timeout=180: ClaudeCommandProbe(ok=True),
    )
    code = cmd_check(Namespace(config=str(tmp_path / "skilldiff.yaml"), no_grade=True))
    out = capsys.readouterr().out
    assert code == 1
    assert "cost_basis is harness" in out


def test_check_fails_when_a_task_runtime_probe_is_refused(
    tmp_path, monkeypatch, capsys, capable_cli
):
    from argparse import Namespace

    from skilldiff.cli import cmd_check
    from skilldiff.runner import ClaudeCommandProbe, ClaudeModelProbe

    monkeypatch.setenv("CLAUDE_BIN", sys.executable)
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\n")
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "app.py").write_text("ok = True\n")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "t.yaml").write_text(
        "id: t\nprompt: hi\nrepo: ../fixture\n"
        'runtime_probe: "python3 -c \\"print(1)\\""\n'
    )
    (tmp_path / "skilldiff.yaml").write_text(
        "name: t\nskill: ./skill\nmodels: [claude-sonnet-5-5]\n"
        "tasks: [./tasks/*.yaml]\n"
    )
    monkeypatch.setattr(
        AgentRunner,
        "probe_claude_auth",
        lambda self, model, cfg, pricing=None, timeout=90: ClaudeModelProbe(
            configured=model, ok=True, detail="ok", served_models=[model]
        ),
    )
    monkeypatch.setattr(
        AgentRunner,
        "probe_claude_sandbox_canary",
        lambda self, model, cfg, pricing=None, timeout=180: ClaudeCommandProbe(ok=True),
    )
    monkeypatch.setattr(AgentRunner, "preflight", lambda self, *a, **k: {})
    monkeypatch.setattr(
        AgentRunner,
        "probe_claude_commands",
        lambda self, *a, **k: ClaudeCommandProbe(
            ok=False, denials=["Bash (runtime probe denied)"]
        ),
    )
    code = cmd_check(Namespace(config=str(tmp_path / "skilldiff.yaml"), no_grade=True))
    out = capsys.readouterr().out
    assert code == 1
    assert "runtime_probe refused" in out

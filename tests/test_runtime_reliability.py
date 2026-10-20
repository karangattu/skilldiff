"""Regressions from the October main-versus-PR evaluation, without model calls."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from skilldiff.cli import build_parser
from skilldiff.config import ExperimentConfig, load_experiment
from skilldiff.experiment import ExperimentRunner
from skilldiff.runner import AgentRunner


@pytest.mark.parametrize("command", ["check", "run"])
def test_help_renders_percentages(capsys, command):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([command, "--help"])
    assert exc.value.code == 0
    assert "100%" in capsys.readouterr().out


@pytest.mark.parametrize("stdout,stderr,exit_code", [
    ("missing browser executable", "Traceback: assertion failed", 1),
    ("", "git: error: Operation not permitted", 0),
])
def test_runtime_probe_explains_both_output_streams_and_refuses_hidden_denials(
    tmp_path, capable_cli, stdout, stderr, exit_code
):
    cfg = ExperimentConfig(name="test", skill=None, models=["m"], tasks_patterns=[])
    probe = (f"printf %s {shlex.quote(stdout)}; "
             f"printf %s {shlex.quote(stderr)} >&2; exit {exit_code}")
    with pytest.raises(RuntimeError) as exc:
        AgentRunner().preflight(tmp_path, cfg, [], runtime_probe=probe)
    message = str(exc.value)
    assert stdout in message and stderr in message


def test_yaml_serial_parallel_setting_is_not_replaced_by_auto_workers(
    tmp_path, monkeypatch, capable_cli
):
    monkeypatch.delenv("SKILLDIFF_MOCK_RUNNER", raising=False)
    agent = tmp_path / "agent"
    agent.write_text("#!/bin/sh\nprintf '%s\\n' "
                     "'{\"type\":\"result\",\"result\":\"done\",\"num_turns\":1}'\n")
    agent.chmod(0o755)
    (tmp_path / "skill").mkdir()
    (tmp_path / "skill/SKILL.md").write_text("---\nname: sample\ndescription: Repair\n---\n")
    (tmp_path / "task.yaml").write_text("id: t\nprompt: Repair\n")
    config = tmp_path / "skilldiff.yaml"
    config.write_text(f"name: serial\nskill: ./skill\nmodels: [m]\ntasks: [task.yaml]\nruns: 2\n"
                      f"parallel: 1\narm_parallel: false\nclaude:\n  bin_path: {agent}\n")
    cfg, tasks = load_experiment(config)
    progress = []
    results = ExperimentRunner(cfg, tasks, output_dir=tmp_path / "runs",
                               progress=progress.append).run()
    assert len(results["runs"]["control"]) == 2
    assert not any("worker(s)" in line for line in progress), progress


def test_codex_nested_command_denials_reach_reports_and_diagnose(tmp_path, monkeypatch):
    from skilldiff.diagnose import diagnose_run
    from skilldiff.experiment import _run_warnings
    from skilldiff.runner import CodexConfig, ExecResult

    stream = "\n".join(json.dumps(event) for event in [
        {"type": "item.completed", "item": {"id": "bad", "type": "command_execution",
         "command": "pytest", "exit_code": 4, "status": "completed",
         "aggregated_output": "PermissionError: [Errno 1] Operation not permitted: '/parent'"}},
        # Successful output quoting error text is not a blocked operation.
        {"type": "item.completed", "item": {"id": "ok", "type": "command_execution",
         "command": "cat guide.md", "exit_code": 0, "status": "completed",
         "aggregated_output": "PermissionError: example only"}},
        {"type": "turn.completed", "usage": {"input_tokens": 50,
         "cached_input_tokens": 0, "output_tokens": 10}},
    ])
    monkeypatch.setattr(AgentRunner, "_exec", lambda *a, **kw: ExecResult(stream, "", 0, 1))
    result = AgentRunner()._run_codex("repair", tmp_path, "m", CodexConfig())
    assert result.status == "ok"
    assert result.permission_denials == ["shell (filesystem: Operation not permitted)"]
    record = {"arm": "control", "task_id": "t", "model": "m", "repetition": 1,
              "status": "ok", "grade_status": "graded", "score": 1,
              "permission_denials": result.permission_denials}
    assert any("denied tool calls" in w for w in _run_warnings([record], [], []))
    (tmp_path / "results.json").write_text(json.dumps({"runs": {"control": [record]}}))
    assert diagnose_run(tmp_path)["permission_denied"][0]["tools"] == result.permission_denials


@pytest.mark.parametrize("detail,kind", [
    ("workspace routing discovery failed: Connection refused", "harness_error"),
    ("429 Too Many Requests", "rate_limit"),
])
def test_codex_generic_error_keeps_service_cause_and_redacts_credentials(
    tmp_path, monkeypatch, detail, kind
):
    from skilldiff.runner import CodexConfig, ExecResult

    monkeypatch.setenv("TEST_API_TOKEN", "sensitive-test-value")
    stream = json.dumps({"type": "error", "error": {"message": "Codex failed"}})
    monkeypatch.setattr(AgentRunner, "_exec", lambda *a, **kw: ExecResult(
        stream, detail + " sensitive-test-value", 1, 1))
    result = AgentRunner()._run_codex("repair", tmp_path, "m", CodexConfig())
    assert result.status == "error"
    assert result.failure_kind == kind
    assert detail in result.error
    assert "sensitive-test-value" not in result.error + result.transcript


def test_malformed_codex_tool_result_stays_a_harness_error(tmp_path, monkeypatch):
    from skilldiff.runner import CodexConfig, ExecResult

    stream = json.dumps({"method": "item/completed", "params": ["invalid"]})
    monkeypatch.setattr(AgentRunner, "_exec", lambda *a, **kw: ExecResult(stream, "", 0, 1))
    result = AgentRunner()._run_codex("repair", tmp_path, "m", CodexConfig())
    assert result.status == "error"
    assert result.failure_kind == "harness_error"
    assert "Malformed structured output" in result.error


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS isolation")
def test_zsh_heredoc_uses_private_scratch_instead_of_inherited_prefix(tmp_path):
    workspace, scratch = tmp_path / "workspace", tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    script = "cat <<'END'\n" + "x" * 16000 + "\nEND\n"
    result = AgentRunner()._exec(
        ["/bin/zsh", "-c", script], workspace,
        {"PATH": os.environ["PATH"], "TMPPREFIX": "/not-readable/host-temp"}, 10,
        isolation="macos", temp_dir=scratch,
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "x" * 16000 + "\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS isolation")
def test_shell_can_launch_python_through_declared_runtime_alias(tmp_path):
    workspace, scratch = tmp_path / "workspace", tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    alias = tmp_path / "runtime-current"
    alias.symlink_to(sys.base_prefix, target_is_directory=True)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(alias / "bin" / Path(sys.executable).resolve().name)
    result = AgentRunner()._exec(
        ["/bin/sh", "-c", f"{shlex.quote(str(venv / 'bin/python'))} -c 'print(42)'"],
        workspace, {}, 10, isolation="macos", temp_dir=scratch,
        read_paths=[str(venv), sys.base_prefix],
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "42"


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS isolation")
@pytest.mark.parametrize("relative", ["package/.agents/skills/secret/SKILL.md",
                                     ".agents/skills/secret/SKILL.md"])
def test_package_metadata_can_stat_bundled_skills_but_cannot_read_them(tmp_path, relative):
    workspace, scratch, library = (tmp_path / n for n in ("workspace", "scratch", "library"))
    for folder in (workspace, scratch, library):
        folder.mkdir()
    skill = library / relative
    skill.parent.mkdir(parents=True)
    skill.write_text("secret instructions")
    info = library / "runtime_sample-1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Name: runtime-sample\nVersion: 1\n")
    (info / "RECORD").write_text(relative + ",,\n")
    code = f"""from importlib.metadata import distribution
from pathlib import Path
import sys
sys.path.insert(0, {str(library)!r})
assert len(distribution('runtime-sample').files) == 1
try: Path({str(skill)!r}).read_text()
except PermissionError: pass
else: raise AssertionError('skill content exposed')
print('verified')
"""
    result = AgentRunner()._exec(
        [sys.executable, "-c", code], workspace, {}, 10, isolation="macos",
        temp_dir=scratch, read_paths=[sys.prefix, sys.base_prefix, str(library)],
        protected_paths=[str(library / ".agents")],
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "verified"


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS isolation")
def test_pytest_collects_session_scratch_without_reading_sibling_files(tmp_path):
    workspace, scratch = tmp_path / "session/workspace", tmp_path / "session/scratch"
    workspace.mkdir(parents=True)
    scratch.mkdir()
    secret = tmp_path / "session/grader.py"
    secret.write_text("private grader")
    code = f"""import tempfile, pathlib, pytest
with tempfile.TemporaryDirectory() as directory:
    test = pathlib.Path(directory) / 'test_runtime.py'
    test.write_text('def test_ok(): assert 2 + 2 == 4\\n')
    assert pytest.main(['-q', str(test)]) == 0
try: pathlib.Path({str(secret)!r}).read_text()
except PermissionError: pass
else: raise AssertionError('sibling content exposed')
"""
    result = AgentRunner()._exec(
        [sys.executable, "-c", code], workspace, {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}, 20,
        isolation="macos", temp_dir=scratch, read_paths=[sys.prefix, sys.base_prefix],
    )
    assert result.exit_code == 0, result.stderr + result.stdout
    assert "1 passed" in result.stdout
    assert "PytestCacheWarning" not in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS isolation")
def test_git_works_without_host_config_or_xcrun_cache_writes(tmp_path):
    workspace, scratch = tmp_path / "workspace", tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    result = AgentRunner()._exec(
        ["/bin/sh", "-c", "git status --porcelain"], workspace,
        {"PATH": "/usr/bin:/bin", "HOME": str(Path.home())}, 10,
        isolation="macos", temp_dir=scratch,
    )
    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS isolation")
def test_native_codex_shell_gets_configured_environment_without_secrets_in_argv(
    tmp_path, monkeypatch
):
    from skilldiff.runner import CodexConfig

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    workspace, scratch = tmp_path / "workspace", tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    agent = tmp_path / "agent"
    agent.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, stat, subprocess, sys
try: import tomllib
except ImportError: import tomli as tomllib
config=pathlib.Path(os.environ['CODEX_HOME'])/'config.toml'
settings=tomllib.loads(config.read_text())['shell_environment_policy']['set']
assert stat.S_IMODE(config.stat().st_mode) == 0o600
assert settings['MY_TOKEN'] == 'explicit-test-secret'
assert 'explicit-test-secret' not in str(sys.argv)
assert settings['MY_CACHE'] == '/warm-cache'
assert settings['TMPPREFIX'].startswith(os.environ['TMPDIR'])
assert settings['GIT_CONFIG_GLOBAL'] == '/dev/null'
assert settings['PATH'].startswith('/custom-bin:')
subprocess.run(['/bin/zsh', '-c', 'cat <<END\\nready\\nEND'],
               env={**os.environ, **settings}, check=True, capture_output=True)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':50,
                  'cached_input_tokens':0,'output_tokens':5}}))
''')
    agent.chmod(0o755)
    result = AgentRunner()._run_codex(
        "repair", workspace, "m", CodexConfig(bin_path=str(agent),
                                               read_paths=[sys.prefix, sys.base_prefix]),
        isolation="macos", temp_dir=scratch,
        extra_env={"PATH": "/custom-bin:/usr/bin:/bin", "MY_CACHE": "/warm-cache",
                   "MY_TOKEN": "explicit-test-secret"},
    )
    assert result.status == "ok", result.transcript
    assert "explicit-test-secret" not in result.transcript


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS isolation")
def test_runtime_launcher_alias_does_not_grant_read_access_to_undeclared_targets(tmp_path):
    workspace, scratch, runtime = (tmp_path / p for p in ("workspace", "scratch", "runtime"))
    for p in (workspace, scratch, runtime / "bin"):
        p.mkdir(parents=True)
    secret = tmp_path / "secret"
    secret.write_text("not a runtime")
    alias = runtime / "bin/python-secret"
    alias.symlink_to(secret)
    result = AgentRunner()._exec(
        ["/bin/cat", str(alias)], workspace, {}, 10, isolation="macos", temp_dir=scratch,
        read_paths=[str(runtime)],
    )
    assert result.exit_code != 0
    assert "not a runtime" not in result.stdout


@pytest.mark.parametrize("replacement,comment,expected", [
    (("'alpha'", "'beta'"), "", 1),
    (("'left_card'", "'right_card'"), "", 1),
    (("'same'", "'same'"), '# "alpha" and "beta" are old labels\n', 2 / 3),
])
def test_example_module_grader_accepts_equivalent_names_and_ignores_comments(
    tmp_path, replacement, comment, expected
):
    pack = Path(__file__).resolve().parents[1] / "eval/py-shiny-troubleshooting"
    source = (pack / "solutions/module-namespace-collision/good-1/app.py").read_text()
    source = comment + source.replace('"alpha"', replacement[0]).replace('"beta"', replacement[1])
    (tmp_path / "app.py").write_text(source)
    result = subprocess.run(
        [sys.executable, str(pack / "graders/grade_module_namespace_collision.py")],
        cwd=tmp_path, check=True, text=True, capture_output=True,
    )
    assert json.loads(result.stdout)["score"] == pytest.approx(expected)

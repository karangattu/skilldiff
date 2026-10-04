"""Regression contracts for trustworthy execution and grading evidence."""

import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from skilldiff.config import AntigravityConfig, ExperimentConfig, GraderConfig, TaskConfig
from skilldiff.experiment import ExperimentRunner
from skilldiff.grader import Grader, GradeResult, validate_grader_against_directories
from skilldiff.runner import AgentRunner, ExecResult
from skilldiff.workspace import Workspace


def executable(path, body):
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize("kind", ["llm", "rubric"])
def test_judge_without_command_cannot_fabricate_grade(tmp_path, monkeypatch, kind):
    monkeypatch.delenv("SKILLDIFF_MOCK_RUNNER", raising=False)
    result = Grader(
        GraderConfig(type=kind, rubric="Reject empty solutions"), "s", "m"
    ).grade_workspace(tmp_path)
    assert result.score is None
    assert result.success is None
    assert result.grade_status == "error"


@pytest.mark.parametrize("payload", ["not JSON", '{"feedback":"no verdict"}'])
def test_judge_malformed_verdict_cannot_become_perfect_score(tmp_path, monkeypatch, payload):
    monkeypatch.delenv("SKILLDIFF_MOCK_RUNNER", raising=False)
    result = Grader(
        GraderConfig(type="llm", command="printf '%s' " + shlex.quote(payload)), "s", "m"
    ).grade_workspace(tmp_path)
    assert result.grade_status == "error"
    assert result.score is None


def test_actual_judge_can_reject_empty_candidate(tmp_path, monkeypatch):
    monkeypatch.delenv("SKILLDIFF_MOCK_RUNNER", raising=False)
    script = tmp_path / "judge.py"
    script.write_text(
        "import os,pathlib,json\n"
        "p=pathlib.Path(os.environ['SKILLDIFF_JUDGE_PROMPT_FILE']).read_text()\n"
        "assert 'Reject empty' in p\nprint(json.dumps({'score':0,'success':False}))\n"
    )
    result = Grader(
        GraderConfig(
            type="rubric",
            rubric="Reject empty",
            command=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
        ),
        "s",
        "m",
    ).grade_workspace(tmp_path)
    assert result.grade_status == "graded"
    assert result.score == 0
    assert result.success is False


@pytest.mark.parametrize(
    "harness,payload",
    [
        ("claude", '{"type":"result","result":"ok"}'),
        ("codex", '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}'),
        ("opencode", '{"type":"text","part":{"text":"ok"}}'),
    ],
)
def test_other_harnesses_preserve_missing_multiturn_measurements(tmp_path, harness, payload):
    binary = executable(tmp_path / "fake-cli", "print(" + repr(payload) + ")\n")
    config = ExperimentConfig(
        name="t", skill=tmp_path, models=["m"], tasks_patterns=[], harness=harness
    )
    getattr(config, harness).bin_path = binary
    result = AgentRunner().run(["one", "two"], tmp_path, "m", config)
    assert result.status == "ok"
    for name in ("cost", "input_tokens", "output_tokens", "num_turns", "cache_read_tokens"):
        assert getattr(result, name) is None, name


@pytest.mark.parametrize("git_state", ["unstaged", "staged", "committed"])
def test_final_diff_retains_renames_deletions_and_additions(tmp_path, git_state):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "old name.txt").write_text("rename me\n" * 10)
    (fixture / "remove.txt").write_text("remove me\n")
    workspace = Workspace(tmp_path / "work", False, None, fixture)
    workspace.setup()
    (workspace.root / "old name.txt").rename(workspace.root / "new name.txt")
    (workspace.root / "remove.txt").unlink()
    (workspace.root / "protected file.txt").write_text("forbidden\n")
    if git_state != "unstaged":
        subprocess.run(["git", "add", "-A"], cwd=workspace.root, check=True)
    if git_state == "committed":
        subprocess.run(["git", "commit", "-qm", "agent edits"], cwd=workspace.root, check=True)
    diff, files = workspace.get_diff()
    assert set(files) == {"old name.txt", "new name.txt", "remove.txt", "protected file.txt"}
    assert "forbidden" in diff
    assert "remove me" in diff
    assert "new name.txt" in diff
    grade = Grader(GraderConfig(command="exit 0"), "s", "m", forbidden_paths=["protected file.txt"])
    result, _ = grade.grade_pair(
        workspace.root, workspace.root, "", "", diff, diff, "", "", files, files
    )
    assert result.grade_status == "error"
    assert result.success is False


def test_missing_initial_commit_refuses_partial_diff(tmp_path):
    workspace = Workspace(tmp_path / "work", False, None)
    workspace.setup()
    subprocess.run(["git", "init", "--bare", "-q", str(tmp_path / "empty.git")], check=True)
    import shutil

    shutil.rmtree(workspace.root / ".git")
    shutil.copytree(tmp_path / "empty.git", workspace.root / ".git")
    with pytest.raises((ValueError, RuntimeError, subprocess.CalledProcessError)):
        workspace.get_diff()


def test_ignored_fixture_content_is_part_of_initial_comparison(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / ".gitignore").write_text("data.txt\n")
    (fixture / "data.txt").write_text("initial")
    workspace = Workspace(tmp_path / "work", False, None, fixture)
    workspace.setup()
    assert workspace.get_diff() == ("", [])
    (workspace.root / "data.txt").write_text("changed")
    diff, names = workspace.get_diff()
    assert names == ["data.txt"]
    assert "initial" in diff and "changed" in diff


@pytest.mark.parametrize(
    "bad_result",
    [
        GradeResult(None, None, "candidate-A", grade_status="error"),
        GradeResult(None, None, "candidate-A", grade_status="timeout"),
        GradeResult(None, None, "candidate-A", grade_status="ungraded"),
        GradeResult(float("nan"), False, "candidate-A"),
    ],
)
def test_broken_fixture_requires_an_actual_failing_grade(tmp_path, bad_result):
    # Grading is an external boundary; exercise validation with each documented outcome.
    class Outcomes(Grader):
        def grade_workspace(self, workspace_dir):
            return {
                "untouched": GradeResult(0, False, "candidate-A"),
                "good": GradeResult(1, True, "candidate-A"),
                "broken": bad_result,
            }[workspace_dir.name]

    report = validate_grader_against_directories(
        Outcomes(None, "s", "m"), tmp_path / "untouched", tmp_path / "good", [tmp_path / "broken"]
    )
    assert report["verdict"] == "grader-broken"


def test_grader_timeout_kills_delayed_child(tmp_path):
    sentinel = tmp_path / "late.txt"
    child = "import time,pathlib;time.sleep(.4);pathlib.Path('late.txt').write_text('alive')"
    code = (
        f"import subprocess,time;subprocess.Popen([{sys.executable!r},'-c',"
        f"{child!r}]);time.sleep(5)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    result = Grader(GraderConfig(command=command), "s", "m", timeout=0.08).grade_workspace(tmp_path)
    assert result.grade_status == "timeout"
    time.sleep(0.5)
    assert not sentinel.exists(), "timed-out grader descendants must not mutate the candidate"


def test_completed_grader_kills_background_child(tmp_path):
    child = "import time,pathlib;time.sleep(.4);pathlib.Path('late.txt').write_text('alive')"
    code = f"import subprocess;subprocess.Popen([{sys.executable!r},'-c',{child!r}]);print(1)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    result = Grader(GraderConfig(command=command), "s", "m").grade_workspace(tmp_path)
    assert result.grade_status == "graded"
    time.sleep(0.5)
    assert not (tmp_path / "late.txt").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal/process-group contract")
def test_interrupted_grader_kills_background_child(tmp_path):
    child = "import time,pathlib;time.sleep(.8);pathlib.Path('late.txt').write_text('alive')"
    code = (
        f"import subprocess,pathlib,time;subprocess.Popen([{sys.executable!r},'-c',{child!r}]);"
        "pathlib.Path('ready.txt').write_text('ready');time.sleep(5)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    parent_code = (
        "from pathlib import Path\nfrom skilldiff.grader import Grader\n"
        "from skilldiff.config import GraderConfig\ntry:\n"
        f" Grader(GraderConfig(command={command!r}), 's', 'm').grade_workspace(Path.cwd())\n"
        "except KeyboardInterrupt:\n pass\n"
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    parent = subprocess.Popen([sys.executable, "-c", parent_code], cwd=tmp_path, env=env)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "ready.txt").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (tmp_path / "ready.txt").exists()
        os.kill(parent.pid, signal.SIGINT)
        assert parent.wait(timeout=3) == 0
        time.sleep(0.9)
        assert not (tmp_path / "late.txt").exists()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()


def test_local_grader_uses_the_windows_command_shell(tmp_path, monkeypatch):
    shell = "C:\\Windows\\System32\\cmd.exe"
    monkeypatch.setattr("skilldiff.grader.os", SimpleNamespace(
        name="nt", environ={"COMSPEC": shell},
    ))
    commands = []

    def execute(self, cmd, *args, **kwargs):
        commands.append(cmd)
        return ExecResult('{"score":1}', "", 0, 0)

    monkeypatch.setattr(AgentRunner, "_exec", execute)
    result = Grader(GraderConfig(command="echo grade"), "s", "m").grade_workspace(tmp_path)
    assert result.grade_status == "graded"
    assert commands == [[shell, "/c", "echo grade"]]


def test_antigravity_failure_keeps_first_attempt_and_edits(tmp_path):
    fake = executable(
        tmp_path / "agy",
        "import pathlib,json,sys\np=pathlib.Path('attempts')\n"
        "n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n))\n"
        "print(json.dumps({'status':'ERROR','error':'Unavailable','retryable':True,"
        "'cost':.75,'usage':{'input_tokens':100}}));sys.exit(1)\n",
    )
    result = AgentRunner(antigravity_bin=fake).run("x", tmp_path, "m", AntigravityConfig())
    assert (tmp_path / "attempts").read_text() == "1"
    assert result.status == "error"
    assert result.cost == 0.75
    assert result.input_tokens == 100


@pytest.mark.parametrize("partial", [False, True])
def test_multi_turn_preserves_unknown_metrics(tmp_path, partial):
    payload = "{'status':'SUCCESS','response':'ok'}"
    body = (
        "import pathlib,json\np=pathlib.Path('count')\n"
        "n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n))\nd=" + payload + "\n"
    )
    if partial:
        body += (
            "if n == 1:d.update(cost=.1,usage={'input_tokens':10,'output_tokens':2},"
            "tool_calls_count=1,num_turns=1)\n"
        )
    body += "print(json.dumps(d))\n"
    fake = executable(tmp_path / "agy", body)
    result = AgentRunner(antigravity_bin=fake).run(
        ["one", "two"], tmp_path, "m", AntigravityConfig()
    )
    for name in (
        "cost",
        "input_tokens",
        "output_tokens",
        "tool_calls",
        "num_turns",
        "cache_read_tokens",
    ):
        assert getattr(result, name) is None, name


def test_multi_turn_config_timeout_bounds_whole_task(tmp_path, monkeypatch):
    elapsed = 0.0
    monkeypatch.setattr("skilldiff.runner.time.monotonic", lambda: elapsed)

    class TimedExecution(AgentRunner):
        # Replace the external paid process boundary with deterministic clock progression.
        def _exec(self, cmd, cwd, env, timeout, **kwargs):
            nonlocal elapsed
            timed_out = timeout < 0.6
            duration = min(timeout, 0.6)
            elapsed += duration
            return ExecResult(
                '{"status":"SUCCESS","response":"ok"}',
                "",
                -1 if timed_out else 0,
                duration,
                timed_out,
            )

    config = ExperimentConfig(
        name="t",
        skill=tmp_path,
        models=["m"],
        tasks_patterns=[],
        harness="antigravity",
        timeout_seconds=1,
    )
    result = TimedExecution().run(["one", "two", "three"], tmp_path, "m", config)
    assert result.status == "timeout"
    assert result.duration == 1
    assert "TURN 1" in result.transcript and "TURN 2" in result.transcript
    assert "TURN 3" not in result.transcript


@pytest.mark.parametrize("forbidden", [False, True])
def test_baseline_has_same_artifacts_and_integrity_rules(tmp_path, monkeypatch, forbidden):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setenv("SKILLDIFF_MOCK_RESPONSE", "Real candidate response")
    monkeypatch.setenv("SKILLDIFF_MOCK_SCRIPT", "echo actual-edit > protected.txt")
    monkeypatch.setattr("skilldiff.experiment._cli_version", lambda _: "test")
    monkeypatch.setattr("skilldiff.grader.random.shuffle", lambda values: values.reverse())
    skill = tmp_path / "test-special-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("skill")
    source = tmp_path / "task.yaml"
    source.write_text("id: t")
    code = (
        "import os;print(int('Real candidate response' in os.environ['SKILLDIFF_RESPONSE'] "
        "and 'actual-edit' in os.environ['SKILLDIFF_DIFF']))"
    )
    task = TaskConfig(
        id="t",
        prompt="Task prompt",
        source_path=source,
        grader=GraderConfig(command=f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"),
        forbidden_paths=["protected.txt"] if forbidden else [],
    )
    config = ExperimentConfig(
        name="t",
        skill=None,
        skill_a=skill,
        skill_b=skill,
        include_baseline=True,
        models=["fake-model-xyz"],
        tasks_patterns=[],
        runs=1,
    )
    results = ExperimentRunner(config, [task], output_dir=tmp_path / "runs").run()
    for arm in ("control", "treatment", "baseline"):
        record = results["runs"][arm][0]
        assert record["grade_status"] == ("error" if forbidden else "graded")
        assert record["score"] == (0 if forbidden else 1)
    assert results["runs"]["baseline"][0]["blind_label"] == "candidate-A"
    assert {results["runs"][arm][0]["blind_label"] for arm in
            ("control", "treatment", "baseline")} == {"candidate-A", "candidate-B", "candidate-C"}


def test_report_footprint_uses_frozen_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setattr("skilldiff.experiment._cli_version", lambda _: "test")
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text("original")
    source = tmp_path / "task.yaml"
    source.write_text("id: t")

    class EditingAgent(AgentRunner):
        def run(self, *args, **kwargs):
            (skill / "SKILL.md").write_text("changed much larger contents")
            return super().run(*args, **kwargs)

    config = ExperimentConfig(name="t", skill=skill, models=["m"], tasks_patterns=[], runs=1)
    results = ExperimentRunner(
        config,
        [TaskConfig(id="t", prompt="x", source_path=source)],
        output_dir=tmp_path / "runs",
        agent_runner=EditingAgent(),
    ).run()
    assert results["context_tax"]["total_bytes"] == 8

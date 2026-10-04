"""Container boundary checks, plus an opt-in real runtime smoke fixture."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from skilldiff.config import ExperimentConfig, GraderConfig
from skilldiff.experiment import ExperimentRunner, collect_provenance
from skilldiff.grader import Grader
from skilldiff.runner import AgentRunner


def test_unknown_isolation_never_falls_back_to_host(tmp_path):
    with pytest.raises((ValueError, RuntimeError), match="isolation"):
        AgentRunner()._exec(["touch", "escaped"], tmp_path, {}, 1, isolation="dockre")
    assert not (tmp_path / "escaped").exists()


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_container_refuses_host_login_auth_before_starting(tmp_path, harness):
    config = ExperimentConfig(
        name="t",
        skill=tmp_path,
        models=["m"],
        tasks_patterns=[],
        harness=harness,
        isolation="docker",
    )
    with pytest.raises(ValueError, match="api_key"):
        ExperimentRunner(config, [], output_dir=tmp_path / "runs").run()
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize(
    "field,value", [("isolation", "docker"), ("container_image", "changed:tag")]
)
def test_resume_rejects_changed_container_settings(tmp_path, field, value):
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("skill")
    config = ExperimentConfig(name="t", skill=skill, models=["m"], tasks_patterns=[])
    runner = ExperimentRunner(config, [])
    metadata = runner._metadata("test")
    setattr(runner.config, field, value)
    assert any(field in reason for reason in runner._resume_mismatches(metadata))


def test_resume_rejects_changed_immutable_image_identity(tmp_path):
    config = ExperimentConfig(name="t", skill=None, models=["m"], tasks_patterns=[])
    runner = ExperimentRunner(config, [])
    metadata = runner._metadata("test")
    metadata["container_image_id"] = "sha256:old-image"
    runner._container_image_id = "sha256:new-image"
    assert any("image" in reason for reason in runner._resume_mismatches(metadata))


def test_runtime_receives_translated_paths_selected_env_and_identity(tmp_path, monkeypatch):
    runtime = tmp_path / "docker"
    runtime.write_text(
        "#!/usr/bin/env python3\nimport json,sys,pathlib\na=sys.argv[1:];pathlib.Path("
        + repr(str(tmp_path / "runtime-calls.jsonl"))
        + ").open('a').write(json.dumps(a)+'\\n')\n"
        "if a[:2] == ['image','inspect']:print('sha256:fixture-image')\n"
        "elif a[0] == 'run':print(json.dumps(a))\n"
    )
    runtime.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    result = AgentRunner()._exec(
        [str(tmp_path / "agent"), str(tmp_path / "file")],
        tmp_path,
        dict(os.environ, OPENAI_API_KEY="fixture", HOST_SECRET="hidden"),
        2,
        isolation="docker",
        container_image="fixture:tag",
    )
    command = json.loads(result.stdout)
    assert command[-3:] == ["sha256:fixture-image", "/workspace/agent", "/workspace/file"]
    assert "OPENAI_API_KEY" in command
    assert "HOST_SECRET" not in command
    assert "--name" in command
    calls = [
        json.loads(line) for line in (tmp_path / "runtime-calls.jsonl").read_text().splitlines()
    ]
    name = command[command.index("--name") + 1]
    assert ["rm", "-f", name] in calls


def test_container_provenance_uses_image_cli_version(tmp_path, monkeypatch):
    runtime = tmp_path / "docker"
    runtime.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        "if sys.argv[1:3] == ['image','inspect']:print('sha256:fixture-image')\n"
        "elif sys.argv[1] == 'run':\n"
        " assert sys.argv[-2:] == ['/image/bin/agent','--version']\n"
        " print('image-agent 1.0')\n"
    )
    runtime.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    config = ExperimentConfig(
        name="t",
        skill=None,
        models=["m"],
        tasks_patterns=[],
        isolation="docker",
        container_image="fixture:tag",
    )
    config.claude.bin_path = "/image/bin/agent"
    provenance = collect_provenance(config, [])
    assert provenance["agent_cli"] == {"claude": "image-agent 1.0"}


def test_container_cleanup_failure_is_reported(tmp_path, monkeypatch):
    runtime = tmp_path / "docker"
    runtime.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        "if sys.argv[1:3] == ['image','inspect']:print('sha256:fixture-image')\n"
        "elif sys.argv[1] == 'run':print('finished')\n"
        "else:print('daemon unavailable',file=sys.stderr);sys.exit(2)\n"
    )
    runtime.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    result = AgentRunner()._exec(["echo", "x"], tmp_path, dict(os.environ), 2,
                                 isolation="docker", container_image="fixture:tag")
    assert result.exit_code != 0
    assert "cleanup" in result.stderr.lower()


def test_grader_cleanup_failure_cannot_become_a_valid_grade(tmp_path, monkeypatch):
    runtime = tmp_path / "docker"
    runtime.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        "if sys.argv[1:3] == ['image','inspect']:print('sha256:fixture-image')\n"
        "elif sys.argv[1] == 'run':print('{\"score\":1,\"success\":true}')\n"
        "else:print('daemon unavailable',file=sys.stderr);sys.exit(2)\n"
    )
    runtime.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    result = Grader(
        GraderConfig(command="echo grade"), "s", "m",
        isolation="docker", container_image="fixture:tag",
    ).grade_workspace(tmp_path)
    assert result.grade_status == "error"
    assert result.score is None
    assert "cleanup" in result.feedback.lower()


@pytest.fixture
def runtime():
    name = os.environ.get("SKILLDIFF_CONTAINER_RUNTIME")
    if not name:
        pytest.skip("set SKILLDIFF_CONTAINER_RUNTIME for the real container smoke")
    subprocess.run([name, "info"], check=True, capture_output=True, timeout=20)
    return name, os.environ.get("SKILLDIFF_CONTAINER_IMAGE", "python:3.11-slim")


def test_real_container_paths_env_grader_and_cleanup(tmp_path, monkeypatch, runtime):
    runtime_name, image = runtime
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = workspace / "fake-agent"
    agent.write_text(
        "#!/usr/bin/env python3\nimport json,os,pathlib,sys\n"
        "pathlib.Path('evidence.json').write_text(json.dumps({'cwd':os.getcwd(),"
        "'path':sys.argv[1],'auth':os.getenv('OPENAI_API_KEY'),"
        "'secret':os.getenv('UNRELATED_HOST_SECRET'),'host_home':os.getenv('HOME')}))\n"
    )
    agent.chmod(0o755)
    monkeypatch.setenv("UNRELATED_HOST_SECRET", "must-not-cross-boundary")
    env = dict(os.environ, OPENAI_API_KEY="fixture-api-key")
    runner = AgentRunner()
    result = runner._exec(
        [str(agent), str(workspace / "evidence.json")],
        workspace,
        env,
        20,
        isolation=runtime_name,
        container_image=image,
    )
    assert result.exit_code == 0, result.stderr
    evidence = json.loads((workspace / "evidence.json").read_text())
    assert evidence["cwd"] == "/workspace"
    assert evidence["path"] == "/workspace/evidence.json"
    assert evidence["auth"] == "fixture-api-key"
    assert evidence["secret"] is None
    assert evidence["host_home"] != str(Path.home())

    task_dir = tmp_path / "tasks" / "dev"
    task_dir.mkdir(parents=True)
    graders = tmp_path / "graders"
    graders.mkdir()
    grade_script = graders / "grade.py"
    grade_script.write_text(
        "import os,pathlib,json\n"
        "assert pathlib.Path('/.dockerenv').exists() or "
        "pathlib.Path('/run/.containerenv').exists()\n"
        "assert os.getcwd() == '/workspace'\n"
        "assert pathlib.Path(os.environ['SKILLDIFF_RESPONSE_FILE']).read_text() "
        "== 'actual response'\n"
        "try:\n pathlib.Path(__file__).write_text('mutated')\n"
        "except OSError:\n print(json.dumps({'score':1,'success':True}))\n"
        "else:\n raise AssertionError('grader inputs writable')\n"
    )
    grader = Grader(
        GraderConfig(command='python3 "$SKILLDIFF_TASK_DIR/../../graders/grade.py"'),
        "test-skill",
        "fake-model",
        task_dir=task_dir,
        isolation=runtime_name,
        container_image=image,
        inputs_root=tmp_path,
    )
    grade = grader.grade_candidate(workspace, "actual response", "actual diff", "transcript", [])
    assert grade.grade_status == "graded", grade.feedback
    assert grade.score == 1
    assert grade_script.read_text().startswith("import os")

    delayed = (
        "import subprocess,time;subprocess.Popen(['sh','-c',"
        "'sleep 1; echo alive > /workspace/late.txt']);time.sleep(10)"
    )
    result = runner._exec(
        ["python3", "-c", delayed],
        workspace,
        {},
        0.3,
        isolation=runtime_name,
        container_image=image,
    )
    assert result.timed_out
    time.sleep(1.2)
    assert not (workspace / "late.txt").exists()
    names = subprocess.check_output([runtime_name, "ps", "-a", "--format", "{{.Names}}"], text=True)
    assert not any(name.startswith("skilldiff-") for name in names.splitlines())

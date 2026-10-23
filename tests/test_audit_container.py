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
from skilldiff.runner import AgentRunner, build_derived_container_image


def test_unknown_isolation_never_falls_back_to_host(tmp_path):
    with pytest.raises((ValueError, RuntimeError), match="isolation"):
        AgentRunner()._exec(["touch", "escaped"], tmp_path, {}, 1, isolation="dockre")
    assert not (tmp_path / "escaped").exists()


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode", "antigravity"])
def test_container_refuses_missing_auth_before_starting(tmp_path, monkeypatch, harness):
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "OPENAI_API_KEY",
        "CODEX_HOME",
        "OPENCODE_API_KEY",
        "OPENROUTER_API_KEY",
        "XDG_DATA_HOME",
        "OPENCODE_CONFIG_DIR",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    config = ExperimentConfig(
        name="t",
        skill=tmp_path,
        models=["m"],
        tasks_patterns=[],
        harness=harness,
        isolation="docker",
    )
    with pytest.raises(ValueError, match="api_key|GEMINI_API_KEY"):
        ExperimentRunner(config, [], output_dir=tmp_path / "runs").run()
    assert not (tmp_path / "runs").exists()


def test_container_stages_subscription_credentials_for_all_harnesses(tmp_path, monkeypatch):
    import sqlite3

    from skilldiff.runner import stage_container_auth, validate_container_auth

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "OPENCODE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "XDG_DATA_HOME",
        "OPENCODE_CONFIG_DIR",
    ):
        monkeypatch.delenv(key, raising=False)

    (home / ".claude" / "skills" / "secret-skill").mkdir(parents=True)
    (home / ".claude" / "skills" / "secret-skill" / "SKILL.md").write_text("leaked")
    (home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth":{"accessToken":"tok"}}')
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {"emailAddress": "user@example.com"},
                "userID": "u1",
                "projects": {"/secret/project": {}},
            }
        )
    )

    (home / ".codex" / "skills" / "secret-skill").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"tokens":{"access_token":"codex-tok"}}')

    opencode_share = home / ".local" / "share" / "opencode"
    opencode_share.mkdir(parents=True)
    (opencode_share / "auth.json").write_text('{"anthropic":{"type":"api","key":"sk-ant"}}')
    db_path = opencode_share / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE credential ("
        "id text PRIMARY KEY, integration_id text, label text NOT NULL, value text NOT NULL, "
        "connector_id text, method_id text, active integer, "
        "time_created integer NOT NULL, time_updated integer NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE session_v2 (id text PRIMARY KEY, title text)"
    )
    conn.execute(
        "INSERT INTO credential VALUES "
        "('c1','opencode-go','API key','{\"type\":\"key\",\"key\":\"oc_sk_1\"}',NULL,NULL,1,1,1)"
    )
    conn.execute("INSERT INTO session_v2 VALUES ('s1', 'secret history')")
    conn.commit()
    conn.close()

    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "service.json").write_text('{"service":"go"}')

    (home / ".gemini" / "antigravity-cli" / "skills" / "secret").mkdir(parents=True)
    (home / ".gemini" / "GEMINI.md").write_text("secret instructions")
    (home / ".gemini" / "jetski-standalone-oauth-token").write_text("agy-oauth-token")
    (home / ".gemini" / "antigravity-cli" / "jetski_state.pbtxt").write_text("oauth_token: {}")

    for harness in ("claude", "codex", "opencode", "antigravity"):
        cfg = ExperimentConfig(
            name="t",
            skill=tmp_path,
            models=["m"],
            tasks_patterns=[],
            harness=harness,
            isolation="docker",
        )
        validate_container_auth(cfg)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env: dict[str, str] = {}
    gemini_home = stage_container_auth(scratch, env)

    assert env["IS_SANDBOX"] == "1"
    assert (scratch / "claude-home" / ".credentials.json").read_text() == (
        '{"claudeAiOauth":{"accessToken":"tok"}}'
    )
    claude_state = json.loads((scratch / "claude-home" / ".claude.json").read_text())
    assert claude_state["oauthAccount"] == {"emailAddress": "user@example.com"}
    assert claude_state["hasCompletedOnboarding"] is True
    assert claude_state["bypassPermissionsModeAccepted"] is True
    assert "projects" not in claude_state
    assert not (scratch / "claude-home" / "skills").exists()

    assert env["CODEX_HOME"] == "/root/.codex"
    assert (scratch / "codex-home" / "auth.json").is_file()
    assert not (scratch / "codex-home" / "skills").exists()

    staged_oc_auth = json.loads(
        (scratch / "opencode-data" / "opencode" / "auth.json").read_text()
    )
    assert staged_oc_auth["opencode-go"] == {"type": "api", "key": "oc_sk_1"}
    assert staged_oc_auth["anthropic"] == {"type": "api", "key": "sk-ant"}
    staged_db = sqlite3.connect(scratch / "opencode-data" / "opencode" / "opencode.db")
    creds = dict(
        staged_db.execute("SELECT integration_id, value FROM credential").fetchall()
    )
    sessions = staged_db.execute("SELECT count(*) FROM session_v2").fetchone()[0]
    staged_db.close()
    assert "opencode-go" in creds and "anthropic" in creds
    assert sessions == 0
    assert (scratch / "opencode_config" / "service.json").is_file()

    assert gemini_home is not None
    assert (gemini_home / "jetski-standalone-oauth-token").read_text() == "agy-oauth-token"
    assert (gemini_home / "antigravity-cli" / "jetski_state.pbtxt").is_file()
    assert not (gemini_home / "GEMINI.md").exists()
    assert not (gemini_home / "antigravity-cli" / "skills").exists()


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


@pytest.mark.parametrize("isolation", ["docker", "podman"])
def test_agent_container_mounts_only_its_session_temp(tmp_path, monkeypatch, isolation):
    runtime = tmp_path / isolation
    runtime.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\n"
        "if sys.argv[1]=='run':\n"
        " print(json.dumps({'args':sys.argv[1:],"
        "'temp':[os.getenv(k) for k in ('TMPDIR','TMP','TEMP')]}))\n"
    )
    runtime.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_temp = tmp_path / "private-temp"
    session_temp.mkdir()
    result = AgentRunner()._exec(
        ["agent"], workspace, dict(os.environ, TMPDIR="/host/shared"), 2,
        isolation=isolation, container_image="sha256:fixture-image", temp_dir=session_temp,
    )
    assert result.exit_code == 0
    evidence = json.loads(result.stdout)
    assert evidence["temp"] == [str(session_temp.resolve())] * 3
    assert all(f"{key}=/session-tmp" in evidence["args"] for key in ("TMPDIR", "TMP", "TEMP"))
    assert f"{session_temp.resolve()}:/session-tmp" in evidence["args"]
    assert "/host/shared" not in evidence["args"]


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
    assert result.feedback is not None and "cleanup" in result.feedback.lower()


@pytest.fixture
def runtime():
    name = os.environ.get("SKILLDIFF_CONTAINER_RUNTIME")
    if not name:
        pytest.skip("set SKILLDIFF_CONTAINER_RUNTIME for the real container smoke")
    subprocess.run([name, "info"], check=True, capture_output=True, timeout=20)
    return name, os.environ.get("SKILLDIFF_CONTAINER_IMAGE", "python:3.11-slim")


@pytest.mark.container
@pytest.mark.slow
def test_real_container_paths_env_grader_and_cleanup(tmp_path, monkeypatch, runtime):
    runtime_name, image = runtime
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_temp = tmp_path / "session-temp"
    session_temp.mkdir()
    sibling = tmp_path / "other-session"
    sibling.mkdir()
    (sibling / "skill.txt").write_text("must not be mounted")
    agent = workspace / "fake-agent"
    agent.write_text(
        "#!/usr/bin/env python3\nimport json,os,pathlib,sys,tempfile\n"
        "pathlib.Path(tempfile.gettempdir(),'agent-temp.txt').write_text('private')\n"
        "pathlib.Path('evidence.json').write_text(json.dumps({'cwd':os.getcwd(),"
        "'path':sys.argv[1],'auth':os.getenv('OPENAI_API_KEY'),"
        "'temp':tempfile.gettempdir(),'sibling_visible':pathlib.Path("
        + repr(str(sibling / "skill.txt")) + ").exists(),"
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
        temp_dir=session_temp,
    )
    assert result.exit_code == 0, result.stderr
    evidence = json.loads((workspace / "evidence.json").read_text())
    assert evidence["cwd"] == "/workspace"
    assert evidence["path"] == "/workspace/evidence.json"
    assert evidence["auth"] == "fixture-api-key"
    assert evidence["secret"] is None
    assert evidence["host_home"] != str(Path.home())
    assert evidence["temp"] == "/session-tmp"
    assert evidence["sibling_visible"] is False
    assert (session_temp / "agent-temp.txt").read_text() == "private"

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


def test_build_derived_container_image_noop(tmp_path, monkeypatch):
    runtime = tmp_path / "docker"
    runtime.write_text("#!/bin/sh\nexit 1\n")
    runtime.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    img = build_derived_container_image("docker", "base:latest")
    assert img == "base:latest"


def test_build_derived_container_image_with_targets(tmp_path, monkeypatch):
    runtime = tmp_path / "docker"
    captured_dockerfile = tmp_path / "dockerfile.txt"
    runtime.write_text(
        "#!/usr/bin/env python3\nimport sys, pathlib\n"
        "args = sys.argv[1:]\n"
        "if args[0] == 'build':\n"
        f"    pathlib.Path({repr(str(captured_dockerfile))}).write_text("
        "pathlib.Path('Dockerfile').read_text())\n"
        "    print('sha256:derived-image-123')\n"
        "elif args[:2] == ['image', 'inspect']:\n"
        "    print('sha256:derived-image-123')\n"
    )
    runtime.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    res = build_derived_container_image(
        "docker", "base:latest", install_targets=["pytest>=8.0", "shiny[all]"], tag="test:tag"
    )
    assert res == "sha256:derived-image-123"
    content = captured_dockerfile.read_text()
    assert "FROM base:latest" in content
    assert "pytest>=8.0" in content
    assert "shiny[all]" in content


def test_build_derived_container_image_with_repo_src(tmp_path, monkeypatch):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'pkg'\n")
    runtime = tmp_path / "docker"
    captured_dockerfile = tmp_path / "dockerfile_repo.txt"
    runtime.write_text(
        "#!/usr/bin/env python3\nimport sys, pathlib\n"
        "args = sys.argv[1:]\n"
        "if args[0] == 'build':\n"
        f"    pathlib.Path({repr(str(captured_dockerfile))}).write_text("
        "pathlib.Path('Dockerfile').read_text())\n"
        "    print('sha256:repo-image-456')\n"
        "elif args[:2] == ['image', 'inspect']:\n"
        "    print('sha256:repo-image-456')\n"
    )
    runtime.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    res = build_derived_container_image(
        "docker", "base:latest", repo_src=repo, install_spec=".[dev]"
    )
    assert res == "sha256:repo-image-456"
    content = captured_dockerfile.read_text()
    assert "COPY repo_src /tmp/skilldiff-repo" in content
    assert "/tmp/skilldiff-repo[dev]" in content


def test_resume_rejects_changed_container_install(tmp_path):
    config = ExperimentConfig(
        name="t", skill=None, models=["m"], tasks_patterns=[], container_install="pkg-a"
    )
    runner = ExperimentRunner(config, [])
    metadata = runner._metadata("test")
    runner.config.container_install = "pkg-b"
    assert any("container_install" in reason for reason in runner._resume_mismatches(metadata))


def test_resume_checks_derived_container_image_presence(tmp_path, monkeypatch):
    runtime = tmp_path / "docker"
    runtime.write_text("#!/bin/sh\nexit 1\n")
    runtime.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    config = ExperimentConfig(
        name="t", skill=None, models=["m"], tasks_patterns=[], isolation="docker"
    )
    runner = ExperimentRunner(config, [])
    runner._provenance_snapshot = {"agent_cli": "fake"}
    runner._container_image_id = "sha256:base"
    metadata = runner._metadata("test")
    metadata["container_image_id"] = "sha256:base"
    metadata["container_image_ids"] = {"control": "sha256:missing_ctrl"}
    reasons = runner._resume_mismatches(metadata)
    assert any("missing_ctrl" in reason for reason in reasons)


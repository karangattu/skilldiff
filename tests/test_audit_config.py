import json
import os
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from skilldiff.cli import cmd_check, cmd_init, cmd_run
from skilldiff.config import load_experiment, load_task
from skilldiff.grader import Grader
from skilldiff.linter import lint_skill


def experiment_file(tmp_path: Path, changes: dict | None = None) -> Path:
    skill = tmp_path / "skill"
    skill.mkdir(exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: sample\ndescription: Sample skill.\n---\n")
    tasks = tmp_path / "tasks"
    tasks.mkdir(exist_ok=True)
    (tasks / "one.yaml").write_text("id: one\nprompt: Do it\n")
    data = {
        "name": "strict-inputs", "skill": "./skill", "models": ["m"],
        "tasks": ["./tasks/*.yaml"],
    }
    data.update(changes or {})
    config = tmp_path / "skilldiff.yaml"
    config.write_text(yaml.safe_dump(data))
    return config


@pytest.mark.parametrize(
    "changes,key",
    [
        ({"include_baseline": "false"}, "include_baseline"),
        ({"claude": {"isolate": "false"}}, "claude.isolate"),
        ({"codex": {"dangerously_bypass_approvals_and_sandbox": "false"}},
         "codex.dangerously_bypass_approvals_and_sandbox"),
        ({"opencode": {"dangerously_skip_permissions": 1}},
         "opencode.dangerously_skip_permissions"),
        ({"antigravity": {"dangerously_skip_permissions": "false"}},
         "antigravity.dangerously_skip_permissions"),
        ({"agy": {"dangerously_skip_permissions": "false"}},
         "agy.dangerously_skip_permissions"),
        ({"runs": 1.5}, "runs"),
        ({"runs": True}, "runs"),
        ({"parallel": 1.5}, "parallel"),
        ({"parallel": -1}, "parallel"),
        ({"claude": {"max_turns": 1.5}}, "claude.max_turns"),
        ({"claude": {"max_turns": 0}}, "claude.max_turns"),
        ({"seed": 0.5}, "seed"),
        ({"timeout_seconds": -1}, "timeout_seconds"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": float("nan")}, "timeout_seconds"),
        ({"timeout_seconds": 10 ** 1000}, "timeout_seconds"),
        ({"claude": {"max_budget_usd": float("inf")}}, "claude.max_budget_usd"),
        ({"claude": {"max_budget_usd": -1}}, "claude.max_budget_usd"),
        ({"thresholds": {"meaningful_score_gain_pp": float("nan")}},
         "thresholds.meaningful_score_gain_pp"),
        ({"thresholds": {"required_cost_reduction_pct": -1}},
         "thresholds.required_cost_reduction_pct"),
        ({"isolation": "dockre"}, "isolation"),
        ({"container_image": 123}, "container_image"),
        ({"models": [123]}, "models"),
        ({"tasks": [123]}, "tasks"),
        ({"claude": {"allowed_tools": "Bash(*)"}}, "claude.allowed_tools"),
        ({"claude": {"extra_args": [False]}}, "claude.extra_args"),
        ({"codex": {"extra_args": "--unsafe"}}, "codex.extra_args"),
        ({"opencode": {"extra_args": [1]}}, "opencode.extra_args"),
        ({"antigravity": {"extra_args": "--unsafe"}}, "antigravity.extra_args"),
        ({"claude": {"effort": "hihg"}}, "claude.effort"),
        ({"claude": {"permission_mode": "acceptEdit"}}, "claude.permission_mode"),
        ({"codex": {"sandbox": "workspace-writ"}}, "codex.sandbox"),
        ({"skill": None, "pr": {"repo": "./skill", "base": "main", "head": "feature",
                                  "mode": ""}},
         "pr.mode"),
        ({"skill": None, "pr": {"repo": "./skill", "base": "main", "head": "feature",
                                  "pair": None}},
         "pr.pair"),
        ({"opencode": {"service": "go", "subscription": "g0"}}, "opencode.subscription"),
        ({"antigravity": {}, "agy": {"extra_args": "--skip"}}, "agy.extra_args"),
        ({"failure_policy": {"agent_failure": "zero"},
          "on_failure": {"agent_failure": "retry"}}, "on_failure.agent_failure"),
    ],
)
def test_config_rejects_invalid_values_before_execution(tmp_path, changes, key):
    with pytest.raises(ValueError, match=key):
        load_experiment(experiment_file(tmp_path, changes))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True])
def test_config_rejects_nonfinite_or_boolean_pricing(tmp_path, value):
    pricing = {
        "source": "checked provider rate", "date": "2026-10-04",
        "rates": {"m": {"input": value, "output": 2, "cache_read": 0, "cache_write": 1}},
    }
    with pytest.raises(ValueError, match="pricing.rates.m.input"):
        load_experiment(experiment_file(tmp_path, {"pricing": pricing}))


@pytest.mark.parametrize(
    "key,value",
    [
        ("allowed_paths", "src/**"), ("forbidden_paths", "secrets.txt"),
        ("allowed_paths", [False]), ("forbidden_paths", [123]),
        ("prompts", "Do it"), ("prompts", [False]),
        ("category", "intentded"), ("repo", 123),
        ("validation", {"broken": [1]}), ("validation", {"good": False}),
        ("split", ""),
    ],
)
def test_task_rejects_scalar_or_malformed_values(tmp_path, key, value):
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump({"id": "t", "prompt": "Do it", key: value}))
    with pytest.raises(ValueError, match=key):
        load_task(path)


@pytest.mark.parametrize("grader_type", ["llm", "rubric"])
def test_judge_grader_requires_a_command_at_load_time(tmp_path, grader_type):
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump({
        "id": "t", "prompt": "Do it", "grader": {"type": grader_type, "rubric": "Check it"},
    }))
    with pytest.raises(ValueError, match="grader.command"):
        load_task(path)


@pytest.mark.parametrize("option", ["runs", "parallel"])
@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_cli_rejects_invalid_overrides_before_runner(tmp_path, monkeypatch, capsys, option, value):
    config = experiment_file(tmp_path)

    def forbid_runner(*args, **kwargs):
        pytest.fail("Runner constructed for an invalid CLI override")

    monkeypatch.setattr("skilldiff.cli.ExperimentRunner", forbid_runner)
    assert cmd_run(Namespace(config=str(config), **{option: value})) == 1
    assert option in capsys.readouterr().err


def test_duplicate_ids_in_distinct_files_are_rejected(tmp_path):
    config = experiment_file(tmp_path)
    other = tmp_path / "tasks" / "other.yaml"
    other.write_text("id: one\nprompt: Different task\n")
    with pytest.raises(ValueError, match="Duplicate task id.*one") as error:
        load_experiment(config)
    assert "one.yaml" in str(error.value)
    assert "other.yaml" in str(error.value)


def test_overlapping_globs_load_each_resolved_file_once(tmp_path):
    config = experiment_file(tmp_path, {"tasks": ["./tasks/*.yaml", "./tasks/one.yaml"]})
    _, tasks = load_experiment(config)
    assert [task.id for task in tasks] == ["one"]


def test_force_reinitializing_legacy_ab_scaffold_does_not_leave_duplicate_tasks(tmp_path):
    for name in ("a", "b"):
        skill = tmp_path / name
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: shared\ndescription: Shared trigger.\n---\n")
    root = tmp_path / "evaluation"
    (root / "tasks" / "heldout").mkdir(parents=True)
    (root / "tasks" / "my-first-task.yaml").write_text("id: my-first-task\nprompt: Do it\n")
    (root / "tasks" / "heldout" / "my-first-task.yaml").write_text(
        "id: my-first-task\nprompt: Do it\n"
    )
    (root / "skilldiff.yaml").write_text("name: legacy\n")
    args = Namespace(dir=str(root), force=True, skill_a=str(tmp_path / "a"),
                     skill_b=str(tmp_path / "b"))
    assert cmd_init(args) == 0
    _, tasks = load_experiment(root / "skilldiff.yaml")
    assert len(tasks) == 2
    assert {task.split for task in tasks} == {"dev", "held-out"}


@pytest.mark.parametrize("preset", ["demo", "skill", "revision", "compression", "pr"])
def test_initializer_tasks_round_trip_with_both_splits_and_reachable_graders(tmp_path, preset):
    root = tmp_path / "evaluation"
    options = {"dir": str(root), "force": False}
    for name in ("original", "revised"):
        skill = tmp_path / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: same-trigger\ndescription: A shared trigger for the test.\n---\n" + name
        )
    if preset == "skill":
        options["skill"] = str(tmp_path / "original")
    elif preset in {"revision", "compression"}:
        options.update(skill_a=str(tmp_path / "original"), skill_b=str(tmp_path / "revised"),
                       preset=preset)
    elif preset == "pr":
        options.update(pr=42, repo=str(tmp_path / "original"), base="main")
    assert cmd_init(Namespace(**options)) == 0
    config, tasks = load_experiment(root / "skilldiff.yaml")
    expected_splits = {"dev"} if preset == "demo" else {"dev", "held-out"}
    assert {task.split for task in tasks} == expected_splits
    assert len({task.id for task in tasks}) == len(tasks)
    for task in tasks:
        task_dir = task.source_path.parent
        fixture = config.pr.repo if config.pr else task_dir / task.repo
        assert fixture.is_dir()
        grade = Grader(task.grader, [], "m", task_dir=task_dir).grade_workspace(fixture)
        assert grade.grade_status == "graded", grade.feedback
    # Every preset discovers the documented development directory too.
    dev_task = root / "tasks" / "dev" / "additional.yaml"
    dev_task.parent.mkdir(exist_ok=True)
    dev_task.write_text("id: additional\nprompt: Separate development task\n")
    _, discovered = load_experiment(root / "skilldiff.yaml")
    assert "additional" in {task.id for task in discovered}


def test_lint_warns_when_frontmatter_name_differs_from_directory(tmp_path):
    skill = tmp_path / "folder-name"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: skill-name\ndescription: Use for testing a useful trigger.\n"
        "---\nInstructions.\n"
    )
    result = lint_skill(skill)
    assert result.valid
    assert any("differs from directory name" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    "behavior,expected_exit",
    [("crash", 1), ("timeout", 1), ("invalid", 1), ("partial", 0), ("failure", 0)],
)
def test_check_requires_a_usable_grade_for_each_broken_fixture(
    tmp_path, monkeypatch, capsys, behavior, expected_exit
):
    config = experiment_file(tmp_path)
    for fixture, state in (("untouched", "untouched"), ("good", "good"), ("broken", "broken")):
        directory = tmp_path / fixture
        directory.mkdir()
        (directory / "state.txt").write_text(state)
    judge = tmp_path / "judge.py"
    judge.write_text(
        "import json, time\nfrom pathlib import Path\n"
        "state = Path('state.txt').read_text()\n"
        f"behavior = {behavior!r}\n"
        "if state == 'broken':\n"
        "    if behavior == 'crash':\n"
        "        raise RuntimeError('broken grader')\n"
        "    if behavior == 'timeout':\n"
        "        time.sleep(2)\n"
        "    score = None if behavior == 'invalid' else (0.5 if behavior == 'partial' else 0)\n"
        "else:\n"
        "    score = 1 if state == 'good' else 0\n"
        "print(json.dumps({'score': score, 'success': score == 1}))\n"
    )
    task = tmp_path / "tasks" / "one.yaml"
    task.write_text(yaml.safe_dump({
        "id": "one", "prompt": "Do it", "repo": "../untouched",
        "grader": {"type": "command",
                   "command": 'python3 "$SKILLDIFF_TASK_DIR/../judge.py"'},
        "validation": {"good": "../good", "broken": ["../broken"]},
    }))
    # A short real subprocess timeout makes the CLI check cheap and deterministic.
    def bounded_grader(*args, **kwargs):
        return Grader(*args, **kwargs, timeout=1)

    monkeypatch.setattr("skilldiff.cli.Grader", bounded_grader)
    monkeypatch.setenv("CLAUDE_BIN", "/bin/echo")
    monkeypatch.delenv("SKILLDIFF_MOCK_RUNNER", raising=False)
    assert cmd_check(Namespace(config=str(config), no_grade=False)) == expected_exit
    output = capsys.readouterr().out
    if expected_exit:
        assert "grader validation grader-broken" in output
        assert "grader validation ok" not in output
    else:
        assert "grader validation ok" in output


def fake_container_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "docker"
    log = tmp_path / "runtime.jsonl"
    runtime.write_text(
        "#!/usr/bin/env python3\nimport json, pathlib, sys\nargs = sys.argv[1:]\n"
        f"with pathlib.Path({str(log)!r}).open('a') as stream:\n"
        "    stream.write(json.dumps(args) + '\\n')\n"
        "if args[:2] == ['image', 'inspect']:\n"
        "    print('sha256:checked-image')\n"
        "elif args[0] == 'run':\n"
        "    if args[-1] == '--version':\n"
        "        assert args[-2] == '/image/bin/agent'\n"
        "        print('image-agent 9.9.9')\n"
        "    elif 'app-server' in args:\n"
        "        host = next(value.rsplit(':',1)[0] for value in args "
        "if value.endswith(':/workspace'))\n"
        "        skills = []\n"
        "        for path in pathlib.Path(host, '.agents/skills').glob('*/SKILL.md'):\n"
        "            name = next(line.split(':',1)[1].strip() for line in "
        "path.read_text().splitlines() if line.startswith('name:'))\n"
        "            skills.append({'name':name, 'enabled':True, "
        "'path':'/workspace/'+str(path.relative_to(host))})\n"
        "        for line in sys.stdin:\n"
        "            request=json.loads(line)\n"
        "            if 'id' not in request: continue\n"
        "            result={'data':[{'skills':skills,'errors':[]}]} "
        "if request['method']=='skills/list' else {}\n"
        "            print(json.dumps({'id':request['id'],'result':result}),flush=True)\n"
        "    else:\n"
        "        print(json.dumps({'score': 0, 'success': False}))\n"
    )
    runtime.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    return log


@pytest.mark.parametrize("harness,credential", [
    ("claude", "ANTHROPIC_API_KEY"), ("claude", "ANTHROPIC_AUTH_TOKEN"),
    ("codex", "OPENAI_API_KEY"),
])
def test_check_versions_image_binary_and_grades_inside_container(
    tmp_path, monkeypatch, capsys, harness, credential
):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(credential, "fixture-auth")
    monkeypatch.setenv("UNRELATED_HOST_SECRET", "should-stay-on-host")
    log = fake_container_runtime(tmp_path, monkeypatch)
    config = experiment_file(tmp_path, {
        "harness": harness, "isolation": "docker", "container_image": "fixture:tag",
        "tasks": ["./tasks/dev/*.yaml"],
        harness: {"auth": "api_key", "bin_path": "/image/bin/agent"},
    })
    (tmp_path / "tasks" / "dev").mkdir()
    (tmp_path / "tasks" / "dev" / "one.yaml").write_text(
        "id: one\nprompt: Do it\ngrader:\n  command: python3 judge.py\n"
    )
    monkeypatch.setattr("skilldiff.cli.find_user_level_installs",
                        lambda *args: ["skill installed at user level"])
    monkeypatch.setattr("skilldiff.cli.find_harness_inheritance",
                        lambda *args: ["host instructions that are not mounted"])
    assert cmd_check(Namespace(config=str(config), no_grade=False)) == 0
    output = capsys.readouterr().out
    assert "image-agent 9.9.9" in output
    assert "untouched fixture scores 0%" in output
    assert "skill is installed at user level" not in output
    assert "host instructions that are not mounted" not in output
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    runs = [call for call in calls if call[0] == "run"]
    assert len(runs) == (4 if harness == "codex" else 2)
    assert ["sha256:checked-image", "/image/bin/agent", "--version"] == runs[0][-3:]
    assert any(argument.startswith(f"{tmp_path.resolve()}:") and argument.endswith(":ro")
               for argument in runs[-1])
    assert all("UNRELATED_HOST_SECRET" not in call for call in runs)


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("auth", ["host_login", "missing_key"])
def test_check_rejects_unsupported_container_auth_before_runtime(
    tmp_path, monkeypatch, capsys, harness, auth
):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    log = fake_container_runtime(tmp_path, monkeypatch)
    harness_auth = "api_key" if auth == "missing_key" else (
        "subscription" if harness == "claude" else "stored"
    )
    config = experiment_file(tmp_path, {
        "harness": harness, "isolation": "docker",
        harness: {"auth": harness_auth, "bin_path": "/image/bin/agent"},
    })
    assert cmd_check(Namespace(config=str(config), no_grade=True)) == 1
    assert "api_key" in capsys.readouterr().out
    assert not log.exists()

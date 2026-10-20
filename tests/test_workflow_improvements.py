import subprocess
from pathlib import Path

import pytest

from skilldiff.aggregation import aggregate_results
from skilldiff.cli import build_parser, cmd_check, cmd_diagnose, cmd_results
from skilldiff.config import ExperimentConfig, GraderConfig, TaskConfig, load_experiment, load_task
from skilldiff.diagnose import diagnose_run
from skilldiff.experiment import ExperimentRunner
from skilldiff.grader import Candidate, Grader, check_blast_radius
from skilldiff.persistence import atomic_json
from skilldiff.runner import AgentRunner, RunResult
from skilldiff.scope import normalize_changed_paths
from skilldiff.workspace import Workspace


def test_grader_ignore_preset_string_and_list_expansion(tmp_path: Path) -> None:
    task_str = tmp_path / "task-str.yaml"
    task_str.write_text(
        "id: t1\nprompt: fix it\ngrader_ignore: python-tooling\n",
        encoding="utf-8",
    )
    loaded_str = load_task(task_str)
    assert ".venv/**" in loaded_str.grader_ignore
    assert ".pytest_cache/**" in loaded_str.grader_ignore
    assert "uv.lock" in loaded_str.grader_ignore

    task_list = tmp_path / "task-list.yaml"
    task_list.write_text(
        "id: t2\nprompt: fix it\ngrader_ignore:\n  - python-tooling\n  - outputs/*\n",
        encoding="utf-8",
    )
    loaded_list = load_task(task_list)
    assert ".venv/**" in loaded_list.grader_ignore
    assert "outputs/*" in loaded_list.grader_ignore


def test_infrastructure_noise_not_flagged_as_blast_radius() -> None:
    violation = check_blast_radius(
        changed_files=["app.py", ".venv/lib/site.py", ".pytest_cache/v/cache", "uv.lock"],
        allowed_paths=["app.py"],
        forbidden_paths=[],
        ignore_paths=[],
    )
    assert violation is None


def test_mounted_fixtures_in_pr_mode_and_workspace_overlay(tmp_path: Path) -> None:
    pr_repo = tmp_path / "repo"
    pr_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=pr_repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=pr_repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@local"], cwd=pr_repo, check=True)
    (pr_repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=pr_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=pr_repo, check=True)
    base_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=pr_repo, text=True
    ).strip()

    fixture_dir = tmp_path / "fixtures" / "broken-app"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "app.py").write_text("broken = True\n", encoding="utf-8")

    task_file = tmp_path / "tasks" / "diag.yaml"
    task_file.parent.mkdir(parents=True)
    task_file.write_text(
        "id: diag\nprompt: Diagnose app.py\nfixtures:\n  - ../fixtures/broken-app\n",
        encoding="utf-8",
    )
    exp_file = tmp_path / "skilldiff.yaml"
    exp_file.write_text(
        f"name: pr-eval\npr:\n  repo: ./repo\n  base: {base_sha}\n  head: {base_sha}\n"
        "models:\n  - sonnet\ntasks:\n  - ./tasks/*.yaml\n",
        encoding="utf-8",
    )
    exp_cfg, tasks = load_experiment(exp_file)
    assert len(tasks) == 1
    assert tasks[0].fixtures == ["../fixtures/broken-app"]

    ws_root = tmp_path / "ws"
    ws = Workspace(
        root=ws_root,
        is_treatment=False,
        skill_dir=None,
        fixture_repo=pr_repo,
        source_commit=base_sha,
        mounted_fixtures=[fixture_dir],
    )
    ws.setup()
    assert (ws_root / "README.md").read_text(encoding="utf-8") == "base\n"
    assert (ws_root / "app.py").read_text(encoding="utf-8") == "broken = True\n"
    # Baseline includes mounted fixture, so untouched workspace diff is empty.
    diff_text, changed = ws.get_diff()
    assert diff_text == ""
    assert changed == []


def test_workspace_diff_excludes_generated_dirs_and_normalizer_collapses(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "app.py").write_text("x = 1\n", encoding="utf-8")
    ws = Workspace(root=tmp_path / "ws", is_treatment=False, skill_dir=None, fixture_repo=fixture)
    ws.setup()
    (ws.root / "app.py").write_text("x = 2\n", encoding="utf-8")
    venv_file = ws.root / ".venv" / "lib" / "pkg.py"
    venv_file.parent.mkdir(parents=True)
    venv_file.write_text("installed = True\n", encoding="utf-8")
    _, changed = ws.get_diff()
    assert changed == ["app.py"]

    normalized = normalize_changed_paths(
        ["app.py", ".venv/lib/pkg.py", "node_modules/a/index.js", "outputs/log.txt"],
        grader_ignore=["outputs/*"],
    )
    assert normalized == ["<generated:.venv>", "<generated:node_modules>", "app.py"]


def test_env_and_budget_limits_applied(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\nbody\n", encoding="utf-8")
    exp_file = tmp_path / "skilldiff.yaml"
    exp_file.write_text(
        "name: e\nskill: ./skill\nmodels:\n  - sonnet\ntasks:\n  - ./task.yaml\n"
        "max_turns: 5\nmax_tool_budget: 3\nterminate_on_ceiling: true\n"
        "env:\n  PYTHONPATH: /tmp/custom-shiny\n",
        encoding="utf-8",
    )
    task_file = tmp_path / "task.yaml"
    task_file.write_text(
        "id: t1\nprompt: p\nenv:\n  EXTRA_VAR: hello\n",
        encoding="utf-8",
    )
    exp_cfg, tasks = load_experiment(exp_file)
    assert exp_cfg.max_turns == 5
    assert exp_cfg.claude.max_turns == 5
    assert exp_cfg.max_tool_budget == 3
    assert exp_cfg.terminate_on_ceiling is True
    assert exp_cfg.env == {"PYTHONPATH": "/tmp/custom-shiny"}
    assert tasks[0].env == {"EXTRA_VAR": "hello"}

    res = RunResult(
        prompt="p",
        response="ok",
        transcript="",
        duration=1.0,
        cost=0.01,
        input_tokens=10,
        output_tokens=10,
        tool_calls=6,
        exit_code=0,
        num_turns=2,
    )
    limited = AgentRunner._apply_budget_limits(res, exp_cfg)
    assert limited.status == "error"
    assert "Tool budget exceeded (6 > 3)" in (limited.error or "")

    grader_cmd = (
        'python3 -c \'import os, json; '
        's = 1.0 if os.environ.get("EXTRA_VAR") == "hello" else 0.0; '
        'print(json.dumps({"score": s, "success": True}))\''
    )
    grader = Grader(
        GraderConfig(type="command", command=grader_cmd),
        skill_name="s",
        model_name="sonnet",
        extra_env={"EXTRA_VAR": "hello"},
    )
    grade = grader.grade_candidates(
        [Candidate("A", "control", tmp_path, "ok", "", "", [])]
    )["control"]
    assert grade.score == 1.0


def test_check_fail_on_ceiling_and_terminate_on_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capable_cli, conversation_cli
) -> None:
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setenv("CLAUDE_BIN", conversation_cli)
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n", encoding="utf-8")
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "app.py").write_text("ok = True\n", encoding="utf-8")
    task_file = tmp_path / "task.yaml"
    task_file.write_text(
        "id: easy\nprompt: do nothing\nrepo: ./fixture\n"
        "grader:\n  type: command\n  command: \"python3 -c 'print(1.0)'\"\n",
        encoding="utf-8",
    )
    exp_file = tmp_path / "skilldiff.yaml"
    exp_file.write_text(
        "name: ceil\nskill: ./skill\nmodels:\n  - sonnet\ntasks:\n  - ./task.yaml\n"
        "runs: 3\nterminate_on_ceiling: true\n",
        encoding="utf-8",
    )
    parser = build_parser()
    args_warn = parser.parse_args(["check", "-c", str(exp_file), "--no-probe"])
    assert cmd_check(args_warn) == 0

    args_fail = parser.parse_args(
        ["check", "-c", str(exp_file), "--no-probe", "--fail-on-ceiling"]
    )
    assert cmd_check(args_fail) == 1

    exp_cfg, tasks = load_experiment(exp_file)
    runner = ExperimentRunner(exp_cfg, tasks, output_dir=tmp_path / "runs")
    results = runner.run()
    assert results.get("terminated_on_ceiling") is True
    assert results.get("non_discriminating_tasks") == ["easy"]
    # Stopped after repetition 1 instead of running all 3 repetitions.
    assert len(results["runs"]["control"]) == 1


def test_adoption_failure_in_aggregation_diagnose_and_ci(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n", encoding="utf-8")
    cfg = ExperimentConfig(name="adopt", skill=skill, models=["sonnet"], tasks_patterns=[], runs=3)
    task = TaskConfig(id="t1", prompt="p", category="intended")
    ctrl = [
        {
            "model": "sonnet",
            "task_id": "t1",
            "repetition": i,
            "arm": "control",
            "status": "ok",
            "score": 0.5,
            "grade_status": "graded",
            "skill_invoked": False,
        }
        for i in (1, 2, 3)
    ]
    treat = [
        {
            "model": "sonnet",
            "task_id": "t1",
            "repetition": i,
            "arm": "treatment",
            "status": "ok",
            "score": 0.5,
            "grade_status": "graded",
            "skill_invoked": (i == 1),
        }
        for i in (1, 2, 3)
    ]
    run_dir = tmp_path / "runs" / "2026-10-15T000000Z"
    run_dir.mkdir(parents=True)
    results = aggregate_results(
        cfg, [task], run_dir, "2026-10-15T000000Z", ctrl, treat, [], False
    )
    assert results["adoption_failed"] is True
    atomic_json(run_dir / "results.json", results)

    diag = diagnose_run(run_dir)
    assert diag["adoption_failed"] is True
    assert len(diag["under_triggered"]) == 2

    parser = build_parser()
    res_args = parser.parse_args(["results", str(run_dir), "--json", "--ci"])
    assert cmd_results(res_args) == 4
    diag_args = parser.parse_args(["diagnose", str(run_dir), "--json", "--ci"])
    assert cmd_diagnose(diag_args) == 1


def test_py_shiny_troubleshooting_pack_validates(
    monkeypatch: pytest.MonkeyPatch, capable_cli, conversation_cli
) -> None:
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setenv("CLAUDE_BIN", conversation_cli)
    pack_cfg = (
        Path(__file__).resolve().parents[1]
        / "examples/py-shiny-troubleshooting/skilldiff.yaml"
    )
    exp_cfg, tasks = load_experiment(pack_cfg)
    assert len(tasks) == 6
    parser = build_parser()
    args = parser.parse_args(["check", "-c", str(pack_cfg), "--no-probe", "--fail-on-ceiling"])
    rc = cmd_check(args)
    shutil_runs = pack_cfg.parent / "runs"
    if shutil_runs.exists():
        import shutil

        shutil.rmtree(shutil_runs, ignore_errors=True)
    assert rc == 0

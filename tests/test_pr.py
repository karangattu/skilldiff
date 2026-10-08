import json
import subprocess
from pathlib import Path

import pytest
import yaml

from skilldiff.cli import _format_results, cmd_check
from skilldiff.config import load_experiment
from skilldiff.experiment import ExperimentRunner


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def pr_experiment(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "feature.txt").write_text("old")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "base")
    (repo / "feature.txt").write_text("new")
    git(repo, "commit", "-qam", "feature")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "feature")
    git(repo, "checkout", "-q", "base")
    (repo / "unrelated.txt").write_text("base advanced")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "unrelated")
    (repo / "dirty.txt").write_text("must not leak")
    (tmp_path / "task.yaml").write_text(
        "id: feature\nprompt: Use the feature\ngrader:\n"
        '  command: test "$(cat feature.txt)" = new\n'
    )
    path = tmp_path / "skilldiff.yaml"
    data = dict(
        name="pr-eval",
        pr=dict(repo="./repo", base="base", head="feature"),
        models=["test"],
        tasks=["task.yaml"],
        runs=2,
        parallel=2,
    )
    path.write_text(yaml.safe_dump(data))
    return path, repo, base, head


def test_pr_pipeline_isolates_revisions_and_reports_treatment(pr_experiment, monkeypatch):
    path, repo, base, head = pr_experiment
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setenv(
        "SKILLDIFF_MOCK_SCRIPT",
        "test ! -e dirty.txt && test ! -e unrelated.txt && echo ok > result.txt",
    )
    before = git(repo, "status", "--porcelain")
    cfg, tasks = load_experiment(path)
    results = ExperimentRunner(cfg, tasks).run()
    assert results["comparison"]["control_commit"] == base
    assert results["comparison"]["treatment_commit"] == head
    assert all(r["score"] == 0 for r in results["runs"]["control"])
    assert all(r["score"] == 1 for r in results["runs"]["treatment"])
    assert all(r["skill_invoked"] is None for r in results["runs"]["treatment"])
    assert not results["warnings"]
    root = Path(results["run_dir"])
    for arm, sha in [("control", base), ("treatment", head)]:
        for r in results["runs"][arm]:
            assert r["source_commit"] == sha
            diff = (root / r["artifacts"] / "diff.patch").read_text()
            assert "result.txt" in diff
            assert "feature.txt" not in diff
    assert git(repo, "status", "--porcelain") == before
    assert json.loads((root / "experiment.json").read_text())["comparison"] == results["comparison"]
    for name in ["report.md", "report.html", "report.qmd"]:
        report = (root / name).read_text()
        assert "Treatment" in report and base in report and head in report
        assert "Skill used" not in report and "skill installed" not in report
    assert "Treatment" in _format_results(results)


def test_pr_check_uses_control_revision(pr_experiment, monkeypatch, capsys):
    from argparse import Namespace

    path, _, base, _ = pr_experiment
    monkeypatch.setenv("CLAUDE_BIN", "/bin/echo")
    assert cmd_check(Namespace(config=str(path), no_grade=False)) == 0
    output = capsys.readouterr().out
    assert base in output
    assert "untouched fixture scores 0%" in output


@pytest.mark.parametrize("pair", ["merge-base", "base-merge"])
def test_pr_resume_reuses_original_commits(pr_experiment, monkeypatch, pair):
    path, _, _, _ = pr_experiment
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setattr("skilldiff.experiment._cli_version", lambda _: "test 1")
    cfg, tasks = load_experiment(path)
    cfg.pr.pair = pair
    cfg.pr.mode = "correctness"
    cfg.runs = 1
    first = ExperimentRunner(cfg, tasks).run()
    # A new synthetic merge would have a different commit timestamp and SHA.
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2030-01-01T00:00:00+00:00")
    second = ExperimentRunner(cfg, tasks).run(resume=Path(first["run_dir"]))
    assert second["comparison"] == first["comparison"]
    assert second["runs"] == first["runs"]


@pytest.mark.parametrize(
    "change,match",
    [
        ({"skill": "./repo"}, "exactly one"),
        ({"pr": {"repo": "./repo", "base": "base"}}, "head"),
        ({"pr": {"repo": "./repo", "base": "base", "head": "missing"}}, "revision"),
    ],
)
def test_pr_rejects_invalid_configuration(pr_experiment, change, match):
    path, _, _, _ = pr_experiment
    data = yaml.safe_load(path.read_text())
    data.update(change)
    path.write_text(yaml.safe_dump(data))
    with pytest.raises((ValueError, FileNotFoundError), match=match):
        cfg, tasks = load_experiment(path)
        ExperimentRunner(cfg, tasks).run()


def test_pr_rejects_task_fixture_override(pr_experiment):
    path, _, _, _ = pr_experiment
    task = path.parent / "task.yaml"
    task.write_text(task.read_text() + "repo: ./repo\n")
    with pytest.raises(ValueError, match="repo"):
        load_experiment(path)


def test_pr_init_scaffolds_loadable_experiment(tmp_path):
    from skilldiff.cli import build_parser, cmd_init

    repo = tmp_path / "repo with spaces"
    repo.mkdir()
    root = tmp_path / "eval"
    args = build_parser().parse_args(
        [
            "init",
            "--pr",
            "42",
            "--repo",
            str(repo),
            "--base",
            "origin/main",
            "--dir",
            str(root),
            "--harness",
            "codex",
        ]
    )
    assert cmd_init(args) == 0
    cfg, tasks = load_experiment(root / "skilldiff.yaml")
    assert cfg.pr.repo == repo.resolve()
    assert cfg.pr.head == "refs/pull/42/head"
    assert cfg.pr.base == "origin/main"
    assert cfg.skill is None and tasks[0].repo is None
    assert not (root / "skills").exists()


def test_merged_pr_requires_pre_merge_base(pr_experiment):
    path, repo, _, head = pr_experiment
    git(repo, "merge", "--no-edit", "feature")
    cfg, tasks = load_experiment(path)
    with pytest.raises(ValueError, match="pre-merge"):
        ExperimentRunner(cfg, tasks).run()
    # Selecting a base from before the merge still evaluates the original PR.
    data = yaml.safe_load(path.read_text())
    data["pr"]["base"] = head + "^"
    path.write_text(yaml.safe_dump(data))
    from skilldiff.revisions import resolve_comparison

    cfg, _ = load_experiment(path)
    assert resolve_comparison(cfg.pr)["treatment_commit"] == head


def test_pr_workspace_does_not_expose_source_history(pr_experiment, tmp_path):
    from skilldiff.workspace import Workspace

    _, repo, base, head = pr_experiment
    ws = Workspace(tmp_path / "control", False, None, repo, source_commit=base)
    ws.setup()
    assert (ws.root / "feature.txt").read_text() == "old"
    assert git(ws.root, "rev-list", "--count", "HEAD") == "1"
    proc = subprocess.run(["git", "-C", str(ws.root), "cat-file", "-e", head], capture_output=True)
    assert proc.returncode != 0


def test_pr_snapshot_baselines_tracked_ignored_files(pr_experiment, tmp_path):
    from skilldiff.workspace import Workspace

    _, repo, _, _ = pr_experiment
    (repo / ".gitignore").write_text("tracked.txt\n")
    (repo / "tracked.txt").write_text("original")
    git(repo, "add", ".gitignore")
    git(repo, "add", "-f", "tracked.txt")
    git(repo, "commit", "-qm", "tracked but ignored")
    ws = Workspace(
        tmp_path / "control", False, None, repo, source_commit=git(repo, "rev-parse", "HEAD")
    )
    ws.setup()
    (ws.root / "tracked.txt").write_text("changed")
    diff, files = ws.get_diff()
    assert "tracked.txt" in files
    assert "+changed" in diff


def test_pr_diff_includes_agent_configuration_changes(pr_experiment, tmp_path):
    from skilldiff.workspace import Workspace

    _, repo, base, _ = pr_experiment
    ws = Workspace(tmp_path / "control", False, None, repo, source_commit=base)
    ws.setup()
    (ws.root / ".agents").mkdir()
    (ws.root / ".agents" / "config.txt").write_text("change")
    diff, files = ws.get_diff()
    assert ".agents/config.txt" in diff
    assert files


def test_find_pr_touched_skills(tmp_path):
    from skilldiff.revisions import find_pr_touched_skills

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "skills" / "foo").mkdir(parents=True)
    (repo / "skills" / "foo" / "SKILL.md").write_text("---\nname: foo\n---\n")
    (repo / "SKILL.md").write_text("---\nname: root\n---\n")
    (repo / "unrelated.txt").write_text("v1")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    base = git(repo, "rev-parse", "HEAD")

    (repo / "skills" / "foo" / "helper.py").write_text("print(1)")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "touch foo")
    head1 = git(repo, "rev-parse", "HEAD")
    assert find_pr_touched_skills(repo, base, head1) == ["skills/foo"]

    (repo / "SKILL.md").write_text("---\nname: root\n---\nupdated")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "touch root")
    head2 = git(repo, "rev-parse", "HEAD")
    assert find_pr_touched_skills(repo, head1, head2) == [""]


def test_workspace_installs_pr_touched_skills(tmp_path):
    from skilldiff.workspace import Workspace

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    skill_dir = fixture / "nested" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n")

    ws_root = tmp_path / "ws"
    ws = Workspace(
        ws_root,
        is_treatment=False,
        skill_dir=None,
        fixture_repo=fixture,
        pr_touched_skills=["nested/my-skill"],
    )
    ws.setup()
    installed = ws.root / ".claude" / "skills" / "my-skill" / "SKILL.md"
    assert installed.is_file()
    assert ws.is_treatment is True
    assert ws.root / ".claude" / "skills" / "my-skill" in ws.skill_dirs


def test_resolve_comparison_populates_touched_skills(tmp_path):
    from skilldiff.config import PRConfig
    from skilldiff.revisions import resolve_comparison

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "skills" / "bar").mkdir(parents=True)
    (repo / "skills" / "bar" / "SKILL.md").write_text("---\nname: bar\n---\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "branch", "base")

    (repo / "skills" / "bar" / "SKILL.md").write_text("---\nname: bar\n---\nupdated")
    git(repo, "commit", "-qam", "head")
    git(repo, "branch", "feature")

    pr_cfg = PRConfig(repo=repo, base="base", head="feature", mode="agent")
    res = resolve_comparison(pr_cfg)
    assert res.get("touched_skills") == ["skills/bar"]


def test_cli_check_pr_touched_skills_warnings_and_messages(tmp_path, monkeypatch, capsys):
    from argparse import Namespace

    from skilldiff.cli import cmd_check

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_BIN", "/bin/echo")

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "skills" / "baz").mkdir(parents=True)
    (repo / "skills" / "baz" / "SKILL.md").write_text("---\nname: baz\n---\n")
    (repo / "task.txt").write_text("1")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "branch", "base")

    (repo / "skills" / "baz" / "SKILL.md").write_text("---\nname: baz\n---\nupdated")
    git(repo, "commit", "-qam", "head")
    git(repo, "branch", "feature")

    (tmp_path / "task.yaml").write_text("id: t1\nprompt: test\ngrader:\n  command: echo 1\n")

    cfg_correctness = {
        "name": "pr-test",
        "pr": {"repo": "./repo", "base": "base", "head": "feature", "mode": "correctness"},
        "models": ["test"],
        "tasks": ["task.yaml"],
    }
    cfg_path = tmp_path / "skilldiff.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg_correctness))

    assert cmd_check(Namespace(config=str(cfg_path), no_grade=True)) == 0
    out = capsys.readouterr().out
    assert "PR touches skill(s) skills/baz, but pr.mode is 'correctness'" in out

    cfg_agent = {
        "name": "pr-test",
        "pr": {"repo": "./repo", "base": "base", "head": "feature", "mode": "agent"},
        "models": ["test"],
        "tasks": ["task.yaml"],
    }
    cfg_path.write_text(yaml.safe_dump(cfg_agent))

    assert cmd_check(Namespace(config=str(cfg_path), no_grade=True)) == 0
    out = capsys.readouterr().out
    assert "PR touches skill(s): skills/baz; installed into agent workspaces" in out


def test_cli_init_pr_warns_on_skill_touched(tmp_path, monkeypatch, capsys):
    from argparse import Namespace

    from skilldiff.cli import cmd_init

    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "skills" / "demo").mkdir(parents=True)
    (repo / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "branch", "base")

    (repo / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n---\nmod")
    git(repo, "commit", "-qam", "head")
    git(repo, "update-ref", "refs/pull/123/head", "HEAD")

    args = Namespace(
        pr=123,
        repo=str(repo),
        base="base",
        harness="claude",
        dir=str(tmp_path / "exp"),
        force=False,
        pr_mode="correctness",
        pr_pair="merge-base",
    )
    cmd_init(args)
    out = capsys.readouterr().out
    assert "Warning: The PR touches a SKILL.md path, but the chosen mode 'correctness'" in out

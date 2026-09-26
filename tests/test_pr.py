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

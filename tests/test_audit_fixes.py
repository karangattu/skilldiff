"""Focused coverage for audit fixes: A/B config, baseline, resume, PR workflows."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from skilldiff.config import load_experiment
from skilldiff.experiment import ExperimentRunner, balanced_three_order
from skilldiff.reporter import _verdict, build_markdown_report


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _skill(tmp_path: Path, name: str, desc: str, body: str = "body") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n")
    return d


def _task_file(tmp_path: Path, tid: str = "t1") -> Path:
    td = tmp_path / "tasks"
    td.mkdir(parents=True, exist_ok=True)
    f = td / f"{tid}.yaml"
    f.write_text(f"id: {tid}\nprompt: do it\ncategory: intended\n")
    return f


def test_ab_config_requires_both_and_distinct_dirs(tmp_path):
    _skill(tmp_path, "v1", "d")
    _task_file(tmp_path)
    exp = tmp_path / "skilldiff.yaml"
    exp.write_text(
        yaml.safe_dump(
            {
                "name": "ab",
                "skill_a": "./v1",
                "models": ["m"],
                "tasks": ["./tasks/*.yaml"],
                "runs": 1,
            }
        )
    )
    with pytest.raises(ValueError, match="both"):
        load_experiment(exp)


def test_compression_preset_requires_matching_trigger(tmp_path):
    _skill(tmp_path, "s", "trigger one", body="long body " * 50)
    _skill(tmp_path, "m", "trigger two", body="short")
    # Rename dirs to distinct paths with same skill name but different desc.
    (tmp_path / "m" / "SKILL.md").write_text(
        "---\nname: s\ndescription: different trigger\n---\n\nshort\n"
    )
    _task_file(tmp_path)
    exp = tmp_path / "skilldiff.yaml"
    exp.write_text(
        yaml.safe_dump(
            {
                "name": "c",
                "preset": "compression",
                "skill_a": "./s",
                "skill_b": "./m",
                "models": ["m"],
                "tasks": ["./tasks/*.yaml"],
                "runs": 1,
            }
        )
    )
    with pytest.raises(ValueError, match="identical skill name"):
        load_experiment(exp)


def test_compression_preset_loads_with_matching_trigger(tmp_path):
    _skill(tmp_path, "orig", "same trigger", body="long body " * 100)
    b = _skill(tmp_path, "mini", "same trigger", body="short")
    (b / "SKILL.md").write_text("---\nname: orig\ndescription: same trigger\n---\n\nshort\n")
    _task_file(tmp_path)
    exp = tmp_path / "skilldiff.yaml"
    exp.write_text(
        yaml.safe_dump(
            {
                "name": "c",
                "preset": "compression",
                "skill_a": "./orig",
                "skill_b": "./mini",
                "models": ["m"],
                "tasks": ["./tasks/*.yaml"],
                "runs": 1,
                "thresholds": {
                    "acceptable_score_regression_pp": 2,
                    "required_token_reduction_pct": 20,
                },
            }
        )
    )
    cfg, tasks = load_experiment(exp)
    assert cfg.preset == "compression"
    assert cfg.thresholds["required_token_reduction_pct"] == 20


def test_ab_run_labels_and_source_sizes(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    _skill(tmp_path, "v1", "d", body="x" * 500)
    _skill(tmp_path, "v2", "d", body="x")
    _task_file(tmp_path)
    exp = tmp_path / "skilldiff.yaml"
    exp.write_text(
        yaml.safe_dump(
            {
                "name": "ab",
                "preset": "revision",
                "skill_a": "./v1",
                "skill_b": "./v2",
                "models": ["m"],
                "tasks": ["./tasks/*.yaml"],
                "runs": 1,
            }
        )
    )
    cfg, tasks = load_experiment(exp)
    res = ExperimentRunner(cfg, tasks, output_dir=tmp_path / "runs").run()
    assert res["preset"] == "revision"
    assert res["arm_labels"] == {"control": "Skill A", "treatment": "Skill B"}
    assert res["skill_comparison"]["source_bytes_a"] > res["skill_comparison"]["source_bytes_b"]
    # A/B control carries skill A: must not warn as contaminated.
    assert not any("clean baseline" in w for w in res["warnings"])
    assert not any("INVALID" in w for w in res["warnings"])


def test_baseline_balanced_order_and_summaries(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    _skill(tmp_path, "v1", "d")
    _skill(tmp_path, "v2", "d")
    _task_file(tmp_path)
    exp = tmp_path / "skilldiff.yaml"
    exp.write_text(
        yaml.safe_dump(
            {
                "name": "ab",
                "skill_a": "./v1",
                "skill_b": "./v2",
                "include_baseline": True,
                "models": ["m"],
                "tasks": ["./tasks/*.yaml"],
                "runs": 3,
            }
        )
    )
    cfg, tasks = load_experiment(exp)
    res = ExperimentRunner(cfg, tasks, output_dir=tmp_path / "runs").run()
    assert len(res["runs"]["baseline"]) == 3
    assert set(res["baseline_comparisons"]) == {"baseline_vs_a", "baseline_vs_b"}
    assert res["baseline_comparisons"]["baseline_vs_a"]["pairs"] == 3
    # Balanced rotation: each arm first at least once across 3 reps.
    orders = [balanced_three_order("m", "t1", r, res["seed"]) for r in (1, 2, 3)]
    firsts = {o[0] for o in orders}
    assert firsts == {"control", "treatment", "baseline"}
    first_positions = [r["run_order"] for r in res["runs"]["baseline"]]
    assert set(first_positions) != {3}, "baseline must not always run third"
    md = build_markdown_report(res)
    assert "Baseline comparisons" in md


def test_resume_refuses_changed_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    a = _skill(tmp_path, "v1", "d", body="one")
    _skill(tmp_path, "v2", "d", body="two")
    _task_file(tmp_path)
    exp = tmp_path / "skilldiff.yaml"
    exp.write_text(
        yaml.safe_dump(
            {
                "name": "ab",
                "skill_a": "./v1",
                "skill_b": "./v2",
                "models": ["m"],
                "tasks": ["./tasks/*.yaml"],
                "runs": 2,
            }
        )
    )
    cfg, tasks = load_experiment(exp)
    res = ExperimentRunner(cfg, tasks, output_dir=tmp_path / "runs").run()
    run_dir = Path(res["run_dir"])
    before = json.loads((run_dir / "experiment.json").read_text())
    # Change skill B content, then resume must refuse before overwriting metadata.
    (a / "SKILL.md").write_text("---\nname: v1\ndescription: d\n---\n\nCHANGED\n")
    cfg2, tasks2 = load_experiment(exp)
    with pytest.raises(ValueError, match="incompatible"):
        ExperimentRunner(cfg2, tasks2, output_dir=tmp_path / "runs").run(resume=run_dir)
    after = json.loads((run_dir / "experiment.json").read_text())
    assert before["provenance"]["skill_hash"] == after["provenance"]["skill_hash"]


def test_resume_refuses_changed_pr_commits(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@e.com")
    git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("old")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "branch", "base")
    (repo / "f.txt").write_text("new")
    git(repo, "commit", "-qam", "feat")
    git(repo, "branch", "feat")
    (tmp_path / "task.yaml").write_text("id: t1\nprompt: hi\n")
    exp = tmp_path / "skilldiff.yaml"
    exp.write_text(
        yaml.safe_dump(
            {
                "name": "pr",
                "pr": {"repo": "./repo", "base": "base", "head": "feat"},
                "models": ["m"],
                "tasks": ["task.yaml"],
                "runs": 1,
            }
        )
    )
    cfg, tasks = load_experiment(exp)
    res = ExperimentRunner(cfg, tasks, output_dir=tmp_path / "runs").run()
    run_dir = Path(res["run_dir"])
    (repo / "f.txt").write_text("newer")
    git(repo, "commit", "-qam", "feat2")
    cfg2, tasks2 = load_experiment(exp)
    with pytest.raises(ValueError, match="incompatible"):
        ExperimentRunner(cfg2, tasks2, output_dir=tmp_path / "runs").run(resume=run_dir)


def test_pr_workflow_options_load(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@e.com")
    git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("x")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "b")
    _task_file(tmp_path)
    exp = tmp_path / "skilldiff.yaml"
    exp.write_text(
        yaml.safe_dump(
            {
                "name": "pr",
                "preset": "pr",
                "pr": {
                    "repo": "./repo",
                    "base": "HEAD",
                    "head": "HEAD",
                    "mode": "correctness",
                    "pair": "base-merge",
                },
                "models": ["m"],
                "tasks": ["./tasks/*.yaml"],
                "runs": 1,
            }
        )
    )
    cfg, _ = load_experiment(exp)
    assert cfg.preset == "pr"
    assert cfg.pr.mode == "correctness"
    assert cfg.pr.pair == "base-merge"


def test_compression_verdict_on_equal_scores_with_savings():
    paired = {
        "pairs": 6,
        "score": {"mean_diff": 0.0, "ci_low": 0.0, "ci_high": 0.0, "n": 6},
        "cost": {
            "mean_diff": -0.2,
            "relative_change": -0.4,
            "ci_low": -0.3,
            "ci_high": -0.1,
            "n": 6,
        },
        "tokens": {
            "mean_diff": -500,
            "relative_change": -0.5,
            "ci_low": -700,
            "ci_high": -300,
            "n": 6,
        },
    }
    thresholds = {
        "acceptable_score_regression_pp": 2,
        "required_token_reduction_pct": 20,
    }
    verdict, kind = _verdict(
        paired, "minified", thresholds=thresholds, tasks_count=4, preset="compression"
    )
    assert "Quality preserved" in verdict
    assert "Practical check" in verdict
    assert "meets compression criteria" in verdict
    assert kind == "tip"


def test_equal_scores_without_thresholds_stays_neutral():
    paired = {
        "pairs": 6,
        "score": {"mean_diff": 0.0, "ci_low": 0.0, "ci_high": 0.0, "n": 6},
    }
    verdict, _ = _verdict(paired, "skill", thresholds=None, tasks_count=4)
    assert "No task-score difference" in verdict

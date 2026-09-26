"""Tests for correctness improvements."""

from skilldiff.compare import compare_results
from skilldiff.config import GraderConfig, load_task
from skilldiff.grader import Grader
from skilldiff.reporter import (
    _metric_rows,
    _verdict,
    calculate_metrics,
    compare_checks,
    extract_checks,
)
from skilldiff.stats import paired_comparison


def _run(model="m", task="t", rep=1, **kw):
    base = {"model": model, "task_id": task, "repetition": rep, "arm": "control"}
    base.update(kw)
    return base


def test_named_dict_checks_count_correctly():
    run = {"feedback": '{"checks": [{"passed": true}, {"passed": false}]}'}
    assert extract_checks(run) == [("Check 1", True), ("Check 2", False)]
    from skilldiff.reporter import _format_checks_passed

    assert _format_checks_passed(run) == "1/2"


def test_named_checks_with_names():
    run = {
        "feedback": {
            "checks": [
                {"name": "has entry", "passed": True},
                {"name": "type tag", "passed": False},
            ]
        }
    }
    assert extract_checks(run) == [("has entry", True), ("type tag", False)]


def test_per_check_comparison_shows_gains():
    c = [_run(score=1.0, feedback='{"checks": [true, true]}')]
    t = [_run(score=1.0, feedback='{"checks": [true, false]}')]
    # Need task_id/repetition/model for pairing
    for r in c + t:
        r.setdefault("model", "m")
        r.setdefault("task_id", "t")
        r.setdefault("repetition", 1)
    rows = compare_checks(c, t)
    assert len(rows) == 2
    by_name = {r["check"]: r for r in rows}
    assert by_name["Check 1"]["diff_pp"] == 0
    assert by_name["Check 2"]["diff_pp"] == -100
    assert by_name["Check 2"]["losses"] == 1


def test_paired_mean_diff_matches_ci_not_median():
    # Medians: control $0, skill $1 => +$1 median diff.
    # Paired diffs [1,1,-99] => mean -$32.33.
    control = [
        _run(score=1.0, cost=c, duration=10, input_tokens=100, output_tokens=50, num_turns=1)
        for c in [0, 0, 100]
    ]
    skill = [
        _run(score=1.0, cost=c, duration=10, input_tokens=100, output_tokens=50, num_turns=1)
        for c in [1, 1, 1]
    ]
    for i, r in enumerate(control + skill):
        r["repetition"] = (i % 3) + 1
    cm, sm = calculate_metrics(control), calculate_metrics(skill)
    p = paired_comparison(control, skill)
    assert abs(p["cost"]["mean_diff"] + 32.33) < 0.01
    rows = _metric_rows(cm, sm, p)
    cost_row = next(r for r in rows if r[0] == "Cost (median)")
    # Control/Skill show medians, Difference shows paired mean.
    assert cost_row[1] == "$0.00"
    assert cost_row[2] == "$1.00"
    diff_txt = cost_row[3][0] if isinstance(cost_row[3], tuple) else cost_row[3]
    assert "-$32.33" in diff_txt
    assert "-$99" in cost_row[4]  # CI matches paired mean, not median diff


def test_ungraded_scores_na_not_100():
    runs = [_run(score=None, grade_status="ungraded", cost=0.5, duration=10)]
    m = calculate_metrics(runs)
    assert m["task_score"] is None
    assert m["graded_count"] == 0


def test_grader_timeout_is_na_not_zero(tmp_path):
    from skilldiff.grader import Candidate

    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = GraderConfig(type="command", command="sleep 2")
    g = Grader(cfg, skill_name="s", model_name="m", timeout=0.1)
    out = g._evaluate_candidate(Candidate("candidate-A", "control", ws, "", "", ""))
    assert out.score is None
    assert out.grade_status == "timeout"


def test_grader_ungraded_returns_na(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    g = Grader(None, skill_name="s", model_name="m")
    from skilldiff.grader import Candidate

    out = g._evaluate_candidate(Candidate("candidate-A", "control", ws, "", "", ""))
    assert out.score is None
    assert out.grade_status == "ungraded"


def test_missing_cost_is_na_not_zero():
    runs = [_run(score=1.0, duration=10)]  # no cost key
    m = calculate_metrics(runs)
    assert m["median_cost"] is None
    assert m["cost_known_count"] == 0
    p = paired_comparison(runs, runs)
    assert p["cost"]["n"] == 0
    assert p["cost"]["mean_diff"] is None


def test_valid_pair_counts_exclude_ungraded():
    c = [
        _run(task="t", rep=1, score=1.0, grade_status="graded", cost=0.5),
        _run(task="t", rep=2, score=None, grade_status="ungraded", cost=0.5),
    ]
    t = [
        _run(task="t", rep=1, score=0.0, grade_status="graded", cost=0.5),
        _run(task="t", rep=2, score=None, grade_status="ungraded", cost=0.5),
    ]
    p = paired_comparison(c, t)
    assert p["pairs"] == 2
    assert p["score"]["n"] == 1
    assert p["cost"]["n"] == 2


def test_verdict_flags_tiny_sample_and_collapsed():
    paired = {
        "pairs": 2,
        "score": {"mean_diff": 1.0, "ci_low": 1.0, "ci_high": 1.0, "n": 2},
    }
    verdict, _ = _verdict(paired, "skill")
    assert "only 2 pair" in verdict
    assert "CI collapsed" in verdict


def test_verdict_practical_thresholds():
    paired = {
        "pairs": 6,
        "score": {"mean_diff": 0.1, "ci_low": 0.05, "ci_high": 0.15, "n": 6},
        "cost": {
            "mean_diff": -0.1,
            "relative_change": -0.2,
            "ci_low": -0.15,
            "ci_high": -0.05,
            "n": 6,
        },
    }
    v, _ = _verdict(
        paired,
        "skill",
        thresholds={"acceptable_score_regression_pp": 5, "required_cost_reduction_pct": 10},
    )
    assert "Practical check" in v
    assert "meets criteria" in v
    v2, _ = _verdict(
        paired,
        "skill",
        thresholds={"acceptable_score_regression_pp": 5, "required_cost_reduction_pct": 30},
    )
    assert "below required" in v2


def test_task_category_parsing(tmp_path):
    f = tmp_path / "t.yaml"
    f.write_text("id: t1\nprompt: hi\ncategory: irrelevant\n")
    task = load_task(f)
    assert task.category == "irrelevant"


def test_compare_shows_newly_failing():
    a = {
        "name": "a",
        "models": ["m"],
        "tasks": ["t"],
        "harness": "claude",
        "overall": {
            "skill": {"task_score": 0.5, "skill_used_count": 1, "skill_known_count": 2},
            "paired": {"pairs": 1, "score": {"mean_diff": 0.1}},
        },
        "runs": {"treatment": [{"task_id": "t", "feedback": '{"checks": [true]}'}]},
    }
    b = {
        "name": "b",
        "models": ["m"],
        "tasks": ["t"],
        "harness": "claude",
        "overall": {
            "skill": {"task_score": 0.6, "skill_used_count": 1, "skill_known_count": 2},
            "paired": {"pairs": 1, "score": {"mean_diff": 0.2}},
        },
        "runs": {"treatment": [{"task_id": "t", "feedback": '{"checks": [false]}'}]},
    }
    comp = compare_results(a, b)
    assert comp["score"]["delta_pp"] == 10
    assert len(comp["newly_failing"]) == 1
    assert comp["newly_failing"][0]["check"] == "Check 1"

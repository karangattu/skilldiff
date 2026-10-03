from skilldiff.stats import (
    analysis_run,
    bootstrap_ci,
    classify_effect,
    pair_runs,
    paired_comparison,
)


def _run(arm, rep, score, cost=0.0, task="t", model="m"):
    return {"arm": arm, "repetition": rep, "task_id": task, "model": model,
            "score": score, "cost": cost, "duration": 1.0}


def test_pair_runs_matches_model_task_and_repetition():
    control = [_run("control", 1, 0.0), _run("control", 2, 0.5), _run("control", 1, 1, task="u")]
    treatment = [_run("treatment", 2, 1.0), _run("treatment", 1, 1.0)]
    pairs = pair_runs(control, treatment)
    assert [(c["repetition"], t["repetition"]) for c, t in pairs] == [(1, 1), (2, 2)]


def test_bootstrap_ci_needs_two_values_and_brackets_mean():
    assert bootstrap_ci([0.5]) is None
    lo, hi = bootstrap_ci([0.0, 0.5, 1.0, 0.5])
    assert lo <= 0.5 <= hi


def test_paired_comparison_counts_and_effects():
    control = [_run("control", i, 0.0, cost=1.0) for i in range(1, 6)]
    treatment = [_run("treatment", i, 1.0, cost=0.5) for i in range(1, 6)]
    result = paired_comparison(control, treatment)
    assert result["pairs"] == 5
    assert (result["wins"], result["losses"], result["ties"]) == (5, 0, 0)
    assert result["score"]["mean_diff"] == 1.0
    assert classify_effect(result["score"]) == "better"
    assert result["cost"]["relative_change"] == -0.5
    assert classify_effect(result["cost"], higher_is_better=False) == "better"


def test_classify_effect_unclear_when_ci_spans_zero():
    control = [_run("control", i, s) for i, s in enumerate([0, 1, 0, 1], 1)]
    treatment = [_run("treatment", i, s) for i, s in enumerate([1, 0, 1, 0], 1)]
    assert classify_effect(paired_comparison(control, treatment)["score"]) == "unclear"


def test_agent_error_partial_grade_is_excluded_unless_zero_policy():
    control = _run("control", 1, 1 / 11, cost=0.1)
    control.update(status="error", duration=48, input_tokens=1000)
    treatment = _run("treatment", 1, 1.0, cost=0.2)
    treatment["input_tokens"] = 2000

    excluded = paired_comparison([control], [treatment])
    assert excluded["pairs"] == 1
    assert all(excluded[key]["n"] == 0 for key in ("score", "cost", "duration", "tokens"))

    zeroed = analysis_run(control, "zero")
    assert control["score"] == 1 / 11  # Raw grader evidence is unchanged.
    assert zeroed["score"] == 0.0
    included = paired_comparison([zeroed], [treatment])
    assert included["score"]["mean_diff"] == 1.0
    assert included["cost"]["n"] == included["duration"]["n"] == 1

    grader_error = analysis_run({**control, "grade_status": "error"}, "zero")
    assert paired_comparison([grader_error], [treatment])["score"]["n"] == 0

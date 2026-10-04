from pathlib import Path

import skilldiff.reporter as reporter
from skilldiff.reporter import (
    calculate_metrics,
    format_cost_diff,
    format_count_diff,
    format_pp_diff,
    format_time_diff,
    render_report_table,
)


def test_calculate_metrics():
    runs = [
        {"score": 0.8, "success": True, "cost": 0.40, "duration": 90.0},
        {"score": 0.6, "success": False, "cost": 0.50, "duration": 100.0},
    ]
    m = calculate_metrics(runs)
    assert m["task_score"] == 0.7
    assert m["success_count"] == 1
    assert m["total_count"] == 2
    assert m["median_cost"] == 0.45
    assert m["median_time"] == 95.0


def test_diff_formatters():
    assert format_pp_diff(14) == "+14 pp"
    assert format_pp_diff(-5) == "-5 pp"
    assert format_count_diff(3) == "+3"
    assert format_count_diff(-1) == "-1"
    assert format_cost_diff(0.06) == "+$0.06"
    assert format_cost_diff(-0.02) == "-$0.02"
    assert format_time_diff(-7) == "-7s"
    assert format_time_diff(5) == "+5s"


def test_render_report_table():
    c_metrics = {
        "task_score": 0.72,
        "success_count": 5,
        "total_count": 9,
        "median_cost": 0.42,
        "median_time": 95.0,
    }
    s_metrics = {
        "task_score": 0.86,
        "success_count": 8,
        "total_count": 9,
        "median_cost": 0.48,
        "median_time": 88.0,
    }
    table = render_report_table(
        experiment_name="code-review-skill",
        control_metrics=c_metrics,
        skill_metrics=s_metrics,
        models_count=1,
        tasks_count=3,
        runs_per_arm=3,
    )

    assert "code-review-skill" in table
    assert "72%" in table
    assert "86%" in table
    assert "+14 pp" in table
    assert "5/9" in table
    assert "8/9" in table
    assert "+3" in table
    assert "$0.42" in table
    assert "$0.48" in table
    assert "+$0.06" in table
    assert "95s" in table
    assert "88s" in table
    assert "-7s" in table
    assert "Models: 1    Tasks: 3    Runs per arm: 3" in table


def test_build_quarto_report_shows_overall_models_and_tasks():
    results = {
        "name": "api-skill",
        "timestamp": "2026-09-22T120000Z",
        "models": ["claude-sonnet-5"],
        "tasks_count": 1,
        "runs_per_arm": 2,
        "overall": {
            "control": {
                "task_score": 0.5,
                "success_count": 1,
                "total_count": 2,
                "median_cost": 0.4,
                "median_time": 30.0,
            },
            "skill": {
                "task_score": 1.0,
                "success_count": 2,
                "total_count": 2,
                "median_cost": 0.3,
                "median_time": 20.0,
            },
        },
        "by_model": {
            "claude-sonnet-5": {
                "control": {
                    "task_score": 0.5,
                    "success_count": 1,
                    "total_count": 2,
                    "median_cost": 0.4,
                    "median_time": 30.0,
                },
                "skill": {
                    "task_score": 1.0,
                    "success_count": 2,
                    "total_count": 2,
                    "median_cost": 0.3,
                    "median_time": 20.0,
                },
                "runs_count": 2,
                "by_task": {
                    "fix-parser": {
                        "control": {
                            "task_score": 0.5,
                            "success_count": 1,
                            "total_count": 2,
                            "median_cost": 0.4,
                            "median_time": 30.0,
                        },
                        "skill": {
                            "task_score": 1.0,
                            "success_count": 2,
                            "total_count": 2,
                            "median_cost": 0.3,
                            "median_time": 20.0,
                        },
                    }
                },
            }
        },
    }

    report = reporter.build_quarto_report(results)

    assert 'title: "skilldiff: api-skill"' in report
    assert "::: {.callout-tip}" in report
    assert "Skill improved task score by **50 percentage points**" in report
    assert "## Summary" in report
    assert "claude-sonnet-5" in report
    assert "| fix-parser | 50% | 100% | +50 pp |" in report


def test_calculate_metrics_computes_totals():
    runs = [
        {
            "score": 1.0,
            "success": True,
            "cost": 0.10,
            "duration": 50.0,
            "input_tokens": 1000,
            "output_tokens": 200,
            "tool_calls": 3,
        },
        {
            "score": 0.5,
            "success": False,
            "cost": 0.20,
            "duration": 60.0,
            "input_tokens": 1500,
            "output_tokens": 300,
            "tool_calls": 5,
        },
    ]
    m = calculate_metrics(runs)
    assert m["total_duration"] == 110.0
    assert m["total_cost"] == 0.30
    assert m["total_input_tokens"] == 2500
    assert m["total_output_tokens"] == 500
    assert m["total_tool_calls"] == 8


def test_format_checks_passed():
    from skilldiff.reporter import _format_checks_passed

    assert _format_checks_passed({"feedback": '{"checks": [true, false, true]}'}) == "2/3"
    assert _format_checks_passed({"feedback": {"checks": [True, True]}}) == "2/2"
    # Named-dict checks must look inside, not count nonempty dicts as True.
    assert _format_checks_passed(
        {"feedback": '{"checks": [{"passed": true}, {"passed": false}]}'}
    ) == "1/2"
    assert _format_checks_passed(
        {"feedback": {"checks": [{"name": "a", "passed": True}, {"name": "b", "passed": False}]}}
    ) == "1/2"
    assert _format_checks_passed({"success": True}) == "1/1"
    assert _format_checks_passed({"success": False}) == "0/1"
    assert _format_checks_passed({}) == "-"
    assert _format_checks_passed({"grade_status": "ungraded"}) == "N/A"


def test_build_quarto_report_with_runs_table_and_takeaways():
    ctrl_runs = [
        {
            "task_id": "t1",
            "arm": "control",
            "repetition": 1,
            "score": 1.0,
            "success": True,
            "duration": 60.0,
            "input_tokens": 80000,
            "output_tokens": 4000,
            "feedback": '{"checks": [true, true, true]}',
        }
    ]
    treat_runs = [
        {
            "task_id": "t1",
            "arm": "treatment",
            "repetition": 1,
            "score": 0.8,
            "success": False,
            "duration": 50.0,
            "input_tokens": 70000,
            "output_tokens": 3500,
            "feedback": '{"checks": [false, true, true]}',
        }
    ]
    results = {
        "name": "eval-run",
        "timestamp": "2026-09-22T170000Z",
        "models": ["gemini-3.8"],
        "tasks_count": 1,
        "runs_per_arm": 1,
        "overall": {
            "control": calculate_metrics(ctrl_runs),
            "skill": calculate_metrics(treat_runs),
        },
        "by_model": {
            "gemini-3.8": {
                "control": calculate_metrics(ctrl_runs),
                "skill": calculate_metrics(treat_runs),
                "runs_count": 1,
                "by_task": {
                    "t1": {
                        "control": calculate_metrics(ctrl_runs),
                        "skill": calculate_metrics(treat_runs),
                    }
                },
                "runs": {"control": ctrl_runs, "treatment": treat_runs},
            }
        },
        "runs": {"control": ctrl_runs, "treatment": treat_runs},
    }

    report = reporter.build_quarto_report(results)
    assert "## Key takeaways" in report
    assert "**Accuracy.**" in report
    assert "lower in 1, and tied in 0 of 1" in report
    assert "**Sample size.**" in report
    assert "## Run details" in report
    # Missing cost/turns are N/A, not zero/-.
    assert "| t1 | 1 | Control | ok | 100% | 3/3 | ? | N/A | 60s | N/A | 84k | 0 |" in report
    assert "| t1 | 1 | Skill | ok | 80% | 2/3 | ? | N/A | 50s | N/A | 74k | 0 |" in report
    assert "One pair can't separate a real effect from noise" in report


def _runs(arm: str, scores: list[float], cost: float, skill_invoked=None) -> list[dict]:
    return [
        {
            "model": "m",
            "task_id": "t",
            "repetition": i + 1,
            "arm": arm,
            "status": "ok",
            "score": sc,
            "success": sc == 1.0,
            "cost": cost,
            "duration": 10.0,
            "input_tokens": 100,
            "cache_read_tokens": 1000,
            "output_tokens": 50,
            "num_turns": 4,
            "skill_invoked": skill_invoked,
            "artifacts": f"m/t/{arm}/{i + 1:03d}",
        }
        for i, sc in enumerate(scores)
    ]


def _results(control: list[dict], treatment: list[dict], **extra) -> dict:
    return {
        "name": "exp",
        "timestamp": "2026-09-22T000000Z",
        "harness": "claude",
        "models": ["m"],
        "tasks": ["t"],
        "tasks_count": 1,
        "runs_per_arm": len(control),
        "runs": {"control": control, "treatment": treatment},
        **extra,
    }


def test_report_verdict_uses_confidence_interval():
    control = _runs("control", [0.0] * 6, 0.5)
    treatment = _runs("treatment", [1.0] * 6, 0.25, skill_invoked=True)
    md = reporter.build_markdown_report(_results(control, treatment))
    assert "> [!TIP]" in md
    assert "improved task score by **+100 pp**" in md
    # Collapsed intervals and valid-pair counts flagged in headline/CI.
    assert "CI collapsed" in md
    assert "(n=6)" in md
    assert "With the skill, runs cost 50% less, summed over the compared pairs." in md
    assert "| Skill used | unknown | 6/6 |" in md
    assert "**Adoption.** The agent used the skill in 6 of 6 skill runs" in md


def test_report_flags_unclear_effect_and_warnings(tmp_path):
    control = _runs("control", [1.0, 0.0, 1.0, 0.0], 0.5)
    treatment = _runs("treatment", [0.0, 1.0, 1.0, 0.0], 0.5, skill_invoked=False)
    results = _results(control, treatment, warnings=["The agent did not use the skill"])
    md = reporter.build_markdown_report(results, run_root=tmp_path)
    assert "No clear task-score effect" in md
    assert "> [!WARNING]" in md
    assert "The agent did not use the skill" in md
    assert "[transcript](m/t/control/001/transcript.txt)" in md


def test_html_report_is_self_contained(tmp_path):
    control = _runs("control", [0.5, 0.5], 0.5)
    treatment = _runs("treatment", [1.0, 1.0], 0.4, skill_invoked=True)
    paths = reporter.create_reports(_results(control, treatment), tmp_path)
    html_text = paths["html"].read_text()
    assert html_text.startswith("<!doctype html>")
    assert "<style>" in html_text and "<script" not in html_text
    assert 'class="r good"' in html_text
    assert paths["md"].exists() and paths["qmd"].exists()


def test_report_handles_summary_only_results():
    # Older results.json files had metrics but no run records.
    results = {
        "name": "old",
        "models": ["m"],
        "tasks_count": 1,
        "runs_per_arm": 1,
        "overall": {
            "control": {"task_score": 1.0, "success_count": 1, "total_count": 1,
                        "median_cost": 0.0, "median_time": 60.0},
            "skill": {"task_score": 0.8, "success_count": 0, "total_count": 1,
                      "median_cost": 0.0, "median_time": 50.0},
        },
        "by_model": {},
    }
    md = reporter.build_markdown_report(results)
    assert "Skill reduced task score by **20 percentage points**" in md


def test_closing_decision_ships_on_established_gain():
    control = _runs("control", [0.0, 0.0, 0.5, 0.5, 1.0, 1.0], 0.5)
    treatment = _runs("treatment", [1.0, 1.0, 1.0, 1.0, 1.0, 1.0], 0.25, skill_invoked=True)
    md = reporter.build_markdown_report(_results(control, treatment, tasks_count=4))

    # The report ends with the decision, after the reading notes.
    assert md.index("## Closing decision") > md.index("## How to read this report")
    closing = md[md.index("## Closing decision") :]
    assert "| Task score (mean) |" in closing
    assert "| Cost (median) |" in closing
    assert "| Time (median) |" in closing
    assert "| Tokens (median) |" in closing
    assert "| Adoption (skill used) |" in closing
    assert "> **Recommendation: SHIP**" in closing
    # Recommendation is the last thing in the report.
    assert md.rstrip().endswith("the interval excludes zero.")


def test_closing_decision_needs_more_runs_when_ci_includes_zero():
    control = _runs("control", [1.0, 0.0, 1.0, 0.0], 0.5)
    treatment = _runs("treatment", [0.0, 1.0, 1.0, 0.0], 0.5, skill_invoked=True)
    md = reporter.build_markdown_report(_results(control, treatment, tasks_count=3))
    assert "> **Recommendation: NEEDS MORE RUNS**" in md
    assert "The interval includes zero" in md


def test_closing_decision_rejects_established_regression():
    control = _runs("control", [1.0] * 6, 0.5)
    treatment = _runs("treatment", [0.0, 0.0, 0.0, 0.5, 0.5, 1.0], 0.5, skill_invoked=True)
    md = reporter.build_markdown_report(_results(control, treatment, tasks_count=4))
    assert "> **Recommendation: DO NOT SHIP**" in md
    assert "the regression is established" in md


def test_closing_decision_rejects_contaminated_control():
    control = _runs("control", [0.0] * 6, 0.5)
    treatment = _runs("treatment", [1.0] * 6, 0.5, skill_invoked=True)
    md = reporter.build_markdown_report(_results(control, treatment, valid=False))
    assert "> **Recommendation: DO NOT SHIP**" in md
    assert "Control contamination" in md


def test_closing_decision_honors_preregistered_thresholds():
    # Identical gains in every pair (collapsed CI) still ship when the
    # pre-registered bounds clear; without thresholds the collapse is caution.
    control = _runs("control", [0.0] * 6, 0.5)
    treatment = _runs("treatment", [1.0] * 6, 0.25, skill_invoked=True)
    without = reporter.build_markdown_report(_results(control, treatment, tasks_count=4))
    assert "> **Recommendation: NEEDS MORE RUNS**" in without
    assert "CI collapsed" in without

    with_th = reporter.build_markdown_report(
        _results(
            control,
            treatment,
            tasks_count=4,
            thresholds={"acceptable_score_regression_pp": 5},
        )
    )
    assert "> **Recommendation: SHIP**" in with_th
    assert "meets criteria" in with_th


def test_closing_decision_uses_held_out_pairs_only():
    def runs(arm: str, task: str, split: str, scores: list[float]) -> list[dict]:
        out = []
        for i, sc in enumerate(scores):
            r = _runs(arm, [sc], 0.5, skill_invoked=arm != "control")[0]
            r["task_id"] = task
            r["task_split"] = split
            r["repetition"] = i + 1
            r["artifacts"] = f"m/{task}/{arm}/{i + 1:03d}"
            out.append(r)
        return out

    control = runs("control", "dev-task", "dev", [0.0] * 3) + runs(
        "control", "held-task", "held-out", [0.5] * 3
    )
    treatment = runs("treatment", "dev-task", "dev", [1.0] * 3) + runs(
        "treatment", "held-task", "held-out", [0.5] * 3
    )
    md = reporter.build_markdown_report(
        _results(control, treatment, tasks=["dev-task", "held-task"], tasks_count=2)
    )
    # Dev looks like a huge win, but headline and decision are held-out only.
    assert "improved task score" not in md
    assert "No task-score difference" in md
    assert "Decision uses the 3 held-out pair(s) only" in md
    assert "> **Recommendation: " in md
    rec = md[md.index("> **Recommendation:") :]
    assert "held-out pair(s) only" in rec


def test_terminal_table_prints_recommendation():
    control = _runs("control", [0.0, 0.0, 0.5, 0.5, 1.0, 1.0], 0.5)
    treatment = _runs("treatment", [1.0] * 6, 0.25, skill_invoked=True)
    text = reporter.render_report_table(
        "exp",
        reporter.calculate_metrics(control),
        reporter.calculate_metrics(treatment),
        models_count=1,
        tasks_count=4,
        runs_per_arm=6,
        paired=reporter.paired_comparison(control, treatment),
    )
    assert "Recommendation: SHIP" in text


_PRICING = {
    "source": "https://example.com/pricing",
    "date": "2026-09-27",
    "currency": "USD",
    "rates": {
        "m": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    },
}


def _tokened(arm: str, scores: list[float], tokens: dict) -> list[dict]:
    runs = _runs(arm, scores, 0.5, skill_invoked=arm != "control")
    for r in runs:
        r.update(tokens)
    return runs


def test_api_equivalent_cost_section_reproduces_estimate():
    # Per run: 100k input + 2M cache read + 20k output with the rates above
    # = $0.30 + $0.60 + $0.30 = $1.20; two runs per arm = $2.40.
    control = _tokened(
        "control",
        [1.0, 1.0],
        {"input_tokens": 100_000, "cache_read_tokens": 2_000_000, "output_tokens": 20_000},
    )
    treatment = _tokened(
        "treatment",
        [1.0, 1.0],
        {"input_tokens": 50_000, "cache_read_tokens": 1_000_000, "output_tokens": 10_000},
    )
    md = reporter.build_markdown_report(
        _results(control, treatment, pricing=_PRICING)
    )

    assert "## API-equivalent cost" in md
    assert "https://example.com/pricing" in md
    assert "checked 2026-09-27" in md
    assert "| Control | 200k | 4.0M | 0 | 40k | $2.40 |" in md
    assert "| Skill | 100k | 2.0M | 0 | 20k | $1.20 |" in md
    assert "| Change (Skill − Control) | -100k | -2.0M | 0 | -20k | -$1.20 |" in md
    # Rate table is in the note, so the report reproduces standalone.
    assert "`m`: input $3.00, output $15.00, cache read $0.30, cache write $3.75" in md


def test_api_equivalent_cost_flags_unpriced_models():
    control = _tokened(
        "control", [1.0, 1.0], {"input_tokens": 1000, "output_tokens": 100}
    )
    treatment = _tokened(
        "treatment", [1.0, 1.0], {"input_tokens": 1000, "output_tokens": 100}
    )
    treatment[0]["model"] = "m2"
    md = reporter.build_markdown_report(_results(control, treatment, pricing=_PRICING))
    assert "No rates recorded for `m2`" in md
    assert "excluded from the API-equivalent cost" in md


def test_api_equivalent_cost_section_absent_without_rates():
    control = _runs("control", [1.0, 1.0], 0.5)
    treatment = _runs("treatment", [1.0, 1.0], 0.5, skill_invoked=True)
    md = reporter.build_markdown_report(_results(control, treatment))
    assert "## API-equivalent cost" not in md
    assert "No pricing rates were recorded with this run" in md


def test_evaluation_table_totals_by_app_and_arm():
    control = _tokened(
        "control", [1.0, 0.5],
        {"input_tokens": 100_000, "cache_read_tokens": 2_000_000,
         "cache_creation_tokens": 10_000, "output_tokens": 20_000, "tool_calls": 3},
    )
    treatment = _tokened(
        "treatment", [1.0, 1.0],
        {"input_tokens": 50_000, "cache_read_tokens": 1_000_000,
         "cache_creation_tokens": 0, "output_tokens": 10_000, "tool_calls": 2},
    )
    results = _results(control, treatment, pricing=_PRICING)
    table = reporter.render_evaluation_table(results)
    assert (
        "| App | Arm | Score | Time | Input | Cached input | Output | Total tokens | "
        "Tool calls | Skill loaded | API-equivalent cost |"
    ) in table
    assert (
        "| t | Control | 75% | 20s | 200,000 | 4,020,000 | 40,000 | 4,260,000 | "
        "6 | 0/2 | $2.48 |"
    ) in table
    assert (
        "| t | Skill | 100% | 20s | 100,000 | 2,000,000 | 20,000 | 2,120,000 | "
        "4 | 2/2 | $1.20 |"
    ) in table
    for build in (reporter.build_markdown_report, reporter.build_quarto_report):
        report = build(results)
        assert table.splitlines()[2] in report
        assert report.index("## Evaluation results") < report.index("## Closing decision")
    assert "<th class=\"r\">Cached input</th>" in reporter.build_html_report(results)


def test_evaluation_table_preserves_unknown_metrics_and_cost():
    control = [{"task_id": "unknown", "arm": "control", "model": "m"}]
    table = reporter.render_evaluation_table(_results(control, [], pricing=_PRICING))
    assert "| unknown | Control | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |" in table
    control[0].update(input_tokens=0, output_tokens=0, tool_calls=0, skill_invoked=False)
    table = reporter.render_evaluation_table(_results(control, []))
    assert "| unknown | Control | N/A | N/A | 0 | 0 | 0 | 0 | 0 | no | N/A |" in table


def test_skill_summary_columns_match_rendered_evaluation_table():
    skill = Path(__file__).resolve().parents[1] / "skills" / "skilldiff" / "SKILL.md"
    template_headers = [
        line.replace("**", "").strip()
        for line in skill.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("| **App** |")
    ]
    table = reporter.render_evaluation_table(_results(_runs("control", [1.0], 0.5), []))
    assert template_headers == [table.splitlines()[0]], (
        "Update the skill's final-summary template and the rendered evaluation table together."
    )


def test_evaluation_table_keeps_models_baseline_and_arm_labels():
    control = _tokened("control", [1.0], {"tool_calls": 2})
    treatment = _tokened("treatment", [0.5], {"tool_calls": 1})
    treatment[0]["model"] = "other"
    results = _results(
        control, treatment, arm_labels={"control": "Original", "treatment": "Minified"}
    )
    results["runs"]["baseline"] = _tokened("baseline", [1.0], {"tool_calls": 1})
    table = reporter.render_evaluation_table(results)
    assert "| t (m) | Original |" in table
    assert "| t (other) | Minified |" in table
    assert "| t (m) | Baseline |" in table


def test_evaluation_table_does_not_present_partial_totals_as_complete():
    control = _tokened("control", [1.0, 1.0], {"tool_calls": 2})
    control[1].pop("duration")
    control[1].pop("tool_calls")
    pricing = {**_PRICING, "rates": {}}
    control[1]["skill_invoked"] = None
    table = reporter.render_evaluation_table(_results(control, [], pricing=pricing))
    assert "| t | Control | 100% | N/A |" in table
    assert "| N/A | 0/1 (1 unknown) | N/A |" in table

    control[1]["output_tokens"] = None
    table = reporter.render_evaluation_table(_results(control, [], pricing=_PRICING))
    assert "| N/A | N/A | N/A | N/A | 0/1 (1 unknown) | N/A |" in table

    control[1]["input_tokens"] = None
    control[1]["cache_read_tokens"] = None
    control[1]["output_tokens"] = None
    table = reporter.render_evaluation_table(_results(control, [], pricing=_PRICING))
    assert "| t | Control | 100% | N/A | N/A | N/A | N/A | N/A |" in table
    assert table.rstrip().endswith("| N/A |")


def test_evaluation_completeness_row():
    control = _runs("control", [1.0, 1.0, 1.0, 1.0], 0.5)
    treatment = _runs("treatment", [1.0, 1.0, 1.0, 1.0], 0.5, skill_invoked=True)
    treatment[0]["status"] = "error"  # agent infrastructure failure
    treatment[1]["grade_status"] = "error"  # grader failure -> N/A score
    treatment[1]["score"] = None
    md = reporter.build_markdown_report(_results(control, treatment, interrupted=True))

    assert "## Evaluation completeness" in md
    # Planned (1 model × 1 task × 4 runs) / completed / usable / failures / errors.
    assert "| 4 | 4 | 2 | 1 | 1 |" in md
    assert (
        "Partial — 1 agent failure(s), 1 grader error(s), 2 pair(s) ungraded" in md
    )
    assert "The run was interrupted" in md


def test_excluded_agent_failure_keeps_partial_evidence_out_of_all_comparisons():
    control = _runs("control", [1.0, 1 / 11], 0.1)
    treatment = _runs("treatment", [1.0, 1.0], 0.2, skill_invoked=True)
    control[1].update(
        status="error", duration=48.0, input_tokens=1000,
        feedback={"checks": [{"name": "repair", "passed": False}]},
    )
    treatment[1].update(duration=90.0, input_tokens=10000, cost=10.0)
    treatment[1]["feedback"] = {"checks": [{"name": "repair", "passed": True}]}
    results = _results(
        control, treatment, failure_policy={"agent_failure": "exclude"}, pricing=_PRICING
    )
    md = reporter.build_markdown_report(results)
    paired = reporter.paired_comparison(control, treatment)

    assert paired["score"]["n"] == paired["duration"]["n"] == 1
    assert "| 2 | 2 | 1 | 1 | 0 |" in md  # One unusable score pair.
    assert "| Task score (mean) | 100% | 100% | 0 pp |" in md
    assert "| Success | 1/1 | 1/1 | 0 |" in md
    assert "| Time (median) | 10s | 10s | 0s |" in md
    assert "time 10s → 10s" in md
    assert "## By check" not in md
    assert "N/A (agent failure)" in md
    assert "0/1 (partial)" in md
    assert reporter.calculate_metrics(control)["total_input_tokens"] == 100
    assert "| Control | 100 | 1.0k | 0 | 50 |" in md
    assert "| Skill | 100 | 1.0k | 0 | 50 |" in md
    table = reporter.render_evaluation_table(results)
    assert "| t | Control | 100% | 10s | 100 | 1,000 | 50 | 1,150 |" in table
    assert control[1]["score"] == 1 / 11  # Reporter did not mutate the saved record.


def test_comparative_tables_use_matched_pairs_for_each_metric():
    for failure in ("agent", "grader"):
        control = _runs("control", [1.0, 1.0], 0.1)
        treatment = _runs("treatment", [1.0, 0.0], 0.1, skill_invoked=True)
        for run in control + treatment:
            run.update(task_split="held-out", task_category="intended")
        control[1]["feedback"] = {"checks": [{"name": "repair", "passed": False}]}
        treatment[1]["feedback"] = {"checks": [{"name": "repair", "passed": True}]}
        if failure == "agent":
            control[1]["status"] = "error"
        else:
            control[1].update(grade_status="error", score=None, success=None)
        other_control = _runs("control", [1.0], 0.1)[0]
        other_treatment = _runs("treatment", [1.0], 0.1, skill_invoked=True)[0]
        for run in (other_control, other_treatment):
            run.update(model="n", task_id="u", task_split="dev", task_category="irrelevant")
        md = reporter.build_markdown_report(_results(
            control + [other_control], treatment + [other_treatment],
            models=["m", "n"], tasks=["t", "u"], tasks_count=2,
            task_categories={"t": "intended", "u": "irrelevant"},
            failure_policy={"agent_failure": "exclude"}, pricing=_PRICING,
        ))

        assert "| Task score (mean) | 100% | 100% | 0 pp |" in md
        for label in ("held-out", "m", "t (m)"):
            assert f"| {label} | 100% | 100% | 0 pp |" in md
        assert "| intended | 1 | 100% | 100% | 0 pp |" in md
        assert "1/2 usable score pair(s)" in md
        assert "**Ceiling effect.**" not in md
        expected_time = "10s" if failure == "agent" else "20s"
        assert f"| t (m) | Control | 100% | {expected_time} |" in md
        assert f"| t (m) | Skill | 100% | {expected_time} |" in md
        assert "## By check" not in md
        assert "0/1 (partial)" in md  # Raw failed attempt is still auditable.


def test_api_cost_requires_complete_token_breakdowns_on_both_sides():
    control = _runs("control", [1.0], 0.1)
    treatment = _runs("treatment", [1.0], 0.1, skill_invoked=True)
    control[0]["output_tokens"] = None
    results = _results(control, treatment, pricing=_PRICING)
    md = reporter.build_markdown_report(results)
    evaluation = reporter.render_evaluation_table(results)

    assert "| Control | N/A | N/A | N/A | N/A | N/A |" in md
    assert "| Skill | N/A | N/A | N/A | N/A | N/A |" in md
    assert "| t | Control | 100% | 10s | N/A | N/A | N/A | N/A |" in evaluation
    assert "| t | Skill | 100% | 10s | N/A | N/A | N/A | N/A |" in evaluation


def test_failed_only_pair_has_no_comparative_score_or_efficiency():
    control = _runs("control", [1 / 11], 0.1)
    treatment = _runs("treatment", [1.0], 0.1, skill_invoked=True)
    control[0].update(
        status="error", feedback={"checks": [{"name": "repair", "passed": False}]}
    )
    md = reporter.build_markdown_report(_results(
        control, treatment, failure_policy={"agent_failure": "exclude"}, pricing=_PRICING
    ))

    assert "| t | N/A | N/A | N/A |" in md  # By task
    assert "| t | Control | N/A | N/A | N/A | N/A | N/A | N/A |" in md
    assert "| t | Skill | N/A | N/A | N/A | N/A | N/A | N/A |" in md
    assert "N/A (agent failure)" in md
    assert "0/1 (partial)" in md


def test_zero_policy_overrides_partial_grade_but_not_grader_error():
    control = _runs("control", [1 / 11, 1 / 11], 0.1)
    treatment = _runs("treatment", [1.0, 1.0], 0.2)
    control[0]["status"] = "error"
    control[1].update(status="error", grade_status="error")
    md = reporter.build_markdown_report(_results(
        control, treatment, failure_policy={"agent_failure": "zero"}
    ))
    assert "| 2 | 2 | 1 | 2 | 1 |" in md
    assert "0% (failure policy)" in md
    assert "N/A (agent failure)" in md


def test_evaluation_completeness_row_complete_run():
    control = _runs("control", [1.0, 1.0], 0.5)
    treatment = _runs("treatment", [1.0, 1.0], 0.5, skill_invoked=True)
    md = reporter.build_markdown_report(_results(control, treatment))
    assert "| 2 | 2 | 2 | 0 | 0 | Complete |" in md


def _split_runs(arm: str, task: str, split: str, scores: list[float]) -> list[dict]:
    out = []
    for i, sc in enumerate(scores):
        r = _runs(arm, [sc], 0.5, skill_invoked=arm != "control")[0]
        r["task_id"] = task
        r["task_split"] = split
        r["repetition"] = i + 1
        r["artifacts"] = f"m/{task}/{arm}/{i + 1:03d}"
        out.append(r)
    return out


def test_by_split_section_separates_dev_and_held_out():
    control = _split_runs("control", "dev-task", "dev", [0.0] * 3) + _split_runs(
        "control", "held-task", "held-out", [0.5] * 3
    )
    treatment = _split_runs("treatment", "dev-task", "dev", [1.0] * 3) + _split_runs(
        "treatment", "held-task", "held-out", [0.5] * 3
    )
    md = reporter.build_markdown_report(
        _results(control, treatment, tasks=["dev-task", "held-task"], tasks_count=2)
    )

    assert "## By split" in md
    assert (
        "The headline and closing decision use the held-out set only "
        "(3/3 usable score pair(s))"
    ) in md
    split_table = md[md.index("## By split") :]
    assert "| dev |" in split_table
    assert "| held-out |" in split_table
    # Held-out shows no effect while dev shows a huge one.
    dev_line = next(ln for ln in split_table.splitlines() if ln.startswith("| dev |"))
    held_line = next(ln for ln in split_table.splitlines() if ln.startswith("| held-out |"))
    assert "Skill wins" in dev_line
    assert "50% | 50%" in held_line
    assert "No clear difference" in held_line


def test_by_split_section_flags_missing_held_out():
    control = _split_runs("control", "dev-task", "dev", [0.0, 1.0])
    treatment = _split_runs("treatment", "dev-task", "dev", [1.0, 1.0])
    md = reporter.build_markdown_report(_results(control, treatment))
    assert "## By split" in md
    assert "No held-out pairs in this run" in md
    assert "development data, not validation" in md


def test_by_split_section_absent_without_split_metadata():
    control = _runs("control", [1.0, 1.0], 0.5)
    treatment = _runs("treatment", [1.0, 1.0], 0.5, skill_invoked=True)
    md = reporter.build_markdown_report(_results(control, treatment))
    assert "## By split" not in md

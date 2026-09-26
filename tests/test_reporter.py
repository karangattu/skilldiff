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
    assert "With the skill, runs cost 50% less, summed over all pairs." in md
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

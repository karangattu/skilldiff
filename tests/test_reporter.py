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
    assert "## Overall result" in report
    assert "Skill improved task score by **50 percentage points**" in report
    assert "claude-sonnet-5" in report
    assert "fix-parser" in report
    assert "| 50% | 100% | +50 pp |" in report
    assert "## Key Takeaways" in report


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
    assert _format_checks_passed({"success": True}) == "1/1"
    assert _format_checks_passed({"success": False}) == "0/1"
    assert _format_checks_passed({}) == "-"


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
    assert "## Key Takeaways" in report
    assert "Task Accuracy & Success Rate" in report
    assert "Execution Speed & Latency" in report
    assert "Token Economy & Context Efficiency" in report
    assert "## Detailed run breakdown" in report
    assert "Checks Passed" in report
    assert "| **t1** | Control | 100% | 1/1 | 60.0s | 80,000 | 4,000 | 3/3 |" in report
    assert "| **t1** | Treatment | 80% | 0/1 | 50.0s | 70,000 | 3,500 | 2/3 |" in report
    assert "| **Overall** | **Control** |" in report
    assert "| **Overall** | **Treatment** |" in report
    assert "| **Difference** | |" in report


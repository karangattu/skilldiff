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

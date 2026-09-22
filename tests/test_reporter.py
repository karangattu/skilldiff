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

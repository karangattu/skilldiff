import statistics
from typing import Any


def calculate_metrics(runs_data: list[dict[str, Any]]) -> dict[str, Any]:
    if not runs_data:
        return {
            "task_score": 0.0,
            "success_count": 0,
            "total_count": 0,
            "median_cost": 0.0,
            "median_time": 0.0,
        }

    scores = [r.get("score", 0.0) for r in runs_data]
    successes = [1 if r.get("success") else 0 for r in runs_data]
    costs = [r.get("cost", 0.0) for r in runs_data]
    times = [r.get("duration", 0.0) for r in runs_data]

    total = len(runs_data)
    avg_score = statistics.mean(scores) if scores else 0.0
    med_cost = statistics.median(costs) if costs else 0.0
    med_time = statistics.median(times) if times else 0.0

    return {
        "task_score": avg_score,
        "success_count": sum(successes),
        "total_count": total,
        "median_cost": med_cost,
        "median_time": med_time,
    }


def format_pp_diff(diff_pct: int) -> str:
    sign = "+" if diff_pct > 0 else ""
    return f"{sign}{diff_pct} pp"


def format_count_diff(diff_cnt: int) -> str:
    sign = "+" if diff_cnt > 0 else ""
    return f"{sign}{diff_cnt}"


def format_cost_diff(diff_val: float) -> str:
    sign = "+" if diff_val > 0 else ("-" if diff_val < 0 else "")
    return f"{sign}${abs(diff_val):.2f}"


def format_time_diff(diff_val: int) -> str:
    sign = "+" if diff_val > 0 else ""
    return f"{sign}{diff_val}s"


def render_report_table(
    experiment_name: str,
    control_metrics: dict[str, Any],
    skill_metrics: dict[str, Any],
    models_count: int,
    tasks_count: int,
    runs_per_arm: int,
    model_name: str | None = None,
) -> str:
    c_score_pct = round(control_metrics["task_score"] * 100)
    s_score_pct = round(skill_metrics["task_score"] * 100)
    diff_score = s_score_pct - c_score_pct

    c_succ = f"{control_metrics['success_count']}/{control_metrics['total_count']}"
    s_succ = f"{skill_metrics['success_count']}/{skill_metrics['total_count']}"
    diff_succ = skill_metrics["success_count"] - control_metrics["success_count"]

    c_cost = f"${control_metrics['median_cost']:.2f}"
    s_cost = f"${skill_metrics['median_cost']:.2f}"
    diff_cost = skill_metrics["median_cost"] - control_metrics["median_cost"]

    c_time = f"{round(control_metrics['median_time'])}s"
    s_time = f"{round(skill_metrics['median_time'])}s"
    diff_time = round(skill_metrics["median_time"]) - round(control_metrics["median_time"])

    title = experiment_name
    if model_name:
        title = f"{experiment_name} ({model_name})"

    score_diff_str = format_pp_diff(diff_score)
    lines = [
        title,
        "",
        f"{'Metric':<18} {'Control':>8} {'Skill':>10} {'Difference':>16}",
        f"{'Task score':<18} {f'{c_score_pct}%':>8} {f'{s_score_pct}%':>10} {score_diff_str:>16}",
        f"{'Success':<18} {c_succ:>8} {s_succ:>10} {format_count_diff(diff_succ):>16}",
        f"{'Median cost':<18} {c_cost:>8} {s_cost:>10} {format_cost_diff(diff_cost):>16}",
        f"{'Median time':<18} {c_time:>8} {s_time:>10} {format_time_diff(diff_time):>16}",
        "",
        f"Models: {models_count}    Tasks: {tasks_count}    Runs per arm: {runs_per_arm}",
    ]
    return "\n".join(lines)

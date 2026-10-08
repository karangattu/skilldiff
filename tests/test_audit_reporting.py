"""Regression coverage for decisions made from the same eligible evidence everywhere."""

import re

import pytest

from skilldiff.cli import _format_results
from skilldiff.reporter import (
    build_html_report,
    build_markdown_report,
    build_quarto_report,
    create_reports,
)


def evaluation(*, valid=True, held_out=False, usable_tasks=3, models=None):
    models = models or ["m"]
    runs = {"control": [], "treatment": []}
    tasks = ["a", "b", "c"]
    if held_out:
        tasks += ["held-a", "held-b", "held-c"]
    for model in models:
        for rep in (1, 2):
            for i, task in enumerate(tasks):
                split = "held-out" if task.startswith("held-") else "dev"
                gain = 0 if split == "held-out" else (0.4, 0.5, 0.6)[i]
                for arm, score in (("control", 0.1), ("treatment", 0.1 + gain)):
                    graded = i < usable_tasks or split == "held-out"
                    runs[arm].append({
                        "model": model, "task_id": task, "repetition": rep, "arm": arm,
                        "score": score if graded else None,
                        "grade_status": "graded" if graded else "error", "status": "ok",
                        "duration": 1, "cost": 0.1, "task_split": split,
                        "success": False,
                    })
    # Different repetition effects prevent the one-task probe's CI collapsing.
    if usable_tasks == 1:
        for run in runs["treatment"]:
            if run["task_id"] == "a":
                run["score"] += 0.1 if run["repetition"] == 2 else 0
        for rep in (3, 4, 5, 6):
            for arm in runs:
                run = dict(runs[arm][0])
                run["repetition"] = rep
                runs[arm].append(run)
    return {
        "name": "audit", "models": models, "tasks": tasks, "tasks_count": len(tasks),
        "runs_per_arm": 6 if usable_tasks == 1 else 2, "runs": runs, "valid": valid,
    }


def recommendations(text):
    return re.findall(r"Recommendation: (SHIP|DO NOT SHIP|NEEDS MORE RUNS)", text)


@pytest.mark.parametrize("models", [["m"], ["m", "n"]])
@pytest.mark.parametrize("case, expected", [
    ({"valid": False}, "DO NOT SHIP"),
    ({"held_out": True}, "DO NOT SHIP"),
    ({"usable_tasks": 1}, "NEEDS MORE RUNS"),
])
def test_every_surface_uses_validity_split_and_usable_task_coverage(case, expected, models):
    results = evaluation(models=models, **case)
    for build in (_format_results, build_markdown_report, build_html_report, build_quarto_report):
        assert recommendations(build(results)) == [expected]


def test_held_out_decision_statistics_match_terminal_and_saved_report():
    results = evaluation(held_out=True)
    terminal = _format_results(results)
    report = build_markdown_report(results)
    terminal_decision = terminal[terminal.index("Recommendation:"):]
    saved_decision = report[report.index("**Recommendation:"):]
    assert "Both arms scored identically in all 6 graded pair(s)" in terminal_decision
    assert "Both arms scored identically in all 6 graded pair(s)" in saved_decision


def test_report_export_builds_blocks_once(tmp_path, monkeypatch):
    from skilldiff import reporter

    calls = []
    build = reporter.build_report_blocks

    def counted(*args, **kwargs):
        calls.append(True)
        return build(*args, **kwargs)

    monkeypatch.setattr(reporter, "build_report_blocks", counted)
    paths = create_reports(evaluation(valid=False), tmp_path)
    assert len(calls) == 1
    for path in paths.values():
        assert recommendations(path.read_text()) == ["DO NOT SHIP"]


def test_explicitly_unknown_token_component_does_not_become_a_partial_total():
    from skilldiff.reporter import calculate_metrics
    from skilldiff.stats import paired_comparison

    control = {"model": "m", "task_id": "t", "repetition": 1,
               "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0}
    treatment = {**control, "input_tokens": None}
    assert paired_comparison([control], [treatment])["tokens"]["n"] == 0
    assert calculate_metrics([treatment])["total_tokens"] is None


def test_ceiling_effect_verdict_recommends_redesign_instead_of_more_repetitions():
    runs = {
        "control": [
            {"model": "m", "task_id": f"t{i}", "repetition": 1, "arm": "control",
             "score": 1.0, "grade_status": "graded", "status": "ok", "duration": 1, "cost": 0.1,
             "success": True}
            for i in range(5)
        ],
        "treatment": [
            {"model": "m", "task_id": f"t{i}", "repetition": 1, "arm": "treatment",
             "score": 1.0, "grade_status": "graded", "status": "ok", "duration": 1, "cost": 0.1,
             "success": True}
            for i in range(5)
        ],
    }
    results = {
        "name": "ceiling_eval", "models": ["m"], "tasks": [f"t{i}" for i in range(5)],
        "tasks_count": 5, "runs_per_arm": 1, "runs": runs, "valid": True,
    }
    terminal = _format_results(results)
    report = build_markdown_report(results)
    assert "Tasks at ceiling: control already scores 100%" in terminal
    assert "Redesign tasks with harder challenges rather than adding repetitions" in terminal
    assert "Tasks at ceiling: control already scores 100%" in report
    assert "Redesign tasks with harder challenges rather than adding repetitions" in report


"""Metrics and reports for skilldiff experiments.

A report is built once as a list of simple blocks and rendered to three formats:
`report.html` (self-contained, no dependencies), `report.md` (renders on GitHub, handy
for pull requests), and `report.qmd` (for customizing with Quarto).
"""

import html
import json
import re
import statistics
from pathlib import Path
from typing import Any, Optional, Union

from skilldiff.stats import classify_effect, paired_comparison

# --------------------------------------------------------------------------- metrics


def _total_tokens(run: dict[str, Any]) -> int:
    return (
        int(run.get("input_tokens", 0) or 0)
        + int(run.get("cache_read_tokens", 0) or 0)
        + int(run.get("cache_creation_tokens", 0) or 0)
        + int(run.get("output_tokens", 0) or 0)
    )


def calculate_metrics(runs_data: list[dict[str, Any]]) -> dict[str, Any]:
    if not runs_data:
        return {
            "task_score": 0.0,
            "success_count": 0,
            "total_count": 0,
            "median_cost": 0.0,
            "median_time": 0.0,
            "median_tokens": 0,
            "median_turns": 0,
            "total_duration": 0.0,
            "total_cost": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cache_creation_tokens": 0,
            "total_tokens": 0,
            "total_tool_calls": 0,
            "skill_used_count": 0,
            "skill_known_count": 0,
            "error_count": 0,
        }

    scores = [float(r.get("score", 0.0) or 0.0) for r in runs_data]
    costs = [float(r.get("cost", 0.0) or 0.0) for r in runs_data]
    times = [float(r.get("duration", 0.0) or 0.0) for r in runs_data]
    tokens = [_total_tokens(r) for r in runs_data]
    turns = [int(r.get("num_turns", 0) or 0) for r in runs_data]
    known = [r for r in runs_data if r.get("skill_invoked") is not None]

    return {
        "task_score": statistics.mean(scores),
        "success_count": sum(1 for r in runs_data if r.get("success")),
        "total_count": len(runs_data),
        "median_cost": statistics.median(costs),
        "median_time": statistics.median(times),
        "median_tokens": statistics.median(tokens),
        "median_turns": statistics.median(turns),
        "total_duration": sum(times),
        "total_cost": round(sum(costs), 4),
        "total_input_tokens": sum(int(r.get("input_tokens", 0) or 0) for r in runs_data),
        "total_output_tokens": sum(int(r.get("output_tokens", 0) or 0) for r in runs_data),
        "total_cache_read_tokens": sum(
            int(r.get("cache_read_tokens", 0) or 0) for r in runs_data
        ),
        "total_cache_creation_tokens": sum(
            int(r.get("cache_creation_tokens", 0) or 0) for r in runs_data
        ),
        "total_tokens": sum(tokens),
        "total_tool_calls": sum(int(r.get("tool_calls", 0) or 0) for r in runs_data),
        "skill_used_count": sum(1 for r in known if r.get("skill_invoked")),
        "skill_known_count": len(known),
        "error_count": sum(1 for r in runs_data if r.get("status") not in (None, "ok")),
    }


# ------------------------------------------------------------------------ formatting


def format_pp_diff(diff_pct: int) -> str:
    sign = "+" if diff_pct > 0 else ""
    return f"{sign}{diff_pct} pp"


def format_count_diff(diff_cnt: int) -> str:
    sign = "+" if diff_cnt > 0 else ""
    return f"{sign}{diff_cnt}"


def format_cost_diff(diff_val: float) -> str:
    sign = "+" if diff_val > 0.005 else ("-" if diff_val < -0.005 else "")
    return f"{sign}${abs(diff_val):.2f}"


def format_time_diff(diff_val: int) -> str:
    sign = "+" if diff_val > 0 else ""
    return f"{sign}{diff_val}s"


def _fmt_tokens(value: float) -> str:
    value = float(value)
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 10_000:
        return f"{value / 1000:.0f}k"
    if abs(value) >= 1000:
        return f"{value / 1000:.1f}k"
    return f"{value:.0f}"


def _signed_tokens(value: float) -> str:
    text = _fmt_tokens(abs(value))
    return f"+{text}" if value > 0.5 else (f"-{text}" if value < -0.5 else "0")


def _fmt_ci(metric: dict[str, Any], kind: str) -> str:
    lo, hi = metric.get("ci_low"), metric.get("ci_high")
    if lo is None or hi is None:
        return "n/a"
    if kind == "score":
        return f"{lo * 100:+.0f} to {hi * 100:+.0f} pp"
    if kind == "cost":
        return f"{format_cost_diff(lo)} to {format_cost_diff(hi)}"
    if kind == "duration":
        return f"{lo:+.0f}s to {hi:+.0f}s"
    return f"{_signed_tokens(lo)} to {_signed_tokens(hi)}"


def _score_percent(metrics: dict[str, Any]) -> int:
    return round(metrics.get("task_score", 0.0) * 100)


def _score_difference(control: dict[str, Any], skill: dict[str, Any]) -> int:
    return _score_percent(skill) - _score_percent(control)


def _skill_usage(metrics: dict[str, Any]) -> str:
    known = metrics.get("skill_known_count", 0)
    if not known:
        return "unknown"
    return f"{metrics.get('skill_used_count', 0)}/{known}"


def render_report_table(
    experiment_name: str,
    control_metrics: dict[str, Any],
    skill_metrics: dict[str, Any],
    models_count: int,
    tasks_count: int,
    runs_per_arm: int,
    model_name: str | None = None,
    paired: dict[str, Any] | None = None,
) -> str:
    """Plain-text summary for the terminal."""
    c_score_pct = _score_percent(control_metrics)
    s_score_pct = _score_percent(skill_metrics)
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

    title = f"{experiment_name} ({model_name})" if model_name else experiment_name

    def row(label: str, c: str, s: str, d: str) -> str:
        return f"{label:<18} {c:>8} {s:>10} {d:>16}"

    lines = [
        title,
        "",
        f"{'Metric':<18} {'Control':>8} {'Skill':>10} {'Difference':>16}",
        row("Task score", f"{c_score_pct}%", f"{s_score_pct}%", format_pp_diff(diff_score)),
        row("Success", c_succ, s_succ, format_count_diff(diff_succ)),
        row("Median cost", c_cost, s_cost, format_cost_diff(diff_cost)),
        row("Median time", c_time, s_time, format_time_diff(diff_time)),
    ]
    if control_metrics.get("median_tokens") or skill_metrics.get("median_tokens"):
        c_tok = control_metrics.get("median_tokens", 0)
        s_tok = skill_metrics.get("median_tokens", 0)
        lines.append(
            row(
                "Median tokens",
                _fmt_tokens(c_tok),
                _fmt_tokens(s_tok),
                _signed_tokens(s_tok - c_tok),
            )
        )
    if skill_metrics.get("skill_known_count"):
        lines.append(row("Skill used", "-", _skill_usage(skill_metrics), ""))
    lines.extend(
        ["", f"Models: {models_count}    Tasks: {tasks_count}    Runs per arm: {runs_per_arm}"]
    )
    if paired and paired.get("pairs"):
        verdict, _ = _verdict(paired)
        lines.append(_strip_inline(verdict))
    return "\n".join(lines)


# ------------------------------------------------------------------- report blocks

Cell = Union[str, tuple[str, Optional[str]]]  # text, or (text, tone) with tone good/bad


def _tone(value: float, higher_is_better: bool, eps: float = 1e-9) -> Optional[str]:
    if abs(value) <= eps:
        return None
    return "good" if (value > 0) == higher_is_better else "bad"


def _verdict(paired: dict[str, Any]) -> tuple[str, str]:
    """Return (sentence, callout kind) for the task-score effect."""
    n = paired.get("pairs", 0)
    score = paired["score"]
    effect = classify_effect(score)
    mean_pp = round(score["mean_diff"] * 100)
    ci = _fmt_ci(score, "score")
    pairs_txt = f"{n} paired run{'s' if n != 1 else ''}"
    if n < 2:
        if mean_pp == 0:
            return f"No task-score difference in {pairs_txt}. Add repetitions.", "note"
        return (
            f"Task score changed by **{format_pp_diff(mean_pp)}** in {pairs_txt}. "
            "One pair can't separate a real effect from noise.",
            "note",
        )
    if effect == "better":
        return (
            f"The skill improved task score by **{format_pp_diff(mean_pp)}** "
            f"(95% CI {ci}, {pairs_txt}).",
            "tip",
        )
    if effect == "worse":
        return (
            f"The skill reduced task score by **{abs(mean_pp)} pp** "
            f"(95% CI {ci}, {pairs_txt}).",
            "warning",
        )
    if mean_pp == 0 and score["ci_low"] == score["ci_high"] == 0:
        return f"No task-score difference: both arms scored the same in all {pairs_txt}.", "note"
    return (
        f"No clear task-score effect: **{format_pp_diff(mean_pp)}**, but the 95% CI "
        f"({ci}) includes zero ({pairs_txt}).",
        "note",
    )


def _efficiency_sentence(paired: dict[str, Any]) -> Optional[str]:
    phrases: list[str] = []
    for key, less, more in (
        ("cost", "cost {}% less", "cost {}% more"),
        ("duration", "took {}% less time", "took {}% more time"),
        ("tokens", "used {}% fewer tokens", "used {}% more tokens"),
    ):
        metric = paired.get(key) or {}
        rel = metric.get("relative_change")
        if rel is None or abs(rel) < 0.005:
            continue
        text = (less if rel < 0 else more).format(f"{abs(rel) * 100:.0f}")
        if classify_effect(metric) == "unclear":
            text += " (within noise)"
        phrases.append(text)
    if not phrases:
        return None
    joined = phrases[0] if len(phrases) == 1 else ", ".join(phrases[:-1]) + " and " + phrases[-1]
    return f"With the skill, runs {joined}, summed over all pairs."


def _metric_rows(
    control: dict[str, Any], skill: dict[str, Any], paired: dict[str, Any]
) -> list[list[Cell]]:
    score_diff = _score_difference(control, skill)
    success_diff = skill.get("success_count", 0) - control.get("success_count", 0)
    cost_diff = skill.get("median_cost", 0.0) - control.get("median_cost", 0.0)
    time_diff = round(skill.get("median_time", 0.0)) - round(control.get("median_time", 0.0))
    tok_diff = skill.get("median_tokens", 0) - control.get("median_tokens", 0)
    has_cost = bool(control.get("total_cost") or skill.get("total_cost") or
                    control.get("median_cost") or skill.get("median_cost"))
    rows: list[list[Cell]] = [
        [
            "Task score",
            f"{_score_percent(control)}%",
            f"{_score_percent(skill)}%",
            (format_pp_diff(score_diff), _tone(score_diff, True)),
            _fmt_ci(paired.get("score", {}), "score"),
        ],
        [
            "Success",
            f"{control.get('success_count', 0)}/{control.get('total_count', 0)}",
            f"{skill.get('success_count', 0)}/{skill.get('total_count', 0)}",
            (format_count_diff(success_diff), _tone(success_diff, True)),
            "",
        ],
        [
            "Median cost",
            f"${control.get('median_cost', 0.0):.2f}",
            f"${skill.get('median_cost', 0.0):.2f}",
            (format_cost_diff(cost_diff), _tone(round(cost_diff, 2), False)),
            _fmt_ci(paired.get("cost", {}), "cost"),
        ],
        [
            "Median time",
            f"{round(control.get('median_time', 0.0))}s",
            f"{round(skill.get('median_time', 0.0))}s",
            (format_time_diff(time_diff), _tone(time_diff, False)),
            _fmt_ci(paired.get("duration", {}), "duration"),
        ],
    ]
    if not has_cost:
        rows = [r for r in rows if r[0] != "Median cost"]
    if control.get("median_tokens") or skill.get("median_tokens"):
        rows.append(
            [
                "Median tokens",
                _fmt_tokens(control.get("median_tokens", 0)),
                _fmt_tokens(skill.get("median_tokens", 0)),
                (_signed_tokens(tok_diff), _tone(tok_diff, False, eps=0.5)),
                _fmt_ci(paired.get("tokens", {}), "tokens"),
            ]
        )
    if control.get("median_turns") or skill.get("median_turns"):
        turn_diff = skill.get("median_turns", 0) - control.get("median_turns", 0)
        rows.append(
            [
                "Median turns",
                f"{control.get('median_turns', 0):g}",
                f"{skill.get('median_turns', 0):g}",
                (f"{turn_diff:+g}", _tone(turn_diff, False)),
                "",
            ]
        )
    if skill.get("skill_known_count") or control.get("skill_known_count"):
        rows.append(["Skill used", _skill_usage(control), _skill_usage(skill), "", ""])
    return rows


def _format_checks_passed(run: dict[str, Any]) -> str:
    fb = run.get("feedback")
    if fb:
        data: dict[str, Any] | None = None
        if isinstance(fb, str):
            try:
                data = json.loads(fb.strip().splitlines()[-1]) if fb.strip() else None
            except Exception:
                data = None
        elif isinstance(fb, dict):
            data = fb
        if isinstance(data, dict):
            checks = data.get("checks")
            if isinstance(checks, list) and checks:
                passed = sum(1 for c in checks if c)
                return f"{passed}/{len(checks)}"
    succ = run.get("success")
    if succ is not None:
        return "1/1" if succ else "0/1"
    return "-"


def _load_runs_for_report(
    results: dict[str, Any], run_root: Path | None = None
) -> dict[str, list[dict[str, Any]]]:
    if "runs" in results and isinstance(results["runs"], dict):
        c = results["runs"].get("control", [])
        t = results["runs"].get("treatment", [])
        if c or t:
            return {"control": list(c), "treatment": list(t)}

    search_dirs: list[Path] = []
    if run_root and run_root.exists():
        search_dirs.append(run_root)
    if "run_dir" in results and results["run_dir"]:
        p = Path(results["run_dir"])
        if p.exists() and p not in search_dirs:
            search_dirs.append(p)

    for sdir in search_dirs:
        c_runs: list[dict[str, Any]] = []
        t_runs: list[dict[str, Any]] = []
        for run_file in sorted(sdir.rglob("run.json")):
            try:
                d = json.loads(run_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            d.setdefault("artifacts", str(run_file.parent.relative_to(sdir)))
            if d.get("arm") == "control":
                c_runs.append(d)
            elif d.get("arm") in {"treatment", "skill"}:
                t_runs.append(d)
        if c_runs or t_runs:
            return {"control": c_runs, "treatment": t_runs}

    return {"control": [], "treatment": []}


def _run_status(run: dict[str, Any]) -> Cell:
    status = str(run.get("status") or "ok")
    return ("ok", None) if status == "ok" else (status, "bad")


def _skill_cell(run: dict[str, Any]) -> Cell:
    invoked = run.get("skill_invoked")
    if invoked is None:
        return "?"
    if run.get("arm") == "control":
        return ("yes", "bad") if invoked else "no"
    return "yes" if invoked else ("no", "bad")


def _build_runs_table(
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
    multi_model: bool = False,
    link_artifacts: bool = True,
) -> tuple[list[str], list[list[Cell]]]:
    headers = ["Task", "Run", "Arm", "Status", "Score", "Checks", "Skill used",
               "Cost", "Time", "Turns", "Tokens", "Files"]
    if multi_model:
        headers.insert(0, "Model")
    if link_artifacts:
        headers.append("Artifacts")

    def key(r: dict[str, Any]) -> tuple[str, str, int]:
        return (str(r.get("model", "")), str(r.get("task_id", "")), int(r.get("repetition", 1)))

    ctrl_map = {key(r): r for r in control_runs}
    treat_map = {key(r): r for r in treatment_runs}
    ordered_keys = list(dict.fromkeys([key(r) for r in control_runs + treatment_runs]))

    rows: list[list[Cell]] = []
    for k in ordered_keys:
        for arm_label, run in (("Control", ctrl_map.get(k)), ("Skill", treat_map.get(k))):
            if run is None:
                continue
            row: list[Cell] = [
                str(run.get("task_id", "")),
                str(run.get("repetition", 1)),
                arm_label,
                _run_status(run),
                f"{round(float(run.get('score', 0.0)) * 100)}%",
                _format_checks_passed(run),
                _skill_cell(run),
                f"${float(run.get('cost', 0.0) or 0.0):.2f}",
                f"{float(run.get('duration', 0.0) or 0.0):.0f}s",
                str(run.get("num_turns") or "-"),
                _fmt_tokens(_total_tokens(run)),
                str(len(run.get("files_changed") or [])),
            ]
            if multi_model:
                row.insert(0, str(run.get("model", "")))
            if link_artifacts:
                art = run.get("artifacts")
                row.append(
                    f"[transcript]({art}/transcript.txt) · [diff]({art}/diff.patch)" if art else ""
                )
            rows.append(row)
    return headers, rows


def _key_takeaways(
    results: dict[str, Any],
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
) -> list[str]:
    paired = paired_comparison(control_runs, treatment_runs)
    control = calculate_metrics(control_runs)
    skill = calculate_metrics(treatment_runs)
    bullets: list[str] = []

    n = paired["pairs"]
    if n:
        bullets.append(
            f"**Accuracy.** Control averaged {_score_percent(control)}% and the skill "
            f"{_score_percent(skill)}%. Pair by pair, the skill scored higher in "
            f"{paired['wins']}, lower in {paired['losses']}, and tied in {paired['ties']} "
            f"of {n}."
        )

    efficiency = _efficiency_sentence(paired)
    if efficiency:
        totals = [f"time {control['total_duration']:.0f}s → {skill['total_duration']:.0f}s"]
        if control["total_cost"] or skill["total_cost"]:
            totals.insert(0, f"cost ${control['total_cost']:.2f} → ${skill['total_cost']:.2f}")
        bullets.append(f"**Efficiency.** {efficiency} Totals: {', '.join(totals)}.")

    if skill.get("skill_known_count"):
        used, known = skill["skill_used_count"], skill["skill_known_count"]
        text = f"**Adoption.** The agent used the skill in {used} of {known} skill runs"
        c_used = control.get("skill_used_count", 0)
        text += (
            f"; {c_used} control run(s) also referenced it." if c_used
            else "; no control run referenced it."
        )
        if used < known:
            text += (
                " Runs where the skill was ignored dilute any effect. A sharper "
                "`description` usually fixes this."
            )
        bullets.append(text)

    task_effects: list[tuple[float, str]] = []
    for task_id in results.get("tasks") or sorted({r["task_id"] for r in control_runs}):
        tc = [r for r in control_runs if r["task_id"] == task_id]
        tt = [r for r in treatment_runs if r["task_id"] == task_id]
        if tc and tt:
            task_effects.append((paired_comparison(tc, tt)["score"]["mean_diff"], task_id))
    if len(task_effects) > 1:
        best = max(task_effects)
        worst = min(task_effects)
        if best[0] > 0:
            bullets.append(
                f"**Biggest gain:** `{best[1]}` ({format_pp_diff(round(best[0] * 100))})."
            )
        if worst[0] < 0:
            bullets.append(
                f"**Biggest regression:** `{worst[1]}` "
                f"({format_pp_diff(round(worst[0] * 100))})."
            )
    if task_effects and all(
        abs(e) < 1e-9 for e, _ in task_effects
    ) and _score_percent(control) >= 95:
        bullets.append(
            "**Ceiling effect.** Control already solves these tasks, so accuracy can't "
            "improve. Add harder tasks the skill is designed for, such as obscure APIs, "
            "recent changes, or house conventions."
        )

    runs_per_arm = int(results.get("runs_per_arm") or 1)
    if n and (runs_per_arm < 5 or n < 5):
        bullets.append(
            f"**Sample size.** {n} pair(s) with {runs_per_arm} repetition(s) per task "
            "gives wide error bars. Use `runs: 5` or more before treating the result "
            "as conclusive."
        )
    return bullets


def build_report_blocks(
    results: dict[str, Any], run_root: Path | None = None
) -> tuple[str, list[tuple]]:
    name = str(results.get("name", "experiment"))
    runs_data = _load_runs_for_report(results, run_root)
    control_runs = runs_data["control"]
    treatment_runs = runs_data["treatment"]

    if control_runs or treatment_runs:
        control = calculate_metrics(control_runs)
        skill = calculate_metrics(treatment_runs)
        paired = paired_comparison(control_runs, treatment_runs)
    else:  # Summary-only results (older versions or hand-built input).
        overall = results.get("overall", {})
        control = {**calculate_metrics([]), **overall.get("control", {})}
        skill = {**calculate_metrics([]), **overall.get("skill", {})}
        paired = overall.get("paired") or {}

    blocks: list[tuple] = []

    if paired.get("pairs"):
        verdict, kind = _verdict(paired)
    else:
        diff = _score_difference(control, skill)
        if diff > 0:
            verdict, kind = f"Skill improved task score by **{diff} percentage points**.", "tip"
        elif diff < 0:
            verdict = f"Skill reduced task score by **{abs(diff)} percentage points**."
            kind = "warning"
        else:
            verdict, kind = "No measured task-score change between control and skill.", "note"
    body = [verdict]
    efficiency = _efficiency_sentence(paired) if paired.get("pairs") else None
    if efficiency:
        body.append(efficiency)
    blocks.append(("callout", kind, "Verdict", body))

    warnings = list(results.get("warnings") or [])
    if warnings:
        blocks.append(("callout", "warning", "Check before trusting this result", warnings))

    models = results.get("models", [])
    blocks.append(("h", 2, "Summary"))
    blocks.append(
        (
            "table",
            ["Metric", "Control", "Skill", "Difference", "95% CI"],
            _metric_rows(control, skill, paired),
            ["l", "r", "r", "r", "r"],
        )
    )
    blocks.append(
        (
            "p",
            f"**Harness:** {results.get('harness', 'claude')} · **Models:** {len(models)} · "
            f"**Tasks:** {results.get('tasks_count', 0)} · "
            f"**Runs per arm:** {results.get('runs_per_arm', 0)}",
        )
    )

    if control_runs:
        takeaways = _key_takeaways(results, control_runs, treatment_runs)
        if takeaways:
            blocks.append(("h", 2, "Key takeaways"))
            blocks.append(("ul", takeaways))

    by_model = results.get("by_model", {})
    if len(models) > 1:
        blocks.append(("h", 2, "By model"))
        rows: list[list[Cell]] = []
        for model in models:
            if control_runs:
                mc = [r for r in control_runs if r.get("model") == model]
                mt = [r for r in treatment_runs if r.get("model") == model]
                m_control, m_skill = calculate_metrics(mc), calculate_metrics(mt)
                m_paired = paired_comparison(mc, mt)
            else:
                m_control = by_model.get(model, {}).get("control", {})
                m_skill = by_model.get(model, {}).get("skill", {})
                m_paired = {}
            rows.append(_group_row(model, m_control, m_skill, m_paired))
        blocks.append(("table", _group_headers("Model"), rows, _GROUP_ALIGN))

    blocks.append(("h", 2, "By task"))
    task_rows: list[list[Cell]] = []
    for model in models:
        task_ids = results.get("tasks") or list(by_model.get(model, {}).get("by_task", {}).keys())
        for task_id in task_ids:
            if control_runs:
                tc = [
                    r for r in control_runs
                    if r.get("model") == model and r.get("task_id") == task_id
                ]
                tt = [
                    r for r in treatment_runs
                    if r.get("model") == model and r.get("task_id") == task_id
                ]
                t_control, t_skill = calculate_metrics(tc), calculate_metrics(tt)
                t_paired = paired_comparison(tc, tt)
            else:
                task_data = by_model.get(model, {}).get("by_task", {}).get(task_id, {})
                t_control, t_skill = task_data.get("control", {}), task_data.get("skill", {})
                t_paired = {}
            label = f"{task_id} ({model})" if len(models) > 1 else task_id
            task_rows.append(_group_row(label, t_control, t_skill, t_paired))
    blocks.append(("table", _group_headers("Task"), task_rows, _GROUP_ALIGN))

    if control_runs or treatment_runs:
        headers, run_rows = _build_runs_table(
            control_runs,
            treatment_runs,
            multi_model=len(models) > 1,
            link_artifacts=run_root is not None,
        )
        blocks.append(("h", 2, "Run details"))
        randomized = any("run_order" in r for r in control_runs)
        blocks.append(
            ("p", "Each pair ran in identical fresh workspaces"
             + (", in random order" if randomized else "")
             + ". Only the skill arm had the skill installed.")
        )
        align = ["r" if h in _NUMERIC_RUN_COLUMNS else "l" for h in headers]
        blocks.append(("table", headers, run_rows, align))

    blocks.append(("h", 2, "Setup"))
    setup_items = [
        f"**Skill:** `{results.get('skill', '')}`"
        + (
            f" (names: {', '.join(f'`{n}`' for n in results['skill_names'])})"
            if results.get("skill_names") else ""
        ),
        f"**Models:** {', '.join(f'`{m}`' for m in models)}",
    ]
    for key, value in (results.get("settings") or {}).items():
        if key == "harness":
            continue
        shown = ", ".join(f"`{v}`" for v in value) if isinstance(value, list) else f"`{value}`"
        setup_items.append(f"**{key}:** {shown}")
    if results.get("skilldiff_version"):
        setup_items.append(f"**skilldiff:** {results['skilldiff_version']}")
    blocks.append(("ul", setup_items))

    blocks.append(("h", 2, "How to read this report"))
    blocks.append(
        (
            "ul",
            [
                "Differences are **skill minus control**. Higher scores are better; lower "
                "cost, time, and tokens are better.",
                "The 95% CI is a bootstrap interval over paired runs. If it includes zero, "
                "the difference could be noise.",
                "Tokens include cached input where the harness reports it. Claude's cost "
                "is the API-equivalent price, even on a subscription.",
                "*Skill used* comes from the harness's tool calls (Claude) or from "
                "references to the skill's files in the transcript (other harnesses).",
            ],
        )
    )
    return f"skilldiff: {name}", blocks


_NUMERIC_RUN_COLUMNS = {"Run", "Score", "Checks", "Cost", "Time", "Turns", "Tokens", "Files"}


def _group_headers(first: str) -> list[str]:
    return [first, "Control", "Skill", "Δ score", "Better/worse/tie", "Δ cost", "Δ time",
            "Skill used"]


_GROUP_ALIGN = ["l", "r", "r", "r", "r", "r", "r", "r"]


def _group_row(
    label: str, control: dict[str, Any], skill: dict[str, Any], paired: dict[str, Any]
) -> list[Cell]:
    score_diff = _score_difference(control, skill)
    cost_diff = skill.get("median_cost", 0.0) - control.get("median_cost", 0.0)
    time_diff = round(skill.get("median_time", 0.0) - control.get("median_time", 0.0))
    wlt = (
        f"{paired.get('wins', 0)}/{paired.get('losses', 0)}/{paired.get('ties', 0)}"
        if paired.get("pairs") else "-"
    )
    return [
        label,
        f"{_score_percent(control)}%",
        f"{_score_percent(skill)}%",
        (format_pp_diff(score_diff), _tone(score_diff, True)),
        wlt,
        (format_cost_diff(cost_diff), _tone(round(cost_diff, 2), False)),
        (format_time_diff(time_diff), _tone(time_diff, False)),
        _skill_usage(skill),
    ]


# ------------------------------------------------------------------------ renderers


def _cell_text(cell: Cell) -> str:
    return cell[0] if isinstance(cell, tuple) else cell


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _md_table(headers: list[str], rows: list[list[Cell]], align: Optional[list[str]]) -> list[str]:
    align = align or ["l"] + ["r"] * (len(headers) - 1)
    if len(align) < len(headers):
        align = align + ["l"] * (len(headers) - len(align))
    sep = ["---:" if a == "r" else ":---" for a in align[: len(headers)]]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(sep) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_markdown_cell(_cell_text(c)) for c in row) + " |")
    return lines


def _render_markdown(title: str, blocks: list[tuple], flavor: str, date: str) -> str:
    lines: list[str] = []
    if flavor == "quarto":
        lines.extend(
            [
                "---",
                f"title: {json.dumps(title)}",
                f"date: {json.dumps(date)}",
                "format:",
                "  html:",
                "    theme: cosmo",
                "    toc: true",
                "    embed-resources: true",
                "---",
                "",
            ]
        )
    else:
        lines.extend([f"# {title}", "", f"_{date}_", ""])

    for block in blocks:
        kind = block[0]
        if kind == "h":
            lines.extend(["#" * block[1] + " " + block[2], ""])
        elif kind == "p":
            lines.extend([block[1], ""])
        elif kind == "ul":
            lines.extend([f"- {item}" for item in block[1]] + [""])
        elif kind == "table":
            lines.extend(_md_table(block[1], block[2], block[3]) + [""])
        elif kind == "callout":
            _, ctype, ctitle, body = block
            bullets = len(body) > 1 and ctype == "warning"
            if flavor == "quarto":
                lines.append(f"::: {{.callout-{ctype}}}")
                lines.append(f"## {ctitle}")
                lines.extend([f"- {b}" for b in body] if bullets else body)
                lines.extend([":::", ""])
            else:
                gh = {"tip": "TIP", "warning": "WARNING", "note": "NOTE"}.get(ctype, "NOTE")
                lines.append(f"> [!{gh}]")
                lines.append(f"> **{ctitle}**")
                for b in body:
                    lines.append(f"> - {b}" if bullets else f"> {b}")
                    if not bullets:
                        lines.append(">")
                if lines[-1] == ">":
                    lines.pop()
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_quarto_report(results: dict[str, Any], run_root: Path | None = None) -> str:
    title, blocks = build_report_blocks(results, run_root)
    return _render_markdown(title, blocks, "quarto", str(results.get("timestamp", "")))


def build_markdown_report(results: dict[str, Any], run_root: Path | None = None) -> str:
    title, blocks = build_report_blocks(results, run_root)
    return _render_markdown(title, blocks, "gfm", str(results.get("timestamp", "")))


_INLINE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\)|_(.+?)_(?!\w)")


def _inline_html(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _INLINE.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        bold, code, link_text, link_url, italic = m.groups()
        if bold is not None:
            out.append(f"<strong>{_inline_html(bold)}</strong>")
        elif code is not None:
            out.append(f"<code>{html.escape(code)}</code>")
        elif link_text is not None:
            href = html.escape(link_url, quote=True)
            out.append(f'<a href="{href}">{html.escape(link_text)}</a>')
        else:
            out.append(f"<em>{_inline_html(italic)}</em>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def _strip_inline(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text).replace("`", "")


_CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--soft:#f6f8fa;
--good:#1a7f37;--bad:#cf222e;--tip:#1a7f37;--warning:#9a6700;--note:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;
--line:#3d444d;--soft:#151b23;--good:#3fb950;--bad:#f85149;--tip:#3fb950;
--warning:#d29922;--note:#4493f8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:1100px;margin:0 auto;padding:32px 24px 64px}
h1{font-size:28px;margin:0 0 4px}h2{font-size:20px;margin:36px 0 12px;
padding-bottom:6px;border-bottom:1px solid var(--line)}
.date{color:var(--muted);margin:0 0 24px}
.callout{border-left:4px solid var(--note);background:var(--soft);padding:12px 16px;
margin:16px 0;border-radius:6px}
.callout.tip{border-color:var(--tip)}.callout.warning{border-color:var(--warning)}
.callout-title{font-weight:600;margin:0 0 6px}.callout p{margin:4px 0}
.callout ul{margin:4px 0;padding-left:20px}
.table-wrap{overflow-x:auto;margin:8px 0 16px}
table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{text-align:left;color:var(--muted);font-weight:600;background:var(--soft)}
td.r,th.r{text-align:right}.good{color:var(--good);font-weight:600}
.bad{color:var(--bad);font-weight:600}
code{font:13px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--soft);
padding:1px 5px;border-radius:4px}
a{color:var(--note)}li{margin:4px 0}
"""


def build_html_report(results: dict[str, Any], run_root: Path | None = None) -> str:
    title, blocks = build_report_blocks(results, run_root)
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><main>",
        f"<h1>{html.escape(title)}</h1>",
        f'<p class="date">{html.escape(str(results.get("timestamp", "")))}</p>',
    ]
    for block in blocks:
        kind = block[0]
        if kind == "h":
            parts.append(f"<h{block[1]}>{_inline_html(block[2])}</h{block[1]}>")
        elif kind == "p":
            parts.append(f"<p>{_inline_html(block[1])}</p>")
        elif kind == "ul":
            items = "".join(f"<li>{_inline_html(i)}</li>" for i in block[1])
            parts.append(f"<ul>{items}</ul>")
        elif kind == "table":
            headers, rows, align = block[1], block[2], block[3]
            align = align or ["l"] + ["r"] * (len(headers) - 1)
            align = align + ["l"] * (len(headers) - len(align))
            head = "".join(
                f'<th class="{a}">{html.escape(h)}</th>' for h, a in zip(headers, align)
            )
            body_rows = []
            for row in rows:
                cells = []
                for cell, a in zip(row, align):
                    text, tone = (cell if isinstance(cell, tuple) else (cell, None))
                    cls = " ".join(c for c in (a, tone or "") if c)
                    cells.append(f'<td class="{cls}">{_inline_html(text)}</td>')
                body_rows.append("<tr>" + "".join(cells) + "</tr>")
            parts.append(
                f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
                f"<tbody>{''.join(body_rows)}</tbody></table></div>"
            )
        elif kind == "callout":
            _, ctype, ctitle, body = block
            if len(body) > 1 and ctype == "warning":
                inner = "<ul>" + "".join(f"<li>{_inline_html(b)}</li>" for b in body) + "</ul>"
            else:
                inner = "".join(f"<p>{_inline_html(b)}</p>" for b in body)
            parts.append(
                f'<div class="callout {ctype}"><p class="callout-title">'
                f"{html.escape(ctitle)}</p>{inner}</div>"
            )
    parts.append("</main></body></html>")
    return "\n".join(parts)


def create_reports(results: dict[str, Any], run_root: Path) -> dict[str, Path]:
    """Write report.html, report.md, and report.qmd into run_root."""
    builders = {
        "html": ("report.html", build_html_report),
        "md": ("report.md", build_markdown_report),
        "qmd": ("report.qmd", build_quarto_report),
    }
    paths: dict[str, Path] = {}
    for key, (filename, build) in builders.items():
        paths[key] = run_root / filename
        paths[key].write_text(build(results, run_root), encoding="utf-8")
    return paths


def create_quarto_report(results: dict[str, Any], run_root: Path) -> tuple[Path, Path | None]:
    """Backward-compatible wrapper around create_reports."""
    paths = create_reports(results, run_root)
    return paths["qmd"], paths["html"]

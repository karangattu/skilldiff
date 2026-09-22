import json
import shutil
import statistics
import subprocess
from pathlib import Path
from typing import Any


def calculate_metrics(runs_data: list[dict[str, Any]]) -> dict[str, Any]:
    if not runs_data:
        return {
            "task_score": 0.0,
            "success_count": 0,
            "total_count": 0,
            "median_cost": 0.0,
            "median_time": 0.0,
            "total_duration": 0.0,
            "total_cost": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_tool_calls": 0,
        }

    scores = [r.get("score", 0.0) for r in runs_data]
    successes = [1 if r.get("success") else 0 for r in runs_data]
    costs = [r.get("cost", 0.0) for r in runs_data]
    times = [r.get("duration", 0.0) for r in runs_data]
    input_tokens = [int(r.get("input_tokens", 0) or 0) for r in runs_data]
    output_tokens = [int(r.get("output_tokens", 0) or 0) for r in runs_data]
    tool_calls = [int(r.get("tool_calls", 0) or 0) for r in runs_data]

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
        "total_duration": sum(times),
        "total_cost": round(sum(costs), 4),
        "total_input_tokens": sum(input_tokens),
        "total_output_tokens": sum(output_tokens),
        "total_tool_calls": sum(tool_calls),
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


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _score_percent(metrics: dict[str, Any]) -> int:
    return round(metrics.get("task_score", 0.0) * 100)


def _score_difference(control: dict[str, Any], skill: dict[str, Any]) -> int:
    return _score_percent(skill) - _score_percent(control)


def _quarto_metric_rows(
    control: dict[str, Any], skill: dict[str, Any]
) -> list[str]:
    score_diff = _score_difference(control, skill)
    success_diff = skill.get("success_count", 0) - control.get("success_count", 0)
    cost_diff = skill.get("median_cost", 0.0) - control.get("median_cost", 0.0)
    time_diff = skill.get("median_time", 0.0) - control.get("median_time", 0.0)
    control_success = f"{control.get('success_count', 0)}/{control.get('total_count', 0)}"
    skill_success = f"{skill.get('success_count', 0)}/{skill.get('total_count', 0)}"
    control_score = _score_percent(control)
    skill_score = _score_percent(skill)
    control_cost = control.get("median_cost", 0.0)
    skill_cost = skill.get("median_cost", 0.0)
    control_time = round(control.get("median_time", 0.0))
    skill_time = round(skill.get("median_time", 0.0))
    return [
        f"| Task score | {control_score}% | {skill_score}% | "
        f"{format_pp_diff(score_diff)} |",
        f"| Success | {control_success} | {skill_success} | {format_count_diff(success_diff)} |",
        f"| Median cost | ${control_cost:.2f} | ${skill_cost:.2f} | "
        f"{format_cost_diff(cost_diff)} |",
        f"| Median time | {control_time}s | {skill_time}s | "
        f"{format_time_diff(round(time_diff))} |",
    ]


def _format_checks_passed(run: dict[str, Any]) -> str:
    fb = run.get("feedback")
    if fb:
        data: dict[str, Any] | None = None
        if isinstance(fb, str):
            try:
                data = json.loads(fb)
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
                if d.get("arm") == "control":
                    c_runs.append(d)
                elif d.get("arm") in {"treatment", "skill"}:
                    t_runs.append(d)
            except Exception:
                continue
        if c_runs or t_runs:
            return {"control": c_runs, "treatment": t_runs}

    return {"control": [], "treatment": []}


def _build_runs_table(
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
    runs_per_arm: int = 1,
) -> list[str]:
    if not control_runs and not treatment_runs:
        return []

    lines = [
        "| Task | Arm | Score | Success | Duration | Input Tokens | "
        "Output Tokens | Checks Passed |",
        "|:---|:---|---:|---:|---:|---:|---:|---:|",
    ]

    ctrl_map: dict[tuple[str, int], dict[str, Any]] = {
        (str(r.get("task_id", "")), int(r.get("repetition", 1))): r for r in control_runs
    }
    treat_map: dict[tuple[str, int], dict[str, Any]] = {
        (str(r.get("task_id", "")), int(r.get("repetition", 1))): r for r in treatment_runs
    }

    all_keys = sorted(
        set(ctrl_map.keys()) | set(treat_map.keys()),
        key=lambda k: (k[0], k[1]),
    )

    for task_id, rep in all_keys:
        task_label = f"**{task_id}**"
        if runs_per_arm > 1:
            task_label = f"**{task_id} (run {rep})**"

        if (task_id, rep) in ctrl_map:
            cr = ctrl_map[(task_id, rep)]
            score_str = f"{round(float(cr.get('score', 0.0)) * 100)}%"
            succ_str = "1/1" if cr.get("success") else "0/1"
            dur_str = f"{float(cr.get('duration', 0.0)):.1f}s"
            in_tok = f"{int(cr.get('input_tokens', 0) or 0):,}"
            out_tok = f"{int(cr.get('output_tokens', 0) or 0):,}"
            checks_str = _format_checks_passed(cr)
            lines.append(
                f"| {task_label} | Control | {score_str} | {succ_str} | "
                f"{dur_str} | {in_tok} | {out_tok} | {checks_str} |"
            )

        if (task_id, rep) in treat_map:
            tr = treat_map[(task_id, rep)]
            score_str = f"{round(float(tr.get('score', 0.0)) * 100)}%"
            succ_str = "1/1" if tr.get("success") else "0/1"
            dur_str = f"{float(tr.get('duration', 0.0)):.1f}s"
            in_tok = f"{int(tr.get('input_tokens', 0) or 0):,}"
            out_tok = f"{int(tr.get('output_tokens', 0) or 0):,}"
            checks_str = _format_checks_passed(tr)
            lines.append(
                f"| {task_label} | Treatment | {score_str} | {succ_str} | "
                f"{dur_str} | {in_tok} | {out_tok} | {checks_str} |"
            )

    c_scores = [float(r.get("score", 0.0)) for r in control_runs]
    t_scores = [float(r.get("score", 0.0)) for r in treatment_runs]
    c_score_pct = round((sum(c_scores) / len(c_scores) * 100)) if c_scores else 0
    t_score_pct = round((sum(t_scores) / len(t_scores) * 100)) if t_scores else 0
    diff_score_pct = t_score_pct - c_score_pct

    c_succ = sum(1 for r in control_runs if r.get("success"))
    t_succ = sum(1 for r in treatment_runs if r.get("success"))
    c_tot = len(control_runs)
    t_tot = len(treatment_runs)
    diff_succ = t_succ - c_succ

    c_dur = sum(float(r.get("duration", 0.0)) for r in control_runs)
    t_dur = sum(float(r.get("duration", 0.0)) for r in treatment_runs)
    diff_dur = t_dur - c_dur
    if c_dur > 0:
        dur_pct = (diff_dur / c_dur) * 100
        dur_diff_str = f"{diff_dur:+.1f}s ({dur_pct:+.1f}%)"
    else:
        dur_diff_str = f"{diff_dur:+.1f}s"

    c_in = sum(int(r.get("input_tokens", 0) or 0) for r in control_runs)
    t_in = sum(int(r.get("input_tokens", 0) or 0) for r in treatment_runs)
    diff_in = t_in - c_in
    if c_in > 0:
        in_pct = (diff_in / c_in) * 100
        in_diff_str = f"{diff_in:+,} ({in_pct:+.1f}%)"
    else:
        in_diff_str = f"{diff_in:+,}"

    c_out = sum(int(r.get("output_tokens", 0) or 0) for r in control_runs)
    t_out = sum(int(r.get("output_tokens", 0) or 0) for r in treatment_runs)
    diff_out = t_out - c_out
    if c_out > 0:
        out_pct = (diff_out / c_out) * 100
        out_diff_str = f"{diff_out:+,} ({out_pct:+.1f}%)"
    else:
        out_diff_str = f"{diff_out:+,}"

    score_diff_str = format_pp_diff(diff_score_pct)
    succ_diff_str = format_count_diff(diff_succ)

    lines.extend([
        f"| **Overall** | **Control** | **{c_score_pct}%** | **{c_succ}/{c_tot}** | "
        f"**{c_dur:.1f}s** | **{c_in:,}** | **{c_out:,}** | - |",
        f"| **Overall** | **Treatment** | **{t_score_pct}%** | **{t_succ}/{t_tot}** | "
        f"**{t_dur:.1f}s** | **{t_in:,}** | **{t_out:,}** | - |",
        f"| **Difference** | | **{score_diff_str}** | **{succ_diff_str}** | "
        f"**{dur_diff_str}** | **{in_diff_str}** | **{out_diff_str}** | |",
    ])
    return lines


def _build_key_takeaways(
    results: dict[str, Any],
    control_runs: list[dict[str, Any]] | None = None,
    treatment_runs: list[dict[str, Any]] | None = None,
) -> list[str]:
    overall = results.get("overall", {})
    control = overall.get("control", {})
    skill = overall.get("skill", {})

    c_score = round(control.get("task_score", 0.0) * 100)
    s_score = round(skill.get("task_score", 0.0) * 100)
    score_diff = s_score - c_score
    c_succ = control.get("success_count", 0)
    c_tot = control.get("total_count", 0)
    s_succ = skill.get("success_count", 0)
    s_tot = skill.get("total_count", 0)

    c_runs = control_runs or []
    t_runs = treatment_runs or []

    c_dur = (
        sum(float(r.get("duration", 0.0)) for r in c_runs)
        if c_runs
        else float(control.get("total_duration") or control.get("median_time", 0.0))
    )
    s_dur = (
        sum(float(r.get("duration", 0.0)) for r in t_runs)
        if t_runs
        else float(skill.get("total_duration") or skill.get("median_time", 0.0))
    )
    diff_dur = s_dur - c_dur
    dur_pct = (diff_dur / c_dur * 100) if c_dur > 0 else 0.0

    med_c_time = round(control.get("median_time", 0.0))
    med_s_time = round(skill.get("median_time", 0.0))
    med_time_diff = med_s_time - med_c_time

    c_in = (
        sum(int(r.get("input_tokens", 0) or 0) for r in c_runs)
        if c_runs
        else int(control.get("total_input_tokens", 0) or 0)
    )
    s_in = (
        sum(int(r.get("input_tokens", 0) or 0) for r in t_runs)
        if t_runs
        else int(skill.get("total_input_tokens", 0) or 0)
    )
    diff_in = s_in - c_in
    in_pct = (diff_in / c_in * 100) if c_in > 0 else 0.0

    c_out = (
        sum(int(r.get("output_tokens", 0) or 0) for r in c_runs)
        if c_runs
        else int(control.get("total_output_tokens", 0) or 0)
    )
    s_out = (
        sum(int(r.get("output_tokens", 0) or 0) for r in t_runs)
        if t_runs
        else int(skill.get("total_output_tokens", 0) or 0)
    )
    diff_out = s_out - c_out
    out_pct = (diff_out / c_out * 100) if c_out > 0 else 0.0

    bullets: list[str] = []

    if score_diff > 0:
        bullets.append(
            f"- **Task Accuracy & Success Rate**: The skill improved the task score by "
            f"**{format_pp_diff(score_diff)}** ({c_score}% → {s_score}%), achieving "
            f"{s_succ}/{s_tot} successful runs compared to {c_succ}/{c_tot} in control."
        )
    elif score_diff == 0:
        bullets.append(
            f"- **Task Accuracy & Success Rate**: The skill maintained parity on task score at "
            f"**{s_score}%** ({s_succ}/{s_tot} successes for skill vs "
            f"{c_succ}/{c_tot} for control)."
        )
    else:
        bullets.append(
            f"- **Task Accuracy & Success Rate**: The skill achieved a **{s_score}%** overall "
            f"task score ({s_succ}/{s_tot} passes) compared to **{c_score}%** ({c_succ}/{c_tot} "
            f"passes) in control ({format_pp_diff(score_diff)})."
        )

    if diff_dur < 0:
        bullets.append(
            f"- **Execution Speed & Latency**: Total execution time was "
            f"**{abs(dur_pct):.1f}% faster** with the skill (saving {abs(diff_dur):.1f}s "
            f"total duration; median task duration decreased from {med_c_time}s to {med_s_time}s, "
            f"saving {abs(med_time_diff)}s per task)."
        )
    elif diff_dur > 0:
        bullets.append(
            f"- **Execution Speed & Latency**: Total execution time was "
            f"**{abs(dur_pct):.1f}% longer** with the skill (+{abs(diff_dur):.1f}s "
            f"total duration; median task duration was {med_s_time}s vs {med_c_time}s in control)."
        )
    else:
        bullets.append(
            f"- **Execution Speed & Latency**: Execution time was comparable between arms "
            f"(median duration: {med_s_time}s)."
        )

    if c_in > 0 or s_in > 0:
        if diff_in < 0:
            bullets.append(
                f"- **Token Economy & Context Efficiency**: Providing the skill reduced input "
                f"token consumption by **{abs(in_pct):.1f}%** ({diff_in:,} tokens) and output "
                f"tokens by **{abs(out_pct):.1f}%** ({diff_out:,} tokens), demonstrating that "
                f"structured local documentation eliminates expensive exploratory file searches "
                f"across the repository."
            )
        else:
            bullets.append(
                f"- **Token Economy**: Input tokens changed by {diff_in:+,} ({in_pct:+.1f}%) and "
                f"output tokens changed by {diff_out:+,} ({out_pct:+.1f}%)."
            )

    if diff_in < 0 and diff_dur < 0:
        bullets.append(
            "- **Behavioral Impact**: Having targeted documentation directly in the agent's "
            "workspace enables more focused and autonomous execution, eliminating speculative "
            "tool calls and guesswork."
        )
    else:
        bullets.append(
            "- **Behavioral Impact**: The skill equips the agent with domain-specific "
            "conventions, APIs, and guidelines directly inside the workspace."
        )

    return [
        "## Key Takeaways",
        "",
        *bullets,
    ]


def build_quarto_report(
    results: dict[str, Any], run_root: Path | None = None
) -> str:
    name = str(results.get("name", "experiment"))
    overall = results.get("overall", {})
    control = overall.get("control", {})
    skill = overall.get("skill", {})
    score_diff = _score_difference(control, skill)
    if score_diff > 0:
        verdict = f"Skill improved task score by **{score_diff} percentage points**."
        callout = "tip"
    elif score_diff < 0:
        verdict = f"Skill reduced task score by **{abs(score_diff)} percentage points**."
        callout = "warning"
    else:
        verdict = "No measured task-score change between control and skill."
        callout = "note"

    lines = [
        "---",
        f"title: {json.dumps(f'skilldiff: {name}')}",
        f"date: {json.dumps(str(results.get('timestamp', '')))}",
        "format:",
        "  html:",
        "    theme: cosmo",
        "    toc: true",
        "    embed-resources: true",
        "    code-fold: true",
        "---",
        "",
        "## Overall result",
        "",
        f"::: {{.callout-{callout}}}",
        "## Verdict",
        verdict,
        ":::",
        "",
        "| Metric | Control | Skill | Difference |",
        "|---|---:|---:|---:|",
        *_quarto_metric_rows(control, skill),
        "",
        f"**Models:** {len(results.get('models', []))}  ",
        f"**Tasks:** {results.get('tasks_count', 0)}  ",
        f"**Runs per arm:** {results.get('runs_per_arm', 0)}",
    ]

    runs_data = _load_runs_for_report(results, run_root)
    control_runs = runs_data.get("control", [])
    treatment_runs = runs_data.get("treatment", [])

    key_takeaways = _build_key_takeaways(results, control_runs, treatment_runs)
    if key_takeaways:
        lines.extend(["", *key_takeaways])

    runs_table = _build_runs_table(control_runs, treatment_runs, results.get("runs_per_arm", 1))
    if runs_table:
        lines.extend([
            "",
            "## Detailed run breakdown",
            "",
            *runs_table,
        ])

    lines.extend([
        "",
        "## Model comparison",
        "",
        "| Model | Control score | Skill score | Difference | Control success | "
        "Skill success | Cost difference | Time difference |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])

    by_model = results.get("by_model", {})
    for model in results.get("models", []):
        model_data = by_model.get(model, {})
        model_control = model_data.get("control", {})
        model_skill = model_data.get("skill", {})
        model_score_diff = _score_difference(model_control, model_skill)
        model_cost_diff = model_skill.get("median_cost", 0.0) - model_control.get(
            "median_cost", 0.0
        )
        model_time_diff = model_skill.get("median_time", 0.0) - model_control.get(
            "median_time", 0.0
        )
        model_control_success = (
            f"{model_control.get('success_count', 0)}/"
            f"{model_control.get('total_count', 0)}"
        )
        model_skill_success = (
            f"{model_skill.get('success_count', 0)}/"
            f"{model_skill.get('total_count', 0)}"
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(model),
                    f"{_score_percent(model_control)}%",
                    f"{_score_percent(model_skill)}%",
                    format_pp_diff(model_score_diff),
                    model_control_success,
                    model_skill_success,
                    format_cost_diff(model_cost_diff),
                    format_time_diff(round(model_time_diff)),
                ]
            )
            + " |"
        )

    for model in results.get("models", []):
        model_data = by_model.get(model, {})
        lines.extend(
            [
                "",
                f"## {_markdown_cell(model)}",
                "",
                "### Task breakdown",
                "",
                "| Task | Control score | Skill score | Difference | Control success | "
                "Skill success |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for task_id, task_data in model_data.get("by_task", {}).items():
            task_control = task_data.get("control", {})
            task_skill = task_data.get("skill", {})
            task_control_success = (
                f"{task_control.get('success_count', 0)}/"
                f"{task_control.get('total_count', 0)}"
            )
            task_skill_success = (
                f"{task_skill.get('success_count', 0)}/"
                f"{task_skill.get('total_count', 0)}"
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        _markdown_cell(task_id),
                        f"{_score_percent(task_control)}%",
                        f"{_score_percent(task_skill)}%",
                        format_pp_diff(_score_difference(task_control, task_skill)),
                        task_control_success,
                        task_skill_success,
                    ]
                )
                + " |"
            )

    lines.extend(
        [
            "",
            "## How to read this report",
            "",
            "Task-score and success differences are **skill minus control**, so higher is better. ",
            "Cost and time differences are also skill minus control, so negative values "
            "are better.",
            "",
        ]
    )
    return "\n".join(lines)


def create_quarto_report(
    results: dict[str, Any], run_root: Path
) -> tuple[Path, Path | None]:
    qmd_path = run_root / "report.qmd"
    qmd_path.write_text(build_quarto_report(results, run_root=run_root), encoding="utf-8")

    quarto_bin = shutil.which("quarto")
    if not quarto_bin:
        return qmd_path, None

    html_path = run_root / "report.html"
    proc = subprocess.run(
        [quarto_bin, "render", qmd_path.name, "--output", html_path.name],
        cwd=run_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not html_path.exists():
        return qmd_path, None
    return qmd_path, html_path

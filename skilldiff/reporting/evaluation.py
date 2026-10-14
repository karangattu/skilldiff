import json
from pathlib import Path
from typing import Any, Optional

from skilldiff.reporting.checks import _format_checks_passed
from skilldiff.reporting.metrics import (
    _api_equivalent_summary,
    _fmt_int_delta,
    _fmt_int_opt,
    _fmt_money,
    _fmt_money_diff,
    _fmt_time_opt,
    _fmt_tokens,
    _paired_agent_runs,
    _paired_comparison_metrics,
    _paired_metric_runs,
    _score_percent,
    _total_tokens_opt,
    calculate_metrics,
    decision_cost_runs,
    format_pp_diff,
    format_time_diff,
)
from skilldiff.reporting.table import _md_table
from skilldiff.reporting.types import Cell
from skilldiff.scope import is_blast_violation
from skilldiff.stats import analysis_run, usable_agent_run

_EVALUATION_HEADERS = [
    "App", "Arm", "Score", "Time", "Input", "Cached input", "Output",
    "Total tokens", "Tool calls", "Turns", "Skill loaded", "API-equivalent cost",
]
_EVALUATION_ALIGN = ["l", "l", "r", "r", "r", "r", "r", "r", "r", "r", "l", "r"]
_EVALUATION_NOTE = (
    "App is the task ID (with the model when multiple models were evaluated). "
    "Control and skill scores use pairs graded on both sides. Time, tokens, and cost "
    "use matched eligible pairs with those measurements; tool calls and turns use matched "
    "eligible agent runs. The Δ row is the second arm minus the first over those same "
    "pairs. Excluded and unpaired attempts remain in Run details. "
    "Cached input includes cache reads and cache writes. Skill loaded covers all "
    "eligible runs, including unpaired runs. A lone arm and the optional baseline "
    "show descriptive totals. Tool calls and turns are counted by each harness "
    "differently, so compare them within a run, not across harnesses. "
    "N/A means a measurement or recorded pricing is missing."
)


def _apply_codex_cache_write_fallback(
    runs: dict[str, list[dict[str, Any]]], results: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    if results.get("harness") != "codex":
        return {arm: decision_cost_runs(records, results) for arm, records in runs.items()}
    normalized = {arm: [dict(run) for run in records] for arm, records in runs.items()}
    for records in normalized.values():
        for run in records:
            if (
                run.get("cache_creation_tokens") is None
                and run.get("input_tokens") is not None
                and run.get("output_tokens") is not None
            ):
                run["cache_creation_tokens"] = 0
    return {arm: decision_cost_runs(records, results) for arm, records in normalized.items()}


def _load_runs_for_report(
    results: dict[str, Any], run_root: Path | None = None
) -> dict[str, list[dict[str, Any]]]:
    if "runs" in results and isinstance(results["runs"], dict):
        c = results["runs"].get("control", [])
        t = results["runs"].get("treatment", [])
        if c or t:
            loaded = {"control": list(c), "treatment": list(t)}
            return _apply_codex_cache_write_fallback(loaded, results)

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
            return _apply_codex_cache_write_fallback(
                {"control": c_runs, "treatment": t_runs}, results
            )

    return {"control": [], "treatment": []}


def _run_status(run: dict[str, Any]) -> Cell:
    status = str(run.get("status") or "ok")
    return ("ok", None) if status == "ok" else (status, "bad")


def _is_blast_run(run: dict[str, Any]) -> bool:
    return run.get("grade_error_kind") == "blast_radius" or is_blast_violation(
        run.get("feedback")
    )


def _score_cell(run: dict[str, Any]) -> str:
    if not usable_agent_run(run):
        return "N/A (agent failure)"
    if run.get("failure_scored_zero"):
        return "0% (failure policy)"
    gs = run.get("grade_status")
    if gs in ("ungraded", "timeout", "error"):
        label = {"ungraded": "ungraded", "timeout": "grader timeout", "error": "grader error"}[gs]
        if _is_blast_run(run):
            label = "blast radius"
        return f"N/A ({label})"
    if "score" not in run or run.get("score") is None:
        return "N/A"
    try:
        return f"{round(float(run['score']) * 100)}%"
    except (TypeError, ValueError):
        return "N/A"


def _cost_cell(run: dict[str, Any]) -> str:
    if "cost" not in run or run.get("cost") is None:
        return "N/A"
    try:
        return f"${float(run['cost']):.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _time_cell(run: dict[str, Any]) -> str:
    if "duration" not in run or run.get("duration") is None:
        return "N/A"
    try:
        return f"{float(run['duration']):.0f}s"
    except (TypeError, ValueError):
        return "N/A"


def _turns_cell(run: dict[str, Any]) -> str:
    if "num_turns" not in run or run.get("num_turns") is None:
        return "N/A"
    try:
        return str(int(run["num_turns"]))
    except (TypeError, ValueError):
        return "N/A"


def _tokens_cell(run: dict[str, Any]) -> str:
    val = _total_tokens_opt(run)
    return _fmt_tokens(val) if val is not None else "N/A"


def _skill_cell(run: dict[str, Any], preset: str | None = None) -> Cell:
    invoked = run.get("skill_invoked")
    if invoked is None:
        return "?"
    if run.get("arm") == "control" and preset not in {"revision", "compression"}:
        return ("yes", "bad") if invoked else "no"
    return "yes" if invoked else ("no", "bad")


def _evaluation_delta_row(
    app: str, label: str, first: dict[str, Any], second: dict[str, Any], currency: str
) -> list[Cell]:
    def diff(key: str) -> Optional[float]:
        a, b = first[key], second[key]
        return None if a is None or b is None else b - a

    score_diff = (
        None if first["score"] is None or second["score"] is None
        else second["score"] - first["score"]
    )
    cells: list[Cell] = [app, label, "N/A" if score_diff is None else format_pp_diff(score_diff)]
    time_diff = diff("time")
    cells.append("N/A" if time_diff is None else format_time_diff(time_diff))
    for key in ("input", "cached", "output", "tokens", "tool_calls", "turns"):
        value = diff(key)
        cells.append("N/A" if value is None else _fmt_int_delta(int(value)))
    cells.append("")
    cost_diff = diff("cost")
    cells.append(_fmt_money_diff(cost_diff, currency))
    return cells


def _evaluation_rows(
    results: dict[str, Any],
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
) -> list[list[Cell]]:
    policy = (results.get("failure_policy") or (results.get("settings") or {}).get(
        "failure_policy") or {}).get("agent_failure", "exclude")
    baseline_runs = [
        analysis_run(r, policy) for r in ((results.get("runs") or {}).get("baseline") or [])
    ]
    arms = {"control": control_runs, "treatment": treatment_runs, "baseline": baseline_runs}
    keys = list(dict.fromkeys(
        (str(r.get("task_id", "")), str(r.get("model", "")))
        for runs in arms.values() for r in runs
    ))
    multi_model = len({model for _, model in keys}) > 1
    labels = results.get("arm_labels") or {}
    preset = results.get("preset") or (results.get("settings") or {}).get("preset")
    treatment_label = (
        "Minified" if preset == "compression" else
        "Skill B" if preset == "revision" or results.get("skill_comparison") else
        "Treatment" if results.get("comparison") else "Skill"
    )
    control_label = (
        "Original" if preset == "compression" else
        "Skill A" if preset == "revision" or results.get("skill_comparison") else
        "Control"
    )
    if labels.get("control"):
        control_label = str(labels["control"])
    if labels.get("treatment"):
        treatment_label = str(labels["treatment"])
    default_labels = {
        "control": control_label,
        "treatment": treatment_label,
        "baseline": "Baseline",
    }
    pricing = results.get("pricing") or {}
    currency = str(pricing.get("currency") or "USD")
    rows: list[list[Cell]] = []
    for task, model in keys:
        app = f"{task} ({model})" if multi_model else task
        grouped = {
            arm: [
                r for r in runs
                if (str(r.get("task_id", "")), str(r.get("model", ""))) == (task, model)
            ]
            for arm, runs in arms.items()
        }
        control_metrics, treatment_metrics = _paired_comparison_metrics(
            grouped["control"], grouped["treatment"]
        )
        paired_control, paired_treatment = _paired_agent_runs(
            grouped["control"], grouped["treatment"]
        )
        token_control, token_treatment = _paired_metric_runs(
            grouped["control"], grouped["treatment"], "token_breakdown"
        )
        values: dict[str, dict[str, Any]] = {}
        for arm in arms:
            group_all = grouped[arm]
            if not group_all:
                continue
            standalone = arm == "baseline" or not grouped[
                "treatment" if arm == "control" else "control"
            ]
            if arm == "control" and grouped["treatment"]:
                group, token_group, metrics = paired_control, token_control, control_metrics
            elif arm == "treatment" and grouped["control"]:
                group, token_group, metrics = paired_treatment, token_treatment, treatment_metrics
            else:
                group = [r for r in group_all if usable_agent_run(r)]
                token_group = group
                metrics = calculate_metrics(group)

            def total(field: str, measured: list[dict[str, Any]]) -> Optional[int]:
                if not measured or any(r.get(field) is None for r in measured):
                    return None
                return sum(int(r[field]) for r in measured)

            tokens_known = bool(token_group) and all(
                r.get("input_tokens") is not None
                and r.get("output_tokens") is not None
                and r.get("cache_read_tokens", 0) is not None
                and r.get("cache_creation_tokens", 0) is not None
                for r in token_group
            )
            token_metrics = calculate_metrics(token_group)
            api = _api_equivalent_summary(token_group, pricing)
            used, known = metrics["skill_used_count"], metrics["skill_known_count"]
            eligible_count = calculate_metrics(group_all)["total_count"]
            loaded = ("yes" if used else "no") if eligible_count == known == 1 else (
                f"{used}/{known}" if known else "N/A"
            )
            if known and known < eligible_count:
                loaded += f" ({eligible_count - known} unknown)"
            time_known = not standalone or metrics["time_known_count"] == len(group)
            total_known = bool(metrics["tokens_known_count"]) and (
                not standalone or metrics["tokens_known_count"] == len(group))
            v: dict[str, Any] = {
                "score": _score_percent(metrics),
                "time": metrics["total_duration"] if time_known else None,
                "input": total("input_tokens", token_group),
                "cached": (
                    token_metrics["total_cache_read_tokens"]
                    + token_metrics["total_cache_creation_tokens"]
                    if tokens_known else None
                ),
                "output": total("output_tokens", token_group),
                "tokens": metrics["total_tokens"] if total_known else None,
                "tool_calls": total("tool_calls", group),
                "turns": total("num_turns", group),
                "cost": (
                    api["cost"]
                    if tokens_known and api["priced_runs"] == len(token_group) else None
                ),
            }
            values[arm] = v
            rows.append([
                app,
                str(labels.get(arm) or default_labels[arm]),
                "N/A" if v["score"] is None else f"{v['score']}%",
                _fmt_time_opt(v["time"]),
                _fmt_int_opt(v["input"]), _fmt_int_opt(v["cached"]),
                _fmt_int_opt(v["output"]), _fmt_int_opt(v["tokens"]),
                _fmt_int_opt(v["tool_calls"]), _fmt_int_opt(v["turns"]),
                loaded,
                _fmt_money(v["cost"], currency),
            ])
            if arm == "treatment" and "control" in values:
                first_label = str(labels.get("control") or default_labels["control"])
                second_label = str(labels.get("treatment") or default_labels["treatment"])
                rows.append(_evaluation_delta_row(
                    app, f"Δ ({second_label} - {first_label})",
                    values["control"], v, currency,
                ))
    return rows


def render_evaluation_table(results: dict[str, Any], run_root: Path | None = None) -> str:
    runs = _load_runs_for_report(results, run_root)
    policy = (results.get("failure_policy") or (results.get("settings") or {}).get(
        "failure_policy") or {}).get("agent_failure", "exclude")
    runs = {arm: [analysis_run(r, policy) for r in records] for arm, records in runs.items()}
    rows = _evaluation_rows(results, runs["control"], runs["treatment"])
    if not rows:
        return ""
    return "\n".join(_md_table(_EVALUATION_HEADERS, rows, _EVALUATION_ALIGN))


def _build_runs_table(
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
    multi_model: bool = False,
    link_artifacts: bool = True,
    treatment_label: str = "Skill",
    control_label: str = "Control",
    preset: str | None = None,
) -> tuple[list[str], list[list[Cell]]]:
    headers = [
        "Task",
        "Run",
        "Arm",
        "Status",
        "Score",
        "Checks",
        "Skill used",
        "Cost",
        "Time",
        "Turns",
        "Tokens",
        "Files",
    ]
    if multi_model:
        headers.insert(0, "Model")
    if link_artifacts:
        headers.append("Artifacts")
    has_sessions = any(r.get("session_id") for r in control_runs + treatment_runs)
    if has_sessions:
        headers.append("Session")

    def key(r: dict[str, Any]) -> tuple[str, str, int]:
        return (str(r.get("model", "")), str(r.get("task_id", "")), int(r.get("repetition", 1)))

    ctrl_map = {key(r): r for r in control_runs}
    treat_map = {key(r): r for r in treatment_runs}
    ordered_keys = list(dict.fromkeys([key(r) for r in control_runs + treatment_runs]))

    rows: list[list[Cell]] = []
    for k in ordered_keys:
        arm_pairs = ((control_label, ctrl_map.get(k)), (treatment_label, treat_map.get(k)))
        for arm_label, run in arm_pairs:
            if run is None:
                continue
            row: list[Cell] = [
                str(run.get("task_id", "")),
                str(run.get("repetition", 1)),
                arm_label,
                _run_status(run),
                _score_cell(run),
                _format_checks_passed(run),
                _skill_cell(run, preset=preset),
                _cost_cell(run),
                _time_cell(run),
                _turns_cell(run),
                _tokens_cell(run),
                str(len(run.get("files_changed") or [])),
            ]
            if multi_model:
                row.insert(0, str(run.get("model", "")))
            if link_artifacts:
                art = run.get("artifacts")
                row.append(
                    f"[transcript]({art}/transcript.txt) · [diff]({art}/diff.patch)" if art else ""
                )
            if has_sessions:
                row.append(str(run.get("session_id") or "N/A"))
            rows.append(row)
    if treatment_label != "Skill" and preset not in {"revision", "compression"}:
        index = headers.index("Skill used")
        headers.pop(index)
        for row in rows:
            row.pop(index)
    return headers, rows

"""One table across saved runs: the same skill on several harnesses, models, or revisions.

`skilldiff compare` pairs two runs of one harness. This module lines up any number
of finished runs side by side so a skill evaluated on Claude Code, Codex, AGY, and
OpenCode can be read at a glance. Each row keeps its own run's decision; the
table never pools runs, because tool calls, turns, and cost bases differ per
harness. Scores and deltas are comparable only when the runs share task inputs,
which the provenance warnings call out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from skilldiff.compare import _arm_runs, load_results
from skilldiff.reporting.verdict import build_decision_context


def _pct(value: Any) -> int | None:
    return None if value is None else round(float(value) * 100)


def _mean(metrics: dict[str, Any], total_key: str, count_key: str) -> float | None:
    total, count = metrics.get(total_key), metrics.get(count_key)
    if total is None or not count:
        return None
    return float(total) / float(count)


def summarize_run(results: dict[str, Any], run_root: Path) -> dict[str, Any]:
    overall = results.get("overall") or {}
    control = overall.get("control") or {}
    skill = overall.get("skill") or {}
    paired = overall.get("paired") or {}
    score = paired.get("score") or {}
    labels = results.get("arm_labels") or {}
    treat_label = labels.get("treatment") or (
        "Skill B" if results.get("skill_comparison") else "Skill"
    )
    try:
        decision = build_decision_context(
            results,
            _arm_runs(results, "control"),
            _arm_runs(results, "treatment"),
            run_root=run_root,
            treatment_label=treat_label,
        )
        _, rec_label, rec_reason = decision["recommendation"]
    except Exception as exc:  # results saved by an older version may lack fields
        rec_label, rec_reason = "N/A", f"decision unavailable: {exc}"
    provenance = results.get("provenance") or {}
    agent_cli = provenance.get("agent_cli")
    if isinstance(agent_cli, dict):
        # Recorded per harness, e.g. {"codex": "codex-cli 0.160.1"}.
        agent_cli = agent_cli.get(results.get("harness")) or ", ".join(
            str(v) for v in agent_cli.values()
        )
    ci_low, ci_high = score.get("ci_low"), score.get("ci_high")
    return {
        "run_dir": str(run_root),
        "run": Path(run_root).name,
        "name": results.get("name"),
        "harness": results.get("harness"),
        "agent_cli": agent_cli,
        "models": list(results.get("models") or []),
        "skilldiff_version": results.get("skilldiff_version"),
        "tasks_hash": provenance.get("tasks_hash"),
        "skill_hash": provenance.get("skill_hash"),
        "cost_basis": results.get("cost_basis"),
        "pairs": paired.get("pairs"),
        "control_score_pct": _pct(control.get("task_score")),
        "skill_score_pct": _pct(skill.get("task_score")),
        "delta_pp": None if score.get("mean_diff") is None else float(score["mean_diff"]) * 100,
        "delta_ci_pp": (
            [float(ci_low) * 100, float(ci_high) * 100]
            if ci_low is not None and ci_high is not None
            else None
        ),
        "adoption_used": skill.get("skill_used_count"),
        "adoption_known": skill.get("skill_known_count"),
        "skill_cost_mean": _mean(skill, "total_cost", "cost_known_count"),
        "skill_time_mean": _mean(skill, "total_duration", "time_known_count"),
        "skill_tokens_mean": _mean(skill, "total_tokens", "tokens_known_count"),
        "valid": results.get("valid"),
        "adoption_failed": results.get("adoption_failed"),
        "decision": rec_label,
        "reason": rec_reason,
    }


def summarize_runs(paths: list[Path]) -> dict[str, Any]:
    rows = []
    for path in paths:
        results, run_root = load_results(Path(path))
        rows.append(summarize_run(results, run_root))
    warnings: list[str] = []
    if len(rows) > 1:
        if len({row["tasks_hash"] for row in rows}) > 1:
            warnings.append(
                "Task inputs differ between runs (tasks_hash), so scores and deltas are "
                "not comparable across all rows."
            )
        if len({row["skill_hash"] for row in rows}) > 1:
            warnings.append("Skill revisions differ between runs (skill_hash).")
        if len({row["harness"] for row in rows}) > 1:
            warnings.append(
                "Runs span several harnesses: each row keeps its own decision, and tool "
                "calls, turns, and cost bases are not comparable across harnesses."
            )
        if len({row["cost_basis"] for row in rows}) > 1:
            warnings.append("Cost bases differ between runs, so costs mean different things.")
        if len({row["skilldiff_version"] for row in rows}) > 1:
            warnings.append("skilldiff versions differ between runs.")
    return {"runs": rows, "warnings": warnings}


def _fmt(value: Any, spec: str = "", suffix: str = "") -> str:
    if value is None:
        return "N/A"
    return f"{value:{spec}}{suffix}"


def format_summary(summary: dict[str, Any]) -> str:
    rows = summary["runs"]
    if not rows:
        return "No finished runs to summarize."
    header = ["Run", "Harness", "CLI", "Model", "Control", "Skill", "Δ (pp)", "Adoption",
              "Cost", "Time (s)", "Decision"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for row in rows:
        delta = _fmt(row["delta_pp"], "+.0f")
        if row["delta_ci_pp"] is not None:
            delta += f" [{row['delta_ci_pp'][0]:+.0f}, {row['delta_ci_pp'][1]:+.0f}]"
        adoption = (
            f"{row['adoption_used']}/{row['adoption_known']}"
            if row["adoption_known"] else "N/A"
        )
        decision = row["decision"]
        if row.get("valid") is False:
            decision += " (INVALID)"
        lines.append("| " + " | ".join([
            row["run"],
            str(row["harness"] or "N/A"),
            str(row["agent_cli"] or "N/A"),
            ", ".join(row["models"]) or "N/A",
            _fmt(row["control_score_pct"], "", "%"),
            _fmt(row["skill_score_pct"], "", "%"),
            delta,
            adoption,
            _fmt(row["skill_cost_mean"], ".2f"),
            _fmt(row["skill_time_mean"], ".0f"),
            decision,
        ]) + " |")
    lines.append("")
    lines.append(
        "Control and Skill are mean task scores; Δ is the paired skill-minus-control "
        "difference with its 95% interval; Cost and Time are per-run means of the skill arm. "
        "Each decision comes from its own run."
    )
    if summary["warnings"]:
        lines.append("")
        lines.append("Caveats:")
        lines.extend(f"- {w}" for w in summary["warnings"])
    return "\n".join(lines)

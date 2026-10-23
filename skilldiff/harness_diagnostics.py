"""Shared harness-failure summaries for reports and diagnose."""

from __future__ import annotations

from collections import Counter
from typing import Any


def split_denial(text: str) -> tuple[str, str | None]:
    """Split ``Bash (reason)`` into its tool and reason when the reason is present."""
    value = str(text or "")
    if " (" in value and value.endswith(")"):
        tool, reason = value.split(" (", 1)
        return tool.strip(), reason[:-1].strip() or None
    return value.strip(), None


def summarize_harness_diagnostics(
    runs_by_arm: dict[str, list[dict[str, Any]]],
    retry_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    denials: dict[str, dict[str, Any]] = {}
    model_mismatches: list[dict[str, Any]] = []
    unrecognized: list[dict[str, Any]] = []
    for arm, runs in runs_by_arm.items():
        arm_tools: Counter[str] = Counter()
        arm_reasons: Counter[str] = Counter()
        sessions = 0
        for run in runs or []:
            records = run.get("permission_denials")
            if isinstance(records, list) and records:
                sessions += 1
                for record in records:
                    tool, reason = split_denial(str(record))
                    arm_tools[tool or "unknown"] += 1
                    if reason:
                        arm_reasons[reason] += 1
            served = run.get("served_models")
            configured = str(run.get("model") or "")
            if (
                isinstance(served, list)
                and served
                and configured
                and configured not in {str(model) for model in served}
            ):
                model_mismatches.append(
                    {
                        "arm": arm,
                        "task_id": str(run.get("task_id") or ""),
                        "repetition": run.get("repetition", 1),
                        "configured": configured,
                        "served": sorted({str(model) for model in served}),
                    }
                )
            flagged = run.get("unrecognized_models")
            if isinstance(flagged, list) and flagged:
                unrecognized.append(
                    {
                        "arm": arm,
                        "task_id": str(run.get("task_id") or ""),
                        "repetition": run.get("repetition", 1),
                        "models": sorted({str(model) for model in flagged}),
                    }
                )
        if sessions:
            denials[arm] = {
                "sessions": sessions,
                "tools": dict(arm_tools.most_common()),
                "reasons": dict(arm_reasons.most_common()),
            }

    retries = [
        entry
        for entry in (retry_history or [])
        if isinstance(entry, dict)
        and (
            entry.get("status") == "rate_limit"
            or entry.get("failure_kind") == "rate_limit"
        )
    ]
    retry_cost = 0.0
    retry_cost_known = False
    for entry in retries:
        try:
            if entry.get("cost") is not None:
                retry_cost += float(entry["cost"])
                retry_cost_known = True
        except (TypeError, ValueError):
            pass
    retry_summary = {
        "count": len(retries),
        "by_arm": dict(Counter(str(entry.get("arm") or "unknown") for entry in retries)),
        "by_model": dict(Counter(str(entry.get("model") or "unknown") for entry in retries)),
        "cost": retry_cost if retry_cost_known else None,
    }
    return {
        "denials": denials,
        "model_mismatches": model_mismatches,
        "unrecognized": unrecognized,
        "retries": retry_summary,
        "has_issues": bool(
            denials or model_mismatches or unrecognized or retry_summary["count"]
        ),
    }


def denial_sentence(arm: str, data: dict[str, Any]) -> str:
    tools = ", ".join(f"{tool} ×{count}" for tool, count in (data.get("tools") or {}).items())
    reasons = "; ".join(
        f"{reason} ×{count}" for reason, count in list((data.get("reasons") or {}).items())[:3]
    )
    text = f"{arm}: {data.get('sessions', 0)} session(s) ({tools or 'tools unknown'})"
    if reasons:
        text += f"; reasons: {reasons}"
    return text

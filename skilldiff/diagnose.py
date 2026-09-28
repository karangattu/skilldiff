import json
from pathlib import Path
from typing import Any


def diagnose_run(run_dir: Path) -> dict[str, Any]:
    path = Path(run_dir).resolve()
    if path.is_dir():
        results_file = path / "results.json"
    else:
        results_file = path

    if not results_file.is_file():
        raise FileNotFoundError(f"results.json not found in {run_dir}")

    data = json.loads(results_file.read_text(encoding="utf-8"))
    raw_runs = data.get("runs") or {}
    if isinstance(raw_runs, list):
        control_runs = [r for r in raw_runs if r.get("arm") == "control"]
        treatment_runs = [r for r in raw_runs if r.get("arm") in {"treatment", "skill"}]
    elif isinstance(raw_runs, dict):
        control_runs = raw_runs.get("control") or []
        treatment_runs = raw_runs.get("treatment") or raw_runs.get("skill") or []
    else:
        control_runs = []
        treatment_runs = []
    task_details = {str(t.get("id")): t for t in (data.get("task_details") or [])}

    under_triggered = []
    over_triggered = []
    regressions = []
    blast_violations = []
    agent_failures = []
    token_bloat_tasks = []
    recommendations = []

    ctrl_by_key = {
        (r.get("model"), r.get("task_id"), r.get("repetition")): r for r in control_runs
    }

    for t_run in treatment_runs:
        task_id = str(t_run.get("task_id", ""))
        model = str(t_run.get("model", ""))
        rep = t_run.get("repetition")
        key = (model, task_id, rep)
        c_run = ctrl_by_key.get(key, {})

        meta = task_details.get(task_id, {})
        category = meta.get("category", t_run.get("task_category", "general"))

        invoked = t_run.get("skill_invoked")
        if category == "intended" and invoked is False:
            under_triggered.append({"task_id": task_id, "model": model, "rep": rep})

        if category == "irrelevant" and invoked is True:
            over_triggered.append({"task_id": task_id, "model": model, "rep": rep})

        c_score = c_run.get("score")
        t_score = t_run.get("score")
        if c_score is not None and t_score is not None and float(t_score) < float(c_score):
            regressions.append(
                {
                    "task_id": task_id,
                    "model": model,
                    "rep": rep,
                    "control_score": c_score,
                    "skill_score": t_score,
                    "feedback": t_run.get("feedback"),
                }
            )

        feedback = str(t_run.get("feedback") or "")
        if "Blast radius violation" in feedback:
            blast_violations.append({"task_id": task_id, "model": model, "violation": feedback})

        if t_run.get("status") in {"error", "timeout"} or (
            t_run.get("exit_code") not in (0, None)
        ):
            agent_failures.append(
                {
                    "task_id": task_id,
                    "model": model,
                    "status": t_run.get("status"),
                    "error": t_run.get("error"),
                }
            )

        c_tok = sum(
            int(c_run.get(k) or 0)
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
            )
        )
        t_tok = sum(
            int(t_run.get(k) or 0)
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
            )
        )
        if c_tok > 0 and t_tok > c_tok * 1.5 and (t_score or 0) <= (c_score or 0):
            token_bloat_tasks.append(
                {
                    "task_id": task_id,
                    "control_tokens": c_tok,
                    "skill_tokens": t_tok,
                    "ratio": round(t_tok / c_tok, 2),
                }
            )

    if under_triggered:
        tasks_list = sorted({item["task_id"] for item in under_triggered})
        recommendations.append(
            f"Adoption: skill failed to trigger on {len(under_triggered)} intended task run(s) "
            f"({', '.join(tasks_list)}). Refine SKILL.md description with explicit trigger terms."
        )

    if over_triggered:
        tasks_list = sorted({item["task_id"] for item in over_triggered})
        recommendations.append(
            f"Over-triggering: skill triggered on {len(over_triggered)} irrelevant task run(s) "
            f"({', '.join(tasks_list)}). Add scoping boundaries in the SKILL.md description."
        )

    if blast_violations:
        tasks_list = sorted({item["task_id"] for item in blast_violations})
        recommendations.append(
            f"Blast radius: agent modified out-of-scope files on {', '.join(tasks_list)}. "
            "Add explicit path instructions in the skill body to keep edits confined."
        )

    if regressions:
        tasks_list = sorted({item["task_id"] for item in regressions})
        recommendations.append(
            f"Regressions: skill arm scored lower than control on {len(regressions)} run(s) "
            f"across tasks: {', '.join(tasks_list)}. Check run diffs and feedback."
        )

    if token_bloat_tasks:
        tasks_list = sorted({item["task_id"] for item in token_bloat_tasks})
        recommendations.append(
            f"Efficiency: token consumption rose >50% on {', '.join(tasks_list)} without "
            "score gains. Consider compressing SKILL.md instructions."
        )

    if not recommendations:
        recommendations.append(
            "Clean run: no adoption failures, regressions, blast violations, or bloat detected."
        )

    return {
        "run_dir": str(path),
        "total_pairs": len(treatment_runs),
        "under_triggered": under_triggered,
        "over_triggered": over_triggered,
        "regressions": regressions,
        "blast_violations": blast_violations,
        "agent_failures": agent_failures,
        "token_bloat_tasks": token_bloat_tasks,
        "recommendations": recommendations,
    }

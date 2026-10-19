"""Skill adoption accounting shared by results, reports, and diagnosis.

A *hit* is an eligible run in which the agent loaded the target skill or read its
files at least once. A *miss* is an eligible run with confirmed no use. Unknown
runs have no measurement and are never folded into the hit rate as misses.
"""

from __future__ import annotations

import math
from typing import Any

from skilldiff.stats import METRICS, analysis_run, pair_runs

_SKILL_PRESETS = {"revision", "compression"}
_SKILL_ARM_ORDER = ("control", "treatment", "baseline")
_DEFAULT_LABELS = {
    "control": "Control",
    "treatment": "Skill",
    "baseline": "Baseline",
}
_ADOPTION_FAILURE_RATE = 2.0 / 3.0


def adoption_eligible(run: dict[str, Any]) -> bool:
    """Whether a run could have used a skill and contributes to the hit rate."""
    return (
        run.get("complete") is not False
        and run.get("status") in (None, "ok", "correctness")
        and run.get("failure_kind") != "harness_error"
    )


def _task_details(results: dict[str, Any]) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    for item in results.get("task_details") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        prompt = item.get("prompt")
        if not prompt and isinstance(item.get("prompts"), list):
            prompt = item["prompts"][0] if item["prompts"] else None
        details[str(item["id"])] = {
            "category": str(item.get("category") or "general"),
            "split": str(item.get("split") or "dev"),
            "prompt": str(prompt) if prompt else "",
        }
    return details


def _run_task(run: dict[str, Any]) -> str:
    return str(run.get("task_id", ""))


def _run_category(run: dict[str, Any], details: dict[str, dict[str, Any]]) -> str:
    task = _run_task(run)
    return str(
        run.get("task_category")
        or (details.get(task) or {}).get("category")
        or "general"
    )


def _run_split(run: dict[str, Any], details: dict[str, dict[str, Any]]) -> str:
    task = _run_task(run)
    return str(
        run.get("task_split")
        or (details.get(task) or {}).get("split")
        or "dev"
    )


def _arm_has_skill_measurement(runs: list[dict[str, Any]]) -> bool:
    return any(adoption_eligible(run) and run.get("skill_invoked") is not None for run in runs)


def skill_arm_names(
    results: dict[str, Any], runs_by_arm: dict[str, list[dict[str, Any]]]
) -> list[str]:
    """Return arms that actually carry a skill in this experiment."""
    preset = str(
        results.get("preset")
        or (results.get("settings") or {}).get("preset")
        or ""
    )
    skill_comparison = results.get("skill_comparison") or {}
    is_ab = bool(skill_comparison) or preset in _SKILL_PRESETS
    if results.get("comparison") and not is_ab:
        # PR mode may install skills touched by the diff in either arm.
        return [
            arm
            for arm in ("control", "treatment")
            if _arm_has_skill_measurement(runs_by_arm.get(arm) or [])
        ]
    if is_ab:
        return ["control", "treatment"]
    return ["treatment"] if runs_by_arm.get("treatment") else []


def _empty_task() -> dict[str, Any]:
    return {"hits": 0, "known": 0, "unknown": 0, "loads": 0, "loads_unknown": 0}


def _add_task(bucket: dict[str, dict[str, Any]], key: str, run: dict[str, Any]) -> None:
    entry = bucket.setdefault(key, _empty_task())
    invoked = run.get("skill_invoked")
    if invoked is None:
        entry["unknown"] += 1
        return
    entry["known"] += 1
    if invoked:
        entry["hits"] += 1
        value = run.get("skill_invocations")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            entry["loads"] += value
        else:
            entry["loads_unknown"] += 1


def _reading(hits: int, known: int) -> str:
    if not known:
        return "Unknown"
    if hits <= 0:
        return "Not used"
    if hits >= known:
        return "Full adoption"
    return "Partial adoption"


def _prompt_snippet(text: str, limit: int = 180) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _default_arm_label(arm: str, preset: str) -> str:
    if preset == "revision":
        return "Skill A" if arm == "control" else "Skill B" if arm == "treatment" else "Baseline"
    if preset == "compression":
        return "Original" if arm == "control" else "Minified" if arm == "treatment" else "Baseline"
    if preset == "pr":
        return "With PR" if arm == "treatment" else "Without PR" if arm == "control" else "Baseline"
    return _DEFAULT_LABELS.get(arm, arm)


def adoption_summary(
    results: dict[str, Any], runs_by_arm: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Per-arm hits, misses, unknowns, load events, and trigger failures."""
    details = _task_details(results)
    labels = results.get("arm_labels") or {}
    preset = str(
        results.get("preset")
        or (results.get("settings") or {}).get("preset")
        or ""
    )
    arms: dict[str, dict[str, Any]] = {}
    for arm in skill_arm_names(results, runs_by_arm):
        runs = runs_by_arm.get(arm) or []
        eligible = [run for run in runs if adoption_eligible(run)]
        known = [run for run in eligible if run.get("skill_invoked") is not None]
        hits = [run for run in known if run.get("skill_invoked")]
        unknown = len(eligible) - len(known)
        loads = 0
        loads_unknown = 0
        for run in hits:
            value = run.get("skill_invocations")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                loads += value
            else:
                loads_unknown += 1
        by_task: dict[str, dict[str, Any]] = {}
        by_category: dict[str, dict[str, Any]] = {}
        by_split: dict[str, dict[str, Any]] = {}
        category_by_task: dict[str, str] = {}
        untriggered: list[dict[str, Any]] = []
        over_triggered: list[dict[str, Any]] = []
        for run in known:
            task = _run_task(run)
            category = _run_category(run, details)
            category_by_task[task] = category
            split = _run_split(run, details)
            _add_task(by_task, task, run)
            _add_task(by_category, category, run)
            _add_task(by_split, split, run)
            identity = {
                "arm": arm,
                "task_id": task,
                "model": str(run.get("model", "")),
                "rep": run.get("repetition", 1),
            }
            if category == "intended" and not run.get("skill_invoked"):
                untriggered.append(identity)
            if category == "irrelevant" and run.get("skill_invoked"):
                over_triggered.append(identity)
        for entry in (*by_task.values(), *by_category.values(), *by_split.values()):
            entry["hit_rate"] = (
                entry["hits"] / entry["known"] if entry["known"] else None
            )
        partial_tasks = sorted(
            task
            for task, entry in by_task.items()
            if category_by_task.get(task, "general") == "intended"
            and 0 < entry["hits"] < entry["known"]
        )
        untriggered_tasks = sorted(
            task
            for task, entry in by_task.items()
            if category_by_task.get(task, "general") == "intended"
            and entry["known"] > 0
            and entry["hits"] == 0
        )
        over_triggered_tasks = sorted({item["task_id"] for item in over_triggered})
        unavailable = sum(
            1 for run in eligible if run.get("skill_available") is False
        )
        hit_rate = len(hits) / len(known) if known else None
        arms[arm] = {
            "label": str(labels.get(arm) or _default_arm_label(arm, preset)),
            "skill": str(
                results.get("skill_a")
                if arm == "control"
                else results.get("skill_b") or results.get("skill") or ""
            ),
            "hits": len(hits),
            "known": len(known),
            "eligible": len(eligible),
            "unknown": unknown,
            "failed": len(runs) - len(eligible),
            "hit_rate": hit_rate,
            "loads": loads if not loads_unknown else None,
            "loads_unknown": loads_unknown,
            "reading": _reading(len(hits), len(known)),
            "unavailable_runs": unavailable,
            "untriggered": untriggered,
            "untriggered_tasks": untriggered_tasks,
            "partial_intended_tasks": partial_tasks,
            "over_triggered": over_triggered,
            "over_triggered_tasks": over_triggered_tasks,
            "by_task": by_task,
            "by_category": by_category,
            "by_split": by_split,
        }
    failed_arms = [
        arm
        for arm, data in arms.items()
        if data["known"] and data["hit_rate"] < _ADOPTION_FAILURE_RATE
    ]
    return {
        "arms": arms,
        "adoption_failed": bool(failed_arms),
        "adoption_failed_arms": failed_arms,
    }


def adoption_failure_detail(summary: dict[str, Any]) -> str:
    parts = []
    for arm in summary.get("adoption_failed_arms") or []:
        data = (summary.get("arms") or {}).get(arm) or {}
        known = int(data.get("known") or 0)
        hits = int(data.get("hits") or 0)
        if known:
            parts.append(
                f"{data.get('label') or arm} loaded in {hits}/{known} measured runs"
            )
    return "; ".join(parts)


def no_effect_findings(
    results: dict[str, Any], runs_by_arm: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Intended tasks where hit runs still did not beat their paired control."""
    preset = str(
        results.get("preset")
        or (results.get("settings") or {}).get("preset")
        or ""
    )
    if results.get("skill_comparison") or preset in _SKILL_PRESETS:
        return []
    control = runs_by_arm.get("control") or []
    treatment = runs_by_arm.get("treatment") or []
    if not control or not treatment:
        return []
    policy = (
        results.get("failure_policy")
        or (results.get("settings") or {}).get("failure_policy")
        or {}
    ).get("agent_failure", "exclude")
    details = _task_details(results)
    intended = {
        task
        for task, item in details.items()
        if item.get("category") == "intended"
    }
    if not intended:
        intended = {
            _run_task(run)
            for run in treatment
            if _run_category(run, details) == "intended"
        }
    findings: list[dict[str, Any]] = []
    for task in sorted(intended):
        hits = [
            run
            for run in treatment
            if _run_task(run) == task
            and adoption_eligible(run)
            and run.get("skill_invoked") is True
        ]
        if len(hits) < 2:
            continue
        ctrl = [run for run in control if _run_task(run) == task]
        pairs = pair_runs(
            [analysis_run(run, policy) for run in ctrl],
            [analysis_run(run, policy) for run in hits],
        )
        score_pairs = []
        for c_run, t_run in pairs:
            c_score = METRICS["score"](c_run)
            t_score = METRICS["score"](t_run)
            if c_score is not None and t_score is not None:
                score_pairs.append((float(c_score), float(t_score)))
        diffs = [t_score - c_score for c_score, t_score in score_pairs]
        if not diffs or not all(math.isfinite(value) for value in diffs):
            continue
        mean_diff = sum(diffs) / len(diffs)
        if mean_diff <= 1e-9:
            findings.append(
                {
                    "task_id": task,
                    "arm": "treatment",
                    "hit_runs": len(hits),
                    "pairs": len(diffs),
                    "control_score": sum(c for c, _ in score_pairs) / len(score_pairs),
                    "skill_score": sum(t for _, t in score_pairs) / len(score_pairs),
                    "mean_diff": mean_diff,
                }
            )
    return findings


def _skill_path(results: dict[str, Any], arm: str) -> str:
    path = (
        results.get("skill_a")
        if arm == "control"
        else results.get("skill_b") or results.get("skill")
    )
    return str(path) if path else "<skill>"


def _task_prompt(results: dict[str, Any], task: str) -> str:
    detail = _task_details(results).get(task) or {}
    return _prompt_snippet(str(detail.get("prompt") or ""))


def _format_tasks(tasks: list[str]) -> str:
    return ", ".join(f"`{task}`" for task in tasks)


def adoption_actions(
    results: dict[str, Any],
    summary: dict[str, Any],
    no_effect: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Concrete next steps for improving the hit rate, grouped by arm."""
    actions: list[dict[str, Any]] = []
    for arm, data in summary.get("arms", {}).items():
        label = data.get("label") or arm
        skill = _skill_path(results, arm)
        if data.get("unavailable_runs"):
            actions.append(
                {
                    "kind": "availability",
                    "arm": arm,
                    "tasks": [],
                    "advice": (
                        f"The harness did not list {label} in "
                        f"{data['unavailable_runs']} of {data['eligible']} eligible "
                        "run(s). Fix SKILL.md frontmatter and discovery "
                        f"(`skilldiff lint {skill}`, then `skilldiff check`) before "
                        "tuning trigger wording."
                    ),
                    "prompt_examples": [],
                }
            )
        if data.get("untriggered_tasks"):
            tasks = list(data["untriggered_tasks"])
            examples = [
                {"task": task, "prompt": _task_prompt(results, task)}
                for task in tasks
                if _task_prompt(results, task)
            ]
            prompt_text = ""
            if examples:
                quoted = "; ".join(
                    f"`{item['task']}` asks \"{item['prompt']}\""
                    for item in examples[:3]
                )
                prompt_text = f" Those tasks show the wording: {quoted}."
            actions.append(
                {
                    "kind": "description",
                    "arm": arm,
                    "tasks": tasks,
                    "advice": (
                        f"No intended run loaded {label} on {_format_tasks(tasks)}."
                        f"{prompt_text} Add those intents and nouns to the SKILL.md "
                        "description, or lead with an explicit \"Use when the user "
                        "asks to ...\" sentence if they are already there. Retest on "
                        f"dev with `skilldiff lint {skill}` and "
                        "`skilldiff run --split dev --runs 1`."
                    ),
                    "prompt_examples": examples,
                }
            )
        if data.get("partial_intended_tasks"):
            tasks = list(data["partial_intended_tasks"])
            counts = "; ".join(
                f"`{task}` {data['by_task'][task]['hits']}/{data['by_task'][task]['known']}"
                for task in tasks
            )
            examples = [
                {"task": task, "prompt": _task_prompt(results, task)}
                for task in tasks
                if _task_prompt(results, task)
            ]
            prompt_text = ""
            if examples:
                quoted = "; ".join(
                    f"`{item['task']}` asks \"{item['prompt']}\""
                    for item in examples[:3]
                )
                prompt_text = f" Task wording: {quoted}."
            actions.append(
                {
                    "kind": "description",
                    "arm": arm,
                    "tasks": tasks,
                    "advice": (
                        f"{label} loaded inconsistently on intended task(s): "
                        f"{counts}.{prompt_text} Partial triggering usually means the "
                        "description matches the topic but not how users phrase the "
                        "request. Add explicit \"Use when the user asks to ...\" phrases "
                        "for the missed wording, then retest on dev."
                    ),
                    "prompt_examples": examples,
                }
            )
        if data.get("over_triggered_tasks"):
            tasks = list(data["over_triggered_tasks"])
            actions.append(
                {
                    "kind": "scope",
                    "arm": arm,
                    "tasks": tasks,
                    "advice": (
                        f"Over-triggering: {label} loaded on irrelevant task(s) "
                        f"{_format_tasks(tasks)}. Narrow the description and name what "
                        "the skill should not handle so it stays out of the way."
                    ),
                    "prompt_examples": [
                        {"task": task, "prompt": _task_prompt(results, task)}
                        for task in tasks
                        if _task_prompt(results, task)
                    ],
                }
            )
        for finding in no_effect or []:
            if finding.get("arm") != arm:
                continue
            task = str(finding.get("task_id"))
            actions.append(
                {
                    "kind": "body",
                    "arm": arm,
                    "tasks": [task],
                    "advice": (
                        f"{label} loaded on `{task}` in {finding['hit_runs']} run(s) "
                        "but did not beat control there. Check whether the skill body "
                        "was actually applied; inspect a transcript before changing "
                        "only the description."
                    ),
                    "prompt_examples": [],
                }
            )
        if data.get("unknown"):
            actions.append(
                {
                    "kind": "coverage",
                    "arm": arm,
                    "tasks": [],
                    "advice": (
                        f"Skill use is unknown for {data['unknown']} eligible "
                        f"{label} run(s). Inspect those transcripts before concluding "
                        "that adoption is clean."
                    ),
                    "prompt_examples": [],
                }
            )
    if len(summary.get("arms", {})) > 1:
        failed = [
            data
            for data in summary["arms"].values()
            if data.get("known") and data.get("hit_rate") is not None
        ]
        rates = {data["label"]: data["hit_rate"] for data in failed}
        if len(rates) > 1 and max(rates.values()) - min(rates.values()) >= 0.2:
            actions.append(
                {
                    "kind": "confound",
                    "arm": "",
                    "tasks": [],
                    "advice": (
                        "The skill arms triggered at different rates ("
                        + ", ".join(
                            f"{label} {rate * 100:.0f}%"
                            for label, rate in sorted(rates.items())
                        )
                        + "). Alignment problems confound the body comparison; fix "
                        "the weaker description first, then rerun."
                    ),
                    "prompt_examples": [],
                }
            )
    if summary.get("adoption_failed_arms"):
        labels = [
            summary["arms"][arm].get("label", arm)
            for arm in summary["adoption_failed_arms"]
            if arm in summary["arms"]
        ]
        actions.append(
            {
                "kind": "verdict",
                "arm": ",".join(summary["adoption_failed_arms"]),
                "tasks": [],
                "advice": (
                    "Low adoption blocks a SHIP result: "
                    + ", ".join(labels)
                    + " loaded in fewer than two-thirds of measured runs. Fix the "
                    "trigger on dev tasks and rerun before deciding."
                ),
                "prompt_examples": [],
            }
        )
    return actions

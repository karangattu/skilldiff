from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from skilldiff import __version__
from skilldiff.adoption import adoption_eligible, adoption_summary, decision_runs
from skilldiff.config import ExperimentConfig, TaskConfig
from skilldiff.persistence import read_json
from skilldiff.profiler import compute_context_tax, measure_skill_footprint
from skilldiff.reporting.evaluation import _apply_codex_cache_write_fallback
from skilldiff.reporting.metrics import calculate_metrics
from skilldiff.stats import analysis_run, paired_comparison


def _arm_labels(config: ExperimentConfig, comparison: dict[str, Any] | None) -> dict[str, str]:
    preset = getattr(config, "preset", None) or (
        "pr" if comparison else ("revision" if config.is_skill_comparison else "skill")
    )
    if preset == "pr":
        mode = (comparison or {}).get("mode", "agent")
        if mode == "correctness":
            return {"control": "Base (untouched)", "treatment": "PR (untouched)"}
        return {"control": "Without PR", "treatment": "With PR"}
    if preset == "compression":
        return {"control": "Original", "treatment": "Minified"}
    if preset == "revision":
        return {"control": "Skill A", "treatment": "Skill B"}
    return {"control": "Control", "treatment": "Skill"}


def _run_summary(run: dict[str, Any]) -> str:
    if run.get("status") not in (None, "ok", "correctness"):
        return str(run["status"]).upper()
    if run.get("grade_status") in ("ungraded", "timeout", "error") or run.get("score") is None:
        label = {"ungraded": "ungraded", "timeout": "grader timeout", "error": "grader error"}.get(
            str(run.get("grade_status") or ""), "N/A"
        )
        dur = run.get("duration")
        dur_txt = f" in {float(dur):.0f}s" if dur is not None else ""
        prefix = "correctness " if run.get("status") == "correctness" else ""
        text = f"{prefix}{label}{dur_txt}"
    else:
        try:
            pct = round(float(run["score"]) * 100)
        except (TypeError, ValueError):
            pct = None
        dur = run.get("duration")
        dur_txt = f" in {float(dur):.0f}s" if dur is not None else ""
        text = f"{pct}%{dur_txt}" if pct is not None else f"N/A{dur_txt}"
    if run.get("arm") == "treatment" and run.get("skill_invoked") is False:
        text += " (skill unused)"
    return text


def _settings_summary(config: ExperimentConfig) -> dict[str, Any]:
    harness_cfg = asdict(getattr(config, config.harness, config.claude))
    harness_cfg.pop("bin_path", None)
    out: dict[str, Any] = {
        "harness": config.harness,
        "cost_basis": config.cost_basis,
        "preset": getattr(config, "preset", None),
        "timeout_seconds": config.timeout_seconds,
        "parallel": config.parallel,
        **{k: v for k, v in harness_cfg.items() if v not in (None, [], "")},
    }
    thresholds = dict(getattr(config, "thresholds", {}) or {})
    if thresholds:
        out["thresholds"] = thresholds
    failure_policy = dict(getattr(config, "failure_policy", {}) or {})
    if failure_policy:
        out["failure_policy"] = failure_policy
    if getattr(config, "seed", None) is not None:
        out["seed"] = config.seed
    if getattr(config, "is_skill_comparison", False):
        out["skill_a"] = str(config.skill_a) if config.skill_a else None
        out["skill_b"] = str(config.skill_b) if config.skill_b else None
        out["include_baseline"] = bool(config.include_baseline)
    if config.pr is not None:
        out["pr_mode"] = getattr(config.pr, "mode", "agent")
        out["pr_pair"] = getattr(config.pr, "pair", "merge-base")
    if getattr(config, "arm_parallel", False):
        out["arm_parallel"] = True
    if getattr(config, "reflink", False):
        out["reflink"] = True
    return out


def _run_warnings(
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
    tasks: list[TaskConfig],
    treatment_label: str = "skill",
    is_skill_comparison: bool = False,
    is_pr: bool = False,
    baseline_runs: list[dict[str, Any]] | None = None,
) -> list[str]:
    warnings: list[str] = []
    for arm_name, runs in (("control", control_runs), (treatment_label, treatment_runs)):
        failed = [
            r
            for r in runs
            if r.get("status") not in (None, "ok", "correctness")
        ]
        if failed:
            kinds = sorted({str(r.get("status")) for r in failed})
            fkinds = sorted(
                {str(r.get("failure_kind")) for r in failed if r.get("failure_kind")}
            )
            sample = next((r.get("error") for r in failed if r.get("error")), None)
            graded = sum(1 for r in failed if r.get("grade_status") == "graded")
            ungraded = len(failed) - graded
            what = (
                "harness errors (the agent CLI did not run)"
                if fkinds == ["harness_error"]
                else " or ".join(kinds)
            )
            msg = (
                f"{len(failed)} of {len(runs)} {arm_name} runs ended with "
                f"{what}, not a low score"
            )
            if graded:
                msg += f"; {graded} were still graded on partial work"
            if ungraded:
                msg += f"; {ungraded} have N/A scores (grading unavailable)"
            msg += "."
            if sample:
                msg += f" First error: {str(sample).strip().splitlines()[0][:200]}"
            if len(failed) == len(runs):
                msg += " Every run failed, so this arm has no usable evidence."
            warnings.append(msg)
        grade_failed = [r for r in runs if r.get("grade_status") in ("timeout", "error")]
        if grade_failed:
            kinds = sorted({str(r.get("grade_status")) for r in grade_failed})
            warnings.append(
                f"{len(grade_failed)} of {len(runs)} {arm_name} runs have grader "
                f"{' or '.join(kinds)}; their scores are N/A and excluded from means. "
                "These are evaluation-infra failures, shown with valid-pair counts."
            )
    denied_arms = []
    for arm_name, runs in (("control", control_runs), (treatment_label, treatment_runs),
                           ("baseline", baseline_runs or [])):
        denied = [r for r in runs if r.get("permission_denials")]
        if denied:
            tools = Counter(str(t) for r in denied for t in r["permission_denials"])
            named = ", ".join(f"{tool} ×{count}" for tool, count in tools.most_common())
            denied_arms.append(f"{len(denied)} of {len(runs)} {arm_name} runs ({named})")
    if denied_arms:
        warnings.append(
            "The harness denied tool calls in " + "; ".join(denied_arms) + ". Headless "
            "sessions cannot answer permission prompts, so those agents worked without the "
            "denied tools and scores reflect the permission setup as well as the skill. "
            "Keep the harness permission defaults (`claude.sandbox`, `codex.sandbox`, "
            "`dangerously_skip_permissions` for OpenCode and Antigravity), allow the needed "
            "network (`claude.allowed_domains`, `codex.network_access`), then rerun."
        )

    if is_skill_comparison:
        for arm_name, runs, expected in (
            ("control (skill A)", control_runs, "A"),
            ("treatment (skill B)", treatment_runs, "B"),
        ):
            wrong = [
                r
                for r in runs
                if r.get("skill_revision") and r.get("skill_revision") != expected
            ]
            if wrong:
                warnings.append(
                    f"{len(wrong)} {arm_name} run(s) carry the wrong skill revision."
                )
        missing_a = [
            r
            for r in control_runs
            if r.get("skill_available") is False and r.get("status") in (None, "ok")
        ]
        missing_b = [
            r
            for r in treatment_runs
            if r.get("skill_available") is False and r.get("status") in (None, "ok")
        ]
        if missing_a:
            warnings.append(
                f"The harness did not list skill A in {len(missing_a)} control run(s). "
                "Check the SKILL.md frontmatter."
            )
        if missing_b:
            warnings.append(
                f"The harness did not list skill B in {len(missing_b)} treatment run(s). "
                "Check the SKILL.md frontmatter."
            )
        for arm_name, runs in (
            ("skill A", control_runs),
            ("skill B", treatment_runs),
        ):
            known = [
                r
                for r in runs
                if adoption_eligible(r) and r.get("skill_invoked") is not None
            ]
            unused = [r for r in known if not r.get("skill_invoked")]
            if known and unused:
                warnings.append(
                    f"The agent did not use {arm_name} in {len(unused)} of {len(known)} "
                    "eligible runs. Those runs measure install cost, not following the "
                    "revision."
                )
    elif not is_pr:
        contaminated = [
            r for r in control_runs if r.get("skill_available") or r.get("skill_invoked")
        ]
        if contaminated:
            warnings.append(
                f"{len(contaminated)} control run(s) had access to the skill (installed at user "
                "or plugin level, or referenced in the transcript). The control arm is not a "
                "clean baseline."
            )
        unavailable = [r for r in treatment_runs if r.get("skill_available") is False]
        if unavailable:
            warnings.append(
                f"The harness did not list the skill in {len(unavailable)} skill run(s). Check the "
                "SKILL.md frontmatter."
            )
        known = [
            r
            for r in treatment_runs
            if adoption_eligible(r) and r.get("skill_invoked") is not None
        ]
        unused = [r for r in known if not r.get("skill_invoked")]
        if known and unused:
            warnings.append(
                f"The agent did not use the skill in {len(unused)} of {len(known)} "
                "eligible skill runs. Those runs measure the cost of having the skill "
                "installed, not of following it. Consider sharpening the skill's "
                "`description` so it triggers."
            )
    removed = [r for r in control_runs if r.get("removed_from_fixture")]
    if removed:
        warnings.append(
            "A fixture already contained the skill; skilldiff removed it from the control "
            "workspace."
        )
    ungraded = [t.id for t in tasks if not (t.grader and t.grader.command)]
    if ungraded:
        warnings.append(
            "No grader is configured for " + ", ".join(ungraded) + "; those runs score "
            "N/A (not 100%), so only cost and time are compared. Valid-pair counts shown."
        )
    return warnings


def aggregate_by_model(
    models: list[str],
    tasks: list[TaskConfig],
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    by_model: dict[str, dict[str, Any]] = {}
    for model in models:
        m_ctrl = [r for r in control_runs if r["model"] == model]
        m_treat = [r for r in treatment_runs if r["model"] == model]
        by_task: dict[str, dict[str, Any]] = {}
        for task in tasks:
            t_ctrl = [r for r in m_ctrl if r["task_id"] == task.id]
            t_treat = [r for r in m_treat if r["task_id"] == task.id]
            by_task[task.id] = {
                "control": calculate_metrics(t_ctrl),
                "skill": calculate_metrics(t_treat),
                "paired": paired_comparison(t_ctrl, t_treat),
            }
        by_model[model] = {
            "control": calculate_metrics(m_ctrl),
            "skill": calculate_metrics(m_treat),
            "paired": paired_comparison(m_ctrl, m_treat),
            "runs_count": len(m_ctrl),
            "by_task": by_task,
            "runs": {"control": m_ctrl, "treatment": m_treat},
        }
    return by_model


def aggregate_by_category(
    task_categories: dict[str, str],
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    by_category: dict[str, Any] = {}
    for cat in sorted(set(task_categories.values())):
        c_runs = [
            r
            for r in control_runs
            if r.get("task_category", task_categories.get(r.get("task_id"), "general")) == cat
        ]
        t_runs = [
            r
            for r in treatment_runs
            if r.get("task_category", task_categories.get(r.get("task_id"), "general")) == cat
        ]
        by_category[cat] = {
            "control": calculate_metrics(c_runs),
            "skill": calculate_metrics(t_runs),
            "paired": paired_comparison(c_runs, t_runs),
            "tasks": sorted({r.get("task_id", "") for r in c_runs + t_runs}),
        }
    return by_category


def aggregate_baseline_comparisons(
    baseline_runs: list[dict[str, Any]],
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not baseline_runs:
        return {}, {}

    def _paired_vs(
        base: list[dict[str, Any]], other: list[dict[str, Any]]
    ) -> dict[str, Any]:
        b_by_key = {
            (
                str(r.get("model", "")),
                str(r.get("task_id", "")),
                int(r.get("repetition", 0)),
            ): r
            for r in base
        }
        pairs = [
            (b_by_key[k], r)
            for r in other
            for k in [
                (
                    str(r.get("model", "")),
                    str(r.get("task_id", "")),
                    int(r.get("repetition", 0)),
                )
            ]
            if k in b_by_key
        ]
        ctrl_side = [c for c, _ in pairs]
        treat_side = [t for _, t in pairs]
        return {
            "pairs": len(pairs),
            "baseline": calculate_metrics(ctrl_side),
            "other": calculate_metrics(treat_side),
            "paired": paired_comparison(ctrl_side, treat_side),
        }

    baseline_comparisons = {
        "baseline_vs_a": _paired_vs(baseline_runs, control_runs),
        "baseline_vs_b": _paired_vs(baseline_runs, treatment_runs),
    }
    baseline_overall = {
        "baseline": calculate_metrics(baseline_runs),
    }
    return baseline_comparisons, baseline_overall


def check_contamination(
    control_runs: list[dict[str, Any]],
    baseline_runs: list[dict[str, Any]],
    comparison: dict[str, Any] | None,
    is_skill_comparison: bool,
) -> tuple[bool, list[str]]:
    valid = True
    warnings: list[str] = []
    contaminated = [
        r for r in control_runs if r.get("skill_available") or r.get("skill_invoked")
    ]
    if contaminated and not comparison and not is_skill_comparison:
        valid = False
        warnings.append(
            f"INVALID: {len(contaminated)} control run(s) had access to the skill. "
            "There is no clean baseline; do not ship on this result."
        )
    baseline_contam = [
        r for r in baseline_runs if r.get("skill_invoked") or r.get("skill_available")
    ]
    if baseline_contam:
        valid = False
        warnings.append(
            f"INVALID: {len(baseline_contam)} baseline run(s) loaded a skill revision. "
            "The no-skill baseline is contaminated."
        )
    return valid, warnings


def aggregate_results(
    config: ExperimentConfig,
    tasks: list[TaskConfig],
    run_root: Path,
    timestamp: str,
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
    warnings: list[str],
    interrupted: bool,
    provenance_snapshot: dict[str, Any] | None = None,
    comparison: dict[str, Any] | None = None,
    baseline_runs: list[dict[str, Any]] | None = None,
    retry_history: list[dict[str, Any]] | None = None,
    seed: int | None = None,
    execution_config: ExperimentConfig | None = None,
    skill_comparison_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    model_indices = {m: idx for idx, m in enumerate(config.models)}
    task_indices = {t.id: idx for idx, t in enumerate(tasks)}

    def sort_key(r: dict[str, Any]) -> tuple[int, int, int]:
        model_idx = model_indices.get(r.get("model", ""), 0)
        task_idx = task_indices.get(r.get("task_id", ""), 0)
        return (model_idx, task_idx, int(r.get("repetition", 0)))

    control_runs.sort(key=sort_key)
    treatment_runs.sort(key=sort_key)
    base_runs = sorted(list(baseline_runs or []), key=sort_key)
    raw_control_runs, raw_treatment_runs, raw_baseline_runs = (
        control_runs, treatment_runs, base_runs
    )
    agent_failure = (getattr(config, "failure_policy", {}) or {}).get(
        "agent_failure", "exclude"
    )
    normalized = {
        "control": raw_control_runs,
        "treatment": raw_treatment_runs,
        "baseline": raw_baseline_runs,
    }
    if config.cost_basis == "api-equivalent":
        normalized = _apply_codex_cache_write_fallback(
            {
                "control": raw_control_runs,
                "treatment": raw_treatment_runs,
                "baseline": raw_baseline_runs,
            },
            {
                "harness": config.harness,
                "cost_basis": config.cost_basis,
                "pricing": config.pricing,
            },
        )
    control_runs = [analysis_run(r, agent_failure) for r in normalized["control"]]
    treatment_runs = [analysis_run(r, agent_failure) for r in normalized["treatment"]]
    base_runs = [analysis_run(r, agent_failure) for r in normalized["baseline"]]

    by_model = aggregate_by_model(config.models, tasks, control_runs, treatment_runs)

    treat_label = (
        "treatment" if (comparison or config.is_skill_comparison) else "skill"
    )
    all_warnings = list(warnings)
    all_warnings.extend(
        _run_warnings(
            raw_control_runs,
            raw_treatment_runs,
            tasks,
            treatment_label=treat_label,
            is_skill_comparison=bool(config.is_skill_comparison),
            is_pr=bool(comparison),
            baseline_runs=raw_baseline_runs,
        )
    )
    if interrupted:
        all_warnings.append(
            f"The experiment was interrupted after {len(control_runs)} of "
            f"{len(config.models) * len(tasks) * config.runs} pairs."
        )

    provenance = provenance_snapshot
    if not provenance:
        try:
            from skilldiff.experiment import collect_provenance

            provenance = collect_provenance(config, tasks)
        except Exception:
            provenance = {}

    incomplete_runs = []
    for path in sorted(run_root.rglob("run.json")):
        record = read_json(path)
        if record.get("complete") is False:
            record.setdefault("artifacts", str(path.parent.relative_to(run_root)))
            incomplete_runs.append(record)

    task_categories = {t.id: getattr(t, "category", "general") for t in tasks}
    task_details = [
        {
            "id": t.id,
            "category": getattr(t, "category", "general"),
            "split": getattr(t, "split", "dev"),
            "repo": t.repo,
            "prompt": t.prompt or (t.prompts[0] if t.prompts else None),
            "prompts": list(t.prompts or []),
            "grader": t.grader.command if t.grader else None,
        }
        for t in tasks
    ]
    by_category = aggregate_by_category(task_categories, control_runs, treatment_runs)

    exec_cfg = execution_config or config
    skill_target = exec_cfg.skill or exec_cfg.skill_b
    footprint = measure_skill_footprint(skill_target)
    overall_skill_metrics = calculate_metrics(treatment_runs)
    context_tax = compute_context_tax(
        footprint,
        median_turns=overall_skill_metrics.get("median_turns", 1.0),
        run_count=len(treatment_runs),
    )

    valid, contam_warnings = check_contamination(
        control_runs, base_runs, comparison, bool(config.is_skill_comparison)
    )
    all_warnings.extend(contam_warnings)

    baseline_comparisons, baseline_overall = aggregate_baseline_comparisons(
        base_runs, control_runs, treatment_runs
    )

    non_discriminating_tasks: list[str] = []
    for t in tasks:
        t_scores = [
            float(r["score"])
            for r in control_runs
            if r.get("task_id") == t.id
            and r.get("score") is not None
            and r.get("grade_status") not in ("ungraded", "timeout", "error")
        ]
        if t_scores and all(s >= 1.0 for s in t_scores):
            non_discriminating_tasks.append(t.id)

    adoption_results = {
        "preset": getattr(config, "preset", None) or "skill",
        "skill": str(config.skill) if config.skill else None,
        "skill_a": str(config.skill_a) if config.skill_a else None,
        "skill_b": str(config.skill_b) if config.skill_b else None,
        "skill_comparison": skill_comparison_info,
        "comparison": comparison,
        "arm_labels": _arm_labels(config, comparison),
        "task_details": task_details,
    }
    runs_by_arm = {
        "control": raw_control_runs,
        "treatment": raw_treatment_runs,
        "baseline": raw_baseline_runs,
    }
    # Every run, for the report's descriptive table.
    adoption = adoption_summary(adoption_results, runs_by_arm, basis="all")
    # The runs the closing decision uses, for the shipping gate.
    decision_arm_runs, adoption_basis = decision_runs(adoption_results, runs_by_arm)
    decision_adoption = adoption_summary(
        adoption_results, decision_arm_runs, basis=adoption_basis
    )
    adoption_failed = bool(decision_adoption["adoption_failed"])

    return {
        "name": config.name,
        "skilldiff_version": __version__,
        "preset": getattr(config, "preset", None) or "skill",
        "arm_labels": _arm_labels(config, comparison),
        "harness": config.harness,
        "skill": str(config.skill) if config.skill else None,
        "skill_a": str(config.skill_a) if config.skill_a else None,
        "skill_b": str(config.skill_b) if config.skill_b else None,
        "skill_comparison": skill_comparison_info,
        "comparison": comparison,
        "skill_names": exec_cfg.skill_names,
        "timestamp": timestamp,
        "run_dir": str(run_root),
        "models": config.models,
        "tasks": [t.id for t in tasks],
        "task_categories": task_categories,
        "task_details": task_details,
        "tasks_count": len(tasks),
        "runs_per_arm": config.runs,
        "seed": seed,
        "failure_policy": dict(getattr(config, "failure_policy", {}) or {}),
        "valid": valid,
        "adoption_failed": adoption_failed,
        "adoption_failed_arms": decision_adoption["adoption_failed_arms"],
        "adoption_unmeasured_arms": decision_adoption["adoption_unmeasured_arms"],
        "adoption_basis": adoption_basis,
        "skill_usage": adoption["arms"],
        "non_discriminating_tasks": non_discriminating_tasks,
        "interrupted": interrupted,
        "incomplete_runs": incomplete_runs,
        "warnings": all_warnings,
        "settings": _settings_summary(config),
        "thresholds": dict(getattr(config, "thresholds", {}) or {}),
        "pricing": dict(getattr(config, "pricing", {}) or {}),
        "cost_basis": config.cost_basis,
        "provenance": provenance,
        "by_model": by_model,
        "by_category": by_category,
        "context_tax": context_tax,
        "overall": {
            "control": calculate_metrics(control_runs),
            "skill": calculate_metrics(treatment_runs),
            "paired": paired_comparison(control_runs, treatment_runs),
        },
        "baseline_overall": baseline_overall,
        "baseline_comparisons": baseline_comparisons,
        "runs": {
            "control": raw_control_runs,
            "treatment": raw_treatment_runs,
            **({"baseline": raw_baseline_runs} if raw_baseline_runs else {}),
        },
        "retries": list(retry_history or []),
    }

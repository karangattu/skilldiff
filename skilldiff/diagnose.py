import json
import math
import re
from pathlib import Path
from typing import Any

from skilldiff.scope import is_blast_violation
from skilldiff.stats import METRICS, analysis_run, pair_runs, usable_agent_run


def _graded_score(run: dict[str, Any]) -> float | None:
    if not usable_agent_run(run) or run.get("grade_status") in ("ungraded", "timeout", "error"):
        return None
    try:
        score = float(run["score"])
    except (KeyError, TypeError, ValueError):
        return None
    return score if math.isfinite(score) and 0 <= score <= 1 else None


def _known_tokens(run: dict[str, Any]) -> float | None:
    value = METRICS["tokens"](run)
    return value if value is not None and math.isfinite(value) else None


def _planned_pairs(data: dict[str, Any]) -> int | None:
    settings = data.get("settings") or {}
    repetitions = data.get("runs_per_arm", settings.get("runs"))
    models = len(data.get("models") or [])
    tasks = data.get("tasks_count") or len(data.get("tasks") or [])
    if repetitions is None or not models or not tasks:
        return None
    return int(repetitions) * models * int(tasks)


def _run_key(run: dict[str, Any]) -> tuple[str, str, int]:
    return (str(run.get("model", "")), str(run.get("task_id", "")), int(run.get("repetition", 1)))


def _planned_keys(data: dict[str, Any]) -> set[tuple[str, str, int]] | None:
    repetitions = data.get("runs_per_arm", (data.get("settings") or {}).get("runs"))
    if repetitions is None or not data.get("models") or not data.get("tasks"):
        return None
    return {
        (str(model), str(task), rep)
        for model in data["models"]
        for task in data["tasks"]
        for rep in range(1, int(repetitions) + 1)
    }


SKILL_FILE_READS_THRESHOLD = 1


def count_skill_file_reads(transcript: str) -> int:
    if not transcript:
        return 0
    found = set()
    for m in re.finditer(
        r"(?:^|[\s\"'/\\])(SKILL\.md|references[/\\][\w.-]+\.md)",
        transcript,
        re.IGNORECASE,
    ):
        found.add(m.group(1).lower().replace("\\", "/"))
    return len(found)


def _extract_transcript(record: dict[str, Any], root: Path) -> str:
    if record.get("transcript"):
        return str(record["transcript"])
    artifacts = record.get("artifacts")
    if artifacts:
        t_file = root / artifacts / "transcript.txt"
        if t_file.is_file():
            try:
                return t_file.read_text(encoding="utf-8")
            except OSError:
                pass
    return ""


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
        baseline_runs = [r for r in raw_runs if r.get("arm") == "baseline"]
    elif isinstance(raw_runs, dict):
        control_runs = raw_runs.get("control") or []
        treatment_runs = raw_runs.get("treatment") or raw_runs.get("skill") or []
        baseline_runs = raw_runs.get("baseline") or []
    else:
        control_runs = []
        treatment_runs = []
        baseline_runs = []
    arms = {"control": control_runs, "treatment": treatment_runs}
    has_baseline = bool(baseline_runs) or bool(
        (data.get("skill_comparison") or {}).get("include_baseline")
        or (data.get("settings") or {}).get("include_baseline")
        or data.get("include_baseline")
    )
    if has_baseline:
        arms["baseline"] = baseline_runs
    task_details = {str(t.get("id")): t for t in (data.get("task_details") or [])}
    policy = (
        data.get("failure_policy") or (data.get("settings") or {}).get("failure_policy") or {}
    ).get("agent_failure", "exclude")

    under_triggered = []
    over_triggered = []
    regressions = []
    blast_violations = []
    agent_failures = []
    grader_failures = []
    permission_denied = []
    token_bloat_tasks = []
    recommendations = []
    unknown_adoption_runs = 0
    is_pr = bool(data.get("comparison")) or data.get("preset") == "pr"
    skill_arms = {"treatment"}
    if data.get("skill_comparison") or data.get("preset") in {"revision", "compression"}:
        skill_arms.add("control")
    for arm, records in arms.items():
        for record in records:
            identity = {
                "arm": arm,
                "task_id": str(record.get("task_id", "")),
                "model": str(record.get("model", "")),
                "rep": record.get("repetition", 1),
            }
            failed = record.get("status") not in (None, "ok", "correctness") or (
                record.get("exit_code") not in (0, None)
            )
            if failed:
                agent_failures.append(
                    identity
                    | {
                        "status": record.get("status"),
                        "error": record.get("error"),
                    }
                )
            denials = record.get("permission_denials")
            if isinstance(denials, list) and denials:
                permission_denied.append(identity | {"tools": [str(t) for t in denials]})
            feedback = str(record.get("feedback") or "")
            blast = record.get("grade_error_kind") == "blast_radius" or is_blast_violation(feedback)
            if blast:
                blast_violations.append(identity | {"violation": feedback})
            elif record.get("grade_status") in {"error", "timeout"}:
                grader_failures.append(
                    identity
                    | {
                        "status": record["grade_status"],
                        "feedback": record.get("feedback"),
                    }
                )
            if is_pr or arm not in skill_arms or failed:
                continue
            category = task_details.get(identity["task_id"], {}).get(
                "category", record.get("task_category", "general")
            )
            invoked = record.get("skill_invoked")
            transcript = _extract_transcript(record, path if path.is_dir() else path.parent)
            file_reads = count_skill_file_reads(transcript)
            over_read = file_reads > SKILL_FILE_READS_THRESHOLD
            if not isinstance(invoked, bool) and not over_read:
                unknown_adoption_runs += 1
            elif category == "intended" and not invoked and not over_read:
                under_triggered.append(identity)
            elif category == "irrelevant" and (invoked or over_read):
                over_triggered.append(
                    identity | ({"skill_file_reads": file_reads} if file_reads else {})
                )

    pairs = pair_runs(control_runs, treatment_runs)
    usable_score_pairs = 0
    unknown_token_pairs = 0
    for c_run, t_run in pairs:
        task_id = str(t_run.get("task_id", ""))
        model = str(t_run.get("model", ""))
        rep = t_run.get("repetition", 1)
        c_analysis = analysis_run(c_run, policy)
        t_analysis = analysis_run(t_run, policy)
        c_score = _graded_score(c_analysis)
        t_score = _graded_score(t_analysis)
        if c_score is not None and t_score is not None:
            usable_score_pairs += 1
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

        c_tok = _known_tokens(c_analysis)
        t_tok = _known_tokens(t_analysis)
        if c_tok is None or t_tok is None:
            unknown_token_pairs += 1
        elif (
            c_score is not None
            and t_score is not None
            and c_tok > 0
            and t_tok > c_tok * 1.5
            and t_score <= c_score
        ):
            token_bloat_tasks.append(
                {
                    "task_id": task_id,
                    "model": model,
                    "rep": rep,
                    "control_tokens": c_tok,
                    "skill_tokens": t_tok,
                    "ratio": round(t_tok / c_tok, 2),
                }
            )

    if agent_failures:
        recommendations.append(
            f"Agent failures: {len(agent_failures)} session(s) failed across "
            f"{', '.join(sorted({r['arm'] for r in agent_failures}))}. "
            "Inspect their errors and restore completed sessions before drawing conclusions."
        )
    if grader_failures:
        recommendations.append(
            f"Grader failures: {len(grader_failures)} result(s) could not be evaluated. "
            "Fix the grader errors or timeouts and rerun the affected pairs."
        )
    if permission_denied:
        tools = sorted({tool for item in permission_denied for tool in item["tools"]})
        patterns = sorted({
            t.split("(", 1)[1].rstrip(")")
            for item in permission_denied
            for t in item["tools"]
            if "(" in t
        })
        arm_counts = ", ".join(
            f"{arm}: {sum(1 for r in permission_denied if r.get('arm') == arm)}"
            for arm in sorted({r["arm"] for r in permission_denied})
        )
        asymmetry = ""
        c_denials = sum(1 for r in permission_denied if r.get("arm") == "control")
        t_denials = sum(1 for r in permission_denied if r.get("arm") == "treatment")
        if c_denials != t_denials and (c_denials > 0 or t_denials > 0):
            asymmetry = " (uneven across arms — confound)"
        pattern_str = f"; denied patterns: {', '.join(patterns)}" if patterns else ""
        recommendations.append(
            f"Permission denials: the harness refused tool calls ({', '.join(tools)}) in "
            f"{len(permission_denied)} session(s) ({arm_counts}{asymmetry}){pattern_str}. "
            "Headless sessions cannot answer prompts and sandboxed commands fail, so those "
            "agents worked without the tools. Fix the harness permissions (see `skilldiff check`) "
            "before blaming the agent or the skill."
        )
    planned_pairs = _planned_pairs(data)
    expected_keys = _planned_keys(data)
    matched_keys = {_run_key(t) for _, t in pairs}
    missing_planned_pairs = len(expected_keys - matched_keys) if expected_keys is not None else None
    unmatched_runs = len(control_runs) + len(treatment_runs) - 2 * len(pairs)
    if unmatched_runs:
        recommendations.append(
            f"Incomplete pairing: {unmatched_runs} control/treatment record(s) have no "
            "matching model, task, and repetition in the other arm."
        )
    if not pairs or usable_score_pairs < len(pairs):
        recommendations.append(
            f"Score coverage: {usable_score_pairs}/{len(pairs)} matched pair(s) have "
            "usable scores on both arms; complete grading before interpreting the result."
        )
    if planned_pairs is None or expected_keys is None:
        recommendations.append(
            "Planned coverage is unknown: this run does not record all model, task, and run counts."
        )
    elif (
        len(pairs) != planned_pairs
        or any(len(records) != planned_pairs for records in arms.values())
        or matched_keys != expected_keys
    ):
        completed_planned_pairs = len(matched_keys & expected_keys)
        recommendations.append(
            f"Incomplete evaluation: {completed_planned_pairs}/{planned_pairs} planned pair(s) "
            "are matched; "
            "check missing arm results and resume the run."
        )
    baseline_pairs = pair_runs([t for _, t in pairs], baseline_runs)
    control_by_key = {_run_key(c): c for c, _ in pairs}
    baseline_usable_score_pairs = sum(
        all(
            _graded_score(analysis_run(record, policy)) is not None
            for record in (control_by_key[_run_key(t)], t, baseline)
        )
        for t, baseline in baseline_pairs
    )
    if has_baseline and (
        len(baseline_pairs) != len(pairs) or baseline_usable_score_pairs != len(pairs)
    ):
        recommendations.append(
            f"Baseline coverage: {baseline_usable_score_pairs}/{len(pairs)} primary pair(s) "
            "have matching, usable baseline scores; complete the baseline evaluation."
        )
    if unknown_adoption_runs:
        recommendations.append(
            f"Adoption coverage: skill use is unknown for {unknown_adoption_runs} session(s); "
            "inspect transcripts before concluding that adoption is clean."
        )
    if unknown_token_pairs:
        recommendations.append(
            f"Efficiency coverage: token totals are unavailable for {unknown_token_pairs} "
            "matched pair(s); token bloat cannot be ruled out for those pairs."
        )
    if data.get("valid") is False:
        recommendations.append(
            "Invalid evaluation: control or baseline contamination prevents a clean comparison."
        )
    if data.get("interrupted") or any(
        r.get("complete") is False for records in arms.values() for r in records
    ):
        recommendations.append(
            "Interrupted evaluation: resume the run to complete the missing results."
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
            f"Blast radius: runs on {', '.join(tasks_list)} edited paths outside the task's "
            "allowed_paths/forbidden_paths and scored N/A. First check the task scope: "
            "files the work legitimately writes (such as outputs/) belong in allowed_paths "
            "or grader_ignore, and a forbidden pattern like 'data/*' also matches nested "
            "copies. Only if the edits are truly out of scope, tighten the prompt or skill."
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
        "total_pairs": len(pairs),
        "usable_score_pairs": usable_score_pairs,
        "planned_pairs": planned_pairs,
        "missing_planned_pairs": missing_planned_pairs,
        "baseline_pairs": len(baseline_pairs),
        "baseline_usable_score_pairs": baseline_usable_score_pairs,
        "unmatched_runs": unmatched_runs,
        "unknown_adoption_runs": unknown_adoption_runs,
        "unknown_token_pairs": unknown_token_pairs,
        "under_triggered": under_triggered,
        "over_triggered": over_triggered,
        "regressions": regressions,
        "blast_violations": blast_violations,
        "agent_failures": agent_failures,
        "grader_failures": grader_failures,
        "permission_denied": permission_denied,
        "token_bloat_tasks": token_bloat_tasks,
        "recommendations": recommendations,
    }

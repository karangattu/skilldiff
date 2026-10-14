from __future__ import annotations

import concurrent.futures
import hashlib
import inspect
import os
import random
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from skilldiff.config import ExperimentConfig, TaskConfig, read_skill_name
from skilldiff.grader import Candidate
from skilldiff.persistence import atomic_json, atomic_write
from skilldiff.runner import (
    AgentRunner,
    RunResult,
    auth_login_hint,
    looks_like_auth_error,
    looks_like_rate_limit,
)
from skilldiff.scope import is_blast_violation, scope_instructions
from skilldiff.snapshots import file_hash
from skilldiff.stats import analysis_run
from skilldiff.workspace import Workspace

MAX_STORED_ERROR = 2000


def safe_path_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "item"


def balanced_arm_order(model: str, task_id: str, repetition: int, seed: int) -> list[str]:
    h = hashlib.sha256(f"{seed}:{model}:{task_id}".encode()).hexdigest()
    start_with_control = (int(h[:8], 16) + (repetition - 1)) % 2 == 0
    if start_with_control:
        return ["control", "treatment"]
    return ["treatment", "control"]


def balanced_three_order(
    model: str, task_id: str, repetition: int, seed: int
) -> list[str]:
    h = hashlib.sha256(f"{seed}:{model}:{task_id}:3arm".encode()).hexdigest()
    rotations = [
        ["control", "treatment", "baseline"],
        ["treatment", "baseline", "control"],
        ["baseline", "control", "treatment"],
    ]
    return list(rotations[(int(h[:8], 16) + (repetition - 1)) % 3])


@dataclass
class _Pair:
    model: str
    task: TaskConfig
    repetition: int


def postprocess_grade(
    record: dict[str, Any], failure_policy: dict[str, Any] | None = None
) -> None:
    if record.get("failure_kind") == "harness_error":
        record["partial_score"] = record.get("score")
        record["score"] = None
        record["success"] = False
        record["grade_status"] = "ungraded"
        record["analysis_excluded"] = True
    fp = failure_policy or {}
    if record.get("status") not in (None, "ok", "correctness"):
        if (
            fp.get("agent_failure") == "zero"
            and record.get("grade_status") not in ("timeout", "error")
        ):
            record["partial_score"] = record.get("score")
            record.update(analysis_run(record, "zero"))
        else:
            record["analysis_excluded"] = True


def save_run_artifacts(
    arm_dir: Path,
    record: dict[str, Any],
    transcript: str,
    diff: str,
) -> None:
    from skilldiff.telemetry import redact_secrets

    clean_transcript = redact_secrets(transcript)
    if record.get("error"):
        record["error"] = redact_secrets(str(record["error"]))
    if record.get("transcript"):
        record["transcript"] = redact_secrets(str(record["transcript"]))
    atomic_write(arm_dir / "transcript.txt", clean_transcript)
    atomic_write(arm_dir / "diff.patch", diff)
    record["artifact_hashes"] = {
        name: file_hash(arm_dir / name) for name in ("transcript.txt", "diff.patch")
    }
    atomic_json(arm_dir / "run.json", record)


def prepare_pair_workspaces(
    config: ExperimentConfig,
    execution: ExperimentConfig,
    comparison: dict[str, Any] | None,
    fixture_repo: Path | None,
    tmp_ctrl: Path,
    tmp_treat: Path,
    tmp_base: Path,
) -> dict[str, Workspace]:
    is_ab = config.is_skill_comparison
    pr_mode = (comparison or {}).get("mode", "agent") if comparison else "agent"

    if is_ab:
        ctrl_skill = execution.skill_a
        treat_skill = execution.skill_b
        ctrl_is_treatment = True
        treat_is_treatment = True
    elif comparison:
        ctrl_skill = None
        treat_skill = None
        ctrl_is_treatment = False
        treat_is_treatment = False
    else:
        ctrl_skill = execution.skill
        treat_skill = execution.skill
        ctrl_is_treatment = False
        treat_is_treatment = True

    pr_skills = (
        (comparison or {}).get("touched_skills")
        if (comparison and pr_mode == "agent")
        else None
    )

    reflink_enabled = getattr(config, "reflink", False)
    workspaces = {
        "control": Workspace(
            root=tmp_ctrl / "workspace",
            is_treatment=ctrl_is_treatment,
            source_commit=(comparison or {}).get("control_commit"),
            skill_dir=ctrl_skill,
            strip_skill_dirs=([treat_skill] if is_ab and treat_skill else None),
            fixture_repo=fixture_repo,
            harness=config.harness,
            pr_touched_skills=pr_skills,
            reflink=reflink_enabled,
        ),
        "treatment": Workspace(
            root=tmp_treat / "workspace",
            is_treatment=treat_is_treatment,
            source_commit=(comparison or {}).get("treatment_commit"),
            skill_dir=treat_skill,
            strip_skill_dirs=([ctrl_skill] if is_ab and ctrl_skill else None),
            fixture_repo=fixture_repo,
            harness=config.harness,
            pr_touched_skills=pr_skills,
            reflink=reflink_enabled,
        ),
    }
    if is_ab and config.include_baseline:
        workspaces["baseline"] = Workspace(
            root=tmp_base / "workspace",
            is_treatment=False,
            source_commit=None,
            skill_dir=None,
            strip_skill_dirs=[
                s for s in (ctrl_skill, treat_skill) if s is not None
            ] or None,
            fixture_repo=fixture_repo,
            harness=config.harness,
            reflink=reflink_enabled,
        )
    for ws in workspaces.values():
        ws.setup()
    return workspaces


def probe_arm_preflights(
    workspaces: dict[str, Workspace],
    execution: ExperimentConfig,
    agent_runner: AgentRunner,
    task: TaskConfig,
    model: str,
    arm_dirs: dict[str, Path],
    progress: Callable[[str], None],
) -> dict[str, Any]:
    preflight_reports: dict[str, Any] = {}
    for arm, ws in workspaces.items():
        expected = (
            [read_skill_name(path) or path.name for path in ws.skill_dirs]
            if ws.is_treatment
            else []
        )
        directory = arm_dirs[arm]
        progress(f"Preflight: {model} · {task.id} · {arm}")
        try:
            probe = agent_runner.preflight(
                ws.root, execution, expected, runtime_probe=task.runtime_probe
            )
            preflight_reports[arm] = probe
            atomic_json(directory / "preflight.json", {"status": "ok", **probe})
        except Exception as exc:
            atomic_json(
                directory / "preflight.json", {"status": "error", "error": str(exc)}
            )
            raise
    return preflight_reports


def build_arm_record(
    arm: str,
    arm_dir: Path,
    run_root: Path,
    model: str,
    task: TaskConfig,
    repetition: int,
    order: list[str],
    seed: int,
    res: RunResult,
    grade: Any,
    diff_text: str,
    files: list[str],
    preflight_reports: dict[str, Any],
    workspace: Workspace,
    comparison: dict[str, Any] | None,
    is_ab: bool,
    config: ExperimentConfig,
    pr_mode: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "complete": True,
        "model": model,
        "task_id": task.id,
        "task_category": getattr(task, "category", "general"),
        "task_split": getattr(task, "split", "dev"),
        "repetition": repetition,
        "arm": arm,
        "run_order": (order.index(arm) + 1) if arm in order else 3,
        "seed": seed,
        "attempt": 1,
        "retries": [],
        "status": res.status,
        "failure_kind": res.failure_kind,
        "error": (res.error or "")[:MAX_STORED_ERROR] or None,
        "prompt": res.prompt,
        "response": res.response,
        "duration": res.duration,
        "cost": res.cost,
        "input_tokens": res.input_tokens,
        "cache_read_tokens": res.cache_read_tokens,
        "cache_creation_tokens": res.cache_creation_tokens,
        "output_tokens": res.output_tokens,
        "reported_total_tokens": res.reported_total_tokens,
        "session_id": res.session_id,
        "cli": preflight_reports.get(arm, {}).get("cli"),
        "num_turns": res.num_turns,
        "tool_calls": res.tool_calls,
        "skill_invoked": res.skill_invoked,
        "skill_available": res.skill_available,
        "permission_denials": res.permission_denials,
        "files_changed": files,
        "score": grade.score,
        "success": grade.success,
        "grade_status": getattr(grade, "grade_status", "graded"),
        "blind_label": grade.label,
        "feedback": grade.feedback,
        "exit_code": res.exit_code,
        "artifacts": str(arm_dir.relative_to(run_root)),
        "isolation_issues": workspace.isolation_issues or [],
    }
    if is_blast_violation(grade.feedback):
        record["grade_error_kind"] = "blast_radius"
    if comparison:
        record["source_commit"] = comparison[f"{arm}_commit"]
        record["pr_mode"] = pr_mode
        record["pr_pair"] = comparison.get("pair")
    if is_ab:
        record["skill_revision"] = (
            "A" if arm == "control" else ("B" if arm == "treatment" else "baseline")
        )
        if arm == "control" and config.skill_a:
            record["skill_path"] = str(config.skill_a)
        if arm == "treatment" and config.skill_b:
            record["skill_path"] = str(config.skill_b)
    if arm in ("control", "baseline") and workspace.removed_from_control:
        record["removed_from_fixture"] = workspace.removed_from_control
    return record


def check_pair_harness_abort(
    records: dict[str, dict[str, Any]],
    harness: str,
    task_id: str,
    repetition: int,
) -> None:
    if records and all(
        r.get("failure_kind") == "harness_error" for r in records.values()
    ):
        sample = next(
            (r.get("error") for r in records.values() if r.get("error")),
            "agent CLI failed to start",
        )
        hint = (
            f" {auth_login_hint(harness)}, then rerun with --resume."
            if looks_like_auth_error(sample)
            else ""
        )
        raise RuntimeError(
            f"Harness '{harness}' could not run any session for task "
            f"'{task_id}' (rep {repetition}): "
            f"{str(sample).strip().splitlines()[0][:300]}. "
            f"Aborting before running the remaining pairs.{hint}"
        )


def run_pair(
    runner: Any,
    pair: _Pair,
    run_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    runner._ensure_running()
    runner.progress(
        f"  -> Started {pair.model} · {pair.task.id} (rep {pair.repetition})"
    )
    task = pair.task
    model = pair.model
    execution = runner._execution_config
    fixture_repo = (
        runner.config.pr.repo
        if runner.config.pr
        else runner._fixture_snapshots.get(task.id)
    )
    grader = runner._make_grader(task, model, execution, runner._grader_names(execution))

    rep_str = f"{pair.repetition:03d}"
    model_dir = safe_path_component(model)
    task_path = safe_path_component(task.id)
    ctrl_dir = run_root / model_dir / task_path / "control" / rep_str
    treat_dir = run_root / model_dir / task_path / "treatment" / rep_str
    baseline_dir = run_root / model_dir / task_path / "baseline" / rep_str
    ctrl_dir.mkdir(parents=True, exist_ok=True)
    treat_dir.mkdir(parents=True, exist_ok=True)
    if runner.config.is_skill_comparison and runner.config.include_baseline:
        baseline_dir.mkdir(parents=True, exist_ok=True)

    is_ab = runner.config.is_skill_comparison
    pr_mode = (runner.comparison or {}).get("mode", "agent") if runner.comparison else "agent"
    is_correctness = bool(runner.comparison) and pr_mode == "correctness"

    with (
        tempfile.TemporaryDirectory(prefix="skilldiff-") as tmp_ctrl,
        tempfile.TemporaryDirectory(prefix="skilldiff-") as tmp_treat,
        tempfile.TemporaryDirectory(prefix="skilldiff-") as tmp_base,
    ):
        workspaces = prepare_pair_workspaces(
            config=runner.config,
            execution=execution,
            comparison=runner.comparison,
            fixture_repo=fixture_repo,
            tmp_ctrl=Path(tmp_ctrl),
            tmp_treat=Path(tmp_treat),
            tmp_base=Path(tmp_base),
        )

        preflight_reports: dict[str, Any] = {}
        arm_dirs_map = {
            "control": ctrl_dir,
            "treatment": treat_dir,
            "baseline": baseline_dir,
        }
        if not is_correctness and not os.environ.get("SKILLDIFF_MOCK_RUNNER"):
            preflight_reports = probe_arm_preflights(
                workspaces=workspaces,
                execution=execution,
                agent_runner=runner.agent_runner,
                task=task,
                model=model,
                arm_dirs=arm_dirs_map,
                progress=runner.progress,
            )

        has_baseline = "baseline" in workspaces and not is_correctness
        if has_baseline:
            order = balanced_three_order(model, task.id, pair.repetition, runner._seed)
        else:
            order = balanced_arm_order(model, task.id, pair.repetition, runner._seed)

        results: dict[str, RunResult] = {}
        if is_correctness:
            from skilldiff.runner import RunResult as _RR

            for arm in ("control", "treatment"):
                results[arm] = _RR(
                    prompt=f"[correctness] untouched {arm} revision",
                    response="",
                    transcript="correctness mode: no agent session",
                    duration=0.0,
                    cost=0.0,
                    input_tokens=0,
                    output_tokens=0,
                    tool_calls=0,
                    exit_code=0,
                    num_turns=0,
                    skill_invoked=None,
                    skill_available=None,
                    status="correctness",
                )
            for arm, arm_dir in (("control", ctrl_dir), ("treatment", treat_dir)):
                runner._save_run_artifacts(
                    arm_dir,
                    {
                        "model": model,
                        "task_id": task.id,
                        "repetition": pair.repetition,
                        "arm": arm,
                        "status": "correctness",
                        "transcript": "correctness mode",
                    },
                    "correctness mode: no agent session",
                    "",
                )
        else:
            def _execute_arm(arm: str) -> tuple[str, RunResult]:
                max_retries = 2
                delay = 1.0
                last_res: RunResult | None = None
                for attempt in range(1, max_retries + 2):
                    runner._ensure_running()
                    task_prompt_val = (
                        task.prompts if getattr(task, "prompts", None) else task.prompt
                    )
                    constraints = scope_instructions(
                        task.allowed_paths, task.forbidden_paths, task.grader_ignore
                    )
                    if isinstance(task_prompt_val, list):
                        task_prompt_val = [prompt + constraints for prompt in task_prompt_val]
                    else:
                        task_prompt_val += constraints
                    runner.progress(f"Agent starting: {model} · {task.id} · {arm}")
                    arm_skills = (
                        [
                            read_skill_name(path) or path.name
                            for path in workspaces[arm].skill_dirs
                        ]
                        if workspaces[arm].skill_dirs
                        else None
                    )
                    try:
                        run_sig = inspect.signature(runner.agent_runner.run)
                        if "skill_names" in run_sig.parameters or any(
                            p.kind == inspect.Parameter.VAR_KEYWORD
                            for p in run_sig.parameters.values()
                        ):
                            res = runner.agent_runner.run(
                                task_prompt_val,
                                workspaces[arm].root,
                                model,
                                execution,
                                skill_names=arm_skills,
                            )
                        else:
                            res = runner.agent_runner.run(
                                task_prompt_val,
                                workspaces[arm].root,
                                model,
                                execution,
                            )
                    except KeyboardInterrupt:
                        diff_text, changed = workspaces[arm].get_diff()
                        runner._save_run_artifacts(
                            arm_dirs_map[arm],
                            {
                                "model": model,
                                "task_id": task.id,
                                "repetition": pair.repetition,
                                "arm": arm,
                                "complete": False,
                                "status": "interrupted",
                                "grade_status": "pending",
                                "prompt": task_prompt_val,
                                "cost": None,
                                "input_tokens": None,
                                "output_tokens": None,
                                "files_changed": changed,
                                "error": "Interrupted before harness telemetry was returned",
                            },
                            "Interrupted; harness telemetry unavailable",
                            diff_text,
                        )
                        raise

                    last_res = res
                    if (
                        attempt <= max_retries
                        and (
                            res.failure_kind == "rate_limit"
                            or looks_like_rate_limit(res.error or res.transcript)
                        )
                    ):
                        lock = getattr(runner, "_lock", None)
                        retry_entry = {
                            "model": model,
                            "task_id": task.id,
                            "repetition": pair.repetition,
                            "arm": arm,
                            "attempt": attempt,
                            "status": "rate_limit",
                            "cost": res.cost,
                        }
                        if lock:
                            with lock:
                                runner._retry_history.append(retry_entry)
                        else:
                            runner._retry_history.append(retry_entry)
                        runner.progress(
                            f"Rate limit for {model} · {task.id} · {arm} (attempt {attempt}); "
                            f"retrying in {delay:.1f}s..."
                        )
                        time.sleep(delay + random.uniform(0.1, 0.4))
                        delay *= 2.0
                        workspaces[arm].setup()
                        continue

                    immediate_diff, immediate_files = workspaces[arm].get_diff()
                    runner._save_run_artifacts(
                        arm_dirs_map[arm],
                        {
                            **{
                                key: value
                                for key, value in asdict(res).items()
                                if key != "transcript"
                            },
                            "model": model,
                            "task_id": task.id,
                            "repetition": pair.repetition,
                            "arm": arm,
                            "complete": False,
                            "grade_status": "pending",
                            "attempt": attempt,
                            "files_changed": immediate_files,
                        },
                        res.transcript,
                        immediate_diff,
                    )
                    runner.progress(
                        f"Agent finished: {model} · {task.id} · {arm}; artifacts saved"
                    )
                    return arm, res

                assert last_res is not None
                immediate_diff, immediate_files = workspaces[arm].get_diff()
                runner._save_run_artifacts(
                    arm_dirs_map[arm],
                    {
                        **{
                            key: value
                            for key, value in asdict(last_res).items()
                            if key != "transcript"
                        },
                        "model": model,
                        "task_id": task.id,
                        "repetition": pair.repetition,
                        "arm": arm,
                        "complete": False,
                        "grade_status": "pending",
                        "attempt": max_retries + 1,
                        "files_changed": immediate_files,
                    },
                    last_res.transcript,
                    immediate_diff,
                )
                return arm, last_res

            arm_parallel = getattr(runner.config, "arm_parallel", False)
            active_arms = [arm for arm in order if arm in workspaces]
            if arm_parallel and len(active_arms) > 1:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=len(active_arms)
                ) as executor:
                    futures = []
                    for idx, arm in enumerate(active_arms):
                        if idx > 0:
                            time.sleep(0.1)
                        futures.append(executor.submit(_execute_arm, arm))
                    for fut in concurrent.futures.as_completed(futures):
                        arm, res = fut.result()
                        results[arm] = res
            else:
                for arm in active_arms:
                    arm, res = _execute_arm(arm)
                    results[arm] = res

        diffs = {
            arm: workspaces[arm].get_diff()
            for arm in workspaces
            if arm in results
        }
        runner._ensure_running()
        runner._check_graders()
        grades = grader.grade_candidates([
            Candidate(
                "",
                arm,
                workspaces[arm].root,
                results[arm].response,
                diffs[arm][0],
                results[arm].transcript,
                diffs[arm][1],
            )
            for arm in workspaces
            if arm in results
        ])
        runner._check_graders()

        records: dict[str, dict[str, Any]] = {}
        arm_dirs = {"control": ctrl_dir, "treatment": treat_dir}
        if "baseline" in results:
            arm_dirs["baseline"] = baseline_dir
        for arm, arm_dir in arm_dirs.items():
            runner._ensure_running()
            res = results[arm]
            if runner.comparison and not is_ab:
                res.skill_invoked = None
                res.skill_available = None
            grade = grades[arm]
            diff_text, files = diffs[arm]
            records[arm] = build_arm_record(
                arm=arm,
                arm_dir=arm_dir,
                run_root=run_root,
                model=model,
                task=task,
                repetition=pair.repetition,
                order=order,
                seed=runner._seed,
                res=res,
                grade=grade,
                diff_text=diff_text,
                files=files,
                preflight_reports=preflight_reports,
                workspace=workspaces[arm],
                comparison=runner.comparison,
                is_ab=is_ab,
                config=runner.config,
                pr_mode=pr_mode,
            )
            runner._postprocess_grade(records[arm])
            runner._save_run_artifacts(arm_dir, records[arm], res.transcript, diff_text)

        if "baseline" in records:
            with runner._lock:
                runner._baseline_runs.append(records["baseline"])
                if records["baseline"].get("status") not in (None, "ok"):
                    runner._retry_history.append(
                        {
                            "model": model,
                            "task_id": task.id,
                            "repetition": pair.repetition,
                            "arm": "baseline",
                            "status": records["baseline"].get("status"),
                            "cost": records["baseline"].get("cost"),
                        }
                    )

        for ws in workspaces.values():
            ws.cleanup()

    check_pair_harness_abort(records, execution.harness, task.id, pair.repetition)
    return records["control"], records["treatment"]

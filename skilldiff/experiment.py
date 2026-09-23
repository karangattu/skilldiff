import json
import platform
import random
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from skilldiff import __version__
from skilldiff.config import ExperimentConfig, TaskConfig
from skilldiff.grader import Grader
from skilldiff.reporter import calculate_metrics, create_reports
from skilldiff.revisions import resolve_comparison
from skilldiff.runner import AgentRunner, RunResult
from skilldiff.stats import paired_comparison
from skilldiff.workspace import Workspace

MAX_STORED_ERROR = 2000

USER_SKILL_DIRS = {
    "claude": ["~/.claude/skills"],
    "codex": ["~/.codex/skills", "~/.agents/skills"],
    "opencode": ["~/.config/opencode/skills", "~/.claude/skills", "~/.agents/skills"],
    "antigravity": ["~/.gemini/skills", "~/.agents/skills"],
}


def safe_path_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "item"


def find_user_level_installs(skill_names: list[str], harness: str) -> list[str]:
    """User-level copies of the skill that the control arm could also load."""
    found: list[str] = []
    for base in USER_SKILL_DIRS.get(harness, []):
        for name in skill_names:
            path = Path(base).expanduser() / name
            if (path / "SKILL.md").is_file():
                found.append(str(path))
    return found


@dataclass
class _Pair:
    model: str
    task: TaskConfig
    repetition: int


class ExperimentRunner:
    def __init__(
        self,
        config: ExperimentConfig,
        tasks: list[TaskConfig],
        output_dir: Path | None = None,
        agent_runner: AgentRunner | None = None,
        progress: Optional[Callable[[str], None]] = None,
    ):
        self.config = config
        self.tasks = tasks
        if output_dir is None:
            base = config.config_path.parent if config.config_path else Path(".")
            output_dir = base / "runs"
        self.output_dir = output_dir
        self.agent_runner = agent_runner or AgentRunner()
        self.progress = progress or (lambda _msg: None)
        self._lock = threading.Lock()
        self.comparison = None

    # ------------------------------------------------------------------ setup

    def preflight_warnings(self) -> list[str]:
        warnings: list[str] = []
        installs = find_user_level_installs(self.config.skill_names, self.config.harness)
        isolated = self.config.harness == "claude" and self.config.claude.isolate
        if installs and not isolated:
            warnings.append(
                "The skill is also installed at user level, so the control arm can load it: "
                + ", ".join(installs)
            )
        return warnings

    def _metadata(self, timestamp: str) -> dict[str, Any]:
        return {
            "name": self.config.name,
            "skilldiff_version": __version__,
            "skill": str(self.config.skill) if self.config.skill else None,
            "comparison": self.comparison,
            "skill_names": self.config.skill_names,
            "harness": self.config.harness,
            "models": self.config.models,
            "runs": self.config.runs,
            "timeout_seconds": self.config.timeout_seconds,
            "parallel": self.config.parallel,
            "claude": asdict(self.config.claude),
            "codex": asdict(self.config.codex),
            "opencode": asdict(self.config.opencode),
            "antigravity": asdict(self.config.antigravity),
            "tasks": [
                {
                    "id": t.id,
                    "repo": t.repo,
                    "grader": t.grader.command if t.grader else None,
                }
                for t in self.tasks
            ],
            "timestamp": timestamp,
            "system": {
                "os": platform.system(),
                "python": platform.python_version(),
            },
        }

    # -------------------------------------------------------------------- run

    def run(self) -> dict[str, Any]:
        self.comparison = resolve_comparison(self.config.pr) if self.config.pr else None
        timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        run_root = self.output_dir / timestamp_str
        run_root.mkdir(parents=True, exist_ok=True)

        metadata = self._metadata(timestamp_str)
        warnings = self.preflight_warnings()
        for warning in warnings:
            self.progress(f"warning: {warning}")
        with open(run_root / "experiment.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

        pairs = [
            _Pair(model, task, rep)
            for model in self.config.models
            for task in self.tasks
            for rep in range(1, self.config.runs + 1)
        ]
        control_runs: list[dict[str, Any]] = []
        treatment_runs: list[dict[str, Any]] = []
        done = 0
        interrupted = False

        def record(pair_result: tuple[dict[str, Any], dict[str, Any]]) -> None:
            nonlocal done
            ctrl, treat = pair_result
            with self._lock:
                control_runs.append(ctrl)
                treatment_runs.append(treat)
                done += 1
                self.progress(
                    f"[{done}/{len(pairs)}] {ctrl['model']} · {ctrl['task_id']} · "
                    f"run {ctrl['repetition']}: control {_run_summary(ctrl)} | "
                    f"{'treatment' if self.comparison else 'skill'} {_run_summary(treat)}"
                )

        try:
            if self.config.parallel <= 1:
                for pair in pairs:
                    record(self._run_pair(pair, run_root))
            else:
                pool = ThreadPoolExecutor(max_workers=self.config.parallel)
                futures = [pool.submit(self._run_pair, p, run_root) for p in pairs]
                try:
                    for future in as_completed(futures):
                        record(future.result())
                except KeyboardInterrupt:
                    # Agents run in their own process groups, so Ctrl-C doesn't reach them.
                    pool.shutdown(wait=False, cancel_futures=True)
                    self.agent_runner.terminate_all()
                    pool.shutdown(wait=True)
                    raise
                pool.shutdown()
        except KeyboardInterrupt:
            interrupted = True
            self.progress("Interrupted: writing a report for the completed pairs...")

        results = self._aggregate(
            run_root, timestamp_str, control_runs, treatment_runs, warnings, interrupted
        )
        paths = create_reports(results, run_root)
        results["report"] = {k: (str(v) if v else None) for k, v in paths.items()}

        with open(run_root / "results.json", "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        return results

    def _run_pair(self, pair: _Pair, run_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        task = pair.task
        model = pair.model
        task_dir = task.source_path.parent if task.source_path else None
        fixture_repo = (task_dir / task.repo).resolve() if task.repo and task_dir else None
        if self.config.pr:
            fixture_repo = self.config.pr.repo
        grader = Grader(task.grader, self.config.skill_names, model, task_dir=task_dir)

        rep_str = f"{pair.repetition:03d}"
        model_dir = safe_path_component(model)
        task_path = safe_path_component(task.id)
        ctrl_dir = run_root / model_dir / task_path / "control" / rep_str
        treat_dir = run_root / model_dir / task_path / "treatment" / rep_str
        ctrl_dir.mkdir(parents=True, exist_ok=True)
        treat_dir.mkdir(parents=True, exist_ok=True)

        with (
            tempfile.TemporaryDirectory(prefix="skilldiff-") as tmp_ctrl,
            tempfile.TemporaryDirectory(prefix="skilldiff-") as tmp_treat,
        ):
            workspaces = {
                "control": Workspace(
                    root=Path(tmp_ctrl) / "workspace",
                    is_treatment=False,
                    source_commit=(self.comparison or {}).get("control_commit"),
                    skill_dir=self.config.skill,
                    fixture_repo=fixture_repo,
                    harness=self.config.harness,
                ),
                "treatment": Workspace(
                    root=Path(tmp_treat) / "workspace",
                    is_treatment=True,
                    source_commit=(self.comparison or {}).get("treatment_commit"),
                    skill_dir=self.config.skill,
                    fixture_repo=fixture_repo,
                    harness=self.config.harness,
                ),
            }
            for ws in workspaces.values():
                ws.setup()

            # Randomize which arm runs first so warm caches, rate limits, and time-of-day
            # effects do not systematically favor one arm.
            order = ["control", "treatment"]
            random.shuffle(order)
            results: dict[str, RunResult] = {}
            for arm in order:
                results[arm] = self.agent_runner.run(
                    task.prompt, workspaces[arm].root, model, self.config
                )

            diffs = {arm: workspaces[arm].get_diff() for arm in order}
            grade_ctrl, grade_treat = grader.grade_pair(
                control_ws=workspaces["control"].root,
                treatment_ws=workspaces["treatment"].root,
                control_response=results["control"].response,
                treatment_response=results["treatment"].response,
                control_diff=diffs["control"][0],
                treatment_diff=diffs["treatment"][0],
                control_transcript=results["control"].transcript,
                treatment_transcript=results["treatment"].transcript,
            )
            grades = {"control": grade_ctrl, "treatment": grade_treat}

            records: dict[str, dict[str, Any]] = {}
            for arm, arm_dir in (("control", ctrl_dir), ("treatment", treat_dir)):
                res = results[arm]
                if self.comparison:
                    res.skill_invoked = None
                    res.skill_available = None
                grade = grades[arm]
                diff_text, files = diffs[arm]
                records[arm] = {
                    "model": model,
                    "task_id": task.id,
                    "repetition": pair.repetition,
                    "arm": arm,
                    "run_order": order.index(arm) + 1,
                    "status": res.status,
                    "error": (res.error or "")[:MAX_STORED_ERROR] or None,
                    "prompt": res.prompt,
                    "response": res.response,
                    "duration": res.duration,
                    "cost": res.cost,
                    "input_tokens": res.input_tokens,
                    "cache_read_tokens": res.cache_read_tokens,
                    "cache_creation_tokens": res.cache_creation_tokens,
                    "output_tokens": res.output_tokens,
                    "num_turns": res.num_turns,
                    "tool_calls": res.tool_calls,
                    "skill_invoked": res.skill_invoked,
                    "skill_available": res.skill_available,
                    "files_changed": files,
                    "score": grade.score,
                    "success": grade.success,
                    "blind_label": grade.label,
                    "feedback": grade.feedback,
                    "exit_code": res.exit_code,
                    "artifacts": str(arm_dir.relative_to(run_root)),
                }
                if self.comparison:
                    records[arm]["source_commit"] = self.comparison[f"{arm}_commit"]
                if arm == "control" and workspaces[arm].removed_from_control:
                    records[arm]["removed_from_fixture"] = workspaces[arm].removed_from_control
                self._save_run_artifacts(arm_dir, records[arm], res.transcript, diff_text)

        return records["control"], records["treatment"]

    # -------------------------------------------------------------- aggregate

    def _aggregate(
        self,
        run_root: Path,
        timestamp: str,
        control_runs: list[dict[str, Any]],
        treatment_runs: list[dict[str, Any]],
        warnings: list[str],
        interrupted: bool,
    ) -> dict[str, Any]:
        def sort_key(r: dict[str, Any]) -> tuple[int, int, int]:
            model_idx = (
                self.config.models.index(r["model"]) if r["model"] in self.config.models else 0
            )
            task_ids = [t.id for t in self.tasks]
            task_idx = task_ids.index(r["task_id"]) if r["task_id"] in task_ids else 0
            return (model_idx, task_idx, int(r["repetition"]))

        control_runs.sort(key=sort_key)
        treatment_runs.sort(key=sort_key)

        by_model: dict[str, dict[str, Any]] = {}
        for model in self.config.models:
            m_ctrl = [r for r in control_runs if r["model"] == model]
            m_treat = [r for r in treatment_runs if r["model"] == model]
            by_task: dict[str, dict[str, Any]] = {}
            for task in self.tasks:
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

        warnings = list(warnings)
        warnings.extend(_run_warnings(
            control_runs, treatment_runs, self.tasks,
            treatment_label="treatment" if self.comparison else "skill",
        ))
        if interrupted:
            warnings.append(
                f"The experiment was interrupted after {len(control_runs)} of "
                f"{len(self.config.models) * len(self.tasks) * self.config.runs} pairs."
            )

        return {
            "name": self.config.name,
            "skilldiff_version": __version__,
            "harness": self.config.harness,
            "skill": str(self.config.skill) if self.config.skill else None,
            "comparison": self.comparison,
            "skill_names": self.config.skill_names,
            "timestamp": timestamp,
            "run_dir": str(run_root),
            "models": self.config.models,
            "tasks": [t.id for t in self.tasks],
            "tasks_count": len(self.tasks),
            "runs_per_arm": self.config.runs,
            "interrupted": interrupted,
            "warnings": warnings,
            "settings": _settings_summary(self.config),
            "by_model": by_model,
            "overall": {
                "control": calculate_metrics(control_runs),
                "skill": calculate_metrics(treatment_runs),
                "paired": paired_comparison(control_runs, treatment_runs),
            },
            "runs": {"control": control_runs, "treatment": treatment_runs},
        }

    def _save_run_artifacts(
        self,
        arm_dir: Path,
        record: dict[str, Any],
        transcript: str,
        diff: str,
    ) -> None:
        with open(arm_dir / "run.json", "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        with open(arm_dir / "transcript.txt", "w", encoding="utf-8") as f:
            f.write(transcript)
        with open(arm_dir / "diff.patch", "w", encoding="utf-8") as f:
            f.write(diff)


def _run_summary(run: dict[str, Any]) -> str:
    if run.get("status") not in (None, "ok"):
        return str(run["status"]).upper()
    text = f"{round(float(run.get('score', 0.0)) * 100)}% in {float(run.get('duration', 0)):.0f}s"
    if run.get("arm") == "treatment" and run.get("skill_invoked") is False:
        text += " (skill unused)"
    return text


def _settings_summary(config: ExperimentConfig) -> dict[str, Any]:
    harness_cfg = asdict(getattr(config, config.harness, config.claude))
    harness_cfg.pop("bin_path", None)
    return {
        "harness": config.harness,
        "timeout_seconds": config.timeout_seconds,
        "parallel": config.parallel,
        **{k: v for k, v in harness_cfg.items() if v not in (None, [], "")},
    }


def _run_warnings(
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
    tasks: list[TaskConfig],
    treatment_label: str = "skill",
) -> list[str]:
    warnings: list[str] = []
    for arm_name, runs in (("control", control_runs), (treatment_label, treatment_runs)):
        failed = [r for r in runs if r.get("status") not in (None, "ok")]
        if failed:
            kinds = sorted({str(r.get("status")) for r in failed})
            sample = next((r.get("error") for r in failed if r.get("error")), None)
            msg = (
                f"{len(failed)} of {len(runs)} {arm_name} runs ended with "
                f"{' or '.join(kinds)}; their scores reflect whatever the agent left behind."
            )
            if sample:
                msg += f" First error: {str(sample).strip().splitlines()[0][:200]}"
            warnings.append(msg)

    contaminated = [r for r in control_runs if r.get("skill_available") or r.get("skill_invoked")]
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
    known = [r for r in treatment_runs if r.get("skill_invoked") is not None]
    unused = [r for r in known if not r.get("skill_invoked")]
    if known and unused:
        warnings.append(
            f"The agent did not use the skill in {len(unused)} of {len(known)} skill runs. "
            "Those runs measure the cost of having the skill installed, not of following it. "
            "Consider sharpening the skill's `description` so it triggers."
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
            "No grader is configured for " + ", ".join(ungraded) + "; every run of those "
            "tasks scores 100%, so only cost and time are compared."
        )
    return warnings

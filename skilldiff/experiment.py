import hashlib
import json
import platform
import random
import re
import shutil
import subprocess
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


def _sha256_file(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _hash_dir(root: Path, limit_files: int = 1000) -> tuple[str, list[dict[str, str]]]:
    files: list[dict[str, str]] = []
    if not root.is_dir():
        return "", files
    paths = sorted(p for p in root.rglob("*") if p.is_file())
    for p in paths[:limit_files]:
        try:
            rel = str(p.relative_to(root))
        except ValueError:
            rel = str(p)
        digest = _sha256_file(p)
        if digest:
            files.append({"path": rel, "sha256": digest})
    combined = (
        hashlib.sha256("\n".join(f"{f['path']}:{f['sha256']}" for f in files).encode()).hexdigest()
        if files
        else ""
    )
    return combined, files


def _cli_version(binary: str) -> str:
    resolved = shutil.which(binary) or (binary if Path(binary).exists() else None)
    if not resolved:
        return "not found"
    for flag in ("--version", "version", "-v"):
        try:
            proc = subprocess.run(
                [resolved, flag],
                capture_output=True,
                text=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
            )
            out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            line = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
            if line and proc.returncode == 0:
                return line[:200]
        except Exception:
            continue
    return "unknown"


def collect_provenance(config: ExperimentConfig, tasks: list[TaskConfig]) -> dict[str, Any]:
    """Hashes/snapshots of skills, prompts, fixtures, graders + CLI versions."""
    skill_hashes: list[dict[str, Any]] = []
    combined_skill = hashlib.sha256()
    for d in config.skill_dirs:
        h, files = _hash_dir(d)
        skill_hashes.append({"dir": str(d), "hash": h, "files": files[:200]})
        combined_skill.update(h.encode())
    skill_hash = combined_skill.hexdigest() if skill_hashes else ""

    task_entries: list[dict[str, Any]] = []
    tasks_combined = hashlib.sha256()
    for t in tasks:
        prompt_sha = hashlib.sha256(t.prompt.encode()).hexdigest()
        task_file_sha = _sha256_file(t.source_path) if t.source_path else None
        grader_sha = None
        fixture_info: dict[str, Any] = {}
        if t.source_path:
            task_dir = t.source_path.parent
            # Grader file referenced by command (best effort): hash sibling graders.
            graders_dir = (
                task_dir.parent / "graders" if task_dir.name == "tasks" else task_dir / "graders"
            )
            if graders_dir.is_dir():
                gh, _ = _hash_dir(graders_dir, limit_files=200)
                grader_sha = gh
            if t.repo and not config.pr:
                fixture = (task_dir / t.repo).resolve() if t.repo else None
                if fixture and fixture.exists() and fixture.is_dir():
                    fh, files = _hash_dir(fixture, limit_files=500)
                    fixture_info = {"hash": fh, "files": len(files)}
                    tasks_combined.update(fh.encode())
        entry = {
            "id": t.id,
            "category": getattr(t, "category", "general"),
            "prompt_sha256": prompt_sha,
            "task_file": str(t.source_path) if t.source_path else None,
            "task_file_sha256": task_file_sha,
            "grader": t.grader.command if t.grader else None,
            "graders_hash": grader_sha,
            "fixture": fixture_info or None,
        }
        task_entries.append(entry)
        tasks_combined.update(prompt_sha.encode())
        tasks_combined.update(str(t.grader.command if t.grader else "").encode())
        if task_file_sha:
            tasks_combined.update(task_file_sha.encode())

    try:
        runner = AgentRunner()
        binary = runner.binary_for(config.harness, config)
    except Exception:
        binary = config.harness
    agent_cli = {config.harness: _cli_version(binary)}

    return {
        "skill_hash": skill_hash,
        "skills": skill_hashes,
        "tasks_hash": tasks_combined.hexdigest(),
        "task_files": task_entries,
        "agent_cli": agent_cli,
        "skilldiff_version": __version__,
        "system": {"os": platform.system(), "python": platform.python_version()},
    }


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
        try:
            provenance = collect_provenance(self.config, self.tasks)
        except Exception:
            provenance = {}
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
            "thresholds": dict(getattr(self.config, "thresholds", {}) or {}),
            "claude": asdict(self.config.claude),
            "codex": asdict(self.config.codex),
            "opencode": asdict(self.config.opencode),
            "antigravity": asdict(self.config.antigravity),
            "tasks": [
                {
                    "id": t.id,
                    "repo": t.repo,
                    "grader": t.grader.command if t.grader else None,
                    "category": getattr(t, "category", "general"),
                }
                for t in self.tasks
            ],
            "timestamp": timestamp,
            "system": {
                "os": platform.system(),
                "python": platform.python_version(),
            },
            "provenance": provenance,
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
                    "task_category": getattr(task, "category", "general"),
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
                    "grade_status": getattr(grade, "grade_status", "graded"),
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
        warnings.extend(
            _run_warnings(
                control_runs,
                treatment_runs,
                self.tasks,
                treatment_label="treatment" if self.comparison else "skill",
            )
        )
        if interrupted:
            warnings.append(
                f"The experiment was interrupted after {len(control_runs)} of "
                f"{len(self.config.models) * len(self.tasks) * self.config.runs} pairs."
            )

        try:
            provenance = collect_provenance(self.config, self.tasks)
        except Exception:
            provenance = {}
        task_categories = {t.id: getattr(t, "category", "general") for t in self.tasks}
        task_details = [
            {
                "id": t.id,
                "category": getattr(t, "category", "general"),
                "repo": t.repo,
                "grader": t.grader.command if t.grader else None,
            }
            for t in self.tasks
        ]
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
            "task_categories": task_categories,
            "task_details": task_details,
            "tasks_count": len(self.tasks),
            "runs_per_arm": self.config.runs,
            "interrupted": interrupted,
            "warnings": warnings,
            "settings": _settings_summary(self.config),
            "thresholds": dict(getattr(self.config, "thresholds", {}) or {}),
            "provenance": provenance,
            "by_model": by_model,
            "by_category": by_category,
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
    if run.get("grade_status") in ("ungraded", "timeout", "error") or run.get("score") is None:
        label = {"ungraded": "ungraded", "timeout": "grader timeout", "error": "grader error"}.get(
            str(run.get("grade_status") or ""), "N/A"
        )
        dur = run.get("duration")
        dur_txt = f" in {float(dur):.0f}s" if dur is not None else ""
        text = f"{label}{dur_txt}"
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
        "timeout_seconds": config.timeout_seconds,
        "parallel": config.parallel,
        **{k: v for k, v in harness_cfg.items() if v not in (None, [], "")},
    }
    thresholds = dict(getattr(config, "thresholds", {}) or {})
    if thresholds:
        out["thresholds"] = thresholds
    return out


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
            # Distinguish infrastructure failures (agent error/timeout) from
            # agent failures (low scores on completed runs). Failed sessions
            # were still graded on partial work when possible.
            graded = sum(1 for r in failed if r.get("grade_status") == "graded")
            ungraded = len(failed) - graded
            msg = (
                f"{len(failed)} of {len(runs)} {arm_name} runs ended with "
                f"{' or '.join(kinds)} (agent infrastructure failure, not a low score)"
            )
            if graded:
                msg += f"; {graded} were still graded on partial work"
            if ungraded:
                msg += f"; {ungraded} have N/A scores (grading unavailable)"
            msg += "."
            if sample:
                msg += f" First error: {str(sample).strip().splitlines()[0][:200]}"
            warnings.append(msg)
        grade_failed = [r for r in runs if r.get("grade_status") in ("timeout", "error")]
        if grade_failed:
            kinds = sorted({str(r.get("grade_status")) for r in grade_failed})
            warnings.append(
                f"{len(grade_failed)} of {len(runs)} {arm_name} runs have grader "
                f"{' or '.join(kinds)}; their scores are N/A and excluded from means. "
                "These are evaluation-infra failures, shown with valid-pair counts."
            )

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
            "No grader is configured for " + ", ".join(ungraded) + "; those runs score "
            "N/A (not 100%), so only cost and time are compared. Valid-pair counts shown."
        )
    return warnings

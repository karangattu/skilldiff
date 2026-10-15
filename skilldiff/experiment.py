import functools
import hashlib
import json
import os
import platform
import random
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from skilldiff import __version__
from skilldiff.aggregation import (
    _arm_labels,
    _run_summary,
    _run_warnings,
    _settings_summary,
    aggregate_results,
)
from skilldiff.config import ExperimentConfig, TaskConfig
from skilldiff.grader import Candidate, Grader
from skilldiff.host_exposure import host_exposure_warnings
from skilldiff.pairing import (
    _Pair,
    balanced_arm_order,
    balanced_three_order,
    postprocess_grade,
    run_pair,
    safe_path_component,
    save_run_artifacts,
)
from skilldiff.persistence import atomic_json, read_json, run_lock
from skilldiff.reporter import create_reports
from skilldiff.revisions import resolve_comparison
from skilldiff.runner import (
    AgentRunner,
    cached_which,
    resolve_container_image,
    validate_container_auth,
)
from skilldiff.scope import is_blast_violation
from skilldiff.snapshots import (
    SNAPSHOT_VERSION,
    create_snapshots,
    file_hash,
    grader_inputs,
    input_sources,
    load_snapshots,
    snapshot_paths,
    tree_contents,
)
from skilldiff.workspace import user_skill_paths

MAX_STORED_ERROR = 2000


def find_user_level_installs(skill_names: list[str], harness: str) -> list[str]:
    """User-level copies of the skill that the control arm could also load."""
    return [str(path) for path in user_skill_paths(skill_names, harness)]


def find_harness_inheritance(skill_names: list[str], harness: str) -> list[str]:
    """Harness-specific instructions/plugins/memory that can leak into both arms."""
    try:
        from skilldiff.workspace import find_harness_contamination

        return find_harness_contamination(skill_names, harness)
    except Exception:
        return []


def _sha256_file(path: Path) -> str:
    return file_hash(path)


def _hash_dir(
    root: Path, store_limit: int = 200, *, grader: bool = False
) -> tuple[str, list[dict[str, Any]], int]:
    contents = tree_contents(root, grader=grader)
    hashed = [item for item in contents if item["type"] != "directory"]
    combined = hashlib.sha256(json.dumps(contents, sort_keys=True).encode()).hexdigest()
    return combined, hashed[:store_limit], len(hashed)


def _hash_dependency_locks(task_dir: Path | None) -> dict[str, str]:
    """Hash existing lockfiles that affect evaluated inputs, failing on unreadable files."""
    locks: dict[str, str] = {}
    if task_dir is None:
        return locks
    candidates = [
        task_dir / name
        for name in (
            "requirements.txt",
            "requirements.lock",
            "uv.lock",
            "poetry.lock",
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "pyproject.toml",
        )
    ]
    # Also check parent (experiment root) for locks.
    candidates.extend(
        [task_dir.parent / name for name in ("uv.lock", "requirements.txt", "pyproject.toml")]
    )
    for p in candidates:
        if p.is_file():
            locks[str(p)] = _sha256_file(p)
    return locks


def _dir_bytes(root: Path) -> int:
    """Total source bytes under root (regular files only)."""
    total = 0
    try:
        if not root.is_dir():
            return 0
        for p in root.rglob("*"):
            try:
                if p.is_file() and not p.is_symlink():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


@functools.lru_cache(maxsize=64)
def _cli_version(binary: str) -> str:
    resolved = cached_which(binary) or (binary if Path(binary).exists() else None)
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
            out = (proc.stdout or proc.stderr or "").strip()
            line = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
            if line and proc.returncode == 0:
                return line[:200]
        except Exception:
            continue
    return "unknown"


def collect_provenance(config: ExperimentConfig, tasks: list[TaskConfig]) -> dict[str, Any]:
    """Hashes of skills, prompts, fixtures, graders and CLI versions.

    The combined task hash includes grader contents and dependency locks, so
    `compare` can detect incompatible experiments. All files are hashed;
    nothing is silently capped. The runner separately freezes skill and fixture
    files and verifies that the snapshot matches this provenance before execution.
    """
    skill_hashes: list[dict[str, Any]] = []
    combined_skill = hashlib.sha256()
    # In A/B mode record each revision separately plus the combined hash.
    skill_groups: list[tuple[str, list[Path]]] = []
    if config.is_skill_comparison:
        skill_groups = [
            ("skill_a", config.skill_a_dirs),
            ("skill_b", config.skill_b_dirs),
        ]
    else:
        skill_groups = [("skill", config.skill_dirs)]
    for label, dirs in skill_groups:
        for d in dirs:
            h, files, total = _hash_dir(d)
            skill_hashes.append(
                {
                    "dir": str(d),
                    "hash": h,
                    "files": files,
                    "total_files": total,
                    "role": label,
                    "source_bytes": _dir_bytes(d),
                }
            )
            combined_skill.update(f"{label}:{h}".encode())
    skill_hash = combined_skill.hexdigest() if skill_hashes else ""
    # Source-size reduction is recorded separately from session tokens/cost/time
    # so compression reports can show static shrinkage alongside runtime savings.
    source_bytes_by_role: dict[str, int] = {}
    for entry in skill_hashes:
        role = str(entry.get("role") or "")
        source_bytes_by_role[role] = source_bytes_by_role.get(role, 0) + int(
            entry.get("source_bytes") or 0
        )

    task_entries: list[dict[str, Any]] = []
    tasks_combined = hashlib.sha256()
    for t in tasks:
        definition = asdict(t)
        definition.pop("source_path", None)
        tasks_combined.update(json.dumps(definition, sort_keys=True).encode())
        prompt_sha = hashlib.sha256(t.prompt.encode()).hexdigest()
        task_file_sha = _sha256_file(t.source_path) if t.source_path else None
        grader_sha = None
        grader_files_total = 0
        locks: dict[str, str] = {}
        fixture_info: dict[str, Any] = {}
        if t.source_path:
            task_dir = t.source_path.parent
            # Grader file referenced by command (best effort): hash sibling graders.
            graders_dir = (
                task_dir.parent / "graders" if task_dir.name == "tasks" else task_dir / "graders"
            )
            if graders_dir.is_dir():
                gh, gfiles, gtotal = _hash_dir(graders_dir, grader=True)
                grader_sha = gh
                grader_files_total = gtotal
            locks = _hash_dependency_locks(task_dir)
            if t.repo and not config.pr:
                fixture = (task_dir / t.repo).resolve() if t.repo else None
                if fixture and fixture.exists() and fixture.is_dir():
                    fh, ffiles, ftotal = _hash_dir(fixture)
                    fixture_info = {"hash": fh, "files": len(ffiles), "total_files": ftotal}
                    tasks_combined.update(fh.encode())
        entry = {
            "id": t.id,
            "category": getattr(t, "category", "general"),
            "prompt_sha256": prompt_sha,
            "task_file": str(t.source_path) if t.source_path else None,
            "task_file_sha256": task_file_sha,
            "grader": t.grader.command if t.grader else None,
            "graders_hash": grader_sha,
            "graders_total_files": grader_files_total,
            "locks": locks or None,
            "fixture": fixture_info or None,
            "validation": getattr(t, "validation", {}) or None,
        }
        if getattr(t, "grader_ignore", None):
            entry["grader_ignore"] = list(t.grader_ignore)
        task_entries.append(entry)
        tasks_combined.update(prompt_sha.encode())
        tasks_combined.update(str(t.grader.command if t.grader else "").encode())
        if grader_sha:
            tasks_combined.update(grader_sha.encode())
        for lock_name in sorted(locks):
            tasks_combined.update(f"{lock_name}:{locks[lock_name]}".encode())
        if task_file_sha:
            tasks_combined.update(task_file_sha.encode())

    try:
        runner = AgentRunner()
        binary = runner.binary_for(config.harness, config)
    except Exception:
        binary = config.harness
    if config.isolation in {"docker", "podman"}:
        configured = getattr(config, config.harness).bin_path
        image_binary = configured or Path(binary).name
        with tempfile.TemporaryDirectory(prefix="skilldiff-version-") as tmp:
            result = runner._exec(
                [image_binary, "--version"], Path(tmp), os.environ.copy(), 10,
                isolation=config.isolation, container_image=config.container_image,
            )
        if result.exit_code or result.timed_out:
            raise ValueError("Cannot inspect container agent CLI version: " + result.stderr.strip())
        agent_cli = {config.harness: (result.stdout or result.stderr).strip()}
    else:
        agent_cli = {config.harness: _cli_version(binary)}

    return {
        "skill_hash": skill_hash,
        "skills": skill_hashes,
        "source_bytes": source_bytes_by_role,
        "tasks_hash": tasks_combined.hexdigest(),
        "task_files": task_entries,
        "agent_cli": agent_cli,
        "skilldiff_version": __version__,
        "system": {"os": platform.system(), "python": platform.python_version()},
    }




class ExperimentRunner:
    def __init__(
        self,
        config: ExperimentConfig,
        tasks: list[TaskConfig],
        output_dir: Path | None = None,
        agent_runner: AgentRunner | None = None,
        progress: Optional[Callable[[str], None]] = None,
        resume_dir: Path | None = None,
    ):
        self.config = deepcopy(config)
        self.tasks = deepcopy(tasks)
        if output_dir is None:
            base = config.config_path.parent if config.config_path else Path(".")
            output_dir = base / "runs"
        self.output_dir = output_dir
        self.agent_runner = agent_runner or AgentRunner()
        self.progress = progress or (lambda _msg: None)
        self._lock = threading.Lock()
        self.comparison = None
        self.resume_dir = resume_dir
        self._baseline_runs: list[dict[str, Any]] = []
        self._retry_history: list[dict[str, Any]] = []
        self._seed: int = (
            int(config.seed) if config.seed is not None else random.randint(0, 2**31 - 1)
        )
        self._provenance_snapshot: dict[str, Any] = {}
        self._checkpoint_completed: set[tuple[str, str, int]] = set()
        self._execution_config = self.config
        self._snapshot_paths: dict[Path, Path] = {}
        self._fixture_snapshots: dict[str, Path] = {}
        self._mounted_fixture_snapshots: dict[str, list[Path]] = {}
        self._record_hashes: dict[str, str] = {}
        self._grader_inputs: dict[str, Any] = {}
        self._snapshot_manifest_hash: str | None = None
        self._timestamp = ""
        self._stopped = threading.Event()
        self._container_image_id: str | None = None

    # ------------------------------------------------------------------ setup

    def preflight_warnings(self) -> list[str]:
        warnings = host_exposure_warnings(
            self.config, self.output_dir,
            snapshot_skills=(self._execution_config.skill_dirs if self._snapshot_paths else []),
        )
        installs = (
            find_user_level_installs(self.config.skill_names, self.config.harness)
            if self.config.isolation == "local" else []
        )
        isolated = self.config.isolation != "local"
        if installs and not isolated:
            warnings.append(
                "The skill is also installed at user level, so the control arm can load it: "
                + ", ".join(installs)
            )
        # Harness-specific inheritance (instructions/plugins/memory) is checked
        # for skill experiments; PR mode has no skill so generic host files are
        # not flagged here (they are still noted by `check` when relevant).
        if (self.config.isolation == "local" and
                (self.config.skill or self.config.is_skill_comparison) and self.config.skill_names):
            extra = [
                i
                for i in find_harness_inheritance(self.config.skill_names, self.config.harness)
                if "skill installed" not in i
            ]
            if extra and not isolated:
                warnings.append(
                    "Possible harness inheritance outside skills (instructions/plugins/memory): "
                    + ", ".join(extra)
                )
        # A/B mode: both revisions must differ.
        if self.config.is_skill_comparison:
            try:
                prov = self._provenance_snapshot or collect_provenance(self.config, self.tasks)
                skills = prov.get("skills") or []
                hashes = {}
                for s in skills:
                    hashes.setdefault(s.get("role"), []).append(s.get("hash"))
                ha = "".join(hashes.get("skill_a", []))
                hb = "".join(hashes.get("skill_b", []))
                if ha and hb and ha == hb:
                    warnings.append(
                        "skill_a and skill_b have identical hashes; A/B compares the same revision."
                    )
            except Exception:
                pass
        return warnings

    def _skill_comparison_info(self) -> dict[str, Any] | None:
        if not self.config.is_skill_comparison:
            return None
        preset = getattr(self.config, "preset", None) or "revision"
        info: dict[str, Any] = {
            "type": "skill_ab",
            "preset": preset,
            "skill_a": str(self.config.skill_a) if self.config.skill_a else None,
            "skill_b": str(self.config.skill_b) if self.config.skill_b else None,
            "include_baseline": bool(self.config.include_baseline),
            "control_is": "skill_a",
            "treatment_is": "skill_b",
        }
        # Source-size reduction lives alongside (not instead of) session
        # tokens/cost/time so compression reports show static shrinkage
        # separately from runtime savings.
        try:
            prov = self._provenance_snapshot or {}
            sizes = dict(prov.get("source_bytes") or {})
            bytes_a = int(sizes.get("skill_a") or 0)
            bytes_b = int(sizes.get("skill_b") or 0)
            if bytes_a or bytes_b:
                info["source_bytes_a"] = bytes_a
                info["source_bytes_b"] = bytes_b
                if bytes_a > 0:
                    info["source_pct_change"] = (bytes_b - bytes_a) / bytes_a * 100
                    info["source_reduction_pct"] = (bytes_a - bytes_b) / bytes_a * 100
        except Exception:
            pass
        return info

    def _metadata(self, timestamp: str) -> dict[str, Any]:
        # Reuse the pre-execution snapshot when run() already collected it, so
        # resume validation and the written metadata cannot diverge.
        if self._provenance_snapshot:
            provenance = self._provenance_snapshot
        else:
            provenance = collect_provenance(self.config, self.tasks)
            self._provenance_snapshot = provenance
        skill_comparison = self._skill_comparison_info()
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "snapshot_manifest_hash": self._snapshot_manifest_hash,
            "name": self.config.name,
            "skilldiff_version": __version__,
            "preset": getattr(self.config, "preset", None),
            "skill": str(self.config.skill) if self.config.skill else None,
            "skill_a": str(self.config.skill_a) if self.config.skill_a else None,
            "skill_b": str(self.config.skill_b) if self.config.skill_b else None,
            "include_baseline": bool(self.config.include_baseline),
            "skill_comparison": skill_comparison,
            "comparison": self.comparison,
            "skill_names": self._execution_config.skill_names,
            "skill_a_names": self._execution_config.skill_a_names,
            "skill_b_names": self._execution_config.skill_b_names,
            "harness": self.config.harness,
            "isolation": self.config.isolation,
            "container_image": self.config.container_image,
            "container_image_id": self._container_image_id,
            "models": self.config.models,
            "runs": self.config.runs,
            "timeout_seconds": self.config.timeout_seconds,
            "parallel": self.config.parallel,
            "thresholds": dict(getattr(self.config, "thresholds", {}) or {}),
            "failure_policy": dict(getattr(self.config, "failure_policy", {}) or {}),
            "pricing": dict(getattr(self.config, "pricing", {}) or {}),
            "cost_basis": self.config.cost_basis,
            "seed": self._seed,
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
                    "split": getattr(t, "split", "dev"),
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

    def _checkpoint_path(self, run_root: Path) -> Path:
        return run_root / "checkpoint.json"

    def _arms(self) -> list[str]:
        arms = ["control", "treatment"]
        if self.config.is_skill_comparison and self.config.include_baseline:
            arms.append("baseline")
        return arms

    @staticmethod
    def _arm_directory(root: Path, key: tuple[str, str, int], arm: str) -> Path:
        model, task, rep = key
        return root / safe_path_component(model) / safe_path_component(task) / arm / f"{rep:03d}"

    def _write_checkpoint(self, run_root: Path) -> None:
        atomic_json(self._checkpoint_path(run_root), {
            "version": 1,
            "completed": [list(key) for key in sorted(self._checkpoint_completed)],
            "seed": self._seed,
            "record_hashes": self._record_hashes,
            "experiment": self._metadata(self._timestamp),
        })

    def _resume_mismatches(
        self, prev_exp: dict[str, Any]
    ) -> list[str]:
        """Reasons the current config cannot resume the previous run."""
        reasons: list[str] = []
        prev_prov = (prev_exp.get("provenance") or {})
        cur_skill = self._provenance_snapshot.get("skill_hash")
        cur_tasks = self._provenance_snapshot.get("tasks_hash")
        if cur_skill != prev_prov.get("skill_hash"):
            reasons.append(
                "skill hashes differ "
                f"(previous {str(prev_prov.get('skill_hash'))[:12]}, "
                f"current {str(cur_skill)[:12]})"
            )
        if cur_tasks != prev_prov.get("tasks_hash"):
            reasons.append("task/prompt/fixture/grader hashes differ")
        # PR revisions and workflow: resuming across different commits would
        # silently mix revisions.
        prev_comp = prev_exp.get("comparison")
        cur_comp = self.comparison
        if (prev_comp is None) != (cur_comp is None):
            reasons.append("PR mode changed between runs")
        elif prev_comp and cur_comp:
            for key in (
                "control_commit", "treatment_commit", "base_tip", "head_commit",
                "merge_base", "mode", "pair", "repo",
            ):
                if prev_comp.get(key) != cur_comp.get(key):
                    reasons.append(
                        f"PR {key} changed ({prev_comp.get(key)} -> {cur_comp.get(key)})"
                    )
                    break
        # Execution settings that change what was measured.
        if list(prev_exp.get("models") or []) != list(self.config.models):
            reasons.append(
                f"models changed ({prev_exp.get('models')} -> {list(self.config.models)})"
            )
        previous_tasks = [
            t.get("id") if isinstance(t, dict) else t for t in prev_exp.get("tasks", [])
        ]
        if previous_tasks != [t.id for t in self.tasks]:
            reasons.append("task list changed")
        if (prev_exp.get("harness") or None) != self.config.harness:
            reasons.append(
                f"harness changed ({prev_exp.get('harness')} -> {self.config.harness})"
            )
        for key, cur_val in (
            ("skill", str(self.config.skill) if self.config.skill else None),
            ("skill_a", str(self.config.skill_a) if self.config.skill_a else None),
            ("skill_b", str(self.config.skill_b) if self.config.skill_b else None),
        ):
            if (prev_exp.get(key) or None) != cur_val:
                reasons.append(f"{key} changed ({prev_exp.get(key)} -> {cur_val})")
                break
        if bool(prev_exp.get("include_baseline", False)) != bool(
            self.config.include_baseline
        ):
            reasons.append("include_baseline changed")
        if (prev_exp.get("preset") or "skill") != (
            getattr(self.config, "preset", None) or "skill"
        ):
            reasons.append(
                f"preset changed ({prev_exp.get('preset')} -> "
                f"{getattr(self.config, 'preset', None)})"
            )
        for key in ("timeout_seconds", "parallel", "thresholds", "failure_policy",
                    "container_image", "pricing"):
            if prev_exp.get(key) != getattr(self.config, key):
                reasons.append(f"{key} changed")
        if prev_exp.get("cost_basis", "harness") != self.config.cost_basis:
            reasons.append("cost_basis changed")
        if prev_exp.get("isolation", "local") != self.config.isolation:
            reasons.append("isolation changed")
        if prev_exp.get("container_image_id") != self._container_image_id:
            reasons.append("container image identity changed")
        harness = self.config.harness
        current_harness = asdict(getattr(self.config, harness))
        previous_harness = prev_exp.get(harness) or {}
        for key in sorted(set(current_harness) | set(previous_harness)):
            if current_harness.get(key) != previous_harness.get(key):
                reasons.append(f"{harness}.{key} changed")
        if self.config.seed is not None and self.config.seed != prev_exp.get("seed"):
            reasons.append("seed changed")
        if self.config.runs < int(prev_exp.get("runs", 0)):
            reasons.append("runs decreased")
        if prev_exp.get("skilldiff_version") != __version__:
            reasons.append("skilldiff_version changed")
        if prev_prov.get("agent_cli") != self._provenance_snapshot.get("agent_cli"):
            reasons.append("agent_cli version changed")
        if (prev_exp.get("system") or {}).get("python") != platform.python_version():
            reasons.append("python version changed")
        return reasons

    def _load_resume_state(self, resume_dir: Path) -> set[tuple[str, str, int]]:
        previous = read_json(resume_dir / "experiment.json")
        checkpoint = read_json(resume_dir / "checkpoint.json")
        if checkpoint.get("version") != 1 or not isinstance(checkpoint.get("experiment"), dict):
            raise ValueError("Cannot resume without versioned checkpoint metadata; start a new run")
        mismatches = self._resume_mismatches(checkpoint["experiment"])
        if (checkpoint.get("seed") != previous.get("seed")
                or checkpoint["experiment"].get("seed") != previous.get("seed")):
            mismatches.append("checkpoint seed differs from metadata")
        if checkpoint["experiment"].get("snapshot_manifest_hash") != self._snapshot_manifest_hash:
            mismatches.append("checkpoint snapshot differs from metadata")
        if mismatches:
            raise ValueError(
                "Resume refused: incompatible checkpoint (" + "; ".join(mismatches) + ")"
            )
        hashes = checkpoint.get("record_hashes")
        if not isinstance(hashes, dict):
            raise ValueError("Missing completed arm record hashes in checkpoint")
        self._record_hashes = dict(hashes)
        completed: set[tuple[str, str, int]] = set()
        allowed = {
            (model, task.id, rep)
            for model in self.config.models for task in self.tasks
            for rep in range(1, int(previous["runs"]) + 1)
        }
        items = checkpoint.get("completed")
        if not isinstance(items, list):
            raise ValueError("Invalid completed pairs in checkpoint")
        for item in items:
            if not isinstance(item, list) or len(item) != 3:
                raise ValueError("Invalid completed pair in checkpoint")
            key = tuple(item)
            if (not isinstance(item[0], str) or not isinstance(item[1], str)
                    or type(item[2]) is not int or key not in allowed or key in completed):
                raise ValueError(f"Invalid or duplicate completed pair in checkpoint: {item}")
            completed.add(key)
        return completed

    def _load_previous_runs(
        self, resume_dir: Path, completed: set[tuple[str, str, int]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        runs: dict[str, list[dict[str, Any]]] = {"control": [], "treatment": [], "baseline": []}
        expected_records = set()
        for model in self.config.models:
            for task in self.tasks:
                for rep in range(1, self.config.runs + 1):
                    key = (model, task.id, rep)
                    for arm in self._arms():
                        directory = self._arm_directory(resume_dir, key, arm)
                        if key not in completed:
                            if directory.exists():
                                raise ValueError(
                                    f"Incomplete saved pair at {directory}; artifacts preserved. "
                                    "Start a new run; sessions will not be rerun automatically."
                                )
                            continue
                        record = read_json(directory / "run.json")
                        relative = str((directory / "run.json").relative_to(resume_dir))
                        expected_records.add(relative)
                        if file_hash(directory / "run.json") != self._record_hashes.get(relative):
                            raise ValueError(f"Changed saved arm record: {directory / 'run.json'}")
                        identity = (
                            record.get("model"), record.get("task_id"), record.get("repetition")
                        )
                        if (identity != key or record.get("arm") != arm
                                or not record.get("complete") or "grade_status" not in record):
                            raise ValueError(
                                f"Invalid completed arm record: {directory / 'run.json'}"
                            )
                        for name in ("transcript.txt", "diff.patch"):
                            artifact = directory / name
                            expected = (record.get("artifact_hashes") or {}).get(name)
                            try:
                                matches = expected and file_hash(artifact) == expected
                            except OSError:
                                matches = False
                            if not matches:
                                raise ValueError(f"Missing or changed saved artifact: {artifact}")
                        runs[arm].append(record)
        if expected_records != set(self._record_hashes):
            raise ValueError("Checkpoint record hashes do not match its completed pairs")
        self._baseline_runs = runs["baseline"]
        return runs["control"], runs["treatment"]

    def _check_graders(self) -> None:
        if grader_inputs(self.tasks) != self._grader_inputs:
            raise ValueError("Tracked grader files or dependency locks changed; pair not completed")

    def _use_snapshots(self, run_root: Path, manifest: dict[str, Any]) -> None:
        self._snapshot_paths = snapshot_paths(run_root, manifest)
        self._fixture_snapshots = {
            task.id: self._snapshot_paths[(task.source_path.parent / task.repo).resolve()]
            for task in self.tasks
            if task.source_path and task.repo and not self.config.pr
        }
        self._execution_config = deepcopy(self.config)
        for field in ("skill", "skill_a", "skill_b"):
            source = getattr(self.config, field)
            if source:
                setattr(self._execution_config, field, self._snapshot_paths[source.resolve()])
        self._mounted_fixture_snapshots = {}
        for task in self.tasks:
            snapshots = []
            for fixture in getattr(task, "fixtures", []) or []:
                if task.source_path:
                    source = (task.source_path.parent / fixture).resolve()
                    snapshots.append(self._snapshot_paths.get(source, source))
            self._mounted_fixture_snapshots[task.id] = snapshots
        if self._container_image_id:
            self._execution_config.container_image = self._container_image_id

    def run(self, resume: bool | Path = False) -> dict[str, Any]:
        if self.config.isolation not in {"local", "docker", "podman", "macos"}:
            raise ValueError(f"Unsupported isolation mode: {self.config.isolation}")
        if self.config.isolation == "macos":
            if platform.system() != "Darwin" or self.config.harness not in {"codex", "claude"}:
                raise ValueError("isolation: macos requires macOS and the Codex or Claude harness")
        validate_container_auth(self.config)
        if self.config.isolation in {"docker", "podman"}:
            self._container_image_id = resolve_container_image(
                self.config.isolation, self.config.container_image
            )
        for label, values in (
            ("models", self.config.models), ("tasks", [task.id for task in self.tasks])
        ):
            components = [safe_path_component(value) for value in values]
            if any(c in {".", "..", "inputs"} for c in components):
                raise ValueError(f"Reserved output path in {label}")
            if len(set(components)) != len(components):
                raise ValueError(f"Output path collision in {label}: {values}")
        self._timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S%fZ")
        resume_dir = resume if isinstance(resume, Path) else self.resume_dir
        if resume is True and resume_dir is None:
            candidates = sorted(
                p for p in self.output_dir.glob("*") if p.is_dir()
                and (p / "experiment.json").exists()
            )
            if candidates:
                resume_dir = candidates[-1]
        if resume_dir is not None:
            run_root = Path(resume_dir).resolve()
            if not run_root.is_dir():
                raise ValueError(f"Resume directory does not exist: {run_root}")
        else:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            run_root = Path(tempfile.mkdtemp(
                prefix=self._timestamp + "-", dir=self.output_dir
            )).resolve()
        with run_lock(run_root):
            try:
                return self._run_locked(run_root, resume_dir is not None)
            except BaseException:
                self._stopped.set()
                self.agent_runner.terminate_all()
                raise

    def _run_locked(self, run_root: Path, resuming: bool) -> dict[str, Any]:
        previous = read_json(run_root / "experiment.json") if resuming else None
        if previous is not None and previous.get("snapshot_version") != SNAPSHOT_VERSION:
            raise ValueError(
                "Cannot resume a run without frozen-input metadata; start a new run"
            )
        self.comparison = (
            resolve_comparison(self.config.pr, previous=(previous or {}).get("comparison"))
            if self.config.pr else None
        )
        sources = input_sources(self.config, self.tasks)
        if any(run_root.is_relative_to(source) for source in sources):
            raise ValueError("Run directory must be outside skill and fixture input directories")
        self._grader_inputs = grader_inputs(self.tasks)
        provenance_config = deepcopy(self.config)
        if self._container_image_id:
            provenance_config.container_image = self._container_image_id
        self._provenance_snapshot = collect_provenance(provenance_config, self.tasks)
        control_runs: list[dict[str, Any]] = []
        treatment_runs: list[dict[str, Any]] = []
        completed: set[tuple[str, str, int]] = set()
        if resuming:
            mismatches = self._resume_mismatches(previous)
            if mismatches:
                raise ValueError(
                    "Resume refused: incompatible experiment (" + "; ".join(mismatches) + ")"
                )
            self._snapshot_manifest_hash = previous.get("snapshot_manifest_hash")
            if file_hash(run_root / "inputs/manifest.json") != self._snapshot_manifest_hash:
                raise ValueError("Snapshot manifest changed; resume refused")
            manifest = load_snapshots(run_root, sources)
            if manifest.get("grader_inputs") != self._grader_inputs:
                raise ValueError("Tracked grader files or dependency locks differ from snapshot")
            completed = self._load_resume_state(run_root)
            control_runs, treatment_runs = self._load_previous_runs(run_root, completed)
            self._seed = previous["seed"]
            self._timestamp = previous["timestamp"]
        else:
            manifest = create_snapshots(
                run_root,
                sources,
                self._grader_inputs,
                reflink=getattr(self.config, "reflink", False),
            )
            self._check_graders()
            # Detect changes between provenance collection and copying, including task files.
            after = collect_provenance(provenance_config, self.tasks)
            if after != self._provenance_snapshot:
                raise ValueError("Inputs changed while preparing snapshots; start a new run")
            self._snapshot_manifest_hash = file_hash(run_root / "inputs/manifest.json")
        self._use_snapshots(run_root, manifest)
        self._checkpoint_completed = set(completed)
        warnings = self.preflight_warnings()
        for warning in warnings:
            self.progress(f"warning: {warning}")
        # Every resume validation above is read-only. Publication begins here.
        atomic_json(run_root / "experiment.json", self._metadata(self._timestamp))
        self._write_checkpoint(run_root)
        pairs = [
            _Pair(model, task, rep)
            for model in self.config.models for task in self.tasks
            for rep in range(1, self.config.runs + 1)
            if (model, task.id, rep) not in completed
        ]
        done = len(completed)
        total = done + len(pairs)
        if done:
            self.progress(f"Resuming: {done} completed pair(s) reused (inputs verified).")
        interrupted = False

        def record(pair_result: tuple[dict[str, Any], dict[str, Any]]) -> None:
            nonlocal done
            ctrl, treat = pair_result
            key = (ctrl["model"], ctrl["task_id"], ctrl["repetition"])
            with self._lock:
                previous_hashes = dict(self._record_hashes)
                for arm in self._arms():
                    path = self._arm_directory(run_root, key, arm) / "run.json"
                    self._record_hashes[str(path.relative_to(run_root))] = file_hash(path)
                self._checkpoint_completed.add(key)
                try:
                    self._write_checkpoint(run_root)
                except BaseException:
                    self._checkpoint_completed.remove(key)
                    self._record_hashes = previous_hashes
                    raise
                control_runs.append(ctrl)
                treatment_runs.append(treat)
                done += 1
                spent = sum(
                    float(r.get("cost") or 0.0) for r in control_runs + treatment_runs
                )
                self.progress(
                    f"[{done}/{total}] {ctrl['model']} · {ctrl['task_id']} · "
                    f"run {ctrl['repetition']}: control {_run_summary(ctrl)} | "
                    f"treatment {_run_summary(treat)} [spent: ${spent:.2f}]"
                )

        terminated_on_ceiling = False
        ceiling_pairs: set[tuple[str, str]] = set()
        try:
            if self.config.parallel <= 1:
                for pair in pairs:
                    if (
                        getattr(self.config, "terminate_on_ceiling", False)
                        and (pair.model, pair.task.id) in ceiling_pairs
                    ):
                        continue
                    res_pair = self._run_pair(pair, run_root)
                    record(res_pair)
                    ctrl_res = res_pair[0]
                    if (
                        getattr(self.config, "terminate_on_ceiling", False)
                        and ctrl_res.get("score") is not None
                        and float(ctrl_res["score"]) >= 1.0
                    ):
                        ceiling_pairs.add((pair.model, pair.task.id))
                        terminated_on_ceiling = True
                        self.progress(
                            f"Ceiling reached on {pair.model} · {pair.task.id} (control 100%); "
                            "skipping remaining repetitions for this task."
                        )
            else:
                self.progress(
                    f"Running {len(pairs)} pair(s) across {self.config.parallel} worker(s)..."
                )
                pool = ThreadPoolExecutor(max_workers=self.config.parallel)
                futures = [pool.submit(self._run_pair, p, run_root) for p in pairs]
                try:
                    for future in as_completed(futures):
                        record(future.result())
                except BaseException:
                    self._stopped.set()
                    pool.shutdown(wait=False, cancel_futures=True)
                    self.agent_runner.terminate_all()
                    pool.shutdown(wait=True)
                    raise
                pool.shutdown()
        except KeyboardInterrupt:
            interrupted = True
            self.progress("Interrupted: writing a report for the completed pairs...")
        # A parallel baseline may finish before its pair can be checkpointed.
        self._baseline_runs = [
            r for r in self._baseline_runs
            if (r["model"], r["task_id"], r["repetition"]) in self._checkpoint_completed
        ]
        if terminated_on_ceiling:
            warnings.append(
                "Terminated early on ceiling: one or more tasks already scored 100% in control."
            )
        results = self._aggregate(
            run_root, self._timestamp, control_runs, treatment_runs, warnings,
            interrupted, provenance_snapshot=self._provenance_snapshot,
        )
        if terminated_on_ceiling:
            results["terminated_on_ceiling"] = True
        paths = create_reports(results, run_root)
        results["report"] = {k: (str(v) if v else None) for k, v in paths.items()}
        atomic_json(run_root / "results.json", results)
        return results

    def _make_grader(
        self,
        task: TaskConfig,
        model: str,
        execution: ExperimentConfig,
        grader_names: list[str],
    ) -> Grader:
        task_dir = task.source_path.parent if task.source_path else None
        extra_env = dict(getattr(execution, "env", None) or {})
        extra_env.update(getattr(task, "env", None) or {})
        return Grader(
            task.grader,
            grader_names,
            model,
            task_dir=task_dir,
            allowed_paths=getattr(task, "allowed_paths", None),
            forbidden_paths=getattr(task, "forbidden_paths", None),
            grader_ignore=getattr(task, "grader_ignore", None),
            task_prompt=task.prompt,
            isolation="local" if execution.isolation == "macos" else execution.isolation,
            container_image=execution.container_image,
            inputs_root=self.config.config_path.parent if self.config.config_path else None,
            extra_env=extra_env,
        )

    def _grader_names(self, execution: ExperimentConfig) -> list[str]:
        # Skill names differ per mode: A/B uses per-arm names for blind grading.
        if self.config.is_skill_comparison:
            return list(dict.fromkeys(execution.skill_a_names + execution.skill_b_names))
        return execution.skill_names

    def _postprocess_grade(self, record: dict[str, Any]) -> None:
        postprocess_grade(record, getattr(self.config, "failure_policy", {}) or {})

    def _run_pair(self, pair: _Pair, run_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        return run_pair(self, pair, run_root)


    # ---------------------------------------------------------------- regrade

    def regrade(
        self,
        run_root: Path,
        task_ids: Optional[set[str]] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Re-run the current graders on a finished run, without rerunning any agent.

        Each arm's workspace is rebuilt as the run's frozen fixture plus the saved
        `diff.patch`, graded again, and the run's scores and aggregates are rewritten.
        Every regraded record keeps its previous grade, and the report gets a visible
        warning, so a grader fix after the fact stays auditable.
        """
        if self.config.pr:
            raise ValueError("regrade does not support PR experiments (revisions are not frozen)")
        run_root = run_root.resolve()
        old_results = read_json(run_root / "results.json")
        checkpoint = read_json(run_root / "checkpoint.json")
        manifest = read_json(run_root / "inputs/manifest.json")
        if checkpoint.get("version") != 1 or not isinstance(checkpoint.get("record_hashes"), dict):
            raise ValueError("Cannot regrade: run has no versioned checkpoint")
        for entry in manifest.get("sources", []):
            if tree_contents(run_root / entry["path"]) != entry.get("contents"):
                raise ValueError(f"Cannot regrade: frozen input changed: {entry['path']}")
        paths = {Path(e["source"]).resolve(): run_root / e["path"] for e in manifest["sources"]}
        fixtures: dict[str, Path] = {}
        for task in self.tasks:
            if not (task.source_path and task.repo):
                continue
            source = (task.source_path.parent / task.repo).resolve()
            if source not in paths:
                # The experiment directory may have moved since the run: fall back
                # to a unique frozen input with the same directory name.
                same_name = [path for src, path in paths.items() if src.name == source.name]
                if len(same_name) == 1:
                    fixtures[task.id] = same_name[0]
                    continue
                raise ValueError(
                    f"Cannot regrade: task '{task.id}' fixture {source} is not among the "
                    "run's frozen inputs (was the config or fixture path changed?)"
                )
            fixtures[task.id] = paths[source]
        self._execution_config = deepcopy(self.config)
        self._baseline_runs = []
        self.comparison = None
        self._seed = int(old_results.get("seed") or 0)
        arms = self._arms()
        in_scope = [t for t in self.tasks if not task_ids or t.id in task_ids]
        if not in_scope:
            raise ValueError("No tasks selected for regrade")
        grader_names = self._grader_names(self._execution_config)
        completed = [tuple(k) for k in checkpoint.get("completed", [])]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw: dict[str, list[dict[str, Any]]] = {arm: [] for arm in arms}
        changes: list[dict[str, Any]] = []
        skipped: list[str] = []
        regraded = 0
        new_records: dict[Path, dict[str, Any]] = {}

        for model in self.config.models:
            for task in self.tasks:
                for rep in range(1, self.config.runs + 1):
                    key = (model, task.id, rep)
                    if key not in completed:
                        continue
                    dirs = {arm: self._arm_directory(run_root, key, arm) for arm in arms}
                    records = {arm: read_json(dirs[arm] / "run.json") for arm in arms}
                    if task not in in_scope:
                        for arm in arms:
                            raw[arm].append(records[arm])
                        continue
                    grader = self._make_grader(
                        task, model, self._execution_config, grader_names
                    )
                    with tempfile.TemporaryDirectory(prefix="skilldiff-regrade-") as tmp:
                        candidates: list[Candidate] = []
                        failure: Optional[str] = None
                        for arm in arms:
                            workspace = Path(tmp) / arm / "workspace"
                            diff_text = (dirs[arm] / "diff.patch").read_text(encoding="utf-8")
                            try:
                                _rebuild_workspace(fixtures.get(task.id), diff_text, workspace)
                            except ValueError as exc:
                                failure = f"{arm}: {exc}"
                                break
                            candidates.append(
                                Candidate(
                                    "", arm, workspace,
                                    str(records[arm].get("response") or ""),
                                    diff_text,
                                    (dirs[arm] / "transcript.txt").read_text(encoding="utf-8"),
                                    list(records[arm].get("files_changed") or []),
                                )
                            )
                        if failure:
                            skipped.append(f"{model}/{task.id}/rep {rep}: {failure}")
                            for arm in arms:
                                raw[arm].append(records[arm])
                            continue
                        grades = grader.grade_candidates(candidates)
                    for arm in arms:
                        old = records[arm]
                        new = {
                            k: v for k, v in old.items()
                            if k not in {
                                "partial_score", "analysis_excluded", "failure_scored_zero",
                                "grade_error_kind",
                            }
                        }
                        grade = grades[arm]
                        new.update(
                            score=grade.score,
                            success=grade.success,
                            grade_status=getattr(grade, "grade_status", "graded"),
                            blind_label=grade.label,
                            feedback=grade.feedback,
                        )
                        if is_blast_violation(grade.feedback):
                            new["grade_error_kind"] = "blast_radius"
                        self._postprocess_grade(new)
                        history = list(old.get("regrade_history") or [])
                        history.append(
                            {
                                "at": now,
                                "score": old.get("score"),
                                "success": old.get("success"),
                                "grade_status": old.get("grade_status"),
                                "feedback": old.get("feedback"),
                            }
                        )
                        new["regrade_history"] = history
                        regraded += 1
                        if (old.get("score"), old.get("grade_status")) != (
                            new.get("score"), new.get("grade_status")
                        ):
                            changes.append(
                                {
                                    "model": model, "task_id": task.id, "repetition": rep,
                                    "arm": arm,
                                    "previous_score": old.get("score"),
                                    "previous_grade_status": old.get("grade_status"),
                                    "score": new.get("score"),
                                    "grade_status": new.get("grade_status"),
                                }
                            )
                        new_records[dirs[arm]] = new
                        raw[arm].append(new)

        summary = {
            "at": now,
            "tasks": [t.id for t in in_scope],
            "runs_regraded": regraded,
            "runs_changed": len(changes),
            "changes": changes,
            "skipped": skipped,
        }
        if dry_run:
            return summary | {"dry_run": True}

        current = collect_provenance(self.config, self.tasks)
        summary["tasks_hash_before"] = (old_results.get("provenance") or {}).get("tasks_hash")
        summary["tasks_hash_after"] = current.get("tasks_hash")
        record_hashes = dict(checkpoint["record_hashes"])
        for directory, record in new_records.items():
            atomic_json(directory / "run.json", record)
            record_hashes[str((directory / "run.json").relative_to(run_root))] = file_hash(
                directory / "run.json"
            )
        checkpoint["record_hashes"] = record_hashes
        atomic_json(run_root / "checkpoint.json", checkpoint)

        self._baseline_runs = raw.get("baseline", [])
        warnings = list(old_results.get("warnings") or [])
        if regraded or skipped:
            skip_msg = f"; skipped {len(skipped)} (binary diff)" if skipped else ""
            warnings.append(
                f"Regraded {regraded} run(s){skip_msg} on {now} for task(s) "
                f"{', '.join(summary['tasks'])} with updated graders; {len(changes)} run(s) "
                "scored differently than in the original run. Previous grades are kept in "
                "each run record under regrade_history."
            )
        results = self._aggregate(
            run_root, old_results.get("timestamp", ""), raw["control"], raw["treatment"],
            warnings, bool(old_results.get("interrupted")),
            provenance_snapshot=old_results.get("provenance"),
        )
        # Only grade-dependent aggregates are recomputed; run metadata stays as recorded.
        for key in (
            "name", "skilldiff_version", "preset", "arm_labels", "harness", "skill",
            "skill_a", "skill_b", "skill_comparison", "comparison", "skill_names",
            "timestamp", "run_dir", "models", "tasks", "task_categories", "task_details",
            "tasks_count", "runs_per_arm", "seed", "failure_policy", "settings",
            "thresholds", "pricing", "cost_basis", "provenance", "context_tax", "retries",
        ):
            if key in old_results:
                results[key] = old_results[key]
        results["regrades"] = list(old_results.get("regrades") or []) + [summary]
        report_paths = create_reports(results, run_root)
        results["report"] = {k: (str(v) if v else None) for k, v in report_paths.items()}
        atomic_json(run_root / "results.json", results)
        summary["results"] = results
        return summary

    def _ensure_running(self) -> None:
        if self._stopped.is_set():
            raise RuntimeError("Experiment stopped after an earlier failure")

    # -------------------------------------------------------------- aggregate

    def _aggregate(
        self,
        run_root: Path,
        timestamp: str,
        control_runs: list[dict[str, Any]],
        treatment_runs: list[dict[str, Any]],
        warnings: list[str],
        interrupted: bool,
        provenance_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return aggregate_results(
            config=self.config,
            tasks=self.tasks,
            run_root=run_root,
            timestamp=timestamp,
            control_runs=control_runs,
            treatment_runs=treatment_runs,
            warnings=warnings,
            interrupted=interrupted,
            provenance_snapshot=provenance_snapshot or self._provenance_snapshot,
            comparison=self.comparison,
            baseline_runs=self._baseline_runs,
            retry_history=self._retry_history,
            seed=self._seed,
            execution_config=self._execution_config,
            skill_comparison_info=self._skill_comparison_info(),
        )

    def _save_run_artifacts(
        self,
        arm_dir: Path,
        record: dict[str, Any],
        transcript: str,
        diff: str,
    ) -> None:
        save_run_artifacts(arm_dir, record, transcript, diff)



def _rebuild_workspace(fixture: Path | None, diff_text: str, destination: Path) -> None:
    """Recreate an arm's final files: the frozen fixture with the saved diff applied."""
    if fixture is not None:
        shutil.copytree(fixture, destination, symlinks=True)
    else:
        destination.mkdir(parents=True)
    if not diff_text.strip():
        return
    proc = subprocess.run(
        ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
        input=diff_text, cwd=destination, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise ValueError(
            "could not apply the saved diff (binary or excluded files cannot be "
            f"rebuilt): {detail[0] if detail else 'git apply failed'}"
        )


__all__ = [
    "ExperimentRunner",
    "collect_provenance",
    "find_user_level_installs",
    "find_harness_inheritance",
    "safe_path_component",
    "balanced_arm_order",
    "balanced_three_order",
    "_Pair",
    "_arm_labels",
    "_run_summary",
    "_settings_summary",
    "_run_warnings",
    "_rebuild_workspace",
]


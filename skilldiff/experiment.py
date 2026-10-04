import hashlib
import json
import os
import platform
import random
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from skilldiff import __version__
from skilldiff.config import ExperimentConfig, TaskConfig
from skilldiff.grader import Candidate, Grader
from skilldiff.persistence import atomic_json, atomic_write, read_json, run_lock
from skilldiff.reporter import calculate_metrics, create_reports
from skilldiff.revisions import resolve_comparison
from skilldiff.runner import (
    AgentRunner,
    RunResult,
    resolve_container_image,
    validate_container_auth,
)
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
from skilldiff.stats import analysis_run, paired_comparison
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


def find_harness_inheritance(skill_names: list[str], harness: str) -> list[str]:
    """Harness-specific instructions/plugins/memory that can leak into both arms."""
    try:
        from skilldiff.workspace import find_harness_contamination

        return find_harness_contamination(skill_names, harness)
    except Exception:
        return []


def balanced_arm_order(model: str, task_id: str, repetition: int, seed: int) -> list[str]:
    """Deterministic, balanced arm order within each task/model.

    Random shuffling per pair can imbalance first-arm counts (warm caches,
    rate limits favour one arm). Instead, alternate which arm runs first for
    successive repetitions, offset by a hash of (seed, model, task) so the
    starting arm is still randomized. With N reps per task/model, each arm
    goes first ceil/floor(N/2) times.
    """
    h = hashlib.sha256(f"{seed}:{model}:{task_id}".encode()).hexdigest()
    start_with_control = (int(h[:8], 16) + (repetition - 1)) % 2 == 0
    if start_with_control:
        return ["control", "treatment"]
    return ["treatment", "control"]


def balanced_three_order(
    model: str, task_id: str, repetition: int, seed: int
) -> list[str]:
    """Deterministic, balanced order for control/treatment/baseline.

    Rotates which arm runs first across repetitions, offset by a hash of
    (seed, model, task) so the starting arm stays randomized. Each arm goes
    first roughly one third of the time instead of the baseline always
    running last.
    """
    h = hashlib.sha256(f"{seed}:{model}:{task_id}:3arm".encode()).hexdigest()
    rotations = [
        ["control", "treatment", "baseline"],
        ["treatment", "baseline", "control"],
        ["baseline", "control", "treatment"],
    ]
    return list(rotations[(int(h[:8], 16) + (repetition - 1)) % 3])


def _sha256_file(path: Path) -> str:
    return file_hash(path)


def _hash_dir(
    root: Path, store_limit: int = 200, *, grader: bool = False
) -> tuple[str, list[dict[str, Any]], int]:
    """Hash every file under root. Never silently cap the hash input.

    All files contribute to the combined hash. Only the first `store_limit`
    entries are returned for provenance JSON so reports stay small; the total
    file count is always reported and hash mismatches from truncation are
    impossible.
    """
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
        self._record_hashes: dict[str, str] = {}
        self._grader_inputs: dict[str, Any] = {}
        self._snapshot_manifest_hash: str | None = None
        self._timestamp = ""
        self._stopped = threading.Event()
        self._container_image_id: str | None = None

    # ------------------------------------------------------------------ setup

    def preflight_warnings(self) -> list[str]:
        warnings: list[str] = []
        installs = (
            find_user_level_installs(self.config.skill_names, self.config.harness)
            if self.config.isolation == "local" else []
        )
        isolated = (self.config.isolation != "local" or
                    self.config.harness == "claude" and self.config.claude.isolate)
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
                    "container_image"):
            if prev_exp.get(key) != getattr(self.config, key):
                reasons.append(f"{key} changed")
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
        if self._container_image_id:
            self._execution_config.container_image = self._container_image_id

    def run(self, resume: bool | Path = False) -> dict[str, Any]:
        if self.config.isolation not in {"local", "docker", "podman"}:
            raise ValueError(f"Unsupported isolation mode: {self.config.isolation}")
        validate_container_auth(self.config)
        if self.config.isolation != "local":
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
            manifest = create_snapshots(run_root, sources, self._grader_inputs)
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

        try:
            if self.config.parallel <= 1:
                for pair in pairs:
                    record(self._run_pair(pair, run_root))
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
        results = self._aggregate(
            run_root, self._timestamp, control_runs, treatment_runs, warnings,
            interrupted, provenance_snapshot=self._provenance_snapshot,
        )
        paths = create_reports(results, run_root)
        results["report"] = {k: (str(v) if v else None) for k, v in paths.items()}
        atomic_json(run_root / "results.json", results)
        return results

    def _run_pair(self, pair: _Pair, run_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        self._ensure_running()
        self.progress(
            f"  -> Started {pair.model} · {pair.task.id} (rep {pair.repetition})"
        )
        task = pair.task
        model = pair.model
        execution = self._execution_config
        task_dir = task.source_path.parent if task.source_path else None
        fixture_repo = (
            self.config.pr.repo if self.config.pr else self._fixture_snapshots.get(task.id)
        )
        # Skill names differ per mode: A/B uses per-arm names for blind grading.
        if self.config.is_skill_comparison:
            grader_names = list(
                dict.fromkeys(execution.skill_a_names + execution.skill_b_names)
            )
        else:
            grader_names = execution.skill_names
        grader = Grader(
            task.grader,
            grader_names,
            model,
            task_dir=task_dir,
            allowed_paths=getattr(task, "allowed_paths", None),
            forbidden_paths=getattr(task, "forbidden_paths", None),
            task_prompt=task.prompt,
            isolation=execution.isolation,
            container_image=execution.container_image,
            inputs_root=self.config.config_path.parent if self.config.config_path else None,
        )

        rep_str = f"{pair.repetition:03d}"
        model_dir = safe_path_component(model)
        task_path = safe_path_component(task.id)
        ctrl_dir = run_root / model_dir / task_path / "control" / rep_str
        treat_dir = run_root / model_dir / task_path / "treatment" / rep_str
        baseline_dir = run_root / model_dir / task_path / "baseline" / rep_str
        ctrl_dir.mkdir(parents=True, exist_ok=True)
        treat_dir.mkdir(parents=True, exist_ok=True)
        if self.config.is_skill_comparison and self.config.include_baseline:
            baseline_dir.mkdir(parents=True, exist_ok=True)

        is_ab = self.config.is_skill_comparison
        pr_mode = (self.comparison or {}).get("mode", "agent") if self.comparison else "agent"
        is_correctness = bool(self.comparison) and pr_mode == "correctness"

        # Arm -> skill root mapping.
        if is_ab:
            ctrl_skill = execution.skill_a
            treat_skill = execution.skill_b
            ctrl_is_treatment = True
            treat_is_treatment = True
        elif self.comparison:
            ctrl_skill = None
            treat_skill = None
            ctrl_is_treatment = False
            treat_is_treatment = False
        else:
            # Single-skill mode: control knows the skill so it can strip
            # fixture copies; only the treatment arm installs it.
            ctrl_skill = execution.skill
            treat_skill = execution.skill
            ctrl_is_treatment = False
            treat_is_treatment = True

        with (
            tempfile.TemporaryDirectory(prefix="skilldiff-") as tmp_ctrl,
            tempfile.TemporaryDirectory(prefix="skilldiff-") as tmp_treat,
            tempfile.TemporaryDirectory(prefix="skilldiff-") as tmp_base,
        ):
            workspaces = {
                "control": Workspace(
                    root=Path(tmp_ctrl) / "workspace",
                    is_treatment=ctrl_is_treatment,
                    source_commit=(self.comparison or {}).get("control_commit"),
                    skill_dir=ctrl_skill,
                    strip_skill_dirs=([treat_skill] if is_ab and treat_skill else None),
                    fixture_repo=fixture_repo,
                    harness=self.config.harness,
                ),
                "treatment": Workspace(
                    root=Path(tmp_treat) / "workspace",
                    is_treatment=treat_is_treatment,
                    source_commit=(self.comparison or {}).get("treatment_commit"),
                    skill_dir=treat_skill,
                    strip_skill_dirs=([ctrl_skill] if is_ab and ctrl_skill else None),
                    fixture_repo=fixture_repo,
                    harness=self.config.harness,
                ),
            }
            if is_ab and self.config.include_baseline:
                workspaces["baseline"] = Workspace(
                    root=Path(tmp_base) / "workspace",
                    is_treatment=False,
                    source_commit=None,
                    skill_dir=None,
                    strip_skill_dirs=[
                        s for s in (ctrl_skill, treat_skill) if s is not None
                    ] or None,
                    fixture_repo=fixture_repo,
                    harness=self.config.harness,
                )
            for ws in workspaces.values():
                ws.setup()

            # Balanced arm order within each task/model (seeded, auditable).
            # With a baseline, all three positions rotate so the baseline does
            # not always run last (warm-cache / rate-limit bias).
            has_baseline = "baseline" in workspaces and not is_correctness
            if has_baseline:
                order = balanced_three_order(model, task.id, pair.repetition, self._seed)
            else:
                order = balanced_arm_order(model, task.id, pair.repetition, self._seed)
            results: dict[str, RunResult] = {}
            if is_correctness:
                # PR correctness: same external tests against untouched revisions.
                # No agents run; workspaces are graded as-is.
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
                # Persist each arm immediately (auditable even if grading fails).
                for arm, arm_dir in (("control", ctrl_dir), ("treatment", treat_dir)):
                    self._save_run_artifacts(
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
                arm_dirs_tmp = {
                    "control": ctrl_dir,
                    "treatment": treat_dir,
                    "baseline": baseline_dir,
                }
                for arm in order:
                    if arm not in workspaces:
                        continue
                    self._ensure_running()
                    task_prompt_val = (
                        task.prompts if getattr(task, "prompts", None) else task.prompt
                    )
                    results[arm] = self.agent_runner.run(
                        task_prompt_val, workspaces[arm].root, model, execution
                    )
                    # Persist each arm immediately so rerunning failures cannot
                    # silently improve the reported result; retries are logged.
                    self._save_run_artifacts(
                        arm_dirs_tmp[arm],
                        {
                            "model": model,
                            "task_id": task.id,
                            "repetition": pair.repetition,
                            "arm": arm,
                            "status": results[arm].status,
                            "attempt": 1,
                        },
                        results[arm].transcript,
                        "",
                    )

            diffs = {arm: workspaces[arm].get_diff() for arm in workspaces if arm in results}
            self._ensure_running()
            self._check_graders()
            grades = grader.grade_candidates([
                Candidate("", arm, workspaces[arm].root, results[arm].response,
                          diffs[arm][0], results[arm].transcript, diffs[arm][1])
                for arm in workspaces if arm in results
            ])
            self._check_graders()

            records: dict[str, dict[str, Any]] = {}
            arm_dirs = {"control": ctrl_dir, "treatment": treat_dir}
            if "baseline" in results:
                arm_dirs["baseline"] = baseline_dir
            for arm, arm_dir in arm_dirs.items():
                self._ensure_running()
                res = results[arm]
                # PR mode has no skills; A/B arms both carry a skill.
                if self.comparison and not is_ab:
                    res.skill_invoked = None
                    res.skill_available = None
                grade = grades[arm]
                diff_text, files = diffs[arm]
                records[arm] = {
                    "complete": True,
                    "model": model,
                    "task_id": task.id,
                    "task_category": getattr(task, "category", "general"),
                    "task_split": getattr(task, "split", "dev"),
                    "repetition": pair.repetition,
                    "arm": arm,
                    "run_order": (order.index(arm) + 1) if arm in order else 3,
                    "seed": self._seed,
                    "attempt": 1,
                    "retries": [],
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
                    "isolation_issues": workspaces[arm].isolation_issues or [],
                }
                if self.comparison:
                    records[arm]["source_commit"] = self.comparison[f"{arm}_commit"]
                    records[arm]["pr_mode"] = pr_mode
                    records[arm]["pr_pair"] = self.comparison.get("pair")
                if is_ab:
                    records[arm]["skill_revision"] = (
                        "A" if arm == "control" else ("B" if arm == "treatment" else "baseline")
                    )
                    if arm == "control" and self.config.skill_a:
                        records[arm]["skill_path"] = str(self.config.skill_a)
                    if arm == "treatment" and self.config.skill_b:
                        records[arm]["skill_path"] = str(self.config.skill_b)
                if arm in ("control", "baseline") and workspaces[arm].removed_from_control:
                    records[arm]["removed_from_fixture"] = workspaces[arm].removed_from_control
                # Preserve the partial grader output for audit, but do not
                # count an infrastructure failure as a completed repair.
                fp = dict(getattr(self.config, "failure_policy", {}) or {})
                if records[arm].get("status") not in (None, "ok", "correctness"):
                    if (fp.get("agent_failure") == "zero"
                            and records[arm].get("grade_status") not in ("timeout", "error")):
                        records[arm]["partial_score"] = records[arm].get("score")
                        records[arm].update(analysis_run(records[arm], "zero"))
                    else:
                        records[arm]["analysis_excluded"] = True
                self._save_run_artifacts(arm_dir, records[arm], res.transcript, diff_text)

            if "baseline" in records:
                with self._lock:
                    self._baseline_runs.append(records["baseline"])
                    # Record retry costs: rerunning a failed baseline must not
                    # silently drop its earlier cost.
                    if records["baseline"].get("status") not in (None, "ok"):
                        self._retry_history.append(
                            {
                                "model": model,
                                "task_id": task.id,
                                "repetition": pair.repetition,
                                "arm": "baseline",
                                "status": records["baseline"].get("status"),
                                "cost": records["baseline"].get("cost"),
                            }
                        )

        return records["control"], records["treatment"]

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
        def sort_key(r: dict[str, Any]) -> tuple[int, int, int]:
            model_idx = (
                self.config.models.index(r["model"]) if r["model"] in self.config.models else 0
            )
            task_ids = [t.id for t in self.tasks]
            task_idx = task_ids.index(r["task_id"]) if r["task_id"] in task_ids else 0
            return (model_idx, task_idx, int(r["repetition"]))

        control_runs.sort(key=sort_key)
        treatment_runs.sort(key=sort_key)
        baseline_runs = sorted(list(self._baseline_runs), key=sort_key)
        raw_control_runs, raw_treatment_runs, raw_baseline_runs = (
            control_runs, treatment_runs, baseline_runs
        )
        agent_failure = (getattr(self.config, "failure_policy", {}) or {}).get(
            "agent_failure", "exclude"
        )
        control_runs = [analysis_run(r, agent_failure) for r in raw_control_runs]
        treatment_runs = [analysis_run(r, agent_failure) for r in raw_treatment_runs]
        baseline_runs = [analysis_run(r, agent_failure) for r in raw_baseline_runs]

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

        treat_label = (
            "treatment" if (self.comparison or self.config.is_skill_comparison) else "skill"
        )
        warnings = list(warnings)
        warnings.extend(
            _run_warnings(
                raw_control_runs,
                raw_treatment_runs,
                self.tasks,
                treatment_label=treat_label,
                is_skill_comparison=bool(self.config.is_skill_comparison),
                is_pr=bool(self.comparison),
                baseline_runs=raw_baseline_runs,
            )
        )
        if interrupted:
            warnings.append(
                f"The experiment was interrupted after {len(control_runs)} of "
                f"{len(self.config.models) * len(self.tasks) * self.config.runs} pairs."
            )

        # Reuse the pre-execution snapshot so edits during a run cannot change later pairs.
        provenance = provenance_snapshot if provenance_snapshot else self._provenance_snapshot
        if not provenance:
            try:
                provenance = collect_provenance(self.config, self.tasks)
            except Exception:
                provenance = {}
        task_categories = {t.id: getattr(t, "category", "general") for t in self.tasks}
        task_details = [
            {
                "id": t.id,
                "category": getattr(t, "category", "general"),
                "split": getattr(t, "split", "dev"),
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

        from skilldiff.profiler import compute_context_tax, measure_skill_footprint
        skill_target = self._execution_config.skill or self._execution_config.skill_b
        footprint = measure_skill_footprint(skill_target)
        overall_skill_metrics = calculate_metrics(treatment_runs)
        context_tax = compute_context_tax(
            footprint,
            median_turns=overall_skill_metrics.get("median_turns", 1.0),
            run_count=len(treatment_runs),
        )

        # Control contamination invalidates the experiment (no clean baseline).
        valid = True
        contaminated = [
            r for r in control_runs if r.get("skill_available") or r.get("skill_invoked")
        ]
        if contaminated and not self.comparison and not self.config.is_skill_comparison:
            valid = False
            warnings.append(
                f"INVALID: {len(contaminated)} control run(s) had access to the skill. "
                "There is no clean baseline; do not ship on this result."
            )
        # A/B arms both carry skills; baseline contamination still invalidates.
        baseline_contam = [
            r for r in baseline_runs if r.get("skill_invoked") or r.get("skill_available")
        ]
        if baseline_contam:
            valid = False
            warnings.append(
                f"INVALID: {len(baseline_contam)} baseline run(s) loaded a skill revision. "
                "The no-skill baseline is contaminated."
            )

        # Baseline summaries: baseline-vs-A and baseline-vs-B, so the optional
        # baseline answers whether either revision helps at all.
        baseline_comparisons: dict[str, Any] = {}
        baseline_overall: dict[str, Any] = {}
        if baseline_runs:

            def _paired_vs(
                base: list[dict[str, Any]], other: list[dict[str, Any]]
            ) -> dict[str, Any]:
                # Pair on (model, task, repetition); baseline shares the same keys.
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

        return {
            "name": self.config.name,
            "skilldiff_version": __version__,
            "preset": getattr(self.config, "preset", None) or "skill",
            "arm_labels": _arm_labels(self.config, self.comparison),
            "harness": self.config.harness,
            "skill": str(self.config.skill) if self.config.skill else None,
            "skill_a": str(self.config.skill_a) if self.config.skill_a else None,
            "skill_b": str(self.config.skill_b) if self.config.skill_b else None,
            "skill_comparison": self._skill_comparison_info(),
            "comparison": self.comparison,
            "skill_names": self._execution_config.skill_names,
            "timestamp": timestamp,
            "run_dir": str(run_root),
            "models": self.config.models,
            "tasks": [t.id for t in self.tasks],
            "task_categories": task_categories,
            "task_details": task_details,
            "tasks_count": len(self.tasks),
            "runs_per_arm": self.config.runs,
            "seed": self._seed,
            "failure_policy": dict(getattr(self.config, "failure_policy", {}) or {}),
            "valid": valid,
            "interrupted": interrupted,
            "warnings": warnings,
            "settings": _settings_summary(self.config),
            "thresholds": dict(getattr(self.config, "thresholds", {}) or {}),
            # Cost basis recorded with the run: regenerated reports reproduce
            # the API-equivalent estimate from these rates and the token counts.
            "pricing": dict(getattr(self.config, "pricing", {}) or {}),
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
            "retries": list(self._retry_history),
        }

    def _save_run_artifacts(
        self,
        arm_dir: Path,
        record: dict[str, Any],
        transcript: str,
        diff: str,
    ) -> None:
        atomic_write(arm_dir / "transcript.txt", transcript)
        atomic_write(arm_dir / "diff.patch", diff)
        record["artifact_hashes"] = {
            name: file_hash(arm_dir / name) for name in ("transcript.txt", "diff.patch")
        }
        atomic_json(arm_dir / "run.json", record)


def _arm_labels(config: ExperimentConfig, comparison: dict[str, Any] | None) -> dict[str, str]:
    """Clear arm labels per preset so reports never say just Control/Treatment."""
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
    """Warnings that depend on the expected contents of each arm.

    Single-skill mode expects control to have NO skill; A/B mode expects
    control to carry skill A and treatment to carry skill B, so their
    presence must not be flagged as contamination. PR mode has no skills at
    all. Baseline arms must always be skill-free.
    """
    warnings: list[str] = []
    for arm_name, runs in (("control", control_runs), (treatment_label, treatment_runs)):
        # Correctness mode has no agent sessions; don't flag it as infra failure.
        failed = [
            r
            for r in runs
            if r.get("status") not in (None, "ok", "correctness")
        ]
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

    if is_skill_comparison:
        # A/B: both arms are supposed to carry a revision. Flag only genuine
        # problems: a revision missing where expected, or the wrong revision.
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
        # Adoption is still informative per revision.
        for arm_name, runs in (
            ("skill A", control_runs),
            ("skill B", treatment_runs),
        ):
            known = [r for r in runs if r.get("skill_invoked") is not None]
            unused = [r for r in known if not r.get("skill_invoked")]
            if known and unused:
                warnings.append(
                    f"The agent did not use {arm_name} in {len(unused)} of {len(known)} runs. "
                    "Those runs measure install cost, not following the revision."
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

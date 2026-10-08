import json
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from skilldiff.config import GraderConfig
from skilldiff.runner import AgentRunner
from skilldiff.scope import (
    BLAST_RADIUS_PREFIX,
    filter_diff,
    path_matches,
)

# Environment variables share the OS argument-size limit, so large responses and diffs
# are truncated there. Graders that need the full text should read the *_FILE paths.
MAX_ENV_TEXT = 64_000
DEFAULT_GRADER_TIMEOUT = 600


DEFAULT_BYTECODE_IGNORES = ["__pycache__/*", "*.pyc", "*/__pycache__/*", "*/*.pyc"]


def check_blast_radius(
    changed_files: list[str],
    allowed_paths: list[str],
    forbidden_paths: list[str],
    ignore_paths: Optional[list[str]] = None,
) -> Optional[str]:
    combined_ignores = (ignore_paths or []) + DEFAULT_BYTECODE_IGNORES
    for file_path in changed_files:
        if path_matches(file_path, combined_ignores):
            continue
        if path_matches(file_path, forbidden_paths):
            pattern = next(p for p in forbidden_paths if path_matches(file_path, [p]))
            return (
                f"{BLAST_RADIUS_PREFIX}: modified forbidden path '{file_path}' "
                f"(matches '{pattern}')"
            )
        if allowed_paths and not path_matches(file_path, allowed_paths):
            return f"{BLAST_RADIUS_PREFIX}: modified path '{file_path}' outside allowed paths"
    return None


@dataclass
class Candidate:
    label: str
    arm: str
    workspace_dir: Path
    response: str
    diff: str
    transcript: str
    changed_files: list[str] = field(default_factory=list)


@dataclass
class GradeResult:
    score: Optional[float]
    success: Optional[bool]
    label: str
    feedback: Optional[str] = None
    # "graded" | "ungraded" | "timeout" | "error"
    grade_status: str = "graded"


def sanitize_text(text: str, clues: list[str]) -> str:
    sanitized = text
    for clue in clues:
        if clue:
            pattern = re.compile(re.escape(clue), re.IGNORECASE)
            sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


class Grader:
    def __init__(
        self,
        config: Optional[GraderConfig],
        skill_name: str | list[str],
        model_name: str,
        task_dir: Optional[Path] = None,
        timeout: Optional[float] = DEFAULT_GRADER_TIMEOUT,
        allowed_paths: Optional[list[str]] = None,
        forbidden_paths: Optional[list[str]] = None,
        task_prompt: Optional[str] = None,
        isolation: str = "local",
        container_image: Optional[str] = None,
        inputs_root: Optional[Path] = None,
        grader_ignore: Optional[list[str]] = None,
    ):
        self.config = config
        self.skill_names = [skill_name] if isinstance(skill_name, str) else list(skill_name)
        self.model_name = model_name
        self.task_dir = task_dir
        self.timeout = timeout
        self.allowed_paths = list(allowed_paths or [])
        self.forbidden_paths = list(forbidden_paths or [])
        self.task_prompt = task_prompt
        self.isolation = isolation
        self.container_image = container_image
        self.inputs_root = inputs_root
        self.grader_ignore = list(grader_ignore or [])

    def grade_pair(
        self,
        control_ws: Path,
        treatment_ws: Path,
        control_response: str,
        treatment_response: str,
        control_diff: str,
        treatment_diff: str,
        control_transcript: str,
        treatment_transcript: str,
        control_changed_files: Optional[list[str]] = None,
        treatment_changed_files: Optional[list[str]] = None,
    ) -> tuple[GradeResult, GradeResult]:
        ctrl_files = control_changed_files or []
        treat_files = treatment_changed_files or []
        pair = [
            ("control", control_ws, control_response, control_diff, control_transcript, ctrl_files),
            (
                "treatment",
                treatment_ws,
                treatment_response,
                treatment_diff,
                treatment_transcript,
                treat_files,
            ),
        ]
        results_by_arm = self.grade_candidates([
            Candidate("", arm, ws, resp, diff, trans, files)
            for arm, ws, resp, diff, trans, files in pair
        ])
        return results_by_arm["control"], results_by_arm["treatment"]

    def grade_candidates(self, candidates: list[Candidate]) -> dict[str, GradeResult]:
        candidates = list(candidates)
        random.shuffle(candidates)
        clues = [*self.skill_names, self.model_name, "control", "treatment", "baseline"]
        results = {}
        for index, cand in enumerate(candidates):
            anonymous = Candidate(
                f"candidate-{chr(ord('A') + index)}", cand.arm, cand.workspace_dir,
                sanitize_text(cand.response, clues), sanitize_text(cand.diff, clues),
                sanitize_text(cand.transcript, clues), cand.changed_files,
            )
            results[cand.arm] = self._evaluate_candidate(anonymous)
        return results

    def grade_workspace(self, workspace_dir: Path) -> GradeResult:
        """Grade a single workspace, e.g. an untouched fixture during `skilldiff check`."""
        return self._evaluate_candidate(
            Candidate("candidate-A", "control", workspace_dir, "", "", "")
        )

    def grade_candidate(
        self,
        workspace_dir: Path,
        response: str,
        diff: str,
        transcript: str,
        changed_files: list[str],
        label: str = "candidate-A",
    ) -> GradeResult:
        clues = [*self.skill_names, self.model_name, "control", "treatment", "baseline"]
        return self._evaluate_candidate(
            Candidate(
                label,
                "baseline",
                workspace_dir,
                sanitize_text(response, clues),
                sanitize_text(diff, clues),
                sanitize_text(transcript, clues),
                changed_files,
            )
        )

    def _evaluate_llm(self, cand: Candidate) -> GradeResult:
        if not self.config or not self.config.command:
            return GradeResult(
                None,
                None,
                cand.label,
                "LLM/rubric grading requires an executable judge command",
                "error",
            )
        if os.environ.get("SKILLDIFF_MOCK_RUNNER"):
            mock_score = float(os.environ.get("SKILLDIFF_MOCK_LLM_SCORE", "1.0"))
            return GradeResult(
                score=mock_score,
                success=mock_score >= 0.5,
                label=cand.label,
                feedback="Mock LLM evaluation completed",
                grade_status="graded",
            )

        rubric = (
            self.config.rubric if self.config else None
        ) or "Evaluate task correctness and completeness."
        task_prompt = self.task_prompt or ""
        judge_prompt = (
            f"You are an impartial evaluator grading a task solution.\n\n"
            f"TASK:\n{task_prompt}\n\n"
            f"RUBRIC:\n{rubric}\n\n"
            f"RESPONSE:\n{cand.response[:10000]}\n\n"
            f"DIFF:\n{cand.diff[:10000]}\n\n"
            "Return ONLY valid JSON with keys: score (float 0.0-1.0), "
            "success (bool), feedback (string)."
        )

        if self.config and self.config.command:
            with tempfile.TemporaryDirectory(prefix="skilldiff-llm-grade-") as tmp:
                prompt_file = Path(tmp) / "judge_prompt.txt"
                prompt_file.write_text(judge_prompt, encoding="utf-8")
                env = os.environ.copy()
                env["SKILLDIFF_JUDGE_PROMPT_FILE"] = str(prompt_file)
                env["SKILLDIFF_RUBRIC"] = rubric
                return self._run_command(cand, env)

        raise AssertionError("Judge command is required")

    def _evaluate_candidate(self, cand: Candidate) -> GradeResult:
        if self.allowed_paths or self.forbidden_paths:
            violation = check_blast_radius(
                cand.changed_files,
                self.allowed_paths,
                self.forbidden_paths,
                self.grader_ignore,
            )
            if violation:
                return GradeResult(
                    score=0.0,
                    success=False,
                    label=cand.label,
                    feedback=violation,
                    grade_status="error",
                )

        if not self.config or not self.config.type:
            return GradeResult(
                score=None,
                success=None,
                label=cand.label,
                feedback="No grader configured",
                grade_status="ungraded",
            )

        if self.config.type in {"llm", "rubric"}:
            return self._evaluate_llm(cand)

        if self.config.type != "command" or not self.config.command:
            return GradeResult(
                score=None,
                success=None,
                label=cand.label,
                feedback=f"Unsupported grader type: {self.config.type}",
                grade_status="ungraded",
            )

        with tempfile.TemporaryDirectory(prefix="skilldiff-grade-") as tmp:
            visible_files = [
                f for f in cand.changed_files if not path_matches(f, self.grader_ignore)
            ]
            diff_text = filter_diff(cand.diff, self.grader_ignore)
            workspace_dir = cand.workspace_dir
            if self.grader_ignore:
                # Graders see the workspace without grader_ignore paths, so files
                # the agent wrote for its own audit trail cannot change the grade.
                workspace_dir = Path(tmp) / "workspace"
                self._copy_visible(cand.workspace_dir, workspace_dir)
            response_file = Path(tmp) / "response.txt"
            diff_file = Path(tmp) / "diff.patch"
            files_file = Path(tmp) / "changed_files.txt"
            response_file.write_text(cand.response, encoding="utf-8")
            diff_file.write_text(diff_text, encoding="utf-8")
            files_file.write_text("\n".join(visible_files), encoding="utf-8")

            env = os.environ.copy()
            env["SKILLDIFF_CANDIDATE_LABEL"] = cand.label
            env["SKILLDIFF_CANDIDATE_DIR"] = str(workspace_dir)
            env["SKILLDIFF_RESPONSE"] = cand.response[:MAX_ENV_TEXT]
            env["SKILLDIFF_DIFF"] = diff_text[:MAX_ENV_TEXT]
            env["SKILLDIFF_RESPONSE_FILE"] = str(response_file)
            env["SKILLDIFF_DIFF_FILE"] = str(diff_file)
            env["SKILLDIFF_CHANGED_FILES_FILE"] = str(files_file)
            if self.task_dir:
                env["SKILLDIFF_TASK_DIR"] = str(self.task_dir)
            view = Candidate(
                cand.label, cand.arm, workspace_dir, cand.response, diff_text,
                cand.transcript, visible_files,
            )
            return self._run_command(view, env)

    def _copy_visible(self, source: Path, destination: Path) -> None:
        root = source.resolve()

        def skip(directory: str, names: list[str]) -> list[str]:
            rel_dir = Path(directory).resolve().relative_to(root)
            hidden = []
            for name in names:
                rel = (rel_dir / name).as_posix()
                # A pattern such as `outputs/*` means "everything under outputs", so
                # the directory itself is hidden too, not left behind empty.
                if path_matches(rel, self.grader_ignore) or (
                    (Path(directory) / name).is_dir()
                    and path_matches(f"{rel}/x", self.grader_ignore)
                ):
                    hidden.append(name)
            return hidden

        shutil.copytree(source, destination, symlinks=True, ignore=skip)

    def _run_command(self, cand: Candidate, env: dict[str, str]) -> GradeResult:
        assert self.config and self.config.command
        try:
            mounts: list[tuple[Path, str]] = []
            command = self.config.command
            if self.isolation in {"docker", "podman"}:
                if self.task_dir:
                    root = (self.inputs_root or self.task_dir.parent).resolve()
                    if root == Path(root.anchor) or root == Path.home():
                        raise ValueError(
                            "Container grader inputs must have a confined project root"
                        )
                    mounts.append((root, "/evaluation/inputs"))
                for key in (
                    "SKILLDIFF_RESPONSE_FILE",
                    "SKILLDIFF_DIFF_FILE",
                    "SKILLDIFF_JUDGE_PROMPT_FILE",
                ):
                    if key in env:
                        source = Path(env[key]).parent.resolve()
                        if not any(source == existing for existing, _ in mounts):
                            mounts.append((source, f"/evaluation/artifacts-{len(mounts)}"))
                for source, target in mounts:
                    command = command.replace(str(source), target)
            shell_command = ["/bin/sh", "-c", command]
            if self.isolation == "local" and os.name == "nt":
                shell_command = [os.environ.get("COMSPEC", "cmd.exe"), "/c", command]
            proc = AgentRunner()._exec(
                shell_command,
                cand.workspace_dir,
                env,
                self.timeout,
                isolation=self.isolation,
                container_image=self.container_image,
                readonly_mounts=mounts,
                forward_env=[key for key in env if key.startswith("SKILLDIFF_")],
            )
            if proc.cleanup_error:
                return GradeResult(None, None, cand.label, proc.cleanup_error, "error")
            if proc.timed_out:
                return GradeResult(
                    None, None, cand.label, f"Grader timed out after {self.timeout}s", "timeout"
                )
            stdout = (proc.stdout or "").strip()
            stderr = (proc.stderr or "").strip()

            # Accept JSON or a bare number, either as the whole output or as its last line
            # (so graders can print test logs before the final verdict).
            last_line = stdout.splitlines()[-1].strip() if stdout else ""
            parsed_score: Optional[float] = None
            parsed_success: Optional[bool] = None
            parsed_any = False
            validation_error: Optional[str] = None
            for candidate in dict.fromkeys([stdout, last_line]):
                if not candidate:
                    continue
                try:
                    payload = json.loads(candidate)
                except (json.JSONDecodeError, ValueError):
                    # Bare numbers are also accepted.
                    try:
                        payload = float(candidate)
                    except (TypeError, ValueError):
                        continue
                validation_error = validate_grader_payload(payload)
                if validation_error is not None:
                    parsed_any = True
                    parsed_score = None
                    parsed_success = None
                    break
                parsed = _parse_grader_output(candidate)
                if parsed is not None:
                    parsed_any = True
                    parsed_score, parsed_success = parsed
                    break

            if validation_error is not None:
                return GradeResult(
                    score=None,
                    success=None,
                    label=cand.label,
                    feedback=(stdout or stderr) + f"\n[grader output invalid: {validation_error}]",
                    grade_status="error",
                )

            if parsed_any and parsed_score is not None:
                try:
                    score = min(max(float(parsed_score), 0.0), 1.0)
                except (TypeError, ValueError):
                    return GradeResult(
                        score=None,
                        success=None,
                        label=cand.label,
                        feedback=(stdout or stderr) + "\n[grader score not a number]",
                        grade_status="error",
                    )
                success = parsed_success if parsed_success is not None else score >= 0.5
                return GradeResult(
                    score=score,
                    success=bool(success),
                    label=cand.label,
                    feedback=stdout or stderr,
                    grade_status="graded",
                )

            if self.config.type in {"llm", "rubric"}:
                return GradeResult(
                    None,
                    None,
                    cand.label,
                    (stdout or stderr) + "\n[judge returned no valid verdict]",
                    "error",
                )

            # No structured output: distinguish test failure from grader crash.
            # A crashing grader must never become an ordinary zero score.
            if _looks_like_grader_crash(proc.exit_code, stdout, stderr, parsed_any):
                return GradeResult(
                    score=None,
                    success=None,
                    label=cand.label,
                    feedback=(stdout or stderr) or f"grader exited {proc.exit_code}",
                    grade_status="error",
                )
            score = 1.0 if proc.exit_code == 0 else 0.0
            success = proc.exit_code == 0
            return GradeResult(
                score=score,
                success=success,
                label=cand.label,
                feedback=stdout or stderr,
                grade_status="graded",
            )
        except subprocess.TimeoutExpired:
            return GradeResult(
                score=None,
                success=None,
                label=cand.label,
                feedback=f"Grader timed out after {self.timeout}s",
                grade_status="timeout",
            )
        except Exception as exc:
            return GradeResult(
                score=None,
                success=None,
                label=cand.label,
                feedback=str(exc),
                grade_status="error",
            )


def _parse_grader_output(text: str) -> Optional[tuple[float, Optional[bool]]]:
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        data = None
    if isinstance(data, dict) and ("score" in data or "success" in data):
        success = bool(data["success"]) if "success" in data else None
        if "score" in data:
            return float(data["score"]), success
        return (1.0 if success else 0.0), success
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        val = float(data)
        return (val if val <= 1.0 else val / 100.0), None
    return None


# Markers that indicate the grader itself crashed (evaluation-infra failure),
# as opposed to the agent's work failing tests (score 0, still graded).
GRADER_CRASH_MARKERS = (
    "Traceback",
    "SyntaxError",
    "ModuleNotFoundError",
    "ImportError",
    "command not found",
    "No such file",
    "not found",
    "Permission denied",
    "TypeError",
    "NameError",
)


def validate_grader_payload(data: object) -> Optional[str]:
    """Strict validation for structured grader output. Returns error or None."""
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        try:
            val = float(data)
        except (TypeError, ValueError):
            return "grader score is not a number"
        if not (0.0 <= val <= 100.0):
            return f"grader score {val!r} out of range [0, 1] (or [0, 100])"
        return None
    if not isinstance(data, dict):
        return "grader output must be a JSON object or number"
    if "score" not in data and "success" not in data and "checks" not in data:
        return "grader JSON must contain at least one of score, success, checks"
    if "score" in data:
        try:
            score = float(data["score"])
        except (TypeError, ValueError):
            return "grader score must be a number in [0, 1]"
        if not (0.0 <= score <= 1.0):
            return f"grader score {score!r} out of range [0, 1]"
    if "success" in data and not isinstance(data["success"], bool):
        return "grader success must be a boolean"
    if "notes" in data:
        notes = data["notes"]
        if isinstance(notes, list):
            if not all(isinstance(n, str) for n in notes):
                return "grader notes must be a string, a list of strings, or a mapping"
        elif not isinstance(notes, (str, dict)):
            return "grader notes must be a string, a list of strings, or a mapping"
    if "checks" in data:
        checks = data["checks"]
        if not isinstance(checks, (list, dict)):
            return "grader checks must be a list or mapping"
        if isinstance(checks, dict):
            for key, val in checks.items():
                if not isinstance(key, str) or not key.strip():
                    return "grader check names must be non-empty strings"
                if val is not None and not isinstance(val, (bool, int, float, str, dict)):
                    return f"grader check {key!r} has unsupported value"
        else:
            for i, entry in enumerate(checks):
                if isinstance(entry, bool) or entry is None:
                    continue
                if isinstance(entry, (int, float)) and not isinstance(entry, bool):
                    continue
                if isinstance(entry, str):
                    continue
                if isinstance(entry, dict):
                    if not any(
                        k in entry
                        for k in ("passed", "pass", "success", "ok", "result", "score", "name")
                    ):
                        return f"grader checks[{i}] must contain passed/success/score"
                    continue
                return f"grader checks[{i}] has unsupported shape"
    return None


def _looks_like_grader_crash(returncode: int, stdout: str, stderr: str, parsed: bool) -> bool:
    """A crashing grader must become grade_status=error, never a plain 0."""
    if parsed:
        return False
    if returncode in (126, 127, 128, 129):
        return True
    combined = f"{stdout}\n{stderr}"
    return any(marker in combined for marker in GRADER_CRASH_MARKERS)


def grader_isolation_note(task_dir: Path | None, fixture: Path | None) -> Optional[str]:
    """Keeping graders outside the fixture does not by itself isolate them.

    The agent runs with the workspace as cwd but can still read parent
    directories unless the harness sandbox confines it. Returns a warning
    when the setup looks escapable.
    """
    if task_dir is None:
        return None
    graders_dir = task_dir.parent / "graders" if task_dir.name == "tasks" else task_dir / "graders"
    if not graders_dir.is_dir():
        return None
    if fixture is not None:
        try:
            graders_dir.resolve().relative_to(fixture.resolve())
            return "graders live inside the fixture and are visible to the agent"
        except ValueError:
            pass
    return (
        "graders are outside the fixture but still readable via parent paths; "
        "confine agents with the harness sandbox (e.g. workspace-write) so they "
        "cannot read or edit grader files"
    )


def validate_grader_against_directories(
    grader: "Grader",
    untouched_dir: Path,
    good_dir: "Path | list[Path] | None" = None,
    broken_dirs: list[Path] | None = None,
    reference_dirs: list[Path] | None = None,
    deprecated_patterns: list[str] | None = None,
) -> dict[str, object]:
    """Grade untouched, known-good, and deliberately broken workspaces."""
    report: dict[str, object] = {"checks": []}
    checks: list[str] = report["checks"]  # type: ignore
    untouched = grader.grade_workspace(untouched_dir)
    report["untouched"] = {
        "score": untouched.score,
        "grade_status": untouched.grade_status,
        "feedback": untouched.feedback,
    }
    if (
        untouched.grade_status != "graded"
        or untouched.score is None
        or not math.isfinite(untouched.score)
    ):
        checks.append("untouched fixture could not be graded (grader error/timeout): "
                      + (untouched.feedback or "No feedback"))
        report["verdict"] = "grader-broken"
        return report
    if untouched.score is not None and untouched.score >= 1.0:
        checks.append("untouched fixture already scores 100% (task too easy)")
    else:
        checks.append(f"untouched scores {round((untouched.score or 0) * 100)}% (must be <100%)")

    good_dirs: list[Path] = (
        [] if good_dir is None else [good_dir] if isinstance(good_dir, Path) else list(good_dir)
    )
    from skilldiff.snapshots import tree_contents

    if deprecated_patterns:
        for sol_dir in good_dirs + (reference_dirs or []):
            for path in sol_dir.rglob("*"):
                if path.is_file() and not path.name.startswith("."):
                    try:
                        content = path.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        continue
                    for dep in deprecated_patterns:
                        if dep in content:
                            rel = path.relative_to(sol_dir)
                            checks.append(
                                f"solution in {sol_dir.name} uses deprecated API '{dep}' in {rel}"
                            )
                            report["verdict"] = "deprecated-api-used"
                            return report

    scoped = bool(grader.allowed_paths or grader.forbidden_paths)
    original = {entry["path"]: entry for entry in tree_contents(untouched_dir)
                if entry["type"] != "directory"} if good_dirs and scoped else {}
    for i, directory in enumerate(good_dirs):
        label = "known-good solution" if len(good_dirs) == 1 else f"known-good solution {i}"
        candidate = {entry["path"]: entry for entry in tree_contents(directory)
                     if entry["type"] != "directory"} if scoped else {}
        changed = sorted(path for path in original.keys() | candidate.keys()
                         if original.get(path) != candidate.get(path))
        conflict = check_blast_radius(changed, grader.allowed_paths,
                                      grader.forbidden_paths, grader.grader_ignore)
        if conflict:
            checks.append(f"{label} conflicts with task scope: {conflict}")
            report["verdict"] = "scope-conflict"
            return report
        good = grader.grade_workspace(directory)
        report["good" if len(good_dirs) == 1 else f"good_{i}"] = {
            "score": good.score,
            "grade_status": good.grade_status,
            "feedback": good.feedback,
        }
        if good.grade_status != "graded" or good.score is None or not math.isfinite(good.score):
            checks.append(f"{label} could not be graded: {good.feedback or 'No feedback'}")
            report["verdict"] = "grader-broken"
            return report
        if good.score < 0.99:
            checks.append(
                f"{label} scores only {round(good.score * 100)}% "
                "(grader rejects valid work)"
            )
            report["verdict"] = "grader-too-strict"
            return report
        checks.append(f"{label} scores 100%")

    n_ref = len(reference_dirs or [])
    for i, directory in enumerate(reference_dirs or []):
        label = "reference solution" if n_ref == 1 else f"reference solution {i}"
        ref = grader.grade_workspace(directory)
        report[f"ref_{i}"] = {
            "score": ref.score,
            "grade_status": ref.grade_status,
            "feedback": ref.feedback,
        }
        if ref.grade_status != "graded" or ref.score is None or not math.isfinite(ref.score):
            checks.append(f"{label} could not be graded: {ref.feedback or 'No feedback'}")
            report["verdict"] = "grader-broken"
            return report
        if ref.score < 0.99:
            checks.append(
                f"{label} scores only {round(ref.score * 100)}% "
                "(grader rejects independent reference solution)"
            )
            report["verdict"] = "grader-rejects-reference"
            return report
        checks.append(f"{label} scores 100%")

    for i, bad in enumerate(broken_dirs or []):
        broken = grader.grade_workspace(bad)
        report[f"broken_{i}"] = {
            "score": broken.score,
            "grade_status": broken.grade_status,
            "feedback": broken.feedback,
        }
        if (
            broken.grade_status != "graded"
            or broken.score is None
            or not math.isfinite(broken.score)
        ):
            checks.append(f"broken solution {i} could not be graded: "
                          f"{broken.feedback or 'No feedback'}")
            report["verdict"] = "grader-broken"
            return report
        if broken.grade_status == "graded" and broken.score is not None and broken.score >= 1.0:
            checks.append(f"broken solution {i} still scores 100% (grader misses failures)")
            report["verdict"] = "grader-too-lax"
            return report
    checks.append(
        "broken solutions fail as expected" if broken_dirs else "no broken examples provided"
    )
    report["verdict"] = "ok"
    return report

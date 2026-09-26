import json
import os
import random
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from skilldiff.config import GraderConfig

# Environment variables share the OS argument-size limit, so large responses and diffs
# are truncated there. Graders that need the full text should read the *_FILE paths.
MAX_ENV_TEXT = 64_000
DEFAULT_GRADER_TIMEOUT = 600


@dataclass
class Candidate:
    label: str
    arm: str
    workspace_dir: Path
    response: str
    diff: str
    transcript: str


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
    ):
        self.config = config
        self.skill_names = [skill_name] if isinstance(skill_name, str) else list(skill_name)
        self.model_name = model_name
        self.task_dir = task_dir
        self.timeout = timeout

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
    ) -> tuple[GradeResult, GradeResult]:
        pair = [
            ("control", control_ws, control_response, control_diff, control_transcript),
            ("treatment", treatment_ws, treatment_response, treatment_diff, treatment_transcript),
        ]
        random.shuffle(pair)

        labels = ["candidate-A", "candidate-B"]
        candidates: list[Candidate] = []
        for i, (arm, ws, resp, diff, trans) in enumerate(pair):
            clues = [*self.skill_names, self.model_name, arm, "control", "treatment"]
            candidates.append(
                Candidate(
                    label=labels[i],
                    arm=arm,
                    workspace_dir=ws,
                    response=sanitize_text(resp, clues),
                    diff=sanitize_text(diff, clues),
                    transcript=sanitize_text(trans, clues),
                )
            )

        results_by_arm: dict[str, GradeResult] = {}
        for cand in candidates:
            res = self._evaluate_candidate(cand)
            results_by_arm[cand.arm] = res

        return results_by_arm["control"], results_by_arm["treatment"]

    def grade_workspace(self, workspace_dir: Path) -> GradeResult:
        """Grade a single workspace, e.g. an untouched fixture during `skilldiff check`."""
        return self._evaluate_candidate(
            Candidate("candidate-A", "control", workspace_dir, "", "", "")
        )

    def _evaluate_candidate(self, cand: Candidate) -> GradeResult:
        if not self.config or self.config.type != "command" or not self.config.command:
            return GradeResult(
                score=None,
                success=None,
                label=cand.label,
                feedback="No grader configured",
                grade_status="ungraded",
            )

        with tempfile.TemporaryDirectory(prefix="skilldiff-grade-") as tmp:
            response_file = Path(tmp) / "response.txt"
            diff_file = Path(tmp) / "diff.patch"
            response_file.write_text(cand.response, encoding="utf-8")
            diff_file.write_text(cand.diff, encoding="utf-8")

            env = os.environ.copy()
            env["SKILLDIFF_CANDIDATE_LABEL"] = cand.label
            env["SKILLDIFF_CANDIDATE_DIR"] = str(cand.workspace_dir)
            env["SKILLDIFF_RESPONSE"] = cand.response[:MAX_ENV_TEXT]
            env["SKILLDIFF_DIFF"] = cand.diff[:MAX_ENV_TEXT]
            env["SKILLDIFF_RESPONSE_FILE"] = str(response_file)
            env["SKILLDIFF_DIFF_FILE"] = str(diff_file)
            if self.task_dir:
                env["SKILLDIFF_TASK_DIR"] = str(self.task_dir)
            return self._run_command(cand, env)

    def _run_command(self, cand: Candidate, env: dict[str, str]) -> GradeResult:
        assert self.config and self.config.command
        try:
            proc = subprocess.run(
                self.config.command,
                shell=True,
                cwd=cand.workspace_dir,
                capture_output=True,
                text=True,
                env=env,
                check=False,
                stdin=subprocess.DEVNULL,
                timeout=self.timeout,
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
                success = (
                    parsed_success if parsed_success is not None else score >= 0.5
                )
                return GradeResult(
                    score=score,
                    success=bool(success),
                    label=cand.label,
                    feedback=stdout or stderr,
                    grade_status="graded",
                )

            # No structured output: distinguish test failure from grader crash.
            # A crashing grader must never become an ordinary zero score.
            if _looks_like_grader_crash(proc.returncode, stdout, stderr, parsed_any):
                return GradeResult(
                    score=None,
                    success=None,
                    label=cand.label,
                    feedback=(stdout or stderr) or f"grader exited {proc.returncode}",
                    grade_status="error",
                )
            score = 1.0 if proc.returncode == 0 else 0.0
            success = proc.returncode == 0
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


def _looks_like_grader_crash(
    returncode: int, stdout: str, stderr: str, parsed: bool
) -> bool:
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
    good_dir: Path | None = None,
    broken_dirs: list[Path] | None = None,
) -> dict[str, object]:
    """Grade untouched, known-good, and deliberately broken workspaces.

    Checking only that the untouched fixture fails is insufficient: a broken
    grader can fail everything. Returns a report with scores and a verdict.
    """
    report: dict[str, object] = {"checks": []}
    checks: list[str] = report["checks"]  # type: ignore
    untouched = grader.grade_workspace(untouched_dir)
    report["untouched"] = {
        "score": untouched.score,
        "grade_status": untouched.grade_status,
    }
    if untouched.grade_status in ("timeout", "error"):
        checks.append("untouched fixture could not be graded (grader error/timeout)")
        report["verdict"] = "grader-broken"
        return report
    if untouched.score is not None and untouched.score >= 1.0:
        checks.append("untouched fixture already scores 100% (task too easy)")
    else:
        checks.append(
            f"untouched scores {round((untouched.score or 0) * 100)}% (must be <100%)"
        )

    if good_dir is not None:
        good = grader.grade_workspace(good_dir)
        report["good"] = {"score": good.score, "grade_status": good.grade_status}
        if good.grade_status != "graded" or good.score is None:
            checks.append("known-good solution could not be graded")
            report["verdict"] = "grader-broken"
            return report
        if good.score < 0.99:
            checks.append(
                f"known-good solution scores only {round(good.score * 100)}% "
                "(grader rejects valid work)"
            )
            report["verdict"] = "grader-too-strict"
            return report
        checks.append("known-good solution scores 100%")

    for i, bad in enumerate(broken_dirs or []):
        broken = grader.grade_workspace(bad)
        report[f"broken_{i}"] = {
            "score": broken.score,
            "grade_status": broken.grade_status,
        }
        if broken.grade_status == "graded" and broken.score is not None and broken.score >= 1.0:
            checks.append(f"broken solution {i} still scores 100% (grader misses failures)")
            report["verdict"] = "grader-too-lax"
            return report
    checks.append(
        "broken solutions fail as expected" if broken_dirs else "no broken examples provided"
    )
    report["verdict"] = "ok"
    return report

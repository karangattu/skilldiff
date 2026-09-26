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
            stdout = proc.stdout.strip()
            stderr = proc.stderr.strip()

            score: float = 1.0 if proc.returncode == 0 else 0.0
            success: bool = proc.returncode == 0

            # Accept JSON or a bare number, either as the whole output or as its last line
            # (so graders can print test logs before the final verdict).
            last_line = stdout.splitlines()[-1].strip() if stdout else ""
            for candidate in dict.fromkeys([stdout, last_line]):
                parsed = _parse_grader_output(candidate)
                if parsed is not None:
                    score, reported_success = parsed
                    success = reported_success if reported_success is not None else score >= 0.5
                    break
            score = min(max(score, 0.0), 1.0)

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

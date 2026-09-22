import json
import os
import random
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from skilldiff.config import GraderConfig


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
    score: float
    success: bool
    label: str
    feedback: Optional[str] = None


def sanitize_text(text: str, clues: list[str]) -> str:
    sanitized = text
    for clue in clues:
        if clue:
            pattern = re.compile(re.escape(clue), re.IGNORECASE)
            sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


class Grader:
    def __init__(self, config: Optional[GraderConfig], skill_name: str, model_name: str):
        self.config = config
        self.skill_name = skill_name
        self.model_name = model_name

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
            clues = [self.skill_name, self.model_name, arm, "control", "treatment"]
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

    def _evaluate_candidate(self, cand: Candidate) -> GradeResult:
        if not self.config or self.config.type != "command" or not self.config.command:
            return GradeResult(score=1.0, success=True, label=cand.label)

        env = os.environ.copy()
        env["SKILLDIFF_CANDIDATE_LABEL"] = cand.label
        env["SKILLDIFF_CANDIDATE_DIR"] = str(cand.workspace_dir)
        env["SKILLDIFF_RESPONSE"] = cand.response
        env["SKILLDIFF_DIFF"] = cand.diff

        try:
            proc = subprocess.run(
                self.config.command,
                shell=True,
                cwd=cand.workspace_dir,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            stdout = proc.stdout.strip()
            stderr = proc.stderr.strip()

            score: float = 1.0 if proc.returncode == 0 else 0.0
            success: bool = proc.returncode == 0

            try:
                data = json.loads(stdout)
                if isinstance(data, dict):
                    if "score" in data:
                        score = float(data["score"])
                    if "success" in data:
                        success = bool(data["success"])
                    else:
                        success = score >= 0.5
            except (json.JSONDecodeError, ValueError):
                try:
                    val = float(stdout)
                    score = val if val <= 1.0 else val / 100.0
                    success = score >= 0.5
                except ValueError:
                    pass

            return GradeResult(
                score=score,
                success=success,
                label=cand.label,
                feedback=stdout or stderr,
            )
        except Exception as exc:
            return GradeResult(
                score=0.0,
                success=False,
                label=cand.label,
                feedback=str(exc),
            )

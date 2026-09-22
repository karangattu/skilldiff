import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from skilldiff.config import ClaudeConfig


@dataclass
class RunResult:
    prompt: str
    response: str
    transcript: str
    duration: float
    cost: float
    input_tokens: int
    output_tokens: int
    tool_calls: int
    exit_code: int
    error: Optional[str] = None


class AgentRunner:
    def __init__(self, claude_bin: Optional[str] = None):
        self.claude_bin = claude_bin or shutil.which("claude") or "claude"

    def run(
        self,
        prompt: str,
        cwd: Path,
        model: str,
        claude_cfg: ClaudeConfig,
    ) -> RunResult:
        if os.environ.get("SKILLDIFF_MOCK_RUNNER"):
            return self._run_mock(prompt, cwd, model)

        cmd = [
            self.claude_bin,
            "-p",
            prompt,
            "--model",
            model,
            "--output-format",
            "json",
        ]
        if claude_cfg.permission_mode:
            cmd.extend(["--permission-mode", claude_cfg.permission_mode])
        if claude_cfg.allowed_tools:
            cmd.extend(["--allowedTools", ",".join(claude_cfg.allowed_tools)])
        if claude_cfg.effort:
            cmd.extend(["--effort", str(claude_cfg.effort)])
        if claude_cfg.max_budget_usd is not None:
            cmd.extend(["--max-budget-usd", str(claude_cfg.max_budget_usd)])

        start_time = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
            duration = round(time.perf_counter() - start_time, 2)
            stdout = proc.stdout
            stderr = proc.stderr
            transcript = f"STDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
            exit_code = proc.returncode

            response = stdout
            cost = 0.0
            input_tokens = 0
            output_tokens = 0
            tool_calls = 0

            try:
                data = json.loads(stdout)
                if isinstance(data, dict):
                    response = data.get("result") or data.get("response") or stdout
                    cost = float(data.get("total_cost_usd") or data.get("cost") or 0.0)
                    usage = data.get("usage", {})
                    input_tokens = int(usage.get("input_tokens", 0))
                    output_tokens = int(usage.get("output_tokens", 0))
                    tool_calls = int(data.get("tool_calls_count", 0))
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

            return RunResult(
                prompt=prompt,
                response=response,
                transcript=transcript,
                duration=duration,
                cost=cost,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                tool_calls=tool_calls,
                exit_code=exit_code,
                error=stderr if exit_code != 0 else None,
            )
        except Exception as exc:
            duration = round(time.perf_counter() - start_time, 2)
            return RunResult(
                prompt=prompt,
                response="",
                transcript=str(exc),
                duration=duration,
                cost=0.0,
                input_tokens=0,
                output_tokens=0,
                tool_calls=0,
                exit_code=-1,
                error=str(exc),
            )

    def _run_mock(self, prompt: str, cwd: Path, model: str) -> RunResult:
        mock_response = os.environ.get("SKILLDIFF_MOCK_RESPONSE", "Mock agent response")
        mock_cost = float(os.environ.get("SKILLDIFF_MOCK_COST", "0.05"))
        mock_duration = float(os.environ.get("SKILLDIFF_MOCK_DURATION", "1.5"))

        script = os.environ.get("SKILLDIFF_MOCK_SCRIPT")
        if script:
            subprocess.run(script, shell=True, cwd=cwd, check=False)

        return RunResult(
            prompt=prompt,
            response=mock_response,
            transcript=f"Mock run for prompt: {prompt} with model {model}",
            duration=mock_duration,
            cost=mock_cost,
            input_tokens=100,
            output_tokens=50,
            tool_calls=1,
            exit_code=0,
        )

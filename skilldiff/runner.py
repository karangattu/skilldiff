import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from skilldiff.config import (
    AntigravityConfig,
    ClaudeConfig,
    CodexConfig,
    ExperimentConfig,
    OpenCodeConfig,
)


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
    def __init__(
        self,
        claude_bin: Optional[str] = None,
        codex_bin: Optional[str] = None,
        opencode_bin: Optional[str] = None,
        antigravity_bin: Optional[str] = None,
    ):
        self.claude_bin = (
            claude_bin or os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
        )
        self.codex_bin = (
            codex_bin or os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"
        )
        self.opencode_bin = (
            opencode_bin or os.environ.get("OPENCODE_BIN") or shutil.which("opencode") or "opencode"
        )
        self.antigravity_bin = (
            antigravity_bin
            or os.environ.get("AGY_BIN")
            or os.environ.get("ANTIGRAVITY_BIN")
            or shutil.which("agy")
            or shutil.which("antigravity")
            or "agy"
        )

    def run(
        self,
        prompt: str,
        cwd: Path,
        model: str,
        config: ExperimentConfig | ClaudeConfig | Any,
        harness: Optional[str] = None,
    ) -> RunResult:
        if os.environ.get("SKILLDIFF_MOCK_RUNNER"):
            return self._run_mock(prompt, cwd, model)

        if isinstance(config, ExperimentConfig):
            active_harness = (harness or config.harness).lower().strip()
            claude_cfg = config.claude
            codex_cfg = config.codex
            opencode_cfg = config.opencode
            antigravity_cfg = config.antigravity
        elif isinstance(config, ClaudeConfig):
            active_harness = "claude"
            claude_cfg = config
            codex_cfg = CodexConfig()
            opencode_cfg = OpenCodeConfig()
            antigravity_cfg = AntigravityConfig()
        elif isinstance(config, CodexConfig):
            active_harness = "codex"
            claude_cfg = ClaudeConfig()
            codex_cfg = config
            opencode_cfg = OpenCodeConfig()
            antigravity_cfg = AntigravityConfig()
        elif isinstance(config, OpenCodeConfig):
            active_harness = "opencode"
            claude_cfg = ClaudeConfig()
            codex_cfg = CodexConfig()
            opencode_cfg = config
            antigravity_cfg = AntigravityConfig()
        elif isinstance(config, AntigravityConfig):
            active_harness = "antigravity"
            claude_cfg = ClaudeConfig()
            codex_cfg = CodexConfig()
            opencode_cfg = OpenCodeConfig()
            antigravity_cfg = config
        else:
            active_harness = (harness or "claude").lower().strip()
            claude_cfg = getattr(config, "claude", ClaudeConfig())
            codex_cfg = getattr(config, "codex", CodexConfig())
            opencode_cfg = getattr(config, "opencode", OpenCodeConfig())
            antigravity_cfg = getattr(config, "antigravity", AntigravityConfig())

        if active_harness == "codex":
            return self._run_codex(prompt, cwd, model, codex_cfg)
        if active_harness == "opencode":
            return self._run_opencode(prompt, cwd, model, opencode_cfg)
        if active_harness in {"antigravity", "agy"}:
            return self._run_antigravity(prompt, cwd, model, antigravity_cfg)
        return self._run_claude(prompt, cwd, model, claude_cfg)

    def _run_claude(
        self,
        prompt: str,
        cwd: Path,
        model: str,
        claude_cfg: ClaudeConfig,
    ) -> RunResult:
        bin_path = claude_cfg.bin_path or self.claude_bin
        cmd = [
            bin_path,
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

        env = os.environ.copy()
        if claude_cfg.auth == "subscription":
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)

        start_time = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
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

    def _run_codex(
        self,
        prompt: str,
        cwd: Path,
        model: str,
        codex_cfg: CodexConfig,
    ) -> RunResult:
        bin_path = codex_cfg.bin_path or self.codex_bin
        cmd = [bin_path, "exec", prompt, "-m", model, "--json"]
        if codex_cfg.dangerously_bypass_approvals_and_sandbox:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        elif codex_cfg.sandbox:
            cmd.extend(["-s", codex_cfg.sandbox])

        if codex_cfg.extra_args:
            cmd.extend(codex_cfg.extra_args)

        env = os.environ.copy()
        if codex_cfg.auth in {"subscription", "stored"}:
            env.pop("OPENAI_API_KEY", None)

        start_time = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            duration = round(time.perf_counter() - start_time, 2)
            stdout = proc.stdout
            stderr = proc.stderr
            transcript = f"STDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
            exit_code = proc.returncode

            response_texts: list[str] = []
            cost = 0.0
            input_tokens = 0
            output_tokens = 0
            tool_calls = 0

            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        continue
                    item_type = str(item.get("type", ""))
                    if "tool" in item_type or item.get("tool_calls"):
                        tool_calls += 1

                    usage = item.get("usage")
                    if isinstance(usage, dict):
                        input_tokens = int(
                            usage.get("input_tokens") or usage.get("prompt_tokens") or input_tokens
                        )
                        output_tokens = int(
                            usage.get("output_tokens")
                            or usage.get("completion_tokens")
                            or output_tokens
                        )

                    if "cost" in item:
                        try:
                            cost = float(item["cost"])
                        except (ValueError, TypeError):
                            pass

                    if item_type in {"message", "agent_message", "assistant_message", "text"}:
                        content = item.get("content") or item.get("text") or item.get("message")
                        if isinstance(content, str) and content:
                            response_texts.append(content)
                    elif "result" in item and isinstance(item["result"], str):
                        response_texts.append(item["result"])
                except (json.JSONDecodeError, ValueError):
                    pass

            response = response_texts[-1] if response_texts else stdout

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

    def _run_opencode(
        self,
        prompt: str,
        cwd: Path,
        model: str,
        opencode_cfg: OpenCodeConfig,
    ) -> RunResult:
        bin_path = opencode_cfg.bin_path or self.opencode_bin
        target_model = model
        if opencode_cfg.service == "go" or opencode_cfg.provider == "opencode-go":
            if target_model.startswith("opencode/"):
                target_model = f"opencode-go/{target_model[len('opencode/'):]}"
            elif "/" not in target_model:
                target_model = f"opencode-go/{target_model}"
        elif "/" not in target_model and opencode_cfg.provider:
            target_model = f"{opencode_cfg.provider}/{target_model}"

        cmd = [bin_path, "run", "--dir", str(cwd), "--format", "json", "-m", target_model]
        if opencode_cfg.dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if opencode_cfg.variant:
            cmd.extend(["--variant", str(opencode_cfg.variant)])
        if opencode_cfg.extra_args:
            cmd.extend(opencode_cfg.extra_args)
        cmd.append(prompt)

        env = os.environ.copy()
        start_time = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            duration = round(time.perf_counter() - start_time, 2)
            stdout = proc.stdout
            stderr = proc.stderr
            transcript = f"STDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
            exit_code = proc.returncode

            response_texts: list[str] = []
            cost = 0.0
            input_tokens = 0
            output_tokens = 0
            tool_calls = 0

            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        continue
                    item_type = str(item.get("type", ""))
                    if "tool" in item_type or item.get("tool"):
                        tool_calls += 1

                    part = item.get("part")
                    if isinstance(part, dict):
                        text = part.get("text")
                        if text and isinstance(text, str):
                            response_texts.append(text)
                        tokens = part.get("tokens")
                        if isinstance(tokens, dict):
                            input_tokens += int(tokens.get("input") or 0)
                            output_tokens += int(tokens.get("output") or 0)
                        if "cost" in part:
                            try:
                                cost += float(part["cost"])
                            except (ValueError, TypeError):
                                pass

                    if "cost" in item:
                        try:
                            cost += float(item["cost"])
                        except (ValueError, TypeError):
                            pass

                    if "text" in item and isinstance(item["text"], str):
                        response_texts.append(item["text"])
                except (json.JSONDecodeError, ValueError):
                    pass

            response = "\n".join(response_texts) if response_texts else stdout
            cost = round(cost, 6)

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

    def _run_antigravity(
        self,
        prompt: str,
        cwd: Path,
        model: str,
        antigravity_cfg: AntigravityConfig,
    ) -> RunResult:
        bin_path = antigravity_cfg.bin_path or self.antigravity_bin
        cmd = [bin_path, "-p", prompt]
        if model:
            cmd.extend(["--model", model])
        if antigravity_cfg.dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if antigravity_cfg.extra_args:
            cmd.extend(antigravity_cfg.extra_args)

        env = os.environ.copy()
        start_time = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
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

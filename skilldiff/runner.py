import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
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
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    num_turns: int = 0
    # True/False when the harness output says whether the agent loaded the skill;
    # None when it cannot be determined.
    skill_invoked: Optional[bool] = None
    # Whether the harness reported the skill as installed (Claude init event only).
    skill_available: Optional[bool] = None
    # "ok", "error", or "timeout".
    status: str = "ok"


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration: float
    timed_out: bool = False


# Variables that tie a process to the Claude Code session it runs in. When skilldiff is
# started from inside Claude Code (e.g. by the skilldiff skill), each agent session must
# be independent, so these are removed. Auth and provider settings are left alone.
PARENT_SESSION_VARS = (
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_HOST_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    "CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL",
    "CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES",
    "CLAUDE_CODE_EAGER_FLUSH",
    "CLAUDE_CODE_REPORT_FINDINGS",
    "CLAUDE_CODE_DESKTOP_APP_VERSION",
)


def _kill_group(proc: subprocess.Popen) -> None:
    if os.name != "posix":
        proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def detect_skill_reference(text: str, skill_names: list[str]) -> bool:
    """Heuristic: the transcript mentions a path inside the installed skill directory."""
    for name in skill_names:
        pattern = rf"skills[/\\]+{re.escape(name)}(?:[/\\\"'\s]|$)"
        if re.search(pattern, text):
            return True
    return False


def _failed_result(prompt: str, exc: Exception, duration: float) -> RunResult:
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
        status="error",
    )


def _finalize_status(result: RunResult, execution: ExecResult) -> RunResult:
    if execution.timed_out:
        result.status = "timeout"
        result.error = result.error or f"Agent timed out after {execution.duration:.0f}s"
        if result.exit_code == 0:
            result.exit_code = -1
    elif result.exit_code != 0:
        result.status = "error"
        result.error = result.error or execution.stderr.strip() or f"exit code {result.exit_code}"
    return result


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
        gemini_agy = Path.home() / ".gemini" / "bin" / "agy"
        self.antigravity_bin = (
            antigravity_bin
            or os.environ.get("AGY_BIN")
            or os.environ.get("ANTIGRAVITY_BIN")
            or shutil.which("agy")
            or shutil.which("antigravity")
            or (str(gemini_agy) if gemini_agy.exists() else None)
            or "agy"
        )
        self._active: set[subprocess.Popen] = set()
        self._active_lock = threading.Lock()
        self._cancelled = False

    def terminate_all(self) -> None:
        """Kill running agent sessions and refuse new ones (used on Ctrl-C)."""
        with self._active_lock:
            self._cancelled = True
            procs = list(self._active)
        for proc in procs:
            _kill_group(proc)

    def binary_for(self, harness: str, config: Optional[ExperimentConfig] = None) -> str:
        harness = "antigravity" if harness == "agy" else harness
        if config is not None:
            override = getattr(config, harness).bin_path
            if override:
                return override
        return {
            "claude": self.claude_bin,
            "codex": self.codex_bin,
            "opencode": self.opencode_bin,
            "antigravity": self.antigravity_bin,
        }[harness]

    def _exec(
        self,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        timeout: Optional[float],
    ) -> ExecResult:
        """Run an agent CLI without a stdin pipe, with a timeout, and clean up its children.

        Output goes to temporary files rather than pipes so that background processes the
        agent leaves behind (dev servers, watchers) cannot keep the run from finishing.
        """
        if self._cancelled:
            raise RuntimeError("Cancelled before the agent started")
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            start = time.perf_counter()
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=os.name == "posix",
            )
            with self._active_lock:
                self._active.add(proc)
            timed_out = False
            try:
                exit_code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
                exit_code = proc.wait()
            finally:
                _kill_group(proc)
                with self._active_lock:
                    self._active.discard(proc)
            duration = round(time.perf_counter() - start, 2)
            out.seek(0)
            err.seek(0)
            return ExecResult(
                stdout=out.read().decode("utf-8", errors="replace"),
                stderr=err.read().decode("utf-8", errors="replace"),
                exit_code=exit_code,
                duration=duration,
                timed_out=timed_out,
            )

    def run(
        self,
        prompt: str,
        cwd: Path,
        model: str,
        config: ExperimentConfig | ClaudeConfig | Any,
        harness: Optional[str] = None,
        skill_names: Optional[list[str]] = None,
        timeout: Optional[float] = None,
    ) -> RunResult:
        if os.environ.get("SKILLDIFF_MOCK_RUNNER"):
            return self._run_mock(prompt, cwd, model)

        if isinstance(config, ExperimentConfig):
            active_harness = (harness or config.harness).lower().strip()
            claude_cfg = config.claude
            codex_cfg = config.codex
            opencode_cfg = config.opencode
            antigravity_cfg = config.antigravity
            skill_names = skill_names if skill_names is not None else config.skill_names
            timeout = timeout if timeout is not None else config.timeout_seconds
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

        names = skill_names or []
        if active_harness == "codex":
            result = self._run_codex(prompt, cwd, model, codex_cfg, timeout)
        elif active_harness == "opencode":
            result = self._run_opencode(prompt, cwd, model, opencode_cfg, timeout)
        elif active_harness in {"antigravity", "agy"}:
            result = self._run_antigravity(prompt, cwd, model, antigravity_cfg, timeout)
        else:
            result = self._run_claude(prompt, cwd, model, claude_cfg, timeout, names)

        if result.skill_invoked is None and names and result.transcript:
            result.skill_invoked = detect_skill_reference(result.transcript, names)
        return result

    def claude_command(self, prompt: str, model: str, claude_cfg: ClaudeConfig) -> list[str]:
        cmd = [
            claude_cfg.bin_path or self.claude_bin,
            "-p",
            prompt,
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
        ]
        if claude_cfg.isolate:
            cmd.extend(["--setting-sources", "project,local"])
        if claude_cfg.permission_mode:
            cmd.extend(["--permission-mode", claude_cfg.permission_mode])
        if claude_cfg.allowed_tools:
            cmd.extend(["--allowedTools", ",".join(claude_cfg.allowed_tools)])
        if claude_cfg.effort:
            cmd.extend(["--effort", str(claude_cfg.effort)])
        if claude_cfg.max_turns:
            cmd.extend(["--max-turns", str(claude_cfg.max_turns)])
        if claude_cfg.max_budget_usd is not None:
            cmd.extend(["--max-budget-usd", str(claude_cfg.max_budget_usd)])
        cmd.extend(claude_cfg.extra_args)
        return cmd

    def _run_claude(
        self,
        prompt: str,
        cwd: Path,
        model: str,
        claude_cfg: ClaudeConfig,
        timeout: Optional[float] = None,
        skill_names: Optional[list[str]] = None,
    ) -> RunResult:
        cmd = self.claude_command(prompt, model, claude_cfg)
        env = os.environ.copy()
        for var in PARENT_SESSION_VARS:
            env.pop(var, None)
        if claude_cfg.auth == "subscription":
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)

        start_time = time.perf_counter()
        try:
            execution = self._exec(cmd, cwd, env, timeout)
        except Exception as exc:
            return _failed_result(prompt, exc, round(time.perf_counter() - start_time, 2))

        parsed = parse_claude_output(execution.stdout, skill_names or [])
        exit_code = execution.exit_code
        error = None
        if parsed["is_error"]:
            exit_code = exit_code or 1
            error = parsed["error"]
        elif exit_code != 0:
            error = execution.stderr or parsed["error"]

        result = RunResult(
            prompt=prompt,
            response=parsed["response"] if parsed["response"] is not None else execution.stdout,
            transcript=f"STDOUT:\n{execution.stdout}\n\nSTDERR:\n{execution.stderr}",
            duration=execution.duration,
            cost=parsed["cost"],
            input_tokens=parsed["input_tokens"],
            output_tokens=parsed["output_tokens"],
            tool_calls=parsed["tool_calls"],
            exit_code=exit_code,
            error=error,
            cache_read_tokens=parsed["cache_read_tokens"],
            cache_creation_tokens=parsed["cache_creation_tokens"],
            num_turns=parsed["num_turns"],
            skill_invoked=parsed["skill_invoked"] if skill_names else None,
            skill_available=parsed["skill_available"] if skill_names else None,
        )
        return _finalize_status(result, execution)

    def _run_codex(
        self,
        prompt: str,
        cwd: Path,
        model: str,
        codex_cfg: CodexConfig,
        timeout: Optional[float] = None,
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
            execution = self._exec(cmd, cwd, env, timeout)
        except Exception as exc:
            return _failed_result(prompt, exc, round(time.perf_counter() - start_time, 2))

        stdout = execution.stdout
        response_texts: list[str] = []
        cost = 0.0
        input_tokens = 0
        cached_tokens = 0
        output_tokens = 0
        tool_calls = 0
        num_turns = 0

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", ""))

            # Current `codex exec --json` wraps work in item.* events.
            inner = item.get("item")
            if isinstance(inner, dict):
                if item_type == "item.completed":
                    inner_type = str(inner.get("type", ""))
                    if inner_type in {"agent_message", "assistant_message", "message"}:
                        text = inner.get("text") or inner.get("content")
                        if isinstance(text, str) and text:
                            response_texts.append(text)
                    elif inner_type not in {"reasoning", "todo_list", "error"}:
                        tool_calls += 1
                continue

            if item_type == "turn.completed":
                num_turns += 1
            elif "tool" in item_type or item.get("tool_calls"):
                tool_calls += 1

            usage = item.get("usage")
            if isinstance(usage, dict):
                if item_type == "turn.completed":
                    input_tokens += int(usage.get("input_tokens") or 0)
                    cached_tokens += int(usage.get("cached_input_tokens") or 0)
                    output_tokens += int(usage.get("output_tokens") or 0)
                else:
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

        # Codex reports cached tokens as a subset of input tokens.
        uncached_input = max(input_tokens - cached_tokens, 0) if cached_tokens else input_tokens
        result = RunResult(
            prompt=prompt,
            response=response_texts[-1] if response_texts else stdout,
            transcript=f"STDOUT:\n{stdout}\n\nSTDERR:\n{execution.stderr}",
            duration=execution.duration,
            cost=cost,
            input_tokens=uncached_input,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
            exit_code=execution.exit_code,
            error=execution.stderr if execution.exit_code != 0 else None,
            cache_read_tokens=cached_tokens,
            num_turns=num_turns,
        )
        return _finalize_status(result, execution)

    def _run_opencode(
        self,
        prompt: str,
        cwd: Path,
        model: str,
        opencode_cfg: OpenCodeConfig,
        timeout: Optional[float] = None,
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

        start_time = time.perf_counter()
        try:
            execution = self._exec(cmd, cwd, os.environ.copy(), timeout)
        except Exception as exc:
            return _failed_result(prompt, exc, round(time.perf_counter() - start_time, 2))

        stdout = execution.stdout
        response_texts: list[str] = []
        cost = 0.0
        input_tokens = 0
        output_tokens = 0
        cache_read = 0
        cache_write = 0
        tool_calls = 0

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
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
                    cache = tokens.get("cache")
                    if isinstance(cache, dict):
                        cache_read += int(cache.get("read") or 0)
                        cache_write += int(cache.get("write") or 0)
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

        result = RunResult(
            prompt=prompt,
            response="\n".join(response_texts) if response_texts else stdout,
            transcript=f"STDOUT:\n{stdout}\n\nSTDERR:\n{execution.stderr}",
            duration=execution.duration,
            cost=round(cost, 6),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
            exit_code=execution.exit_code,
            error=execution.stderr if execution.exit_code != 0 else None,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_write,
        )
        return _finalize_status(result, execution)

    def _run_antigravity(
        self,
        prompt: str,
        cwd: Path,
        model: str,
        antigravity_cfg: AntigravityConfig,
        timeout: Optional[float] = None,
    ) -> RunResult:
        bin_path = antigravity_cfg.bin_path or self.antigravity_bin
        cmd = [bin_path, "-p", prompt, "--output-format", "json", "--add-dir", str(cwd)]
        target_model = model
        if target_model in {"gemini-3.8", "gemini 3.8", "gemini-3-8"}:
            target_model = "gemini-3.8-flash-medium"
        elif target_model in {"gemini-3.7", "gemini 3.7", "gemini-3-7"}:
            target_model = "gemini-3.7-flash-medium"

        if target_model:
            cmd.extend(["--model", target_model])
        if antigravity_cfg.dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if antigravity_cfg.extra_args:
            cmd.extend(antigravity_cfg.extra_args)

        start_time = time.perf_counter()
        try:
            execution = self._exec(cmd, cwd, os.environ.copy(), timeout)
        except Exception as exc:
            return _failed_result(prompt, exc, round(time.perf_counter() - start_time, 2))

        stdout = execution.stdout
        exit_code = execution.exit_code
        duration = execution.duration
        response = stdout
        cost = 0.0
        input_tokens = 0
        output_tokens = 0
        tool_calls = 0
        num_turns = 0

        try:
            data = json.loads(stdout)
            if isinstance(data, dict):
                response = data.get("response") or data.get("result") or stdout
                cost = float(data.get("total_cost_usd") or data.get("cost") or 0.0)
                if "duration_seconds" in data:
                    try:
                        duration = float(data["duration_seconds"])
                    except (ValueError, TypeError):
                        pass
                usage = data.get("usage") or {}
                input_tokens = int(usage.get("input_tokens", 0))
                output_tokens = int(usage.get("output_tokens", 0))
                num_turns = int(data.get("num_turns") or 0)
                tool_calls = int(data.get("tool_calls_count") or num_turns)
                if data.get("status") and data.get("status") != "SUCCESS" and exit_code == 0:
                    exit_code = 1
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

        result = RunResult(
            prompt=prompt,
            response=response,
            transcript=f"STDOUT:\n{stdout}\n\nSTDERR:\n{execution.stderr}",
            duration=duration,
            cost=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
            exit_code=exit_code,
            error=execution.stderr if exit_code != 0 else None,
            num_turns=num_turns,
        )
        return _finalize_status(result, execution)

    def _run_mock(self, prompt: str, cwd: Path, model: str) -> RunResult:
        mock_response = os.environ.get("SKILLDIFF_MOCK_RESPONSE", "Mock agent response")
        mock_cost = float(os.environ.get("SKILLDIFF_MOCK_COST", "0.05"))
        mock_duration = float(os.environ.get("SKILLDIFF_MOCK_DURATION", "1.5"))

        script = os.environ.get("SKILLDIFF_MOCK_SCRIPT")
        if script:
            subprocess.run(script, shell=True, cwd=cwd, check=False)

        has_skill = any(
            (cwd / base / "skills").is_dir() and any((cwd / base / "skills").iterdir())
            for base in (".claude", ".codex", ".agents")
        )
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
            num_turns=1,
            skill_invoked=has_skill,
            skill_available=has_skill,
        )


def _skill_matches(value: Any, skill_names: list[str]) -> bool:
    if not isinstance(value, str) or not value:
        return False
    # Plugin skills are namespaced as "plugin:skill".
    candidate = value.lstrip("/").split(":")[-1]
    return candidate in skill_names


def parse_claude_output(stdout: str, skill_names: list[str]) -> dict[str, Any]:
    """Parse `claude -p --output-format stream-json` (or plain `json`) output."""
    parsed: dict[str, Any] = {
        "response": None,
        "cost": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "tool_calls": 0,
        "num_turns": 0,
        "is_error": False,
        "error": None,
        "skill_invoked": False,
        "skill_available": None,
    }
    last_text: Optional[str] = None
    saw_result = False

    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict):
            events.append(event)
    if not events:
        try:
            whole = json.loads(stdout)
            if isinstance(whole, dict):
                events.append(whole)
        except (json.JSONDecodeError, ValueError):
            pass

    for event in events:
        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "init":
            skills = event.get("skills")
            if isinstance(skills, list):
                parsed["skill_available"] = any(_skill_matches(s, skill_names) for s in skills)
        elif event_type == "assistant":
            content = (event.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    last_text = block["text"]
                elif block.get("type") == "tool_use":
                    parsed["tool_calls"] += 1
                    tool_input = block.get("input") or {}
                    if block.get("name") == "Skill" and _skill_matches(
                        tool_input.get("skill") or tool_input.get("command"), skill_names
                    ):
                        parsed["skill_invoked"] = True
                    elif detect_skill_reference(json.dumps(tool_input), skill_names):
                        parsed["skill_invoked"] = True
        elif event_type == "result" or ("result" in event and event_type is None):
            saw_result = True
            result_text = event.get("result")
            parsed["response"] = result_text if isinstance(result_text, str) else last_text
            parsed["cost"] = float(event.get("total_cost_usd") or event.get("cost") or 0.0)
            usage = event.get("usage") or {}
            parsed["input_tokens"] = int(usage.get("input_tokens") or 0)
            parsed["output_tokens"] = int(usage.get("output_tokens") or 0)
            parsed["cache_read_tokens"] = int(usage.get("cache_read_input_tokens") or 0)
            parsed["cache_creation_tokens"] = int(usage.get("cache_creation_input_tokens") or 0)
            parsed["num_turns"] = int(event.get("num_turns") or 0)
            subtype = str(event.get("subtype") or "")
            if event.get("is_error") or subtype.startswith("error"):
                parsed["is_error"] = True
                parsed["error"] = (
                    result_text if isinstance(result_text, str) and result_text else subtype
                ) or "Claude reported an error"

    if not saw_result:
        parsed["response"] = last_text
        if events:
            parsed["is_error"] = True
            parsed["error"] = "Claude exited without a result event"
    return parsed

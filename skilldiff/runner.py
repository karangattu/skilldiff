import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from copy import deepcopy
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
    duration: Optional[float]
    cost: Optional[float]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    tool_calls: Optional[int]
    exit_code: int
    error: Optional[str] = None
    cache_read_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None
    num_turns: Optional[int] = None
    # True/False when the harness output says whether the agent loaded the skill;
    # None when it cannot be determined.
    skill_invoked: Optional[bool] = None
    # Whether the harness reported the skill as installed (Claude init event only).
    skill_available: Optional[bool] = None
    # "ok", "error", or "timeout".
    status: str = "ok"
    # Why a non-ok status happened. "harness_error" means the agent CLI could
    # not be invoked (bad flags, missing binary, auth); "timeout" is a deadline.
    # None on ok runs. Kept separate from status so the reporter can refuse to
    # treat "the tool would not start" as a low agent score.
    failure_kind: Optional[str] = None


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration: float
    timed_out: bool = False
    cleanup_error: Optional[str] = None


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

# Container CLIs receive only explicit provider credentials/configuration.
# Host session state, HOME, PATH and unrelated secrets stay on the host.
CONTAINER_AUTH_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
)


def resolve_container_image(runtime: str, image: Optional[str], timeout: float = 20) -> str:
    binary = shutil.which(runtime)
    if not binary:
        raise ValueError(f"Container runtime '{runtime}' not found on PATH")
    proc = subprocess.run(
        [binary, "image", "inspect", "--format", "{{.Id}}", image or "python:3.11"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    identity = proc.stdout.strip()
    if proc.returncode or not identity.startswith("sha256:") or "\n" in identity:
        raise ValueError(
            "Container image must be available locally and inspectable: "
            + (proc.stderr.strip() or image or "python:3.11")
        )
    return identity


def validate_container_auth(config: ExperimentConfig) -> None:
    if config.isolation == "local":
        return
    harness = config.harness
    if harness in {"claude", "codex"}:
        if getattr(config, harness).auth != "api_key":
            raise ValueError(f"Container {harness} execution requires {harness}.auth: api_key")
        credentials = (
            ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
            if harness == "claude"
            else ("OPENAI_API_KEY",)
        )
        if not any(os.environ.get(key) for key in credentials):
            raise ValueError(
                f"Container {harness} api_key authentication requires {' or '.join(credentials)}"
            )


def _kill_group(proc: subprocess.Popen) -> None:
    if os.name != "posix":
        if hasattr(proc, "kill"):
            try:
                proc.kill()
            except Exception:
                pass
        return
    pid = getattr(proc, "pid", None)
    if type(pid) is not int or pid <= 1:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def detect_skill_reference(text: str, skill_names: list[str]) -> bool:
    """Heuristic: the transcript mentions a path inside the installed skill directory."""
    for name in skill_names:
        pattern = rf"skills[/\\]+{re.escape(name)}(?:[/\\\"'\s]|$)"
        if re.search(pattern, text):
            return True
    return False


def detect_opencode_skill_load(transcript: str, skill_names: list[str]) -> bool:
    """Structured adoption check for OpenCode's JSON stream.

    A loaded skill shows up as the harness's own skill tool call
    (``"tool":"skill"`` with ``input.id`` equal to the skill name), as the
    injected ``<skill_content name="...">`` payload, or as a read of a file
    inside the installed skill directory. Only calls that match the skill
    under test count: agents may invoke unrelated user-level skills, and
    those must not be mistaken for adoption of the skill under test (or,
    in the control arm, for contamination).
    """
    if not skill_names:
        return False
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(item, dict):
            continue
        part = item.get("part")
        candidates: list[Any] = [item, part if isinstance(part, dict) else {}]
        for candidate in candidates:
            tool_input = candidate.get("input")
            if not isinstance(tool_input, dict):
                # OpenCode v2 nests tool inputs under the part's ``state``.
                state = candidate.get("state")
                if isinstance(state, dict):
                    tool_input = state.get("input")
            if isinstance(tool_input, dict):
                skill_id = tool_input.get("id") or tool_input.get("skill")
                if skill_id and _skill_matches(skill_id, skill_names):
                    return True
    if any(
        re.search(rf'<skill_content\s+name=["\']{re.escape(name)}["\']', transcript)
        for name in skill_names
    ):
        return True
    return detect_skill_reference(transcript, skill_names)


def _opencode_error_message(stdout: str, stderr: str) -> Optional[str]:
    """Extract OpenCode's structured error message from a run's output."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(item, dict) or item.get("type") != "error":
            continue
        error = item.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("data")
        else:
            message = item.get("message") or error
        if isinstance(message, str) and message.strip():
            return message.strip()
    return stderr.strip() or None


def _opencode_unrecognized_flags(stdout: str, stderr: str) -> list[str]:
    """Flags OpenCode rejected, e.g. "Unrecognized flag: --dir in command ..."."""
    text = f"{stdout}\n{stderr}"
    return sorted(set(re.findall(r"Unrecognized flag:\s*(--[A-Za-z0-9-]+)", text)))


AUTH_ERROR_MARKERS = (
    "failed to authenticate",
    "oauth",
    "not logged in",
    "please run /login",
    "invalid api key",
    "authentication_error",
    "401 unauthorized",
    "api error: 401",
)


def looks_like_auth_error(text: object) -> bool:
    """True when a harness error message points at expired or missing credentials."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in AUTH_ERROR_MARKERS)


def auth_login_hint(harness: str) -> str:
    return {
        "claude": "Sign in with `claude auth login`",
        "codex": "Sign in with `codex login`",
        "opencode": "Sign in with `opencode providers login`",
    }.get(harness, f"Sign in to the {harness} CLI")


def _failed_result(prompt: str, exc: Exception, duration: float) -> RunResult:
    return RunResult(
        prompt=prompt,
        response="",
        transcript=str(exc),
        duration=duration,
        cost=None,
        input_tokens=None,
        output_tokens=None,
        tool_calls=None,
        exit_code=-1,
        error=str(exc),
        status="error",
        cache_read_tokens=None,
        cache_creation_tokens=None,
        num_turns=None,
        failure_kind="harness_error",
    )


def _finalize_status(result: RunResult, execution: ExecResult) -> RunResult:
    if execution.timed_out:
        result.status = "timeout"
        result.failure_kind = result.failure_kind or "timeout"
        result.error = result.error or f"Agent timed out after {execution.duration:.0f}s"
        if result.exit_code == 0:
            result.exit_code = -1
    elif result.exit_code != 0:
        result.status = "error"
        result.failure_kind = result.failure_kind or "harness_error"
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
        # Cache of `opencode run --help` flags per binary, so capability
        # detection happens once per process instead of per session.
        self._opencode_flags: dict[str, set[str]] = {}

    def opencode_run_flags(self, bin_path: str) -> set[str]:
        """Long flags `opencode run` advertises, or an empty set if unknown.

        Empty means "could not probe" — callers must then avoid optional flags
        rather than guess, because the same CLI rejects unknown flags outright.
        """
        cached = self._opencode_flags.get(bin_path)
        if cached is not None:
            return cached
        flags: set[str] = set()
        try:
            proc = subprocess.run(
                [bin_path, "run", "--help"],
                capture_output=True,
                text=True,
                timeout=20,
                stdin=subprocess.DEVNULL,
            )
            text = (proc.stdout or "") + "\n" + (proc.stderr or "")
            flags = set(re.findall(r"--[a-z][a-z0-9-]+", text))
        except (OSError, subprocess.SubprocessError):
            flags = set()
        self._opencode_flags[bin_path] = flags
        return flags

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
        isolation: str = "local",
        container_image: Optional[str] = None,
        readonly_mounts: Optional[list[tuple[Path, str]]] = None,
        forward_env: Optional[list[str]] = None,
        temp_dir: Optional[Path] = None,
    ) -> ExecResult:
        """Run an agent CLI without a stdin pipe, with a timeout, and clean up its children.

        Output goes to temporary files rather than pipes so that background processes the
        agent leaves behind (dev servers, watchers) cannot keep the run from finishing.
        """
        if self._cancelled:
            raise RuntimeError("Cancelled before the agent started")
        if isolation not in {"local", "docker", "podman"}:
            raise ValueError(f"Unsupported isolation mode: {isolation}")

        started = time.monotonic()
        deadline = started + timeout if timeout is not None else None

        exec_cmd = list(cmd)
        env = dict(env)
        # Keep PWD consistent with the working directory we actually launch in.
        # Harnesses that resolve their project directory from PWD (Bun/OpenCode,
        # for example) otherwise operate on the caller's directory.
        env["PWD"] = str(cwd.resolve())
        if temp_dir is not None:
            for key in ("TMPDIR", "TMP", "TEMP"):
                env[key] = str(temp_dir.resolve())
        container_name = None
        runtime = None
        if sys.platform == "darwin" and isolation == "local" and shutil.which("caffeinate"):
            exec_cmd = ["caffeinate", "-i", *exec_cmd]
        elif isolation in {"docker", "podman"}:
            runtime = shutil.which(isolation)
            if not runtime:
                raise ValueError(f"Container runtime '{isolation}' not found on PATH")
            try:
                img = (container_image if container_image and container_image.startswith("sha256:")
                       else resolve_container_image(isolation, container_image,
                            min(20, timeout) if timeout is not None else 20))
            except subprocess.TimeoutExpired:
                return ExecResult("", "Container image inspection timed out", -1,
                                  round(time.monotonic() - started, 2), True)
            container_name = "skilldiff-" + uuid.uuid4().hex
            mounts = list(readonly_mounts or [])
            mappings = [(cwd.resolve(), "/workspace"), *mounts]
            if temp_dir is not None:
                mappings.append((temp_dir.resolve(), "/session-tmp"))

            def translate(value: str) -> str:
                for source, target in mappings:
                    original = str(source.resolve())
                    if value == original or value.startswith(original + "/"):
                        return target + value[len(original) :]
                return value

            container_cmd = [translate(arg) for arg in cmd]
            exec_cmd = [
                runtime,
                "run",
                "--rm",
                "--name",
                container_name,
                "-v",
                f"{cwd.resolve()}:/workspace",
                "-w",
                "/workspace",
            ]
            if temp_dir is not None:
                exec_cmd.extend(["-v", f"{temp_dir.resolve()}:/session-tmp"])
            for source, target in mounts:
                exec_cmd.extend(["-v", f"{source.resolve()}:{target}:ro"])
            container_env = {
                key: env[key]
                for key in (*CONTAINER_AUTH_VARS, *(forward_env or []),
                            *(("TMPDIR", "TMP", "TEMP") if temp_dir is not None else ()))
                if key in env
            }
            # Pass values through the client's environment, keeping credentials out of argv.
            for key, value in container_env.items():
                if temp_dir is not None and key in {"TMPDIR", "TMP", "TEMP"}:
                    # These paths are not secrets. Keep the runtime client's temp
                    # directory usable on the host, and set the container path explicitly.
                    exec_cmd.extend(["-e", f"{key}={translate(value)}"])
                else:
                    env[key] = translate(value)
                    exec_cmd.extend(["-e", key])
            exec_cmd.extend([img, *container_cmd])

        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return ExecResult("", "Task timeout exhausted before process start", -1,
                                  round(time.monotonic() - started, 2), True)
            proc = subprocess.Popen(
                exec_cmd,
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
            cleanup_error = None
            try:
                exit_code = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
                exit_code = proc.wait()
            finally:
                _kill_group(proc)
                with self._active_lock:
                    self._active.discard(proc)
                if container_name:
                    try:
                        cleanup = subprocess.run(
                            [runtime, "rm", "-f", container_name],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            text=True,
                            timeout=10,
                            check=False,
                        )
                        if cleanup.returncode and "No such container" not in cleanup.stderr:
                            cleanup_error = "Container cleanup failed: " + cleanup.stderr.strip()
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        cleanup_error = "Container cleanup failed: " + str(exc)
            duration = round(time.monotonic() - started, 2)
            out.seek(0)
            err.seek(0)
            return ExecResult(
                stdout=out.read().decode("utf-8", errors="replace"),
                stderr=err.read().decode("utf-8", errors="replace") +
                       ("\n" + cleanup_error if cleanup_error else ""),
                exit_code=exit_code if not cleanup_error else (exit_code or -1),
                duration=duration,
                timed_out=timed_out,
                cleanup_error=cleanup_error,
            )

    def _run_multi_turn(
        self,
        prompts: list[str],
        cwd: Path,
        model: str,
        config: Any,
        harness: Optional[str] = None,
        skill_names: Optional[list[str]] = None,
        timeout: Optional[float] = None,
        temp_dir: Optional[Path] = None,
    ) -> RunResult:
        started = time.monotonic()
        deadline = started + timeout if timeout is not None else None
        metrics = (
            "cost",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "tool_calls",
            "num_turns",
        )
        totals = dict.fromkeys(metrics, 0)
        skill_invoked = False
        skill_available = None
        transcripts: list[str] = []
        last_res: Optional[RunResult] = None

        for idx, turn_prompt in enumerate(prompts):
            turn_timeout = None if deadline is None else deadline - time.monotonic()
            if turn_timeout is not None and turn_timeout <= 0:
                if last_res is not None:
                    last_res.status = "timeout"
                    last_res.exit_code = -1
                    last_res.error = "Task timeout exhausted before the next turn"
                break
            res = self._run_session(
                turn_prompt, cwd, model, config, harness, skill_names, turn_timeout,
                temp_dir=temp_dir,
            )
            last_res = res
            for metric in metrics:
                value = getattr(res, metric)
                totals[metric] = (
                    None if value is None or totals[metric] is None else totals[metric] + value
                )
            if res.skill_invoked:
                skill_invoked = True
            if res.skill_available is not None:
                skill_available = res.skill_available
            turn_header = (
                f"--- TURN {idx + 1} ---\nPROMPT: {turn_prompt}\nRESPONSE:\n{res.response}"
            )
            transcripts.append(f"{turn_header}\n\nTRANSCRIPT:\n{res.transcript}")

            if res.status != "ok" or res.exit_code != 0:
                break

        if last_res is None:
            return _failed_result("", ValueError("No turns executed"), 0.0)

        return RunResult(
            prompt="\n---\n".join(prompts),
            response=last_res.response,
            transcript="\n\n".join(transcripts),
            duration=round(time.monotonic() - started, 2),
            cost=round(totals["cost"], 6) if totals["cost"] is not None else None,
            input_tokens=totals["input_tokens"],
            output_tokens=totals["output_tokens"],
            tool_calls=totals["tool_calls"],
            exit_code=last_res.exit_code,
            error=last_res.error,
            cache_read_tokens=totals["cache_read_tokens"],
            cache_creation_tokens=totals["cache_creation_tokens"],
            num_turns=totals["num_turns"],
            skill_invoked=skill_invoked,
            skill_available=skill_available,
            status=last_res.status,
        )

    def run(
        self,
        prompt: str | list[str],
        cwd: Path,
        model: str,
        config: ExperimentConfig | ClaudeConfig | Any,
        harness: Optional[str] = None,
        skill_names: Optional[list[str]] = None,
        timeout: Optional[float] = None,
    ) -> RunResult:
        # Workspaces already have private roots. Keep temporary files outside
        # the fixture/diff, reuse them across turns, and never mutate os.environ.
        with tempfile.TemporaryDirectory(prefix="tmp-", dir=cwd.resolve().parent) as tmp:
            return self._run_session(
                prompt, cwd, model, config, harness, skill_names, timeout,
                temp_dir=Path(tmp),
            )

    def _run_session(
        self,
        prompt: str | list[str],
        cwd: Path,
        model: str,
        config: ExperimentConfig | ClaudeConfig | Any,
        harness: Optional[str] = None,
        skill_names: Optional[list[str]] = None,
        timeout: Optional[float] = None,
        temp_dir: Optional[Path] = None,
    ) -> RunResult:
        timeout = timeout if timeout is not None else getattr(config, "timeout_seconds", None)
        if isinstance(config, ExperimentConfig):
            validate_container_auth(config)
        if timeout is not None and timeout <= 0:
            result = _failed_result(str(prompt), TimeoutError("Task timeout exhausted"), 0.0)
            result.status = "timeout"
            return result
        if isinstance(prompt, list):
            if not prompt:
                return _failed_result("", ValueError("Empty prompt list"), 0.0)
            if len(prompt) == 1:
                prompt = prompt[0]
            else:
                return self._run_multi_turn(
                    prompt, cwd, model, config, harness, skill_names, timeout, temp_dir
                )

        if os.environ.get("SKILLDIFF_MOCK_RUNNER"):
            return self._run_mock(prompt, cwd, model)

        isolation = getattr(config, "isolation", "local")
        container_image = getattr(config, "container_image", None)

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

        if isolation in {"docker", "podman"}:
            # An explicitly configured bin_path is a path in the image. Automatically
            # discovered host paths instead refer to the same executable name in PATH.
            configs = {
                "claude": claude_cfg,
                "codex": codex_cfg,
                "opencode": opencode_cfg,
                "antigravity": antigravity_cfg,
            }
            configs = {key: deepcopy(value) for key, value in configs.items()}
            for key, value in configs.items():
                if not value.bin_path:
                    value.bin_path = Path(self.binary_for(key)).name
            claude_cfg, codex_cfg = configs["claude"], configs["codex"]
            opencode_cfg, antigravity_cfg = configs["opencode"], configs["antigravity"]

        names = skill_names or []
        if active_harness == "codex":
            result = self._run_codex(
                prompt,
                cwd,
                model,
                codex_cfg,
                timeout,
                isolation=isolation,
                container_image=container_image,
                temp_dir=temp_dir,
            )
        elif active_harness == "opencode":
            result = self._run_opencode(
                prompt,
                cwd,
                model,
                opencode_cfg,
                timeout,
                isolation=isolation,
                container_image=container_image,
                temp_dir=temp_dir,
                skill_names=names,
            )
        elif active_harness in {"antigravity", "agy"}:
            result = self._run_antigravity(
                prompt,
                cwd,
                model,
                antigravity_cfg,
                timeout,
                isolation=isolation,
                container_image=container_image,
                temp_dir=temp_dir,
            )
        else:
            result = self._run_claude(
                prompt,
                cwd,
                model,
                claude_cfg,
                timeout,
                names,
                isolation=isolation,
                container_image=container_image,
                temp_dir=temp_dir,
            )

        if result.skill_invoked is None and names and result.transcript:
            result.skill_invoked = detect_skill_reference(result.transcript, names)
        return result

    def probe_claude_auth(
        self, model: str, claude_cfg: ClaudeConfig, timeout: float = 90
    ) -> tuple[bool, str]:
        """Make one tiny authenticated call so expired credentials fail before a run.

        Returns (ok, detail). Uses a single turn, no tools, and a small budget cap.
        """
        import dataclasses

        probe_cfg = dataclasses.replace(
            claude_cfg,
            max_turns=1,
            max_budget_usd=0.05,
            allowed_tools=[],
            effort=None,
            permission_mode=None,
        )
        with tempfile.TemporaryDirectory(prefix="skilldiff-auth-probe-") as tmp:
            result = self._run_claude(
                "Reply with the single word OK.", Path(tmp), model, probe_cfg, timeout=timeout
            )
        if result.status == "ok" and not result.error:
            return True, "authenticated call succeeded"
        detail = (result.error or result.status or "unknown failure").strip().splitlines()
        return False, (detail[0] if detail else "unknown failure")[:300]

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
        isolation: str = "local",
        container_image: Optional[str] = None,
        temp_dir: Optional[Path] = None,
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
            execution = self._exec(
                cmd, cwd, env, timeout, isolation=isolation, container_image=container_image,
                temp_dir=temp_dir,
            )
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
        isolation: str = "local",
        container_image: Optional[str] = None,
        temp_dir: Optional[Path] = None,
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
            execution = self._exec(
                cmd, cwd, env, timeout, isolation=isolation, container_image=container_image,
                temp_dir=temp_dir,
            )
        except Exception as exc:
            return _failed_result(prompt, exc, round(time.perf_counter() - start_time, 2))

        stdout = execution.stdout
        response_texts: list[str] = []
        cost = 0.0
        input_tokens = 0
        cached_tokens = 0
        cache_write_tokens = 0
        output_tokens = 0
        tool_calls = 0
        num_turns = 0
        saw_usage = saw_cost = False
        input_complete = output_complete = cache_complete = True
        cache_write_complete = True
        saw_cache_write = False

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
                saw_usage = True
                input_complete = input_complete and any(
                    k in usage for k in ("input_tokens", "prompt_tokens")
                )
                output_complete = output_complete and any(
                    k in usage for k in ("output_tokens", "completion_tokens")
                )
                cache_complete = cache_complete and "cached_input_tokens" in usage
                if item_type == "turn.completed":
                    input_tokens += int(usage.get("input_tokens") or 0)
                    cached_tokens += int(usage.get("cached_input_tokens") or 0)
                    output_tokens += int(usage.get("output_tokens") or 0)
                    cache_write_complete = cache_write_complete and (
                        "cache_write_input_tokens" in usage
                    )
                    if "cache_write_input_tokens" in usage:
                        saw_cache_write = True
                        cache_write_tokens += int(usage.get("cache_write_input_tokens") or 0)
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
                    saw_cost = True
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
            cost=cost if saw_cost else None,
            input_tokens=uncached_input if saw_usage and input_complete else None,
            output_tokens=output_tokens if saw_usage and output_complete else None,
            tool_calls=tool_calls if num_turns or tool_calls else None,
            exit_code=execution.exit_code,
            error=execution.stderr if execution.exit_code != 0 else None,
            cache_read_tokens=cached_tokens if saw_usage and cache_complete else None,
            # The Codex stream can omit cache-write usage while still reporting
            # input, cached-input, and output counts. Keep those token and cost
            # calculations usable; the missing write component contributes 0.
            cache_creation_tokens=(
                cache_write_tokens if saw_cache_write and cache_write_complete
                else 0 if saw_usage and input_complete and output_complete and not saw_cache_write
                else None
            ),
            num_turns=num_turns or None,
        )
        return _finalize_status(result, execution)

    def _run_opencode(
        self,
        prompt: str,
        cwd: Path,
        model: str,
        opencode_cfg: OpenCodeConfig,
        timeout: Optional[float] = None,
        isolation: str = "local",
        container_image: Optional[str] = None,
        temp_dir: Optional[Path] = None,
        skill_names: Optional[list[str]] = None,
    ) -> RunResult:
        bin_path = opencode_cfg.bin_path or self.opencode_bin
        target_model = model
        if opencode_cfg.service == "go" or opencode_cfg.provider == "opencode-go":
            if target_model.startswith("opencode/"):
                target_model = f"opencode-go/{target_model[len('opencode/') :]}"
            elif "/" not in target_model:
                target_model = f"opencode-go/{target_model}"
        elif "/" not in target_model and opencode_cfg.provider:
            target_model = f"{opencode_cfg.provider}/{target_model}"

        # Probe the installed CLI once and build only flags it advertises.
        # `--dir` is deliberately never passed: skilldiff already launches the
        # process in `cwd` (and pins PWD), and OpenCode v2 rejects `--dir`.
        probed = self.opencode_run_flags(bin_path)

        def build_cmd(skip: set[str]) -> list[str]:
            target = target_model
            cmd = [bin_path, "run"]
            if "--format" not in skip:
                cmd.extend(["--format", "json"])
            if (
                opencode_cfg.dangerously_skip_permissions
                and "--dangerously-skip-permissions" not in skip
            ):
                cmd.append("--dangerously-skip-permissions")
            if opencode_cfg.variant:
                if "--variant" in skip or (probed and "--variant" not in probed):
                    # v2 carries the variant in the model id instead of a flag.
                    target = f"{target}#{opencode_cfg.variant}"
                else:
                    cmd.extend(["--variant", str(opencode_cfg.variant)])
            if opencode_cfg.isolate and "--standalone" in probed and "--standalone" not in skip:
                cmd.append("--standalone")
            cmd.extend(arg for arg in opencode_cfg.extra_args if arg not in skip)
            cmd.extend(["-m", target, prompt])
            return cmd

        cmd = build_cmd(set())
        env = os.environ.copy()

        start_time = time.perf_counter()
        try:
            execution = self._exec(
                cmd,
                cwd,
                env,
                timeout,
                isolation=isolation,
                container_image=container_image,
                temp_dir=temp_dir,
            )
        except Exception as exc:
            return _failed_result(prompt, exc, round(time.perf_counter() - start_time, 2))

        # Self-heal a flag the installed CLI rejects ("Unrecognized flag: --x"):
        # drop it and retry once so a single version drift cannot kill a run.
        rejected = _opencode_unrecognized_flags(execution.stdout, execution.stderr)
        if execution.exit_code != 0 and rejected:
            retry_cmd = build_cmd(set(rejected))
            if retry_cmd != cmd:
                try:
                    execution = self._exec(
                        retry_cmd,
                        cwd,
                        env,
                        timeout,
                        isolation=isolation,
                        container_image=container_image,
                        temp_dir=temp_dir,
                    )
                    cmd = retry_cmd
                except Exception as exc:
                    return _failed_result(
                        prompt, exc, round(time.perf_counter() - start_time, 2)
                    )

        stdout = execution.stdout
        response_texts: list[str] = []
        cost = 0.0
        input_tokens = 0
        output_tokens = 0
        cache_read = 0
        cache_write = 0
        tool_calls = 0
        saw_cost = saw_stream = False
        saw_input = saw_output = saw_cache_read = saw_cache_write = False

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
            saw_stream = saw_stream or item_type in {
                "step_start",
                "step_finish",
                "step-start",
                "step-finish",
            }
            if "tool" in item_type or item.get("tool"):
                tool_calls += 1

            part = item.get("part")
            if isinstance(part, dict):
                text = part.get("text")
                if text and isinstance(text, str):
                    response_texts.append(text)
                tokens = part.get("tokens")
                if isinstance(tokens, dict):
                    saw_input = saw_input or "input" in tokens
                    saw_output = saw_output or "output" in tokens
                    saw_cache_read = saw_cache_read or "read" in (tokens.get("cache") or {})
                    saw_cache_write = saw_cache_write or "write" in (tokens.get("cache") or {})
                    input_tokens += int(tokens.get("input") or 0)
                    output_tokens += int(tokens.get("output") or 0)
                    cache = tokens.get("cache")
                    if isinstance(cache, dict):
                        cache_read += int(cache.get("read") or 0)
                        cache_write += int(cache.get("write") or 0)
                if "cost" in part:
                    try:
                        cost += float(part["cost"])
                        saw_cost = True
                    except (ValueError, TypeError):
                        pass

            if "cost" in item:
                try:
                    cost += float(item["cost"])
                    saw_cost = True
                except (ValueError, TypeError):
                    pass

            if "text" in item and isinstance(item["text"], str):
                response_texts.append(item["text"])

        # OpenCode reports invocation failures as a structured error event.
        # Surface the message instead of a bare exit code, and force a non-ok
        # exit so it is classified as a harness error, not a low score.
        error_message = _opencode_error_message(stdout, execution.stderr)
        exit_code = execution.exit_code
        if error_message and exit_code == 0:
            exit_code = 1

        result = RunResult(
            prompt=prompt,
            response="\n".join(response_texts) if response_texts else stdout,
            transcript=f"STDOUT:\n{stdout}\n\nSTDERR:\n{execution.stderr}",
            duration=execution.duration,
            cost=round(cost, 6) if saw_cost else None,
            input_tokens=input_tokens if saw_input else None,
            output_tokens=output_tokens if saw_output else None,
            tool_calls=tool_calls if saw_stream or tool_calls else None,
            exit_code=exit_code,
            error=error_message if exit_code != 0 else None,
            cache_read_tokens=cache_read if saw_cache_read else None,
            cache_creation_tokens=cache_write if saw_cache_write else None,
            skill_invoked=(
                detect_opencode_skill_load(stdout, skill_names) if skill_names else None
            ),
        )
        return _finalize_status(result, execution)

    def _run_antigravity(
        self,
        prompt: str,
        cwd: Path,
        model: str,
        antigravity_cfg: AntigravityConfig,
        timeout: Optional[float] = None,
        isolation: str = "local",
        container_image: Optional[str] = None,
        temp_dir: Optional[Path] = None,
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
            execution = self._exec(
                cmd,
                cwd,
                os.environ.copy(),
                timeout,
                isolation=isolation,
                container_image=container_image,
                temp_dir=temp_dir,
            )
        except Exception as exc:
            return _failed_result(prompt, exc, round(time.perf_counter() - start_time, 2))

        stdout = execution.stdout
        exit_code = execution.exit_code
        duration = execution.duration
        response = stdout
        cost = input_tokens = output_tokens = tool_calls = num_turns = None
        cache_read_tokens = None
        data = None
        try:
            data = json.loads(stdout)
            if isinstance(data, dict):
                response = data.get("response") or data.get("result") or stdout
                raw_cost = data.get("total_cost_usd", data.get("cost"))
                cost = float(raw_cost) if raw_cost is not None else None
                usage = data.get("usage") or {}
                input_tokens = (
                    int(usage["input_tokens"]) if usage.get("input_tokens") is not None else None
                )
                output_tokens = (
                    int(usage["output_tokens"]) if usage.get("output_tokens") is not None else None
                )
                cache_read_tokens = (
                    int(usage["cache_read_tokens"])
                    if usage.get("cache_read_tokens") is not None
                    else None
                )
                num_turns = int(data["num_turns"]) if data.get("num_turns") is not None else None
                tool_calls = (
                    int(data["tool_calls_count"])
                    if data.get("tool_calls_count") is not None
                    else None
                )
                if data.get("status") and data.get("status") != "SUCCESS" and exit_code == 0:
                    exit_code = 1
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

        transcript = f"STDOUT:\n{stdout}\n\nSTDERR:\n{execution.stderr}"
        if isinstance(data, dict):
            conv_id = data.get("conversation_id")
            if conv_id:
                cli_dir = Path.home() / ".gemini" / "antigravity-cli" / "brain" / conv_id
                ide_dir = Path.home() / ".gemini" / "antigravity" / "brain" / conv_id
                bases = [
                    cli_dir / ".system_generated" / "logs",
                    ide_dir / ".system_generated" / "logs",
                ]
                for base in bases:
                    target = None
                    for name in ("transcript_full.jsonl", "transcript.jsonl"):
                        candidate = base / name
                        if candidate.exists():
                            target = candidate
                            break
                    if target:
                        try:
                            log_text = target.read_text(encoding="utf-8")
                            transcript += f"\n\n--- AGY SESSION LOG ({conv_id}) ---\n{log_text}"
                            break
                        except Exception:
                            pass

        result = RunResult(
            prompt=prompt,
            response=response,
            transcript=transcript,
            duration=duration,
            cost=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
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
            cache_read_tokens=0,
            cache_creation_tokens=0,
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
        "cost": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
        "tool_calls": None,
        "num_turns": None,
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
            if parsed["tool_calls"] is None:
                parsed["tool_calls"] = 0
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
            raw_cost = event.get("total_cost_usd", event.get("cost"))
            parsed["cost"] = float(raw_cost) if raw_cost is not None else None
            usage = event.get("usage") or {}
            for target, source in (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("cache_read_tokens", "cache_read_input_tokens"),
                ("cache_creation_tokens", "cache_creation_input_tokens"),
            ):
                parsed[target] = int(usage[source]) if usage.get(source) is not None else None
            parsed["num_turns"] = (
                int(event["num_turns"]) if event.get("num_turns") is not None else None
            )
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

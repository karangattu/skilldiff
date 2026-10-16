import functools
import json
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
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
    read_skill_name,
)
from skilldiff.conversations import Conversation
from skilldiff.preflight import codex_env, protected_read_paths
from skilldiff.processes import OwnedDescendants
from skilldiff.telemetry import redact_secrets


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
    # Whether initialization or a workspace discovery probe established availability.
    skill_available: Optional[bool] = None
    # "ok", "error", or "timeout".
    status: str = "ok"
    # Why a non-ok status happened. "harness_error" means the agent CLI could
    # not be invoked (bad flags, missing binary, auth); "timeout" is a deadline.
    # None on ok runs. Kept separate from status so the reporter can refuse to
    # treat "the tool would not start" as a low agent score.
    failure_kind: Optional[str] = None
    # Tool calls the harness refused (no one answers prompts in headless runs),
    # e.g. ["Bash", "Edit"] for Claude, "bash" for OpenCode, or
    # "shell (sandbox: filesystem)" for Codex. None when not reported.
    permission_denials: Optional[list[str]] = None
    session_id: Optional[str] = None
    reported_total_tokens: Optional[int] = None


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration: float
    timed_out: bool = False
    cleanup_error: Optional[str] = None


# Variables that tie a process to the agent session it runs in. When skilldiff is
# started from inside an agent (e.g. by the skilldiff skill), each agent session must
# be independent, so these are removed from every child CLI. Auth and provider
# settings are left alone.
PARENT_SESSION_VARS = frozenset(
    {
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
        "CLAUDE_CODE_OAUTH_SCOPES",
        "CLAUDE_CODE_ORGANIZATION_UUID",
        "CLAUDE_CODE_ACCOUNT_UUID",
        "CLAUDE_CODE_USER_EMAIL",
        "CLAUDE_CODE_TERMINAL_MCP_TOOLS",
        "CLAUDE_CODE_DISABLE_CRON",
        "CLAUDE_CODE_DISABLE_TERMINAL_TITLE",
        "CLAUDE_CODE_SIMPLE",
        "CLAUDE_CODE_SAFE_MODE",
        "CLAUDE_AGENT_SDK_MCP_NO_PREFIX",
        "CLAUDE_PREVIEW_CLASSIFIER_FLOOR",
        "AI_AGENT",
        "OPENCODE",
        "OPENCODE_SESSION_ID",
        "OPENCODE_TERMINAL",
        "ANTIGRAVITY_CONVERSATION_ID",
        "ANTIGRAVITY_PROJECT_ID",
        "ANTIGRAVITY_LS_ADDRESS",
        "ANTIGRAVITY_CSRF_TOKEN",
        "ANTIGRAVITY_SIDECAR_UI_TOKEN",
    }
)

CONTAINER_PATH_VARS = frozenset(
    {
        "TMPDIR",
        "TMP",
        "TEMP",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "XDG_DATA_HOME",
        "OPENCODE_CONFIG_DIR",
        "IS_SANDBOX",
    }
)

CONTAINER_AUTH_VARS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "IS_SANDBOX",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_HOME",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "OPENCODE_API_KEY",
        "OPENROUTER_API_KEY",
        "XDG_DATA_HOME",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_DISABLE_CLAUDE_CODE",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS",
        "OPENCODE_DISABLE_AUTOUPDATE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    }
)

_CLAUDE_SAFE_STATE_KEYS = (
    "oauthAccount",
    "userID",
    "hasCompletedOnboarding",
    "lastOnboardingVersion",
    "firstStartTime",
    "claudeCodeFirstTokenDate",
    "bypassPermissionsModeAccepted",
)

_ANTIGRAVITY_AUTH_FILES = (
    "jetski-standalone-oauth-token",
    "oauth_creds.json",
    "google_accounts.json",
    "google_account_id",
    "installation_id",
    "state.json",
    "antigravity-cli/jetski_state.pbtxt",
    "antigravity-cli/installation_id",
)


@functools.lru_cache(maxsize=256)
def _cached_which_impl(cmd: str, path: Optional[str]) -> Optional[str]:
    return shutil.which(cmd, path=path)


def cached_which(cmd: str, path: Optional[str] = None) -> Optional[str]:
    if path is None:
        path = os.environ.get("PATH")
    return _cached_which_impl(cmd, path)


cached_which.cache_clear = _cached_which_impl.cache_clear
cached_which.cache_info = _cached_which_impl.cache_info


def clean_parent_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in PARENT_SESSION_VARS}


def _real_user_home() -> Path:
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path.home()


def _read_claude_keychain() -> Optional[str]:
    if sys.platform != "darwin":
        return None
    try:
        if Path.home().resolve() != _real_user_home().resolve():
            return None
    except OSError:
        return None
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        secret = proc.stdout.strip()
        if proc.returncode == 0 and secret.startswith("{"):
            return secret
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _claude_credentials_json(env: Mapping[str, str]) -> Optional[str]:
    config_dir = Path(env.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    cred_file = config_dir / ".credentials.json"
    if cred_file.is_file():
        try:
            text = cred_file.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    if "CLAUDE_CONFIG_DIR" not in env:
        return _read_claude_keychain()
    return None


def _has_claude_subscription_auth(env: Mapping[str, str]) -> bool:
    return bool(env.get("CLAUDE_CODE_OAUTH_TOKEN") or _claude_credentials_json(env))


def _has_codex_stored_auth(env: Mapping[str, str]) -> bool:
    codex_home = Path(env.get("CODEX_HOME") or (Path.home() / ".codex"))
    auth_file = codex_home / "auth.json"
    try:
        return auth_file.is_file() and auth_file.stat().st_size > 2
    except OSError:
        return False


def _collect_opencode_auth(env: Mapping[str, str]) -> dict[str, Any]:
    data_root = Path(env.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "opencode"
    merged: dict[str, Any] = {}
    auth_file = data_root / "auth.json"
    if auth_file.is_file():
        try:
            raw = json.loads(auth_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                merged.update(raw)
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    db_file = data_root / "opencode.db"
    if db_file.is_file():
        try:
            conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=2.0)
            try:
                rows = conn.execute("SELECT integration_id, value FROM credential").fetchall()
            finally:
                conn.close()
            for integration_id, raw_val in rows:
                if not integration_id or not raw_val:
                    continue
                try:
                    parsed = json.loads(raw_val)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    if parsed.get("type") == "key" and "key" in parsed:
                        merged.setdefault(
                            str(integration_id), {"type": "api", "key": parsed["key"]}
                        )
                    else:
                        merged.setdefault(str(integration_id), parsed)
        except (OSError, sqlite3.Error):
            pass
    return merged


def _has_opencode_auth(env: Mapping[str, str]) -> bool:
    if _collect_opencode_auth(env):
        return True
    config_root = Path(env.get("OPENCODE_CONFIG_DIR") or (Path.home() / ".config" / "opencode"))
    if (config_root / "service.json").is_file():
        return True
    return any(
        env.get(k)
        for k in (
            "OPENCODE_API_KEY",
            "OPENROUTER_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
        )
    )


def _antigravity_auth_files() -> list[tuple[Path, str]]:
    gemini_root = Path.home() / ".gemini"
    found: list[tuple[Path, str]] = []
    for rel in _ANTIGRAVITY_AUTH_FILES:
        src = gemini_root / rel
        if src.is_file():
            found.append((src, rel))
    return found


def _has_antigravity_auth(env: Mapping[str, str]) -> bool:
    if env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"):
        return True
    present = {rel for _, rel in _antigravity_auth_files()}
    return bool(
        present
        & {
            "jetski-standalone-oauth-token",
            "oauth_creds.json",
            "antigravity-cli/jetski_state.pbtxt",
        }
    )


def _stage_opencode_db(
    host_db: Path, target_db: Path, opencode_creds: Mapping[str, Any]
) -> None:
    if target_db.exists() or not host_db.is_file():
        return
    try:
        src = sqlite3.connect(f"file:{host_db}?mode=ro", uri=True, timeout=2.0)
        dst = sqlite3.connect(target_db)
        try:
            for (sql,) in src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            ).fetchall():
                dst.execute(sql)
            for (sql,) in src.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            ).fetchall():
                dst.execute(sql)
            for table in (
                "__drizzle_migrations",
                "migration",
                "credential",
                "account",
                "account_state",
                "control_account",
            ):
                try:
                    rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
                    if rows:
                        placeholders = ",".join("?" * len(rows[0]))
                        dst.executemany(
                            f'INSERT INTO "{table}" VALUES ({placeholders})', rows
                        )
                except sqlite3.Error:
                    pass
            now_ms = int(time.time() * 1000)
            for provider, entry in opencode_creds.items():
                if not isinstance(entry, dict):
                    continue
                exists = dst.execute(
                    "SELECT 1 FROM credential WHERE integration_id = ?", (provider,)
                ).fetchone()
                if exists:
                    continue
                if entry.get("type") in {"api", "key"} and "key" in entry:
                    value = json.dumps({"type": "key", "key": entry["key"]})
                    label = "API key"
                else:
                    value = json.dumps(entry)
                    label = "OAuth"
                dst.execute(
                    "INSERT INTO credential "
                    "(id, integration_id, label, value, time_created, time_updated) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (f"cred_{uuid.uuid4().hex[:24]}", provider, label, value, now_ms, now_ms),
                )
            dst.commit()
        finally:
            dst.close()
            src.close()
        target_db.chmod(0o600)
    except (OSError, sqlite3.Error):
        pass


def stage_container_auth(temp_dir: Path, env: dict[str, str]) -> Optional[Path]:
    """Stage host subscription credentials into temp_dir without exposing skills or history."""
    env["IS_SANDBOX"] = "1"
    claude_creds = _claude_credentials_json(env)
    if claude_creds or env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        claude_home = temp_dir / "claude-home"
        claude_home.mkdir(parents=True, exist_ok=True)
        cred_path = claude_home / ".credentials.json"
        if claude_creds and not cred_path.exists():
            cred_path.write_text(claude_creds, encoding="utf-8")
            cred_path.chmod(0o600)
        state: dict[str, Any] = {}
        for candidate in (
            Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json"
            if env.get("CLAUDE_CONFIG_DIR")
            else None,
            Path.home() / ".claude.json",
            Path.home() / ".claude" / ".claude.json",
        ):
            if candidate and candidate.is_file():
                try:
                    loaded = json.loads(candidate.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        for key in _CLAUDE_SAFE_STATE_KEYS:
                            if key in loaded:
                                state[key] = loaded[key]
                        break
                except (OSError, json.JSONDecodeError, ValueError):
                    pass
        state["hasCompletedOnboarding"] = True
        state["bypassPermissionsModeAccepted"] = True
        state_path = claude_home / ".claude.json"
        if not state_path.exists():
            state_path.write_text(json.dumps(state), encoding="utf-8")
            state_path.chmod(0o600)
        env["CLAUDE_CONFIG_DIR"] = str(claude_home.resolve())

    host_codex = Path(env.get("CODEX_HOME") or (Path.home() / ".codex"))
    codex_auth = host_codex / "auth.json"
    if codex_auth.is_file():
        codex_home = temp_dir / "codex-home"
        codex_home.mkdir(parents=True, exist_ok=True)
        target_codex = codex_home / "auth.json"
        if not target_codex.exists():
            shutil.copyfile(codex_auth, target_codex)
            target_codex.chmod(0o600)
        env["CODEX_HOME"] = "/root/.codex"

    opencode_data_root = (
        Path(env.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "opencode"
    )
    opencode_creds = _collect_opencode_auth(env)
    if opencode_creds:
        opencode_data = temp_dir / "opencode-data"
        opencode_dir = opencode_data / "opencode"
        opencode_dir.mkdir(parents=True, exist_ok=True)
        target_opencode = opencode_dir / "auth.json"
        if not target_opencode.exists():
            target_opencode.write_text(json.dumps(opencode_creds), encoding="utf-8")
            target_opencode.chmod(0o600)
        _stage_opencode_db(
            opencode_data_root / "opencode.db", opencode_dir / "opencode.db", opencode_creds
        )
        env["XDG_DATA_HOME"] = str(opencode_data.resolve())
    host_opencode_cfg = Path(
        env.get("OPENCODE_CONFIG_DIR") or (Path.home() / ".config" / "opencode")
    )
    if (host_opencode_cfg / "service.json").is_file():
        isolated_cfg = temp_dir / "opencode_config"
        isolated_cfg.mkdir(parents=True, exist_ok=True)
        target_service = isolated_cfg / "service.json"
        if not target_service.exists():
            shutil.copyfile(host_opencode_cfg / "service.json", target_service)
        env.setdefault("OPENCODE_CONFIG_DIR", str(isolated_cfg.resolve()))

    agy_files = _antigravity_auth_files()
    if agy_files:
        gemini_home = temp_dir / "gemini-home"
        for src, rel in agy_files:
            dst = gemini_home / rel
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                dst.chmod(0o600)
        return gemini_home
    return None


DEFAULT_CONTAINER_IMAGE = "ghcr.io/karangattu/skilldiff-sandbox:latest"


def resolve_container_image(runtime: str, image: Optional[str], timeout: float = 20) -> str:
    binary = cached_which(runtime)
    if not binary:
        raise ValueError(f"Container runtime '{runtime}' not found on PATH")
    candidates = [image] if image else [DEFAULT_CONTAINER_IMAGE, "python:3.11"]
    last_err = ""
    for candidate in candidates:
        proc = subprocess.run(
            [binary, "image", "inspect", "--format", "{{.Id}}", candidate],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        identity = proc.stdout.strip()
        if not proc.returncode and identity.startswith("sha256:") and "\n" not in identity:
            return identity
        last_err = proc.stderr.strip() or candidate
    raise ValueError(
        "Container image must be available locally and inspectable: "
        + (last_err or image or DEFAULT_CONTAINER_IMAGE)
    )


def validate_container_auth(config: ExperimentConfig) -> None:
    if config.isolation not in {"docker", "podman"}:
        return
    env = clean_parent_env()
    if getattr(config, "env", None):
        env.update(config.env)
    harness = config.harness
    if harness == "claude":
        if config.claude.auth == "api_key":
            credentials = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
            if not any(env.get(key) for key in credentials):
                raise ValueError(
                    f"Container claude api_key authentication requires {' or '.join(credentials)}"
                )
        elif not _has_claude_subscription_auth(env):
            raise ValueError(
                "Container claude subscription authentication requires a signed-in Claude login "
                "(`claude auth login`) or claude.auth: api_key"
            )
    elif harness == "codex":
        if config.codex.auth == "api_key":
            if not env.get("OPENAI_API_KEY"):
                raise ValueError("Container codex api_key authentication requires OPENAI_API_KEY")
        elif not _has_codex_stored_auth(env):
            raise ValueError(
                "Container codex stored authentication requires a signed-in Codex login "
                "(`codex login`) or codex.auth: api_key"
            )
    elif harness == "opencode":
        if not _has_opencode_auth(env):
            raise ValueError(
                "Container opencode execution requires a signed-in OpenCode login "
                "(`opencode auth login`) or provider api_key"
            )
    elif harness in {"antigravity", "agy"}:
        if not _has_antigravity_auth(env):
            raise ValueError(
                "Container antigravity execution requires a signed-in Antigravity login "
                "(`agy login`) or GEMINI_API_KEY"
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

    A loaded skill shows up as a completed skill tool call with the target ID,
    or a completed read of a file inside the installed skill directory.
    Only calls that match the skill
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
            state = candidate.get("state")
            if not isinstance(state, dict) or state.get("status") != "completed":
                continue
            tool_input = candidate.get("input")
            if not isinstance(tool_input, dict):
                # OpenCode v2 nests tool inputs under the part's ``state``.
                state = candidate.get("state")
                if isinstance(state, dict):
                    tool_input = state.get("input")
            if isinstance(tool_input, dict):
                skill_id = tool_input.get("id") or tool_input.get("skill")
                if candidate.get("tool") == "skill" and _skill_matches(skill_id, skill_names):
                    return True
                if candidate.get("tool") in {"read", "read_file"} and detect_skill_reference(
                    str(tool_input.get("filePath") or tool_input.get("path") or ""), skill_names
                ):
                    return True
    return False


def opencode_approval_flag(
    probed: set[str], skip: set[str] | frozenset[str] = frozenset()
) -> Optional[str]:
    """Require positive evidence of the current V2 headless approval flag."""
    for flag in ("--auto",):
        if flag not in skip and flag in probed:
            return flag
    return None


EXTERNAL_ISOLATION = frozenset({"macos", "docker", "podman"})
# Claude permission modes that ask before running commands. Headless sessions have
# no one to answer, so the asks become denials.
PROMPTING_MODES = frozenset({None, "acceptEdits", "default", "manual"})


def claude_sandbox_settings(
    claude_cfg: ClaudeConfig, isolation: str = "local", protected_paths: Optional[list[str]] = None
) -> Optional[dict[str, Any]]:
    """``--settings`` for Claude Code's own Bash sandbox, or None when it does not apply.

    Sandboxed Bash runs without approval, writes only to the workspace and Claude's
    temp directory, and reaches only ``allowed_domains``. Grader, solution, and skill
    sources are unreadable to commands. The sandbox cannot start inside another one,
    so external isolation replaces it.
    """
    if not claude_cfg.sandbox or isolation in EXTERNAL_ISOLATION:
        return None
    sandbox: dict[str, Any] = {
        "enabled": True,
        # Exit instead of silently running commands unsandboxed.
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
    }
    if protected_paths:
        sandbox["filesystem"] = {"denyRead": sorted(set(protected_paths))}
    if claude_cfg.allowed_domains:
        sandbox["network"] = {"allowedDomains": list(claude_cfg.allowed_domains)}
    return {"sandbox": sandbox}


def codex_sandbox_args(codex_cfg: CodexConfig, isolation: str = "local") -> list[str]:
    """Codex's own sandbox flags for one session.

    Codex's macOS seatbelt cannot start inside another sandbox
    (``sandbox_apply: Operation not permitted``), and its Linux sandbox
    usually lacks namespaces in containers. Under external isolation the
    outer boundary confines the session, so Codex's sandbox is bypassed.
    """
    if codex_cfg.dangerously_bypass_approvals_and_sandbox or isolation in EXTERNAL_ISOLATION:
        return ["--dangerously-bypass-approvals-and-sandbox"]
    args = ["-s", codex_cfg.sandbox] if codex_cfg.sandbox else []
    if codex_cfg.network_access and codex_cfg.sandbox == "workspace-write":
        args.extend(["-c", "sandbox_workspace_write.network_access=true"])
    return args


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


RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "rate_limit_error",
    "429 too many requests",
    "api error: 429",
    "status code: 429",
    "status 429",
    "quota exceeded",
    "resource exhausted",
    "resource_exhausted",
    "overloaded",
    "server is overloaded",
)


def looks_like_rate_limit(text: object) -> bool:
    """True when a harness error message points at provider rate limits or overload."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def auth_login_hint(harness: str) -> str:
    return {
        "claude": "Sign in with `claude auth login`",
        "codex": "Sign in with `codex login`",
        "opencode": "Sign in with `opencode auth login`",
    }.get(harness, f"Sign in to the {harness} CLI")


def _failed_result(prompt: str, exc: Exception, duration: float) -> RunResult:
    return RunResult(
        prompt=prompt,
        response="",
        transcript=redact_secrets(str(exc)),
        duration=duration,
        cost=None,
        input_tokens=None,
        output_tokens=None,
        tool_calls=None,
        exit_code=-1,
        error=redact_secrets(str(exc)),
        status="error",
        cache_read_tokens=None,
        cache_creation_tokens=None,
        num_turns=None,
        failure_kind="rate_limit" if looks_like_rate_limit(exc) else "harness_error",
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
        if looks_like_rate_limit(result.error or execution.stderr):
            result.failure_kind = "rate_limit"
        else:
            result.failure_kind = result.failure_kind or "harness_error"
        result.error = result.error or execution.stderr.strip() or f"exit code {result.exit_code}"
    elif result.error and looks_like_rate_limit(result.error):
        result.failure_kind = "rate_limit"
    if result.error:
        result.error = redact_secrets(result.error)
    if result.transcript:
        result.transcript = redact_secrets(result.transcript)
    if result.response:
        result.response = redact_secrets(result.response)
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
            claude_bin or os.environ.get("CLAUDE_BIN") or cached_which("claude") or "claude"
        )
        self.codex_bin = (
            codex_bin or os.environ.get("CODEX_BIN") or cached_which("codex") or "codex"
        )
        self.opencode_bin = (
            opencode_bin or os.environ.get("OPENCODE_BIN") or cached_which("opencode") or "opencode"
        )
        gemini_agy = Path.home() / ".gemini" / "bin" / "agy"
        self.antigravity_bin = (
            antigravity_bin
            or os.environ.get("AGY_BIN")
            or os.environ.get("ANTIGRAVITY_BIN")
            or cached_which("agy")
            or cached_which("antigravity")
            or (str(gemini_agy) if gemini_agy.exists() else None)
            or "agy"
        )
        self._active: set[subprocess.Popen] = set()
        self._active_lock = threading.Lock()
        self._cancelled = False
        # Cache of `<cli> <subcommand> --help` flags, so capability detection
        # happens once per process instead of per session.
        self._cli_flags: dict[tuple[str, ...], set[str]] = {}

    def cli_flags(self, bin_path: str, *subcommand: str) -> set[str]:
        """Long flags a CLI subcommand advertises, or an empty set if unknown.

        Empty means "could not probe" — callers must then avoid optional flags
        rather than guess, because these CLIs reject unknown flags outright.
        """
        key = (bin_path, *subcommand)
        cached = self._cli_flags.get(key)
        if cached is not None:
            return cached
        flags: set[str] = set()
        try:
            proc = subprocess.run(
                [bin_path, *subcommand, "--help"],
                capture_output=True,
                text=True,
                timeout=20,
                stdin=subprocess.DEVNULL,
            )
            text = (proc.stdout or "") + "\n" + (proc.stderr or "")
            flags = set(re.findall(r"--[a-z][a-z0-9-]+", text))
        except (OSError, subprocess.SubprocessError):
            flags = set()
        self._cli_flags[key] = flags
        return flags

    def opencode_run_flags(self, bin_path: str) -> set[str]:
        return self.cli_flags(bin_path, "run")

    def capabilities(self, cwd: Path, config: ExperimentConfig, scratch: Path) -> dict:
        from skilldiff.capabilities import probe_capabilities

        return probe_capabilities(self, cwd, config, scratch)

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
        read_paths: Optional[list[str]] = None,
        protected_paths: Optional[list[str]] = None,
        rpc_requests: Optional[list[dict[str, Any]]] = None,
        conversation: Optional[Conversation] = None,
    ) -> ExecResult:
        """Run a CLI with a timeout and clean up its owned children.

        Discovery and conversations use deadline-bounded pipes; other output goes
        to temporary files. Owned descendants cannot keep a session open indefinitely.
        """
        if self._cancelled:
            raise RuntimeError("Cancelled before the agent started")
        if isolation not in {"local", "docker", "podman", "macos"}:
            raise ValueError(f"Unsupported isolation mode: {isolation}")

        started = time.monotonic()
        deadline = started + timeout if timeout is not None else None

        exec_cmd = list(cmd)
        env = {k: v for k, v in env.items() if k not in PARENT_SESSION_VARS}
        # Keep PWD consistent with the working directory we actually launch in.
        # Harnesses that resolve their project directory from PWD (Bun/OpenCode,
        # for example) otherwise operate on the caller's directory.
        env["PWD"] = str(cwd.resolve())
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if temp_dir is not None:
            for key in ("TMPDIR", "TMP", "TEMP"):
                env[key] = str(temp_dir.resolve())
        container_name = None
        runtime = None
        if sys.platform == "darwin" and isolation == "local" and cached_which("caffeinate"):
            exec_cmd = ["caffeinate", "-i", *exec_cmd]
        elif isolation == "macos":
            from skilldiff.isolation import macos_command

            exec_cmd = macos_command(exec_cmd, cwd, temp_dir, read_paths or [],
                                     protected_paths or [])
        elif isolation in {"docker", "podman"}:
            runtime = cached_which(isolation)
            if not runtime:
                raise ValueError(f"Container runtime '{isolation}' not found on PATH")
            try:
                img = (container_image if container_image and container_image.startswith("sha256:")
                       else resolve_container_image(isolation, container_image,
                            min(20, timeout) if timeout is not None else 20))
            except subprocess.TimeoutExpired:
                return ExecResult("", "Container image inspection timed out", -1,
                                  round(time.monotonic() - started, 2), True)
            gemini_home = None
            if temp_dir is not None:
                gemini_home = stage_container_auth(temp_dir, env)
            container_name = "skilldiff-" + uuid.uuid4().hex
            mounts = list(readonly_mounts or [])
            mappings = [(cwd, "/workspace"), *mounts]
            if temp_dir is not None:
                mappings.append((temp_dir, "/session-tmp"))

            def translate(value: str) -> str:
                for source, target in mappings:
                    resolved = str(source.resolve())
                    candidates = [resolved, str(source)]
                    if resolved.startswith("/private/"):
                        candidates.append(resolved[len("/private") :])
                    for candidate in dict.fromkeys(candidates):
                        if value == candidate or value.startswith(candidate + "/"):
                            return target + value[len(candidate) :]
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
            if rpc_requests or conversation:
                exec_cmd.append("-i")
            if temp_dir is not None:
                exec_cmd.extend(["-v", f"{temp_dir.resolve()}:/session-tmp"])
                if (temp_dir / "codex-home").is_dir():
                    exec_cmd.extend(["-v", f"{(temp_dir / 'codex-home').resolve()}:/root/.codex"])
                if gemini_home is not None and gemini_home.is_dir():
                    exec_cmd.extend(["-v", f"{gemini_home.resolve()}:/root/.gemini"])
            for source, target in mounts:
                exec_cmd.extend(["-v", f"{source.resolve()}:{target}:ro"])
            container_env = {
                key: env[key]
                for key in (*CONTAINER_AUTH_VARS, "PYTHONDONTWRITEBYTECODE", *(forward_env or []),
                            *(("TMPDIR", "TMP", "TEMP") if temp_dir is not None else ()))
                if key in env
            }
            # Pass values through the client's environment, keeping credentials out of argv.
            for key, value in container_env.items():
                if key in CONTAINER_PATH_VARS:
                    # These paths/flags are not secrets. Keep the runtime client's
                    # environment usable on the host, and set the container value explicitly.
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
                stdin=subprocess.PIPE if rpc_requests or conversation else subprocess.DEVNULL,
                stdout=subprocess.PIPE if rpc_requests or conversation else out,
                stderr=err,
                start_new_session=os.name == "posix",
            )
            with self._active_lock:
                self._active.add(proc)
            descendants = OwnedDescendants(proc.pid)
            timed_out = False
            cleanup_error = None
            protocol_error = None
            stop_reader = threading.Event()
            try:
                if rpc_requests or conversation:
                    messages: queue.Queue[bytes | None] = queue.Queue()

                    def read_messages() -> None:
                        assert proc.stdout is not None
                        try:
                            for line in iter(proc.stdout.readline, b""):
                                if not stop_reader.is_set():
                                    messages.put(line)
                        finally:
                            proc.stdout.close()
                            messages.put(None)

                    reader = threading.Thread(target=read_messages, daemon=True)
                    reader.start()
                    assert proc.stdin is not None
                    def send(request):
                        proc.stdin.write((json.dumps(request) + "\n").encode())
                        proc.stdin.flush()

                    if conversation:
                        for request in conversation.start():
                            send(request)
                        while not conversation.done:
                            wait = None if deadline is None else max(0, deadline - time.monotonic())
                            try:
                                line = messages.get(timeout=wait)
                            except queue.Empty as exc:
                                raise subprocess.TimeoutExpired(cmd, timeout) from exc
                            if line is None:
                                raise RuntimeError(
                                    "Conversation closed before completing all turns")
                            out.write(line)
                            event = json.loads(line)
                            if not isinstance(event, dict):
                                raise RuntimeError("Malformed conversation event")
                            from skilldiff.telemetry import validate_event

                            validate_event(event)
                            for request in conversation.accept(event):
                                send(request)
                        # app-server deliberately stays alive after an ephemeral thread.
                        # Other streaming CLIs finish when stdin closes.
                        if conversation.harness == "codex":
                            _kill_group(proc)
                            proc.wait(timeout=5)
                            exit_code = 0
                        else:
                            proc.stdin.close()
                            remaining = (None if deadline is None else
                                         max(0, deadline - time.monotonic()))
                            exit_code = proc.wait(timeout=remaining)
                        _kill_group(proc)
                        descendants.cleanup()
                        reader.join(timeout=1)
                        # Drain final counters already emitted before process exit.
                        while not messages.empty():
                            line = messages.get_nowait()
                            if line is not None:
                                out.write(line)
                    for request in rpc_requests or []:
                        send(request)
                        if "id" not in request:
                            continue
                        while True:
                            wait = None if deadline is None else max(0, deadline - time.monotonic())
                            try:
                                line = messages.get(timeout=wait)
                            except queue.Empty as exc:
                                raise subprocess.TimeoutExpired(cmd, timeout) from exc
                            if line is None:
                                raise RuntimeError("Codex discovery closed before replying")
                            out.write(line)
                            message = json.loads(line)
                            if message.get("id") == request["id"]:
                                if "error" in message:
                                    raise RuntimeError(f"Codex discovery error: {message['error']}")
                                break
                    if rpc_requests:
                        _kill_group(proc)
                        proc.wait(timeout=5)
                        descendants.cleanup()
                        reader.join(timeout=1)
                        exit_code = 0
                else:
                    exit_code = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
                exit_code = proc.wait()
            except (RuntimeError, ValueError, KeyError, TypeError, BrokenPipeError) as exc:
                if not rpc_requests and not conversation:
                    raise
                protocol_error = str(exc)
                _kill_group(proc)
                exit_code = -1
            finally:
                _kill_group(proc)
                descendants.cleanup()
                if rpc_requests or conversation:
                    stop_reader.set()
                    proc.wait(timeout=5)
                    if proc.stdin:
                        try:
                            proc.stdin.close()
                        except BrokenPipeError:
                            pass
                    reader.join(timeout=1)
                    while not messages.empty():
                        line = messages.get_nowait()
                        if line is not None:
                            out.write(line)
                    # The reader owns stdout. Closing its pipe from this thread
                    # waits for its read lock if an escaped child kept it open,
                    # silently extending the discovery deadline.
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
                stderr=redact_secrets(
                    err.read().decode("utf-8", errors="replace")
                    + ("\n" + protocol_error if protocol_error else "")
                    + ("\n" + cleanup_error if cleanup_error else "")
                ),
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
            "reported_total_tokens",
        )
        totals = dict.fromkeys(metrics, 0)
        known = set()
        unknown = set()
        session_id = "ses_" + uuid.uuid4().hex
        skill_invoked = False
        skill_available = None
        denials: Optional[list[str]] = None
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
                temp_dir=temp_dir, session_id=session_id,
            )
            last_res = res
            for metric in metrics:
                value = getattr(res, metric)
                if metric == "reported_total_tokens":
                    from dataclasses import asdict

                    from skilldiff.reporter import _total_tokens_opt

                    value = _total_tokens_opt(asdict(res))
                if value is not None:
                    known.add(metric)
                    totals[metric] += value
                elif res.status == "ok":
                    unknown.add(metric)
            if res.skill_invoked:
                skill_invoked = True
            if res.skill_available is not None:
                skill_available = res.skill_available
            if res.permission_denials is not None:
                denials = (denials or []) + res.permission_denials
            turn_header = (
                f"--- TURN {idx + 1} ---\nPROMPT: {turn_prompt}\nRESPONSE:\n{res.response}"
            )
            transcripts.append(f"{turn_header}\n\nTRANSCRIPT:\n{res.transcript}")

            if res.status != "ok" or res.exit_code != 0:
                break

        if last_res is None:
            return _failed_result("", ValueError("No turns executed"), 0.0)

        totals = {key: value if key in known and key not in unknown else None
                  for key, value in totals.items()}

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
            session_id=last_res.session_id,
            reported_total_tokens=totals["reported_total_tokens"],
            status=last_res.status,
            failure_kind=last_res.failure_kind,
            permission_denials=denials,
        )

    def preflight(self, cwd: Path, config: ExperimentConfig,
                  expected_skills: list[str], temp_dir: Optional[Path] = None,
                  runtime_probe: Optional[str] = None) -> dict[str, Any]:
        from skilldiff.preflight import probe_workspace

        if temp_dir is None:
            with tempfile.TemporaryDirectory(prefix="probe-", dir=cwd.resolve().parent) as tmp:
                return self.preflight(cwd, config, expected_skills, Path(tmp), runtime_probe)
        return probe_workspace(self, cwd, config, expected_skills, temp_dir, runtime_probe)

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
            checked = False
            expected = []
            if (isinstance(config, ExperimentConfig)
                    and not os.environ.get("SKILLDIFF_MOCK_RUNNER")):
                expected = sorted({read_skill_name(path.parent) or path.parent.name
                                   for root in (".agents/skills", ".claude/skills",
                                                ".opencode/skills")
                                   for path in (cwd / root).glob("*/SKILL.md")
                                   if path.parent.name in config.skill_names})
                self.preflight(cwd, config, expected, Path(tmp))
                checked = config.harness == "codex" and config.codex.verify_skills
            result = self._run_session(
                prompt, cwd, model, config, harness, skill_names, timeout,
                temp_dir=Path(tmp),
            )
            if checked:
                result.skill_available = bool(expected)
            if (isinstance(config, ExperimentConfig) and not expected and config.skill_names
                    and not os.environ.get("SKILLDIFF_MOCK_RUNNER")
                    and (result.skill_available or result.skill_invoked)):
                result.status = "error"
                result.failure_kind = "harness_error"
                result.exit_code = result.exit_code or 1
                result.error = "Control discovered or loaded the target skill"
            return result

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
        session_id: Optional[str] = None,
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

        if os.environ.get("SKILLDIFF_MOCK_RUNNER"):
            if isinstance(prompt, list):
                res = self._run_multi_turn(prompt, cwd, model, config, harness, skill_names,
                                           timeout, temp_dir)
            else:
                res = self._run_mock(prompt, cwd, model)
            return self._apply_budget_limits(res, config)

        isolation = getattr(config, "isolation", "local")
        if temp_dir is None and isolation in EXTERNAL_ISOLATION:
            with tempfile.TemporaryDirectory(prefix="tmp-", dir=cwd.resolve().parent) as tmp:
                return self._run_session(
                    prompt, cwd, model, config, harness, skill_names, timeout,
                    temp_dir=Path(tmp), session_id=session_id,
                )
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
        extra_env = dict(getattr(config, "env", None) or {})
        if active_harness == "opencode" and isinstance(prompt, list):
            return self._apply_budget_limits(
                self._run_multi_turn(
                    prompt, cwd, model, config, active_harness, names, timeout, temp_dir
                ),
                config,
            )
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
                protected_paths=(protected_read_paths(config)
                                 if isinstance(config, ExperimentConfig) else []),
                skill_names=names,
                extra_env=extra_env,
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
                skill_names=names, session_id=session_id,
                extra_env=extra_env,
            )
        elif active_harness in {"antigravity", "agy"}:
            result = self._run_antigravity(
                prompt,
                cwd,
                model,
                antigravity_cfg,
                timeout,
                skill_names=names,
                isolation=isolation,
                container_image=container_image,
                temp_dir=temp_dir,
                extra_env=extra_env,
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
                protected_paths=(protected_read_paths(config)
                                 if isinstance(config, ExperimentConfig) else []),
                extra_env=extra_env,
            )

        return self._apply_budget_limits(result, config)

    @staticmethod
    def _apply_budget_limits(result: RunResult, config: Any) -> RunResult:
        max_tool_budget = getattr(config, "max_tool_budget", None)
        if (
            max_tool_budget is not None
            and result.tool_calls is not None
            and result.tool_calls > max_tool_budget
        ):
            result.status = "error"
            result.exit_code = result.exit_code or 1
            result.error = f"Tool budget exceeded ({result.tool_calls} > {max_tool_budget})"
        max_turns = getattr(config, "max_turns", None)
        if (
            max_turns is not None
            and result.num_turns is not None
            and result.num_turns > max_turns
        ):
            result.status = "error"
            result.exit_code = result.exit_code or 1
            result.error = f"Turn budget exceeded ({result.num_turns} > {max_turns})"
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

    def probe_claude_sandbox_canary(
        self, model: str, claude_cfg: ClaudeConfig, timeout: float = 90
    ) -> tuple[bool, list[str]]:
        import dataclasses

        probe_cfg = dataclasses.replace(
            claude_cfg,
            max_turns=2,
            max_budget_usd=0.10,
            effort=None,
        )
        prompt = (
            "Run each of these 4 commands using the Bash tool:\n"
            "1. cat << 'EOF'\nhello\nEOF\n"
            "2. python3 -c \"print('canary')\"\n"
            "3. VAR='test'; echo \"$VAR\"\n"
            "4. echo \"quoted string test\""
        )
        with tempfile.TemporaryDirectory(prefix="skilldiff-canary-probe-") as tmp:
            result = self._run_claude(
                prompt, Path(tmp), model, probe_cfg, timeout=timeout
            )
        denials = result.permission_denials or []
        return result.status == "ok", denials

    def claude_command(
        self,
        prompt: str,
        model: str,
        claude_cfg: ClaudeConfig,
        isolation: str = "local",
        protected_paths: Optional[list[str]] = None,
    ) -> list[str]:
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
        settings = claude_sandbox_settings(claude_cfg, isolation, protected_paths or [])
        if settings:
            cmd.extend(["--settings", json.dumps(settings)])
        permission_mode = claude_cfg.permission_mode
        if (
            claude_cfg.sandbox
            and isolation in EXTERNAL_ISOLATION
            and permission_mode in PROMPTING_MODES
        ):
            # The outer sandbox confines the session and Claude's own sandbox cannot
            # nest inside it, so commands would otherwise be denied rather than sandboxed.
            permission_mode = "bypassPermissions"
        if permission_mode:
            cmd.extend(["--permission-mode", permission_mode])
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
        protected_paths: Optional[list[str]] = None,
        extra_env: Optional[dict[str, str]] = None,
    ) -> RunResult:
        prompts = prompt if isinstance(prompt, list) else [prompt]
        cmd = self.claude_command(prompts[0], model, claude_cfg, isolation, protected_paths)
        conversation = None
        if isinstance(prompt, list):
            del cmd[cmd.index("-p") + 1]
            cmd.extend(["--input-format", "stream-json"])
            conversation = Conversation("claude", prompts)
        env = clean_parent_env()
        if claude_cfg.auth == "subscription":
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        if extra_env:
            env.update(extra_env)

        start_time = time.perf_counter()
        try:
            execution = self._exec(
                cmd, cwd, env, timeout, isolation=isolation, container_image=container_image,
                temp_dir=temp_dir, read_paths=claude_cfg.read_paths,
                protected_paths=protected_paths, conversation=conversation,
                forward_env=list((extra_env or {}).keys()),
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
            prompt="\n---\n".join(prompts),
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
            permission_denials=parsed["permission_denials"],
            session_id=parsed["session_id"],
            reported_total_tokens=parsed["reported_total_tokens"],
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
        protected_paths: Optional[list[str]] = None,
        skill_names: Optional[list[str]] = None,
        extra_env: Optional[dict[str, str]] = None,
    ) -> RunResult:
        bin_path = codex_cfg.bin_path or self.codex_bin
        cmd = [bin_path, "exec", prompt, "-m", model, "--json"]
        cmd.extend(codex_sandbox_args(codex_cfg, isolation))
        cmd.append("--ephemeral")
        conversation = None
        if isinstance(prompt, list):
            params = {"model": model, "cwd": ("/workspace" if isolation in
                      {"docker", "podman"} else str(cwd.resolve())),
                      "ephemeral": True, "approvalPolicy": "never",
                      "sandbox": ("danger-full-access" if isolation in
                                  {"macos", "docker", "podman"} or
                                  codex_cfg.dangerously_bypass_approvals_and_sandbox
                                  else codex_cfg.sandbox),
                      "config": {"sandbox_workspace_write.network_access":
                                 codex_cfg.network_access}}
            cmd = [bin_path, "app-server", "--stdio"]
            conversation = Conversation("codex", prompt, params)

        if codex_cfg.extra_args:
            cmd.extend(codex_cfg.extra_args)

        env = codex_env(codex_cfg, isolation, temp_dir)
        if extra_env:
            env.update(extra_env)

        start_time = time.perf_counter()
        try:
            execution = self._exec(
                cmd, cwd, env, timeout, isolation=isolation, container_image=container_image,
                temp_dir=temp_dir, read_paths=codex_cfg.read_paths,
                protected_paths=protected_paths, conversation=conversation,
                forward_env=list((extra_env or {}).keys()),
            )
        except Exception as exc:
            return _failed_result(prompt, exc, round(time.perf_counter() - start_time, 2))

        from skilldiff.telemetry import parse_codex

        parsed = parse_codex(execution.stdout, skill_names or _codex_skill_names(cwd))
        result = RunResult(
            prompt="\n---\n".join(prompt) if isinstance(prompt, list) else prompt,
            transcript=f"STDOUT:\n{execution.stdout}\n\nSTDERR:\n{execution.stderr}",
            duration=execution.duration,
            exit_code=execution.exit_code or (1 if parsed.get("error") else 0),
            permission_denials=codex_permission_denials(execution.stderr),
            **parsed,
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
        session_id: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
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

        if opencode_cfg.variant:
            target_model = f"{target_model}#{opencode_cfg.variant}"
        multi_turn = session_id is not None
        session_id = session_id or "ses_" + uuid.uuid4().hex
        cmd = [bin_path, "run", "--format", "json", "--session", session_id]
        if opencode_cfg.dangerously_skip_permissions:
            cmd.append("--auto")
        if opencode_cfg.isolate or multi_turn:
            cmd.append("--standalone")
        cmd.extend(opencode_cfg.extra_args)
        cmd.extend(["-m", target_model, prompt])
        env = clean_parent_env()
        if opencode_cfg.isolate:
            if temp_dir is not None:
                isolated_config = temp_dir / "opencode_config"
                isolated_config.mkdir(parents=True, exist_ok=True)
                env["OPENCODE_CONFIG_DIR"] = str(isolated_config.resolve())
            env["OPENCODE_DISABLE_CLAUDE_CODE"] = "true"
            env["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] = "true"
            env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "true"
            env["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
        if extra_env:
            env.update(extra_env)

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
                forward_env=list((extra_env or {}).keys()),
            )
        except Exception as exc:
            return _failed_result(prompt, exc, round(time.perf_counter() - start_time, 2))

        from skilldiff.telemetry import parse_opencode

        parsed = parse_opencode(execution.stdout, skill_names or [])
        if parsed["session_id"] not in (None, session_id):
            parsed["error"] = "OpenCode returned an unexpected session ID"
        parsed["session_id"] = session_id
        if (
            parsed.get("error") == "OpenCode exited without a terminal step_finish (reason: stop)"
            and not execution.timed_out
            and execution.exit_code == 0
        ):
            from skilldiff.telemetry import parse_opencode_export

            export_cmd = [bin_path, "session", "export"]
            if opencode_cfg.isolate or multi_turn:
                export_cmd.append("--standalone")
            export_cmd.append(session_id)
            export_timeout = min(30.0, timeout) if timeout else 30.0
            try:
                export_exec = self._exec(
                    export_cmd,
                    cwd,
                    env,
                    export_timeout,
                    isolation=isolation,
                    container_image=container_image,
                    temp_dir=temp_dir,
                )
                if export_exec.exit_code == 0 and export_exec.stdout.strip():
                    export_parsed = parse_opencode_export(export_exec.stdout, skill_names or [])
                    if not export_parsed.get("error"):
                        parsed["error"] = None
                        if export_parsed.get("response"):
                            parsed["response"] = export_parsed["response"]
                        for key in (
                            "cost",
                            "input_tokens",
                            "output_tokens",
                            "cache_read_tokens",
                            "cache_creation_tokens",
                            "reported_total_tokens",
                            "num_turns",
                            "tool_calls",
                        ):
                            if export_parsed.get(key) is not None:
                                parsed[key] = export_parsed[key]
                        if export_parsed.get("skill_invoked") is not None:
                            invoked = export_parsed["skill_invoked"]
                            parsed["skill_invoked"] = parsed["skill_invoked"] or invoked
            except Exception:
                pass
        result = RunResult(
            prompt=prompt,
            transcript=f"STDOUT:\n{execution.stdout}\n\nSTDERR:\n{execution.stderr}",
            duration=execution.duration,
            exit_code=execution.exit_code or (1 if parsed.get("error") else 0),
            permission_denials=opencode_permission_denials(execution.stdout, execution.stderr),
            **parsed,
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
        skill_names: Optional[list[str]] = None,
        extra_env: Optional[dict[str, str]] = None,
    ) -> RunResult:
        bin_path = antigravity_cfg.bin_path or self.antigravity_bin
        prompts = prompt if isinstance(prompt, list) else [prompt]
        cmd = [bin_path, "--output-format", "stream-json", "--add-dir", str(cwd)]
        conversation = None
        if isinstance(prompt, list):
            cmd.extend(["--input-format", "stream-json"])
            conversation = Conversation("antigravity", prompts)
        else:
            cmd.extend(["-p", prompt])
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

        env = clean_parent_env()
        if extra_env:
            env.update(extra_env)

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
                conversation=conversation,
                forward_env=list((extra_env or {}).keys()),
            )
        except Exception as exc:
            return _failed_result(prompt, exc, round(time.perf_counter() - start_time, 2))

        from skilldiff.telemetry import events, parse_agy

        parsed = parse_agy(execution.stdout, skill_names or [])
        stream, _ = events(execution.stdout)
        data = next((e.get("result") for e in reversed(stream) if e.get("event") == "result"),
                    None)
        result = RunResult(
            prompt="\n---\n".join(prompts),
            transcript=f"STDOUT:\n{execution.stdout}\n\nSTDERR:\n{execution.stderr}",
            duration=execution.duration,
            exit_code=execution.exit_code or (1 if parsed.get("error") else 0),
            permission_denials=antigravity_permission_denials(data, execution.stderr),
            **parsed,
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


def _codex_skill_names(workspace: Path) -> list[str]:
    """Read installed workspace skill names for Codex event parsing."""
    names: list[str] = []
    for root in (workspace / ".agents" / "skills", workspace / ".codex" / "skills"):
        if not root.is_dir():
            continue
        for path in root.glob("*/SKILL.md"):
            name = path.parent.name
            if name not in names:
                names.append(name)
            frontmatter_name = read_skill_name(path.parent)
            if frontmatter_name and frontmatter_name not in names:
                names.append(frontmatter_name)
    return names


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
        "permission_denials": None,
        "session_id": None,
        "reported_total_tokens": None,
    }
    last_text: Optional[str] = None
    saw_result = False
    usage_snapshots = []
    turn_counts = []

    from skilldiff.telemetry import count, money
    from skilldiff.telemetry import events as read_events

    events, malformed = read_events(stdout)
    if malformed:
        parsed["is_error"] = True
        parsed["error"] = malformed

    for event in events:
        event_type = event.get("type")
        if event_type == "error":
            parsed["is_error"] = True
            parsed["error"] = str(event.get("error") or event.get("message") or "Claude error")
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
                    if not isinstance(tool_input, dict):
                        parsed["is_error"] = True
                        parsed["error"] = "Malformed Claude tool input"
                        continue
                    if block.get("name") == "Skill" and _skill_matches(
                        tool_input.get("skill") or tool_input.get("command"), skill_names
                    ):
                        parsed["skill_invoked"] = True
                    elif detect_skill_reference(json.dumps(tool_input), skill_names):
                        parsed["skill_invoked"] = True
        elif event_type == "result":
            saw_result = True
            parsed["session_id"] = event.get("session_id") or parsed["session_id"]
            result_text = event.get("result")
            parsed["response"] = result_text if isinstance(result_text, str) else last_text
            raw_cost = event.get("total_cost_usd", event.get("cost"))
            failed = bool(event.get("is_error") or
                          str(event.get("subtype", "")).startswith("error"))
            if raw_cost is not None or not failed:
                parsed["cost"] = money(raw_cost)
            usage = event.get("usage") or {}
            # Claude usage and num_turns are per result; total_cost_usd is
            # cumulative across streaming input. Summing cost bills earlier turns twice.
            if usage or not failed:
                usage_snapshots.append(usage)
                turn_counts.append(count(event.get("num_turns")))
            denials = event.get("permission_denials")
            if isinstance(denials, list):
                if parsed["permission_denials"] is None:
                    parsed["permission_denials"] = []
                for d in denials:
                    if isinstance(d, dict):
                        tool = str(d.get("tool_name") or "unknown")
                        reason = d.get("reason") or d.get("message")
                        if reason:
                            parsed["permission_denials"].append(f"{tool} ({reason})")
                        else:
                            parsed["permission_denials"].append(tool)
                    else:
                        parsed["permission_denials"].append(str(d))
            subtype = str(event.get("subtype") or "")
            if event.get("is_error") or subtype.startswith("error"):
                parsed["is_error"] = True
                parsed["error"] = (
                    result_text if isinstance(result_text, str) and result_text else subtype
                ) or "Claude reported an error"

    if not saw_result:
        parsed["response"] = last_text
        parsed["is_error"] = True
        parsed["error"] = "Claude exited without a result event"
    for target, source in (
        ("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
        ("cache_read_tokens", "cache_read_input_tokens"),
        ("cache_creation_tokens", "cache_creation_input_tokens"),
        ("reported_total_tokens", "total_tokens"),
    ):
        values = [count(usage.get(source)) for usage in usage_snapshots]
        parsed[target] = (sum(values) if values and all(v is not None for v in values) else None)
    parsed["num_turns"] = (sum(turn_counts) if turn_counts and
                           all(v is not None for v in turn_counts) else None)
    return parsed


# `codex exec` logs each blocked operation on stderr, e.g.
#   WARN codex_sandboxing::violation: recorded sandbox violation: resource=filesystem ...
#   ERROR codex_core::tools::router: error=exec_command failed: CreateProcess { message:
#     "Rejected(\"`rm -rf x` rejected: rm -f style commands are not permitted ...\")" }
_CODEX_SANDBOX_VIOLATION = re.compile(r"recorded sandbox violation: resource=(\w+)")
_CODEX_POLICY_REJECTION = re.compile(r"exec_command failed: .*Rejected\(.*` rejected: ")


def codex_permission_denials(stderr: str) -> list[str]:
    """Shell commands Codex's sandbox or exec policy blocked, one label per event."""
    denials = []
    for line in stderr.splitlines():
        violation = _CODEX_SANDBOX_VIOLATION.search(line)
        if violation:
            denials.append(f"shell (sandbox: {violation.group(1)})")
        elif _CODEX_POLICY_REJECTION.search(line):
            denials.append("shell (exec policy)")
    return denials


# Without --auto, `opencode run` prints "permission requested: <action> (<resources>);
# auto-rejecting" on stderr and fails the tool with one of these messages
# (v2 headless notice, v1 rejection, and a configured deny rule).
_OPENCODE_AUTO_REJECT = re.compile(r"permission requested: (\S+) \(")
_OPENCODE_REJECTION_ERRORS = (
    "cannot ask the user for permission",
    "rejected permission",
    "specified a rule which prevents",
)


def opencode_permission_denials(stdout: str, stderr: str) -> list[str]:
    """Tools OpenCode refused, from failed ``tool_use`` events or the stderr notice."""
    from_stream = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        part = item.get("part") if isinstance(item, dict) else None
        state = part.get("state") if isinstance(part, dict) else None
        if not isinstance(state, dict) or state.get("status") != "error":
            continue
        error = state.get("error")
        message = str(error.get("message") if isinstance(error, dict) else error).lower()
        if any(marker in message for marker in _OPENCODE_REJECTION_ERRORS):
            from_stream.append(str(part.get("tool") or "unknown"))
    # v1 interrupts the session after a rejection without emitting the failed tool,
    # so the stderr notice can be the only record.
    from_notice = _OPENCODE_AUTO_REJECT.findall(stderr)
    return from_stream if len(from_stream) >= len(from_notice) else from_notice


# `agy -p` soft-denies tools that need approval, lists them as `denied_actions` in
# JSON output, and prints one of these notices on stderr.
_AGY_TOOLS_NOTICE = re.compile(r"the (.+?) tool\(s\) required approval that headless mode")
_AGY_RULE_NOTICE = re.compile(r"(\S+) required the .+? that headless mode cannot prompt for")


def antigravity_permission_denials(data: Any, stderr: str) -> list[str]:
    """Tools Antigravity soft-denied in print mode."""
    actions = data.get("denied_actions") if isinstance(data, dict) else None
    if isinstance(actions, list) and actions:
        return [
            str(next((a[k] for k in ("tool_name", "tool", "name", "action") if a.get(k)),
                     "unknown")) if isinstance(a, dict) else str(a)
            for a in actions
        ]
    denials = []
    for line in stderr.splitlines():
        tools = _AGY_TOOLS_NOTICE.search(line)
        if tools:
            names = re.split(r",\s*|\s+and\s+", tools.group(1))
            denials.extend(n.strip("`'\"") for n in names if n.strip("`'\""))
            continue
        rule = _AGY_RULE_NOTICE.search(line)
        if rule:
            denials.append(rule.group(1).strip("`'\"") or "unknown")
    return denials

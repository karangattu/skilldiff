"""No-model probes of the fresh workspace and the configured execution boundary."""

import json
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Any

from skilldiff.config import CodexConfig, ExperimentConfig


def protected_read_paths(config: ExperimentConfig) -> list[str]:
    paths = [str(path) for path in config.skill_dirs]
    if os.environ.get("CODEX_HOME"):
        paths.append(os.environ["CODEX_HOME"])
    if config.config_path:
        paths.extend(
            str(config.config_path.parent / name) for name in ("graders", "solutions", "runs")
        )
    paths.extend(
        str(Path.home() / name)
        for name in (".agents/skills", ".codex/skills", ".codex/plugins", ".codex/memories",
                     ".claude/skills", ".claude/plugins")
    )
    for root in config.codex.read_paths:
        paths.extend(str(Path(root) / name) for name in (".agents", ".codex"))
    for root in config.claude.read_paths:
        paths.extend(str(Path(root) / name) for name in (".agents", ".claude"))
    return paths


def codex_env(config: CodexConfig, isolation: str, scratch: Path | None) -> dict[str, str]:
    env = os.environ.copy()
    if config.auth in {"subscription", "stored"}:
        env.pop("OPENAI_API_KEY", None)
    if isolation == "macos" and scratch:
        home = scratch / "codex-home"
        home.mkdir(exist_ok=True)
        auth = Path(env.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
        if auth.is_file() and not (home / "auth.json").exists():
            shutil.copyfile(auth, home / "auth.json")
            (home / "auth.json").chmod(0o600)
        env["CODEX_HOME"] = str(home)
    return env


def probe_workspace(
    runner: Any,
    cwd: Path,
    config: ExperimentConfig,
    expected_skills: list[str],
    scratch: Path,
    runtime_probe: str | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {"expected_skills": expected_skills}
    read_paths = (
        config.claude.read_paths if config.harness == "claude" else config.codex.read_paths
    )
    kwargs = dict(
        isolation=config.isolation,
        container_image=config.container_image,
        temp_dir=scratch,
        read_paths=read_paths,
        protected_paths=protected_read_paths(config),
    )
    if config.isolation == "macos":
        with tempfile.TemporaryDirectory(prefix="read-boundary-", dir=cwd.parent) as outside:
            sentinel = Path(outside) / "secret"
            sentinel.write_text("Must not be readable by this arm", encoding="utf-8")
            command = (
                f"if /bin/cat {shlex.quote(str(sentinel))} >/dev/null 2>&1; "
                "then echo 'Sibling content was readable'; exit 1; fi"
            )
            result = runner._exec(
                ["/bin/sh", "-c", command],
                cwd,
                codex_env(config.codex, config.isolation, scratch)
                if config.harness == "codex"
                else os.environ.copy(),
                10,
                **kwargs,
            )
            if result.exit_code:
                raise RuntimeError(
                    f"Read-boundary preflight failed: {result.stdout or result.stderr}"
                )
            report["read_boundary"] = "sibling content denied"
    if runtime_probe:
        result = runner._exec(
            ["/bin/sh", "-c", runtime_probe],
            cwd,
            codex_env(config.codex, config.isolation, scratch)
            if config.harness == "codex"
            else os.environ.copy(),
            30,
            **kwargs,
        )
        report["runtime_probe"] = {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        if result.exit_code:
            raise RuntimeError(f"Runtime preflight failed: {result.stderr or result.stdout}")
    if config.harness != "codex" or not config.codex.verify_skills:
        return report
    binary = config.codex.bin_path or runner.codex_bin
    if config.isolation in {"docker", "podman"} and not config.codex.bin_path:
        binary = Path(binary).name
    cmd = [binary, "app-server", "--stdio"]
    args = iter(config.codex.extra_args)
    for arg in args:
        if arg in {"-c", "--config", "--enable", "--disable"}:
            cmd.extend([arg, next(args)])
    result = runner._exec(
        cmd,
        cwd,
        codex_env(config.codex, config.isolation, scratch),
        30,
        **kwargs,
        rpc_requests=[
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "skilldiff", "version": "1"}},
            },
            {"method": "initialized"},
            {
                "id": 2,
                "method": "skills/list",
                "params": {
                    "cwds": [
                        "/workspace"
                        if config.isolation in {"docker", "podman"}
                        else str(cwd.resolve())
                    ],
                    "forceReload": True,
                },
            },
        ],
    )
    if result.exit_code or result.timed_out:
        raise RuntimeError(f"Codex discovery preflight failed: {result.stderr or 'timeout'}")
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    response = next(message["result"] for message in replies if message.get("id") == 2)
    skills = [skill for entry in response.get("data", []) for skill in entry.get("skills", [])]
    available = [skill for skill in skills if skill.get("enabled", True)]
    names = {skill.get("name") for skill in available}
    prefix = "/workspace/" if config.isolation in {"docker", "podman"} else str(cwd.resolve()) + "/"
    missing = sorted(
        name
        for name in expected_skills
        if not any(
            skill.get("name") == name and str(skill.get("path", "")).startswith(prefix)
            for skill in available
        )
    )
    unexpected = sorted((set(config.skill_names) & names) - set(expected_skills))
    report["skills"] = skills
    report["errors"] = [
        error for entry in response.get("data", []) for error in entry.get("errors", [])
    ]
    if missing or unexpected:
        raise RuntimeError(
            f"Codex discovery mismatch: unavailable {missing}; "
            f"unexpected target skills {unexpected}; errors {report['errors']}"
        )
    return report

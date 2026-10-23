"""Credential-free capability checks, in the same boundary as paid execution."""

import os
import re
import shutil
import threading
from copy import deepcopy
from pathlib import Path
from typing import cast as _cast

from skilldiff.preflight import codex_env, protected_read_paths

_CAPABILITIES_CACHE: dict[tuple[object, ...], dict[str, object]] = {}
_CAPABILITIES_LOCK = threading.Lock()
_KEY_LOCKS: dict[tuple[object, ...], threading.Lock] = {}


def clear_capabilities_cache() -> None:
    with _CAPABILITIES_LOCK:
        _CAPABILITIES_CACHE.clear()
        _KEY_LOCKS.clear()


def probe_capabilities(runner, cwd, config, scratch):
    harness = config.harness
    native = getattr(config, harness)
    binary = runner.binary_for(harness, config)
    if config.isolation in {"docker", "podman"} and not native.bin_path:
        binary = Path(binary).name
    binary_stat = None
    try:
        bin_path = Path(binary)
        if bin_path.is_file():
            st = bin_path.stat()
            binary_stat = (st.st_mtime_ns, st.st_size)
    except OSError:
        pass
    runner_cls = type(runner)
    exec_impl = runner.__dict__.get("_exec", getattr(runner_cls, "_exec", None))
    runtime_bin = (
        shutil.which(config.isolation) if config.isolation in {"docker", "podman"} else None
    )
    cache_key = (
        runner_cls,
        exec_impl,
        binary,
        binary_stat,
        harness,
        config.isolation,
        runtime_bin,
        config.container_image,
        config.opencode.dangerously_skip_permissions if harness == "opencode" else None,
        tuple(getattr(native, "read_paths", ()) or ()),
    )
    with _CAPABILITIES_LOCK:
        cached = _CAPABILITIES_CACHE.get(cache_key)
        if cached is not None:
            return deepcopy(cached)
        key_lock = _KEY_LOCKS.setdefault(cache_key, threading.Lock())

    with key_lock:
        with _CAPABILITIES_LOCK:
            cached = _CAPABILITIES_CACHE.get(cache_key)
            if cached is not None:
                return deepcopy(cached)

        env = (codex_env(config.codex, config.isolation, scratch) if harness == "codex"
               else os.environ.copy())
        kwargs = dict(isolation=config.isolation, container_image=config.container_image,
                      temp_dir=scratch, read_paths=getattr(native, "read_paths", []),
                      protected_paths=protected_read_paths(config))
        def execute(args):
            result = runner._exec([binary, *args], cwd, env, 30, **kwargs)
            if result.exit_code or result.timed_out:
                raise RuntimeError(f"{harness} capability probe failed: "
                                   f"{result.stderr.strip() or result.stdout.strip()}")
            return ((result.stdout + "\n" + result.stderr).strip() if args[-1] == "--help"
                    else result.stdout.strip())
        version = execute(["--version"])
        if not version or not re.search(r"\d+\.\d+", version):
            raise RuntimeError(f"{harness} capability probe returned no CLI version")
        if harness == "opencode" and not re.search(r"(?:^|\s)v?2\.\d+", version):
            raise RuntimeError("OpenCode capability checks require V2")
        required = _cast(
            dict[tuple[str, ...], set[str]],
            {
                "claude": {(): {"--input-format", "--output-format", "--verbose",
                                 "--no-session-persistence", "--setting-sources"}},
                "codex": {("exec",): {"--json", "--ephemeral"},
                          ("app-server",): {"--stdio"}},
                "antigravity": {(): {"--input-format", "--output-format", "--add-dir"}},
                "opencode": {("run",): {"--format", "--standalone", "--session"}},
            }[harness],
        )
        if harness == "opencode" and config.opencode.dangerously_skip_permissions:
            required[("run",)].add("--auto")
        commands = {}
        cli_flags = getattr(runner, "_cli_flags", None)
        for command, needs in required.items():
            help_text = execute([*command, "--help"])
            flags = set(re.findall(r"--[a-z][a-z0-9-]+", help_text))
            if isinstance(cli_flags, dict) and config.isolation == "local":
                cli_flags[tuple([binary]) + tuple(command)] = set(flags)
            missing = sorted(needs - flags)
            if missing:
                raise RuntimeError(f"{harness} missing required capabilities: {', '.join(missing)}")
            if harness in {"claude", "antigravity"} and "stream-json" not in help_text:
                raise RuntimeError(f"{harness} capability probe did not advertise stream-json")
            if harness == "opencode" and "json" not in help_text:
                raise RuntimeError("OpenCode capability probe did not advertise JSON output")
            commands[" ".join(command) or "root"] = {"flags": sorted(flags)}
        result_dict = {
            "version": version,
            "binary": binary,
            "isolation": config.isolation,
            "container_image": config.container_image,
            "commands": commands,
        }
        with _CAPABILITIES_LOCK:
            _CAPABILITIES_CACHE[cache_key] = deepcopy(result_dict)
        return result_dict

"""Credential-free capability checks, in the same boundary as paid execution."""

import os
import re
from pathlib import Path

from skilldiff.preflight import codex_env, protected_read_paths


def probe_capabilities(runner, cwd, config, scratch):
    harness = config.harness
    native = getattr(config, harness)
    binary = runner.binary_for(harness, config)
    if config.isolation in {"docker", "podman"} and not native.bin_path:
        binary = Path(binary).name
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
    required = {
        "claude": {(): {"--input-format", "--output-format", "--verbose",
                         "--no-session-persistence", "--setting-sources"}},
        "codex": {("exec",): {"--json", "--ephemeral"},
                  ("app-server",): {"--stdio"}},
        "antigravity": {(): {"--input-format", "--output-format", "--add-dir"}},
        "opencode": {("run",): {"--format", "--standalone", "--session"}},
    }[harness]
    if harness == "opencode" and config.opencode.dangerously_skip_permissions:
        required[("run",)].add("--auto")
    commands = {}
    for command, needs in required.items():
        help_text = execute([*command, "--help"])
        flags = set(re.findall(r"--[a-z][a-z0-9-]+", help_text))
        missing = sorted(needs - flags)
        if missing:
            raise RuntimeError(f"{harness} missing required capabilities: {', '.join(missing)}")
        if harness in {"claude", "antigravity"} and "stream-json" not in help_text:
            raise RuntimeError(f"{harness} capability probe did not advertise stream-json")
        if harness == "opencode" and "json" not in help_text:
            raise RuntimeError("OpenCode capability probe did not advertise JSON output")
        commands[" ".join(command) or "root"] = {"flags": sorted(flags)}
    return {"version": version, "binary": binary, "isolation": config.isolation,
            "container_image": config.container_image, "commands": commands}

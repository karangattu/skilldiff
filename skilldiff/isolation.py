"""Native read boundaries for opt-in macOS Codex evaluations."""

import json
import os
import re
import shutil
import sys
from pathlib import Path


def macos_environment(
    env: dict[str, str], scratch: Path | None, forward_env: list[str]
) -> None:
    """Make host-only configuration unnecessary inside the native boundary."""
    env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    env.setdefault("GIT_CONFIG_NOSYSTEM", "1")
    if "GIT_CONFIG_COUNT" not in env:
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.excludesfile",
                   GIT_CONFIG_VALUE_0="/dev/null")
    git = Path("/Library/Developer/CommandLineTools/usr/bin/git")
    if git.is_file() and shutil.which("git", path=env.get("PATH", "")) == "/usr/bin/git":
        parts = env["PATH"].split(os.pathsep)
        parts.insert(parts.index("/usr/bin"), str(git.parent))
        env["PATH"] = os.pathsep.join(parts)
    if scratch is None:
        return
    env.setdefault("PYTEST_ADDOPTS", "-o cache_dir=" + json.dumps(
        str(scratch.resolve() / ".pytest_cache")))
    home = Path(env.get("CODEX_HOME", "/nonexistent"))
    if not home.is_dir() or not home.resolve().is_relative_to(scratch.resolve()):
        return
    # Codex can filter/reset its command environment. Set only explicit task
    # variables and managed runtime variables in its throwaway config. Values,
    # including explicitly configured secrets, must never appear in argv.
    keys = {*forward_env, "PATH", "TMPDIR", "TMP", "TEMP", "TMPPREFIX",
            "PYTHONDONTWRITEBYTECODE", "PYTEST_ADDOPTS",
            *(key for key in env if key.startswith("GIT_CONFIG_"))}
    config = home / "config.toml"
    fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write("[shell_environment_policy.set]\n")
        for key in sorted(keys & env.keys()):
            stream.write(f"{json.dumps(key)} = {json.dumps(env[key])}\n")


def macos_command(
    cmd: list[str],
    cwd: Path,
    scratch: Path | None,
    read_paths: list[str],
    protected_paths: list[str],
) -> list[str]:
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        raise ValueError("isolation: macos requires macOS and sandbox-exec")
    if scratch is None:
        raise ValueError("isolation: macos requires a private session temp directory")
    roots = [cwd.resolve(), scratch.resolve()]
    requested = [Path(p).expanduser().absolute() for p in read_paths]
    reads = [p.resolve() for p in requested]
    for root in reads:
        if not root.exists():
            raise ValueError(f"read_paths root does not exist: {root}")
        if root in cwd.resolve().parents or root == Path.home() or root in Path.home().parents:
            raise ValueError(f"read_paths root is too broad for isolation: {root}")
    binary = shutil.which(cmd[0]) or cmd[0]
    # Executables and OS libraries are readable; user content is opt-in.
    reads += [Path(p) for p in ("/System", "/Library", "/usr", "/bin", "/sbin", "/opt", "/dev")]
    executable = Path(binary).absolute()
    reads.extend([*requested, executable, executable.resolve()])
    # Shell-launched venv interpreters also traverse uv's directory aliases.
    # Only admit directory aliases whose targets are already declared readable.
    for launcher in [executable, *(p for root in requested for p in root.glob("bin/python*"))]:
        entrypoint = launcher == executable
        seen = set()
        while launcher.is_symlink() and launcher not in seen:
            seen.add(launcher)
            target = launcher.readlink()
            launcher = target if target.is_absolute() else launcher.parent / target
            if entrypoint or any(launcher.resolve().is_relative_to(root.resolve())
                                 for root in requested):
                reads.append(launcher)
            for parent in launcher.parents:
                if parent.is_symlink() and any(parent.resolve().is_relative_to(root.resolve())
                                              for root in requested):
                    reads.append(parent)
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        '(allow file-write* (subpath "/dev"))',
    ]
    for value in ("/Users", "/Volumes", "/private/var/folders", "/private/tmp", "/tmp"):
        rules.append(f"(deny file-read* file-write* (subpath {json.dumps(value)}))")
    for root in roots:
        rules.append(f"(allow file-read* file-write* (subpath {json.dumps(str(root))}))")
    if roots[0].parent in roots[1].parents:
        # Pytest collects temporary tests from their common session directory.
        # Permit listing its names, never reading sibling file contents.
        rules.append(f"(allow file-read-data (literal {json.dumps(str(roots[0].parent))}))")
        children = "^" + re.escape(str(roots[0].parent)) + r"/[^/]+$"
        rules.append(f"(allow file-read-metadata (regex {json.dumps(children)}))")
    # Claude Code keeps its per-user scratch directory at /tmp/claude-<uid> (not TMPDIR), and
    # creating it needs write access on the parent. Allow only that name pattern.
    # Stat (not list) the /tmp parents so path lookups through them resolve.
    rules.append('(allow file-read-metadata (literal "/tmp") (literal "/private/tmp") '
                 '(literal "/private"))')
    rules.append(
        '(allow file-read* file-write* (regex #"^/(private/)?tmp/claude-[0-9]+(/.*)?$"))'
    )
    claude_json = Path.home() / ".claude.json"
    if claude_json.is_file():
        rules.append(f"(allow file-read* (literal {json.dumps(str(claude_json))}))")
        for parent in claude_json.parents:
            rules.append(f"(allow file-read-metadata (literal {json.dumps(str(parent))}))")
    for root in reads:
        rules.append(f"(allow file-read* (subpath {json.dumps(str(root))}))")
    for root in [*roots, *reads]:
        for parent in root.parents:
            rules.append(f"(allow file-read-metadata (literal {json.dumps(str(parent))}))")
    # Runtime packages can bundle skills several levels below the declared root.
    for root in set([*requested, *(p.resolve() for p in requested)]):
        pattern = "^" + re.escape(str(root)) + r"/(.*/)?\.(agents|codex|claude)(/|$)"
        # Package metadata tools stat RECORD entries, including bundled skills.
        # Keep their contents and directory listings denied while allowing stat.
        rules.append(f'(deny file-read-data (regex {json.dumps(pattern)}))')
    # Even an explicit runtime root must not expose bundled skills or graders.
    for value in protected_paths:
        root = Path(value).expanduser().resolve()
        if any(root == own or own in root.parents for own in roots):
            raise ValueError(f"Protected read path is inside the workspace: {root}")
        bundled = root.name in {".agents", ".codex", ".claude"} and any(
            root.parent == allowed.resolve() for allowed in requested)
        operation = "file-read-data" if bundled else "file-read*"
        rules.append(f"(deny {operation} (subpath {json.dumps(str(root))}))")
    return ["sandbox-exec", "-p", "\n".join(rules), *cmd]

"""Native read boundaries for opt-in macOS Codex evaluations."""

import json
import re
import shutil
import sys
from pathlib import Path


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
    reads = [Path(p).expanduser().resolve() for p in read_paths]
    for root in reads:
        if not root.exists():
            raise ValueError(f"codex.read_paths does not exist: {root}")
        if root in cwd.resolve().parents or root == Path.home() or root in Path.home().parents:
            raise ValueError(f"codex.read_paths is too broad for isolation: {root}")
    binary = shutil.which(cmd[0]) or cmd[0]
    # Executables and OS libraries are readable; user content is opt-in.
    reads += [Path(p) for p in ("/System", "/Library", "/usr", "/bin", "/sbin", "/opt", "/dev")]
    reads.append(Path(binary).resolve())
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
    for root in reads:
        rules.append(f"(allow file-read* (subpath {json.dumps(str(root))}))")
    for root in [*roots, *reads]:
        for parent in root.parents:
            rules.append(f"(allow file-read-metadata (literal {json.dumps(str(parent))}))")
    # Runtime packages can bundle skills several levels below the declared root.
    for value in read_paths:
        root = Path(value).expanduser().resolve()
        pattern = "^" + re.escape(str(root)) + r"/(.*/)?\.(agents|codex)(/|$)"
        rules.append(f'(deny file-read* (regex {json.dumps(pattern)}))')
    # Even an explicit runtime root must not expose bundled skills or graders.
    for value in protected_paths:
        root = Path(value).expanduser().resolve()
        if any(root == own or own in root.parents for own in roots):
            raise ValueError(f"Protected read path is inside the workspace: {root}")
        rules.append(f"(deny file-read* (subpath {json.dumps(str(root))}))")
    return ["sandbox-exec", "-p", "\n".join(rules), *cmd]

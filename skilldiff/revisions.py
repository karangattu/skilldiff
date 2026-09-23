"""Resolve PR revisions once and export committed files without source Git history."""

import os
import subprocess
import tempfile
from pathlib import Path

from skilldiff.config import PRConfig


def _git(repo: Path, *args: str, env=None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=120,
        env=env,
    )
    if proc.returncode:
        raise ValueError(f"Could not resolve/export PR revision: {proc.stderr.strip()}")
    return proc.stdout.strip()


def resolve_comparison(pr: PRConfig) -> dict[str, str]:
    head = _git(pr.repo, "rev-parse", "--verify", "--end-of-options", pr.head + "^{commit}")
    base = _git(pr.repo, "rev-parse", "--verify", "--end-of-options", pr.base + "^{commit}")
    bases = _git(pr.repo, "merge-base", "--all", base, head).splitlines()
    if len(bases) != 1:
        raise ValueError("PR revisions must have exactly one merge base")
    if bases[0] == head:
        raise ValueError("PR head is already in base; choose the pre-merge base revision")
    return dict(
        type="pr",
        repo=str(pr.repo),
        base=pr.base,
        head=pr.head,
        control_commit=bases[0],
        treatment_commit=head,
    )


def export_revision(repo: Path, commit: str, root: Path) -> None:
    entries = _git(repo, "ls-tree", "-r", commit)
    if any(line.startswith("160000 ") for line in entries.splitlines()):
        raise ValueError("PR revision contains submodules; submodule snapshots are unsupported")
    # A separate index prevents modifying the source checkout or leaking its history.
    with tempfile.TemporaryDirectory(prefix="skilldiff-index-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(repo, "read-tree", commit, env=env)
        _git(repo, "checkout-index", "--all", f"--prefix={root}/", env=env)

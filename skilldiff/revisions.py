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


def _synthetic_merge_commit(repo: Path, base_sha: str, head_sha: str, merge_base: str) -> str:
    """Create a synthetic merge commit: merge head into base tip.

    Uses `git merge-tree` to compute the merged tree without touching the
    working tree, then `git commit-tree` to materialise it as a commit so it
    can be exported like any other revision. Falls back to a temporary
    worktree merge when merge-tree output cannot be parsed.
    """
    # Try modern merge-tree (writes tree OID to stdout).
    for args in (
        ("merge-tree", merge_base, base_sha, head_sha),
        ("merge-tree", base_sha, head_sha, merge_base),
    ):
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        if proc.returncode == 0:
            out = proc.stdout.strip().splitlines()
            # Modern output contains a line with the tree OID; legacy prints tree.
            tree = ""
            for line in out:
                line = line.strip()
                if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
                    tree = line
                    break
            if not tree:
                # `git merge-tree --write-tree` variant prints tree on first line.
                continue
            commit_proc = subprocess.run(
                ["git", "-C", str(repo), "commit-tree", tree, "-p", base_sha, "-p", head_sha,
                 "-m", f"skilldiff synthetic merge of {head_sha[:8]} into {base_sha[:8]}"],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=60,
            )
            if commit_proc.returncode == 0 and commit_proc.stdout.strip():
                return commit_proc.stdout.strip()
    # Fallback: temporary worktree merge.
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="skilldiff-merge-"))
    try:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "--detach", "--force", str(tmp), base_sha],
            capture_output=True, text=True, timeout=120, check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp), "merge", "--no-commit", "--no-ff", head_sha],
            capture_output=True, text=True, timeout=120,
        )
        tree = subprocess.run(
            ["git", "-C", str(tmp), "write-tree"],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "-C", str(repo), "commit-tree", tree, "-p", base_sha, "-p", head_sha,
             "-m", f"skilldiff synthetic merge of {head_sha[:8]} into {base_sha[:8]}"],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout.strip()
        return commit
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(tmp)],
            capture_output=True, timeout=60,
        )
        shutil.rmtree(tmp, ignore_errors=True)


def resolve_comparison(pr: PRConfig) -> dict[str, str]:
    """Resolve control/treatment commits for the configured PR workflow.

    Modes (pr.mode):
      agent: agents work on each revision (measures agent effectiveness).
      correctness: graders run on untouched revisions (measures PR correctness).
    Pairs (pr.pair):
      merge-base: merge-base(base, head) vs head (branch effect).
      base-merge: base tip vs synthetic merge of head into base (integration).
    """
    head = _git(pr.repo, "rev-parse", "--verify", "--end-of-options", pr.head + "^{commit}")
    base_tip = _git(pr.repo, "rev-parse", "--verify", "--end-of-options", pr.base + "^{commit}")
    bases = _git(pr.repo, "merge-base", "--all", base_tip, head).splitlines()
    if len(bases) != 1:
        raise ValueError("PR revisions must have exactly one merge base")
    merge_base = bases[0]
    if merge_base == head:
        raise ValueError("PR head is already in base; choose the pre-merge base revision")

    mode = getattr(pr, "mode", "agent") or "agent"
    pair = getattr(pr, "pair", "merge-base") or "merge-base"
    if mode not in {"agent", "correctness"}:
        raise ValueError("pr.mode must be 'agent' or 'correctness'")
    if pair not in {"merge-base", "base-merge"}:
        raise ValueError("pr.pair must be 'merge-base' or 'base-merge'")

    if pair == "base-merge":
        synthetic = _synthetic_merge_commit(pr.repo, base_tip, head, merge_base)
        return dict(
            type="pr",
            mode=mode,
            pair=pair,
            repo=str(pr.repo),
            base=pr.base,
            head=pr.head,
            control_commit=base_tip,
            treatment_commit=synthetic,
            merge_base=merge_base,
            base_tip=base_tip,
            head_commit=head,
        )
    return dict(
        type="pr",
        mode=mode,
        pair=pair,
        repo=str(pr.repo),
        base=pr.base,
        head=pr.head,
        control_commit=merge_base,
        treatment_commit=head,
        merge_base=merge_base,
        base_tip=base_tip,
        head_commit=head,
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

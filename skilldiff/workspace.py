import os
import shutil
import subprocess
from pathlib import Path

from skilldiff.config import find_skill_dirs
from skilldiff.revisions import export_revision

SKILL_ROOTS = (".claude", ".codex", ".opencode", ".agents", ".agent", ".gemini")

# Files that must never be copied from a fixture: Git history can resurrect
# removed skills, and escaping symlinks can expose the host.
IGNORED_FIXTURE_NAMES = {".git"}

# Harness-specific user-level locations that can leak skills, instructions,
# plugins, or memory into both arms when isolation is off.
HARNESS_USER_PATHS: dict[str, list[str]] = {
    "claude": [
        "~/.claude/skills",
        "~/.claude/plugins",
        "~/.claude/CLAUDE.md",
        "~/.claude/memory",
        "~/.claude/settings.json",
    ],
    "codex": [
        "~/.codex/skills",
        "~/.agents/skills",
        "~/.codex/AGENTS.md",
        "~/.codex/memory",
    ],
    "opencode": [
        "~/.config/opencode/skills",
        "~/.config/opencode/plugins",
        "~/.claude/skills",
        "~/.agents/skills",
        "~/.config/opencode/AGENTS.md",
        "~/.config/opencode/memory",
    ],
    "antigravity": [
        "~/.gemini/skills",
        "~/.agents/skills",
        "~/.gemini/GEMINI.md",
        "~/.gemini/memory",
    ],
}

# Workspace-level instruction/memory files that must not leak between arms.
HARNESS_WORKSPACE_FILES = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".claude/CLAUDE.md",
    ".agents/AGENTS.md",
)


def find_escaping_symlinks(root: Path) -> list[str]:
    """Return symlink paths inside root that resolve outside root."""
    escaping: list[str] = []
    root_resolved = root.resolve()
    for p in root.rglob("*"):
        try:
            if p.is_symlink():
                target = (p.parent / os.readlink(p)).resolve()
                try:
                    target.relative_to(root_resolved)
                except ValueError:
                    escaping.append(str(p.relative_to(root)))
        except OSError:
            continue
    return escaping


def copy_fixture_clean(src_dir: Path, dst_root: Path) -> None:
    """Copy a fixture without .git history and without escaping symlinks.

    Raises ValueError when a symlink would escape the workspace.
    """
    for item in os.listdir(src_dir):
        if item in IGNORED_FIXTURE_NAMES:
            continue
        src = src_dir / item
        dst = dst_root / item
        if src.is_symlink():
            # Resolve against the source tree: escaping links are rejected
            # before they ever enter the workspace.
            try:
                target = (src.parent / os.readlink(src)).resolve()
                src_resolved_root = src_dir.resolve()
                try:
                    target.relative_to(src_resolved_root)
                except ValueError:
                    raise ValueError(
                        f"Fixture symlink escapes workspace: {item} -> {os.readlink(src)}"
                    )
            except OSError as exc:
                raise ValueError(f"Cannot read fixture symlink {item}: {exc}") from exc
            # Copy the link itself (not the target) so in-workspace links keep working.
            if src.is_dir() and not os.path.isfile(src):
                # Symlink to dir: reproduce as symlink, then verify.
                dst.symlink_to(os.readlink(src))
            else:
                dst.symlink_to(os.readlink(src))
            continue
        if src.is_dir():
            shutil.copytree(
                src,
                dst,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git"),
            )
        else:
            shutil.copy2(src, dst)
    escaping = find_escaping_symlinks(dst_root)
    if escaping:
        raise ValueError(
            "Fixture contains symlinks escaping the workspace: " + ", ".join(escaping)
        )
    if (dst_root / ".git").exists():
        raise ValueError("Fixture copy must not contain .git history")


def find_harness_contamination(
    skill_names: list[str], harness: str, extra_bases: list[str] | None = None
) -> list[str]:
    """Host-level skills/instructions/plugins/memory that could leak into arms."""
    found: list[str] = []
    bases = list(HARNESS_USER_PATHS.get(harness, [])) + list(extra_bases or [])
    for base in bases:
        path = Path(base).expanduser()
        name = path.name.lower()
        if "skill" in name:
            for skill in skill_names:
                candidate = path / skill
                if (candidate / "SKILL.md").is_file():
                    found.append(f"skill installed at user level: {candidate}")
        elif path.suffix.lower() in {".md", ".json"} or "memory" in name or "plugin" in name:
            if path.exists():
                found.append(f"{harness} inheritance path exists: {path}")
        else:
            if path.is_dir() and any(path.iterdir()):
                found.append(f"{harness} inheritance path exists: {path}")
    return found


def check_workspace_isolation(
    root: Path, skill_names: list[str], harness: str, is_treatment: bool
) -> list[str]:
    """Harness-specific checks for inherited skills/instructions/plugins/memory.

    Returns a list of contamination descriptions. Control-arm hits mean the
    experiment has no clean baseline and must be treated as invalid.
    """
    issues: list[str] = []
    if (root / ".git").exists():
        # A .git dir here is the fresh workspace repo created by setup(), not
        # leaked fixture history; fixture .git is stripped by copy_fixture_clean.
        pass
    for rel in HARNESS_WORKSPACE_FILES:
        if (root / rel).exists():
            issues.append(f"workspace inherits {rel}")
    if not is_treatment:
        for skill_root in SKILL_ROOTS:
            for name in skill_names:
                if (root / skill_root / "skills" / name).exists():
                    issues.append(
                        f"control workspace still contains skill {name} under {skill_root}"
                    )
    escaping = find_escaping_symlinks(root)
    if escaping:
        issues.append("workspace has escaping symlinks: " + ", ".join(escaping))
    return issues

IGNORED_DIFF_PREFIXES = (
    ".claude/",
    ".codex/",
    ".opencode/",
    ".agents/",
    ".agent/",
    ".gemini/",
)

GIT_DIFF_EXCLUDES = [
    ":(exclude).claude",
    ":(exclude).codex",
    ":(exclude).opencode",
    ":(exclude).agents",
    ":(exclude).agent",
    ":(exclude).gemini",
]


class Workspace:
    def __init__(
        self,
        root: Path,
        is_treatment: bool,
        skill_dir: Path | None,
        fixture_repo: Path | None = None,
        harness: str = "claude",
        source_commit: str | None = None,
        # Extra skill roots to strip from non-treatment arms (used for A/B
        # baselines where two revisions must both be absent).
        strip_skill_dirs: list[Path] | None = None,
    ):
        self.root = root.resolve()
        self.is_treatment = is_treatment
        self.skill_dir = skill_dir.resolve() if skill_dir else None
        self.source_commit = source_commit
        self.fixture_repo = fixture_repo.resolve() if fixture_repo else None
        self.harness = harness
        self.skill_dirs = (find_skill_dirs(self.skill_dir) or [self.skill_dir]) if skill_dir else []
        self.strip_skill_dirs: list[Path] = []
        for extra in strip_skill_dirs or []:
            extra_resolved = extra.resolve() if extra else None
            if extra_resolved:
                self.strip_skill_dirs.extend(
                    find_skill_dirs(extra_resolved) or [extra_resolved]
                )
        # Copies of the skill that shipped with the fixture and were removed from control.
        self.removed_from_control: list[str] = []
        self.isolation_issues: list[str] = []

    def _get_skill_target_dirs(self, skill_name: str) -> list[Path]:
        if self.harness == "codex":
            return [
                self.root / ".codex" / "skills" / skill_name,
                self.root / ".agents" / "skills" / skill_name,
            ]
        if self.harness == "opencode":
            return [
                self.root / ".opencode" / "skills" / skill_name,
                self.root / ".agents" / "skills" / skill_name,
            ]
        if self.harness in {"antigravity", "agy"}:
            return [self.root / ".agents" / "skills" / skill_name]
        return [self.root / ".claude" / "skills" / skill_name]

    def setup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

        if self.source_commit:
            export_revision(self.fixture_repo, self.source_commit, self.root)
        elif self.fixture_repo and self.fixture_repo.exists():
            # Clean snapshot: no .git history, no escaping symlinks.
            copy_fixture_clean(self.fixture_repo, self.root)

        if not (self.root / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
            subprocess.run(["git", "config", "user.name", "skilldiff"], cwd=self.root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "skilldiff@local"],
                cwd=self.root,
                check=True,
            )

        if self.is_treatment:
            for skill_dir in self.skill_dirs:
                for target_dir in self._get_skill_target_dirs(skill_dir.name):
                    if target_dir.exists():
                        shutil.rmtree(target_dir)
                    # Skills are exported without .git so removed contents
                    # cannot be resurrected from history.
                    shutil.copytree(
                        skill_dir,
                        target_dir,
                        symlinks=True,
                        ignore=shutil.ignore_patterns(".git"),
                    )
            # In A/B mode both arms carry a skill; strip the *other* revision
            # if it shipped inside the fixture.
            for skill_dir in self.strip_skill_dirs:
                for root in SKILL_ROOTS:
                    existing = self.root / root / "skills" / skill_dir.name
                    # Don't delete the skill we just installed.
                    installed = {
                        t.relative_to(self.root)
                        for d in self.skill_dirs
                        for t in self._get_skill_target_dirs(d.name)
                    }
                    try:
                        rel = existing.relative_to(self.root)
                    except ValueError:
                        continue
                    if existing.exists() and rel not in installed:
                        shutil.rmtree(existing)
                        self.removed_from_control.append(str(rel))
        else:
            for skill_dir in [*self.skill_dirs, *self.strip_skill_dirs]:
                for root in SKILL_ROOTS:
                    existing = self.root / root / "skills" / skill_dir.name
                    if existing.exists():
                        shutil.rmtree(existing)
                        self.removed_from_control.append(str(existing.relative_to(self.root)))

        # Harness-specific isolation verification. Control contamination
        # invalidates the experiment (checked by the runner).
        from skilldiff.config import read_skill_name as _read_name

        names: list[str] = []
        for d in [*self.skill_dirs, *self.strip_skill_dirs]:
            for candidate in (d.name, _read_name(d) if d.exists() else None):
                if candidate and candidate not in names:
                    names.append(candidate)
        self.isolation_issues = check_workspace_isolation(
            self.root, names, self.harness, self.is_treatment
        )

        add_args = ["git", "add", "-A"] + (["--force"] if self.source_commit else [])
        subprocess.run(add_args, cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial", "--allow-empty"],
            cwd=self.root,
            check=True,
        )

    def get_diff(self) -> tuple[str, list[str]]:
        proc_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        changed_files: list[str] = []
        for line in proc_status.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                path = parts[1]
                if self.source_commit or not path.startswith(IGNORED_DIFF_PREFIXES):
                    changed_files.append(path)

        subprocess.run(["git", "add", "-N", "."], cwd=self.root, check=False)
        cmd_diff = ["git", "diff", "--"]
        if not self.source_commit:
            cmd_diff += GIT_DIFF_EXCLUDES
        proc_diff = subprocess.run(
            cmd_diff,
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        diff_text = proc_diff.stdout
        return diff_text, changed_files

    def cleanup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from skilldiff.config import find_skill_dirs, read_skill_name
from skilldiff.revisions import export_revision
from skilldiff.snapshots import copy_file_cow, supports_reflink

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
        "~/.gemini/antigravity-cli/skills",
        "~/.gemini/antigravity-cli/plugins",
        "~/.gemini/GEMINI.md",
        "~/.gemini/memory",
    ],
}


def user_skill_paths(skill_names: list[str], harness: str) -> list[Path]:
    """Advisory scan, including plugin skills; discovery is a separate runtime check."""
    found: list[Path] = []
    for raw in HARNESS_USER_PATHS.get(harness, []):
        base = Path(raw).expanduser()
        if base.name == "skills":
            candidates = [base / name for name in skill_names]
        elif base.name == "plugins" and base.is_dir():
            candidates = [p.parent for p in base.glob("**/skills/*/SKILL.md")
                          if p.parent.name in skill_names]
        else:
            continue
        found.extend(p for p in candidates if (p / "SKILL.md").is_file())
    return sorted(set(found))

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


def copy_fixture_clean(src_dir: Path, dst_root: Path, reflink: bool | None = None) -> None:
    use_reflink = supports_reflink() if reflink is None else reflink
    copy_fn = (
        (lambda s, d: copy_file_cow(s, d, reflink=True))
        if use_reflink
        else shutil.copy2
    )
    for item in os.listdir(src_dir):
        if item in IGNORED_FIXTURE_NAMES:
            continue
        src = src_dir / item
        dst = dst_root / item
        if src.is_symlink():
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
            if src.is_dir() and not os.path.isfile(src):
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
                copy_function=copy_fn,
            )
        else:
            copy_file_cow(src, dst, reflink=use_reflink)
    escaping = find_escaping_symlinks(dst_root)
    if escaping:
        raise ValueError("Fixture contains symlinks escaping the workspace: " + ", ".join(escaping))
    if (dst_root / ".git").exists():
        raise ValueError("Fixture copy must not contain .git history")


def find_harness_contamination(
    skill_names: list[str], harness: str, extra_bases: list[str] | None = None
) -> list[str]:
    """Host-level skills/instructions/plugins/memory that could leak into arms."""
    found: list[str] = []
    found.extend(f"skill installed at user level: {p}"
                 for p in user_skill_paths(skill_names, harness))
    bases = list(HARNESS_USER_PATHS.get(harness, [])) + list(extra_bases or [])
    for base in bases:
        path = Path(base).expanduser()
        name = path.name.lower()
        if "skill" in name:
            for skill in skill_names:
                candidate = path / skill
                if (candidate / "SKILL.md").is_file():
                    warning = f"skill installed at user level: {candidate}"
                    if warning not in found:
                        found.append(warning)
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


GENERATED_DIFF_EXCLUDES = [
    ":(exclude).venv",
    ":(exclude)**/.venv",
    ":(exclude)node_modules",
    ":(exclude)**/node_modules",
    ":(exclude).pytest_cache",
    ":(exclude)**/.pytest_cache",
    ":(exclude).uv-cache",
    ":(exclude)**/.uv-cache",
    ":(exclude)__pycache__",
    ":(exclude)**/__pycache__",
    ":(exclude).cache",
    ":(exclude)**/.cache",
]

GIT_DIFF_EXCLUDES = [
    ":(exclude).claude",
    ":(exclude).codex",
    ":(exclude).opencode",
    ":(exclude).agents",
    ":(exclude).agent",
    ":(exclude).gemini",
    *GENERATED_DIFF_EXCLUDES,
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
        pr_touched_skills: list[str] | None = None,
        reflink: bool | None = None,
        mounted_fixtures: list[Path] | None = None,
    ):
        self.root = root.resolve()
        self.reflink = supports_reflink() if reflink is None else reflink
        self.is_treatment = is_treatment
        self.skill_dir = skill_dir.resolve() if skill_dir else None
        self.source_commit = source_commit
        self.fixture_repo = fixture_repo.resolve() if fixture_repo else None
        self.mounted_fixtures = [Path(p) for p in (mounted_fixtures or [])]
        self.harness = harness
        self.skill_dirs = (
            (find_skill_dirs(self.skill_dir) or [self.skill_dir])
            if self.skill_dir
            else []
        )
        self.strip_skill_dirs: list[Path] = []
        self.pr_touched_skills = list(pr_touched_skills or [])
        for extra in strip_skill_dirs or []:
            extra_resolved = extra.resolve() if extra else None
            if extra_resolved:
                self.strip_skill_dirs.extend(find_skill_dirs(extra_resolved) or [extra_resolved])
        # Copies of the skill that shipped with the fixture and were removed from control.
        self.removed_from_control: list[str] = []
        self.isolation_issues: list[str] = []
        self.initial_commit: str | None = None
        self._initial_index: bytes | None = None
        self._template: Workspace | None = None

    def _copy_fn(self):
        return (
            (lambda s, d: copy_file_cow(s, d, reflink=self.reflink))
            if self.reflink
            else shutil.copy2
        )

    def _get_skill_target_dirs(self, skill_name: str) -> list[Path]:
        if self.harness == "codex":
            return [self.root / ".agents" / "skills" / skill_name]
        if self.harness == "opencode":
            return [self.root / ".opencode" / "skills" / skill_name]
        if self.harness in {"antigravity", "agy"}:
            return [self.root / ".agents" / "skills" / skill_name]
        return [self.root / ".claude" / "skills" / skill_name]

    def installed_skill_names(self) -> list[str]:
        """The arm's own skills that are actually on disk for the harness.

        A control arm is configured with a skill but the skill is stripped
        from the workspace, so it has none. Callers that need to know what
        the agent can see must ask the workspace, not the config.
        """
        expected: list[str] = []
        for skill_dir in self.skill_dirs:
            name = read_skill_name(skill_dir) or skill_dir.name
            if name and name not in expected:
                expected.append(name)
        installed: list[str] = []
        for root in (".agents/skills", ".claude/skills", ".opencode/skills"):
            for path in (self.root / root).glob("*/SKILL.md"):
                name = read_skill_name(path.parent) or path.parent.name
                if name in expected and name not in installed:
                    installed.append(name)
        return installed

    def clone_from(self, template: "Workspace") -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        shutil.copytree(
            template.root,
            self.root,
            symlinks=True,
            copy_function=self._copy_fn(),
        )
        self.is_treatment = template.is_treatment
        self.skill_dirs = [
            (self.root / p.relative_to(template.root))
            if p.is_relative_to(template.root)
            else p
            for p in template.skill_dirs
        ]
        self.removed_from_control = list(template.removed_from_control)
        self.isolation_issues = list(template.isolation_issues)
        self.initial_commit = template.initial_commit
        self._initial_index = template._initial_index
        self._template = template

    def setup(self) -> None:
        if self._template is not None and self._template.root.exists():
            self.clone_from(self._template)
            return
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

        if self.source_commit:
            assert self.fixture_repo is not None
            export_revision(self.fixture_repo, self.source_commit, self.root)
        elif self.fixture_repo and self.fixture_repo.exists():
            copy_fixture_clean(self.fixture_repo, self.root, reflink=self.reflink)

        for mounted in self.mounted_fixtures:
            if not mounted.exists():
                raise ValueError(f"Mounted fixture not found: {mounted}")
            copy_fixture_clean(mounted, self.root, reflink=self.reflink)

        if not (self.root / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
            git_config = self.root / ".git" / "config"
            with git_config.open("a", encoding="utf-8") as f:
                # Templates are copied immediately; no background writer may mutate .git.
                f.write("[user]\n\tname = skilldiff\n\temail = skilldiff@local\n"
                        "[maintenance]\n\tauto = false\n[gc]\n\tauto = 0\n")

        if self.pr_touched_skills:
            for rel in self.pr_touched_skills:
                source_dir = (self.root / rel).resolve() if rel else self.root.resolve()
                skill_md = source_dir / "SKILL.md"
                if skill_md.is_file():
                    from skilldiff.config import read_skill_name as _rsn

                    skill_name = _rsn(source_dir) or source_dir.name or "skill"
                    for target_dir in self._get_skill_target_dirs(skill_name):
                        if target_dir.resolve() != source_dir:
                            if target_dir.exists():
                                shutil.rmtree(target_dir)
                            target_dir.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copytree(
                                source_dir,
                                target_dir,
                                symlinks=True,
                                ignore=shutil.ignore_patterns(".git"),
                                copy_function=self._copy_fn(),
                            )
                        if target_dir not in self.skill_dirs:
                            self.skill_dirs.append(target_dir)
                    self.is_treatment = True
        elif self.is_treatment:
            for skill_dir in self.skill_dirs:
                for target_dir in self._get_skill_target_dirs(skill_dir.name):
                    if target_dir.exists():
                        shutil.rmtree(target_dir)
                    shutil.copytree(
                        skill_dir,
                        target_dir,
                        symlinks=True,
                        ignore=shutil.ignore_patterns(".git"),
                        copy_function=self._copy_fn(),
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
                        rel_path: Path = existing.relative_to(self.root)
                    except ValueError:
                        continue
                    if existing.exists() and rel_path not in installed:
                        shutil.rmtree(existing)
                        self.removed_from_control.append(str(rel_path))
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

        add_args = ["git", "add", "-A", "--force"]
        subprocess.run(add_args, cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial", "--allow-empty"],
            cwd=self.root,
            check=True,
        )
        head_sha = None
        head_file = self.root / ".git" / "HEAD"
        if head_file.is_file():
            head_val = head_file.read_text(encoding="utf-8").strip()
            if head_val.startswith("ref: "):
                ref_file = self.root / ".git" / head_val[5:]
                if ref_file.is_file():
                    head_sha = ref_file.read_text(encoding="utf-8").strip()
            elif len(head_val) == 40:
                head_sha = head_val
        self.initial_commit = head_sha or subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.root, text=True
        ).strip()
        index_file = self.root / ".git" / "index"
        self._initial_index = index_file.read_bytes() if index_file.is_file() else None

    def get_diff(self) -> tuple[str, list[str]]:
        """Compare final content to setup, independently of the agent's Git index/HEAD."""
        if not self.initial_commit:
            raise ValueError("Workspace has no immutable initial commit")
        # A separate index includes untracked files without changing the agent's index.
        with tempfile.TemporaryDirectory(prefix="skilldiff-index-") as tmp:
            index_path = Path(tmp) / "index"
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = str(index_path)
            if self._initial_index is not None:
                index_path.write_bytes(self._initial_index)
            else:
                subprocess.run(
                    ["git", "read-tree", self.initial_commit],
                    cwd=self.root,
                    env=env,
                    check=True,
                    capture_output=True,
                )
            subprocess.run(
                ["git", "add", "--all", "--force", "."],
                cwd=self.root,
                env=env,
                check=True,
                capture_output=True,
            )
            base = ["git", "diff", "--cached", "--no-renames", "--binary", self.initial_commit]
            paths = ["--", *(GENERATED_DIFF_EXCLUDES if self.source_commit else GIT_DIFF_EXCLUDES)]
            names = subprocess.check_output(
                base + ["--name-only", "-z"] + paths,
                cwd=self.root,
                env=env,
                stderr=subprocess.DEVNULL,
            )
            changed = [os.fsdecode(name) for name in names.split(b"\0") if name]
            if not changed:
                return "", []
            patch = subprocess.check_output(
                base + paths, cwd=self.root, env=env, text=True, stderr=subprocess.DEVNULL
            )
            return patch, changed

    def cleanup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)

    def __del__(self) -> None:
        self.cleanup()


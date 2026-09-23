import os
import shutil
import subprocess
from pathlib import Path

from skilldiff.config import find_skill_dirs
from skilldiff.revisions import export_revision

SKILL_ROOTS = (".claude", ".codex", ".opencode", ".agents", ".agent", ".gemini")

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
    ):
        self.root = root.resolve()
        self.is_treatment = is_treatment
        self.skill_dir = skill_dir.resolve() if skill_dir else None
        self.source_commit = source_commit
        self.fixture_repo = fixture_repo.resolve() if fixture_repo else None
        self.harness = harness
        self.skill_dirs = (find_skill_dirs(self.skill_dir) or [self.skill_dir]) if skill_dir else []
        # Copies of the skill that shipped with the fixture and were removed from control.
        self.removed_from_control: list[str] = []

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
            for item in os.listdir(self.fixture_repo):
                src = self.fixture_repo / item
                dst = self.root / item
                if src.is_dir():
                    shutil.copytree(src, dst, symlinks=True)
                else:
                    shutil.copy2(src, dst)

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
                    shutil.copytree(skill_dir, target_dir, symlinks=True)
        else:
            for skill_dir in self.skill_dirs:
                for root in SKILL_ROOTS:
                    existing = self.root / root / "skills" / skill_dir.name
                    if existing.exists():
                        shutil.rmtree(existing)
                        self.removed_from_control.append(str(existing.relative_to(self.root)))

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

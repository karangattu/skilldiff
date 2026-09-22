import os
import shutil
import subprocess
from pathlib import Path

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
        skill_dir: Path,
        fixture_repo: Path | None = None,
        harness: str = "claude",
    ):
        self.root = root.resolve()
        self.is_treatment = is_treatment
        self.skill_dir = skill_dir.resolve()
        self.fixture_repo = fixture_repo.resolve() if fixture_repo else None
        self.harness = harness

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

        if self.fixture_repo and self.fixture_repo.exists():
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
            skill_name = self.skill_dir.name
            target_dirs = self._get_skill_target_dirs(skill_name)
            for target_dir in target_dirs:
                target_dir.mkdir(parents=True, exist_ok=True)
                for item in os.listdir(self.skill_dir):
                    src = self.skill_dir / item
                    dst = target_dir / item
                    if src.is_dir():
                        shutil.copytree(src, dst, symlinks=True)
                    else:
                        shutil.copy2(src, dst)

        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
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
                if not path.startswith(IGNORED_DIFF_PREFIXES):
                    changed_files.append(path)

        subprocess.run(["git", "add", "-N", "."], cwd=self.root, check=False)
        cmd_diff = ["git", "diff", "--"] + GIT_DIFF_EXCLUDES
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

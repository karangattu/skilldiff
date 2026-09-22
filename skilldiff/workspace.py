import os
import shutil
import subprocess
from pathlib import Path


class Workspace:
    def __init__(
        self,
        root: Path,
        is_treatment: bool,
        skill_dir: Path,
        fixture_repo: Path | None = None,
    ):
        self.root = root.resolve()
        self.is_treatment = is_treatment
        self.skill_dir = skill_dir.resolve()
        self.fixture_repo = fixture_repo.resolve() if fixture_repo else None

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
            target_skill_dir = self.root / ".claude" / "skills" / skill_name
            target_skill_dir.mkdir(parents=True, exist_ok=True)
            for item in os.listdir(self.skill_dir):
                src = self.skill_dir / item
                dst = target_skill_dir / item
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
                if not path.startswith(".claude/"):
                    changed_files.append(path)

        subprocess.run(["git", "add", "-N", "."], cwd=self.root, check=False)
        proc_diff = subprocess.run(
            ["git", "diff", "--", ":(exclude).claude"],
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

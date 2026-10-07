"""Advisory detection of skill copies outside local agent workspaces."""

import hashlib
import os
import time
from pathlib import Path

from skilldiff.config import ExperimentConfig, read_skill_name

# Bound discovery rather than recursively reading the whole host by default.
SKIPPED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".cache"}
MAX_SKILL_BYTES = 1024 * 1024


def scan_skill_copies(
    root: Path,
    skills: list[Path],
    *,
    max_entries: int = 20000,
    max_seconds: float = 5,
    max_matches: int = 20,
) -> tuple[list[Path], str]:
    """Find copies by directory/frontmatter name or identical SKILL.md contents.

    Do not follow symlinks. The summary always describes coverage limits; absence
    of matches is never evidence that an agent cannot read a skill on the host.
    """
    names = {name for skill in skills for name in (skill.name, read_skill_name(skill)) if name}
    hashes = set()
    for skill in skills:
        try:
            hashes.add(hashlib.sha256((skill / "SKILL.md").read_bytes()).digest())
        except OSError:
            pass
    excluded = {skill.resolve() for skill in skills}
    matches: list[Path] = []
    pending = [root]
    entries = skipped = 0
    errors: list[str] = []
    reason = ""
    deadline = time.monotonic() + max_seconds
    while pending and not reason:
        directory = pending.pop()
        try:
            with os.scandir(directory) as children:
                for child in children:
                    if entries >= max_entries or time.monotonic() >= deadline:
                        reason = "entry/time limit reached"
                        break
                    entries += 1
                    path = Path(child.path)
                    try:
                        if child.is_symlink():
                            skipped += 1
                            continue
                        if child.is_dir(follow_symlinks=False):
                            if child.name in SKIPPED_DIRS:
                                skipped += 1
                            else:
                                pending.append(path)
                        elif child.name == "SKILL.md" and child.is_file(follow_symlinks=False):
                            if path.parent.resolve() in excluded:
                                continue
                            if child.stat(follow_symlinks=False).st_size > MAX_SKILL_BYTES:
                                skipped += 1
                                continue
                            contents = path.read_bytes()
                            if (path.parent.name in names
                                    or hashlib.sha256(contents).digest() in hashes
                                    or read_skill_name(path.parent) in names):
                                matches.append(path.parent.resolve())
                                if len(matches) >= max_matches:
                                    reason = "match limit reached"
                                    break
                    except (OSError, UnicodeError) as exc:
                        errors.append(f"{path}: {exc}")
        except FileNotFoundError:
            # A not-yet-created runs directory is expected during check.
            if directory != root:
                errors.append(f"{directory}: disappeared during scan")
        except OSError as exc:
            errors.append(f"{directory}: {exc}")
    summary = (
        f"Scanned {root.resolve()}: {entries} entries, {len(matches)} possible copy/copies, "
        f"{skipped} skipped, {len(errors)} unreadable. "
        "Symlinks are not followed; excluded directory names: "
        + ", ".join(sorted(SKIPPED_DIRS))
        + "; SKILL.md files over 1 MiB are skipped. "
        "Limits: 20000 entries, 5 seconds, 20 matches per scan by default. "
        "This scan cannot certify a clean host."
    )
    if reason:
        summary += f" Incomplete: {reason}."
    if errors:
        summary += " Unreadable paths: " + "; ".join(errors[:3])
    return sorted(set(matches)), summary


def host_exposure_warnings(
    config: ExperimentConfig, runs_dir: Path, *, scan_home: bool = False,
    snapshot_skills: list[Path] | None = None,
) -> list[str]:
    """Local-file exposure is distinct from automatic harness skill inheritance."""
    if config.isolation != "local" or not config.skill_dirs:
        return []
    skills = config.skill_dirs
    saved, saved_summary = scan_skill_copies(runs_dir, skills)
    # Active snapshots are known inputs, including on resume from another output
    # directory. Always report them even when bounded discovery stops early.
    saved = sorted(set(saved) | {path.resolve() for path in snapshot_skills or []})
    paths = "live skill sources: " + ", ".join(str(skill.resolve()) for skill in skills)
    if saved:
        paths += "; saved run copies: " + ", ".join(str(path) for path in saved)
    warnings = [
        "Potential host skill exposure — " + paths + ". "
        "Local agents may be able to read these paths even when user-level skill loading "
        "is disabled. These are exposure paths, not proof of contamination. "
        "Restrict filesystem reads with isolation: docker/podman or an enforced read "
        "sandbox; separate workspaces and TMPDIR do not block host reads."
    ]
    # Report limits even without matches; the source warning is always present.
    warnings.append(saved_summary)
    if scan_home:
        copies, summary = scan_skill_copies(Path.home(), skills)
        if copies:
            warnings.append(
                "Potential host skill exposure — matching copies under HOME: "
                + ", ".join(str(path) for path in copies)
                + ". Matching names or contents indicate potential exposure, "
                "not proof of contamination."
            )
        warnings.append(summary)
    return warnings

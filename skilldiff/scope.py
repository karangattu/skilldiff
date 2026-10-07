"""Path-scope helpers shared by the grader, reporter, and diagnosis."""

import fnmatch
import re

BLAST_RADIUS_PREFIX = "Blast radius violation"


def is_blast_violation(feedback: object) -> bool:
    """True when a grade's feedback is a blast-radius violation, not a grader failure."""
    return isinstance(feedback, str) and feedback.startswith(BLAST_RADIUS_PREFIX)


def path_matches(path: str, patterns: list[str]) -> bool:
    """Match a repo-relative path against globs, at the root or at any depth."""
    norm_path = path.replace("\\", "/").lstrip("./")
    for pattern in patterns:
        norm_pattern = pattern.replace("\\", "/").lstrip("./")
        if fnmatch.fnmatch(norm_path, norm_pattern) or fnmatch.fnmatch(
            norm_path, f"*/{norm_pattern}"
        ):
            return True
    return False


def filter_diff(diff: str, ignore: list[str]) -> str:
    """Drop whole-file hunks for paths matching `ignore` from a `git diff` text."""
    if not ignore or not diff:
        return diff
    blocks: list[str] = []
    current: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            blocks.append("".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append("".join(current))
    kept = []
    for block in blocks:
        header = block.splitlines()[0] if block else ""
        match = re.match(r"diff --git a/(.*) b/(.*)$", header)
        if match and path_matches(match.group(2), ignore):
            continue
        kept.append(block)
    return "".join(kept)


def scope_pattern_warnings(
    task_id: str, forbidden_paths: list[str], grader_ignore: list[str]
) -> list[str]:
    """Warn about forbidden patterns that also match nested copies under outputs/.

    Patterns match at any depth, so `data/*` also matches
    `outputs/measurements/baseline/data/orders.csv`. Agents that save audit copies
    of the project then trip the check even though they changed nothing.
    """
    messages: list[str] = []
    for pattern in forbidden_paths:
        probe = "outputs/measurements/copy/" + pattern.replace("*", "x").lstrip("./")
        if path_matches(probe, [pattern]) and not path_matches(probe, grader_ignore):
            messages.append(
                f"task {task_id}: forbidden_paths pattern '{pattern}' also matches nested "
                f"copies such as '{probe}'; an agent that saves an audit copy of the "
                "project would be flagged as a blast-radius violation. Add "
                "`grader_ignore: [\"outputs/*\"]` or rely on a grader-side file hash"
            )
    return messages

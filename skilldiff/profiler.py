from pathlib import Path
from typing import Any, Optional


def measure_skill_footprint(skill_dir: Optional[Path]) -> dict[str, Any]:
    if not skill_dir or not Path(skill_dir).is_dir():
        return {
            "file_count": 0,
            "total_bytes": 0,
            "total_chars": 0,
            "estimated_tokens": 0,
            "files": [],
        }

    root = Path(skill_dir).resolve()
    files_info = []
    total_bytes = 0
    total_chars = 0

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(
            part.startswith(".") or part == "__pycache__" for part in path.relative_to(root).parts
        ):
            continue
        try:
            raw = path.read_bytes()
            b_size = len(raw)
            total_bytes += b_size
            try:
                text = raw.decode("utf-8")
                c_size = len(text)
            except UnicodeDecodeError:
                c_size = b_size
            total_chars += c_size
            files_info.append(
                {
                    "path": str(path.relative_to(root)),
                    "bytes": b_size,
                    "estimated_tokens": round(c_size / 3.8),
                }
            )
        except OSError:
            continue

    est_tokens = round(total_chars / 3.8)
    return {
        "file_count": len(files_info),
        "total_bytes": total_bytes,
        "total_chars": total_chars,
        "estimated_tokens": est_tokens,
        "files": files_info,
    }


def compute_context_tax(
    footprint: dict[str, Any],
    median_turns: Optional[float] = 1.0,
    run_count: int = 1,
) -> dict[str, Any]:
    static_tokens = int(footprint.get("estimated_tokens", 0) or 0)
    turns = max(1.0, float(median_turns or 1.0))
    cumulative_session = round(static_tokens * turns)
    cumulative_experiment = cumulative_session * max(1, run_count)
    return {
        "static_tokens": static_tokens,
        "median_turns": turns,
        "session_tax_tokens": cumulative_session,
        "total_experiment_tokens": cumulative_experiment,
        "total_bytes": footprint.get("total_bytes", 0),
        "file_count": footprint.get("file_count", 0),
    }

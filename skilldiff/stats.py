"""Paired statistics for control/skill comparisons.

Each control run is paired with the treatment run that shares its model, task, and
repetition. Differences are always skill minus control.
"""

import random
import statistics
from typing import Any, Callable, Optional

BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20260922


def pair_runs(
    control_runs: list[dict[str, Any]], treatment_runs: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    def key(run: dict[str, Any]) -> tuple[str, str, int]:
        return (
            str(run.get("model", "")),
            str(run.get("task_id", "")),
            int(run.get("repetition", 1)),
        )

    treatment_by_key = {key(r): r for r in treatment_runs}
    return [
        (c, treatment_by_key[key(c)]) for c in control_runs if key(c) in treatment_by_key
    ]


def bootstrap_ci(
    values: list[float], level: float = 0.95
) -> Optional[tuple[float, float]]:
    """Percentile bootstrap confidence interval for the mean. None if n < 2."""
    if len(values) < 2:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(values)
    means = sorted(
        sum(rng.choices(values, k=n)) / n for _ in range(BOOTSTRAP_RESAMPLES)
    )
    alpha = (1 - level) / 2
    lo = means[int(alpha * BOOTSTRAP_RESAMPLES)]
    hi = means[min(int((1 - alpha) * BOOTSTRAP_RESAMPLES), BOOTSTRAP_RESAMPLES - 1)]
    return lo, hi


def _get_score(run: dict[str, Any]) -> Optional[float]:
    status = run.get("grade_status")
    if status in ("ungraded", "timeout", "error"):
        return None
    val = run.get("score", None)
    if val is None:
        # Backward compat: runs without grade_status but with no score key
        # are treated as unknown only when explicitly marked; otherwise
        # missing score means 0 only for very old records. Prefer None
        # when the key is absent to avoid silently inventing 0/100%.
        if "score" not in run:
            return None
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _get_cost(run: dict[str, Any]) -> Optional[float]:
    if "cost" not in run or run.get("cost") is None:
        return None
    try:
        return float(run.get("cost"))
    except (TypeError, ValueError):
        return None


def _get_duration(run: dict[str, Any]) -> Optional[float]:
    if "duration" not in run or run.get("duration") is None:
        return None
    try:
        return float(run.get("duration"))
    except (TypeError, ValueError):
        return None


def _total_tokens(run: dict[str, Any]) -> Optional[float]:
    keys = ("input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens")
    if all(k not in run or run.get(k) is None for k in keys):
        return None
    try:
        return float(sum(int(run.get(k) or 0) for k in keys))
    except (TypeError, ValueError):
        return None


def _get_turns(run: dict[str, Any]) -> Optional[float]:
    if "num_turns" not in run or run.get("num_turns") is None:
        return None
    try:
        return float(run.get("num_turns"))
    except (TypeError, ValueError):
        return None


METRICS: dict[str, Callable[[dict[str, Any]], Optional[float]]] = {
    "score": _get_score,
    "cost": _get_cost,
    "duration": _get_duration,
    "tokens": _total_tokens,
    "turns": _get_turns,
}


def paired_comparison(
    control_runs: list[dict[str, Any]], treatment_runs: list[dict[str, Any]]
) -> dict[str, Any]:
    pairs = pair_runs(control_runs, treatment_runs)
    result: dict[str, Any] = {"pairs": len(pairs)}
    for name, getter in METRICS.items():
        valid = [(getter(c), getter(t)) for c, t in pairs]
        valid = [(c, t) for c, t in valid if c is not None and t is not None]
        diffs = [t - c for c, t in valid]
        control_total = sum(c for c, _ in valid)
        ci = bootstrap_ci(diffs)
        result[name] = {
            "mean_diff": statistics.mean(diffs) if diffs else None,
            "median_diff": statistics.median(diffs) if diffs else None,
            "ci_low": ci[0] if ci else None,
            "ci_high": ci[1] if ci else None,
            "relative_change": (sum(diffs) / control_total) if control_total else None,
            # Valid pairs for this metric; total pairs is result["pairs"].
            "n": len(valid),
        }
    # Wins/losses/ties use only pairs where both scores are known.
    score_pairs = [
        (METRICS["score"](c), METRICS["score"](t)) for c, t in pairs
    ]
    score_pairs = [(c, t) for c, t in score_pairs if c is not None and t is not None]
    score_diffs = [t - c for c, t in score_pairs]
    result["wins"] = sum(1 for d in score_diffs if d > 1e-9)
    result["losses"] = sum(1 for d in score_diffs if d < -1e-9)
    result["ties"] = len(score_diffs) - result["wins"] - result["losses"]
    result["scored_pairs"] = len(score_pairs)
    return result


def classify_effect(metric: dict[str, Any], higher_is_better: bool = True) -> str:
    """Return 'better', 'worse', or 'unclear' from a paired metric summary."""
    lo, hi = metric.get("ci_low"), metric.get("ci_high")
    if lo is None or hi is None:
        return "unclear"
    if lo > 0:
        return "better" if higher_is_better else "worse"
    if hi < 0:
        return "worse" if higher_is_better else "better"
    return "unclear"

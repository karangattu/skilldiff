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


def _total_tokens(run: dict[str, Any]) -> float:
    return float(
        int(run.get("input_tokens", 0) or 0)
        + int(run.get("cache_read_tokens", 0) or 0)
        + int(run.get("cache_creation_tokens", 0) or 0)
        + int(run.get("output_tokens", 0) or 0)
    )


METRICS: dict[str, Callable[[dict[str, Any]], float]] = {
    "score": lambda r: float(r.get("score", 0.0) or 0.0),
    "cost": lambda r: float(r.get("cost", 0.0) or 0.0),
    "duration": lambda r: float(r.get("duration", 0.0) or 0.0),
    "tokens": _total_tokens,
}


def paired_comparison(
    control_runs: list[dict[str, Any]], treatment_runs: list[dict[str, Any]]
) -> dict[str, Any]:
    pairs = pair_runs(control_runs, treatment_runs)
    result: dict[str, Any] = {"pairs": len(pairs)}
    for name, getter in METRICS.items():
        diffs = [getter(t) - getter(c) for c, t in pairs]
        control_total = sum(getter(c) for c, _ in pairs)
        ci = bootstrap_ci(diffs)
        result[name] = {
            "mean_diff": statistics.mean(diffs) if diffs else 0.0,
            "median_diff": statistics.median(diffs) if diffs else 0.0,
            "ci_low": ci[0] if ci else None,
            "ci_high": ci[1] if ci else None,
            "relative_change": (sum(diffs) / control_total) if control_total else None,
        }
    score_diffs = [METRICS["score"](t) - METRICS["score"](c) for c, t in pairs]
    result["wins"] = sum(1 for d in score_diffs if d > 1e-9)
    result["losses"] = sum(1 for d in score_diffs if d < -1e-9)
    result["ties"] = len(score_diffs) - result["wins"] - result["losses"]
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

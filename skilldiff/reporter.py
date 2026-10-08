"""Metrics and reports for skilldiff experiments.

A report is built once as a list of simple blocks and rendered to three formats:
`report.html` (self-contained, no dependencies), `report.md` (renders on GitHub, handy
for pull requests), and `report.qmd` (for customizing with Quarto).
"""

import html
import json
import math
import re
import statistics
from pathlib import Path
from typing import Any, Optional, Union

from skilldiff.persistence import atomic_write
from skilldiff.scope import is_blast_violation
from skilldiff.stats import (
    analysis_run,
    classify_effect,
    pair_runs,
    paired_comparison,
    task_level_effects,
    usable_agent_run,
)

# --------------------------------------------------------------------------- metrics


def _total_tokens(run: dict[str, Any]) -> int:
    return (
        int(run.get("input_tokens", 0) or 0)
        + int(run.get("cache_read_tokens", 0) or 0)
        + int(run.get("cache_creation_tokens", 0) or 0)
        + int(run.get("output_tokens", 0) or 0)
    )


def _total_tokens_opt(run: dict[str, Any]) -> Optional[int]:
    keys = ("input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens")
    if all(k not in run or run.get(k) is None for k in keys):
        return None
    if any(k in run and run[k] is None for k in keys):
        return None
    try:
        return int(sum(int(run.get(k) or 0) for k in keys))
    except (TypeError, ValueError):
        return None


def _known_score(run: dict[str, Any]) -> Optional[float]:
    if not usable_agent_run(run):
        return None
    if run.get("grade_status") in ("ungraded", "timeout", "error"):
        return None
    if "score" not in run or run.get("score") is None:
        return None
    try:
        return float(run.get("score"))
    except (TypeError, ValueError):
        return None


def _known_float(run: dict[str, Any], key: str) -> Optional[float]:
    if key not in run or run.get(key) is None:
        return None
    try:
        return float(run.get(key))
    except (TypeError, ValueError):
        return None


def calculate_metrics(runs_data: list[dict[str, Any]]) -> dict[str, Any]:
    if not runs_data:
        return {
            "task_score": None,
            "success_count": 0,
            "total_count": 0,
            "graded_count": 0,
            "score_known_count": 0,
            "median_cost": None,
            "median_time": None,
            "median_tokens": None,
            "median_turns": None,
            "median_tool_calls": None,
            "cost_known_count": 0,
            "time_known_count": 0,
            "tokens_known_count": 0,
            "turns_known_count": 0,
            "tool_calls_known_count": 0,
            "total_duration": None,
            "total_cost": None,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cache_creation_tokens": 0,
            "total_tokens": None,
            "total_tool_calls": 0,
            "skill_used_count": 0,
            "skill_known_count": 0,
            "error_count": 0,
            "grade_status_counts": {},
        }

    analyzed = [r for r in runs_data if usable_agent_run(r)]
    scores = [s for r in analyzed if (s := _known_score(r)) is not None]
    costs = [c for r in analyzed if (c := _known_float(r, "cost")) is not None]
    times = [t for r in analyzed if (t := _known_float(r, "duration")) is not None]
    tok_opts = [_total_tokens_opt(r) for r in analyzed]
    tokens = [t for t in tok_opts if t is not None]
    turns = [
        int(r["num_turns"])
        for r in analyzed
        if "num_turns" in r and r.get("num_turns") is not None
    ]
    tool_calls = [
        int(r["tool_calls"])
        for r in analyzed
        if "tool_calls" in r and r.get("tool_calls") is not None
    ]
    known = [r for r in analyzed if r.get("skill_invoked") is not None]
    grade_counts: dict[str, int] = {}
    for r in runs_data:
        gs = str(r.get("grade_status") or ("graded" if "score" in r else "unknown"))
        grade_counts[gs] = grade_counts.get(gs, 0) + 1

    return {
        "task_score": statistics.mean(scores) if scores else None,
        "success_count": sum(1 for r in analyzed if r.get("success") is True),
        "total_count": len(analyzed),
        "graded_count": len(scores),
        "score_known_count": len(scores),
        "median_cost": statistics.median(costs) if costs else None,
        "median_time": statistics.median(times) if times else None,
        "median_tokens": statistics.median(tokens) if tokens else None,
        "median_turns": statistics.median(turns) if turns else None,
        "median_tool_calls": statistics.median(tool_calls) if tool_calls else None,
        "cost_known_count": len(costs),
        "time_known_count": len(times),
        "tokens_known_count": len(tokens),
        "turns_known_count": len(turns),
        "tool_calls_known_count": len(tool_calls),
        "total_duration": sum(times) if times else None,
        "total_cost": round(sum(costs), 4) if costs else None,
        "total_input_tokens": sum(int(r.get("input_tokens", 0) or 0) for r in analyzed),
        "total_output_tokens": sum(int(r.get("output_tokens", 0) or 0) for r in analyzed),
        "total_cache_read_tokens": sum(int(r.get("cache_read_tokens", 0) or 0) for r in analyzed),
        "total_cache_creation_tokens": sum(
            int(r.get("cache_creation_tokens", 0) or 0) for r in analyzed
        ),
        "total_tokens": sum(tokens) if tokens else None,
        "total_tool_calls": sum(int(r.get("tool_calls", 0) or 0) for r in analyzed),
        "skill_used_count": sum(1 for r in known if r.get("skill_invoked")),
        "skill_known_count": len(known),
        "error_count": sum(
            1 for r in runs_data if r.get("status") not in (None, "ok", "correctness")
        ),
        "grade_status_counts": grade_counts,
    }


def _paired_agent_runs(
    control_runs: list[dict[str, Any]], treatment_runs: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Use the same completed agent pairs for descriptive efficiency totals."""
    pairs = [
        (control, treatment)
        for control, treatment in pair_runs(control_runs, treatment_runs)
        if usable_agent_run(control) and usable_agent_run(treatment)
    ]
    return [control for control, _ in pairs], [treatment for _, treatment in pairs]


def _paired_metric_runs(
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
    metric: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return pairs with an eligible measurement on both sides for one metric."""
    def complete_tokens(run: dict[str, Any]) -> Optional[int]:
        if run.get("input_tokens") is None or run.get("output_tokens") is None:
            return None
        if any(run.get(key) is None for key in ("cache_read_tokens", "cache_creation_tokens")
               if key in run):
            return None
        return _total_tokens_opt(run)

    getters = {
        "score": _known_score,
        "cost": lambda run: _known_float(run, "cost"),
        "duration": lambda run: _known_float(run, "duration"),
        "tokens": _total_tokens_opt,
        "token_breakdown": complete_tokens,
        "turns": lambda run: _known_float(run, "num_turns"),
        "tool_calls": lambda run: _known_float(run, "tool_calls"),
    }
    getter = getters[metric]
    pairs = [
        (control, treatment)
        for control, treatment in pair_runs(control_runs, treatment_runs)
        if usable_agent_run(control)
        and usable_agent_run(treatment)
        and getter(control) is not None
        and getter(treatment) is not None
    ]
    return [control for control, _ in pairs], [treatment for _, treatment in pairs]


def _paired_comparison_metrics(
    control_runs: list[dict[str, Any]], treatment_runs: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Describe each comparison using the pairs behind that metric's delta."""
    paired_control, paired_treatment = _paired_agent_runs(control_runs, treatment_runs)
    control = calculate_metrics(paired_control)
    treatment = calculate_metrics(paired_treatment)
    fields = {
        "score": ("task_score",),
        "cost": ("median_cost", "total_cost", "cost_known_count"),
        "duration": ("median_time", "total_duration", "time_known_count"),
        "tokens": (
            "median_tokens", "total_tokens", "tokens_known_count",
            "total_input_tokens", "total_output_tokens", "total_cache_read_tokens",
            "total_cache_creation_tokens",
        ),
        "turns": ("median_turns", "turns_known_count"),
        "tool_calls": ("median_tool_calls", "total_tool_calls", "tool_calls_known_count"),
    }
    for metric, names in fields.items():
        metric_control, metric_treatment = _paired_metric_runs(
            control_runs, treatment_runs, metric
        )
        for target, runs in ((control, metric_control), (treatment, metric_treatment)):
            measured = calculate_metrics(runs)
            for name in names:
                target[name] = measured[name]
            if metric == "score":
                target["graded_count"] = measured["graded_count"]
                target["score_known_count"] = measured["score_known_count"]
    for target, runs in ((control, control_runs), (treatment, treatment_runs)):
        adoption = calculate_metrics(runs)
        for name in ("skill_used_count", "skill_known_count"):
            target[name] = adoption[name]
    return control, treatment


# ------------------------------------------------------------------------ formatting


def format_pp_diff(diff_pct: int) -> str:
    sign = "+" if diff_pct > 0 else ""
    return f"{sign}{diff_pct} pp"


def format_count_diff(diff_cnt: int) -> str:
    sign = "+" if diff_cnt > 0 else ""
    return f"{sign}{diff_cnt}"


def format_cost_diff(diff_val: float) -> str:
    sign = "+" if diff_val > 0.005 else ("-" if diff_val < -0.005 else "")
    return f"{sign}${abs(diff_val):.2f}"


def format_time_diff(diff_val: float) -> str:
    iv = int(round(float(diff_val)))
    sign = "+" if iv > 0 else ""
    return f"{sign}{iv}s"


def _fmt_count_delta(value: float) -> str:
    value = float(value)
    if abs(value) < 0.05:
        return "0"
    return f"{value:+.1f}" if abs(value) < 10 else f"{int(round(value)):+d}"


def _fmt_tokens(value: float) -> str:
    value = float(value)
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 10_000:
        return f"{value / 1000:.0f}k"
    if abs(value) >= 1000:
        return f"{value / 1000:.1f}k"
    return f"{value:.0f}"


def _signed_tokens(value: float) -> str:
    text = _fmt_tokens(abs(value))
    return f"+{text}" if value > 0.5 else (f"-{text}" if value < -0.5 else "0")


def _fmt_ci(metric: dict[str, Any], kind: str, total_pairs: Optional[int] = None) -> str:
    lo, hi = metric.get("ci_low"), metric.get("ci_high")
    n = metric.get("n", None)
    # Valid-pair suffix, e.g. " (n=3/4)" when some pairs lack this metric.
    suffix = ""
    if n is not None and total_pairs is not None:
        suffix = f" (n={n}/{total_pairs})" if n != total_pairs else f" (n={n})"
    elif n is not None:
        suffix = f" (n={n})"
    if lo is None or hi is None:
        return f"n/a{suffix}" if suffix else "n/a"
    if kind == "score":
        base = f"{lo * 100:+.0f} to {hi * 100:+.0f} pp"
    elif kind == "cost":
        base = f"{format_cost_diff(lo)} to {format_cost_diff(hi)}"
    elif kind == "duration":
        base = f"{lo:+.0f}s to {hi:+.0f}s"
    elif kind in ("turns", "tool_calls"):
        base = f"{_fmt_count_delta(lo)} to {_fmt_count_delta(hi)}"
    else:
        base = f"{_signed_tokens(lo)} to {_signed_tokens(hi)}"
    return base + suffix


def _score_percent(metrics: dict[str, Any]) -> Optional[int]:
    val = metrics.get("task_score", None)
    if val is None:
        return None
    try:
        return round(float(val) * 100)
    except (TypeError, ValueError):
        return None


def _fmt_score_pct(metrics: dict[str, Any]) -> str:
    pct = _score_percent(metrics)
    return f"{pct}%" if pct is not None else "N/A"


def _score_difference(control: dict[str, Any], skill: dict[str, Any]) -> Optional[int]:
    c, s = _score_percent(control), _score_percent(skill)
    if c is None or s is None:
        return None
    return s - c


def _fmt_cost_opt(val: Any) -> str:
    if val is None:
        return "N/A"
    try:
        return f"${float(val):.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_time_opt(val: Any) -> str:
    if val is None:
        return "N/A"
    try:
        return f"{round(float(val))}s"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_tokens_opt(val: Any) -> str:
    if val is None:
        return "N/A"
    try:
        return _fmt_tokens(float(val))
    except (TypeError, ValueError):
        return "N/A"


def _fmt_count_opt(val: Any) -> str:
    if val is None:
        return "N/A"
    try:
        return f"{float(val):g}"
    except (TypeError, ValueError):
        return "N/A"


def _paired_mean(paired: dict[str, Any], key: str) -> Optional[float]:
    m = (paired or {}).get(key) or {}
    v = m.get("mean_diff", None)
    return None if v is None else float(v)


def _skill_usage(metrics: dict[str, Any]) -> str:
    known = metrics.get("skill_known_count", 0)
    if not known:
        return "unknown"
    return f"{metrics.get('skill_used_count', 0)}/{known}"


_READING_BETTER = {
    "score": "Skill wins",
    "success": "More successes",
    "cost": "Costs less",
    "duration": "Faster",
    "tokens": "Fewer tokens",
    "turns": "Fewer turns",
    "tool_calls": "Fewer tool calls",
}
_READING_WORSE = {
    "score": "Skill loses",
    "success": "Fewer successes",
    "cost": "Costs more",
    "duration": "Slower",
    "tokens": "More tokens",
    "turns": "More turns",
    "tool_calls": "More tool calls",
}
_READING_SAME = {
    "score": "No clear difference",
    "success": "Same",
    "cost": "No clear difference",
    "duration": "No clear difference",
    "tokens": "No clear difference",
    "turns": "No clear difference",
    "tool_calls": "No clear difference",
}


def row_reading(
    kind: str,
    mean_diff: Optional[float],
    lo: Any = None,
    hi: Any = None,
    n: int = 0,
    total: int = 0,
    higher_is_better: bool = True,
    eps: float = 1e-9,
) -> str:
    """Plain-language verdict for one table row.

    Uses the same paired-mean difference and interval as the Δ and CI
    columns, so the three columns never disagree. Small or collapsed
    samples get an "(early sign)" suffix instead of a firm claim.
    """
    if mean_diff is None:
        return "Unknown"
    try:
        diff = float(mean_diff)
    except (TypeError, ValueError):
        return "Unknown"
    if total == 0:
        # Summary-only input with no pairing info: read the sign alone.
        if abs(diff) <= eps:
            return _READING_SAME.get(kind, "Same")
        better = (diff > 0) == higher_is_better
        return _READING_BETTER.get(kind, "Better") if better else _READING_WORSE.get(kind, "Worse")
    if n == 0:
        return "Unknown"
    if n == 1:
        return "Too little data"
    early = " (early sign)" if (n < 5 or (lo is not None and hi is not None and lo == hi)) else ""
    if lo is not None and hi is not None:
        try:
            lo_f, hi_f = float(lo), float(hi)
        except (TypeError, ValueError):
            return "Unknown"
        if lo_f > 0:
            return (
                _READING_BETTER.get(kind, "Better")
                if higher_is_better
                else _READING_WORSE.get(kind, "Worse")
            ) + early
        if hi_f < 0:
            return (
                _READING_WORSE.get(kind, "Better")
                if higher_is_better
                else _READING_BETTER.get(kind, "Worse")
            ) + early
        return _READING_SAME.get(kind, "Same") + early
    if abs(diff) <= eps:
        return _READING_SAME.get(kind, "Same")
    better = (diff > 0) == higher_is_better
    return (
        _READING_BETTER.get(kind, "Better") if better else _READING_WORSE.get(kind, "Worse")
    ) + early


def adoption_reading(used: int, known: int) -> str:
    if not known:
        return "Unknown"
    if used <= 0:
        return "Not used"
    if used >= known:
        return "Full adoption"
    return "Partial adoption"


def check_reading(wins: int, losses: int, pairs: int) -> str:
    if not pairs:
        return "No data"
    if wins > losses:
        return "Helps"
    if losses > wins:
        return "Hurts"
    return "No difference"


def category_reading(
    category: str, score_text: str, used: int, known: int, cost_mean: Optional[float]
) -> str:
    if category == "irrelevant":
        if known and used <= 0:
            return "Stays out of the way"
        if used > 0 and cost_mean is not None and cost_mean > 0:
            return "Interferes (costs more)"
        if used > 0:
            return "Uses skill here"
        return score_text
    if category == "intended":
        if score_text.startswith("Skill wins"):
            return "Helps here"
        if score_text.startswith("Skill loses"):
            return "Hurts here"
        return "No clear effect here"
    return score_text + " here" if score_text not in ("Unknown", "Too little data") else score_text


def _cost_basis_note(results: dict[str, Any]) -> str:
    pricing = results.get("pricing") or {}
    if results.get("cost_basis") == "api-equivalent":
        return ("Decision cost basis: API-equivalent cost, calculated from recorded usage "
                f"and rates from {pricing.get('source')} (checked {pricing.get('date')}). "
                "This estimates API spend; it is not subscription billing. Missing usage "
                "or model rates remain N/A. Saved harness cost is unchanged. "
                "For Codex, omitted cache-write usage is treated as zero; "
                "the estimate excludes any unreported cache writes.")
    if pricing:
        return (
            "Tokens include cached input where the harness reports it. Cost is the "
            "harness-reported price. On subscription auth the spend is $0 at the "
            "margin; the API-equivalent cost section converts the recorded token "
            f"breakdown with rates from {pricing.get('source')} "
            f"(checked {pricing.get('date')}), so regenerated reports reproduce "
            "the estimate. For Codex, an omitted cache-write count is treated as "
            "zero for accounting, so any unreported cache-write usage is excluded."
        )
    return (
        "Tokens include cached input where the harness reports it. Cost is the "
        "harness-reported price. On subscription auth the spend is $0 at the "
        "margin. No pricing rates were recorded with this run; record them in "
        "`skilldiff.yaml` (`pricing:` with source, date, and per-model rates per "
        "1M tokens) to get a reproducible API-equivalent cost table. For Codex, "
        "an omitted cache-write count is treated as zero for accounting, so any "
        "unreported cache-write usage is excluded."
    )


# Recorded token fields paired with their pricing rate keys (per 1M tokens).
_API_TOKEN_FIELDS = (
    ("input_tokens", "input"),
    ("cache_read_tokens", "cache_read"),
    ("cache_creation_tokens", "cache_write"),
    ("output_tokens", "output"),
)


def _fmt_money(val: Any, currency: str = "USD") -> str:
    if val is None:
        return "N/A"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "N/A"
    return f"${v:.2f}" if currency == "USD" else f"{v:.2f} {currency}"


def _fmt_money_diff(val: Any, currency: str = "USD") -> str:
    if val is None:
        return "N/A"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "N/A"
    sign = "+" if v > 0.005 else ("-" if v < -0.005 else "")
    body = f"${abs(v):.2f}" if currency == "USD" else f"{abs(v):.2f} {currency}"
    return f"{sign}{body}"


def decision_cost_runs(runs: list[dict[str, Any]], results: dict[str, Any]) -> list[dict[str, Any]]:
    """Use an explicitly recorded cost basis without changing saved run telemetry.

    Legacy results with no basis retain harness cost. Unknown usage or rates
    stay unknown, even if other token categories have measurements.
    """
    if results.get("cost_basis", "harness") != "api-equivalent":
        return runs
    rates = (results.get("pricing") or {}).get("rates") or {}
    normalized = []
    for run in runs:
        record = dict(run)
        record["harness_cost"] = run.get("harness_cost", run.get("cost"))
        record["cost"] = None
        model_rates = rates.get(str(run.get("model", ""))) or {}
        usage = [run.get(field) for field, _ in _API_TOKEN_FIELDS]
        if all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) and value >= 0 for value in usage):
            if all(key in model_rates for _, key in _API_TOKEN_FIELDS):
                record["cost"] = sum(run[field] * model_rates[key]
                                     for field, key in _API_TOKEN_FIELDS) / 1_000_000
        normalized.append(record)
    return normalized


def _api_equivalent_summary(
    runs: list[dict[str, Any]], pricing: dict[str, Any]
) -> dict[str, Any]:
    """Token breakdown and API-equivalent cost for one arm.

    Cost converts each run's recorded token counts with the rates saved in the
    run's pricing block (per 1M tokens), so the estimate reproduces from the
    saved run alone — no price lookups at report time.
    """
    rates = (pricing or {}).get("rates") or {}
    totals = {field: 0 for field, _ in _API_TOKEN_FIELDS}
    cost = 0.0
    priced_runs = 0
    eligible_runs = 0
    unpriced: set[str] = set()
    for run in runs or []:
        if not usable_agent_run(run):
            continue
        eligible_runs += 1
        tokens: dict[str, int] = {}
        for field, _rate_key in _API_TOKEN_FIELDS:
            try:
                tokens[field] = int(run.get(field) or 0)
            except (TypeError, ValueError):
                tokens[field] = 0
            totals[field] += tokens[field]
        model = str(run.get("model", ""))
        model_rates = rates.get(model)
        if not model_rates:
            if any(tokens.values()):
                unpriced.add(model or "(missing model)")
            continue
        priced_runs += 1
        cost += sum(
            tokens[field] * model_rates[rate_key] for field, rate_key in _API_TOKEN_FIELDS
        ) / 1_000_000.0
    return {
        "totals": totals,
        "cost": cost if priced_runs else None,
        "priced_runs": priced_runs,
        "eligible_runs": eligible_runs,
        "unpriced": sorted(unpriced),
    }


def render_report_table(
    experiment_name: str,
    control_metrics: dict[str, Any],
    skill_metrics: dict[str, Any],
    models_count: int,
    tasks_count: int,
    runs_per_arm: int,
    model_name: str | None = None,
    paired: dict[str, Any] | None = None,
    treatment_label: str = "Skill",
    thresholds: dict[str, Any] | None = None,
    preset: str | None = None,
    decision: dict[str, Any] | None = None,
    include_recommendation: bool = True,
) -> str:
    """Plain-text summary for the terminal.

    Control/Skill columns show means (score) or medians (cost/time/tokens)
    as descriptive statistics. Difference shows the paired-mean change so it
    matches the bootstrap CI in the full report.
    """
    c_score_pct = _score_percent(control_metrics)
    s_score_pct = _score_percent(skill_metrics)
    c_score_txt = f"{c_score_pct}%" if c_score_pct is not None else "N/A"
    s_score_txt = f"{s_score_pct}%" if s_score_pct is not None else "N/A"

    # Paired-mean score diff when available, else difference of means.
    paired_score = _paired_mean(paired or {}, "score")
    if paired_score is not None:
        diff_score_txt = format_pp_diff(round(paired_score * 100))
    else:
        d = _score_difference(control_metrics, skill_metrics)
        diff_score_txt = format_pp_diff(d) if d is not None else "N/A"

    paired_success = (paired or {}).get("success") or {}
    if (paired or {}).get("pairs"):
        success_n = int(paired_success.get("n", 0))
        if success_n:
            c_success = int(paired_success["control"])
            s_success = int(paired_success["treatment"])
            c_succ, s_succ = f"{c_success}/{success_n}", f"{s_success}/{success_n}"
            diff_succ = s_success - c_success
        else:
            c_succ = s_succ = "N/A"
            diff_succ = None
    else:
        c_succ = (
            f"{control_metrics.get('success_count', 0)}/"
            f"{control_metrics.get('total_count', 0)}"
        )
        s_succ = (
            f"{skill_metrics.get('success_count', 0)}/"
            f"{skill_metrics.get('total_count', 0)}"
        )
        diff_succ = skill_metrics.get("success_count", 0) - control_metrics.get("success_count", 0)

    c_cost = _fmt_cost_opt(control_metrics.get("median_cost"))
    s_cost = _fmt_cost_opt(skill_metrics.get("median_cost"))
    paired_cost = _paired_mean(paired or {}, "cost")
    if paired_cost is not None:
        diff_cost_txt = format_cost_diff(paired_cost) + " (mean)"
    elif (
        control_metrics.get("median_cost") is not None
        and skill_metrics.get("median_cost") is not None
    ):
        diff_cost_txt = format_cost_diff(
            float(skill_metrics["median_cost"]) - float(control_metrics["median_cost"])
        )
    else:
        diff_cost_txt = "N/A"

    c_time = _fmt_time_opt(control_metrics.get("median_time"))
    s_time = _fmt_time_opt(skill_metrics.get("median_time"))
    paired_dur = _paired_mean(paired or {}, "duration")
    if paired_dur is not None:
        diff_time_txt = format_time_diff(paired_dur) + " (mean)"
    elif (
        control_metrics.get("median_time") is not None
        and skill_metrics.get("median_time") is not None
    ):
        diff_time_txt = format_time_diff(
            float(skill_metrics["median_time"]) - float(control_metrics["median_time"])
        )
    else:
        diff_time_txt = "N/A"

    title = f"{experiment_name} ({model_name})" if model_name else experiment_name

    def row(label: str, c: str, s: str, d: str, r: str = "") -> str:
        return f"{label:<20} {c:>8} {s:>10} {d:>16} {r:<28}"

    def _term_reading(
        key: str, kind: str, diff: Optional[float], hib: bool, fallback: Optional[float] = None
    ) -> str:
        total = (paired or {}).get("pairs", 0) if paired else 0
        metric = ((paired or {}).get(key) or {}) if paired else {}
        if total:
            # Paired run: trust paired stats only, so Reading matches Δ.
            return row_reading(
                kind,
                diff,
                metric.get("ci_low"),
                metric.get("ci_high"),
                metric.get("n", 0),
                total,
                hib,
            )
        fb = fallback if fallback is not None else diff
        return row_reading(kind, fb, None, None, 0, 0, hib)

    score_m = paired_score if paired_score is not None else None
    if score_m is None:
        score_reading: str = "Unknown"
    elif (paired or {}).get("pairs"):
        score_metric = (paired or {}).get("score") or {}
        score_reading = row_reading(
            "score",
            score_m,
            score_metric.get("ci_low"),
            score_metric.get("ci_high"),
            score_metric.get("n", 0),
            (paired or {}).get("pairs", 0),
            True,
        )
    else:
        d_frac = _score_difference(control_metrics, skill_metrics)
        score_reading = row_reading(
            "score", None if d_frac is None else d_frac / 100.0, None, None, 0, 0, True
        )
    success_reading = (
        "No usable success pairs" if diff_succ is None else row_reading(
            "success", diff_succ, None, None,
            paired_success.get("n", 0) if paired_success else 0,
            (paired or {}).get("pairs", 0) if paired else 0,
            True,
        )
    )

    lines = [
        title,
        "",
        f"{'Metric':<20} {'Control':>8} {treatment_label:>10} "
        f"{'Paired mean Δ':>16} {'Reading':<28}",
        row("Task score", c_score_txt, s_score_txt, diff_score_txt, score_reading),
        row("Success", c_succ, s_succ,
            format_count_diff(diff_succ) if diff_succ is not None else "N/A",
            success_reading),
        row(
            "Cost (median)",
            c_cost,
            s_cost,
            diff_cost_txt,
            _term_reading(
                "cost",
                "cost",
                paired_cost,
                False,
                fallback=(
                    float(skill_metrics["median_cost"]) - float(control_metrics["median_cost"])
                    if control_metrics.get("median_cost") is not None
                    and skill_metrics.get("median_cost") is not None
                    else None
                ),
            ),
        ),
        row(
            "Time (median)",
            c_time,
            s_time,
            diff_time_txt,
            _term_reading(
                "duration",
                "duration",
                paired_dur,
                False,
                fallback=(
                    float(skill_metrics["median_time"]) - float(control_metrics["median_time"])
                    if control_metrics.get("median_time") is not None
                    and skill_metrics.get("median_time") is not None
                    else None
                ),
            ),
        ),
    ]
    if (
        control_metrics.get("median_tokens") is not None
        or skill_metrics.get("median_tokens") is not None
    ):
        c_tok = _fmt_tokens_opt(control_metrics.get("median_tokens"))
        s_tok = _fmt_tokens_opt(skill_metrics.get("median_tokens"))
        paired_tok = _paired_mean(paired or {}, "tokens")
        if paired_tok is not None:
            d_tok = _signed_tokens(paired_tok) + " (mean)"
        elif (
            control_metrics.get("median_tokens") is not None
            and skill_metrics.get("median_tokens") is not None
        ):
            d_tok = _signed_tokens(
                float(skill_metrics["median_tokens"]) - float(control_metrics["median_tokens"])
            )
        else:
            d_tok = "N/A"
        lines.append(
            row(
                "Tokens (median)",
                c_tok,
                s_tok,
                d_tok,
                _term_reading(
                    "tokens",
                    "tokens",
                    paired_tok,
                    False,
                    fallback=(
                        float(skill_metrics["median_tokens"])
                        - float(control_metrics["median_tokens"])
                        if control_metrics.get("median_tokens") is not None
                        and skill_metrics.get("median_tokens") is not None
                        else None
                    ),
                ),
            )
        )
    for label, key, field in (
        ("Tool calls (median)", "tool_calls", "median_tool_calls"),
        ("Turns (median)", "turns", "median_turns"),
    ):
        c_val, s_val = control_metrics.get(field), skill_metrics.get(field)
        if c_val is None and s_val is None and not ((paired or {}).get(key) or {}).get("n"):
            continue
        median_diff = (
            float(s_val) - float(c_val) if c_val is not None and s_val is not None else None
        )
        paired_diff = _paired_mean(paired or {}, key)
        if paired_diff is not None:
            diff_txt = _fmt_count_delta(paired_diff) + " (mean)"
        else:
            diff_txt = _fmt_count_delta(median_diff) if median_diff is not None else "N/A"
        lines.append(
            row(
                label,
                _fmt_count_opt(c_val),
                _fmt_count_opt(s_val),
                diff_txt,
                _term_reading(key, key, paired_diff, False, fallback=median_diff),
            )
        )
    if skill_metrics.get("skill_known_count"):
        lines.append(
            row(
                "Skill used",
                "-",
                _skill_usage(skill_metrics),
                "",
                adoption_reading(
                    skill_metrics.get("skill_used_count", 0),
                    skill_metrics.get("skill_known_count", 0),
                ),
            )
        )
    lines.extend(
        ["", f"Models: {models_count}    Tasks: {tasks_count}    Runs per arm: {runs_per_arm}"]
    )
    verdict_paired = decision["paired"] if decision is not None else paired
    if verdict_paired and verdict_paired.get("pairs"):
        c_score = (
            decision["control"] if decision is not None else control_metrics
        ).get("task_score")
        t_score = (
            decision["skill"] if decision is not None else skill_metrics
        ).get("task_score")
        verdict, _ = _verdict(
            verdict_paired,
            treatment_label.lower(),
            thresholds=thresholds,
            tasks_count=decision["tasks_count"] if decision is not None else tasks_count,
            preset=preset,
            control_score=c_score,
            treatment_score=t_score,
        )
        lines.append(_strip_inline(verdict))
    if include_recommendation:
        if decision is not None:
            if decision["basis_note"]:
                lines.append(decision["basis_note"])
            _, rec_label, rec_reason = decision["recommendation"]
        else:
            _, rec_label, rec_reason = _recommendation(
                paired or {}, treatment_label.lower(), thresholds=thresholds,
                tasks_count=tasks_count or None, preset=preset,
                control_score=control_metrics.get("task_score"),
                treatment_score=skill_metrics.get("task_score"),
            )
        lines.append("")
        lines.append(_strip_inline(f"Recommendation: {rec_label} — {rec_reason}"))
    return "\n".join(lines)


# ------------------------------------------------------------------- report blocks

Cell = Union[str, tuple[str, Optional[str]]]  # text, or (text, tone) with tone good/bad


def _tone(value: float, higher_is_better: bool, eps: float = 1e-9) -> Optional[str]:
    if abs(value) <= eps:
        return None
    return "good" if (value > 0) == higher_is_better else "bad"


def _verdict(
    paired: dict[str, Any],
    subject: str = "skill",
    thresholds: dict[str, Any] | None = None,
    tasks_count: int | None = None,
    failure_policy: dict[str, Any] | None = None,
    preset: str | None = None,
    control_score: float | None = None,
    treatment_score: float | None = None,
) -> tuple[str, str]:
    """Return (sentence, callout kind) for the task-score effect."""
    total = int(paired.get("pairs", 0))
    score = paired.get("score") or {}
    n_valid = int(score.get("n", paired.get("scored_pairs", total)))
    mean_raw = score.get("mean_diff", None)
    lo, hi = score.get("ci_low"), score.get("ci_high")
    effect = classify_effect(score)
    ci = _fmt_ci(score, "score", total_pairs=total)
    if n_valid < total:
        pairs_txt = f"{n_valid}/{total} graded pairs"
    else:
        pairs_txt = f"{n_valid} paired run{'s' if n_valid != 1 else ''}"

    if mean_raw is None or n_valid == 0:
        policy_txt = ""
        if failure_policy:
            fp = failure_policy.get("agent_failure", "exclude")
            policy_txt = f" Failure policy: agent_failure={fp} (failed sessions excluded as N/A)."
        return (
            f"No graded task-score pairs in {total} pair(s). "
            "Scores are N/A (ungraded tasks or grader failures); "
            f"only cost/time can be compared.{policy_txt}",
            "note",
        )

    mean_pp = round(float(mean_raw) * 100)

    # Cautions flagged directly in the headline.
    cautions: list[str] = []
    if n_valid < 5:
        cautions.append(f"only {n_valid} pair(s) — treat as preliminary")
    if lo is not None and hi is not None and lo == hi:
        cautions.append("CI collapsed (identical differences) — uncertainty is underestimated")
    if tasks_count is not None and tasks_count < 3 and total >= 4:
        cautions.append(
            f"only {tasks_count} task(s) — repetitions measure those tasks, "
            "not general skill effect"
        )
    caution_txt = f" ({'; '.join(cautions)})" if cautions else ""

    practical_txt = ""
    if thresholds:
        practical_txt = " " + _practical_assessment(paired, thresholds, mean_pp, preset=preset)

    if n_valid < 2:
        if mean_pp == 0:
            if control_score is not None and control_score >= 0.95:
                return (
                    f"Tasks at ceiling: control already scores {round(control_score * 100)}%; "
                    f"tasks cannot discriminate.{caution_txt}",
                    "note",
                )
            return f"No task-score difference in {pairs_txt}. Add repetitions.{caution_txt}", "note"
        return (
            f"Task score changed by **{format_pp_diff(mean_pp)}** in {pairs_txt}. "
            f"One pair can't separate a real effect from noise.{caution_txt}",
            "note",
        )
    if effect == "better":
        return (
            f"The {subject} improved task score by **{format_pp_diff(mean_pp)}** "
            f"(95% CI {ci}, {pairs_txt}){caution_txt}.{practical_txt}".replace("..", "."),
            "tip",
        )
    if effect == "worse":
        return (
            f"The {subject} reduced task score by **{abs(mean_pp)} pp** "
            f"(95% CI {ci}, {pairs_txt}){caution_txt}.{practical_txt}".replace("..", "."),
            "warning",
        )
    if mean_pp == 0 and lo == hi == 0:
        if control_score is not None and control_score >= 0.95:
            return (
                f"Tasks at ceiling: control already scores {round(control_score * 100)}%; "
                f"tasks cannot discriminate.{caution_txt}",
                "note",
            )
        if (
            control_score is not None and control_score <= 0.05
            and (treatment_score is None or treatment_score <= 0.05)
        ):
            return (
                f"Tasks at floor: both arms score ~0%; tasks cannot discriminate.{caution_txt}",
                "note",
            )
        if thresholds:
            if preset == "compression":
                return (
                    f"Quality preserved: both arms scored the same in all {pairs_txt} "
                    f"(95% CI {ci}){caution_txt}.{practical_txt}".replace("..", "."),
                    "tip" if "meets compression criteria" in practical_txt else "note",
                )
            return (
                f"No task-score difference: both arms scored the same in all {pairs_txt} "
                f"{caution_txt}.{practical_txt}".replace("..", "."),
                "note",
            )
        return (
            f"No task-score difference: both arms scored the same in all {pairs_txt}.{caution_txt}",
            "note",
        )
    if control_score is not None and control_score >= 0.95 and abs(mean_pp) <= 5:
        return (
            f"Tasks at ceiling: control already scores {round(control_score * 100)}%; "
            f"tasks cannot discriminate.{caution_txt}",
            "note",
        )
    return (
        f"No clear task-score effect: **{format_pp_diff(mean_pp)}**, but the 95% CI "
        f"({ci}) includes zero ({pairs_txt}){caution_txt}.{practical_txt}".replace("..", "."),
        "note",
    )


def _practical_assessment(
    paired: dict[str, Any],
    thresholds: dict[str, Any],
    mean_pp: int,
    preset: str | None = None,
) -> str:
    """Decision-oriented sentence from practical thresholds.

    Shipping decisions must depend on uncertainty, not point estimates: the
    confidence bound (not the rounded mean) must clear the gain or regression
    limit. Threshold keys (all optional):
      acceptable_score_regression_pp: score drop tolerated (e.g. 5 means -5pp ok).
      required_cost_reduction_pct: cost saving required (e.g. 10 means 10% cheaper).
      required_token_reduction_pct: session-token saving required (compression).
      meaningful_score_gain_pp: gain needed to call an improvement useful.
    For compression, quality must be preserved (lower bound within loss) AND
    resource use must fall by bounds, not just by averages.
    """
    try:
        allowed_loss = float(thresholds.get("acceptable_score_regression_pp", 0))
    except (TypeError, ValueError):
        allowed_loss = 0.0
    try:
        required_saving = float(thresholds.get("required_cost_reduction_pct", 0))
    except (TypeError, ValueError):
        required_saving = 0.0
    try:
        required_tokens = float(thresholds.get("required_token_reduction_pct", 0))
    except (TypeError, ValueError):
        required_tokens = 0.0
    try:
        meaningful_gain = float(thresholds.get("meaningful_score_gain_pp", 0))
    except (TypeError, ValueError):
        meaningful_gain = 0.0

    score_metric = (paired or {}).get("score") or {}
    ci_low = score_metric.get("ci_low")
    try:
        ci_low_pp = float(ci_low) * 100 if ci_low is not None else None
    except (TypeError, ValueError):
        ci_low_pp = None

    def _saving(key: str) -> tuple[Optional[float], bool, Any, Any]:
        metric = (paired or {}).get(key) or {}
        rel = metric.get("relative_change", None)
        pct: Optional[float] = None
        if rel is not None:
            try:
                pct = -float(rel) * 100
            except (TypeError, ValueError):
                pct = None
        lo, hi = metric.get("ci_low"), metric.get("ci_high")
        proven = False
        try:
            if lo is not None and hi is not None and float(hi) < 0:
                proven = True
        except (TypeError, ValueError):
            proven = False
        return pct, proven, lo, hi

    saving_pct, cost_proven_saving, cost_lo, _ = _saving("cost")
    token_pct, token_proven_saving, token_lo, _ = _saving("tokens")

    # Regression gate uses the lower confidence bound, not the mean.
    if ci_low_pp is not None:
        score_ok = ci_low_pp >= -allowed_loss
        gain_ok = (ci_low_pp >= meaningful_gain) if meaningful_gain else (ci_low_pp > 0)
    else:
        # No interval (n<2): fall back to point but flag as provisional.
        score_ok = mean_pp >= -allowed_loss
        gain_ok = mean_pp >= meaningful_gain if meaningful_gain else mean_pp > 0
    cost_ok = True
    if required_saving and saving_pct is None:
        cost_ok = False  # required saving but no cost data
    elif required_saving and saving_pct is not None:
        # Point estimate must meet the bar AND the interval must exclude cost increases.
        cost_ok = (saving_pct >= required_saving) and (
            cost_proven_saving or cost_lo is None
        )
    token_ok = True
    if required_tokens and token_pct is None:
        token_ok = False
    elif required_tokens and token_pct is not None:
        token_ok = (token_pct >= required_tokens) and (
            token_proven_saving or token_lo is None
        )

    if not score_ok:
        bound_txt = (
            f"{ci_low_pp:+.0f} pp lower bound"
            if ci_low_pp is not None
            else f"{mean_pp:+d} pp"
        )
        return (
            f"Practical check: {bound_txt} does not clear the allowed regression "
            f"(-{allowed_loss:g} pp). Do not ship on point estimates."
        )
    if required_saving and saving_pct is None:
        return "Practical check: cost data missing, cannot verify required saving."
    if required_tokens and token_pct is None:
        return "Practical check: token data missing, cannot verify required saving."
    if required_saving and saving_pct is not None and not cost_ok:
        if not cost_proven_saving and cost_lo is not None:
            return (
                f"Practical check: cost saving {saving_pct:+.0f}% meets "
                f"{required_saving:g}% on average "
                "but the cost interval includes zero — saving not established."
            )
        return (
            f"Practical check: cost saving {saving_pct:+.0f}% is below required "
            f"{required_saving:g}%."
        )
    if required_tokens and token_pct is not None and not token_ok:
        if not token_proven_saving and token_lo is not None:
            return (
                f"Practical check: token saving {token_pct:+.0f}% meets "
                f"{required_tokens:g}% on average "
                "but the token interval includes zero — saving not established."
            )
        return (
            f"Practical check: token saving {token_pct:+.0f}% is below required "
            f"{required_tokens:g}%."
        )
    # Compression decision: preserved quality + proven resource reduction.
    if preset == "compression" and (required_saving or required_tokens):
        if score_ok and cost_ok and token_ok:
            parts = [f"{mean_pp:+d} pp"]
            if required_saving and saving_pct is not None:
                parts.append(f"cost {saving_pct:+.0f}%")
            if required_tokens and token_pct is not None:
                parts.append(f"tokens {token_pct:+.0f}%")
            return (
                "Practical check: meets compression criteria — quality preserved "
                f"({', '.join(parts)}; bounds clear)."
            )
        return (
            "Practical check: compression not established — quality must be preserved "
            "by bounds and resource savings proven by intervals."
        )
    if gain_ok and cost_ok and token_ok:
        if (required_saving and saving_pct is not None) or (
            required_tokens and token_pct is not None
        ):
            bits = [f"{mean_pp:+d} pp"]
            if required_saving and saving_pct is not None:
                bits.append(f"cost {saving_pct:+.0f}%")
            if required_tokens and token_pct is not None:
                bits.append(f"tokens {token_pct:+.0f}%")
            return (
                "Practical check: meets criteria — bounds clear "
                f"({', '.join(bits)})."
            )
        if meaningful_gain:
            bound_note = (
                f"lower bound {ci_low_pp:+.0f} pp clears +{meaningful_gain:g} pp"
                if ci_low_pp is not None
                else f"mean {mean_pp:+d} pp (no interval)"
            )
            return f"Practical check: meets criteria — {bound_note} gain criterion."
        return "Practical check: meets criteria — bounds clear."
    return (
        "Practical check: detectable but not practically meaningful yet "
        "(bounds do not clear gain)."
    )


def _efficiency_sentence(paired: dict[str, Any], subject: str = "skill") -> Optional[str]:
    phrases: list[str] = []
    for key, less, more in (
        ("cost", "cost {}% less", "cost {}% more"),
        ("duration", "took {}% less time", "took {}% more time"),
        ("tokens", "used {}% fewer tokens", "used {}% more tokens"),
    ):
        metric = paired.get(key) or {}
        rel = metric.get("relative_change")
        if rel is None or abs(rel) < 0.005:
            continue
        text = (less if rel < 0 else more).format(f"{abs(rel) * 100:.0f}")
        if classify_effect(metric) == "unclear":
            text += " (within noise)"
        phrases.append(text)
    if not phrases:
        return None
    joined = phrases[0] if len(phrases) == 1 else ", ".join(phrases[:-1]) + " and " + phrases[-1]
    return f"With the {subject}, runs {joined}, summed over the compared pairs."


def _paired_diff_text(
    paired: dict[str, Any],
    key: str,
    kind: str,
    fallback: Optional[float] = None,
    higher_is_better: bool = False,
) -> tuple[str, Optional[str], str]:
    """Return (diff_text, tone, ci_text) for a paired-mean difference.

    Difference and CI measure the same thing (paired-mean change). Returns
    N/A when no valid pairs exist.
    """
    total = paired.get("pairs", 0)
    metric = (paired or {}).get(key) or {}
    mean_diff = metric.get("mean_diff", None)
    if mean_diff is None and fallback is not None and not total:
        # Summary-only results (no run records): fall back to aggregate diff.
        mean_diff = fallback
    if mean_diff is None:
        n = metric.get("n", 0)
        suffix = (
            f" (n={n}/{total})" if total and n != total else (f" (n={n})" if n is not None else "")
        )
        return "N/A", None, f"n/a{suffix}" if suffix else "n/a"
    mean_diff = float(mean_diff)
    if kind == "score":
        txt = format_pp_diff(round(mean_diff * 100))
        tone = _tone(round(mean_diff * 100), True)
    elif kind == "cost":
        txt = format_cost_diff(mean_diff)
        tone = _tone(round(mean_diff, 2), False)
    elif kind == "duration":
        txt = format_time_diff(mean_diff)
        tone = _tone(round(mean_diff), False)
    elif kind == "tokens":
        txt = _signed_tokens(mean_diff)
        tone = _tone(mean_diff, False, eps=0.5)
    else:
        txt = _fmt_count_delta(mean_diff)
        tone = _tone(mean_diff, False)
    return txt, tone, _fmt_ci(metric, kind, total_pairs=total)


def _summary_fallbacks(
    control: dict[str, Any], skill: dict[str, Any], total_pairs: int
) -> dict[str, Optional[float]]:
    """Difference-of-means fallbacks for summary-only results (no run records)."""
    out: dict[str, Optional[float]] = {
        "score": None,
        "cost": None,
        "duration": None,
        "tokens": None,
        "turns": None,
        "tool_calls": None,
    }
    if total_pairs:
        return out
    c_pct, s_pct = _score_percent(control), _score_percent(skill)
    if c_pct is not None and s_pct is not None:
        out["score"] = (s_pct - c_pct) / 100.0
    for key, c_key, s_key in (
        ("cost", "median_cost", "median_cost"),
        ("duration", "median_time", "median_time"),
        ("tokens", "median_tokens", "median_tokens"),
        ("turns", "median_turns", "median_turns"),
        ("tool_calls", "median_tool_calls", "median_tool_calls"),
    ):
        c_val, s_val = control.get(c_key), skill.get(s_key)
        if c_val is not None and s_val is not None:
            try:
                out[key] = float(s_val) - float(c_val)
            except (TypeError, ValueError):
                out[key] = None
    return out


def _reading_for(
    paired: dict[str, Any],
    total_pairs: int,
    key: str,
    kind: str,
    fallback: Optional[float],
    higher_is_better: bool = False,
) -> str:
    metric = (paired or {}).get(key) or {}
    md = metric.get("mean_diff", None)
    if md is None and fallback is not None and not total_pairs:
        return row_reading(kind, fallback, None, None, 0, 0, higher_is_better)
    return row_reading(
        kind,
        md,
        metric.get("ci_low"),
        metric.get("ci_high"),
        metric.get("n", 0),
        total_pairs,
        higher_is_better,
    )


def _metric_rows(
    control: dict[str, Any], skill: dict[str, Any], paired: dict[str, Any]
) -> list[list[Cell]]:
    total_pairs = paired.get("pairs", 0)
    # Fallbacks for summary-only results (no paired data).
    fb = _summary_fallbacks(control, skill, total_pairs)
    score_fallback = fb["score"]
    cost_fallback = fb["cost"]
    time_fallback = fb["duration"]
    tok_fallback = fb["tokens"]
    turn_fallback = fb["turns"]
    tc_fallback = fb["tool_calls"]

    score_txt, score_tone, score_ci = _paired_diff_text(
        paired, "score", "score", fallback=score_fallback, higher_is_better=True
    )
    cost_txt, cost_tone, cost_ci = _paired_diff_text(paired, "cost", "cost", fallback=cost_fallback)
    time_txt, time_tone, time_ci = _paired_diff_text(
        paired, "duration", "duration", fallback=time_fallback
    )
    tok_txt, tok_tone, tok_ci = _paired_diff_text(paired, "tokens", "tokens", fallback=tok_fallback)
    tc_txt, tc_tone, tc_ci = _paired_diff_text(
        paired, "tool_calls", "tool_calls", fallback=tc_fallback
    )
    turn_txt, turn_tone, turn_ci = _paired_diff_text(
        paired, "turns", "turns", fallback=turn_fallback
    )

    def _metric_reading(
        key: str, kind: str, fallback: Optional[float], higher_is_better: bool = False
    ) -> str:
        return _reading_for(paired, total_pairs, key, kind, fallback, higher_is_better)

    score_reading = _metric_reading("score", "score", score_fallback, True)
    cost_reading = _metric_reading("cost", "cost", cost_fallback)
    time_reading = _metric_reading("duration", "duration", time_fallback)
    tok_reading = _metric_reading("tokens", "tokens", tok_fallback)
    tc_reading = _metric_reading("tool_calls", "tool_calls", tc_fallback)
    turn_reading = _metric_reading("turns", "turns", turn_fallback)

    paired_success = paired.get("success") or {}
    success_n = int(paired_success.get("n", 0))
    if success_n:
        control_success = int(paired_success["control"])
        skill_success = int(paired_success["treatment"])
        success_diff = skill_success - control_success
        success_control_cell = f"{control_success}/{success_n}"
        success_skill_cell = f"{skill_success}/{success_n}"
        success_diff_cell = format_count_diff(success_diff)
        success_reading = row_reading(
            "success", success_diff, None, None, success_n, total_pairs, True
        )
    elif total_pairs:
        success_diff = 0
        success_control_cell = success_skill_cell = success_diff_cell = "N/A"
        success_reading = "No usable success pairs"
    else:
        # Summary-only records lack individual pairs.
        success_diff = skill.get("success_count", 0) - control.get("success_count", 0)
        success_control_cell = f"{control.get('success_count', 0)}/{control.get('total_count', 0)}"
        success_skill_cell = f"{skill.get('success_count', 0)}/{skill.get('total_count', 0)}"
        success_diff_cell = format_count_diff(success_diff)
        success_reading = row_reading("success", success_diff, None, None, 0, 0, True)
    has_cost = bool(
        (control.get("median_cost") is not None)
        or (skill.get("median_cost") is not None)
        or (control.get("total_cost") is not None and control.get("total_cost") not in (0, 0.0))
        or (skill.get("total_cost") is not None and skill.get("total_cost") not in (0, 0.0))
        or total_pairs == 0
    )
    # Show cost row when any cost data exists or when paired cost has valid pairs.
    cost_n = ((paired or {}).get("cost") or {}).get("n", 0)
    if not has_cost and not cost_n:
        show_cost = False
    else:
        # Hide only when both medians unknown and no valid paired cost.
        show_cost = not (
            control.get("median_cost") is None and skill.get("median_cost") is None and not cost_n
        )

    rows: list[list[Cell]] = [
        [
            "Task score (mean)",
            _fmt_score_pct(control),
            _fmt_score_pct(skill),
            (score_txt, score_tone),
            score_ci,
            score_reading,
        ],
        [
            "Success",
            success_control_cell,
            success_skill_cell,
            (success_diff_cell, _tone(success_diff, True) if success_diff_cell != "N/A" else None),
            "",
            success_reading,
        ],
    ]
    if show_cost:
        rows.append(
            [
                "Cost (median)",
                _fmt_cost_opt(control.get("median_cost")),
                _fmt_cost_opt(skill.get("median_cost")),
                (cost_txt, cost_tone),
                cost_ci,
                cost_reading,
            ]
        )
    rows.append(
        [
            "Time (median)",
            _fmt_time_opt(control.get("median_time")),
            _fmt_time_opt(skill.get("median_time")),
            (time_txt, time_tone),
            time_ci,
            time_reading,
        ]
    )
    if (
        control.get("median_tokens") is not None
        or skill.get("median_tokens") is not None
        or ((paired or {}).get("tokens") or {}).get("n")
    ):
        rows.append(
            [
                "Tokens (median)",
                _fmt_tokens_opt(control.get("median_tokens")),
                _fmt_tokens_opt(skill.get("median_tokens")),
                (tok_txt, tok_tone),
                tok_ci,
                tok_reading,
            ]
        )
    for label, key, field, txt, tone, ci, reading in (
        ("Tool calls (median)", "tool_calls", "median_tool_calls",
         tc_txt, tc_tone, tc_ci, tc_reading),
        ("Turns (median)", "turns", "median_turns",
         turn_txt, turn_tone, turn_ci, turn_reading),
    ):
        if (
            control.get(field) is not None
            or skill.get(field) is not None
            or ((paired or {}).get(key) or {}).get("n")
        ):
            rows.append([
                label, _fmt_count_opt(control.get(field)), _fmt_count_opt(skill.get(field)),
                (txt, tone), ci, reading,
            ])
    if skill.get("skill_known_count") or control.get("skill_known_count"):
        rows.append(
            [
                "Skill used",
                _skill_usage(control),
                _skill_usage(skill),
                "",
                "",
                adoption_reading(
                    skill.get("skill_used_count", 0), skill.get("skill_known_count", 0)
                ),
            ]
        )
    # Valid-pair note for scores when some pairs ungraded.
    score_n = ((paired or {}).get("score") or {}).get("n", None)
    if total_pairs and score_n is not None and score_n < total_pairs:
        rows.append(
            [
                "Graded pairs",
                f"{control.get('graded_count', score_n)}/{control.get('total_count', total_pairs)}",
                f"{skill.get('graded_count', score_n)}/{skill.get('total_count', total_pairs)}",
                "",
                f"n={score_n}/{total_pairs}",
                "",
            ]
        )
    return rows


def _decision_rows(
    control: dict[str, Any], skill: dict[str, Any], paired: dict[str, Any]
) -> list[list[Cell]]:
    """Closing decision table: score, cost, time, tokens, adoption.

    Every row stays visible even when data is missing (N/A), so the closing
    decision shows exactly what is unknown instead of hiding it. Paired
    change, CI, and Reading reuse the same statistics as the Summary table,
    so the two never disagree.
    """
    total_pairs = paired.get("pairs", 0)
    fb = _summary_fallbacks(control, skill, total_pairs)

    def row(
        label: str,
        c_txt: str,
        s_txt: str,
        key: str,
        kind: str,
        fallback: Optional[float],
        higher_is_better: bool = False,
        reading: Optional[str] = None,
    ) -> list[Cell]:
        diff_txt, tone, ci = _paired_diff_text(
            paired, key, kind, fallback=fallback, higher_is_better=higher_is_better
        )
        if reading is None:
            reading = _reading_for(paired, total_pairs, key, kind, fallback, higher_is_better)
        diff_cell: Cell = (diff_txt, tone) if tone else diff_txt
        return [label, c_txt, s_txt, diff_cell, ci, reading]

    return [
        row(
            "Task score (mean)",
            _fmt_score_pct(control),
            _fmt_score_pct(skill),
            "score",
            "score",
            fb["score"],
            True,
        ),
        row(
            "Cost (median)",
            _fmt_cost_opt(control.get("median_cost")),
            _fmt_cost_opt(skill.get("median_cost")),
            "cost",
            "cost",
            fb["cost"],
        ),
        row(
            "Time (median)",
            _fmt_time_opt(control.get("median_time")),
            _fmt_time_opt(skill.get("median_time")),
            "duration",
            "duration",
            fb["duration"],
        ),
        row(
            "Tokens (median)",
            _fmt_tokens_opt(control.get("median_tokens")),
            _fmt_tokens_opt(skill.get("median_tokens")),
            "tokens",
            "tokens",
            fb["tokens"],
        ),
        row(
            "Tool calls (median)",
            _fmt_count_opt(control.get("median_tool_calls")),
            _fmt_count_opt(skill.get("median_tool_calls")),
            "tool_calls",
            "tool_calls",
            fb["tool_calls"],
        ),
        row(
            "Turns (median)",
            _fmt_count_opt(control.get("median_turns")),
            _fmt_count_opt(skill.get("median_turns")),
            "turns",
            "turns",
            fb["turns"],
        ),
        [
            "Adoption (skill used)",
            _skill_usage(control),
            _skill_usage(skill),
            "",
            "",
            adoption_reading(
                skill.get("skill_used_count", 0), skill.get("skill_known_count", 0)
            ),
        ],
    ]


def _completeness_reading(
    planned: int,
    completed: int,
    usable: int,
    agent_failures: int,
    grader_errors: int,
    blast_errors: int = 0,
) -> str:
    """Plain-language verdict for the evaluation-completeness row."""
    if planned and not completed:
        return "Nothing completed"
    if not usable:
        return "No usable scores"
    gaps: list[str] = []
    if planned and completed < planned:
        gaps.append(f"{planned - completed} pair(s) not completed")
    if agent_failures:
        gaps.append(f"{agent_failures} agent failure(s)")
    if grader_errors:
        gaps.append(f"{grader_errors} grader error(s)")
    if blast_errors:
        gaps.append(f"{blast_errors} blast-radius exclusion(s)")
    if usable < completed:
        gaps.append(f"{completed - usable} pair(s) ungraded")
    return "Complete" if not gaps else "Partial — " + ", ".join(gaps)


_RECOMMENDATION_KIND = {"SHIP": "tip", "DO NOT SHIP": "warning", "NEEDS MORE RUNS": "note"}

_HELD_OUT_ALIASES = {"held-out", "heldout", "held_out", "held out", "holdout"}


def _run_split(run: dict[str, Any], task_splits: dict[str, str]) -> str:
    """Dev or held-out for one run, from the run record or task metadata.

    Unlabeled tasks count as dev: development results must never be mistaken
    for validation. Only an explicit held-out label (task `split: held-out`
    or a `heldout/` task directory) marks a run as validation data.
    """
    split = str(run.get("task_split") or task_splits.get(str(run.get("task_id", ""))) or "")
    return "held-out" if split.strip().lower() in _HELD_OUT_ALIASES else "dev"


def build_decision_context(
    results: dict[str, Any],
    control_runs: list[dict[str, Any]] | None = None,
    treatment_runs: list[dict[str, Any]] | None = None,
    run_root: Path | None = None,
    treatment_label: str = "Skill",
) -> dict[str, Any]:
    """Select the same eligible evidence and decision for every output surface."""
    policy = results.get("failure_policy") or (results.get("settings") or {}).get(
        "failure_policy") or {}
    if control_runs is None or treatment_runs is None:
        runs = _load_runs_for_report(results, run_root)
        control_runs, treatment_runs = runs["control"], runs["treatment"]
    control_runs = decision_cost_runs(control_runs, results)
    treatment_runs = decision_cost_runs(treatment_runs, results)
    control_runs = [analysis_run(r, policy.get("agent_failure", "exclude")) for r in control_runs]
    treatment_runs = [analysis_run(r, policy.get("agent_failure", "exclude"))
                      for r in treatment_runs]
    splits = {
        str(task["id"]): str(task.get("split", ""))
        for task in results.get("task_details") or []
        if isinstance(task, dict) and task.get("id")
    }
    held_control = [r for r in control_runs if _run_split(r, splits) == "held-out"]
    held_treatment = [r for r in treatment_runs if _run_split(r, splits) == "held-out"]
    held = paired_comparison(held_control, held_treatment) if held_control or held_treatment else {}
    if control_runs or treatment_runs:
        control, skill = _paired_comparison_metrics(control_runs, treatment_runs)
        paired = paired_comparison(control_runs, treatment_runs)
        selected_control, selected_treatment = control_runs, treatment_runs
        if held_control or held_treatment:
            # Even an unmatched held-out arm must not fall back to development evidence.
            selected_control, selected_treatment = held_control, held_treatment
        selected_paired = paired_comparison(selected_control, selected_treatment)
        selected_metrics = _paired_comparison_metrics(selected_control, selected_treatment)
        tasks_count = task_level_effects(selected_control, selected_treatment)["tasks"]
    else:
        overall = results.get("overall") or {}
        control = {**calculate_metrics([]), **overall.get("control", {})}
        skill = {**calculate_metrics([]), **overall.get("skill", {})}
        paired = overall.get("paired") or {}
        selected_paired, selected_metrics = paired, (control, skill)
        tasks_count = None  # Legacy summary-only records cannot establish task coverage.
    pairs = int(held.get("pairs", 0))
    scored = int((held.get("score") or {}).get("n", 0))
    coverage = f"{scored}/{pairs} usable score pair(s)"
    note = ""
    if held_control or held_treatment:
        note = (
            f"Headline and closing decision use held-out data only ({coverage}). "
            "Dev pairs are for iteration."
        )
    if results.get("cost_basis") == "api-equivalent":
        note = (note + " " if note else "") + _cost_basis_note(results)
    thresholds = results.get("thresholds") or (results.get("settings") or {}).get("thresholds")
    preset = results.get("preset") or (results.get("settings") or {}).get("preset")
    recommendation = _recommendation(
        selected_paired, treatment_label.lower(), thresholds=thresholds,
        tasks_count=tasks_count, preset=preset, valid=results.get("valid") is not False,
        paired_held_out=held if held.get("pairs") else None,
        control_score=selected_metrics[0].get("task_score"),
        treatment_score=selected_metrics[1].get("task_score"),
    )
    return {
        "overall_control": control, "overall_skill": skill, "overall_paired": paired,
        "control": selected_metrics[0], "skill": selected_metrics[1],
        "paired": selected_paired, "held_out": held, "tasks_count": tasks_count,
        "basis_note": note, "coverage": coverage, "recommendation": recommendation,
    }


def _recommendation(
    paired: dict[str, Any],
    subject: str = "skill",
    thresholds: dict[str, Any] | None = None,
    tasks_count: int | None = None,
    preset: str | None = None,
    valid: bool = True,
    paired_held_out: dict[str, Any] | None = None,
    control_score: float | None = None,
    treatment_score: float | None = None,
) -> tuple[str, str, str]:
    """Return (callout kind, SHIP/DO NOT SHIP/NEEDS MORE RUNS, reason)."""
    if not valid:
        return (
            _RECOMMENDATION_KIND["DO NOT SHIP"],
            "DO NOT SHIP",
            "Control contamination: there is no clean baseline, so differences "
            "cannot be attributed to the skill.",
        )

    basis = ""
    use = paired or {}
    if paired_held_out and paired_held_out.get("pairs"):
        use = paired_held_out
        basis = (
            f"Decision uses the {use.get('pairs')} held-out pair(s) only; "
            "dev results guide iteration, not validation. "
        )

    total = int(use.get("pairs", 0))
    score = use.get("score") or {}
    n = int(score.get("n", use.get("scored_pairs", total)))
    mean_raw = score.get("mean_diff", None)
    lo, hi = score.get("ci_low"), score.get("ci_high")
    if lo is not None and hi is not None:
        ci = f"{float(lo) * 100:+.0f} to {float(hi) * 100:+.0f} pp"
    else:
        ci = "n/a"
    pairs_txt = f"{n} graded pairs" if n == total else f"{n} graded of {total} pairs"

    def effect_sentence(direction: str) -> str:
        mean_pp = round(float(mean_raw) * 100)
        change = (
            f"improved task score by {format_pp_diff(mean_pp)}"
            if direction == "up"
            else f"reduced task score by {abs(mean_pp)} pp"
        )
        return f"The {subject} {change} (95% CI {ci}, {pairs_txt})"

    if total == 0:
        return (
            _RECOMMENDATION_KIND["NEEDS MORE RUNS"],
            "NEEDS MORE RUNS",
            "No paired runs in this result, so there is nothing to decide yet. "
            "Run the experiment to get paired scores with a confidence interval.",
        )
    if mean_raw is None or n == 0:
        return (
            _RECOMMENDATION_KIND["NEEDS MORE RUNS"],
            "NEEDS MORE RUNS",
            f"{basis}No graded task-score pairs in {total} pair(s); scores are N/A "
            "(ungraded tasks or grader failures), so the effect cannot be judged. "
            "Fix grading or add repetitions.",
        )
    if n == 1:
        return (
            _RECOMMENDATION_KIND["NEEDS MORE RUNS"],
            "NEEDS MORE RUNS",
            f"{basis}Only one graded pair; one pair cannot separate a real effect "
            "from noise. Add repetitions.",
        )

    effect = classify_effect(score)
    collapsed = lo is not None and hi is not None and lo == hi
    mean_pp = round(float(mean_raw) * 100)
    practical = ""
    if thresholds and mean_raw is not None:
        practical = _practical_assessment(use, thresholds, mean_pp, preset=preset)

    if practical:
        if practical.startswith("Practical check: meets"):
            if tasks_count is not None and tasks_count < 3:
                return (
                    _RECOMMENDATION_KIND["NEEDS MORE RUNS"], "NEEDS MORE RUNS",
                    f"{basis}Only {tasks_count} task(s) contributed usable paired scores. "
                    "Add representative tasks before shipping.",
                )
            return (
                _RECOMMENDATION_KIND["SHIP"],
                "SHIP",
                f"{basis}{effect_sentence('up' if mean_pp >= 0 else 'down')}, and "
                f"{practical[0].lower() + practical[1:]}",
            )
        if "does not clear the allowed regression" in practical or "below required" in practical:
            return (
                _RECOMMENDATION_KIND["DO NOT SHIP"],
                "DO NOT SHIP",
                f"{basis}{effect_sentence('up' if mean_pp >= 0 else 'down')}, but "
                f"{practical[0].lower() + practical[1:]}",
            )
        return (
            _RECOMMENDATION_KIND["NEEDS MORE RUNS"],
            "NEEDS MORE RUNS",
            f"{basis}{effect_sentence('up' if mean_pp >= 0 else 'down')}, but "
            f"{practical[0].lower() + practical[1:]}",
        )

    if effect == "worse":
        return (
            _RECOMMENDATION_KIND["DO NOT SHIP"],
            "DO NOT SHIP",
            f"{basis}{effect_sentence('down')}; the regression is established "
            "because the interval excludes zero.",
        )
    if mean_pp == 0 and lo == hi == 0:
        if control_score is not None and control_score >= 0.95:
            c_pct = round(control_score * 100)
            return (
                _RECOMMENDATION_KIND["NEEDS MORE RUNS"],
                "NEEDS MORE RUNS",
                f"{basis}Tasks at ceiling: control already scores {c_pct}% on average, "
                "so tasks cannot discriminate. Redesign tasks with harder challenges "
                "rather than adding repetitions.",
            )
        if (
            control_score is not None and control_score <= 0.05
            and (treatment_score is None or treatment_score <= 0.05)
        ):
            return (
                _RECOMMENDATION_KIND["NEEDS MORE RUNS"],
                "NEEDS MORE RUNS",
                f"{basis}Tasks at floor: both arms score ~0%, so tasks cannot discriminate. "
                "Redesign tasks with achievable challenges rather than adding repetitions.",
            )
        if n >= 5:
            return (
                _RECOMMENDATION_KIND["DO NOT SHIP"],
                "DO NOT SHIP",
                f"{basis}Both arms scored identically in all {n} graded pair(s) "
                "(95% CI 0 to 0 pp) — no measured benefit.",
            )
        return (
            _RECOMMENDATION_KIND["NEEDS MORE RUNS"],
            "NEEDS MORE RUNS",
            f"{basis}Both arms scored identically in the {n} graded pair(s) so far, "
            "but the sample is too small to rule out an effect. Add repetitions.",
        )
    if effect == "better":
        cautions: list[str] = []
        if n < 5:
            cautions.append(f"only {n} graded pairs")
        if collapsed:
            cautions.append(
                "the CI collapsed (identical differences), so uncertainty is understated"
            )
        if tasks_count is not None and tasks_count < 3:
            cautions.append(f"only {tasks_count} task(s), which limits generalization")
        if cautions:
            joined = (
                cautions[0]
                if len(cautions) == 1
                else ", ".join(cautions[:-1]) + " and " + cautions[-1]
            )
            return (
                _RECOMMENDATION_KIND["NEEDS MORE RUNS"],
                "NEEDS MORE RUNS",
                f"{basis}{effect_sentence('up')}, but {joined}. "
                "Treat the gain as preliminary and add runs before shipping.",
            )
        return (
            _RECOMMENDATION_KIND["SHIP"],
            "SHIP",
            f"{basis}{effect_sentence('up')}; the interval excludes zero.",
        )
    if control_score is not None and control_score >= 0.95 and abs(mean_pp) <= 5:
        c_pct = round(control_score * 100)
        return (
            _RECOMMENDATION_KIND["NEEDS MORE RUNS"],
            "NEEDS MORE RUNS",
            f"{basis}Tasks at ceiling: control already scores {c_pct}% on average, "
            "so tasks cannot discriminate. Redesign tasks with harder challenges "
            "rather than adding repetitions.",
        )
    return (
        _RECOMMENDATION_KIND["NEEDS MORE RUNS"],
        "NEEDS MORE RUNS",
        f"{basis}No clear task-score effect: {format_pp_diff(mean_pp)} "
        f"(95% CI {ci}, {pairs_txt}). The interval includes zero, "
        "so the effect is not established. Add repetitions or harder tasks.",
    )


def _parse_feedback_data(fb: Any) -> Optional[dict[str, Any]]:
    if isinstance(fb, dict):
        return fb
    if not isinstance(fb, str) or not fb.strip():
        return None
    text = fb.strip()
    # Try whole output first (multi-line JSON), then last line (logs + JSON).
    for candidate in (text, text.splitlines()[-1].strip() if text.splitlines() else ""):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def _interpret_check_value(value: Any) -> Optional[bool]:
    """Interpret a single check value. None means unknown (not failed)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and 0.0 <= value <= 1.0:
            # Fractional scores: 0.5+ passes, e.g. partial credit.
            return bool(value >= 0.5)
        return bool(value != 0)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"pass", "passed", "true", "ok", "success", "successful", "yes", "y", "1"}:
            return True
        if s in {"fail", "failed", "false", "no", "n", "0", "error", "timeout"}:
            return False
        return None
    if isinstance(value, dict):
        for key in ("passed", "pass", "success", "ok", "result"):
            if key in value:
                return _interpret_check_value(value[key])
        if "score" in value:
            return _interpret_check_value(value["score"])
        return None
    if isinstance(value, (list, tuple)) and len(value) == 1:
        return _interpret_check_value(value[0])
    return None


def _check_name(entry: Any, index: int) -> str:
    if isinstance(entry, dict):
        for key in ("name", "check", "id", "label", "description", "test"):
            val = entry.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return f"Check {index + 1}"


def extract_checks(run: dict[str, Any]) -> list[tuple[str, Optional[bool]]]:
    """Return [(check_name, passed_or_None)] for a run. Empty when no checks."""
    data = _parse_feedback_data(run.get("feedback"))
    if not isinstance(data, dict):
        return []
    checks = data.get("checks")
    if isinstance(checks, dict):
        # {"check-name": true/false/{passed:...}}
        return [(str(k), _interpret_check_value(v)) for k, v in checks.items()]
    if not isinstance(checks, list) or not checks:
        return []
    out: list[tuple[str, Optional[bool]]] = []
    for i, entry in enumerate(checks):
        out.append((_check_name(entry, i), _interpret_check_value(entry)))
    return out


def extract_notes(run: dict[str, Any]) -> list[str]:
    """Free-text grader diagnostics from a `notes` key; never counted as checks."""
    data = _parse_feedback_data(run.get("feedback"))
    if not isinstance(data, dict):
        return []
    notes = data.get("notes")
    if isinstance(notes, str):
        return [notes] if notes.strip() else []
    if isinstance(notes, list):
        return [str(n) for n in notes if str(n).strip()]
    if isinstance(notes, dict):
        return [f"{k}: {v}" for k, v in notes.items()]
    return []


def _format_checks_passed(run: dict[str, Any]) -> str:
    checks = extract_checks(run)
    if checks:
        known = [(n, p) for n, p in checks if p is not None]
        if not known:
            return "N/A"
        passed = sum(1 for _, p in known if p)
        result = f"{passed}/{len(checks)}"
        if len(known) < len(checks):
            result += "*"
        if (run.get("status") not in (None, "ok", "correctness")
                or run.get("grade_status") in ("timeout", "error")):
            result += " (partial)"
        return result
    # No named checks: fall back to success, or N/A when ungraded.
    if run.get("grade_status") in ("ungraded", "timeout", "error"):
        return "N/A"
    succ = run.get("success")
    if succ is True:
        return "1/1"
    if succ is False:
        return "0/1"
    return "-"


def compare_checks(
    control_runs: list[dict[str, Any]], treatment_runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Per-check comparison showing which requirements the skill helps/hurts.

    Groups by (task_id, check_name). For each group reports control/skill
    pass counts, paired wins/losses/ties, and valid-pair counts.
    """
    from skilldiff.stats import pair_runs as _pair_runs

    pairs = _pair_runs(control_runs, treatment_runs)
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for c, t in pairs:
        # Partial grader checks describe an interrupted attempt, even when a
        # zero-score failure policy keeps that pair in the task-score summary.
        if (c.get("status") not in (None, "ok", "correctness")
                or t.get("status") not in (None, "ok", "correctness")
                or c.get("grade_status") in ("timeout", "error")
                or t.get("grade_status") in ("timeout", "error")):
            continue
        task_id = str(c.get("task_id") or t.get("task_id") or "")
        c_checks = {n: p for n, p in extract_checks(c)}
        t_checks = {n: p for n, p in extract_checks(t)}
        # Align by name; fall back to positional Check N when names are generic.
        names = list(dict.fromkeys(list(c_checks) + list(t_checks)))
        if not names:
            continue
        for name in names:
            key = (task_id, name)
            entry = agg.setdefault(
                key,
                {
                    "task_id": task_id,
                    "check": name,
                    "control_pass": 0,
                    "control_total": 0,
                    "skill_pass": 0,
                    "skill_total": 0,
                    "wins": 0,
                    "losses": 0,
                    "ties": 0,
                    "pairs": 0,
                },
            )
            cp, tp = c_checks.get(name), t_checks.get(name)
            if cp is not None:
                entry["control_total"] += 1
                entry["control_pass"] += 1 if cp else 0
            if tp is not None:
                entry["skill_total"] += 1
                entry["skill_pass"] += 1 if tp else 0
            if cp is not None and tp is not None:
                entry["pairs"] += 1
                if tp and not cp:
                    entry["wins"] += 1
                elif cp and not tp:
                    entry["losses"] += 1
                else:
                    entry["ties"] += 1
    rows = list(agg.values())
    rows.sort(key=lambda r: (r["task_id"], r["check"]))
    for r in rows:
        c_rate = (r["control_pass"] / r["control_total"]) if r["control_total"] else None
        s_rate = (r["skill_pass"] / r["skill_total"]) if r["skill_total"] else None
        r["control_rate"] = c_rate
        r["skill_rate"] = s_rate
        r["diff_pp"] = (
            round((s_rate - c_rate) * 100) if c_rate is not None and s_rate is not None else None
        )
    return rows


def _load_runs_for_report(
    results: dict[str, Any], run_root: Path | None = None
) -> dict[str, list[dict[str, Any]]]:
    if "runs" in results and isinstance(results["runs"], dict):
        c = results["runs"].get("control", [])
        t = results["runs"].get("treatment", [])
        if c or t:
            loaded = {"control": list(c), "treatment": list(t)}
            return _apply_codex_cache_write_fallback(loaded, results)

    search_dirs: list[Path] = []
    if run_root and run_root.exists():
        search_dirs.append(run_root)
    if "run_dir" in results and results["run_dir"]:
        p = Path(results["run_dir"])
        if p.exists() and p not in search_dirs:
            search_dirs.append(p)

    for sdir in search_dirs:
        c_runs: list[dict[str, Any]] = []
        t_runs: list[dict[str, Any]] = []
        for run_file in sorted(sdir.rglob("run.json")):
            try:
                d = json.loads(run_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            d.setdefault("artifacts", str(run_file.parent.relative_to(sdir)))
            if d.get("arm") == "control":
                c_runs.append(d)
            elif d.get("arm") in {"treatment", "skill"}:
                t_runs.append(d)
        if c_runs or t_runs:
            return _apply_codex_cache_write_fallback(
                {"control": c_runs, "treatment": t_runs}, results
            )

    return {"control": [], "treatment": []}


def _apply_codex_cache_write_fallback(
    runs: dict[str, list[dict[str, Any]]], results: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """Treat omitted Codex cache-write usage as zero so known counts remain usable."""
    if results.get("harness") != "codex":
        return {arm: decision_cost_runs(records, results) for arm, records in runs.items()}
    normalized = {arm: [dict(run) for run in records] for arm, records in runs.items()}
    for records in normalized.values():
        for run in records:
            if (
                run.get("cache_creation_tokens") is None
                and run.get("input_tokens") is not None
                and run.get("output_tokens") is not None
            ):
                run["cache_creation_tokens"] = 0
    return {arm: decision_cost_runs(records, results) for arm, records in normalized.items()}


def _run_status(run: dict[str, Any]) -> Cell:
    status = str(run.get("status") or "ok")
    return ("ok", None) if status == "ok" else (status, "bad")


def _is_blast_run(run: dict[str, Any]) -> bool:
    """A run scored N/A because it edited paths outside the task's allowed scope."""
    return run.get("grade_error_kind") == "blast_radius" or is_blast_violation(
        run.get("feedback")
    )


def _score_cell(run: dict[str, Any]) -> str:
    if not usable_agent_run(run):
        return "N/A (agent failure)"
    if run.get("failure_scored_zero"):
        return "0% (failure policy)"
    gs = run.get("grade_status")
    if gs in ("ungraded", "timeout", "error"):
        label = {"ungraded": "ungraded", "timeout": "grader timeout", "error": "grader error"}[gs]
        if _is_blast_run(run):
            label = "blast radius"
        return f"N/A ({label})"
    if "score" not in run or run.get("score") is None:
        return "N/A"
    try:
        return f"{round(float(run['score']) * 100)}%"
    except (TypeError, ValueError):
        return "N/A"


def _cost_cell(run: dict[str, Any]) -> str:
    if "cost" not in run or run.get("cost") is None:
        return "N/A"
    try:
        return f"${float(run['cost']):.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _time_cell(run: dict[str, Any]) -> str:
    if "duration" not in run or run.get("duration") is None:
        return "N/A"
    try:
        return f"{float(run['duration']):.0f}s"
    except (TypeError, ValueError):
        return "N/A"


def _turns_cell(run: dict[str, Any]) -> str:
    if "num_turns" not in run or run.get("num_turns") is None:
        return "N/A"
    try:
        return str(int(run["num_turns"]))
    except (TypeError, ValueError):
        return "N/A"


def _tokens_cell(run: dict[str, Any]) -> str:
    val = _total_tokens_opt(run)
    return _fmt_tokens(val) if val is not None else "N/A"


def _skill_cell(run: dict[str, Any]) -> Cell:
    invoked = run.get("skill_invoked")
    if invoked is None:
        return "?"
    if run.get("arm") == "control":
        return ("yes", "bad") if invoked else "no"
    return "yes" if invoked else ("no", "bad")


_EVALUATION_HEADERS = [
    "App", "Arm", "Score", "Time", "Input", "Cached input", "Output",
    "Total tokens", "Tool calls", "Turns", "Skill loaded", "API-equivalent cost",
]
_EVALUATION_ALIGN = ["l", "l", "r", "r", "r", "r", "r", "r", "r", "r", "l", "r"]
_EVALUATION_NOTE = (
    "App is the task ID (with the model when multiple models were evaluated). "
    "Control and skill scores use pairs graded on both sides. Time, tokens, and cost "
    "use matched eligible pairs with those measurements; tool calls and turns use matched "
    "eligible agent runs. The Δ row is the second arm minus the first over those same "
    "pairs. Excluded and unpaired attempts remain in Run details. "
    "Cached input includes cache reads and cache writes. Skill loaded covers all "
    "eligible runs, including unpaired runs. A lone arm and the optional baseline "
    "show descriptive totals. Tool calls and turns are counted by each harness "
    "differently, so compare them within a run, not across harnesses. "
    "N/A means a measurement or recorded pricing is missing."
)


def _fmt_int_opt(val: Optional[int]) -> str:
    return "N/A" if val is None else f"{val:,}"


def _fmt_int_delta(val: int) -> str:
    return "0" if val == 0 else f"{val:+,}"


def _evaluation_delta_row(
    app: str, label: str, first: dict[str, Any], second: dict[str, Any], currency: str
) -> list[Cell]:
    def diff(key: str) -> Optional[float]:
        a, b = first[key], second[key]
        return None if a is None or b is None else b - a

    score_diff = (
        None if first["score"] is None or second["score"] is None
        else second["score"] - first["score"]
    )
    cells: list[Cell] = [app, label, "N/A" if score_diff is None else format_pp_diff(score_diff)]
    time_diff = diff("time")
    cells.append("N/A" if time_diff is None else format_time_diff(time_diff))
    for key in ("input", "cached", "output", "tokens", "tool_calls", "turns"):
        value = diff(key)
        cells.append("N/A" if value is None else _fmt_int_delta(int(value)))
    cells.append("")
    cost_diff = diff("cost")
    cells.append(_fmt_money_diff(cost_diff, currency))
    return cells


def _evaluation_rows(
    results: dict[str, Any],
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
) -> list[list[Cell]]:
    """One row per task, model, and arm, plus a Δ row, using recorded pricing only."""
    policy = (results.get("failure_policy") or (results.get("settings") or {}).get(
        "failure_policy") or {}).get("agent_failure", "exclude")
    baseline_runs = [
        analysis_run(r, policy) for r in ((results.get("runs") or {}).get("baseline") or [])
    ]
    arms = {"control": control_runs, "treatment": treatment_runs, "baseline": baseline_runs}
    keys = list(dict.fromkeys(
        (str(r.get("task_id", "")), str(r.get("model", "")))
        for runs in arms.values() for r in runs
    ))
    multi_model = len({model for _, model in keys}) > 1
    labels = results.get("arm_labels") or {}
    preset = results.get("preset") or (results.get("settings") or {}).get("preset")
    treatment_label = (
        "Minified" if preset == "compression" else
        "Skill B" if preset == "revision" or results.get("skill_comparison") else
        "Treatment" if results.get("comparison") else "Skill"
    )
    default_labels = {"control": "Control", "treatment": treatment_label, "baseline": "Baseline"}
    pricing = results.get("pricing") or {}
    currency = str(pricing.get("currency") or "USD")
    rows: list[list[Cell]] = []
    for task, model in keys:
        app = f"{task} ({model})" if multi_model else task
        grouped = {
            arm: [
                r for r in runs
                if (str(r.get("task_id", "")), str(r.get("model", ""))) == (task, model)
            ]
            for arm, runs in arms.items()
        }
        control_metrics, treatment_metrics = _paired_comparison_metrics(
            grouped["control"], grouped["treatment"]
        )
        paired_control, paired_treatment = _paired_agent_runs(
            grouped["control"], grouped["treatment"]
        )
        token_control, token_treatment = _paired_metric_runs(
            grouped["control"], grouped["treatment"], "token_breakdown"
        )
        values: dict[str, dict[str, Any]] = {}
        for arm in arms:
            group_all = grouped[arm]
            if not group_all:
                continue
            standalone = arm == "baseline" or not grouped[
                "treatment" if arm == "control" else "control"
            ]
            if arm == "control" and grouped["treatment"]:
                group, token_group, metrics = paired_control, token_control, control_metrics
            elif arm == "treatment" and grouped["control"]:
                group, token_group, metrics = paired_treatment, token_treatment, treatment_metrics
            else:
                group = [r for r in group_all if usable_agent_run(r)]
                token_group = group
                metrics = calculate_metrics(group)

            def total(field: str, measured: list[dict[str, Any]]) -> Optional[int]:
                if not measured or any(r.get(field) is None for r in measured):
                    return None
                return sum(int(r[field]) for r in measured)

            tokens_known = bool(token_group) and all(
                r.get("input_tokens") is not None
                and r.get("output_tokens") is not None
                and r.get("cache_read_tokens", 0) is not None
                and r.get("cache_creation_tokens", 0) is not None
                for r in token_group
            )
            token_metrics = calculate_metrics(token_group)
            api = _api_equivalent_summary(token_group, pricing)
            used, known = metrics["skill_used_count"], metrics["skill_known_count"]
            eligible_count = calculate_metrics(group_all)["total_count"]
            loaded = ("yes" if used else "no") if eligible_count == known == 1 else (
                f"{used}/{known}" if known else "N/A"
            )
            if known and known < eligible_count:
                loaded += f" ({eligible_count - known} unknown)"
            time_known = not standalone or metrics["time_known_count"] == len(group)
            v: dict[str, Any] = {
                "score": _score_percent(metrics),
                "time": metrics["total_duration"] if time_known else None,
                "input": total("input_tokens", token_group),
                "cached": (
                    token_metrics["total_cache_read_tokens"]
                    + token_metrics["total_cache_creation_tokens"]
                    if tokens_known else None
                ),
                "output": total("output_tokens", token_group),
                "tokens": token_metrics["total_tokens"] if tokens_known else None,
                "tool_calls": total("tool_calls", group),
                "turns": total("num_turns", group),
                "cost": (
                    api["cost"]
                    if tokens_known and api["priced_runs"] == len(token_group) else None
                ),
            }
            values[arm] = v
            rows.append([
                app,
                str(labels.get(arm) or default_labels[arm]),
                "N/A" if v["score"] is None else f"{v['score']}%",
                _fmt_time_opt(v["time"]),
                _fmt_int_opt(v["input"]), _fmt_int_opt(v["cached"]),
                _fmt_int_opt(v["output"]), _fmt_int_opt(v["tokens"]),
                _fmt_int_opt(v["tool_calls"]), _fmt_int_opt(v["turns"]),
                loaded,
                _fmt_money(v["cost"], currency),
            ])
            if arm == "treatment" and "control" in values:
                first_label = str(labels.get("control") or default_labels["control"])
                second_label = str(labels.get("treatment") or default_labels["treatment"])
                rows.append(_evaluation_delta_row(
                    app, f"Δ ({second_label} - {first_label})",
                    values["control"], v, currency,
                ))
    return rows


def render_evaluation_table(results: dict[str, Any], run_root: Path | None = None) -> str:
    """Markdown table shared by the final terminal output and saved reports."""
    runs = _load_runs_for_report(results, run_root)
    policy = (results.get("failure_policy") or (results.get("settings") or {}).get(
        "failure_policy") or {}).get("agent_failure", "exclude")
    runs = {arm: [analysis_run(r, policy) for r in records] for arm, records in runs.items()}
    rows = _evaluation_rows(results, runs["control"], runs["treatment"])
    if not rows:
        return ""
    return "\n".join(_md_table(_EVALUATION_HEADERS, rows, _EVALUATION_ALIGN))


def _build_runs_table(
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
    multi_model: bool = False,
    link_artifacts: bool = True,
    treatment_label: str = "Skill",
) -> tuple[list[str], list[list[Cell]]]:
    headers = [
        "Task",
        "Run",
        "Arm",
        "Status",
        "Score",
        "Checks",
        "Skill used",
        "Cost",
        "Time",
        "Turns",
        "Tokens",
        "Files",
    ]
    if multi_model:
        headers.insert(0, "Model")
    if link_artifacts:
        headers.append("Artifacts")

    def key(r: dict[str, Any]) -> tuple[str, str, int]:
        return (str(r.get("model", "")), str(r.get("task_id", "")), int(r.get("repetition", 1)))

    ctrl_map = {key(r): r for r in control_runs}
    treat_map = {key(r): r for r in treatment_runs}
    ordered_keys = list(dict.fromkeys([key(r) for r in control_runs + treatment_runs]))

    rows: list[list[Cell]] = []
    for k in ordered_keys:
        for arm_label, run in (("Control", ctrl_map.get(k)), (treatment_label, treat_map.get(k))):
            if run is None:
                continue
            row: list[Cell] = [
                str(run.get("task_id", "")),
                str(run.get("repetition", 1)),
                arm_label,
                _run_status(run),
                _score_cell(run),
                _format_checks_passed(run),
                _skill_cell(run),
                _cost_cell(run),
                _time_cell(run),
                _turns_cell(run),
                _tokens_cell(run),
                str(len(run.get("files_changed") or [])),
            ]
            if multi_model:
                row.insert(0, str(run.get("model", "")))
            if link_artifacts:
                art = run.get("artifacts")
                row.append(
                    f"[transcript]({art}/transcript.txt) · [diff]({art}/diff.patch)" if art else ""
                )
            rows.append(row)
    if treatment_label != "Skill":
        index = headers.index("Skill used")
        headers.pop(index)
        for row in rows:
            row.pop(index)
    return headers, rows


def _key_takeaways(
    results: dict[str, Any],
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
) -> list[str]:
    subject = "treatment" if results.get("comparison") else "skill"
    paired = paired_comparison(control_runs, treatment_runs)
    control, skill = _paired_comparison_metrics(control_runs, treatment_runs)
    all_control = calculate_metrics(control_runs)
    all_skill = calculate_metrics(treatment_runs)
    bullets: list[str] = []

    n = paired.get("pairs", 0)
    scored = paired.get("scored_pairs", (paired.get("score") or {}).get("n", n))
    if n:
        c_pct, s_pct = _score_percent(control), _score_percent(skill)
        c_txt = f"{c_pct}%" if c_pct is not None else "N/A"
        s_txt = f"{s_pct}%" if s_pct is not None else "N/A"
        if scored:
            acc = (
                f"**Accuracy.** Control averaged {c_txt} and the {subject} "
                f"{s_txt}. Pair by pair, the {subject} scored higher in "
                f"{paired['wins']}, lower in {paired['losses']}, and tied in {paired['ties']} "
                f"of {scored} graded pair(s)"
            )
            if scored < n:
                acc += f" ({n - scored} ungraded)"
            acc += "."
            bullets.append(acc)
        else:
            bullets.append(
                f"**Accuracy.** No graded pairs in {n} pair(s); scores are N/A "
                "(ungraded tasks, agent failures, or grader failures). "
                "Only available efficiency data can be compared."
            )

    efficiency = _efficiency_sentence(paired, subject)
    if efficiency:
        totals: list[str] = []
        if control.get("total_duration") is not None and skill.get("total_duration") is not None:
            totals.append(f"time {control['total_duration']:.0f}s → {skill['total_duration']:.0f}s")
        else:
            totals.append("time N/A")
        if control.get("total_cost") is not None or skill.get("total_cost") is not None:
            cc = f"${control['total_cost']:.2f}" if control.get("total_cost") is not None else "N/A"
            ss = f"${skill['total_cost']:.2f}" if skill.get("total_cost") is not None else "N/A"
            totals.insert(0, f"cost {cc} → {ss}")
        bullets.append(f"**Efficiency.** {efficiency} Totals: {', '.join(totals)}.")

    if all_skill.get("skill_known_count"):
        used, known = all_skill["skill_used_count"], all_skill["skill_known_count"]
        text = f"**Adoption.** The agent used the skill in {used} of {known} skill runs"
        c_used = all_control.get("skill_used_count", 0)
        text += (
            f"; {c_used} control run(s) also referenced it."
            if c_used
            else "; no control run referenced it."
        )
        if used < known:
            text += (
                " Runs where the skill was ignored dilute any effect. A sharper "
                "`description` usually fixes this."
            )
        bullets.append(text)

    # Per-check takeaways: which requirements improved/regressed.
    try:
        check_rows = compare_checks(control_runs, treatment_runs)
    except Exception:
        check_rows = []
    improved = [r for r in check_rows if r.get("diff_pp") is not None and r["diff_pp"] > 0]
    regressed = [r for r in check_rows if r.get("diff_pp") is not None and r["diff_pp"] < 0]
    if improved or regressed:
        parts: list[str] = []
        if improved:
            best = max(improved, key=lambda r: r["diff_pp"] or 0)
            parts.append(
                f"biggest gain `{best['check']}` ({best['task_id']}, "
                f"{format_pp_diff(int(best['diff_pp']))})"
            )
        if regressed:
            worst = min(regressed, key=lambda r: r["diff_pp"] or 0)
            parts.append(
                f"biggest regression `{worst['check']}` ({worst['task_id']}, "
                f"{format_pp_diff(int(worst['diff_pp']))})"
            )
        bullets.append("**Checks.** " + "; ".join(parts) + ". See By check.")

    task_effects: list[tuple[float, str]] = []
    for task_id in results.get("tasks") or sorted({r.get("task_id", "") for r in control_runs}):
        tc = [r for r in control_runs if r.get("task_id") == task_id]
        tt = [r for r in treatment_runs if r.get("task_id") == task_id]
        if tc and tt:
            md = paired_comparison(tc, tt)["score"]["mean_diff"]
            if md is not None:
                task_effects.append((float(md), task_id))
    if len(task_effects) > 1:
        best = max(task_effects)
        worst = min(task_effects)
        if best[0] > 0:
            bullets.append(
                f"**Biggest gain:** `{best[1]}` ({format_pp_diff(round(best[0] * 100))})."
            )
        if worst[0] < 0:
            bullets.append(
                f"**Biggest regression:** `{worst[1]}` ({format_pp_diff(round(worst[0] * 100))})."
            )
    c_pct_all = _score_percent(control)
    planned_pairs = (
        len(results.get("models") or [])
        * int(results.get("tasks_count") or len(results.get("tasks") or []))
        * int(results.get("runs_per_arm") or 1)
    )
    if (task_effects and scored == n and (not planned_pairs or n == planned_pairs)
            and all(abs(e) < 1e-9 for e, _ in task_effects)
            and (c_pct_all or 0) >= 95):
        bullets.append(
            "**Ceiling effect.** Control already solves these tasks, so accuracy can't "
            f"improve. Add harder tasks the {subject} is designed for, such as obscure APIs, "
            "recent changes, or house conventions."
        )

    runs_per_arm = int(results.get("runs_per_arm") or 1)
    tasks_count = int(results.get("tasks_count") or len(results.get("tasks") or []) or 0)
    if n and (runs_per_arm < 5 or n < 5):
        bullets.append(
            f"**Sample size.** {n} pair(s) with {runs_per_arm} repetition(s) per task "
            "gives wide error bars. Use `runs: 5` or more as a starting point, not a "
            "sufficiency rule — pre-register the run budget before looking at results."
        )
    if n and tasks_count and tasks_count < 3:
        bullets.append(
            f"**Task coverage.** Only {tasks_count} distinct task(s): many repetitions "
            "narrow repetition noise but still describe only those tasks. Add "
            "representative, irrelevant, ambiguous, and regression tasks before generalizing."
        )
    # Grading validity note (infra vs agent failures).
    grade_issues = 0
    blast_issues = 0
    for r in control_runs + treatment_runs:
        if r.get("grade_status") in ("timeout", "error"):
            if _is_blast_run(r):
                blast_issues += 1
            else:
                grade_issues += 1
    if blast_issues:
        bullets.append(
            f"**Blast radius.** {blast_issues} run(s) edited paths outside the task's "
            "`allowed_paths`/`forbidden_paths`; their scores are N/A. Check that the "
            "task scope allows files the work legitimately needs (for example "
            "`outputs/`, or add it to `grader_ignore`) before blaming the agent or skill."
        )
    if grade_issues:
        bullets.append(
            f"**Grading.** {grade_issues} run(s) have grader timeouts/errors; "
            "their scores are N/A and excluded from means (valid-pair counts shown). "
            "These are infrastructure failures, not agent failures."
        )
    return bullets


def build_report_blocks(
    results: dict[str, Any], run_root: Path | None = None
) -> tuple[str, list[tuple]]:
    comparison = results.get("comparison")
    skill_comparison = results.get("skill_comparison")
    preset = results.get("preset") or (results.get("settings") or {}).get("preset")
    arm_labels = results.get("arm_labels") or {}
    if skill_comparison:
        preset = preset or skill_comparison.get("preset") or "revision"
    if preset == "compression":
        label = "Minified"
        subject = "minified"
    elif preset == "revision" or skill_comparison:
        label = "Skill B"
        subject = "skill B"
    else:
        label = "Treatment" if comparison else "Skill"
        subject = label.lower()
    # Prefer stored arm labels when present.
    if arm_labels.get("treatment"):
        label = str(arm_labels["treatment"])
        subject = label.lower()
    name = str(results.get("name", "experiment"))
    runs_data = _load_runs_for_report(results, run_root)
    failure_policy = results.get("failure_policy") or (results.get("settings") or {}).get(
        "failure_policy") or {}
    agent_failure = failure_policy.get("agent_failure", "exclude")
    runs_data = {
        arm: [analysis_run(r, agent_failure) for r in records]
        for arm, records in runs_data.items()
    }
    control_runs = runs_data["control"]
    treatment_runs = runs_data["treatment"]
    decision = build_decision_context(
        results, control_runs, treatment_runs, run_root, label
    )
    control, skill, paired = (
        decision["overall_control"], decision["overall_skill"], decision["overall_paired"]
    )
    tasks_count = int(results.get("tasks_count") or len(results.get("tasks") or []) or 0)
    task_splits = {
        str(td.get("id")): str(td.get("split", ""))
        for td in results.get("task_details") or []
        if isinstance(td, dict) and td.get("id")
    }
    paired_held_out = decision["held_out"]
    held_out_pairs = int(paired_held_out.get("pairs", 0))
    held_out_coverage = decision["coverage"]
    rec_tasks_count = decision["tasks_count"]
    headline_paired = decision["paired"]
    headline_control, headline_skill = decision["control"], decision["skill"]
    split_basis_note = decision["basis_note"]

    blocks: list[tuple] = []
    incomplete = results.get("incomplete_runs") or []
    if incomplete:
        known = [run.get("cost") for run in incomplete if run.get("cost") is not None]
        blocks.append(("callout", "warning", "Incomplete trials",
                       f"{len(incomplete)} incomplete arm record(s) are excluded from paired "
                       f"decisions. Saved harness cost: ${sum(known):.2f} across {len(known)} "
                       "measured arm(s); remaining spend and usage are unknown. Telemetry "
                       "and available diffs remain in the arm artifacts. Resume refuses "
                       "incomplete pairs; start a new run to avoid silently repeating paid work."))
    thresholds = results.get("thresholds") or (results.get("settings") or {}).get("thresholds")

    if headline_paired.get("pairs"):
        verdict, kind = _verdict(
            headline_paired,
            subject,
            thresholds=thresholds,
            tasks_count=rec_tasks_count,
            failure_policy=failure_policy,
            preset=preset,
            control_score=headline_control.get("task_score"),
            treatment_score=headline_skill.get("task_score"),
        )
    else:
        diff = _score_difference(headline_control, headline_skill)
        if diff is not None and diff > 0:
            verdict, kind = f"{label} improved task score by **{diff} percentage points**.", "tip"
        elif diff is not None and diff < 0:
            verdict = f"{label} reduced task score by **{abs(diff)} percentage points**."
            kind = "warning"
        elif diff is None:
            verdict, kind = "No graded scores; task-score change is N/A.", "note"
        else:
            verdict, kind = f"No measured task-score change between control and {subject}.", "note"
    # Invalid experiments (control contamination) must not read as shippable.
    if results.get("valid") is False:
        verdict = "INVALID: no clean baseline. " + verdict
        kind = "warning"
    body = [verdict]
    efficiency = (
        _efficiency_sentence(headline_paired, subject)
        if headline_paired.get("pairs")
        else None
    )
    if efficiency:
        body.append(efficiency)
    if split_basis_note:
        body.append(split_basis_note)
    # Task-level uncertainty: repetitions vs tasks.
    try:
        from skilldiff.stats import task_level_effects as _tle

        tle = _tle(control_runs, treatment_runs) if (control_runs or treatment_runs) else {}
        if tle and tle.get("tasks") and paired.get("pairs", 0) >= 4 and tle["tasks"] < 3:
            body.append(
                f"Task coverage: only {tle['tasks']} distinct task(s) across "
                f"{paired.get('pairs')} pair(s). Repetitions narrow repetition noise, "
                "not task variation — add tasks before generalizing."
            )
    except Exception:
        pass
    if failure_policy:
        body.append(
            "Failure policy (pre-registered): "
            + ", ".join(f"{k}={v}" for k, v in failure_policy.items())
            + ". Grader timeouts/errors are always N/A."
        )
    blocks.append(("callout", kind, "Verdict", body))

    warnings = list(results.get("warnings") or [])
    if warnings:
        blocks.append(("callout", "warning", "Check before trusting this result", warnings))

    models = results.get("models", [])
    blocks.append(("h", 2, "Summary"))
    blocks.append(
        (
            "table",
            ["Metric", "Control", label, "Paired mean Δ", "95% CI", "Reading"],
            _metric_rows(control, skill, paired),
            ["l", "r", "r", "r", "r", "l"],
        )
    )
    blocks.append(
        (
            "p",
            f"**Harness:** {results.get('harness', 'claude')} · **Models:** {len(models)} · "
            f"**Tasks:** {results.get('tasks_count', 0)} · "
            f"**Runs per arm:** {results.get('runs_per_arm', 0)}",
        )
    )

    # Evaluation completeness: how much of the planned design produced usable
    # data. One row, so partial results can be judged before reading effects.
    if control_runs or treatment_runs:
        planned = (
            len(models)
            * tasks_count
            * int(results.get("runs_per_arm") or 0)
        )
        completed = int(paired.get("pairs", 0))
        usable = int(
            (paired.get("score") or {}).get("n", paired.get("scored_pairs", 0)) or 0
        )
        agent_failures = sum(
            1
            for r in control_runs + treatment_runs
            if r.get("status") not in (None, "ok", "correctness")
        )
        grader_errors = sum(
            1
            for r in control_runs + treatment_runs
            if r.get("grade_status") in ("timeout", "error") and not _is_blast_run(r)
        )
        blast_errors = sum(1 for r in control_runs + treatment_runs if _is_blast_run(r))
        if not planned:
            planned = completed
        blocks.append(("h", 2, "Evaluation completeness"))
        blocks.append(
            (
                "table",
                [
                    "Planned pairs",
                    "Completed pairs",
                    "Usable score pairs",
                    "Agent failures",
                    "Grader errors",
                    "Reading",
                ],
                [
                    [
                        f"{planned}",
                        f"{completed}",
                        f"{usable}",
                        f"{agent_failures}",
                        f"{grader_errors}",
                        _completeness_reading(
                            planned, completed, usable, agent_failures, grader_errors,
                            blast_errors,
                        ),
                    ]
                ],
                ["r", "r", "r", "r", "r", "l"],
            )
        )
        note = (
            "Planned pairs = models × tasks × runs per arm. Completed pairs match "
            "both arms on (model, task, repetition); usable score pairs are those "
            "with eligible graded scores on both sides. Agent failures and grader errors "
            "count runs across both arms (agent failures: status other than ok; "
            "grader errors: grader timeout or error, scored N/A)."
        )
        if results.get("interrupted"):
            note += " The run was interrupted, so planned pairs reflect the full design."
        blocks.append(("p", note))

    # API-equivalent cost: the recorded token breakdown priced with the rates
    # saved alongside the run, so regeneration reproduces the estimate exactly.
    pricing = results.get("pricing") or {}
    if pricing and (control_runs or treatment_runs):
        currency = str(pricing.get("currency") or "USD")
        priced_control, priced_treatment = _paired_metric_runs(
            control_runs, treatment_runs, "token_breakdown"
        )
        c_api = _api_equivalent_summary(priced_control, pricing)
        s_api = _api_equivalent_summary(priced_treatment, pricing)
        cost_change = None
        if c_api["cost"] is not None and s_api["cost"] is not None:
            cost_change = float(s_api["cost"]) - float(c_api["cost"])

        def _api_row(arm: str, api: dict[str, Any]) -> list[Cell]:
            t = api["totals"]
            return [
                arm,
                *(
                    [_fmt_tokens(t[field]) for field, _ in _API_TOKEN_FIELDS]
                    if api["eligible_runs"] else ["N/A"] * len(_API_TOKEN_FIELDS)
                ),
                _fmt_money(api["cost"], currency),
            ]

        diff_row: list[Cell] = [f"Change ({label} − Control)"]
        for field, _rate_key in _API_TOKEN_FIELDS:
            diff_row.append(
                _signed_tokens(s_api["totals"][field] - c_api["totals"][field])
                if c_api["eligible_runs"] and s_api["eligible_runs"] else "N/A"
            )
        cost_cell: Cell = _fmt_money_diff(cost_change, currency)
        if cost_change is not None:
            cost_cell = (cost_cell, _tone(round(cost_change, 2), False))
        diff_row.append(cost_cell)

        blocks.append(("h", 2, "API-equivalent cost"))
        blocks.append(
            (
                "table",
                [
                    "Arm",
                    "Input",
                    "Cache read",
                    "Cache write",
                    "Output",
                    "API-equivalent cost",
                ],
                [_api_row("Control", c_api), _api_row(label, s_api), diff_row],
                ["l", "r", "r", "r", "r", "r"],
            )
        )
        rates = pricing.get("rates") or {}
        rate_bits = "; ".join(
            f"`{m}`: input {_fmt_money(r.get('input'), currency)}, "
            f"output {_fmt_money(r.get('output'), currency)}, "
            f"cache read {_fmt_money(r.get('cache_read'), currency)}, "
            f"cache write {_fmt_money(r.get('cache_write'), currency)}"
            for m, r in sorted(rates.items())
        )
        note = (
            f"Rates in {currency} per 1M tokens from {pricing.get('source')} "
            f"(checked {pricing.get('date')}). {rate_bits}. "
            "Each paired eligible run's recorded token counts × its model's rates, "
            "summed over matched pairs. Unpaired and failed attempts remain in "
            "Run details. "
            "The run saves rates, source, date, and the token breakdown, so this "
            "estimate reproduces from the saved run alone."
        )
        unpriced = sorted(
            set(_api_equivalent_summary(control_runs, pricing)["unpriced"])
            | set(_api_equivalent_summary(treatment_runs, pricing)["unpriced"])
        )
        if unpriced:
            note += (
                f" No rates recorded for {', '.join(f'`{m}`' for m in unpriced)}: "
                "those runs are excluded from the API-equivalent cost."
            )
        blocks.append(("p", note))

    if control_runs:
        takeaways = _key_takeaways(results, control_runs, treatment_runs)
        if takeaways:
            blocks.append(("h", 2, "Key takeaways"))
            blocks.append(("ul", takeaways))

    by_model = results.get("by_model", {})
    # Headers end with [Skill used?, Reading]; align the trailing Reading left.
    group_align = _GROUP_ALIGN if not comparison else [*_GROUP_ALIGN[:7], "l"]

    # By split: development vs held-out shown separately, so development
    # results are never mistaken for validation.
    if (control_runs or treatment_runs) and (
        task_splits or any(r.get("task_split") for r in control_runs + treatment_runs)
    ):
        blocks.append(("h", 2, "By split"))
        split_rows: list[list[Cell]] = []
        for split in ("dev", "held-out"):
            sc = [r for r in control_runs if _run_split(r, task_splits) == split]
            st = [r for r in treatment_runs if _run_split(r, task_splits) == split]
            if not sc and not st:
                continue
            split_rows.append(
                _group_row(
                    split,
                    *_paired_comparison_metrics(sc, st),
                    paired_comparison(sc, st),
                    show_usage=not comparison,
                )
            )
        blocks.append(("table", _group_headers("Split", label), split_rows, group_align))
        if held_out_pairs:
            blocks.append(
                (
                    "p",
                    f"Dev pairs are for iteration; the held-out set is the honest "
                    f"estimate. The headline and closing decision use the "
                    f"held-out set only ({held_out_coverage}).",
                )
            )
        else:
            blocks.append(
                (
                    "p",
                    "No held-out pairs in this run: everything reported here is "
                    "development data, not validation. Freeze tasks under "
                    "`tasks/heldout/` (or set `split: held-out`) before the full run.",
                )
            )

    if len(models) > 1:
        blocks.append(("h", 2, "By model"))
        rows: list[list[Cell]] = []
        for model in models:
            if control_runs:
                mc = [r for r in control_runs if r.get("model") == model]
                mt = [r for r in treatment_runs if r.get("model") == model]
                m_control, m_skill = _paired_comparison_metrics(mc, mt)
                m_paired = paired_comparison(mc, mt)
            else:
                m_control = by_model.get(model, {}).get("control", {})
                m_skill = by_model.get(model, {}).get("skill", {})
                m_paired = {}
            rows.append(_group_row(model, m_control, m_skill, m_paired, show_usage=not comparison))
        blocks.append(("table", _group_headers("Model", label), rows, group_align))

    blocks.append(("h", 2, "By task"))
    task_rows: list[list[Cell]] = []
    for model in models:
        task_ids = results.get("tasks") or list(by_model.get(model, {}).get("by_task", {}).keys())
        for task_id in task_ids:
            if control_runs:
                tc = [
                    r
                    for r in control_runs
                    if r.get("model") == model and r.get("task_id") == task_id
                ]
                tt = [
                    r
                    for r in treatment_runs
                    if r.get("model") == model and r.get("task_id") == task_id
                ]
                t_control, t_skill = _paired_comparison_metrics(tc, tt)
                t_paired = paired_comparison(tc, tt)
            else:
                task_data = by_model.get(model, {}).get("by_task", {}).get(task_id, {})
                t_control, t_skill = task_data.get("control", {}), task_data.get("skill", {})
                t_paired = {}
            task_label = f"{task_id} ({model})" if len(models) > 1 else task_id
            task_rows.append(
                _group_row(task_label, t_control, t_skill, t_paired, show_usage=not comparison)
            )
    blocks.append(("table", _group_headers("Task", label), task_rows, group_align))

    # Per-check comparison: which requirements the skill helps or hurts.
    if control_runs or treatment_runs:
        try:
            check_data = compare_checks(control_runs, treatment_runs)
        except Exception:
            check_data = []
        if check_data:
            blocks.append(("h", 2, "By check"))
            blocks.append(
                (
                    "p",
                    'Named checks from grader JSON (`{"checks": [{"name": ..., '
                    '"passed": ...}]}` or `[true, false]`). Δ shows skill minus control '
                    "in percentage points; W/L/T counts pairs where the skill passed "
                    "and control failed / vice versa / tied.",
                )
            )
            blocks.append(
                (
                    "table",
                    [
                        "Task",
                        "Check",
                        "Control",
                        label,
                        "Δ",
                        "Better/worse/tie",
                        "Pairs",
                        "Reading",
                    ],
                    _check_table_rows(check_data),
                    ["l", "l", "r", "r", "r", "r", "r", "l"],
                )
            )

    # By category: intended use vs irrelevant vs ambiguous triggers.
    task_cats: dict[str, str] = {}
    if isinstance(results.get("task_categories"), dict):
        task_cats = {str(k): str(v) for k, v in results["task_categories"].items()}
    # Runs may carry task_category directly (new experiments).
    for r in control_runs + treatment_runs:
        tid = str(r.get("task_id", ""))
        if tid and r.get("task_category") and tid not in task_cats:
            task_cats[tid] = str(r["task_category"])
    # Task metadata in results["task_details"] (new) or fallback.
    for td in results.get("task_details") or []:
        if isinstance(td, dict) and td.get("id") and td.get("category"):
            task_cats.setdefault(str(td["id"]), str(td["category"]))
    if task_cats and len(set(task_cats.values())) > 1:
        cat_rows = _category_summary(control_runs, treatment_runs, task_cats)
        if cat_rows:
            blocks.append(("h", 2, "By category"))
            blocks.append(
                (
                    "p",
                    "Tasks grouped by `category` (`intended`, `irrelevant`, `ambiguous`, "
                    "or custom). `intended` measures whether the skill helps relevant "
                    "tasks; `irrelevant` measures whether it stays out of the way "
                    "(adoption should be low, cost should not rise).",
                )
            )
            blocks.append(
                (
                    "table",
                    [
                        "Category",
                        "Tasks",
                        "Control",
                        label,
                        "Δ score",
                        "Skill used",
                        "Δ cost",
                        "Pairs",
                        "Reading",
                    ],
                    cat_rows,
                    ["l", "r", "r", "r", "r", "r", "r", "r", "l"],
                )
            )

    context_tax = results.get("context_tax") or {}
    if context_tax and context_tax.get("static_tokens", 0) > 0:
        blocks.append(("h", 2, "Skill context tax"))
        blocks.append(
            (
                "p",
                "Estimated text footprint of the frozen skill files. Session and experiment "
                "tax assume that footprint is carried each turn; these are estimates, "
                "not measured prompt injection or spend.",
            )
        )
        t_bytes = context_tax.get("total_bytes", 0)
        f_count = context_tax.get("file_count", 0)
        s_tokens = context_tax.get("static_tokens", 0)
        sess_tokens = context_tax.get("session_tax_tokens", 0)
        med_turns = context_tax.get("median_turns", 1.0)
        exp_tokens = context_tax.get("total_experiment_tokens", 0)
        tax_rows = [
            ["Installed skill size", f"{t_bytes:,} bytes", f"{f_count} file(s)", ""],
            ["Estimated text footprint", f"~{s_tokens:,} tokens", "Skill files", "Estimate"],
            [
                "Cumulative session tax",
                f"~{sess_tokens:,} tokens",
                f"Across {med_turns:.0f} median turn(s)",
                "Estimated overhead",
            ],
            ["Total experiment tax", f"~{exp_tokens:,} tokens", "All runs", "Estimated overhead"],
        ]
        blocks.append(
            (
                "table",
                ["Metric", "Value", "Scope", "Reading"],
                tax_rows,
                ["l", "r", "l", "l"],
            )
        )

    # Baseline comparisons: baseline-vs-A and baseline-vs-B so the optional
    # baseline answers whether either revision helps at all.
    baseline_comps = results.get("baseline_comparisons") or {}
    if baseline_comps:
        blocks.append(("h", 2, "Baseline comparisons"))
        blocks.append(
            (
                "p",
                "Optional no-skill arm run on the same fixtures in balanced rotation. "
                "Each row compares the baseline against one revision.",
            )
        )
        brow: list[list[Cell]] = []
        for key, aname in (("baseline_vs_a", "Baseline vs A"), ("baseline_vs_b", "Baseline vs B")):
            comp = baseline_comps.get(key) or {}
            pr = comp.get("paired") or {}
            score_m = (pr.get("score") or {}).get("mean_diff")
            if score_m is not None:
                s_txt: Cell = (
                    format_pp_diff(round(float(score_m) * 100)),
                    _tone(round(float(score_m) * 100), True),
                )
            else:
                s_txt = ("N/A", None)
            cost_m = (pr.get("cost") or {}).get("mean_diff")
            if cost_m is not None:
                c_txt: Cell = (
                    format_cost_diff(float(cost_m)),
                    _tone(round(float(cost_m), 2), False),
                )
            else:
                c_txt = ("N/A", None)
            n = pr.get("pairs", 0)
            scored = (pr.get("score") or {}).get("n", n)
            brow.append([aname, s_txt, c_txt, f"{scored}/{n}" if n else "-"])
        blocks.append(
            (
                "table",
                ["Comparison", "Δ score (other-baseline)", "Δ cost", "Pairs"],
                brow,
                ["l", "r", "r", "r"],
            )
        )

    # Compression static sizes: source reduction alongside session savings.
    sc = skill_comparison or {}
    if (preset == "compression" or sc.get("preset") == "compression") and (
        sc.get("source_bytes_a") or sc.get("source_bytes_b")
    ):
        try:
            ba = int(sc.get("source_bytes_a") or 0)
            bb = int(sc.get("source_bytes_b") or 0)
            red = float(sc.get("source_reduction_pct") or 0)
            blocks.append(("h", 2, "Source size"))
            blocks.append(
                (
                    "p",
                    f"Static skill size: original {ba} bytes → minified {bb} bytes "
                    f"({red:+.1f}% reduction). This is separate from session tokens, "
                    "cost, and time measured per run.",
                )
            )
        except Exception:
            pass

    if control_runs or treatment_runs:
        headers, run_rows = _build_runs_table(
            control_runs,
            treatment_runs,
            multi_model=len(models) > 1,
            link_artifacts=run_root is not None,
            treatment_label=label,
        )
        blocks.append(("h", 2, "Run details"))
        randomized = any("run_order" in r for r in control_runs)
        seeded = results.get("seed") is not None
        if preset == "compression":
            run_note = (
                "Each pair ran the original vs the minified skill on identical fixtures"
                + (", in balanced arm order" if randomized else "")
                + (f" (seed `{results.get('seed')}`)" if seeded else "")
                + ". Control column is Original; treatment column is Minified. "
                "Tune on dev tasks, then compare frozen versions on held-out tasks."
            )
            if (skill_comparison or {}).get("include_baseline"):
                run_note += " A no-skill baseline arm also ran per pair (see runs/baseline)."
        elif skill_comparison:
            run_note = (
                "Each pair ran skill A vs skill B on identical fixtures"
                + (", in balanced arm order" if randomized else "")
                + (f" (seed `{results.get('seed')}`)" if seeded else "")
                + ". Control column is Skill A; treatment column is Skill B."
            )
            if (skill_comparison or {}).get("include_baseline"):
                run_note += (
                    " A no-skill baseline arm also ran per pair in balanced rotation "
                    "(see Baseline comparisons)."
                )
        else:
            run_note = (
                (
                    "Each pair ran in fresh workspaces from the recorded revisions"
                    if comparison
                    else "Each pair ran in identical fresh workspaces"
                )
                + (", in balanced arm order" if randomized else "")
                + (f" (seed `{results.get('seed')}`)" if seeded else "")
                + (
                    ". Control excludes the PR; treatment includes it."
                    if comparison
                    else ". Only the skill arm had the skill installed."
                )
            )
        blocks.append(("p", run_note))
        align = ["r" if h in _NUMERIC_RUN_COLUMNS else "l" for h in headers]
        blocks.append(("table", headers, run_rows, align))

        note_rows = [
            [
                str(run.get("task_id", "")),
                str(run.get("repetition", "")),
                str(run.get("arm", "")),
                "; ".join(extract_notes(run)),
            ]
            for run in control_runs + treatment_runs
            if extract_notes(run)
        ]
        if note_rows:
            blocks.append(("h", 2, "Grader notes"))
            blocks.append(
                (
                    "p",
                    "Diagnostics the graders reported under `notes`. They are not scored "
                    "checks and do not enter the By check table.",
                )
            )
            blocks.append(
                ("table", ["Task", "Run", "Arm", "Notes"], note_rows, ["l", "r", "l", "l"])
            )

    blocks.append(("h", 2, "Setup"))
    preset_setup = results.get("preset") or (results.get("settings") or {}).get("preset")
    setup_items = [
        f"**Skill:** `{results.get('skill', '')}`"
        + (
            f" (names: {', '.join(f'`{n}`' for n in results['skill_names'])})"
            if results.get("skill_names")
            else ""
        ),
        f"**Models:** {', '.join(f'`{m}`' for m in models)}",
    ]
    if preset_setup:
        setup_items.append(f"**Preset:** `{preset_setup}`")
    if skill_comparison:
        baseline_txt = (
            "yes (no-skill arm per pair, balanced rotation)"
            if skill_comparison.get("include_baseline")
            else "no"
        )
        if (preset_setup or skill_comparison.get("preset")) == "compression":
            setup_items = [
                f"**Original (control):** `{skill_comparison.get('skill_a')}`",
                f"**Minified (treatment):** `{skill_comparison.get('skill_b')}`",
                f"**Baseline:** {baseline_txt}",
                setup_items[-1],
            ]
        else:
            setup_items = [
                f"**Skill A (control):** `{skill_comparison.get('skill_a')}`",
                f"**Skill B (treatment):** `{skill_comparison.get('skill_b')}`",
                f"**Baseline:** {baseline_txt}",
                setup_items[-1],
            ]
        if skill_comparison.get("source_bytes_a") or skill_comparison.get("source_bytes_b"):
            try:
                ba = int(skill_comparison.get("source_bytes_a") or 0)
                bb = int(skill_comparison.get("source_bytes_b") or 0)
                red = float(skill_comparison.get("source_reduction_pct") or 0)
                setup_items.append(
                    f"**Source size:** {ba} → {bb} bytes ({red:+.1f}% static reduction)"
                )
            except Exception:
                pass
    if comparison:
        pr_mode = comparison.get("mode", "agent")
        pr_pair = comparison.get("pair", "merge-base")
        mode_note = (
            "agent effectiveness (agents work on each revision)"
            if pr_mode == "agent"
            else "PR correctness (graders run on untouched revisions, no agents)"
        )
        pair_note = (
            "merge-base vs head"
            if pr_pair == "merge-base"
            else "base tip vs synthetic merge (integration)"
        )
        setup_items = [
            f"**Repository:** `{comparison['repo']}`",
            f"**Control (without PR):** `{comparison['control_commit']}`",
            f"**Treatment (with PR):** `{comparison['treatment_commit']}`",
            f"**Base ref:** `{comparison['base']}` · **Head ref:** `{comparison['head']}`",
            f"**PR workflow:** {pr_mode} — {mode_note}; revisions: {pair_note}",
            setup_items[-1],
        ]
    for key, value in (results.get("settings") or {}).items():
        if key == "harness":
            continue
        if key == "thresholds" and isinstance(value, dict):
            shown = ", ".join(f"{k}={v}" for k, v in value.items())
            setup_items.append(f"**thresholds:** {shown}")
            continue
        shown = ", ".join(f"`{v}`" for v in value) if isinstance(value, list) else f"`{value}`"
        setup_items.append(f"**{key}:** {shown}")
    if results.get("thresholds") and "thresholds" not in (results.get("settings") or {}):
        th = results["thresholds"]
        setup_items.append("**thresholds:** " + ", ".join(f"{k}={v}" for k, v in th.items()))
    if results.get("failure_policy"):
        fp = results["failure_policy"]
        setup_items.append(
            "**Failure policy:** " + ", ".join(f"{k}={v}" for k, v in fp.items())
        )
    if results.get("seed") is not None:
        setup_items.append(f"**Seed:** `{results.get('seed')}` (balanced arm order)")
    if results.get("valid") is False:
        setup_items.append("**Validity:** INVALID (control contamination — no clean baseline)")
    if results.get("retries"):
        setup_items.append(
            f"**Retries:** {len(results['retries'])} retry record(s) preserved with costs"
        )
    prov = results.get("provenance") or {}
    if prov:
        if prov.get("skill_hash"):
            setup_items.append(f"**Skill hash:** `{prov['skill_hash'][:12]}`")
        if prov.get("agent_cli"):
            cli_txt = ", ".join(f"{k} {v}" for k, v in prov["agent_cli"].items())
            setup_items.append(f"**Agent CLI:** {cli_txt}")
        if prov.get("tasks_hash"):
            setup_items.append(
                f"**Tasks hash (prompts+graders+fixtures+locks):** `{str(prov['tasks_hash'])[:12]}`"
            )
    if results.get("task_categories"):
        cats = results["task_categories"]
        setup_items.append(
            "**Categories:** " + ", ".join(f"`{tid}`={cat}" for tid, cat in sorted(cats.items()))
        )
    if results.get("skilldiff_version"):
        setup_items.append(f"**skilldiff:** {results['skilldiff_version']}")
    blocks.append(("ul", setup_items))

    blocks.append(("h", 2, "How to read this report"))
    blocks.append(
        (
            "ul",
            [
                f"Differences are **{subject} minus control** as paired-mean changes. "
                "Control/Skill columns show means (score) or medians (cost/time/tokens) "
                "from completed agent pairs; the Δ and 95% CI measure the paired-mean "
                "effect. Adoption includes every eligible skill run. Reading states "
                "each row's verdict in plain words and never disagrees with them.",
                "Success compares only pairs with eligible graded scores and known "
                "success outcomes on both sides.",
                "The 95% CI is a bootstrap interval over paired runs. If it includes zero, "
                "the difference could be noise. `n=X/Y` shows valid pairs for that metric.",
                "Unknown values are **N/A** (ungraded tasks, excluded agent failures, "
                "grader timeouts/errors, or missing cost/tokens). Valid-pair counts "
                "show how many pairs contributed. Failed attempts retain their raw "
                "time, tokens, and partial checks in Run details.",
                _cost_basis_note(results),
                "Checks `N/A` means no named checks; `1/2*` means one check had unknown status.",
                *(
                    []
                    if comparison
                    else [
                        "*Skill used* comes from the harness's tool calls (Claude) or from "
                        "references to the skill's files in the transcript (other harnesses)."
                    ]
                ),
            ],
        )
    )

    evaluation_rows = _evaluation_rows(results, control_runs, treatment_runs)
    if evaluation_rows:
        blocks.append(("h", 2, "Evaluation results"))
        blocks.append(("p", _EVALUATION_NOTE))
        blocks.append(("table", _EVALUATION_HEADERS, evaluation_rows, _EVALUATION_ALIGN))

    # Closing decision: the same statistics as the headline (held-out pairs
    # when they exist), then one bottom line. Reports must end with the
    # decision, not with reading notes.
    rec_kind, rec_label, rec_reason = decision["recommendation"]
    blocks.append(("h", 2, "Closing decision"))
    if decision["held_out"]:
        blocks.append(
            (
                "p",
                f"Held-out data only ({held_out_coverage}); all-pairs "
                "figures are in the Summary table above, and dev results are in "
                "By split.",
            )
        )
    blocks.append(
        (
            "table",
            ["Metric", "Control", label, "Paired change", "95% CI", "Reading"],
            _decision_rows(headline_control, headline_skill, headline_paired),
            ["l", "r", "r", "r", "r", "l"],
        )
    )
    blocks.append(("callout", rec_kind, f"Recommendation: {rec_label}", [rec_reason]))
    return f"skilldiff: {name}", blocks


_NUMERIC_RUN_COLUMNS = {"Run", "Score", "Checks", "Cost", "Time", "Turns", "Tokens", "Files"}


def _group_headers(first: str, treatment_label: str = "Skill") -> list[str]:
    return [
        first,
        "Control",
        treatment_label,
        "Δ score (paired mean)",
        "Better/worse/tie",
        "Δ cost (mean)",
        "Δ time (mean)",
        *(["Skill used"] if treatment_label == "Skill" else []),
        "Reading",
    ]


def _check_table_rows(check_rows: list[dict[str, Any]]) -> list[list[Cell]]:
    out: list[list[Cell]] = []
    for r in check_rows:
        c_txt = f"{r['control_pass']}/{r['control_total']}" if r["control_total"] else "N/A"
        s_txt = f"{r['skill_pass']}/{r['skill_total']}" if r["skill_total"] else "N/A"
        d = r.get("diff_pp")
        d_txt: Cell = (format_pp_diff(int(d)), _tone(int(d), True)) if d is not None else "N/A"
        wlt = f"{r['wins']}/{r['losses']}/{r['ties']}" if r.get("pairs") else "-"
        pairs_txt = f"n={r['pairs']}" if r.get("pairs") is not None else ""
        reading = check_reading(r.get("wins", 0), r.get("losses", 0), r.get("pairs", 0))
        out.append([r["task_id"], r["check"], c_txt, s_txt, d_txt, wlt, pairs_txt, reading])
    return out


def _category_summary(
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
    task_categories: dict[str, str],
) -> list[list[Cell]]:
    from skilldiff.stats import paired_comparison as _paired

    cats = sorted(
        {
            task_categories.get(r.get("task_id", ""), "general")
            for r in control_runs + treatment_runs
        }
    )
    rows: list[list[Cell]] = []
    for cat in cats:
        task_ids = {tid for tid, c in task_categories.items() if c == cat}
        # Fall back to runs' embedded category when mapping is incomplete.
        mc = [
            r
            for r in control_runs
            if (r.get("task_id") in task_ids or r.get("task_category", "general") == cat)
        ]
        mt = [
            r
            for r in treatment_runs
            if (r.get("task_id") in task_ids or r.get("task_category", "general") == cat)
        ]
        if not mc and not mt:
            continue
        m_control, m_skill = _paired_comparison_metrics(mc, mt)
        m_paired = _paired(mc, mt)
        score_m = (m_paired.get("score") or {}).get("mean_diff", None)
        score_txt: Cell = (
            (format_pp_diff(round(float(score_m) * 100)), _tone(round(float(score_m) * 100), True))
            if score_m is not None
            else ("N/A", None)
        )
        c_pct, s_pct = _score_percent(m_control), _score_percent(m_skill)
        adoption = _skill_usage(m_skill)
        cost_m = (m_paired.get("cost") or {}).get("mean_diff", None)
        cost_txt: Cell = (
            (format_cost_diff(float(cost_m)), _tone(round(float(cost_m), 2), False))
            if cost_m is not None
            else ("N/A", None)
        )
        n = m_paired.get("pairs", 0)
        scored = (m_paired.get("score") or {}).get("n", n)
        score_metric = (m_paired or {}).get("score") or {}
        score_reading = row_reading(
            "score",
            score_m,
            score_metric.get("ci_low"),
            score_metric.get("ci_high"),
            score_metric.get("n", 0),
            n,
            True,
        )
        used = m_skill.get("skill_used_count", 0)
        known = m_skill.get("skill_known_count", 0)
        rows.append(
            [
                cat,
                f"{len(task_ids) if task_ids else len({r.get('task_id') for r in mc + mt})}",
                f"{c_pct}%" if c_pct is not None else "N/A",
                f"{s_pct}%" if s_pct is not None else "N/A",
                score_txt,
                adoption,
                cost_txt,
                f"{scored}/{n}" if n else "-",
                category_reading(cat, score_reading, used, known, cost_m),
            ]
        )
    return rows


_GROUP_ALIGN = ["l", "r", "r", "r", "r", "r", "r", "r", "l"]


def _group_row(
    label: str,
    control: dict[str, Any],
    skill: dict[str, Any],
    paired: dict[str, Any],
    show_usage: bool = True,
) -> list[Cell]:
    # Paired-mean diffs so group rows match the summary's CIs.
    total = paired.get("pairs", 0) if paired else 0
    score_m = ((paired or {}).get("score") or {}).get("mean_diff", None) if paired else None
    if score_m is not None:
        score_diff: Optional[int] = round(float(score_m) * 100)
    else:
        score_diff = _score_difference(control, skill) if not total else None
    cost_m = ((paired or {}).get("cost") or {}).get("mean_diff", None) if paired else None
    if cost_m is not None:
        cost_diff: Optional[float] = float(cost_m)
    elif control.get("median_cost") is not None and skill.get("median_cost") is not None:
        cost_diff = float(skill["median_cost"]) - float(control["median_cost"])
    else:
        cost_diff = None
    dur_m = ((paired or {}).get("duration") or {}).get("mean_diff", None) if paired else None
    if dur_m is not None:
        time_diff: Optional[float] = float(dur_m)
    elif control.get("median_time") is not None and skill.get("median_time") is not None:
        time_diff = float(skill["median_time"]) - float(control["median_time"])
    else:
        time_diff = None

    scored_n = ((paired or {}).get("score") or {}).get("n", None) if paired else None
    if paired and paired.get("pairs"):
        if scored_n is not None and scored_n < paired.get("pairs", 0):
            wlt = (
                f"{paired.get('wins', 0)}/{paired.get('losses', 0)}/{paired.get('ties', 0)}"
                f" (n={scored_n})"
            )
        else:
            wlt = f"{paired.get('wins', 0)}/{paired.get('losses', 0)}/{paired.get('ties', 0)}"
    else:
        wlt = "-"
    c_pct, s_pct = _score_percent(control), _score_percent(skill)
    score_metric = (paired or {}).get("score") or {}
    if score_m is not None and total:
        score_reading = row_reading(
            "score",
            score_m,
            score_metric.get("ci_low"),
            score_metric.get("ci_high"),
            score_metric.get("n", 0),
            total,
            True,
        )
    elif score_diff is not None:
        score_reading = row_reading("score", score_diff / 100.0, None, None, 0, 0, True)
    else:
        score_reading = "Unknown"
    return [
        label,
        f"{c_pct}%" if c_pct is not None else "N/A",
        f"{s_pct}%" if s_pct is not None else "N/A",
        (format_pp_diff(score_diff), _tone(score_diff, True))
        if score_diff is not None
        else ("N/A", None),
        wlt,
        (format_cost_diff(cost_diff), _tone(round(cost_diff, 2), False))
        if cost_diff is not None
        else ("N/A", None),
        (format_time_diff(time_diff), _tone(round(time_diff), False))
        if time_diff is not None
        else ("N/A", None),
        *([_skill_usage(skill)] if show_usage else []),
        score_reading,
    ]


# ------------------------------------------------------------------------ renderers


def _cell_text(cell: Cell) -> str:
    return cell[0] if isinstance(cell, tuple) else cell


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _md_table(headers: list[str], rows: list[list[Cell]], align: Optional[list[str]]) -> list[str]:
    align = align or ["l"] + ["r"] * (len(headers) - 1)
    if len(align) < len(headers):
        align = align + ["l"] * (len(headers) - len(align))
    sep = ["---:" if a == "r" else ":---" for a in align[: len(headers)]]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(sep) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_markdown_cell(_cell_text(c)) for c in row) + " |")
    return lines


def _render_markdown(title: str, blocks: list[tuple], flavor: str, date: str) -> str:
    lines: list[str] = []
    if flavor == "quarto":
        lines.extend(
            [
                "---",
                f"title: {json.dumps(title)}",
                f"date: {json.dumps(date)}",
                "format:",
                "  html:",
                "    theme: cosmo",
                "    toc: true",
                "    embed-resources: true",
                "---",
                "",
            ]
        )
    else:
        lines.extend([f"# {title}", "", f"_{date}_", ""])

    for block in blocks:
        kind = block[0]
        if kind == "h":
            lines.extend(["#" * block[1] + " " + block[2], ""])
        elif kind == "p":
            lines.extend([block[1], ""])
        elif kind == "ul":
            lines.extend([f"- {item}" for item in block[1]] + [""])
        elif kind == "table":
            lines.extend(_md_table(block[1], block[2], block[3]) + [""])
        elif kind == "callout":
            _, ctype, ctitle, body = block
            bullets = len(body) > 1 and ctype == "warning"
            if flavor == "quarto":
                lines.append(f"::: {{.callout-{ctype}}}")
                lines.append(f"## {ctitle}")
                lines.extend([f"- {b}" for b in body] if bullets else body)
                lines.extend([":::", ""])
            else:
                gh = {"tip": "TIP", "warning": "WARNING", "note": "NOTE"}.get(ctype, "NOTE")
                lines.append(f"> [!{gh}]")
                lines.append(f"> **{ctitle}**")
                for b in body:
                    lines.append(f"> - {b}" if bullets else f"> {b}")
                    if not bullets:
                        lines.append(">")
                if lines[-1] == ">":
                    lines.pop()
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_quarto_report(results: dict[str, Any], run_root: Path | None = None) -> str:
    title, blocks = build_report_blocks(results, run_root)
    return _render_markdown(title, blocks, "quarto", str(results.get("timestamp", "")))


def build_markdown_report(results: dict[str, Any], run_root: Path | None = None) -> str:
    title, blocks = build_report_blocks(results, run_root)
    return _render_markdown(title, blocks, "gfm", str(results.get("timestamp", "")))


_INLINE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\)|_(.+?)_(?!\w)")


def _inline_html(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _INLINE.finditer(text):
        out.append(html.escape(text[pos : m.start()]))
        bold, code, link_text, link_url, italic = m.groups()
        if bold is not None:
            out.append(f"<strong>{_inline_html(bold)}</strong>")
        elif code is not None:
            out.append(f"<code>{html.escape(code)}</code>")
        elif link_text is not None:
            href = html.escape(link_url, quote=True)
            out.append(f'<a href="{href}">{html.escape(link_text)}</a>')
        else:
            out.append(f"<em>{_inline_html(italic)}</em>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def _strip_inline(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text).replace("`", "")


_CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--soft:#f6f8fa;
--good:#1a7f37;--bad:#cf222e;--tip:#1a7f37;--warning:#9a6700;--note:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;
--line:#3d444d;--soft:#151b23;--good:#3fb950;--bad:#f85149;--tip:#3fb950;
--warning:#d29922;--note:#4493f8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:1100px;margin:0 auto;padding:32px 24px 64px}
h1{font-size:28px;margin:0 0 4px}h2{font-size:20px;margin:36px 0 12px;
padding-bottom:6px;border-bottom:1px solid var(--line)}
.date{color:var(--muted);margin:0 0 24px}
.callout{border-left:4px solid var(--note);background:var(--soft);padding:12px 16px;
margin:16px 0;border-radius:6px}
.callout.tip{border-color:var(--tip)}.callout.warning{border-color:var(--warning)}
.callout-title{font-weight:600;margin:0 0 6px}.callout p{margin:4px 0}
.callout ul{margin:4px 0;padding-left:20px}
.table-wrap{overflow-x:auto;margin:8px 0 16px}
table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{text-align:left;color:var(--muted);font-weight:600;background:var(--soft)}
td.r,th.r{text-align:right}.good{color:var(--good);font-weight:600}
.bad{color:var(--bad);font-weight:600}
code{font:13px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--soft);
padding:1px 5px;border-radius:4px}
a{color:var(--note)}li{margin:4px 0}
"""


def build_html_report(results: dict[str, Any], run_root: Path | None = None) -> str:
    title, blocks = build_report_blocks(results, run_root)
    return _render_html(title, blocks, str(results.get("timestamp", "")))


def _render_html(title: str, blocks: list[tuple], date: str) -> str:
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><main>",
        f"<h1>{html.escape(title)}</h1>",
        f'<p class="date">{html.escape(date)}</p>',
    ]
    for block in blocks:
        kind = block[0]
        if kind == "h":
            parts.append(f"<h{block[1]}>{_inline_html(block[2])}</h{block[1]}>")
        elif kind == "p":
            parts.append(f"<p>{_inline_html(block[1])}</p>")
        elif kind == "ul":
            items = "".join(f"<li>{_inline_html(i)}</li>" for i in block[1])
            parts.append(f"<ul>{items}</ul>")
        elif kind == "table":
            headers, rows, align = block[1], block[2], block[3]
            align = align or ["l"] + ["r"] * (len(headers) - 1)
            align = align + ["l"] * (len(headers) - len(align))
            head = "".join(f'<th class="{a}">{html.escape(h)}</th>' for h, a in zip(headers, align))
            body_rows = []
            for row in rows:
                cells = []
                for cell, a in zip(row, align):
                    text, tone = cell if isinstance(cell, tuple) else (cell, None)
                    cls = " ".join(c for c in (a, tone or "") if c)
                    cells.append(f'<td class="{cls}">{_inline_html(text)}</td>')
                body_rows.append("<tr>" + "".join(cells) + "</tr>")
            parts.append(
                f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
                f"<tbody>{''.join(body_rows)}</tbody></table></div>"
            )
        elif kind == "callout":
            _, ctype, ctitle, body = block
            if len(body) > 1 and ctype == "warning":
                inner = "<ul>" + "".join(f"<li>{_inline_html(b)}</li>" for b in body) + "</ul>"
            else:
                inner = "".join(f"<p>{_inline_html(b)}</p>" for b in body)
            parts.append(
                f'<div class="callout {ctype}"><p class="callout-title">'
                f"{html.escape(ctitle)}</p>{inner}</div>"
            )
    parts.append("</main></body></html>")
    return "\n".join(parts)


def create_reports(results: dict[str, Any], run_root: Path) -> dict[str, Path]:
    """Write report.html, report.md, and report.qmd into run_root."""
    title, blocks = build_report_blocks(results, run_root)
    date = str(results.get("timestamp", ""))
    reports = {
        "html": ("report.html", _render_html(title, blocks, date)),
        "md": ("report.md", _render_markdown(title, blocks, "gfm", date)),
        "qmd": ("report.qmd", _render_markdown(title, blocks, "quarto", date)),
    }
    paths: dict[str, Path] = {}
    for key, (filename, content) in reports.items():
        paths[key] = run_root / filename
        atomic_write(paths[key], content)
    return paths


def create_quarto_report(results: dict[str, Any], run_root: Path) -> tuple[Path, Path | None]:
    """Backward-compatible wrapper around create_reports."""
    paths = create_reports(results, run_root)
    return paths["qmd"], paths["html"]

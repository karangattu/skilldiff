import math
import statistics
from typing import Any, Optional

from skilldiff.stats import pair_runs, usable_agent_run


def _total_tokens(run: dict[str, Any]) -> int:
    if run.get("reported_total_tokens") is not None:
        return int(run["reported_total_tokens"])
    return (
        int(run.get("input_tokens", 0) or 0)
        + int(run.get("cache_read_tokens", 0) or 0)
        + int(run.get("cache_creation_tokens", 0) or 0)
        + int(run.get("output_tokens", 0) or 0)
    )


def _total_tokens_opt(run: dict[str, Any]) -> Optional[int]:
    if run.get("reported_total_tokens") is not None:
        try:
            total = int(run["reported_total_tokens"])
            return total if total >= 0 else None
        except (TypeError, ValueError):
            return None
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


def _fmt_int_opt(val: Optional[int]) -> str:
    return "N/A" if val is None else f"{val:,}"


def _fmt_int_delta(val: int) -> str:
    return "0" if val == 0 else f"{val:+,}"


def _paired_mean(paired: dict[str, Any], key: str) -> Optional[float]:
    m = (paired or {}).get(key) or {}
    v = m.get("mean_diff", None)
    return None if v is None else float(v)


def _skill_usage(metrics: dict[str, Any]) -> str:
    known = metrics.get("skill_known_count", 0)
    if not known:
        return "unknown"
    return f"{metrics.get('skill_used_count', 0)}/{known}"


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

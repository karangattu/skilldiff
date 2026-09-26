"""Metrics and reports for skilldiff experiments.

A report is built once as a list of simple blocks and rendered to three formats:
`report.html` (self-contained, no dependencies), `report.md` (renders on GitHub, handy
for pull requests), and `report.qmd` (for customizing with Quarto).
"""

import html
import json
import re
import statistics
from pathlib import Path
from typing import Any, Optional, Union

from skilldiff.stats import classify_effect, paired_comparison

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
    try:
        return int(sum(int(run.get(k) or 0) for k in keys))
    except (TypeError, ValueError):
        return None


def _known_score(run: dict[str, Any]) -> Optional[float]:
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
            "cost_known_count": 0,
            "time_known_count": 0,
            "tokens_known_count": 0,
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

    scores = [s for r in runs_data if (s := _known_score(r)) is not None]
    costs = [c for r in runs_data if (c := _known_float(r, "cost")) is not None]
    times = [t for r in runs_data if (t := _known_float(r, "duration")) is not None]
    tok_opts = [_total_tokens_opt(r) for r in runs_data]
    tokens = [t for t in tok_opts if t is not None]
    turns = [
        int(r["num_turns"])
        for r in runs_data
        if "num_turns" in r and r.get("num_turns") is not None
    ]
    known = [r for r in runs_data if r.get("skill_invoked") is not None]
    grade_counts: dict[str, int] = {}
    for r in runs_data:
        gs = str(r.get("grade_status") or ("graded" if "score" in r else "unknown"))
        grade_counts[gs] = grade_counts.get(gs, 0) + 1

    return {
        "task_score": statistics.mean(scores) if scores else None,
        "success_count": sum(1 for r in runs_data if r.get("success") is True),
        "total_count": len(runs_data),
        "graded_count": len(scores),
        "score_known_count": len(scores),
        "median_cost": statistics.median(costs) if costs else None,
        "median_time": statistics.median(times) if times else None,
        "median_tokens": statistics.median(tokens) if tokens else None,
        "median_turns": statistics.median(turns) if turns else None,
        "cost_known_count": len(costs),
        "time_known_count": len(times),
        "tokens_known_count": len(tokens),
        "total_duration": sum(times) if times else None,
        "total_cost": round(sum(costs), 4) if costs else None,
        "total_input_tokens": sum(int(r.get("input_tokens", 0) or 0) for r in runs_data),
        "total_output_tokens": sum(int(r.get("output_tokens", 0) or 0) for r in runs_data),
        "total_cache_read_tokens": sum(int(r.get("cache_read_tokens", 0) or 0) for r in runs_data),
        "total_cache_creation_tokens": sum(
            int(r.get("cache_creation_tokens", 0) or 0) for r in runs_data
        ),
        "total_tokens": sum(tokens) if tokens else None,
        "total_tool_calls": sum(int(r.get("tool_calls", 0) or 0) for r in runs_data),
        "skill_used_count": sum(1 for r in known if r.get("skill_invoked")),
        "skill_known_count": len(known),
        "error_count": sum(1 for r in runs_data if r.get("status") not in (None, "ok")),
        "grade_status_counts": grade_counts,
    }


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
    prov = results.get("provenance") or {}
    version = prov.get("pricing_version") or "unversioned"
    date = prov.get("pricing_date") or "unknown date"
    settings = results.get("settings") or {}
    basis = settings.get("cost_basis", "")
    if "token-pricing" in str(basis):
        return (
            "Tokens include cached input where the harness reports it. Cost is the "
            f"API-equivalent price recomputed from token counts (pricing {version}, "
            f"{date}); actual subscription spend is $0 at the margin. "
            "Refresh with `skilldiff prices`."
        )
    return (
        "Tokens include cached input where the harness reports it. Cost is the "
        "harness-reported price (API billing)."
    )


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

    c_succ = f"{control_metrics.get('success_count', 0)}/{control_metrics.get('total_count', 0)}"
    s_succ = f"{skill_metrics.get('success_count', 0)}/{skill_metrics.get('total_count', 0)}"
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

    def row(label: str, c: str, s: str, d: str) -> str:
        return f"{label:<18} {c:>8} {s:>10} {d:>16}"

    lines = [
        title,
        "",
        f"{'Metric':<18} {'Control':>8} {treatment_label:>10} {'Paired mean Δ':>16}",
        row("Task score", c_score_txt, s_score_txt, diff_score_txt),
        row("Success", c_succ, s_succ, format_count_diff(diff_succ)),
        row("Cost (median)", c_cost, s_cost, diff_cost_txt),
        row("Time (median)", c_time, s_time, diff_time_txt),
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
        lines.append(row("Tokens (median)", c_tok, s_tok, d_tok))
    if skill_metrics.get("skill_known_count"):
        lines.append(row("Skill used", "-", _skill_usage(skill_metrics), ""))
    lines.extend(
        ["", f"Models: {models_count}    Tasks: {tasks_count}    Runs per arm: {runs_per_arm}"]
    )
    if paired and paired.get("pairs"):
        verdict, _ = _verdict(paired, treatment_label.lower(), thresholds=thresholds)
        lines.append(_strip_inline(verdict))
    return "\n".join(lines)


# ------------------------------------------------------------------- report blocks

Cell = Union[str, tuple[str, Optional[str]]]  # text, or (text, tone) with tone good/bad


def _tone(value: float, higher_is_better: bool, eps: float = 1e-9) -> Optional[str]:
    if abs(value) <= eps:
        return None
    return "good" if (value > 0) == higher_is_better else "bad"


def _verdict(
    paired: dict[str, Any], subject: str = "skill", thresholds: dict[str, Any] | None = None
) -> tuple[str, str]:
    """Return (sentence, callout kind) for the task-score effect.

    Flags tiny samples and collapsed intervals in the headline so a
    +100pp win on 2 identical pairs is not mistaken for conclusive evidence.
    When practical thresholds are configured, adds a decision-oriented
    assessment (useful improvement vs merely detectable change).
    """
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
        return (
            f"No graded task-score pairs in {total} pair(s). "
            "Scores are N/A (ungraded tasks or grader failures); "
            "only cost/time can be compared.",
            "note",
        )

    mean_pp = round(float(mean_raw) * 100)

    # Cautions flagged directly in the headline.
    cautions: list[str] = []
    if n_valid < 5:
        cautions.append(f"only {n_valid} pair(s) — treat as preliminary")
    if lo is not None and hi is not None and lo == hi:
        cautions.append("CI collapsed (identical differences) — uncertainty is underestimated")
    caution_txt = f" ({'; '.join(cautions)})" if cautions else ""

    practical_txt = ""
    if thresholds:
        practical_txt = " " + _practical_assessment(paired, thresholds, mean_pp)

    if n_valid < 2:
        if mean_pp == 0:
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
        return (
            f"No task-score difference: both arms scored the same in all {pairs_txt}.{caution_txt}",
            "note",
        )
    return (
        f"No clear task-score effect: **{format_pp_diff(mean_pp)}**, but the 95% CI "
        f"({ci}) includes zero ({pairs_txt}){caution_txt}.{practical_txt}".replace("..", "."),
        "note",
    )


def _practical_assessment(paired: dict[str, Any], thresholds: dict[str, Any], mean_pp: int) -> str:
    """Decision-oriented sentence from practical thresholds.

    Threshold keys (all optional):
      acceptable_score_regression_pp: score drop tolerated (e.g. 5 means -5pp ok).
      required_cost_reduction_pct: cost saving required (e.g. 10 means 10% cheaper).
      meaningful_score_gain_pp: gain needed to call an improvement useful.
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
        meaningful_gain = float(thresholds.get("meaningful_score_gain_pp", 0))
    except (TypeError, ValueError):
        meaningful_gain = 0.0

    cost_metric = (paired or {}).get("cost") or {}
    rel = cost_metric.get("relative_change", None)
    saving_pct: Optional[float] = None
    if rel is not None:
        try:
            saving_pct = -float(rel) * 100
        except (TypeError, ValueError):
            saving_pct = None

    score_ok = mean_pp >= -allowed_loss
    gain_ok = mean_pp >= meaningful_gain if meaningful_gain else mean_pp > 0
    cost_ok = True
    if required_saving and saving_pct is None:
        cost_ok = False  # required saving but no cost data
    elif required_saving and saving_pct is not None:
        cost_ok = saving_pct >= required_saving

    if not score_ok:
        return (
            f"Practical check: {mean_pp:+d} pp exceeds the allowed regression "
            f"(-{allowed_loss:g} pp)."
        )
    if required_saving and saving_pct is None:
        return "Practical check: cost data missing, cannot verify required saving."
    if required_saving and saving_pct is not None and not cost_ok:
        return (
            f"Practical check: cost saving {saving_pct:+.0f}% is below required "
            f"{required_saving:g}%."
        )
    if gain_ok and cost_ok:
        if required_saving and saving_pct is not None:
            return f"Practical check: meets criteria ({mean_pp:+d} pp, cost {saving_pct:+.0f}%)."
        if meaningful_gain:
            return f"Practical check: meets +{meaningful_gain:g} pp gain criterion."
        return "Practical check: meets criteria."
    return "Practical check: detectable but not practically meaningful yet."


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
    return f"With the {subject}, runs {joined}, summed over all pairs."


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
    else:  # turns
        txt = f"{mean_diff:+.1f}" if abs(mean_diff) < 10 else f"{int(round(mean_diff)):+d}"
        tone = _tone(mean_diff, False)
    return txt, tone, _fmt_ci(metric, kind, total_pairs=total)


def _metric_rows(
    control: dict[str, Any], skill: dict[str, Any], paired: dict[str, Any]
) -> list[list[Cell]]:
    total_pairs = paired.get("pairs", 0)
    # Fallbacks for summary-only results (no paired data).
    c_pct, s_pct = _score_percent(control), _score_percent(skill)
    score_fallback = None
    if total_pairs == 0 and c_pct is not None and s_pct is not None:
        score_fallback = (s_pct - c_pct) / 100.0
    cost_fallback = None
    if (
        total_pairs == 0
        and control.get("median_cost") is not None
        and skill.get("median_cost") is not None
    ):
        cost_fallback = float(skill["median_cost"]) - float(control["median_cost"])
    time_fallback = None
    if (
        total_pairs == 0
        and control.get("median_time") is not None
        and skill.get("median_time") is not None
    ):
        time_fallback = float(skill["median_time"]) - float(control["median_time"])
    tok_fallback = None
    if (
        total_pairs == 0
        and control.get("median_tokens") is not None
        and skill.get("median_tokens") is not None
    ):
        tok_fallback = float(skill["median_tokens"]) - float(control["median_tokens"])
    turn_fallback = None
    if (
        total_pairs == 0
        and control.get("median_turns") is not None
        and skill.get("median_turns") is not None
    ):
        turn_fallback = float(skill["median_turns"]) - float(control["median_turns"])

    score_txt, score_tone, score_ci = _paired_diff_text(
        paired, "score", "score", fallback=score_fallback, higher_is_better=True
    )
    cost_txt, cost_tone, cost_ci = _paired_diff_text(paired, "cost", "cost", fallback=cost_fallback)
    time_txt, time_tone, time_ci = _paired_diff_text(
        paired, "duration", "duration", fallback=time_fallback
    )
    tok_txt, tok_tone, tok_ci = _paired_diff_text(paired, "tokens", "tokens", fallback=tok_fallback)
    turn_txt, turn_tone, turn_ci = _paired_diff_text(
        paired, "turns", "turns", fallback=turn_fallback
    )

    success_diff = skill.get("success_count", 0) - control.get("success_count", 0)
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
        ],
        [
            "Success",
            f"{control.get('success_count', 0)}/{control.get('total_count', 0)}",
            f"{skill.get('success_count', 0)}/{skill.get('total_count', 0)}",
            (format_count_diff(success_diff), _tone(success_diff, True)),
            "",
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
            ]
        )
    rows.append(
        [
            "Time (median)",
            _fmt_time_opt(control.get("median_time")),
            _fmt_time_opt(skill.get("median_time")),
            (time_txt, time_tone),
            time_ci,
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
            ]
        )
    if (
        control.get("median_turns") is not None
        or skill.get("median_turns") is not None
        or ((paired or {}).get("turns") or {}).get("n")
    ):
        c_turn = (
            f"{control['median_turns']:g}" if control.get("median_turns") is not None else "N/A"
        )
        s_turn = f"{skill['median_turns']:g}" if skill.get("median_turns") is not None else "N/A"
        rows.append(["Turns (median)", c_turn, s_turn, (turn_txt, turn_tone), turn_ci])
    if skill.get("skill_known_count") or control.get("skill_known_count"):
        rows.append(["Skill used", _skill_usage(control), _skill_usage(skill), "", ""])
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
            ]
        )
    return rows


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


def _format_checks_passed(run: dict[str, Any]) -> str:
    checks = extract_checks(run)
    if checks:
        known = [(n, p) for n, p in checks if p is not None]
        if not known:
            return "N/A"
        passed = sum(1 for _, p in known if p)
        if len(known) < len(checks):
            return f"{passed}/{len(checks)}*"
        return f"{passed}/{len(checks)}"
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
            return {"control": list(c), "treatment": list(t)}

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
            return {"control": c_runs, "treatment": t_runs}

    return {"control": [], "treatment": []}


def _run_status(run: dict[str, Any]) -> Cell:
    status = str(run.get("status") or "ok")
    return ("ok", None) if status == "ok" else (status, "bad")


def _score_cell(run: dict[str, Any]) -> str:
    gs = run.get("grade_status")
    if gs in ("ungraded", "timeout", "error"):
        label = {"ungraded": "ungraded", "timeout": "grader timeout", "error": "grader error"}[gs]
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
    control = calculate_metrics(control_runs)
    skill = calculate_metrics(treatment_runs)
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
                "(ungraded tasks or grader failures). Only cost/time can be compared."
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

    if skill.get("skill_known_count"):
        used, known = skill["skill_used_count"], skill["skill_known_count"]
        text = f"**Adoption.** The agent used the skill in {used} of {known} skill runs"
        c_used = control.get("skill_used_count", 0)
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
    if task_effects and all(abs(e) < 1e-9 for e, _ in task_effects) and (c_pct_all or 0) >= 95:
        bullets.append(
            "**Ceiling effect.** Control already solves these tasks, so accuracy can't "
            f"improve. Add harder tasks the {subject} is designed for, such as obscure APIs, "
            "recent changes, or house conventions."
        )

    runs_per_arm = int(results.get("runs_per_arm") or 1)
    if n and (runs_per_arm < 5 or n < 5):
        bullets.append(
            f"**Sample size.** {n} pair(s) with {runs_per_arm} repetition(s) per task "
            "gives wide error bars. Use `runs: 5` or more before treating the result "
            "as conclusive."
        )
    # Grading validity note (infra vs agent failures).
    grade_issues = 0
    for r in control_runs + treatment_runs:
        if r.get("grade_status") in ("timeout", "error"):
            grade_issues += 1
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
    label = "Treatment" if comparison else "Skill"
    subject = label.lower()
    name = str(results.get("name", "experiment"))
    runs_data = _load_runs_for_report(results, run_root)
    control_runs = runs_data["control"]
    treatment_runs = runs_data["treatment"]

    if control_runs or treatment_runs:
        control = calculate_metrics(control_runs)
        skill = calculate_metrics(treatment_runs)
        paired = paired_comparison(control_runs, treatment_runs)
    else:  # Summary-only results (older versions or hand-built input).
        overall = results.get("overall", {})
        control = {**calculate_metrics([]), **overall.get("control", {})}
        skill = {**calculate_metrics([]), **overall.get("skill", {})}
        paired = overall.get("paired") or {}

    blocks: list[tuple] = []
    thresholds = results.get("thresholds") or (results.get("settings") or {}).get("thresholds")

    if paired.get("pairs"):
        verdict, kind = _verdict(paired, subject, thresholds=thresholds)
    else:
        diff = _score_difference(control, skill)
        if diff is not None and diff > 0:
            verdict, kind = f"{label} improved task score by **{diff} percentage points**.", "tip"
        elif diff is not None and diff < 0:
            verdict = f"{label} reduced task score by **{abs(diff)} percentage points**."
            kind = "warning"
        elif diff is None:
            verdict, kind = "No graded scores; task-score change is N/A.", "note"
        else:
            verdict, kind = f"No measured task-score change between control and {subject}.", "note"
    body = [verdict]
    efficiency = _efficiency_sentence(paired, subject) if paired.get("pairs") else None
    if efficiency:
        body.append(efficiency)
    blocks.append(("callout", kind, "Verdict", body))

    warnings = list(results.get("warnings") or [])
    if warnings:
        blocks.append(("callout", "warning", "Check before trusting this result", warnings))

    models = results.get("models", [])
    blocks.append(("h", 2, "Summary"))
    blocks.append(
        (
            "table",
            ["Metric", "Control", label, "Paired mean Δ", "95% CI"],
            _metric_rows(control, skill, paired),
            ["l", "r", "r", "r", "r"],
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

    if control_runs:
        takeaways = _key_takeaways(results, control_runs, treatment_runs)
        if takeaways:
            blocks.append(("h", 2, "Key takeaways"))
            blocks.append(("ul", takeaways))

    by_model = results.get("by_model", {})
    group_align = _GROUP_ALIGN if not comparison else _GROUP_ALIGN[:-1]
    if len(models) > 1:
        blocks.append(("h", 2, "By model"))
        rows: list[list[Cell]] = []
        for model in models:
            if control_runs:
                mc = [r for r in control_runs if r.get("model") == model]
                mt = [r for r in treatment_runs if r.get("model") == model]
                m_control, m_skill = calculate_metrics(mc), calculate_metrics(mt)
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
                t_control, t_skill = calculate_metrics(tc), calculate_metrics(tt)
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
                    ["Task", "Check", "Control", label, "Δ", "Better/worse/tie", "Pairs"],
                    _check_table_rows(check_data),
                    ["l", "l", "r", "r", "r", "r", "r"],
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
                    ],
                    cat_rows,
                    ["l", "r", "r", "r", "r", "r", "r", "r"],
                )
            )

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
        blocks.append(
            (
                "p",
                (
                    "Each pair ran in fresh workspaces from the recorded revisions"
                    if comparison
                    else "Each pair ran in identical fresh workspaces"
                )
                + (", in random order" if randomized else "")
                + (
                    ". Control excludes the PR; treatment includes it."
                    if comparison
                    else ". Only the skill arm had the skill installed."
                ),
            )
        )
        align = ["r" if h in _NUMERIC_RUN_COLUMNS else "l" for h in headers]
        blocks.append(("table", headers, run_rows, align))

    blocks.append(("h", 2, "Setup"))
    setup_items = [
        f"**Skill:** `{results.get('skill', '')}`"
        + (
            f" (names: {', '.join(f'`{n}`' for n in results['skill_names'])})"
            if results.get("skill_names")
            else ""
        ),
        f"**Models:** {', '.join(f'`{m}`' for m in models)}",
    ]
    if comparison:
        setup_items = [
            f"**Repository:** `{comparison['repo']}`",
            f"**Control (without PR):** `{comparison['control_commit']}` (merge base)",
            f"**Treatment (with PR):** `{comparison['treatment_commit']}`",
            f"**Base ref:** `{comparison['base']}` · **Head ref:** `{comparison['head']}`",
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
    prov = results.get("provenance") or {}
    if prov:
        if prov.get("pricing_version"):
            setup_items.append(
                f"**Pricing:** `{prov['pricing_version']}` ({prov.get('pricing_date', '?')})"
            )
        if prov.get("skill_hash"):
            setup_items.append(f"**Skill hash:** `{prov['skill_hash'][:12]}`")
        if prov.get("agent_cli"):
            cli_txt = ", ".join(f"{k} {v}" for k, v in prov["agent_cli"].items())
            setup_items.append(f"**Agent CLI:** {cli_txt}")
        if prov.get("tasks_hash"):
            setup_items.append(f"**Tasks hash:** `{str(prov['tasks_hash'])[:12]}`")
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
                "as descriptive statistics; the Δ and 95% CI measure the same "
                "paired-mean effect.",
                "The 95% CI is a bootstrap interval over paired runs. If it includes zero, "
                "the difference could be noise. `n=X/Y` shows valid pairs for that metric.",
                "Unknown values are **N/A** (ungraded tasks, grader timeouts/errors, or "
                "missing cost/tokens). Valid-pair counts show how many pairs contributed.",
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
        out.append([r["task_id"], r["check"], c_txt, s_txt, d_txt, wlt, pairs_txt])
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
        m_control, m_skill = calculate_metrics(mc), calculate_metrics(mt)
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
            ]
        )
    return rows


_GROUP_ALIGN = ["l", "r", "r", "r", "r", "r", "r", "r"]


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
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><main>",
        f"<h1>{html.escape(title)}</h1>",
        f'<p class="date">{html.escape(str(results.get("timestamp", "")))}</p>',
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
    builders = {
        "html": ("report.html", build_html_report),
        "md": ("report.md", build_markdown_report),
        "qmd": ("report.qmd", build_quarto_report),
    }
    paths: dict[str, Path] = {}
    for key, (filename, build) in builders.items():
        paths[key] = run_root / filename
        paths[key].write_text(build(results, run_root), encoding="utf-8")
    return paths


def create_quarto_report(results: dict[str, Any], run_root: Path) -> tuple[Path, Path | None]:
    """Backward-compatible wrapper around create_reports."""
    paths = create_reports(results, run_root)
    return paths["qmd"], paths["html"]

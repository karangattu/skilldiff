import math
from pathlib import Path
from typing import Any, Optional

from skilldiff.reporting.evaluation import _load_runs_for_report
from skilldiff.reporting.metrics import (
    _cost_basis_note,
    _fmt_ci,
    _fmt_cost_opt,
    _fmt_count_delta,
    _fmt_count_opt,
    _fmt_score_pct,
    _fmt_time_opt,
    _fmt_tokens_opt,
    _paired_comparison_metrics,
    _score_percent,
    _signed_tokens,
    _skill_usage,
    calculate_metrics,
    decision_cost_runs,
    format_cost_diff,
    format_count_diff,
    format_pp_diff,
    format_time_diff,
)
from skilldiff.reporting.table import _tone
from skilldiff.reporting.types import Cell
from skilldiff.stats import analysis_run, classify_effect, paired_comparison, task_level_effects

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

_RECOMMENDATION_KIND = {"SHIP": "tip", "DO NOT SHIP": "warning", "NEEDS MORE RUNS": "note"}

_HELD_OUT_ALIASES = {"held-out", "heldout", "held_out", "held out", "holdout"}


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
    if mean_diff is None:
        return "Unknown"
    try:
        diff = float(mean_diff)
    except (TypeError, ValueError):
        return "Unknown"
    if total == 0:
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


def _verdict(
    paired: dict[str, Any],
    subject: str = "skill",
    thresholds: dict[str, Any] | None = None,
    tasks_count: int | None = None,
    failure_policy: dict[str, Any] | None = None,
    preset: str | None = None,
    control_score: float | None = None,
    treatment_score: float | None = None,
    control_denials: int = 0,
    treatment_denials: int = 0,
) -> tuple[str, str]:
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
    asymmetric_denials = bool(control_denials or treatment_denials) and (
        control_denials != treatment_denials
    )
    if control_denials or treatment_denials:
        if control_denials != treatment_denials:
            cautions.append(
                f"denials: control {control_denials}, treatment {treatment_denials} (confound)"
            )
        else:
            cautions.append(f"denials: control {control_denials}, treatment {treatment_denials}")
    caution_txt = f" ({'; '.join(cautions)})" if cautions else ""

    def _out(msg: str, k: str) -> tuple[str, str]:
        return msg, ("warning" if asymmetric_denials else k)

    practical_txt = ""
    if thresholds:
        practical_txt = " " + _practical_assessment(paired, thresholds, mean_pp, preset=preset)

    if n_valid < 2:
        if mean_pp == 0:
            if control_score is not None and control_score >= 0.95:
                return _out(
                    f"Tasks at ceiling: control already scores {round(control_score * 100)}%; "
                    f"tasks cannot discriminate.{caution_txt}",
                    "note",
                )
            return _out(
                f"No task-score difference in {pairs_txt}. Add repetitions.{caution_txt}", "note"
            )
        return _out(
            f"Task score changed by **{format_pp_diff(mean_pp)}** in {pairs_txt}. "
            f"One pair can't separate a real effect from noise.{caution_txt}",
            "note",
        )
    if effect == "better":
        return _out(
            f"The {subject} improved task score by **{format_pp_diff(mean_pp)}** "
            f"(95% CI {ci}, {pairs_txt}){caution_txt}.{practical_txt}".replace("..", "."),
            "tip",
        )
    if effect == "worse":
        return _out(
            f"The {subject} reduced task score by **{abs(mean_pp)} pp** "
            f"(95% CI {ci}, {pairs_txt}){caution_txt}.{practical_txt}".replace("..", "."),
            "warning",
        )
    if mean_pp == 0 and lo == hi == 0:
        if control_score is not None and control_score >= 0.95:
            return _out(
                f"Tasks at ceiling: control already scores {round(control_score * 100)}%; "
                f"tasks cannot discriminate.{caution_txt}",
                "note",
            )
        if (
            control_score is not None and control_score <= 0.05
            and (treatment_score is None or treatment_score <= 0.05)
        ):
            return _out(
                f"Tasks at floor: both arms score ~0%; tasks cannot discriminate.{caution_txt}",
                "note",
            )
        if thresholds:
            if preset == "compression":
                return _out(
                    f"Quality preserved: both arms scored the same in all {pairs_txt} "
                    f"(95% CI {ci}){caution_txt}.{practical_txt}".replace("..", "."),
                    "tip" if "meets compression criteria" in practical_txt else "note",
                )
            return _out(
                f"No task-score difference: both arms scored the same in all {pairs_txt} "
                f"{caution_txt}.{practical_txt}".replace("..", "."),
                "note",
            )
        return _out(
            f"No task-score difference: both arms scored the same in all {pairs_txt} "
            f"(95% CI 0 to 0 pp){caution_txt}. Add tasks or repetitions.",
            "note",
        )
    return _out(
        f"No clear task-score effect: {format_pp_diff(mean_pp)} (95% CI {ci}, {pairs_txt})"
        f"{caution_txt}. Confidence interval includes zero.{practical_txt}".replace("..", "."),
        "note",
    )


def _practical_assessment(
    paired: dict[str, Any],
    thresholds: dict[str, Any],
    mean_pp: int,
    preset: str | None = None,
) -> str:
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

    if ci_low_pp is None:
        return "Practical check: score interval missing, cannot verify required quality."
    score_ok = ci_low_pp >= -allowed_loss
    gain_ok = (ci_low_pp >= meaningful_gain) if meaningful_gain else (ci_low_pp > 0)
    if not score_ok:
        return (
            f"Practical check: {ci_low_pp:+.0f} pp lower bound does not clear the allowed "
            f"regression (-{allowed_loss:g} pp). Do not ship on point estimates."
        )

    savings: dict[str, float] = {}
    for key, required in (("cost", required_saving), ("tokens", required_tokens)):
        if not required:
            continue
        metric = (paired or {}).get(key) or {}
        relative = metric.get("relative_change")
        if relative is None:
            return f"Practical check: {key} data missing, cannot verify required saving."
        try:
            savings[key] = -float(relative) * 100
            count = int(metric.get("n", 0))
            upper = metric.get("relative_ci_high")
            upper = float(upper) if upper is not None else None
        except (TypeError, ValueError):
            return f"Practical check: {key} data invalid, cannot verify required saving."
        if not math.isfinite(savings[key]) or (upper is not None and not math.isfinite(upper)):
            return f"Practical check: {key} data invalid, cannot verify required saving."
        if count < 5 or upper is None:
            return (
                f"Practical check: insufficient {key} evidence ({count} usable pairs); "
                "need at least 5 pairs and a percentage interval to verify required saving."
            )
        lower_saving = -float(upper) * 100
        if lower_saving < required and not math.isclose(
            lower_saving, required, rel_tol=1e-12, abs_tol=1e-12
        ):
            if savings[key] < required:
                return (
                    f"Practical check: {key} saving {savings[key]:+.0f}% is below required "
                    f"{required:g}%."
                )
            return (
                f"Practical check: {key} saving averages {savings[key]:+.0f}%, but its "
                f"95% lower bound ({lower_saving:+.0f}%) does not establish the required "
                f"{required:g}% saving."
            )
    saving_pct = savings.get("cost")
    token_pct = savings.get("tokens")
    if preset == "compression" and (required_saving or required_tokens):
        parts = [f"{mean_pp:+d} pp"]
        if required_saving and saving_pct is not None:
            parts.append(f"cost {saving_pct:+.0f}%")
        if required_tokens and token_pct is not None:
            parts.append(f"tokens {token_pct:+.0f}%")
        return (
            "Practical check: meets compression criteria — quality preserved "
            f"({', '.join(parts)}; bounds clear)."
        )
    if gain_ok:
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
    total = paired.get("pairs", 0)
    metric = (paired or {}).get(key) or {}
    mean_diff = metric.get("mean_diff", None)
    if mean_diff is None and fallback is not None and not total:
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
    cost_n = ((paired or {}).get("cost") or {}).get("n", 0)
    if not has_cost and not cost_n:
        show_cost = False
    else:
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
    adoption_failures: int = 0,
) -> str:
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
    if adoption_failures:
        gaps.append(f"{adoption_failures} adoption failure(s)")
    if usable < completed:
        gaps.append(f"{completed - usable} pair(s) ungraded")
    return "Complete" if not gaps else "Partial — " + ", ".join(gaps)


def _run_split(run: dict[str, Any], task_splits: dict[str, str]) -> str:
    split = str(run.get("task_split") or task_splits.get(str(run.get("task_id", ""))) or "")
    return "held-out" if split.strip().lower() in _HELD_OUT_ALIASES else "dev"


def build_decision_context(
    results: dict[str, Any],
    control_runs: list[dict[str, Any]] | None = None,
    treatment_runs: list[dict[str, Any]] | None = None,
    run_root: Path | None = None,
    treatment_label: str = "Skill",
) -> dict[str, Any]:
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
    has_held_out = bool(held_control or held_treatment) or any(
        split.strip().lower() in _HELD_OUT_ALIASES for split in splits.values()
    )
    held = paired_comparison(held_control, held_treatment) if has_held_out else {}
    if control_runs or treatment_runs:
        control, skill = _paired_comparison_metrics(control_runs, treatment_runs)
        paired = paired_comparison(control_runs, treatment_runs)
        selected_control, selected_treatment = control_runs, treatment_runs
        if has_held_out:
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
        tasks_count = None
        if has_held_out:
            selected_paired = held
            selected_metrics = _paired_comparison_metrics(held_control, held_treatment)
            tasks_count = 0
    pairs = int(held.get("pairs", 0))
    scored = int((held.get("score") or {}).get("n", 0))
    coverage = f"{scored}/{pairs} usable score pair(s)"
    note = ""
    if has_held_out:
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
        paired_held_out=held if has_held_out else None,
        control_score=selected_metrics[0].get("task_score"),
        treatment_score=selected_metrics[1].get("task_score"),
    )
    control_denials = sum(1 for r in (control_runs or []) if r.get("permission_denials"))
    treatment_denials = sum(1 for r in (treatment_runs or []) if r.get("permission_denials"))
    return {
        "overall_control": control, "overall_skill": skill, "overall_paired": paired,
        "control": selected_metrics[0], "skill": selected_metrics[1],
        "paired": selected_paired, "held_out": held, "tasks_count": tasks_count,
        "basis_note": note, "coverage": coverage, "recommendation": recommendation,
        "control_denials": control_denials, "treatment_denials": treatment_denials,
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
    if not valid:
        return (
            _RECOMMENDATION_KIND["DO NOT SHIP"],
            "DO NOT SHIP",
            "Control contamination: there is no clean baseline, so differences "
            "cannot be attributed to the skill.",
        )

    basis = ""
    use = paired or {}
    if paired_held_out is not None:
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
        reason = (
            "No paired held-out results are available. Complete the planned held-out "
            "evaluation; development results cannot replace validation."
            if paired_held_out is not None else
            "No paired runs in this result, so there is nothing to decide yet. "
            "Run the experiment to get paired scores with a confidence interval."
        )
        return (
            _RECOMMENDATION_KIND["NEEDS MORE RUNS"],
            "NEEDS MORE RUNS",
            reason,
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
    evidence_cautions: list[str] = []
    if n < 5:
        evidence_cautions.append(f"only {n} graded pairs")
    if collapsed:
        evidence_cautions.append(
            "the CI collapsed (identical differences), so uncertainty is understated"
        )
    if tasks_count is not None and tasks_count < 3:
        evidence_cautions.append(f"only {tasks_count} task(s), which limits generalization")
    practical = ""
    if thresholds and mean_raw is not None:
        practical = _practical_assessment(use, thresholds, mean_pp, preset=preset)

    if practical:
        if practical.startswith("Practical check: meets"):
            if evidence_cautions:
                return (
                    _RECOMMENDATION_KIND["NEEDS MORE RUNS"], "NEEDS MORE RUNS",
                    f"{basis}{effect_sentence('up' if mean_pp >= 0 else 'down')}, but "
                    f"{'; '.join(evidence_cautions)}. "
                    "Treat the result as preliminary; add representative tasks or runs.",
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
        cautions = evidence_cautions
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

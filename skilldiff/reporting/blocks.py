from pathlib import Path
from typing import Any, Optional

from skilldiff.adoption import (
    adoption_actions,
    adoption_failure_detail,
    adoption_summary,
    no_effect_findings,
)
from skilldiff.reporting.checks import (
    _check_table_rows,
    compare_checks,
    extract_notes,
)
from skilldiff.reporting.evaluation import (
    _EVALUATION_ALIGN,
    _EVALUATION_HEADERS,
    _EVALUATION_NOTE,
    _build_runs_table,
    _evaluation_rows,
    _is_blast_run,
    _load_runs_for_report,
)
from skilldiff.reporting.metrics import (
    _API_TOKEN_FIELDS,
    _api_equivalent_summary,
    _cost_basis_note,
    _fmt_cost_opt,
    _fmt_count_delta,
    _fmt_count_opt,
    _fmt_money,
    _fmt_money_diff,
    _fmt_time_opt,
    _fmt_tokens,
    _fmt_tokens_opt,
    _paired_comparison_metrics,
    _paired_mean,
    _paired_metric_runs,
    _score_difference,
    _score_percent,
    _signed_tokens,
    _skill_usage,
    format_cost_diff,
    format_count_diff,
    format_pp_diff,
    format_time_diff,
)
from skilldiff.reporting.table import _strip_inline, _tone
from skilldiff.reporting.types import Cell
from skilldiff.reporting.verdict import (
    _completeness_reading,
    _decision_rows,
    _efficiency_sentence,
    _metric_rows,
    _recommendation,
    _run_split,
    _verdict,
    adoption_reading,
    build_decision_context,
    category_reading,
    dual_adoption_reading,
    row_reading,
)
from skilldiff.stats import analysis_run, paired_comparison

_NUMERIC_RUN_COLUMNS = {"Run", "Score", "Checks", "Cost", "Time", "Turns", "Tokens", "Files"}
_GROUP_ALIGN = ["l", "r", "r", "r", "r", "r", "r", "r", "l"]


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
    control_label: str = "Control",
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

    if control_label == "Control" and preset in {"revision", "compression"}:
        control_label = "Skill A"

    lines = [
        title,
        "",
        f"{'Metric':<20} {control_label:>8} {treatment_label:>10} "
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
    if skill_metrics.get("skill_known_count") or (
        control_metrics.get("skill_known_count") and preset in {"revision", "compression"}
    ):
        ctrl_used = (
            _skill_usage(control_metrics)
            if (preset in {"revision", "compression"} and control_metrics.get("skill_known_count"))
            else "-"
        )
        reading = adoption_reading(
            skill_metrics.get("skill_used_count", 0),
            skill_metrics.get("skill_known_count", 0),
        )
        if preset in {"revision", "compression"} and control_metrics.get(
            "skill_known_count"
        ):
            reading = dual_adoption_reading(control_metrics, skill_metrics)
        lines.append(
            row(
                "Skill used",
                ctrl_used,
                _skill_usage(skill_metrics),
                "",
                reading,
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
        adoption = (decision or {}).get("decision_adoption") or {}
        verdict, _ = _verdict(
            verdict_paired,
            treatment_label.lower(),
            thresholds=thresholds,
            tasks_count=decision["tasks_count"] if decision is not None else tasks_count,
            preset=preset,
            control_score=c_score,
            treatment_score=t_score,
            control_denials=decision.get("control_denials", 0) if decision is not None else 0,
            treatment_denials=decision.get("treatment_denials", 0) if decision is not None else 0,
            adoption_failed=bool(adoption.get("adoption_failed")),
            adoption_detail=adoption_failure_detail(adoption),
        )
        lines.append(_strip_inline(verdict))
        if preset == "revision":
            lines.append("Both arms have a skill installed; this comparison is between revisions.")
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


def _key_takeaways(
    results: dict[str, Any],
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
) -> list[str]:
    subject = "treatment" if results.get("comparison") else "skill"
    paired = paired_comparison(control_runs, treatment_runs)
    control, skill = _paired_comparison_metrics(control_runs, treatment_runs)
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

    adoption = adoption_summary(
        results, {"control": control_runs, "treatment": treatment_runs}
    )
    if adoption.get("arms"):
        parts = []
        for data in adoption["arms"].values():
            known = int(data.get("known") or 0)
            hits = int(data.get("hits") or 0)
            rate = (
                f", {data['hit_rate'] * 100:.0f}%"
                if data.get("hit_rate") is not None
                else ""
            )
            loads = data.get("loads")
            loads_txt = f", {loads} load event(s)" if loads is not None else ""
            parts.append(
                f"{data.get('label', 'Skill')} {hits}/{known}{rate}{loads_txt}"
            )
        text = "**Adoption.** " + "; ".join(parts) + "."
        if any(data.get("unknown") for data in adoption["arms"].values()):
            unknown = sum(int(data.get("unknown") or 0) for data in adoption["arms"].values())
            text += f" Skill use is unknown for {unknown} eligible run(s)."
        if any(data.get("hits", 0) < data.get("known", 0) for data in adoption["arms"].values()):
            text += (
                " Runs where the skill was ignored dilute any effect. A sharper "
                "`description` usually fixes this."
            )
        if adoption.get("adoption_failed"):
            text += (
                " Tune the `description` on dev tasks first "
                "(`skilldiff run --split dev --runs 1`)."
            )
        if len(adoption["arms"]) > 1:
            rates = [
                data["hit_rate"]
                for data in adoption["arms"].values()
                if data.get("hit_rate") is not None
            ]
            if len(rates) > 1 and max(rates) - min(rates) >= 0.2:
                text += (
                    " The revisions triggered at different rates, so adoption "
                    "confounds the body comparison."
                )
        bullets.append(text)

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


def _group_headers(
    first: str,
    treatment_label: str = "Skill",
    control_label: str = "Control",
    show_usage: bool = True,
    dual_usage: bool = False,
) -> list[str]:
    return [
        first,
        control_label,
        treatment_label,
        "Δ score (paired mean)",
        "Better/worse/tie",
        "Δ cost (mean)",
        "Δ time (mean)",
        *(
            ["Skill A used", "Skill B used"]
            if show_usage and dual_usage
            else ["Skill used"]
            if show_usage
            else []
        ),
        "Reading",
    ]


def _category_summary(
    control_runs: list[dict[str, Any]],
    treatment_runs: list[dict[str, Any]],
    task_categories: dict[str, str],
    dual_usage: bool = False,
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
        adoption = (
            f"A {_skill_usage(m_control)} · B {_skill_usage(m_skill)}"
            if dual_usage
            else _skill_usage(m_skill)
        )
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


def _group_row(
    label: str,
    control: dict[str, Any],
    skill: dict[str, Any],
    paired: dict[str, Any],
    show_usage: bool = True,
    dual_usage: bool = False,
) -> list[Cell]:
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
    usage_cells = (
        [_skill_usage(control), _skill_usage(skill)]
        if dual_usage
        else [_skill_usage(skill)]
    )
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
        *([*usage_cells] if show_usage else []),
        score_reading,
    ]


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
        default_ctrl = "Original"
    elif preset == "revision" or skill_comparison:
        label = "Skill B"
        subject = "skill B"
        default_ctrl = "Skill A"
    else:
        label = "Treatment" if comparison else "Skill"
        subject = label.lower()
        default_ctrl = "Control"
    control_label = str(arm_labels.get("control") or default_ctrl)
    if arm_labels.get("treatment"):
        label = str(arm_labels["treatment"])
        subject = label.lower()
    name = str(results.get("name", "experiment"))
    dual_usage = preset in {"revision", "compression"}
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
    baseline_runs = runs_data.get("baseline") or []
    decision = build_decision_context(
        results, control_runs, treatment_runs, run_root, label, baseline_runs=baseline_runs
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
            control_denials=decision.get("control_denials", 0),
            treatment_denials=decision.get("treatment_denials", 0),
            adoption_failed=bool(
                (decision.get("decision_adoption") or {}).get("adoption_failed")
            ),
            adoption_detail=adoption_failure_detail(
                decision.get("decision_adoption") or {}
            ),
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
    if results.get("valid") is False:
        verdict = "INVALID: no clean baseline. " + verdict
        kind = "warning"
    body = [verdict]
    if preset == "revision":
        body.append("Both arms have a skill installed; this comparison is between revisions.")
    efficiency = (
        _efficiency_sentence(headline_paired, subject)
        if headline_paired.get("pairs")
        else None
    )
    if efficiency:
        body.append(efficiency)
    if split_basis_note:
        body.append(split_basis_note)
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
            + ". Harness errors are always excluded; grader timeouts/errors are always N/A."
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
            ["Metric", control_label, label, "Paired mean Δ", "95% CI", "Reading"],
            _metric_rows(control, skill, paired, dual_usage=dual_usage),
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
        adoption_failures = 0
        if results.get("adoption_failed"):
            skill_arms = set(
                ((decision.get("adoption") or {}).get("arms") or {}).keys()
            )
            adoption_failures = sum(
                1
                for r in control_runs + treatment_runs
                if r.get("arm") in skill_arms and r.get("skill_invoked") is False
            )
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
                            blast_errors, adoption_failures,
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

        diff_row: list[Cell] = [f"Change ({label} − {control_label})"]
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
                [_api_row(control_label, c_api), _api_row(label, s_api), diff_row],
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
    group_align = (
        [*_GROUP_ALIGN[:7], "l"]
        if comparison
        else [*_GROUP_ALIGN[:7], "r", "r", "l"]
        if dual_usage
        else _GROUP_ALIGN
    )

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
                    dual_usage=dual_usage,
                )
            )
        blocks.append(
            (
                "table",
                _group_headers(
                    "Split", label, control_label=control_label,
                    show_usage=not comparison, dual_usage=dual_usage,
                ),
                split_rows,
                group_align,
            )
        )
        if paired_held_out:
            blocks.append(
                (
                    "p",
                    "Dev pairs are for iteration; the held-out set is the honest "
                    "estimate. The headline and closing decision use the "
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
            rows.append(
                _group_row(
                    model, m_control, m_skill, m_paired,
                    show_usage=not comparison, dual_usage=dual_usage,
                )
            )
        blocks.append(
            (
                "table",
                _group_headers(
                    "Model", label, control_label=control_label,
                    show_usage=not comparison, dual_usage=dual_usage,
                ),
                rows,
                group_align,
            )
        )

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
                _group_row(
                    task_label, t_control, t_skill, t_paired,
                    show_usage=not comparison, dual_usage=dual_usage,
                )
            )
    blocks.append(
        (
            "table",
            _group_headers(
                "Task", label, control_label=control_label,
                show_usage=not comparison, dual_usage=dual_usage,
            ),
            task_rows,
            group_align,
        )
    )

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
                        control_label,
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

    task_cats: dict[str, str] = {}
    if isinstance(results.get("task_categories"), dict):
        task_cats = {str(k): str(v) for k, v in results["task_categories"].items()}
    for r in control_runs + treatment_runs:
        tid = str(r.get("task_id", ""))
        if tid and r.get("task_category") and tid not in task_cats:
            task_cats[tid] = str(r["task_category"])
    for td in results.get("task_details") or []:
        if isinstance(td, dict) and td.get("id") and td.get("category"):
            task_cats.setdefault(str(td["id"]), str(td["category"]))
    if task_cats and len(set(task_cats.values())) > 1:
        cat_rows = _category_summary(
            control_runs, treatment_runs, task_cats, dual_usage=dual_usage
        )
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
                        control_label,
                        label,
                        "Δ score",
                        "Skill A / B used" if dual_usage else "Skill used",
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

    sc = skill_comparison or {}
    if (preset == "compression" or sc.get("preset") == "compression") and (
        sc.get("source_bytes_a") or sc.get("source_bytes_b")
    ):
        try:
            ba = int(sc.get("source_bytes_a") or 0)
            bb = int(sc.get("source_bytes_b") or 0)
            pct = (bb - ba) / ba * 100 if ba else (-float(sc.get("source_reduction_pct") or 0))
            blocks.append(("h", 2, "Source size"))
            blocks.append(
                (
                    "p",
                    f"Static skill size: original {ba} bytes → minified {bb} bytes "
                    f"({pct:+.1f}%). This is separate from session tokens, "
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
            control_label=control_label,
            preset=preset,
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
                pct = (
                    (bb - ba) / ba * 100
                    if ba
                    else (-float(skill_comparison.get("source_reduction_pct") or 0))
                )
                setup_items.append(f"**Source size:** {ba} → {bb} bytes ({pct:+.1f}%)")
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
    cli_probes = sorted({
        f"{run.get('cli', {}).get('version')} ({run.get('cli', {}).get('isolation')})"
        for runs in (results.get("runs") or {}).values() for run in runs
        if isinstance(run.get("cli"), dict)
    })
    if cli_probes:
        setup_items.append("**CLI capability probes:** " + ", ".join(cli_probes))
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

    adoption = decision.get("adoption") or {}
    if adoption.get("arms"):
        blocks.append(("h", 2, "Skill adoption and next steps"))
        adoption_note = (
            "Hits are eligible runs where the skill loaded at least once. "
            "Loads count matching skill load/read events (`N/A` when the run "
            "predates event counting). Unknown runs have no measurement and are "
            "not counted as misses."
        )
        if decision.get("held_out"):
            gate = decision.get("decision_adoption") or {}
            gate_bits = []
            for data in (gate.get("arms") or {}).values():
                gate_bits.append(
                    f"{data.get('label', 'Skill')} "
                    f"{data.get('hits', 0)}/{data.get('known', 0)}"
                )
            if gate_bits:
                adoption_note += (
                    " The closing decision checks held-out adoption: "
                    + ", ".join(gate_bits)
                    + "."
                )
        blocks.append(("p", adoption_note))
        adoption_rows: list[list[Cell]] = []
        for data in adoption["arms"].values():
            rate = (
                f"{data['hit_rate'] * 100:.0f}%"
                if data.get("hit_rate") is not None
                else "N/A"
            )
            loads = (
                str(data["loads"])
                if data.get("loads") is not None
                else "N/A"
            )
            adoption_rows.append(
                [
                    str(data.get("label") or "Skill"),
                    str(data.get("hits", 0)),
                    str(data.get("eligible", 0)),
                    str(data.get("unknown", 0)),
                    rate,
                    loads,
                    str(data.get("reading") or "Unknown"),
                ]
            )
        blocks.append(
            (
                "table",
                ["Arm", "Hits", "Eligible", "Unknown", "Hit rate", "Loads", "Reading"],
                adoption_rows,
                ["l", "r", "r", "r", "r", "r", "l"],
            )
        )
        actions = adoption_actions(
            results,
            adoption,
            no_effect_findings(
                results,
                {
                    "control": control_runs,
                    "treatment": treatment_runs,
                    "baseline": baseline_runs,
                },
            ),
        )
        if actions:
            blocks.append(("ul", [action["advice"] for action in actions]))
        else:
            blocks.append(
                (
                    "p",
                    "No adoption action needed: every eligible skill run loaded the skill.",
                )
            )

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
            ["Metric", control_label, label, "Paired change", "95% CI", "Reading"],
            _decision_rows(
                headline_control, headline_skill, headline_paired,
                dual_usage=dual_usage,
            ),
            ["l", "r", "r", "r", "r", "l"],
        )
    )
    blocks.append(("callout", rec_kind, f"Recommendation: {rec_label}", [rec_reason]))
    return f"skilldiff: {name}", blocks

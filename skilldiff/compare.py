"""Reproducible comparisons across skill revisions: `skilldiff compare RUN_A RUN_B`.

Compares two experiment runs (skill revision A vs B): score, adoption, and
efficiency changes, plus newly failing/passing checks. Warns when provenance
(models, tasks, harness, skilldiff/CLI versions) differs enough to make the
comparison unreliable.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from skilldiff.reporter import _apply_codex_cache_write_fallback
from skilldiff.stats import METRICS, analysis_run, pair_runs, usable_agent_run


def load_results(path: Path) -> tuple[dict[str, Any], Path]:
    p = Path(path)
    if p.is_dir():
        p = p / "results.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    return data, p.parent


def _skill_metrics(results: dict[str, Any]) -> dict[str, Any]:
    overall = results.get("overall") or {}
    return overall.get("skill") or {}


def _paired(results: dict[str, Any]) -> dict[str, Any]:
    return (results.get("overall") or {}).get("paired") or {}


def _arm_runs(results: dict[str, Any], arm: str) -> list[dict[str, Any]]:
    runs = results.get("runs") or {}
    names = ("treatment", "skill") if arm == "treatment" else (arm,)
    if isinstance(runs, list):
        records = [r for r in runs if r.get("arm") in names]
        return _apply_codex_cache_write_fallback({arm: records}, results)[arm]
    for key in names:
        if runs.get(key):
            return _apply_codex_cache_write_fallback({arm: list(runs[key])}, results)[arm]
    return []


def _skill_runs(results: dict[str, Any]) -> list[dict[str, Any]]:
    return _arm_runs(results, "treatment")


def _failure_policy(results: dict[str, Any]) -> dict[str, str]:
    saved = (
        results.get("failure_policy") or (results.get("settings") or {}).get("failure_policy") or {}
    )
    return {"agent_failure": "exclude", "missing": "exclude", **saved}


def _known_number(run: dict[str, Any], field: str) -> float | None:
    if not usable_agent_run(run) or run.get(field) is None:
        return None
    try:
        value = float(run[field])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _known_tokens(run: dict[str, Any]) -> float | None:
    value = METRICS["tokens"](run)
    return value if value is not None and math.isfinite(value) else None


def _analysis_pairs(a: dict[str, Any], b: dict[str, Any]) -> list[tuple[dict, dict]]:
    return pair_runs(
        [analysis_run(r, _failure_policy(a)["agent_failure"]) for r in _skill_runs(a)],
        [analysis_run(r, _failure_policy(b)["agent_failure"]) for r in _skill_runs(b)],
    )


def _matched_efficiency(a: dict[str, Any], b: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Normalize efficiency by matched tasks and repetitions.

    Totals mislead when runs differ in task count or repetitions. We filter
    both experiments to the intersection of (model, task_id, repetition) keys
    in the skill/treatment arm, then use both-known values for each metric. When
    no run records exist (older results.json), fall back to totals.
    """
    aruns, bruns = _skill_runs(a), _skill_runs(b)
    matched_tasks: list[str] = []
    eff: dict[str, Any] = {}
    if aruns or bruns:
        pairs = _analysis_pairs(a, b)
        matched_tasks = sorted(
            {
                str(c.get("task_id", ""))
                for c, t in pairs
                if usable_agent_run(c) and usable_agent_run(t)
            }
        )
        for label, getter in (
            ("cost", lambda r: _known_number(r, "cost")),
            ("time", lambda r: _known_number(r, "duration")),
            ("tokens", _known_tokens),
        ):
            values = [(getter(c), getter(t)) for c, t in pairs]
            known = [(av, bv) for av, bv in values if av is not None and bv is not None]
            av = sum(av for av, _ in known) / len(known) if known else None
            bv = sum(bv for _, bv in known) / len(known) if known else None
            eff[label] = {
                "a": av,
                "b": bv,
                "delta": (bv - av) if av is not None and bv is not None else None,
                "a_runs": len(known),
                "b_runs": len(known),
                "matched": True,
            }
        return eff, matched_tasks

    # Fallback for summary-only results: totals with a warning that they are unnormalized.
    am, bm = _skill_metrics(a), _skill_metrics(b)
    for key_name, label in (
        ("total_cost", "cost"),
        ("total_duration", "time"),
        ("total_tokens", "tokens"),
    ):
        av, bv = am.get(key_name), bm.get(key_name)
        if av is None or bv is None:
            eff[label] = {"a": av, "b": bv, "delta": None, "matched": False}
        else:
            try:
                eff[label] = {
                    "a": float(av),
                    "b": float(bv),
                    "delta": float(bv) - float(av),
                    "matched": False,
                }
            except (TypeError, ValueError):
                eff[label] = {"a": av, "b": bv, "delta": None, "matched": False}
    return eff, []


def check_compatibility(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """List incompatibilities that make cross-run comparison unreliable."""
    warnings: list[str] = []
    if a.get("models") != b.get("models"):
        warnings.append(f"Models differ: {a.get('models')} vs {b.get('models')}.")
    if a.get("tasks") != b.get("tasks"):
        warnings.append(f"Tasks differ: {a.get('tasks')} vs {b.get('tasks')}.")
    if a.get("harness") != b.get("harness"):
        warnings.append(f"Harness differs: {a.get('harness')} vs {b.get('harness')}.")
    if a.get("cost_basis", "harness") != b.get("cost_basis", "harness"):
        warnings.append("Decision cost bases differ; cost comparisons use different meanings.")
    if a.get("cost_basis") == b.get("cost_basis") == "api-equivalent":
        if a.get("pricing") != b.get("pricing"):
            warnings.append("Recorded pricing differs; API cost changes may reflect rates.")
    if _failure_policy(a) != _failure_policy(b):
        warnings.append(f"Failure policies differ: {_failure_policy(a)} vs {_failure_policy(b)}.")
    if a.get("skilldiff_version") != b.get("skilldiff_version"):
        warnings.append(
            f"skilldiff versions differ: {a.get('skilldiff_version')} "
            f"vs {b.get('skilldiff_version')}."
        )
    pa_cli = (a.get("provenance") or {}).get("agent_cli")
    pb_cli = (b.get("provenance") or {}).get("agent_cli")
    if pa_cli != pb_cli and (pa_cli or pb_cli):
        warnings.append(f"Agent CLI versions differ: {pa_cli} vs {pb_cli}.")
    sa = (a.get("provenance") or {}).get("skill_hash")
    sb = (b.get("provenance") or {}).get("skill_hash")
    if sa and sb and sa == sb:
        warnings.append("Skill hashes are identical; runs may test the same revision.")
    ta_h = (a.get("provenance") or {}).get("tasks_hash")
    tb_h = (b.get("provenance") or {}).get("tasks_hash")
    if ta_h and tb_h and ta_h != tb_h:
        warnings.append(
            "Task/prompt/fixture/grader hashes differ; "
            "score changes may come from tasks, not the skill."
        )
    # Strict checks: grader contents and locks are part of tasks_hash now, but
    # surface them explicitly for older runs that lack them.
    for label, results in (("A", a), ("B", b)):
        for entry in (results.get("provenance") or {}).get("task_files") or []:
            if entry.get("graders_hash") is None and entry.get("grader"):
                warnings.append(f"Run {label} task {entry.get('id')} has no recorded grader hash.")
                break
    return warnings


def _pct(metrics: dict[str, Any]) -> Any:
    v = metrics.get("task_score")
    return None if v is None else round(float(v) * 100)


def compare_results(a: dict[str, Any], b: dict[str, Any], strict: bool = False) -> dict[str, Any]:
    am, bm = _skill_metrics(a), _skill_metrics(b)
    ap, bp = _paired(a), _paired(b)

    score_a, score_b = _pct(am), _pct(bm)
    score_counts: dict[str, Any] = {"matched": False}
    matched_pairs = _analysis_pairs(a, b)
    if any("score" in r or "grade_status" in r for r in _skill_runs(a) + _skill_runs(b)):
        values = [(METRICS["score"](arun), METRICS["score"](brun)) for arun, brun in matched_pairs]
        known = [
            (av, bv)
            for av, bv in values
            if av is not None and bv is not None and math.isfinite(av) and math.isfinite(bv)
        ]
        score_a = round(sum(av for av, _ in known) / len(known) * 100) if known else None
        score_b = round(sum(bv for _, bv in known) / len(known) * 100) if known else None
        score_counts = {"matched": True, "a_runs": len(known), "b_runs": len(known)}
    score_delta = (score_b - score_a) if score_a is not None and score_b is not None else None

    adoption_a = (
        f"{am.get('skill_used_count', 0)}/{am.get('skill_known_count', 0)}"
        if am.get("skill_known_count")
        else "unknown"
    )
    adoption_b = (
        f"{bm.get('skill_used_count', 0)}/{bm.get('skill_known_count', 0)}"
        if bm.get("skill_known_count")
        else "unknown"
    )
    if any("skill_invoked" in r for r in _skill_runs(a) + _skill_runs(b)):
        known_adoption = [
            (arun["skill_invoked"], brun["skill_invoked"])
            for arun, brun in matched_pairs
            if usable_agent_run(arun)
            and usable_agent_run(brun)
            and isinstance(arun.get("skill_invoked"), bool)
            and isinstance(brun.get("skill_invoked"), bool)
        ]
        adoption_a = (
            f"{sum(av for av, _ in known_adoption)}/{len(known_adoption)}"
            if known_adoption
            else "unknown"
        )
        adoption_b = (
            f"{sum(bv for _, bv in known_adoption)}/{len(known_adoption)}"
            if known_adoption
            else "unknown"
        )

    eff, matched_tasks = _matched_efficiency(a, b)

    # Treatment effects (skill minus control) in each run, then change.
    def effect(results: dict[str, Any]) -> Any:
        m = (results.get("overall") or {}).get("paired", {}).get("score", {}).get("mean_diff")
        return None if m is None else float(m) * 100

    eff_a, eff_b = effect(a), effect(b)
    if _arm_runs(a, "control") or _arm_runs(b, "control"):

        def effects(results: dict[str, Any]) -> list[dict[str, Any]]:
            policy = _failure_policy(results)["agent_failure"]
            pairs = pair_runs(
                [analysis_run(r, policy) for r in _arm_runs(results, "control")],
                [analysis_run(r, policy) for r in _skill_runs(results)],
            )
            records = []
            for c_run, t_run in pairs:
                cv, tv = METRICS["score"](c_run), METRICS["score"](t_run)
                if cv is not None and tv is not None and math.isfinite(cv) and math.isfinite(tv):
                    records.append({**t_run, "effect": tv - cv})
            return records

        common_effects = pair_runs(effects(a), effects(b))
        eff_a = (
            sum(arun["effect"] for arun, _ in common_effects) / len(common_effects) * 100
            if common_effects
            else None
        )
        eff_b = (
            sum(brun["effect"] for _, brun in common_effects) / len(common_effects) * 100
            if common_effects
            else None
        )
    effect_delta = (eff_b - eff_a) if eff_a is not None and eff_b is not None else None

    # Check changes require completed evidence on both sides of the same repetition.
    from skilldiff.reporter import extract_checks

    checks: dict[tuple[str, str, str], list[int]] = {}
    for arun, brun in pair_runs(_skill_runs(a), _skill_runs(b)):
        if any(
            not usable_agent_run(r)
            or r.get("status") not in (None, "ok", "correctness")
            or r.get("grade_status") in ("error", "timeout", "ungraded")
            for r in (arun, brun)
        ):
            continue
        ca, cb = dict(extract_checks(arun)), dict(extract_checks(brun))
        for name in ca.keys() & cb.keys():
            if ca[name] is None or cb[name] is None:
                continue
            key = (str(arun.get("model", "")), str(arun.get("task_id", "")), name)
            entry = checks.setdefault(key, [0, 0, 0])
            entry[0] += int(ca[name])
            entry[1] += int(cb[name])
            entry[2] += 1
    newly_failing: list[dict[str, Any]] = []
    newly_passing: list[dict[str, Any]] = []
    for (model, task, name), (pa, pb, total) in sorted(checks.items()):
        ra, rb = pa / total, pb / total
        change = {
            "model": model,
            "task": task,
            "check": name,
            "a": f"{pa}/{total}",
            "b": f"{pb}/{total}",
        }
        if ra >= 0.5 and rb < 0.5:
            newly_failing.append(change)
        elif ra < 0.5 and rb >= 0.5:
            newly_passing.append(change)

    # Provenance warnings (shared helper so --strict and default agree).
    warnings = check_compatibility(a, b)
    if strict and warnings:
        raise ValueError(
            "Strict comparison refused: incompatible experiments:\n- " + "\n- ".join(warnings)
        )

    return {
        "a_name": a.get("name"),
        "b_name": b.get("name"),
        "score": {"a": score_a, "b": score_b, "delta_pp": score_delta, **score_counts},
        "adoption": {"a": adoption_a, "b": adoption_b},
        "efficiency": eff,
        "matched_tasks": matched_tasks,
        "effect": {"a_pp": eff_a, "b_pp": eff_b, "delta_pp": effect_delta},
        "newly_failing": newly_failing,
        "newly_passing": newly_passing,
        "provenance_warnings": warnings,
        "pairs": {"a": ap.get("pairs"), "b": bp.get("pairs")},
        "strict": strict,
    }


def format_comparison(comp: dict[str, Any]) -> str:
    s = comp["score"]
    lines = [f"Compare {comp['a_name']} → {comp['b_name']}", ""]
    sa = f"{s['a']}%" if s["a"] is not None else "N/A"
    sb = f"{s['b']}%" if s["b"] is not None else "N/A"
    sd = f"{s['delta_pp']:+.0f} pp" if s["delta_pp"] is not None else "N/A"
    score_note = (
        f" [n={s.get('a_runs')}/{s.get('b_runs')} matched runs]"
        if s.get("matched")
        else " (saved summaries; no score records to match)"
    )
    lines.append(f"Skill score: {sa} → {sb} ({sd}){score_note}")
    lines.append(f"Skill adoption: {comp['adoption']['a']} → {comp['adoption']['b']}")
    matched = comp.get("matched_tasks") or []
    matched_note = (
        f" (matched {len(matched)} task(s): {', '.join(matched[:5])})"
        if matched
        else " (totals; no run records to match)"
        if any(
            not (comp["efficiency"].get(k) or {}).get("matched") for k in ("cost", "time", "tokens")
        )
        else " (per-run means over matched tasks)"
    )
    if all((comp["efficiency"].get(k) or {}).get("matched") for k in ("cost", "time", "tokens")):
        lines.append(f"Efficiency is per-run means over matched tasks/reps{matched_note}:")
    else:
        lines.append("Efficiency uses unnormalized totals (no run records to match):")
    for label in ("cost", "time", "tokens"):
        e = comp["efficiency"][label]
        a_txt = "N/A" if e["a"] is None else f"{e['a']:.2f}" if label == "cost" else f"{e['a']:.0f}"
        b_txt = "N/A" if e["b"] is None else f"{e['b']:.2f}" if label == "cost" else f"{e['b']:.0f}"
        d_txt = (
            "N/A"
            if e["delta"] is None
            else f"{e['delta']:+.2f}"
            if label == "cost"
            else f"{e['delta']:+.0f}"
        )
        suffix = ""
        if e.get("matched"):
            suffix = f" [n={e.get('a_runs')}/{e.get('b_runs')} runs]"
        lines.append(f"Skill {label}: {a_txt} → {b_txt} ({d_txt}){suffix}")
    e = comp["effect"]
    if e["a_pp"] is not None and e["b_pp"] is not None:
        lines.append(
            f"Treatment effect (skill-control): {e['a_pp']:+.0f} pp → {e['b_pp']:+.0f} pp "
            f"({e['delta_pp']:+.0f} pp change)"
        )
    lines.append(f"Pairs: {comp['pairs']['a']} → {comp['pairs']['b']}")
    if comp["newly_failing"]:
        lines.append("Newly failing checks (skill arm):")
        for r in comp["newly_failing"]:
            model = f"{r['model']} · " if r.get("model") else ""
            lines.append(f"  - {model}{r['task']} · {r['check']}: {r['a']} → {r['b']}")
    else:
        lines.append("Newly failing checks: none")
    if comp["newly_passing"]:
        lines.append("Newly passing checks (skill arm):")
        for r in comp["newly_passing"]:
            model = f"{r['model']} · " if r.get("model") else ""
            lines.append(f"  - {model}{r['task']} · {r['check']}: {r['a']} → {r['b']}")
    if comp["provenance_warnings"]:
        lines.append("Provenance:")
        for w in comp["provenance_warnings"]:
            lines.append(f"  - {w}")
    return "\n".join(lines)

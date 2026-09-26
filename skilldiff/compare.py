"""Reproducible comparisons across skill revisions: `skilldiff compare RUN_A RUN_B`.

Compares two experiment runs (skill revision A vs B): score, adoption, and
efficiency changes, plus newly failing/passing checks. Warns when provenance
(models, tasks, harness, skilldiff/CLI versions) differs enough to make the
comparison unreliable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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


def _pct(metrics: dict[str, Any]) -> Any:
    v = metrics.get("task_score")
    return None if v is None else round(float(v) * 100)


def compare_results(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    am, bm = _skill_metrics(a), _skill_metrics(b)
    ap, bp = _paired(a), _paired(b)

    def delta_pct(key: str) -> Any:
        av, bv = am.get(key), bm.get(key)
        if av is None or bv is None:
            return None
        try:
            return float(bv) - float(av)
        except (TypeError, ValueError):
            return None

    score_a, score_b = _pct(am), _pct(bm)
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

    eff: dict[str, Any] = {}
    for key, label in (
        ("total_cost", "cost"),
        ("total_duration", "time"),
        ("total_tokens", "tokens"),
    ):
        av, bv = am.get(key), bm.get(key)
        if av is None or bv is None:
            eff[label] = {"a": av, "b": bv, "delta": None}
        else:
            try:
                eff[label] = {"a": float(av), "b": float(bv), "delta": float(bv) - float(av)}
            except (TypeError, ValueError):
                eff[label] = {"a": av, "b": bv, "delta": None}

    # Treatment effects (skill minus control) in each run, then change.
    def effect(results: dict[str, Any]) -> Any:
        m = (results.get("overall") or {}).get("paired", {}).get("score", {}).get("mean_diff")
        return None if m is None else float(m) * 100

    eff_a, eff_b = effect(a), effect(b)
    effect_delta = (eff_b - eff_a) if eff_a is not None and eff_b is not None else None

    # Newly failing/passing checks: compare skill-arm pass rates per (task, check).
    from skilldiff.reporter import extract_checks

    def skill_checks(results: dict[str, Any]) -> dict[tuple[str, str], tuple[int, int]]:
        runs = results.get("runs") or {}
        # Support both control/treatment and control/skill keys.
        skill_runs = runs.get("treatment") or runs.get("skill") or []
        agg: dict[tuple[str, str], list[int]] = {}
        for r in skill_runs:
            for name, passed in extract_checks(r):
                if passed is None:
                    continue
                key = (str(r.get("task_id", "")), name)
                entry = agg.setdefault(key, [0, 0])
                entry[1] += 1
                entry[0] += 1 if passed else 0
        return {k: (v[0], v[1]) for k, v in agg.items()}

    ca, cb = skill_checks(a), skill_checks(b)
    newly_failing: list[dict[str, Any]] = []
    newly_passing: list[dict[str, Any]] = []
    for key in sorted(set(ca) | set(cb)):
        pa, ta = ca.get(key, (0, 0))
        pb, tb = cb.get(key, (0, 0))
        if not ta or not tb:
            continue
        ra, rb = pa / ta, pb / tb
        if ra >= 0.5 and rb < 0.5:
            newly_failing.append(
                {"task": key[0], "check": key[1], "a": f"{pa}/{ta}", "b": f"{pb}/{tb}"}
            )
        elif ra < 0.5 and rb >= 0.5:
            newly_passing.append(
                {"task": key[0], "check": key[1], "a": f"{pa}/{ta}", "b": f"{pb}/{tb}"}
            )

    # Provenance warnings.
    warnings: list[str] = []
    if a.get("models") != b.get("models"):
        warnings.append(f"Models differ: {a.get('models')} vs {b.get('models')}.")
    if a.get("tasks") != b.get("tasks"):
        warnings.append(f"Tasks differ: {a.get('tasks')} vs {b.get('tasks')}.")
    if a.get("harness") != b.get("harness"):
        warnings.append(f"Harness differs: {a.get('harness')} vs {b.get('harness')}.")
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
            "Task/prompt/fixture hashes differ; score changes may come from tasks, not the skill."
        )

    return {
        "a_name": a.get("name"),
        "b_name": b.get("name"),
        "score": {"a": score_a, "b": score_b, "delta_pp": score_delta},
        "adoption": {"a": adoption_a, "b": adoption_b},
        "efficiency": eff,
        "effect": {"a_pp": eff_a, "b_pp": eff_b, "delta_pp": effect_delta},
        "newly_failing": newly_failing,
        "newly_passing": newly_passing,
        "provenance_warnings": warnings,
        "pairs": {"a": ap.get("pairs"), "b": bp.get("pairs")},
    }


def format_comparison(comp: dict[str, Any]) -> str:
    s = comp["score"]
    lines = [f"Compare {comp['a_name']} → {comp['b_name']}", ""]
    sa = f"{s['a']}%" if s["a"] is not None else "N/A"
    sb = f"{s['b']}%" if s["b"] is not None else "N/A"
    sd = f"{s['delta_pp']:+.0f} pp" if s["delta_pp"] is not None else "N/A"
    lines.append(f"Skill score: {sa} → {sb} ({sd})")
    lines.append(f"Skill adoption: {comp['adoption']['a']} → {comp['adoption']['b']}")
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
        lines.append(f"Skill {label}: {a_txt} → {b_txt} ({d_txt})")
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
            lines.append(f"  - {r['task']} · {r['check']}: {r['a']} → {r['b']}")
    else:
        lines.append("Newly failing checks: none")
    if comp["newly_passing"]:
        lines.append("Newly passing checks (skill arm):")
        for r in comp["newly_passing"]:
            lines.append(f"  - {r['task']} · {r['check']}: {r['a']} → {r['b']}")
    if comp["provenance_warnings"]:
        lines.append("Provenance:")
        for w in comp["provenance_warnings"]:
            lines.append(f"  - {w}")
    return "\n".join(lines)

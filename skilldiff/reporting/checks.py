import json
from typing import Any, Optional

from skilldiff.reporting.metrics import format_pp_diff
from skilldiff.reporting.table import _tone
from skilldiff.reporting.types import Cell


def _parse_feedback_data(fb: Any) -> Optional[dict[str, Any]]:
    if isinstance(fb, dict):
        return fb
    if not isinstance(fb, str) or not fb.strip():
        return None
    text = fb.strip()
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
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and 0.0 <= value <= 1.0:
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
    data = _parse_feedback_data(run.get("feedback"))
    if not isinstance(data, dict):
        return []
    checks = data.get("checks")
    if isinstance(checks, dict):
        return [(str(k), _interpret_check_value(v)) for k, v in checks.items()]
    if not isinstance(checks, list) or not checks:
        return []
    out: list[tuple[str, Optional[bool]]] = []
    for i, entry in enumerate(checks):
        out.append((_check_name(entry, i), _interpret_check_value(entry)))
    return out


def extract_notes(run: dict[str, Any]) -> list[str]:
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
    if run.get("grade_status") in ("ungraded", "timeout", "error"):
        return "N/A"
    succ = run.get("success")
    if succ is True:
        return "1/1"
    if succ is False:
        return "0/1"
    return "-"


def check_reading(wins: int, losses: int, pairs: int) -> str:
    if not pairs:
        return "No data"
    if wins > losses:
        return "Helps"
    if losses > wins:
        return "Hurts"
    return "No difference"


def compare_checks(
    control_runs: list[dict[str, Any]], treatment_runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    from skilldiff.stats import pair_runs as _pair_runs

    pairs = _pair_runs(control_runs, treatment_runs)
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for c, t in pairs:
        if (c.get("status") not in (None, "ok", "correctness")
                or t.get("status") not in (None, "ok", "correctness")
                or c.get("grade_status") in ("timeout", "error")
                or t.get("grade_status") in ("timeout", "error")):
            continue
        task_id = str(c.get("task_id") or t.get("task_id") or "")
        c_checks = {n: p for n, p in extract_checks(c)}
        t_checks = {n: p for n, p in extract_checks(t)}
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

"""Inspect Scout scanners for skilldiff transcripts.

Requires the optional ``scout`` extra. Scanners read the transcripts that
``skilldiff export --format scout-db`` (or ``skilldiff scan``) writes, so the
same evidence the harness parsers use can be audited, cross-checked, and
extended with model-judged questions through Scout's own tooling
(``scout scan``, ``scout view``, validation sets).

Deterministic scanners need no model. ``skill_use`` mirrors the harness
adoption rule: a tool call that loads the skill or reads a file under its
directory counts only when its tool result arrived without an error.
"""

from __future__ import annotations

import json
from typing import Any

from inspect_ai.model import ChatMessageAssistant, ChatMessageTool
from inspect_scout import (
    Reference,
    Result,
    ScanJob,
    Scanner,
    Transcript,
    grep_scanner,
    llm_scanner,
    scanjob,
    scanner,
    transcripts_from,
)

from skilldiff.runner import detect_skill_reference

# Tool names per harness, lower-cased: Claude Skill/Read/Bash, OpenCode
# skill/read/bash, AGY read_file/view_file/run_command, Codex item types.
SKILL_TOOLS = frozenset({"skill", "load_skill", "use_skill"})
READ_TOOLS = frozenset({"read", "read_file", "readfile", "view_file", "file_read", "view", "cat"})
SHELL_TOOLS = frozenset({"bash", "shell", "run_command", "command_execution", "command", "exec"})

INFLUENCE_LABELS = ["followed", "partially_followed", "ignored", "not_applicable"]
MISS_LABELS = ["never_discovered", "discovered_but_ignored", "read_but_not_applied",
               "applied", "not_applicable"]


def skill_names_of(transcript: Transcript) -> list[str]:
    names = transcript.metadata.get("skill_names") or []
    return [str(n) for n in names if n]


def reference_kind(function: str, arguments: Any, names: list[str]) -> str | None:
    """How a tool call touches the target skill, or None when it does not."""
    blob = json.dumps(arguments, default=str) if not isinstance(arguments, str) else arguments
    tool = (function or "").lower()
    if tool in SKILL_TOOLS:
        values = arguments.values() if isinstance(arguments, dict) else [arguments]
        if any(str(v) in names for v in values) or detect_skill_reference(blob, names):
            return "skill"
        return None
    if not detect_skill_reference(blob, names):
        return None
    if tool in READ_TOOLS:
        return "read"
    if tool in SHELL_TOOLS:
        return "command"
    return "unclassified"


@scanner(messages="all")
def skill_use() -> Scanner[Transcript]:
    """Did the agent load or read the target skill, confirmed by a tool result?"""

    async def scan(transcript: Transcript) -> Result:
        names = skill_names_of(transcript)
        meta = transcript.metadata
        if not names:
            return Result(
                value=False, answer="unmeasured",
                explanation="transcript metadata lists no target skill",
            )
        outcomes: dict[str, bool] = {}
        for msg in transcript.messages:
            if isinstance(msg, ChatMessageTool) and msg.tool_call_id:
                outcomes[msg.tool_call_id] = msg.error is None
        hits: list[dict[str, Any]] = []
        for index, msg in enumerate(transcript.messages):
            if not isinstance(msg, ChatMessageAssistant) or not msg.tool_calls:
                continue
            for call in msg.tool_calls:
                kind = reference_kind(call.function, call.arguments, names)
                if kind is None:
                    continue
                hits.append({
                    "message": index + 1, "id": msg.id, "tool": call.function,
                    "kind": kind, "outcome": outcomes.get(call.id),
                })
        classified = [h for h in hits if h["kind"] != "unclassified"]
        confirmed = [h for h in classified if h["outcome"] is True]
        failed = [h for h in classified if h["outcome"] is False]
        unresolved = [h for h in classified if h["outcome"] is None]
        if confirmed:
            answer = "used"
        elif unresolved:
            answer = "unmeasured"
        elif failed:
            answer = "denied"
        else:
            answer = "unused"
        words = {True: "ok", False: "error", None: "no result"}
        explanation = "; ".join(
            f"[M{h['message']}] {h['tool']} ({h['kind']}, {words[h['outcome']]})" for h in hits
        ) or "no tool call referenced the skill"
        return Result(
            value=bool(confirmed),
            answer=answer,
            explanation=explanation,
            references=[Reference(type="message", id=str(h["id"])) for h in hits if h["id"]],
            metadata={
                "confirmed": len(confirmed), "failed": len(failed),
                "unresolved": len(unresolved),
                "unclassified": len(hits) - len(classified),
                "harness_skill_invoked": meta.get("skill_invoked"),
                "arm": meta.get("arm"),
            },
        )

    return scan


def skill_mentions(skills: list[str] | str) -> Scanner[Transcript]:
    """Any mention of the skill's name or directory anywhere in the transcript."""
    names = [s for s in (skills.split(",") if isinstance(skills, str) else skills) if s]
    return grep_scanner(
        {"skill_path": [f"skills/{name}" for name in names], "skill_name": list(names)},
        name="skill_mentions",
    )


def _skill_text(transcript: Transcript) -> str:
    bodies = transcript.metadata.get("skill_bodies") or {}
    if not bodies:
        names = ", ".join(skill_names_of(transcript)) or "the target skill"
        return f"(SKILL.md text unavailable; the skill is named {names})"
    return "\n\n".join(f"### {name}\n{body}" for name, body in bodies.items())


def _arm_sentence(transcript: Transcript) -> str:
    arm = str(transcript.metadata.get("arm") or "")
    label = transcript.metadata.get("arm_label") or arm or "unknown"
    installed = arm in {"treatment", "skill"}
    return (
        f"This transcript is the '{label}' arm: the skill below "
        f"{'was' if installed else 'was NOT'} installed in the agent's workspace."
    )


async def _influence_question(transcript: Transcript) -> str:
    return (
        f"{_arm_sentence(transcript)}\n\n"
        "Judge whether the agent's actions and final work follow the skill's specific "
        "instructions, not generic good practice. Answer 'followed' when the work "
        "clearly reflects the skill's distinctive guidance, 'partially_followed' when "
        "only some of it shows, 'ignored' when the task called for the skill but the "
        "work does not reflect it, and 'not_applicable' when the task did not call "
        "for this skill at all.\n\nSKILL.md:\n\n" + _skill_text(transcript)
    )


async def _miss_question(transcript: Transcript) -> str:
    return (
        f"{_arm_sentence(transcript)}\n\n"
        "If the skill was not installed, answer 'not_applicable'. Otherwise classify "
        "what happened to it: 'never_discovered' (nothing in the transcript shows the "
        "agent saw it), 'discovered_but_ignored' (it was listed or mentioned but never "
        "loaded or read), 'read_but_not_applied' (loaded or read, but the work does not "
        "follow it), or 'applied' (loaded or read and followed).\n\nSKILL.md:\n\n"
        + _skill_text(transcript)
    )


def skill_influence(model: str | None = None) -> Scanner[Transcript]:
    """Model-judged: does the work reflect the skill's instructions? Runs on both arms."""
    return llm_scanner(
        name="skill_influence", question=_influence_question, answer=INFLUENCE_LABELS,
        model=model,
    )


def skill_miss_reason(model: str | None = None) -> Scanner[Transcript]:
    """Model-judged: why a skill arm did or did not end up using the skill."""
    return llm_scanner(
        name="skill_miss_reason", question=_miss_question, answer=MISS_LABELS, model=model,
    )


def build_scanners(
    skills: list[str] | str = "", llm: bool = False, model: str | None = None
) -> dict[str, Scanner[Transcript]]:
    scanners: dict[str, Scanner[Transcript]] = {"skill_use": skill_use()}
    names = [s for s in (skills.split(",") if isinstance(skills, str) else skills) if s]
    if names:
        scanners["skill_mentions"] = skill_mentions(names)
    if llm or model:
        scanners["skill_influence"] = skill_influence(model)
        scanners["skill_miss_reason"] = skill_miss_reason(model)
    return scanners


@scanjob
def skilldiff_adoption(
    transcripts: str | None = None,
    skills: str = "",
    llm: bool = False,
    model: str | None = None,
) -> ScanJob:
    """`scout scan skilldiff/scanners.py -T <run>/scout/db -S skills=<name> [-S llm=true]`."""
    return ScanJob(
        transcripts=transcripts_from(transcripts) if transcripts else None,
        scanners=build_scanners(skills, llm=llm, model=model),
        model=model,
    )


def _cell(record: Any, key: str) -> Any:
    """A data frame cell as a plain Python value; pandas NA/NaN become None."""
    import pandas as pd

    value = record.get(key)
    try:
        if value is None or pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):
            return value
    return value


# Scout reports a single-label answer as its letter (A, B, ...); name it.
LABELS_BY_SCANNER = {"skill_influence": INFLUENCE_LABELS, "skill_miss_reason": MISS_LABELS}


def label_name(scanner_name: str, value: Any) -> Any:
    labels = LABELS_BY_SCANNER.get(scanner_name)
    if labels and isinstance(value, str) and len(value) == 1 and value.isalpha():
        index = ord(value.upper()) - ord("A")
        if 0 <= index < len(labels):
            return labels[index]
    return value


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def scan_summary(scan_location: str, results: dict[str, Any] | None = None) -> dict[str, Any]:
    """Per-transcript rows from a finished scan plus agreement with the harness parser."""
    from inspect_scout import scan_results_df

    frames = scan_results_df(scan_location).scanners
    rows: dict[str, dict[str, Any]] = {}
    for name, frame in frames.items():
        for _, record in frame.iterrows():
            tid = str(record["transcript_id"])
            meta = _loads(_cell(record, "transcript_metadata")) or {}
            row = rows.setdefault(tid, {
                "transcript_id": tid,
                "arm": meta.get("arm"),
                "task": _cell(record, "transcript_task_id"),
                "model": _cell(record, "transcript_model"),
                "repetition": _cell(record, "transcript_task_repeat"),
                "harness_skill_invoked": meta.get("skill_invoked"),
                "scanners": {},
            })
            entry: dict[str, Any] = {
                "value": label_name(name, _loads(_cell(record, "value"))),
                "answer": _cell(record, "answer"),
                "explanation": _cell(record, "explanation"),
                "metadata": _loads(_cell(record, "metadata")) or {},
            }
            error = _cell(record, "scan_error")
            if error:
                entry["error"] = str(error)
            label = _cell(record, "label")
            if label:
                row["scanners"].setdefault(name, {"labels": {}})["labels"][label] = entry
            else:
                row["scanners"][name] = entry
    ordered = sorted(rows.values(), key=lambda r: (
        str(r.get("task")), str(r.get("model")), r.get("repetition") or 0, str(r.get("arm"))
    ))
    agree = disagree = unmeasured = 0
    disagreements = []
    for row in ordered:
        use = row["scanners"].get("skill_use") or {}
        scout = None if use.get("answer") in (None, "unmeasured") else bool(use.get("value"))
        harness = row.get("harness_skill_invoked")
        row["scout_skill_used"] = scout
        if scout is None or harness is None:
            unmeasured += 1
            row["agreement"] = None
        elif scout == bool(harness):
            agree += 1
            row["agreement"] = True
        else:
            disagree += 1
            row["agreement"] = False
            disagreements.append({
                "transcript_id": row["transcript_id"], "arm": row["arm"], "task": row["task"],
                "harness": bool(harness), "scout": scout,
                "explanation": use.get("explanation"),
            })
    arms: dict[str, dict[str, int]] = {}
    for row in ordered:
        bucket = arms.setdefault(str(row.get("arm")), {"runs": 0, "scout_used": 0, "known": 0})
        bucket["runs"] += 1
        if row["scout_skill_used"] is not None:
            bucket["known"] += 1
            bucket["scout_used"] += int(row["scout_skill_used"])
    return {
        "scan_location": scan_location,
        "experiment": (results or {}).get("name"),
        "harness": (results or {}).get("harness"),
        "scanners": sorted(frames.keys()),
        "rows": ordered,
        "by_arm": arms,
        "agreement": {"agree": agree, "disagree": disagree, "unmeasured": unmeasured,
                      "disagreements": disagreements},
    }


def format_scan_summary(summary: dict[str, Any]) -> str:
    llm = any(n in summary["scanners"] for n in ("skill_influence", "skill_miss_reason"))
    header = ["Arm", "Task", "Model", "Rep", "Harness", "Scout", "Hits"]
    if llm:
        header += ["Influence", "Miss reason"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]

    def yes_no(value: Any) -> str:
        return "N/A" if value is None else ("yes" if value else "no")

    for row in summary["rows"]:
        use = row["scanners"].get("skill_use") or {}
        meta = use.get("metadata") or {}
        hits = f"{meta.get('confirmed', 0)} ok"
        if meta.get("failed"):
            hits += f", {meta['failed']} error"
        if meta.get("unresolved"):
            hits += f", {meta['unresolved']} no result"
        cells = [
            str(row.get("arm")), str(row.get("task")), str(row.get("model")),
            str(row.get("repetition")), yes_no(row.get("harness_skill_invoked")),
            yes_no(row.get("scout_skill_used")), hits,
        ]
        if llm:
            for name in ("skill_influence", "skill_miss_reason"):
                entry = row["scanners"].get(name) or {}
                cells.append(str(entry.get("value") or entry.get("error") or "N/A"))
        lines.append("| " + " | ".join(cells) + " |")
    agreement = summary["agreement"]
    lines.append("")
    lines.append(
        f"Skill use: Scout and the harness parser agree on {agreement['agree']} run(s), "
        f"disagree on {agreement['disagree']}, {agreement['unmeasured']} unmeasured."
    )
    for item in agreement["disagreements"]:
        lines.append(
            f"- {item['arm']} · {item['task']}: harness says "
            f"{'used' if item['harness'] else 'unused'}, Scout says "
            f"{'used' if item['scout'] else 'unused'} ({item['explanation']})"
        )
    for arm, bucket in summary["by_arm"].items():
        if bucket["known"]:
            lines.append(f"- {arm}: Scout saw the skill used in "
                         f"{bucket['scout_used']}/{bucket['known']} measured run(s)")
    lines.append("")
    lines.append(f"Browse: scout view {summary['scan_location']}")
    return "\n".join(lines)

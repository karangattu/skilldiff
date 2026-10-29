from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from skilldiff.telemetry import redact_secrets


def _safe_json_events(stdout: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                result.append(obj)
        except (ValueError, TypeError):
            continue
    return result


def _extract_claude_messages(stream_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for event in stream_events:
        etype = event.get("type")
        if etype == "assistant":
            msg = event.get("message") or {}
            content = msg.get("content") or []
            if isinstance(content, list):
                text_parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text" and block.get("text"):
                        text_parts.append(str(block["text"]))
                    elif btype == "tool_use":
                        tool_calls.append(
                            {
                                "id": str(block.get("id") or ""),
                                "function": str(block.get("name") or ""),
                                "arguments": block.get("input") or {},
                            }
                        )
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": "\n".join(text_parts),
                }
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                messages.append(entry)
        elif etype == "user":
            msg = event.get("message") or {}
            content = msg.get("content") or []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": str(block.get("tool_use_id") or ""),
                                "content": str(block.get("content") or ""),
                                "error": "Error" if block.get("is_error") else None,
                            }
                        )
                    elif block.get("type") == "text" and block.get("text"):
                        messages.append({"role": "user", "content": str(block["text"])})
            elif isinstance(content, str) and content.strip():
                messages.append({"role": "user", "content": content})
        elif etype == "result":
            result_val = event.get("result")
            if isinstance(result_val, str) and result_val.strip():
                if not messages or messages[-1].get("role") != "assistant":
                    messages.append({"role": "assistant", "content": result_val.strip()})
    return messages


def _extract_codex_messages(stream_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for event in stream_events:
        method = event.get("method")
        params = event.get("params") or {}
        if method == "turn/start":
            input_items = params.get("input") or []
            for item in input_items:
                if isinstance(item, dict) and item.get("type") == "text":
                    messages.append({"role": "user", "content": str(item.get("text") or "")})
        kind = event.get("event") or method or event.get("type")
        item = event.get("item") if kind == "item.completed" else (
            params.get("item") if kind == "item/completed" else None
        )
        if isinstance(item, dict):
            itype = item.get("type")
            if itype in {"agent_message", "agentMessage"}:
                text = item.get("text") or ""
                messages.append({"role": "assistant", "content": str(text)})
            elif itype not in {"reasoning", "todo_list", "plan", "error"}:
                cid = str(item.get("id") or len(messages))
                payload = item.get("command") or item.get("path") or item.get("input") or {}
                args_payload = (
                    payload if isinstance(payload, dict) else {"payload": payload}
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": cid,
                                "function": str(itype or "tool"),
                                "arguments": args_payload,
                            }
                        ],
                    }
                )
                output = item.get("output") or ""
                error = item.get("error")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": cid,
                        "content": str(output),
                        "error": str(error) if error else None,
                    }
                )
    return messages


def _extract_agy_messages(stream_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for event in stream_events:
        ev = event.get("event")
        if ev == "step_update":
            step = event.get("step_update") or {}
            if step.get("step_type") == "tool" and step.get("state") == "DONE":
                info = step.get("tool_info") or {}
                name = info.get("name") or step.get("tool_name") or "tool"
                cid = str(step.get("step_index") or len(messages))
                messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": cid,
                                "function": str(name),
                                "arguments": info.get("parameters") or {},
                            }
                        ],
                    }
                )
                out = step.get("output") or info.get("result") or ""
                err = info.get("error")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": cid,
                        "content": str(out),
                        "error": str(err) if err else None,
                    }
                )
        elif ev == "result":
            res = event.get("result") or {}
            response = res.get("response")
            if response:
                messages.append({"role": "assistant", "content": str(response)})
    return messages


def _extract_opencode_messages(stream_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for event in stream_events:
        if event.get("type") == "user":
            content = event.get("content") or ""
            messages.append({"role": "user", "content": str(content)})
        elif event.get("type") == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for part in event.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    text_parts.append(str(part.get("text") or ""))
                elif part.get("type") == "tool":
                    state = part.get("state") or {}
                    tool_calls.append(
                        {
                            "id": str(part.get("id") or len(tool_calls)),
                            "function": str(part.get("name") or part.get("tool") or "tool"),
                            "arguments": state.get("input") or {},
                        }
                    )
            msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts)}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)
    return messages


def parse_transcript_messages(
    transcript_text: str,
    record: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rec = record or {}
    stdout_part = transcript_text or ""
    stderr_part = ""

    if "STDOUT:\n" in stdout_part:
        after_stdout = stdout_part.split("STDOUT:\n", 1)[1]
        if "\n\nSTDERR:\n" in after_stdout:
            stdout_part, stderr_part = after_stdout.split("\n\nSTDERR:\n", 1)
        elif "\nSTDERR:\n" in after_stdout:
            stdout_part, stderr_part = after_stdout.split("\nSTDERR:\n", 1)
        else:
            stdout_part = after_stdout

    stream = _safe_json_events(stdout_part)
    messages: list[dict[str, Any]] = []

    if stream:
        harness = str(rec.get("harness") or "").lower()
        if harness == "claude":
            messages = _extract_claude_messages(stream)
        elif harness == "codex":
            messages = _extract_codex_messages(stream)
        elif harness == "antigravity":
            messages = _extract_agy_messages(stream)
        elif harness == "opencode":
            messages = _extract_opencode_messages(stream)
        elif any("message" in e and isinstance(e.get("message"), dict) for e in stream):
            messages = _extract_claude_messages(stream)
        elif any("method" in e or "item" in e for e in stream):
            messages = _extract_codex_messages(stream)
        elif any(e.get("event") in {"step_update", "result"} for e in stream):
            messages = _extract_agy_messages(stream)
        elif any(e.get("type") in {"tool", "part"} for e in stream):
            messages = _extract_opencode_messages(stream)
        else:
            messages = _extract_claude_messages(stream)

    if not messages:
        resp = rec.get("response") or stdout_part.strip()
        if resp:
            messages.append({"role": "assistant", "content": str(resp)})

    prompt = rec.get("prompt")
    if prompt:
        first_role = messages[0].get("role") if messages else None
        first_content = str(messages[0].get("content") or "") if messages else ""
        if first_role != "user" or first_content.strip() != str(prompt).strip():
            messages.insert(0, {"role": "user", "content": str(prompt)})

    events_list: list[dict[str, Any]] = []
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        if role == "tool":
            events_list.append(
                {
                    "type": "tool_result",
                    "step": idx,
                    "tool_call_id": msg.get("tool_call_id"),
                    "content": msg.get("content"),
                    "error": msg.get("error"),
                }
            )
        else:
            event_obj: dict[str, Any] = {
                "type": "message",
                "step": idx,
                "role": role,
                "content": msg.get("content"),
            }
            if msg.get("tool_calls"):
                event_obj["tool_calls"] = msg["tool_calls"]
            events_list.append(event_obj)

    if stderr_part.strip() and rec.get("error"):
        events_list.append({"type": "stderr", "content": stderr_part.strip()})

    return messages, events_list


def build_scout_transcript(
    record: dict[str, Any],
    transcript_text: str,
    diff_text: str = "",
) -> dict[str, Any]:
    clean_text = redact_secrets(transcript_text)
    clean_diff = redact_secrets(diff_text)
    messages, events_list = parse_transcript_messages(clean_text, record)

    task_id = str(record.get("task_id") or "task")
    model = str(record.get("model") or "model")
    rep = record.get("repetition", 1)
    arm = str(record.get("arm") or "arm")
    tid = str(record.get("session_id") or f"{task_id}_{model}_{rep}_{arm}")

    metadata: dict[str, Any] = {
        "harness": record.get("harness"),
        "task_id": record.get("task_id"),
        "model": record.get("model"),
        "repetition": record.get("repetition"),
        "arm": record.get("arm"),
        "cost": record.get("cost"),
        "duration": record.get("duration"),
        "tokens": {
            "input": record.get("input_tokens"),
            "output": record.get("output_tokens"),
            "cache_read": record.get("cache_read_tokens"),
            "cache_creation": record.get("cache_creation_tokens"),
        },
        "num_turns": record.get("num_turns"),
        "tool_calls": record.get("tool_calls"),
        "skill_invoked": record.get("skill_invoked"),
        "skill_invocations": record.get("skill_invocations"),
        "skill_available": record.get("skill_available"),
        "status": record.get("status"),
        "failure_kind": record.get("failure_kind"),
        "session_id": record.get("session_id"),
        "task_split": record.get("task_split"),
        "category": record.get("category"),
    }
    if clean_diff:
        metadata["diff_chars"] = len(clean_diff)

    filtered_metadata = {k: v for k, v in metadata.items() if v is not None}

    return {
        "id": tid,
        "source": "skilldiff",
        "task": str(record.get("task_id") or ""),
        "model": str(record.get("model") or ""),
        "arm": str(record.get("arm") or ""),
        "score": record.get("score"),
        "success": record.get("success"),
        "duration": record.get("duration"),
        "messages": messages,
        "events": events_list,
        "metadata": filtered_metadata,
    }


def export_run_to_scout(
    run_dir: Path,
    output_dir: Path | None = None,
    format: str = "scout",
) -> dict[str, Any]:
    if format not in {"scout", "json", "jsonl"}:
        raise ValueError(f"Unsupported format '{format}'; choose 'scout', 'json', or 'jsonl'")

    resolved = run_dir.resolve()
    if resolved.is_file():
        results_file = resolved
        root_dir = resolved.parent
    else:
        results_file = resolved / "results.json"
        root_dir = resolved

    if not results_file.is_file():
        raise FileNotFoundError(f"results.json not found in {run_dir}")

    data = json.loads(results_file.read_text(encoding="utf-8"))
    raw_runs = data.get("runs") or {}
    all_runs: list[dict[str, Any]] = []

    if isinstance(raw_runs, list):
        all_runs = raw_runs
    elif isinstance(raw_runs, dict):
        for run_list in raw_runs.values():
            if isinstance(run_list, list):
                all_runs.extend(run_list)

    if output_dir is None:
        target_dir = root_dir / "scout" if format == "scout" else root_dir
    else:
        target_dir = output_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    transcripts: list[dict[str, Any]] = []
    exported_ids: list[str] = []

    for rec in all_runs:
        if not isinstance(rec, dict):
            continue
        transcript_text = str(rec.get("transcript") or "")
        diff_text = ""
        artifacts = rec.get("artifacts")

        if artifacts:
            art_dir = root_dir / artifacts
            j_file = art_dir / "transcript.json"
            if j_file.is_file():
                try:
                    scout_obj = json.loads(j_file.read_text(encoding="utf-8"))
                    transcripts.append(scout_obj)
                    exported_ids.append(scout_obj["id"])
                    continue
                except (json.JSONDecodeError, OSError):
                    pass
            t_file = art_dir / "transcript.txt"
            if not transcript_text and t_file.is_file():
                try:
                    transcript_text = t_file.read_text(encoding="utf-8")
                except OSError:
                    pass
            d_file = art_dir / "diff.patch"
            if d_file.is_file():
                try:
                    diff_text = d_file.read_text(encoding="utf-8")
                except OSError:
                    pass

        scout_obj = build_scout_transcript(rec, transcript_text, diff_text)
        transcripts.append(scout_obj)
        exported_ids.append(scout_obj["id"])

    if format == "scout":
        for t in transcripts:
            t_path = target_dir / f"{t['id']}.json"
            t_path.write_text(json.dumps(t, indent=2), encoding="utf-8")
        jsonl_path = target_dir / "transcripts.jsonl"
        with jsonl_path.open("w", encoding="utf-8") as f:
            for t in transcripts:
                f.write(json.dumps(t) + "\n")
        meta_path = target_dir / "metadata.json"
        meta_content = {
            "experiment_name": data.get("name"),
            "harness": data.get("harness"),
            "models": data.get("models"),
            "tasks_count": data.get("tasks_count"),
            "transcripts_count": len(transcripts),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        meta_path.write_text(json.dumps(meta_content, indent=2), encoding="utf-8")
    elif format == "json":
        json_file = target_dir if target_dir.suffix == ".json" else target_dir / "transcripts.json"
        json_file.write_text(json.dumps(transcripts, indent=2), encoding="utf-8")
    elif format == "jsonl":
        is_jsonl = target_dir.suffix == ".jsonl"
        jsonl_file = target_dir if is_jsonl else target_dir / "transcripts.jsonl"
        with jsonl_file.open("w", encoding="utf-8") as f:
            for t in transcripts:
                f.write(json.dumps(t) + "\n")

    return {
        "run_dir": str(root_dir),
        "output_dir": str(target_dir),
        "format": format,
        "count": len(transcripts),
        "transcripts": exported_ids,
    }

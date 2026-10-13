"""Current native event contracts. Completion and usage are independent evidence."""

import json
import math
from typing import Any


def events(stdout: str) -> tuple[list[dict[str, Any]], str | None]:
    result = []
    error = None
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("expected an object")
            validate_event(event)
            result.append(event)
        except (ValueError, TypeError):
            error = "Malformed structured output"
    return result, error


def validate_event(value: dict[str, Any], context: str = "") -> None:
    for key, child in value.items():
        if key in {"usage", "params", "part", "step_update", "tokenUsage",
                   "tool_info", "tokens", "item", "thread", "turn", "init", "cache"} and (
                       child is not None and not isinstance(child, dict)):
            raise ValueError(f"expected {key} object")
        if context == "part" and key == "state" and not isinstance(child, dict):
            raise ValueError("expected OpenCode tool state object")
        # Tool inputs/outputs are opaque user data, not protocol objects.
        if isinstance(child, dict) and key not in {"input", "output", "parameters", "content"}:
            validate_event(child, key)
    if value.get("type") == "result" and value.get("result") is not None and (
            not isinstance(value["result"], str)):
        raise ValueError("expected Claude result text")
    if (value.get("event") == "result" or "id" in value and "result" in value):
        if not isinstance(value.get("result"), dict):
            raise ValueError("expected result object")
    if value.get("event") == "result" and value["result"].get("response") is not None and (
            not isinstance(value["result"]["response"], str)):
        raise ValueError("expected Antigravity response text")
    if context == "item" and value.get("type") in {"agent_message", "agentMessage"} and (
            value.get("text") is not None and not isinstance(value["text"], str)):
        raise ValueError("expected Codex message text")
    if value.get("type") == "assistant" and not isinstance(value.get("message"), dict):
        raise ValueError("expected assistant message object")


def count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if math.isfinite(value) and value >= 0 and value == int(value) else None


def money(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def uncached(total, cached):
    if total is None or cached is None or cached > total:
        return None
    return total - cached


def parse_codex(stdout: str, skill_names: list[str]) -> dict[str, Any]:
    from skilldiff.runner import detect_skill_reference

    stream, error = events(stdout)
    usage = None
    total = None
    response = ""
    session_id = None
    turns = tools = 0
    invoked = False
    complete = False
    for event in stream:
        kind = event.get("type") or event.get("method")
        params = event.get("params") or {}
        if kind == "thread.started":
            session_id = event.get("thread_id")
        if event.get("id") == 2 and isinstance(event.get("result"), dict):
            session_id = (event["result"].get("thread") or {}).get("id")
        if kind == "thread/tokenUsage/updated":
            snapshot = (params.get("tokenUsage") or {}).get("total")
            if isinstance(snapshot, dict):
                usage = {"input_tokens": count(snapshot.get("inputTokens")),
                         "cached_input_tokens": count(snapshot.get("cachedInputTokens")),
                         "output_tokens": count(snapshot.get("outputTokens")),
                         "cache_write_input_tokens": count(
                             snapshot.get("cacheWriteInputTokens", 0))}
                total = count(snapshot.get("totalTokens"))
        if kind in {"turn.completed", "turn/completed"}:
            turn = params.get("turn") or {}
            if kind == "turn/completed" and turn.get("status") != "completed":
                error = str(turn.get("error") or "Codex turn did not complete")
            else:
                complete = True
                turns += 1
            if isinstance(event.get("usage"), dict):
                # exec usage is per-turn; app-server snapshots above are cumulative.
                current = event["usage"]
                if usage is None:
                    usage = {key: count(current.get(key)) for key in
                             ("input_tokens", "cached_input_tokens", "output_tokens")}
                else:
                    for key in usage:
                        value = count(current.get(key))
                        usage[key] = (usage[key] + value if usage[key] is not None
                                      and value is not None else None)
        if kind in {"turn.failed", "error"} and not params.get("willRetry", False):
            error = str(event.get("error") or params.get("error") or "Codex failed")
        item = event.get("item") if kind == "item.completed" else (
            params.get("item") if kind == "item/completed" else None)
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type in {"agent_message", "agentMessage"}:
                response = item.get("text") or response
            elif item_type not in {"reasoning", "todo_list", "plan", "error"}:
                tools += 1
            if item_type in {"command_execution", "commandExecution"}:
                successful = (item.get("exit_code", item.get("exitCode")) == 0)
                invoked |= successful and detect_skill_reference(
                    str(item.get("command") or ""), skill_names)
        if "id" in event and "error" in event:
            error = str(event["error"])
    usage = usage or {}
    raw_input = count(usage.get("input_tokens"))
    cached = count(usage.get("cached_input_tokens"))
    output = count(usage.get("output_tokens"))
    if total is None and raw_input is not None and output is not None:
        total = raw_input + output
    return {"response": response, "error": error or (None if complete else
            "Codex exited without a completed turn"), "cost": None,
            "input_tokens": uncached(raw_input, cached), "output_tokens": output,
            "cache_read_tokens": cached,
            "cache_creation_tokens": usage.get("cache_write_input_tokens", 0)
            if raw_input is not None and output is not None else None,
            "tool_calls": tools if complete or tools else None,
            "num_turns": turns or None, "skill_invoked": invoked,
            "session_id": session_id, "reported_total_tokens": total}


def parse_agy(stdout: str, skill_names: list[str]) -> dict[str, Any]:
    from skilldiff.runner import detect_skill_reference

    stream, error = events(stdout)
    terminal = None
    usage = {}
    completed_tools = {}
    session_id = None
    response = ""
    invoked = False
    cost = num_turns = None
    for event in stream:
        session_id = event.get("conversation_id") or session_id
        if event.get("event") == "error":
            error = str(event.get("error") or "Antigravity error")
        if event.get("event") == "step_update":
            step = event.get("step_update") or {}
            if step.get("step_type") == "tool" and step.get("state") == "DONE":
                completed_tools[(step.get("conversation_id"), step.get("step_index"))] = step
                info = step.get("tool_info") or {}
                name = info.get("name") or step.get("tool_name")
                if not info.get("error") and name in {"view_file", "read_file", "run_command"}:
                    invoked |= detect_skill_reference(json.dumps(info.get("parameters") or {}),
                                                      skill_names)
        if event.get("event") == "result":
            current = event.get("result")
            if not isinstance(current, dict):
                error = "Malformed Antigravity result"
                continue
            terminal = current
            failed = current.get("status") != "SUCCESS"
            raw_cost = current.get("total_cost_usd", current.get("cost"))
            if raw_cost is not None or not failed:
                cost = money(raw_cost)
            if current.get("num_turns") is not None or not failed:
                num_turns = count(current.get("num_turns"))
            response = current.get("response") or response
            session_id = current.get("conversation_id") or session_id
            if isinstance(current.get("usage"), dict):
                usage = current["usage"]
            elif current.get("status") == "SUCCESS":
                usage = {}
            if failed:
                error = str(current.get("error") or
                            f"Antigravity status: {current.get('status')}")
    raw_input = count(usage.get("input_tokens"))
    cached = count(usage.get("cache_read_tokens"))
    return {"response": response, "error": error or (None if terminal else
            "Antigravity exited without a result event"),
            "cost": cost,
            "input_tokens": uncached(raw_input, cached),
            "output_tokens": count(usage.get("output_tokens")),
            "cache_read_tokens": cached,
            "cache_creation_tokens": count(usage.get("cache_creation_tokens")),
            "reported_total_tokens": count(usage.get("total_tokens")),
            "session_id": session_id, "num_turns": num_turns,
            "tool_calls": len(completed_tools) if terminal or completed_tools else None,
            "skill_invoked": invoked}


def parse_opencode(stdout: str, skill_names: list[str]) -> dict[str, Any]:
    from skilldiff.runner import detect_opencode_skill_load

    stream, error = events(stdout)
    steps = {}
    tools = {}
    texts = []
    session_id = None
    complete = False
    for index, event in enumerate(stream):
        session_id = event.get("sessionID") or session_id
        part = event.get("part") or {}
        kind = event.get("type")
        if kind == "error":
            detail = event.get("error") or event.get("message") or "OpenCode error"
            error = str(detail)
        if kind == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        if kind == "tool_use" and (part.get("state") or {}).get("status") in {
            "completed", "error"
        }:
            tools[part.get("id", index)] = part
        if kind == "step_finish":
            steps[part.get("id", index)] = part
            # A tool-calls finish only ends an intermediate model step.
            complete = part.get("reason") == "stop"
    def total(field, *path):
        values = []
        for step in steps.values():
            value = step.get(field)
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            values.append(count(value))
        return sum(values) if values and all(v is not None for v in values) else None
    costs = [money(step.get("cost")) for step in steps.values()]
    return {"response": "\n".join(texts), "error": error or (None if complete else
            "OpenCode exited without a terminal step_finish (reason: stop)"),
            "cost": sum(costs) if costs and all(v is not None for v in costs) else None,
            "input_tokens": total("tokens", "input"),
            "output_tokens": total("tokens", "output"),
            "cache_read_tokens": total("tokens", "cache", "read"),
            "cache_creation_tokens": total("tokens", "cache", "write"),
            "reported_total_tokens": total("tokens", "total"),
            "session_id": session_id, "num_turns": 1 if complete else None,
            "tool_calls": len(tools) if stream else None,
            "skill_invoked": detect_opencode_skill_load(stdout, skill_names)
            if skill_names else None}


def parse_opencode_export(
    data: dict[str, Any] | str, skill_names: list[str] = ()
) -> dict[str, Any]:
    from skilldiff.runner import _skill_matches, detect_skill_reference

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return {"error": "Malformed OpenCode session export"}
    if not isinstance(data, dict):
        return {"error": "expected OpenCode session export object"}

    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    session_id = info.get("id") or data.get("sessionID")

    last_user_idx = -1
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("type") == "user":
            last_user_idx = i
    if last_user_idx == -1:
        subsequent = messages
    else:
        subsequent = messages[last_user_idx + 1 :]
    assistants = [m for m in subsequent if isinstance(m, dict) and m.get("type") == "assistant"]

    idles = [m for m in subsequent if isinstance(m, dict) and m.get("type") == "idle"]
    last_assistant = assistants[-1] if assistants else {}

    outcome = info.get("outcome")
    if not outcome and idles:
        outcome = idles[-1].get("outcome")

    finish = last_assistant.get("finish") or last_assistant.get("rawFinish")
    complete = (finish == "stop" and outcome == "succeeded")

    error = None
    if outcome and outcome != "succeeded":
        error = f"OpenCode session outcome: {outcome}"
    elif not complete:
        error = f"OpenCode session did not complete (finish: {finish or 'unknown'})"

    texts = []
    tool_count = 0
    invoked = False
    inputs, outputs, cache_reads, cache_writes = [], [], [], []
    costs = []

    for m in assistants:
        c_cost = money(m.get("cost"))
        if c_cost is not None:
            costs.append(c_cost)
        tok = m.get("tokens")
        if isinstance(tok, dict):
            inputs.append(count(tok.get("input")))
            outputs.append(count(tok.get("output")))
            cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
            cache_reads.append(count(cache.get("read")))
            cache_writes.append(count(cache.get("write")))
        for c in m.get("content", []):
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and isinstance(c.get("text"), str):
                texts.append(c["text"])
            elif c.get("type") == "tool":
                tool_count += 1
                state = c.get("state") if isinstance(c.get("state"), dict) else {}
                if state.get("status") == "completed":
                    t_input = state.get("input") if isinstance(state.get("input"), dict) else {}
                    t_name = c.get("name") or c.get("tool")
                    target_id = t_input.get("id") or t_input.get("skill")
                    target_path = str(t_input.get("filePath") or t_input.get("path") or "")
                    if t_name == "skill" and _skill_matches(target_id, list(skill_names)):
                        invoked = True
                    elif t_name in {"read", "read_file"} and detect_skill_reference(
                        target_path, list(skill_names)
                    ):
                        invoked = True

    def sum_or_none(vals: list[Any]) -> int | None:
        return sum(vals) if vals and all(v is not None for v in vals) else None

    total_cost = (
        sum(costs)
        if costs and all(v is not None for v in costs)
        else money(info.get("cost"))
    )
    return {
        "response": "\n".join(texts),
        "error": error,
        "cost": total_cost,
        "input_tokens": sum_or_none(inputs),
        "output_tokens": sum_or_none(outputs),
        "cache_read_tokens": sum_or_none(cache_reads),
        "cache_creation_tokens": sum_or_none(cache_writes),
        "reported_total_tokens": None,
        "session_id": session_id,
        "num_turns": 1 if complete else None,
        "tool_calls": tool_count,
        "skill_invoked": invoked if skill_names else None,
    }


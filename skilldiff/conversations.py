"""Prompt sequencing for documented streaming CLI and Codex app-server sessions.

No subprocess ownership here: AgentRunner owns the deadline, pipes, and cleanup.
Each instance belongs to exactly one arm and never resumes a user's conversation.
"""

from typing import Any


class Conversation:
    def __init__(self, harness: str, prompts: list[str], thread_params=None):
        self.harness = harness
        self.prompts = prompts
        self.thread_params = thread_params or {}
        self.index = 0
        self.done = False
        self.thread_id = None
        self.turn_id = None
        self.session_id = None

    def start(self) -> list[dict[str, Any]]:
        if self.harness == "codex":
            return [{"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "skilldiff", "version": "1"}}}]
        return [self._prompt()]

    def _prompt(self) -> dict[str, Any]:
        text = self.prompts[self.index]
        if self.harness == "codex":
            self.turn_id = None
            return {"id": 3 + self.index, "method": "turn/start", "params": {
                "threadId": self.thread_id, "input": [{"type": "text", "text": text,
                                                       "text_elements": []}]}}
        if self.harness == "claude":
            return {"type": "user", "message": {"role": "user", "content": text},
                    "parent_tool_use_id": None}
        return {"event": "user", "message": {"content": text}}

    def accept(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        if self.harness in {"claude", "antigravity"}:
            result = event.get("result") if self.harness == "antigravity" else event
            current_id = (result.get("conversation_id") if isinstance(result, dict)
                          and self.harness == "antigravity" else event.get("session_id"))
            if current_id:
                if self.session_id and current_id != self.session_id:
                    raise RuntimeError("Conversation returned an unexpected session ID")
                self.session_id = current_id
        completed = False
        failed = False
        if self.harness == "codex":
            if "id" in event and "error" in event:
                raise RuntimeError(f"Codex app-server error: {event['error']}")
            if event.get("id") == 1:
                return [{"method": "initialized"}, {"id": 2, "method": "thread/start",
                        "params": self.thread_params}]
            if event.get("id") == 2:
                self.thread_id = event["result"]["thread"]["id"]
                return [self._prompt()]
            if event.get("id") == 3 + self.index:
                self.turn_id = event["result"]["turn"]["id"]
            params = event.get("params") or {}
            if event.get("method") == "turn/completed":
                turn = params.get("turn") or {}
                if params.get("threadId") != self.thread_id or turn.get("id") != self.turn_id:
                    raise RuntimeError("Codex completed an unexpected thread or turn")
                completed = True
                failed = turn.get("status") != "completed"
            if event.get("method") == "error" and not params.get("willRetry", False):
                completed = failed = True
        elif self.harness == "claude" and event.get("type") == "result":
            completed = True
            failed = bool(event.get("is_error") or
                          str(event.get("subtype", "")).startswith("error"))
        elif self.harness == "antigravity" and event.get("event") == "result":
            completed = True
            failed = (event.get("result") or {}).get("status") != "SUCCESS"
        if event.get("type") == "error" or event.get("event") == "error":
            completed = failed = True
        if completed:
            self.index += 1
            self.done = failed or self.index == len(self.prompts)
            if not self.done:
                return [self._prompt()]
        return []

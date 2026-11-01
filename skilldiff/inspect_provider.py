"""An inspect_ai model provider that answers through the signed-in agent CLIs.

Inspect Scout's LLM scanners call ``get_model(...).generate(...)``, which
normally needs a provider API key. This provider routes each generate call
through skilldiff's harness runner instead, so Claude Code, Codex, OpenCode,
and Antigravity subscriptions can judge transcripts. Model ids look like
``skilldiff/claude/claude-sonnet-5-5`` or ``skilldiff/codex/gpt-6.1-sol``.

Each call is one headless session with tools disabled where the CLI allows it
(Claude ``--tools ""``; OpenCode and Antigravity refuse tool prompts; Codex runs
read-only), in a throwaway workspace. The provider is registered through the
``inspect_ai`` entry point, so ``scout scan --model skilldiff/...`` works too.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from inspect_ai.model import (
    ChatMessage,
    ChatMessageSystem,
    GenerateConfig,
    ModelAPI,
    ModelOutput,
    ModelUsage,
    modelapi,
)
from inspect_ai.tool import ToolChoice, ToolInfo

from skilldiff.config import AntigravityConfig, ClaudeConfig, CodexConfig, OpenCodeConfig

HARNESSES = ("claude", "codex", "opencode", "antigravity")
PROVIDER = "skilldiff"
DEFAULT_TIMEOUT = 600.0
# Headless CLI sessions per harness at a time; Scout fans transcripts out widely.
MAX_CONNECTIONS = 2


def model_id(harness: str, model: str) -> str:
    """The inspect_ai model id that judges through this harness and model."""
    return f"{PROVIDER}/{harness}/{model}"


def parse_model_name(name: str) -> tuple[str, str]:
    """Split ``<harness>/<model>`` (the part after ``skilldiff/``)."""
    harness, _, model = name.partition("/")
    harness = harness.lower().strip()
    if harness == "agy":
        harness = "antigravity"
    if harness not in HARNESSES or not model:
        raise ValueError(
            f"skilldiff model ids are skilldiff/<harness>/<model> with harness one of "
            f"{', '.join(HARNESSES)}; got 'skilldiff/{name}'"
        )
    return harness, model


def judge_config(harness: str) -> Any:
    """A harness config for a one-turn, tool-free judge session."""
    if harness == "claude":
        return ClaudeConfig(sandbox=False, isolate=True, max_turns=1, max_budget_usd=None,
                            allowed_tools=[], extra_args=["--tools", ""])
    if harness == "codex":
        return CodexConfig(sandbox="read-only", network_access=False)
    if harness == "opencode":
        # Headless runs auto-reject permission prompts, so tools never execute.
        return OpenCodeConfig(dangerously_skip_permissions=False)
    return AntigravityConfig(dangerously_skip_permissions=False)


def render_prompt(messages: list[ChatMessage]) -> str:
    """Flatten a chat into one prompt: system text first, then labeled turns."""
    system = [m.text for m in messages if isinstance(m, ChatMessageSystem) and m.text]
    turns = []
    for message in messages:
        if isinstance(message, ChatMessageSystem):
            continue
        text = message.text
        if not text:
            continue
        turns.append(f"{message.role.upper()}:\n{text}" if len(messages) > 1 else text)
    parts = []
    if system:
        parts.append("SYSTEM INSTRUCTIONS:\n" + "\n\n".join(system))
    parts.extend(turns)
    parts.append(
        "Answer directly in text. Do not run tools, read files, or change anything."
    )
    return "\n\n".join(parts)


@modelapi(name=PROVIDER)
class SkillDiffCLI(ModelAPI):
    def __init__(
        self,
        model_name: str,
        base_url: str | None = None,
        api_key: str | None = None,
        config: GenerateConfig = GenerateConfig(),
        **model_args: Any,
    ) -> None:
        super().__init__(model_name, base_url, api_key, [], config)
        self.harness, self.model = parse_model_name(model_name)
        self.timeout = float(model_args.get("timeout", DEFAULT_TIMEOUT))
        from skilldiff.runner import AgentRunner

        self.runner = AgentRunner()

    def max_connections(self) -> int:
        return MAX_CONNECTIONS

    def connection_key(self) -> str:
        return self.harness

    async def generate(
        self,
        input: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        prompt = render_prompt(input)
        harness_cfg = judge_config(self.harness)

        def run() -> Any:
            with tempfile.TemporaryDirectory(prefix="skilldiff-judge-") as tmp:
                cwd = Path(tmp) / "workspace"
                cwd.mkdir()
                return self.runner.run(
                    prompt, cwd, self.model, harness_cfg, harness=self.harness,
                    timeout=self.timeout,
                )

        result = await asyncio.to_thread(run)
        if result.status != "ok" or result.error:
            raise RuntimeError(
                f"{self.harness} judge session failed: {result.error or result.status}"
            )
        text = (result.response or "").strip()
        output = ModelOutput.from_content(self.model_name, text)
        output.usage = ModelUsage(
            input_tokens=int(result.input_tokens or 0),
            output_tokens=int(result.output_tokens or 0),
            total_tokens=int(result.input_tokens or 0) + int(result.output_tokens or 0),
            input_tokens_cache_read=result.cache_read_tokens,
            input_tokens_cache_write=result.cache_creation_tokens,
            total_cost=result.cost,
        )
        return output

"""Token-based API-equivalent pricing for subscription runs.

On subscription auth the harness bills $0 at the margin, so the cost column
would be useless for comparisons. Instead skilldiff recomputes an
API-equivalent cost from the reported token counts with this versioned rate
table. Rates were looked up on the web on PRICING_DATE (see SOURCES) and are
checked in so that old reports stay reproducible. The evaluating agent must
verify them against current provider pages before a full run
(`skilldiff prices`) and override stale entries in `skilldiff.yaml`.

Assumptions, stated plainly:
- Anthropic cache-creation tokens use the 5-minute cache-write rate.
- Providers with no documented cache-write price reuse the base input rate
  for cache-creation tokens.
- DeepSeek uses peak rates (off-peak is half price at specific UTC hours;
  per-run timing is not tracked, so peak is the conservative choice).
- Gemini storage and grounding fees are ignored; only token rates apply.
"""

from __future__ import annotations

from typing import Any, Optional

PRICING_VERSION = "2026-09-26"
PRICING_DATE = "2026-09-26"

SOURCES = {
    "anthropic": "https://platform.claude.com/docs/en/about-claude/pricing",
    "openai-codex": "https://developers.openai.com/api/docs/models/gpt-5-codex",
    "deepseek": "https://api-docs.deepseek.com/quick_start/pricing/",
    "gemini": "https://ai.google.dev/gemini-api/docs/pricing",
}

# USD per 1M tokens.
RATES: dict[str, dict[str, float]] = {
    "claude-sonnet-5": {
        "input": 2.00,
        "cache_read": 0.20,
        "cache_write": 2.50,
        "output": 10.00,
    },
    "claude-opus-5": {
        "input": 5.00,
        "cache_read": 0.50,
        "cache_write": 6.25,
        "output": 25.00,
    },
    "claude-haiku-4.5": {
        "input": 1.00,
        "cache_read": 0.10,
        "cache_write": 1.25,
        "output": 5.00,
    },
    "gpt-5-codex": {
        "input": 1.25,
        "cache_read": 0.125,
        "cache_write": 1.25,
        "output": 10.00,
    },
    "gpt-5.3-codex": {
        "input": 1.75,
        "cache_read": 0.175,
        "cache_write": 1.75,
        "output": 14.00,
    },
    "deepseek-v4-pro-peak": {
        "input": 1.32,
        "cache_read": 0.044,
        "cache_write": 1.32,
        "output": 3.96,
    },
    "gemini-3.8-flash": {
        "input": 0.75,
        "cache_read": 0.075,
        "cache_write": 0.75,
        "output": 3.75,
    },
}


def normalize_model(model: str) -> Optional[str]:
    """Map a harness model alias to a pricing-table key. None if unknown."""
    m = (model or "").strip().lower()
    for prefix in ("anthropic/", "opencode-go/", "opencode/", "openai/"):
        if m.startswith(prefix):
            m = m[len(prefix) :]
    if m in RATES:
        return m
    if "sonnet" in m:
        return "claude-sonnet-5"
    if "opus" in m:
        return "claude-opus-5"
    if "haiku" in m:
        return "claude-haiku-4.5"
    if "5.3" in m and "codex" in m:
        return "gpt-5.3-codex"
    if "codex" in m or "gpt-5" in m:
        return "gpt-5-codex"
    if "deepseek" in m:
        return "deepseek-v4-pro-peak"
    if "gemini" in m or "flash" in m:
        return "gemini-3.8-flash"
    return None


def get_rates(model: str, overrides: Optional[dict[str, Any]] = None) -> Optional[dict[str, float]]:
    key = normalize_model(model)
    if key is None:
        return None
    rates = dict(RATES[key])
    if overrides:
        entry = overrides.get(key) or overrides.get(model)
        if isinstance(entry, dict):
            for field in ("input", "cache_read", "cache_write", "output"):
                if entry.get(field) is not None:
                    try:
                        rates[field] = float(entry[field])
                    except (TypeError, ValueError):
                        continue
    return rates


def compute_cost_usd(
    model: str,
    input_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    cache_creation_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    overrides: Optional[dict[str, Any]] = None,
) -> Optional[float]:
    """API-equivalent cost from token counts. None when model unknown."""
    rates = get_rates(model, overrides)
    if rates is None:
        return None
    total = (
        (input_tokens or 0) * rates["input"]
        + (cache_read_tokens or 0) * rates["cache_read"]
        + (cache_creation_tokens or 0) * rates["cache_write"]
        + (output_tokens or 0) * rates["output"]
    ) / 1_000_000
    return round(total, 6)


def price_run(
    model: str,
    harness: str,
    config: Any = None,
    input_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    cache_creation_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    harness_cost: Optional[float] = None,
    status: Optional[str] = "ok",
    overrides: Optional[dict[str, Any]] = None,
) -> tuple[Optional[float], str]:
    """Return (cost, basis) for one run.

    Subscription runs with known models and clean status get token-based
    API-equivalent cost. Everything else keeps the harness cost.
    """
    if not subscription_mode(harness, config) or status not in (None, "ok"):
        return harness_cost, "harness"
    token_cost = compute_cost_usd(
        model,
        input_tokens,
        cache_read_tokens,
        cache_creation_tokens,
        output_tokens,
        overrides,
    )
    if token_cost is not None:
        return token_cost, f"token-pricing/{PRICING_VERSION}"
    if normalize_model(model) is None:
        return harness_cost, "harness-unknown-model"
    return harness_cost, "harness"


def subscription_mode(harness: str, config: Any = None) -> bool:
    """True when the harness bills a subscription, not per-token API usage."""
    h = (harness or "").lower().strip()
    if config is None:
        return True
    try:
        if h == "claude":
            return getattr(config.claude, "auth", "subscription") == "subscription"
        if h == "codex":
            return getattr(config.codex, "auth", "stored") in {"stored", "subscription"}
        if h == "opencode":
            return getattr(config.opencode, "service", "go") == "go"
        if h in {"antigravity", "agy"}:
            return True
    except AttributeError:
        return True
    return True

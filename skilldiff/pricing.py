"""Model rate lookup and API-equivalent cost calculations.

Rates are per 1M tokens. Claude models carry their own source and checked date so a
saved run can reproduce the estimate without a network lookup. Haiku 5.5 is tiered by
prompt size; a tier can only be priced exactly when the harness recorded per-request
usage. Without that, the decision cost is N/A by default.
"""

from __future__ import annotations

import math
import re
from typing import Any, Optional, cast

PRICING_RATE_KEYS = ("input", "output", "cache_read", "cache_write")
PRICING_TIER_KEYS = frozenset({"max_prompt_tokens", *PRICING_RATE_KEYS})
PRICING_ENTRY_KEYS = frozenset({"tiers", *PRICING_RATE_KEYS})

CLAUDE_PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
HAIKU_SOURCE = "https://www.anthropic.com/claude-haiku-5-5"

# The current Claude model IDs. Bare family names are rejected by config loading;
# these aliases stay only for internal lookup and direct ExperimentConfig callers.
MODEL_ALIASES = {
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
    "opus": "claude-opus-5-5",
}

# Five-minute cache writes. One-hour cache writes cost more and are not modeled
# by the current single cache_write rate.
BUILTIN_CLAUDE_RATES: dict[str, dict[str, Any]] = {
    "claude-3-5-sonnet": {
        "source": CLAUDE_PRICING_SOURCE,
        "date": "2026-10-01",
        "currency": "USD",
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
    "claude-3-5-haiku": {
        "source": CLAUDE_PRICING_SOURCE,
        "date": "2026-10-01",
        "currency": "USD",
        "input": 0.80,
        "output": 4.00,
        "cache_read": 0.08,
        "cache_write": 1.00,
    },
    "claude-3-opus": {
        "source": CLAUDE_PRICING_SOURCE,
        "date": "2026-10-01",
        "currency": "USD",
        "input": 15.00,
        "output": 75.00,
        "cache_read": 1.50,
        "cache_write": 18.75,
    },
    "claude-3-haiku": {
        "source": CLAUDE_PRICING_SOURCE,
        "date": "2026-10-01",
        "currency": "USD",
        "input": 0.25,
        "output": 1.25,
        "cache_read": 0.03,
        "cache_write": 0.30,
    },
    "claude-haiku-5-5": {
        "source": HAIKU_SOURCE,
        "date": "2026-10-07",
        "currency": "USD",
        "tiers": [
            {
                "max_prompt_tokens": 100_000,
                "input": 0.10,
                "output": 0.50,
                "cache_read": 0.01,
                "cache_write": 0.125,
            },
            {
                "input": 0.50,
                "output": 2.50,
                "cache_read": 0.05,
                "cache_write": 0.625,
            },
        ],
    },
    "claude-sonnet-5-5": {
        "source": CLAUDE_PRICING_SOURCE,
        "date": "2026-10-19",
        "currency": "USD",
        "input": 2.00,
        "output": 10.00,
        "cache_read": 0.10,
        "cache_write": 2.50,
    },
    "claude-opus-5-5": {
        "source": CLAUDE_PRICING_SOURCE,
        "date": "2026-10-19",
        "currency": "USD",
        "input": 4.00,
        "output": 20.00,
        "cache_read": 0.20,
        "cache_write": 5.00,
    },
}

BUILTIN_HARNESS_RATES: dict[str, dict[str, Any]] = {
    "gpt-6.1-sol": {
        "source": "https://openai.com/api/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 2.00,
        "output": 10.00,
        "cache_read": 0.10,
        "cache_write": 2.50,
    },
    "gpt-6-luna": {
        "source": "https://openai.com/api/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 0.10,
        "output": 0.50,
        "cache_read": 0.01,
        "cache_write": 0.125,
    },
    "gpt-6-astra": {
        "source": "https://openai.com/api/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 10.00,
        "output": 50.00,
        "cache_read": 1.00,
        "cache_write": 12.50,
    },
    "gemini-3.8-flash": {
        "source": "https://ai.google.dev/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 0.75,
        "output": 3.75,
        "cache_read": 0.075,
        "cache_write": 0.50,
    },
    "gemini-3.7-flash": {
        "source": "https://ai.google.dev/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 0.75,
        "output": 3.75,
        "cache_read": 0.075,
        "cache_write": 0.50,
    },
    "gemini-4-argon": {
        "source": "https://ai.google.dev/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 2.00,
        "output": 10.00,
        "cache_read": 0.20,
        "cache_write": 2.00,
    },
    "deepseek-v4-pro": {
        "source": "https://api-docs.deepseek.com/quick_start/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 0.66,
        "output": 1.98,
        "cache_read": 0.022,
        "cache_write": 0.66,
    },
    "deepseek-flash": {
        "source": "https://api-docs.deepseek.com/quick_start/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 0.15,
        "output": 0.60,
        "cache_read": 0.003,
        "cache_write": 0.15,
    },
    "gpt-4o": {
        "source": "https://openai.com/api/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 2.50,
        "output": 10.00,
        "cache_read": 1.25,
        "cache_write": 2.50,
    },
    "gpt-4o-mini": {
        "source": "https://openai.com/api/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 0.15,
        "output": 0.60,
        "cache_read": 0.075,
        "cache_write": 0.15,
    },
    "o3-mini": {
        "source": "https://openai.com/api/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 1.10,
        "output": 4.40,
        "cache_read": 0.55,
        "cache_write": 1.10,
    },
    "o1": {
        "source": "https://openai.com/api/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 15.00,
        "output": 60.00,
        "cache_read": 7.50,
        "cache_write": 15.00,
    },
    "o1-mini": {
        "source": "https://openai.com/api/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 1.10,
        "output": 4.40,
        "cache_read": 0.55,
        "cache_write": 1.10,
    },
    "gemini-2.0-flash": {
        "source": "https://ai.google.dev/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 0.10,
        "output": 0.40,
        "cache_read": 0.025,
        "cache_write": 0.10,
    },
    "gemini-1.5-pro": {
        "source": "https://ai.google.dev/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 1.25,
        "output": 5.00,
        "cache_read": 0.3125,
        "cache_write": 1.25,
    },
    "gemini-1.5-flash": {
        "source": "https://ai.google.dev/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 0.075,
        "output": 0.30,
        "cache_read": 0.01875,
        "cache_write": 0.075,
    },
    "gemini-2.5-pro": {
        "source": "https://ai.google.dev/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 1.25,
        "output": 5.00,
        "cache_read": 0.3125,
        "cache_write": 1.25,
    },
    "deepseek-chat": {
        "source": "https://api-docs.deepseek.com/quick_start/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 0.14,
        "output": 0.28,
        "cache_read": 0.014,
        "cache_write": 0.14,
    },
    "deepseek-reasoner": {
        "source": "https://api-docs.deepseek.com/quick_start/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 0.55,
        "output": 2.19,
        "cache_read": 0.14,
        "cache_write": 0.55,
    },
    "deepseek-v3": {
        "source": "https://api-docs.deepseek.com/quick_start/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 0.14,
        "output": 0.28,
        "cache_read": 0.014,
        "cache_write": 0.14,
    },
    "deepseek-r1": {
        "source": "https://api-docs.deepseek.com/quick_start/pricing",
        "date": "2026-10-01",
        "currency": "USD",
        "input": 0.55,
        "output": 2.19,
        "cache_read": 0.14,
        "cache_write": 0.55,
    },
}

_DATE_SUFFIX = re.compile(r"-\d{8}$")


def normalize_model_id(model: str) -> str:
    """The rate-table key for a configured or served model ID."""
    value = str(model or "").strip()
    if value.lower() in MODEL_ALIASES:
        return MODEL_ALIASES[value.lower()]
    for prefix in ("opencode-go/", "opencode/"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    normalized = _DATE_SUFFIX.sub("", value)
    aliases = {
        "gpt-6": "gpt-6.1-sol",
        "gpt-6-sol": "gpt-6.1-sol",
        "gemini-3.8": "gemini-3.8-flash",
        "gemini-3.8-flash-medium": "gemini-3.8-flash",
        "gemini-3.7": "gemini-3.7-flash",
        "gemini-3.7-flash-medium": "gemini-3.7-flash",
        "deepseek-pro": "deepseek-v4-pro",
        "deepseek-v4": "deepseek-v4-pro",
        "deepseek-v4.1-flash": "deepseek-flash",
    }
    return aliases.get(normalized.lower(), normalized)


def is_bare_family_alias(model: str) -> bool:
    return str(model or "").strip().lower() in MODEL_ALIASES


def builtin_rate_entry(model: str) -> Optional[dict[str, Any]]:
    norm = normalize_model_id(model)
    entry = BUILTIN_CLAUDE_RATES.get(norm) or BUILTIN_HARNESS_RATES.get(norm)
    return dict(entry) if entry else None


def _with_metadata(
    entry: dict[str, Any],
    *,
    model: str,
    source: str,
    date: str,
    currency: str,
) -> dict[str, Any]:
    out = {key: value for key, value in entry.items() if key in PRICING_ENTRY_KEYS}
    out.setdefault("source", source)
    out.setdefault("date", date)
    out.setdefault("currency", currency)
    out["model_id"] = normalize_model_id(model)
    return out


def merge_pricing(
    user_pricing: dict[str, Any],
    models: list[str],
    harness: str,
) -> tuple[dict[str, Any], list[str]]:
    """Fill gaps in a user pricing block from the built-in Claude table.

    Returns (resolved pricing, unpriced configured models). Explicit user rates for a
    model win; missing rates are filled only for the Claude harness.
    """
    resolved: dict[str, Any] = {}
    user_rates = dict((user_pricing or {}).get("rates") or {})
    source = str((user_pricing or {}).get("source") or "").strip()
    date = str((user_pricing or {}).get("date") or "").strip()
    currency = str((user_pricing or {}).get("currency") or "USD").strip().upper() or "USD"
    used_builtin = False
    unpriced: list[str] = []

    for model in models:
        key = str(model)
        user_entry = user_rates.get(key)
        if user_entry is None:
            # Also accept a canonical/base key for a dated snapshot.
            user_entry = user_rates.get(normalize_model_id(key))
        builtin = builtin_rate_entry(key)
        if user_entry is not None:
            resolved[key] = _with_metadata(
                dict(user_entry),
                model=key,
                source=source or CLAUDE_PRICING_SOURCE,
                date=date or "",
                currency=currency,
            )
        elif builtin is not None:
            used_builtin = True
            resolved[key] = _with_metadata(
                builtin,
                model=key,
                source=str(builtin.get("source") or CLAUDE_PRICING_SOURCE),
                date=str(builtin.get("date") or ""),
                currency=str(builtin.get("currency") or "USD"),
            )
        else:
            unpriced.append(key)

    if not resolved and not user_rates:
        return {}, unpriced

    default_src = (
        str(resolved[models[0]].get("source") or CLAUDE_PRICING_SOURCE)
        if resolved and models and models[0] in resolved
        else (CLAUDE_PRICING_SOURCE if used_builtin else "")
    )
    out: dict[str, Any] = {
        "source": source or default_src,
        "date": date or (
            max(str(entry.get("date") or "") for entry in resolved.values())
            if resolved
            else ""
        ),
        "currency": currency,
        "rates": resolved,
    }
    if used_builtin:
        out["builtin"] = True
    if unpriced:
        out["unpriced"] = unpriced
    return out, unpriced


def rate_entry(pricing: dict[str, Any], model: str) -> Optional[dict[str, Any]]:
    rates = (pricing or {}).get("rates") or {}
    entry = rates.get(str(model))
    if entry is None:
        entry = rates.get(normalize_model_id(model))
    if entry is not None:
        return cast(Optional[dict[str, Any]], entry)
    return builtin_rate_entry(model) if not rates else None


def tier_for_prompt(entry: dict[str, Any], prompt_tokens: int) -> dict[str, float]:
    tiers = entry.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        return entry
    for tier in tiers:
        maximum = tier.get("max_prompt_tokens")
        if maximum is None or prompt_tokens <= int(maximum):
            return cast(dict[str, float], tier)
    return cast(dict[str, float], tiers[-1])


def prompt_tokens_for_usage(usage: dict[str, Any]) -> int:
    total = 0
    for key in ("input_tokens", "cache_read_tokens", "cache_creation_tokens"):
        try:
            total += int(usage.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _finite_usage(usage: dict[str, Any]) -> bool:
    # Input and output must be measured. Cache counters are optional and price as
    # zero when the harness omits them (Codex does not report cache writes).
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            if not math.isfinite(value) or value < 0:
                return False
        except OverflowError:
            return False
    for key in ("cache_read_tokens", "cache_creation_tokens"):
        value = usage.get(key)
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            if not math.isfinite(value) or value < 0:
                return False
        except OverflowError:
            return False
    return True


def price_usage(entry: dict[str, Any], usage: dict[str, Any], prompt_size: int) -> float:
    rates = tier_for_prompt(entry, prompt_size)
    return sum(
        float(usage.get(field) or 0) * float(rates.get(rate) or 0)
        for field, rate in (
            ("input_tokens", "input"),
            ("output_tokens", "output"),
            ("cache_read_tokens", "cache_read"),
            ("cache_creation_tokens", "cache_write"),
        )
    ) / 1_000_000.0


def price_run(
    entry: dict[str, Any],
    run: dict[str, Any],
    tier_policy: str = "na",
) -> dict[str, Any]:
    """Price one run.

    Returns {"cost": float|None, "basis": "flat"|"request"|"upper_bound"|"unknown"}.
    Tiered runs without complete per-request usage stay unknown by default. With
    tier_policy="upper_bound" they get a labelled estimate that decisions ignore.
    """
    tiers = entry.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        usage = {
            "input_tokens": run.get("input_tokens"),
            "output_tokens": run.get("output_tokens"),
            "cache_read_tokens": run.get("cache_read_tokens"),
            "cache_creation_tokens": run.get("cache_creation_tokens"),
        }
        if not _finite_usage(usage):
            return {"cost": None, "basis": "unknown"}
        return {"cost": price_usage(entry, usage, 0), "basis": "flat"}

    requests = run.get("request_usage")
    if isinstance(requests, list) and requests:
        costs: list[float] = []
        for request in requests:
            if not isinstance(request, dict) or not _finite_usage(request):
                costs = []
                break
            costs.append(price_usage(entry, request, prompt_tokens_for_usage(request)))
        if costs:
            return {"cost": sum(costs), "basis": "request"}

    if tier_policy == "upper_bound":
        usage = {
            "input_tokens": run.get("input_tokens"),
            "output_tokens": run.get("output_tokens"),
            "cache_read_tokens": run.get("cache_read_tokens"),
            "cache_creation_tokens": run.get("cache_creation_tokens"),
        }
        if not _finite_usage(usage):
            return {"cost": None, "basis": "unknown"}
        # Aggregate prompt tokens are a loose upper bound on each request's size.
        size = prompt_tokens_for_usage(usage)
        return {"cost": price_usage(entry, usage, size), "basis": "upper_bound"}
    return {"cost": None, "basis": "unknown"}


def one_call_cost_estimate(
    pricing: dict[str, Any],
    model: str,
    *,
    prompt_size: int = 32_000,
    output_tokens: int = 8_000,
) -> Optional[float]:
    """A conservative cost for one tiny call, used to size preflight budgets."""
    entry = rate_entry(pricing, model)
    if not entry:
        return None
    usage = {
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": prompt_size,
        "output_tokens": output_tokens,
    }
    # Use the largest applicable tier so a tiered model cannot underrun its cap.
    tiers = entry.get("tiers") or []
    if tiers:
        last = tiers[-1]
        maximum = last.get("max_prompt_tokens")
        size = (int(maximum) + 1) if maximum else 1_000_000
        return price_usage(entry, usage, size)
    return price_usage(entry, usage, prompt_size)


def unpriced_models(pricing: dict[str, Any], models: list[str]) -> list[str]:
    return [str(model) for model in models if rate_entry(pricing, str(model)) is None]

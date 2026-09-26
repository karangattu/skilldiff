from skilldiff.config import ClaudeConfig, CodexConfig, ExperimentConfig, OpenCodeConfig
from skilldiff.pricing import (
    PRICING_VERSION,
    compute_cost_usd,
    normalize_model,
    price_run,
    subscription_mode,
)


def _config(harness="claude", **kw):
    return ExperimentConfig(
        name="x",
        skill=None,
        models=["sonnet"],
        tasks_patterns=[],
        harness=harness,
        claude=ClaudeConfig(**kw.get("claude", {})),
        codex=CodexConfig(**kw.get("codex", {})),
        opencode=OpenCodeConfig(**kw.get("opencode", {})),
    )


def test_normalize_model_aliases():
    assert normalize_model("sonnet") == "claude-sonnet-5"
    assert normalize_model("opus") == "claude-opus-5"
    assert normalize_model("anthropic/claude-sonnet-5") == "claude-sonnet-5"
    assert normalize_model("opencode-go/deepseek-v4-pro") == "deepseek-v4-pro-peak"
    assert normalize_model("gpt-5-codex") == "gpt-5-codex"
    assert normalize_model("gemini-3.8-flash-medium") == "gemini-3.8-flash"
    assert normalize_model("some-future-model-99") is None


def test_compute_cost_usd_sonnet():
    # 1M input + 1M output at Sonnet 5 rates = $2 + $10.
    assert compute_cost_usd("sonnet", 1_000_000, 0, 0, 1_000_000) == 12.0
    # Cache read at 10%: 1M cached = $0.20.
    assert compute_cost_usd("sonnet", 0, 1_000_000, 0, 0) == 0.2
    assert compute_cost_usd("mystery-model", 100, 0, 0, 50) is None


def test_subscription_mode_by_auth():
    assert subscription_mode("claude", _config("claude")) is True
    assert subscription_mode("claude", _config("claude", claude={"auth": "api_key"})) is False
    assert subscription_mode("codex", _config("codex")) is True
    assert subscription_mode("codex", _config("codex", codex={"auth": "api_key"})) is False
    assert subscription_mode("opencode", _config("opencode")) is True
    assert subscription_mode("opencode", _config("opencode", opencode={"service": "zen"})) is False


def test_price_run_subscription_recomputes():
    cost, basis = price_run(
        "sonnet", "claude", _config("claude"), 1_000_000, 0, 0, 0, harness_cost=0.04
    )
    assert cost == 2.0
    assert basis == f"token-pricing/{PRICING_VERSION}"


def test_price_run_api_key_keeps_harness():
    cfg = _config("claude", claude={"auth": "api_key"})
    cost, basis = price_run("sonnet", "claude", cfg, 1_000_000, 0, 0, 0, harness_cost=0.04)
    assert cost == 0.04
    assert basis == "harness"


def test_price_run_unknown_model_keeps_harness_with_flag():
    cost, basis = price_run(
        "mystery-model", "claude", _config("claude"), 100, 0, 0, 50, harness_cost=0.04
    )
    assert cost == 0.04
    assert basis == "harness-unknown-model"


def test_price_run_failed_status_keeps_harness():
    cost, basis = price_run(
        "sonnet",
        "claude",
        _config("claude"),
        100,
        0,
        0,
        50,
        harness_cost=0.04,
        status="error",
    )
    assert cost == 0.04
    assert basis == "harness"


def test_pricing_overrides():
    overrides = {"claude-sonnet-5": {"input": 4.0}}
    assert compute_cost_usd("sonnet", 1_000_000, 0, 0, 0, overrides) == 4.0

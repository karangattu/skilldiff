import math

import pytest

from skilldiff.config import load_experiment, parse_pricing
from skilldiff.pricing import (
    builtin_rate_entry,
    merge_pricing,
    one_call_cost_estimate,
    price_run,
    rate_entry,
)


def _task_config(tmp_path, model="claude-haiku-5-5", extra=""):
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\n")
    (tmp_path / "task.yaml").write_text("id: one\nprompt: Do it\n")
    config = tmp_path / "skilldiff.yaml"
    config.write_text(
        "name: t\nskill: ./skill\n"
        f"models: [{model}]\ntasks: [./task.yaml]\n"
        "harness: claude\n" + extra
    )
    return config


def _usage(input_tokens=0, output_tokens=0, cache_read=0, cache_write=0):
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_write,
    }


def test_haiku_tiers_use_each_request_prompt_size():
    entry = builtin_rate_entry("claude-haiku-5-5")
    assert entry is not None
    low = _usage(input_tokens=50_000, output_tokens=10_000)
    high = _usage(input_tokens=150_000, output_tokens=10_000)
    priced = price_run(entry, {**low, "request_usage": [low, high]})
    expected = (
        50_000 * 0.10 + 10_000 * 0.50 + 150_000 * 0.50 + 10_000 * 2.50
    ) / 1_000_000
    assert priced["basis"] == "request"
    assert priced["cost"] == pytest.approx(expected)


def test_tiered_run_without_request_usage_is_na_for_decisions():
    entry = builtin_rate_entry("claude-haiku-5-5")
    assert entry is not None
    run = _usage(input_tokens=50_000, output_tokens=10_000)
    assert price_run(entry, run) == {"cost": None, "basis": "unknown"}
    upper = price_run(entry, run, tier_policy="upper_bound")
    assert upper["basis"] == "upper_bound"
    # Aggregate prompt size is an upper bound on every request, so 50k stays low.
    assert upper["cost"] == pytest.approx(
        (50_000 * 0.10 + 10_000 * 0.50) / 1_000_000
    )


def test_flat_rates_price_aggregate_usage():
    entry = builtin_rate_entry("claude-sonnet-5-5")
    assert entry is not None
    priced = price_run(entry, _usage(1_000_000, 100_000, 2_000_000, 0))
    assert priced["basis"] == "flat"
    assert priced["cost"] == pytest.approx(2.0 + 1.0 + 0.2)


def test_builtin_table_is_used_without_a_pricing_block(tmp_path):
    cfg, _ = load_experiment(_task_config(tmp_path))
    assert cfg.cost_basis == "api-equivalent"
    assert cfg.pricing["builtin"] is True
    assert cfg.pricing["rates"]["claude-haiku-5-5"]["tiers"][0]["input"] == 0.10
    assert rate_entry(cfg.pricing, "claude-haiku-5-5") is not None


def test_bare_family_aliases_fail_with_the_replacement(tmp_path):
    config = _task_config(tmp_path, model="haiku")
    with pytest.raises(ValueError, match="claude-haiku-5-5"):
        load_experiment(config)


def test_unknown_model_with_implicit_api_basis_is_recorded_for_check(tmp_path):
    cfg, _ = load_experiment(_task_config(tmp_path, model="my-model"))
    assert cfg.cost_basis == "api-equivalent"
    assert cfg.pricing_unpriced == ["my-model"]


def test_explicit_api_equivalent_still_requires_rates(tmp_path):
    config = _task_config(
        tmp_path, model="my-model", extra="cost_basis: api-equivalent\n"
    )
    with pytest.raises(ValueError, match="requires recorded pricing"):
        load_experiment(config)


def test_explicit_pricing_overrides_the_builtin_entry(tmp_path):
    config = _task_config(
        tmp_path,
        extra=(
            "pricing:\n"
            "  source: https://example.com\n"
            '  date: "2026-10-20"\n'
            "  rates:\n"
            "    claude-haiku-5-5:\n"
            "      input: 9.0\n"
            "      output: 9.0\n"
            "      cache_read: 9.0\n"
            "      cache_write: 9.0\n"
        ),
    )
    cfg, _ = load_experiment(config)
    assert cfg.pricing["rates"]["claude-haiku-5-5"]["input"] == 9.0
    assert "tiers" not in cfg.pricing["rates"]["claude-haiku-5-5"]


def test_parse_pricing_accepts_tiers_and_rejects_bad_order():
    data = {
        "source": "https://example.com",
        "date": "2026-10-20",
        "rates": {
            "m": {
                "tiers": [
                    {"max_prompt_tokens": 100, **_usage_rates()},
                    {**_usage_rates()},
                ]
            }
        },
    }
    parsed = parse_pricing(data)
    assert parsed["rates"]["m"]["tiers"][0]["max_prompt_tokens"] == 100
    bad = {
        **data,
        "rates": {
            "m": {
                "tiers": [
                    {**_usage_rates()},
                    {"max_prompt_tokens": 100, **_usage_rates()},
                ]
            }
        },
    }
    with pytest.raises(ValueError, match="last tier"):
        parse_pricing(bad)


def _usage_rates():
    return {"input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_write": 1.0}


def test_one_call_estimate_uses_the_high_tier():
    pricing, _ = merge_pricing({}, ["claude-haiku-5-5"], "claude")
    estimate = one_call_cost_estimate(pricing, "claude-haiku-5-5")
    assert estimate is not None and math.isfinite(estimate)
    # 32k cache writes + 8k output at the over-100k tier.
    assert estimate == pytest.approx((32_000 * 0.625 + 8_000 * 2.50) / 1_000_000)


def test_tiered_report_summary_keeps_na_out_of_decisions_and_shows_upper_bound():
    from skilldiff.reporting.metrics import _api_equivalent_summary, decision_cost_runs

    pricing, _ = merge_pricing({}, ["claude-haiku-5-5"], "claude")
    run = {
        "model": "claude-haiku-5-5",
        "task_id": "t",
        "arm": "control",
        "repetition": 1,
        "status": "ok",
        "grade_status": "graded",
        "score": 1.0,
        **_usage(input_tokens=50_000, output_tokens=10_000),
    }
    strict = _api_equivalent_summary([run], pricing)
    assert strict["cost"] is None
    assert strict["unknown_basis"] == ["claude-haiku-5-5"]
    planned = _api_equivalent_summary([run], pricing, tier_policy="upper_bound")
    assert planned["cost"] is None
    assert planned["upper_bound_cost"] is not None
    decision = decision_cost_runs([run], {"cost_basis": "api-equivalent", "pricing": pricing})
    assert decision[0]["cost"] is None


def test_tier_policy_is_validated(tmp_path):
    config = _task_config(tmp_path, extra="tier_policy: upper_bound\n")
    cfg, _ = load_experiment(config)
    assert cfg.tier_policy == "upper_bound"
    config.write_text(config.read_text() + "tier_policy: guess\n")
    with pytest.raises(ValueError, match="tier_policy"):
        load_experiment(config)


def test_explicit_api_equivalent_uses_the_builtin_table(tmp_path):
    config = _task_config(
        tmp_path, extra="cost_basis: api-equivalent\n"
    )
    cfg, _ = load_experiment(config)
    assert cfg.cost_basis == "api-equivalent"
    assert cfg.pricing["builtin"] is True
    assert cfg.pricing_unpriced == []


def test_builtin_rates_for_codex_antigravity_opencode():
    for model in [
        "gpt-5-codex",
        "gemini-3.8-flash-medium",
        "gemini-3.7-flash-medium",
        "deepseek-v4-pro",
        "opencode-go/deepseek-v4-pro",
    ]:
        entry = builtin_rate_entry(model)
        assert entry is not None
        assert "input" in entry and "output" in entry

    pricing, unpriced = merge_pricing({}, ["gpt-5-codex"], "codex")
    assert unpriced == []
    assert pricing["builtin"] is True
    assert "gpt-5-codex" in pricing["rates"]

    pricing_open, unpriced_open = merge_pricing(
        {}, ["opencode-go/deepseek-v4-pro"], "opencode"
    )
    assert unpriced_open == []
    assert pricing_open["builtin"] is True
    assert "opencode-go/deepseek-v4-pro" in pricing_open["rates"]


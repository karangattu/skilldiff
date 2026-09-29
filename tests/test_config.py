from pathlib import Path

import pytest

from skilldiff.config import load_experiment, load_task, parse_pricing


def test_load_task_valid(tmp_path: Path):
    task_file = tmp_path / "task.yaml"
    task_file.write_text(
        "id: task-1\nprompt: Test prompt\nrepo: ./fixture\n"
        "grader:\n  type: command\n  command: python test.py\n"
    )
    task = load_task(task_file)
    assert task.id == "task-1"
    assert task.prompt == "Test prompt"
    assert task.repo == "./fixture"
    assert task.grader is not None
    assert task.grader.command == "python test.py"


def test_load_task_missing_prompt(tmp_path: Path):
    task_file = tmp_path / "invalid.yaml"
    task_file.write_text("id: bad\n")
    with pytest.raises(ValueError, match="prompt"):
        load_task(task_file)


def test_load_experiment_valid(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill")

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "t1.yaml").write_text("id: t1\nprompt: Do work\n")

    exp_file = tmp_path / "skilldiff.yaml"
    exp_file.write_text(
        "name: test-exp\nskill: ./skills/test-skill\n"
        "models:\n  - sonnet\ntasks:\n  - ./tasks/*.yaml\nruns: 2\n"
    )

    exp_cfg, tasks = load_experiment(exp_file)
    assert exp_cfg.name == "test-exp"
    assert exp_cfg.skill == skill_dir.resolve()
    assert exp_cfg.models == ["sonnet"]
    assert exp_cfg.runs == 2
    assert exp_cfg.claude.auth == "subscription"
    assert len(tasks) == 1
    assert tasks[0].id == "t1"


def test_load_experiment_missing_skill_md(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "empty-skill"
    skill_dir.mkdir(parents=True)

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "t1.yaml").write_text("id: t1\nprompt: Do work\n")

    exp_file = tmp_path / "skilldiff.yaml"
    exp_file.write_text(
        "name: test-exp\nskill: ./skills/empty-skill\n"
        "models:\n  - sonnet\ntasks:\n  - ./tasks/*.yaml\n"
    )

    with pytest.raises(FileNotFoundError, match="SKILL.md"):
        load_experiment(exp_file)


def test_load_experiment_no_tasks(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill")

    exp_file = tmp_path / "skilldiff.yaml"
    exp_file.write_text(
        "name: test-exp\nskill: ./skills/test-skill\n"
        "models:\n  - sonnet\ntasks:\n  - ./nonexistent/*.yaml\n"
    )

    with pytest.raises(ValueError, match="No task files matched"):
        load_experiment(exp_file)


def test_load_experiment_reads_claude_permissions(tmp_path: Path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Test")
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "task.yaml").write_text("id: one\nprompt: Do it\n")
    exp_file = tmp_path / "skilldiff.yaml"
    exp_file.write_text(
        "name: test\n"
        "skill: ./skill\n"
        "models:\n  - sonnet\n"
        "tasks:\n  - ./tasks/*.yaml\n"
        "claude:\n"
        "  auth: api_key\n"
        "  permission_mode: acceptEdits\n"
        "  allowed_tools:\n"
        "    - Bash(shiny docs *)\n"
    )

    config, _ = load_experiment(exp_file)

    assert config.claude.auth == "api_key"
    assert config.claude.permission_mode == "acceptEdits"
    assert config.claude.allowed_tools == ["Bash(shiny docs *)"]


def test_load_experiment_rejects_unknown_claude_auth(tmp_path: Path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Test")
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "task.yaml").write_text("id: one\nprompt: Do it\n")
    exp_file = tmp_path / "skilldiff.yaml"
    exp_file.write_text(
        "name: test\n"
        "skill: ./skill\n"
        "models:\n  - sonnet\n"
        "tasks:\n  - ./tasks/*.yaml\n"
        "claude:\n"
        "  auth: surprise\n"
    )

    with pytest.raises(ValueError, match="claude.auth"):
        load_experiment(exp_file)


def _pricing_yaml() -> str:
    return (
        "pricing:\n"
        "  source: https://example.com/pricing\n"
        '  date: "2026-09-27"\n'
        "  currency: USD\n"
        "  rates:\n"
        "    sonnet:\n"
        "      input: 3.0\n"
        "      output: 15.0\n"
        "      cache_read: 0.3\n"
        "      cache_write: 3.75\n"
    )


def test_load_experiment_pricing_block(tmp_path: Path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Test")
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "task.yaml").write_text("id: one\nprompt: Do it\n")
    exp_file = tmp_path / "skilldiff.yaml"
    exp_file.write_text(
        "name: test\n"
        "skill: ./skill\n"
        "models:\n  - sonnet\n"
        "tasks:\n  - ./tasks/*.yaml\n" + _pricing_yaml()
    )

    exp_cfg, _ = load_experiment(exp_file)
    assert exp_cfg.pricing["source"] == "https://example.com/pricing"
    assert exp_cfg.pricing["date"] == "2026-09-27"
    assert exp_cfg.pricing["currency"] == "USD"
    assert exp_cfg.pricing["rates"]["sonnet"]["input"] == 3.0
    assert exp_cfg.pricing["rates"]["sonnet"]["cache_write"] == 3.75


def test_parse_pricing_requires_provenance_and_complete_rates():
    assert parse_pricing(None) == {}
    assert parse_pricing({}) == {}

    with pytest.raises(ValueError, match="pricing.source"):
        parse_pricing({"date": "2026-09-27", "rates": {"m": _rates()}})
    with pytest.raises(ValueError, match="pricing.date"):
        parse_pricing({"source": "s", "rates": {"m": _rates()}})
    with pytest.raises(ValueError, match="pricing.rates"):
        parse_pricing({"source": "s", "date": "2026-09-27", "rates": {}})
    with pytest.raises(ValueError, match="cache_read"):
        parse_pricing(
            {
                "source": "s",
                "date": "2026-09-27",
                "rates": {"m": {"input": 1.0, "output": 2.0, "cache_write": 1.0}},
            }
        )
    with pytest.raises(ValueError, match="per 1M tokens"):
        parse_pricing(
            {
                "source": "s",
                "date": "2026-09-27",
                "rates": {"m": {**_rates(), "input": "three"}},
            }
        )
    with pytest.raises(ValueError, match=">= 0"):
        parse_pricing(
            {
                "source": "s",
                "date": "2026-09-27",
                "rates": {"m": {**_rates(), "output": -1.0}},
            }
        )


def _rates() -> dict:
    return {"input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_write": 1.25}


def test_load_task_split_explicit_and_inferred(tmp_path: Path):
    dev_dir = tmp_path / "tasks" / "dev"
    held_dir = tmp_path / "tasks" / "heldout"
    dev_dir.mkdir(parents=True)
    held_dir.mkdir(parents=True)

    explicit = held_dir / "explicit.yaml"
    explicit.write_text("id: e\nprompt: Do it\nsplit: held-out\n")
    assert load_task(explicit).split == "held-out"

    inferred_held = held_dir / "inferred.yaml"
    inferred_held.write_text("id: h\nprompt: Do it\n")
    assert load_task(inferred_held).split == "held-out"

    inferred_dev = dev_dir / "inferred.yaml"
    inferred_dev.write_text("id: d\nprompt: Do it\n")
    assert load_task(inferred_dev).split == "dev"

    plain = tmp_path / "tasks" / "plain.yaml"
    plain.write_text("id: p\nprompt: Do it\n")
    assert load_task(plain).split == "dev"


def test_load_task_split_rejects_contradictions_and_bad_values(tmp_path: Path):
    held_dir = tmp_path / "tasks" / "heldout"
    held_dir.mkdir(parents=True)

    contradict = held_dir / "bad.yaml"
    contradict.write_text("id: b\nprompt: Do it\nsplit: dev\n")
    with pytest.raises(ValueError, match="contradicts its directory"):
        load_task(contradict)

    bad = tmp_path / "tasks" / "bad-value.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("id: v\nprompt: Do it\nsplit: validation\n")
    with pytest.raises(ValueError, match="split must be 'dev' or 'held-out'"):
        load_task(bad)


def _scaffold(tmp_path: Path) -> Path:
    """A minimal loadable experiment: skill dir, one task, config path."""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Test")
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "task.yaml").write_text("id: one\nprompt: Do it\n")
    return tmp_path / "skilldiff.yaml"


_BASE = "name: test\nskill: ./skill\nmodels:\n  - sonnet\ntasks:\n  - ./tasks/*.yaml\n"


def test_load_experiment_rejects_unknown_top_level_key(tmp_path: Path):
    exp_file = _scaffold(tmp_path)
    exp_file.write_text(_BASE + "modles:\n  - sonnet\n")

    with pytest.raises(ValueError, match="Unknown key") as excinfo:
        load_experiment(exp_file)
    message = str(excinfo.value)
    assert "'modles'" in message
    assert "did you mean 'models'" in message


def test_load_experiment_rejects_unknown_block_keys(tmp_path: Path):
    exp_file = _scaffold(tmp_path)
    blocks = (
        "claude:\n  max_turs: 5\n",
        "codex:\n  sandbx: workspace-write\n",
        "opencode:\n  servcie: go\n",
        "antigravity:\n  skip_permissions: true\n",
        "thresholds:\n  required_cost_reduction: 10\n",
        "failure_policy:\n  agent_failures: exclude\n",
        "pricing:\n  source: s\n  date: '2026-09-27'\n  rate:\n    m: {}\n",
    )
    for block in blocks:
        exp_file.write_text(_BASE + block)
        with pytest.raises(ValueError, match="Unknown key") as excinfo:
            load_experiment(exp_file)
        assert "Allowed keys" in str(excinfo.value), block


def test_load_experiment_rejects_unknown_pr_key(tmp_path: Path):
    exp_file = _scaffold(tmp_path)
    exp_file.write_text(_BASE + "pr:\n  repo: ./r\n  base: main\n  hed: feature\n")

    with pytest.raises(ValueError, match="Unknown key") as excinfo:
        load_experiment(exp_file)
    assert "did you mean 'head'" in str(excinfo.value)


def test_load_experiment_rejects_non_mapping_block(tmp_path: Path):
    exp_file = _scaffold(tmp_path)
    exp_file.write_text(_BASE + "claude:\n  - acceptEdits\n")

    with pytest.raises(ValueError, match="Experiment 'claude' must be a mapping"):
        load_experiment(exp_file)


@pytest.mark.parametrize(
    "canonical,alias,valid,typo",
    [
        ("failure_policy", "on_failure", "agent_failure: zero", "agent_failures: zero"),
        ("antigravity", "agy", "bin_path: agy", "bin_paths: agy"),
    ],
)
def test_load_experiment_checks_alias_even_when_shadowed(
    tmp_path: Path, canonical, alias, valid, typo
):
    exp_file = _scaffold(tmp_path)
    exp_file.write_text(_BASE + f"{canonical}: {{{valid}}}\n{alias}: {{{typo}}}\n")
    with pytest.raises(ValueError, match=f"Unknown key.*'{alias}' block"):
        load_experiment(exp_file)


@pytest.mark.parametrize(
    "block",
    ["claude", "codex", "opencode", "antigravity", "agy", "pr",
     "thresholds", "failure_policy", "on_failure", "pricing"],
)
@pytest.mark.parametrize("value", ["[]", "false", "0", "''"])
def test_load_experiment_rejects_falsey_non_mapping_blocks(tmp_path: Path, block, value):
    exp_file = _scaffold(tmp_path)
    exp_file.write_text(_BASE + f"{block}: {value}\n")
    with pytest.raises(ValueError, match=f"'{block}' must be a mapping"):
        load_experiment(exp_file)


@pytest.mark.parametrize("block", ["grader", "validation"])
@pytest.mark.parametrize("value", ["[]", "false", "0", "''"])
def test_load_task_rejects_falsey_non_mapping_blocks(tmp_path: Path, block, value):
    task_file = tmp_path / "task.yaml"
    task_file.write_text(f"id: t\nprompt: Do it\n{block}: {value}\n")
    with pytest.raises(ValueError, match=f"'{block}' must be a mapping"):
        load_task(task_file)


@pytest.mark.parametrize("loader", [load_experiment, load_task])
@pytest.mark.parametrize("value", ["[]", "false", "0", "''"])
def test_config_rejects_falsey_non_mapping_documents(tmp_path: Path, loader, value):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(value)
    with pytest.raises(ValueError, match="must be a mapping"):
        loader(config_file)


@pytest.mark.parametrize("value", ["null", "{}"])
def test_optional_empty_blocks_and_valid_aliases_remain_supported(tmp_path: Path, value):
    exp_file = _scaffold(tmp_path)
    exp_file.write_text(
        _BASE + f"claude: {value}\ncodex: {value}\nopencode: {value}\n"
        f"thresholds: {value}\npricing: {value}\n"
        f"antigravity: {value}\nagy: {{bin_path: custom-agy}}\n"
        f"failure_policy: {value}\non_failure: {{agent_failure: zero}}\n"
    )
    (tmp_path / "tasks" / "task.yaml").write_text(
        f"id: t\nprompt: Do it\ngrader: {value}\nvalidation: {value}\n"
    )
    cfg, tasks = load_experiment(exp_file)
    assert cfg.antigravity.bin_path == "custom-agy"
    assert cfg.failure_policy["agent_failure"] == "zero"
    assert tasks[0].grader is None
    assert tasks[0].validation == {}


def test_canonical_blocks_keep_precedence_over_valid_aliases(tmp_path: Path):
    exp_file = _scaffold(tmp_path)
    exp_file.write_text(
        _BASE + "antigravity: {bin_path: primary}\nagy: {bin_path: legacy}\n"
        "failure_policy: {agent_failure: zero}\non_failure: {agent_failure: exclude}\n"
    )
    cfg, _ = load_experiment(exp_file)
    assert cfg.antigravity.bin_path == "primary"
    assert cfg.failure_policy["agent_failure"] == "zero"


def test_load_task_rejects_unknown_keys(tmp_path: Path):
    task_file = tmp_path / "task.yaml"
    task_file.write_text("id: t\npromt: Do it\n")

    with pytest.raises(ValueError, match="Unknown key") as excinfo:
        load_task(task_file)
    message = str(excinfo.value)
    assert "'promt'" in message
    assert "did you mean 'prompt'" in message


def test_load_task_rejects_unknown_grader_and_validation_keys(tmp_path: Path):
    grader_task = tmp_path / "grader-task.yaml"
    grader_task.write_text(
        "id: g\nprompt: Do it\ngrader:\n  type: command\n  comand: python g.py\n"
    )
    with pytest.raises(ValueError, match="Unknown key") as excinfo:
        load_task(grader_task)
    assert "did you mean 'command'" in str(excinfo.value)

    validation_task = tmp_path / "validation-task.yaml"
    validation_task.write_text(
        "id: v\nprompt: Do it\nvalidation:\n  goods: ./fixtures/good\n"
    )
    with pytest.raises(ValueError, match="Unknown key") as excinfo:
        load_task(validation_task)
    assert "did you mean 'good'" in str(excinfo.value)


def test_parse_pricing_rejects_unknown_keys():
    with pytest.raises(ValueError, match="Unknown key") as excinfo:
        parse_pricing(
            {
                "source": "s",
                "date": "2026-09-27",
                "currancy": "USD",
                "rates": {"m": _rates()},
            }
        )
    assert "did you mean 'currency'" in str(excinfo.value)

    with pytest.raises(ValueError, match="Unknown key") as excinfo:
        parse_pricing(
            {
                "source": "s",
                "date": "2026-09-27",
                "rates": {"m": {**_rates(), "cache_writ": 1.0}},
            }
        )
    assert "pricing.rates.m" in str(excinfo.value)

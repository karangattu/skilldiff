import difflib
import glob
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

# Keys skilldiff actually reads from each block. Anything else is a typo or a
# key borrowed from another tool; reject it at load time instead of silently
# running with defaults the author never chose.
EXPERIMENT_KEYS = frozenset(
    {
        "name",
        "skill",
        "skill_a",
        "skill_b",
        "include_baseline",
        "pr",
        "models",
        "tasks",
        "runs",
        "harness",
        "claude",
        "codex",
        "opencode",
        "antigravity",
        "agy",
        "timeout_seconds",
        "parallel",
        "thresholds",
        "preset",
        "seed",
        "failure_policy",
        "on_failure",  # alias of failure_policy
        "pricing",
        "cost_basis",
        "isolation",
        "container_image",
    }
)
CLAUDE_KEYS = frozenset(
    {
        "auth",
        "effort",
        "max_turns",
        "max_budget_usd",
        "permission_mode",
        "allowed_tools",
        "isolate",
        "sandbox",
        "allowed_domains",
        "bin_path",
        "extra_args",
        "read_paths",
    }
)
CODEX_KEYS = frozenset(
    {
        "auth",
        "sandbox",
        "verify_skills",
        "read_paths",
        "network_access",
        "dangerously_bypass_approvals_and_sandbox",
        "bin_path",
        "extra_args",
    }
)
OPENCODE_KEYS = frozenset(
    {
        "service",
        "subscription",  # older name for service
        "provider",
        "dangerously_skip_permissions",
        "isolate",
        "variant",
        "bin_path",
        "extra_args",
    }
)
ANTIGRAVITY_KEYS = frozenset({"dangerously_skip_permissions", "bin_path", "extra_args"})
PR_KEYS = frozenset({"repo", "base", "head", "mode", "pair"})
THRESHOLD_KEYS = frozenset(
    {
        "acceptable_score_regression_pp",
        "required_cost_reduction_pct",
        "required_token_reduction_pct",
        "meaningful_score_gain_pp",
    }
)
FAILURE_POLICY_KEYS = frozenset({"agent_failure", "missing"})
PRICING_KEYS = frozenset({"source", "date", "currency", "rates"})
TASK_KEYS = frozenset(
    {
        "id",
        "prompt",
        "prompts",
        "repo",
        "grader",
        "category",
        "split",
        "validation",
        "allowed_paths",
        "forbidden_paths",
        "grader_ignore",
        "runtime_probe",
    }
)
GRADER_KEYS = frozenset({"type", "command", "rubric", "prompt", "model"})
VALIDATION_KEYS = frozenset({"good", "broken", "bad", "reference", "deprecated"})


def validate_string(value: Any, key: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")


def validate_string_list(value: Any, key: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise ValueError(f"{key} must be a {qualifier}list of strings")
    for index, item in enumerate(value):
        validate_string(item, f"{key}[{index}]")


def validate_boolean(value: Any, key: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{key} must be a boolean (true or false)")


def validate_integer(value: Any, key: str, *, positive: bool = False) -> None:
    if type(value) is not int or (positive and value <= 0):
        qualifier = "positive " if positive else ""
        raise ValueError(f"{key} must be a {qualifier}integer")


def validate_number(value: Any, key: str, *, positive: bool = False) -> None:
    finite = False
    if not isinstance(value, bool) and isinstance(value, (int, float)):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            pass
    if not finite:
        raise ValueError(f"{key} must be a finite number")
    if (positive and value <= 0) or (not positive and value < 0):
        bound = "> 0" if positive else ">= 0"
        raise ValueError(f"{key} must be {bound}")


def validate_enum(value: Any, key: str, choices: set[str], *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or value not in choices:
        expected = " or ".join(repr(choice) for choice in sorted(choices))
        raise ValueError(f"{key} must be {expected}")


def validate_experiment_values(data: dict[str, Any]) -> None:
    """Reject coercions that silently alter safety, budgets, or execution."""
    if "name" in data:
        validate_string(data["name"], "name")
    for key in ("skill", "skill_a", "skill_b", "container_image"):
        if key in data:
            validate_string(data[key], key, nullable=True)
    for key in ("models", "tasks"):
        if key in data:
            validate_string_list(data[key], key, nonempty=True)
    for key in ("runs", "parallel"):
        if key in data:
            validate_integer(data[key], key, positive=True)
    if "seed" in data and data["seed"] is not None:
        validate_integer(data["seed"], "seed")
    if "include_baseline" in data:
        validate_boolean(data["include_baseline"], "include_baseline")
    if "timeout_seconds" in data and data["timeout_seconds"] is not None:
        validate_number(data["timeout_seconds"], "timeout_seconds", positive=True)
    if "isolation" in data:
        validate_enum(data["isolation"], "isolation", {"local", "docker", "podman", "macos"})
    if "cost_basis" in data:
        validate_enum(data["cost_basis"], "cost_basis", {"harness", "api-equivalent"})
    for block in ("claude", "codex", "opencode", "antigravity", "agy"):
        values = data.get(block) or {}
        for key in ("bin_path", "variant", "provider"):
            if key in values:
                validate_string(values[key], f"{block}.{key}", nullable=key != "provider")
        for key in ("extra_args", "allowed_tools", "read_paths", "allowed_domains"):
            if key in values:
                validate_string_list(values[key], f"{block}.{key}")
        for key in ("isolate", "dangerously_skip_permissions",
                    "dangerously_bypass_approvals_and_sandbox", "verify_skills",
                    "network_access"):
            if key in values:
                validate_boolean(values[key], f"{block}.{key}")
        if block == "claude" and "sandbox" in values:
            validate_boolean(values["sandbox"], "claude.sandbox")
    claude = data.get("claude") or {}
    if "max_turns" in claude and claude["max_turns"] is not None:
        validate_integer(claude["max_turns"], "claude.max_turns", positive=True)
    if "max_budget_usd" in claude and claude["max_budget_usd"] is not None:
        validate_number(claude["max_budget_usd"], "claude.max_budget_usd", positive=True)
    for key, choices in (
        ("effort", {"low", "medium", "high", "xhigh", "max"}),
        ("permission_mode", {"acceptEdits", "bypassPermissions", "default", "manual", "dontAsk",
                             "plan", "auto"}),
    ):
        if key in claude:
            validate_enum(claude[key], f"claude.{key}", choices, nullable=True)
    codex = data.get("codex") or {}
    if "sandbox" in codex:
        validate_enum(codex["sandbox"], "codex.sandbox",
                      {"read-only", "workspace-write", "danger-full-access"}, nullable=True)
    opencode = data.get("opencode") or {}
    for key in ("service", "subscription"):
        if key in opencode:
            validate_enum(opencode[key], f"opencode.{key}", {"go", "zen"})
    pr = data.get("pr") or {}
    for key, choices in (("mode", {"agent", "correctness"}),
                         ("pair", {"merge-base", "base-merge"})):
        if key in pr:
            validate_enum(pr[key], f"pr.{key}", choices)
    for key, value in (data.get("thresholds") or {}).items():
        if value is not None:
            validate_number(value, f"thresholds.{key}")
    for block in ("failure_policy", "on_failure"):
        values = data.get(block) or {}
        for key, choices in (("agent_failure", {"exclude", "zero"}), ("missing", {"exclude"})):
            if key in values and values[key] is not None:
                validate_enum(values[key], f"{block}.{key}", choices)


def reject_unknown_keys(data: dict[str, Any], allowed: frozenset[str], context: str) -> None:
    """Fail fast on keys skilldiff does not read, naming the closest match."""
    unknown = [key for key in data if key not in allowed]
    if not unknown:
        return
    named: list[str] = []
    for key in sorted(unknown, key=str):
        close = difflib.get_close_matches(str(key), sorted(allowed), n=1)
        named.append(f"{key!r}" + (f" (did you mean {close[0]!r}?)" if close else ""))
    raise ValueError(
        f"Unknown key(s) in {context}: {', '.join(named)}. "
        f"Allowed keys: {', '.join(sorted(allowed))}"
    )


def reject_unknown_block_keys(data: dict[str, Any]) -> None:
    """Check blocks once, including aliases that another block may override."""
    blocks = (
        ("claude", CLAUDE_KEYS),
        ("codex", CODEX_KEYS),
        ("opencode", OPENCODE_KEYS),
        ("antigravity", ANTIGRAVITY_KEYS),
        ("agy", ANTIGRAVITY_KEYS),
        ("pr", PR_KEYS),
        ("thresholds", THRESHOLD_KEYS),
        ("failure_policy", FAILURE_POLICY_KEYS),
        ("on_failure", FAILURE_POLICY_KEYS),
    )
    for name, allowed in blocks:
        value = data.get(name)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ValueError(f"Experiment '{name}' must be a mapping")
        reject_unknown_keys(value, allowed, f"'{name}' block")


@dataclass
class ClaudeConfig:
    auth: str = "subscription"
    effort: Optional[str] = "high"
    max_turns: Optional[int] = 30
    max_budget_usd: Optional[float] = 2.0
    permission_mode: Optional[str] = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=list)
    # Load only project/local settings so user-level skills, plugins, and CLAUDE.md
    # cannot leak into either arm. Disable to test against your everyday setup.
    isolate: bool = True
    # Run Bash inside Claude Code's own OS sandbox so commands need no approval
    # (headless sessions deny anything that would prompt) but stay in the workspace.
    sandbox: bool = True
    # Hosts sandboxed commands may reach, e.g. ["pypi.org", "*.npmjs.org"].
    allowed_domains: list[str] = field(default_factory=list)
    bin_path: Optional[str] = None
    extra_args: list[str] = field(default_factory=list)
    # Readable roots for isolation: macos (the runtime the agent must execute).
    read_paths: list[str] = field(default_factory=list)


@dataclass
class CodexConfig:
    auth: str = "stored"
    sandbox: Optional[str] = "workspace-write"
    # workspace-write blocks network by default; enable for installs and docs.
    network_access: bool = False
    dangerously_bypass_approvals_and_sandbox: bool = False
    bin_path: Optional[str] = None
    extra_args: list[str] = field(default_factory=list)

    verify_skills: bool = True
    read_paths: list[str] = field(default_factory=list)

@dataclass
class OpenCodeConfig:
    service: str = "go"
    provider: str = "opencode-go"
    dangerously_skip_permissions: bool = True
    # Run each session in a private server (`--standalone`) so a sandbox around
    # the CLI also governs tool execution; the shared background server would
    # otherwise run tools unsandboxed. No-op when the CLI lacks the flag.
    isolate: bool = True
    variant: Optional[str] = None
    bin_path: Optional[str] = None
    extra_args: list[str] = field(default_factory=list)


@dataclass
class AntigravityConfig:
    dangerously_skip_permissions: bool = True
    bin_path: Optional[str] = None
    extra_args: list[str] = field(default_factory=list)


@dataclass
class GraderConfig:
    type: str = "command"
    command: Optional[str] = None
    rubric: Optional[str] = None
    prompt: Optional[str] = None
    model: Optional[str] = None


@dataclass
class TaskConfig:
    id: str
    prompt: str
    repo: Optional[str] = None
    grader: Optional[GraderConfig] = None
    source_path: Optional[Path] = None
    category: str = "general"
    split: str = "dev"
    validation: dict[str, Any] = field(default_factory=dict)
    allowed_paths: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    # Globs hidden from graders and skipped by the blast-radius check, e.g. the
    # audit files an agent writes under outputs/.
    grader_ignore: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    runtime_probe: Optional[str] = None


@dataclass
class PRConfig:
    repo: Path
    base: str
    head: str
    # What the PR experiment measures:
    #   agent: give agents the same task on each revision (agent effectiveness).
    #   correctness: run the same graders/tests against untouched revisions (PR correctness).
    mode: str = "agent"
    # Which revisions to compare:
    #   merge-base: merge-base(base, head) vs head (did the branch change behaviour?).
    #   base-merge: base tip vs synthetic merge of head into base (does it integrate?).
    pair: str = "merge-base"


@dataclass
class ExperimentConfig:
    name: str
    skill: Optional[Path]
    models: list[str]
    tasks_patterns: list[str]
    runs: int = 3
    harness: str = "claude"
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    opencode: OpenCodeConfig = field(default_factory=OpenCodeConfig)
    antigravity: AntigravityConfig = field(default_factory=AntigravityConfig)
    config_path: Optional[Path] = None
    timeout_seconds: Optional[float] = 1800.0
    parallel: int = 1
    pr: Optional[PRConfig] = None
    # Practical decision thresholds for verdicts (all optional):
    #   acceptable_score_regression_pp: tolerated score drop, e.g. 5 = -5pp ok.
    #   required_cost_reduction_pct: required saving, e.g. 10 = 10% cheaper.
    #   required_token_reduction_pct: required session-token saving for compression.
    #   meaningful_score_gain_pp: gain needed to call an improvement useful.
    thresholds: dict[str, float] = field(default_factory=dict)
    # Named preset that configures arm labels and decision criteria on top of
    # the shared runner: "skill" (no skill vs skill), "pr" (without vs with PR),
    # "revision" (skill A vs skill B), "compression" (original vs minified).
    # None means infer from skill/pr/skill_a+b (compression must be explicit).
    preset: Optional[str] = None
    # Skill A/B mode: compare two skill revisions in one experiment.
    # Exactly one of these holds: `skill`+`pr is None` (single skill),
    # `skill_a`+`skill_b` (A/B), or `pr` (PR mode).
    skill_a: Optional[Path] = None
    skill_b: Optional[Path] = None
    # When True, A/B mode also runs a no-skill baseline arm per pair so the
    # report can show whether either revision helps at all.
    include_baseline: bool = False
    # Reproducibility: fixed seed for arm order; None means derive and record one.
    seed: Optional[int] = None
    # Failure / missing-result policy, decided before running:
    #   agent_failure: "exclude" (default, failed sessions are N/A) or "zero"
    #     (failed sessions score 0). Grader timeouts/errors are always N/A.
    #   missing: currently always "exclude".
    failure_policy: dict[str, str] = field(default_factory=dict)
    # Optional API-equivalent cost basis (all optional but validated):
    #   {source, date, currency, rates: {model: {input, output, cache_read,
    #   cache_write}}} with rates per 1M tokens. Saved with the run so
    #   regenerated reports reproduce the estimate without re-looking-up prices.
    pricing: dict[str, Any] = field(default_factory=dict)
    isolation: str = "local"
    container_image: Optional[str] = None
    cost_basis: str = "harness"

    @property
    def is_skill_comparison(self) -> bool:
        return self.skill_a is not None or self.skill_b is not None

    @property
    def skill_dirs(self) -> list[Path]:
        if self.skill:
            return find_skill_dirs(self.skill) if self.skill else []
        # A/B mode: union of both revisions (for contamination checks).
        out: list[Path] = []
        for s in (self.skill_a, self.skill_b):
            if s:
                out.extend(find_skill_dirs(s))
        # De-duplicate by resolved path while keeping order.
        seen: set[str] = set()
        uniq: list[Path] = []
        for d in out:
            key = str(d.resolve()) if d.exists() else str(d)
            if key not in seen:
                seen.add(key)
                uniq.append(d)
        return uniq

    @property
    def skill_a_dirs(self) -> list[Path]:
        return find_skill_dirs(self.skill_a) if self.skill_a else []

    @property
    def skill_b_dirs(self) -> list[Path]:
        return find_skill_dirs(self.skill_b) if self.skill_b else []

    @property
    def skill_names(self) -> list[str]:
        """Every name an agent might use for the skill(s): directory and frontmatter names."""
        names: list[str] = []
        for skill_dir in self.skill_dirs:
            for name in (skill_dir.name, read_skill_name(skill_dir)):
                if name and name not in names:
                    names.append(name)
        return names

    @property
    def skill_a_names(self) -> list[str]:
        names: list[str] = []
        for skill_dir in self.skill_a_dirs:
            for name in (skill_dir.name, read_skill_name(skill_dir)):
                if name and name not in names:
                    names.append(name)
        return names

    @property
    def skill_b_names(self) -> list[str]:
        names: list[str] = []
        for skill_dir in self.skill_b_dirs:
            for name in (skill_dir.name, read_skill_name(skill_dir)):
                if name and name not in names:
                    names.append(name)
        return names


def find_skill_dirs(path: Path) -> list[Path]:
    """Return the skill directories at `path`.

    `path` is either one skill (a directory containing SKILL.md) or a skill pack
    (a directory whose subdirectories contain SKILL.md, e.g. a package's skills/).
    """
    if (path / "SKILL.md").is_file():
        return [path]
    if not path.is_dir():
        return []
    return sorted(p for p in path.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def read_skill_frontmatter(skill_dir: Path) -> dict[str, Any]:
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        meta = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


def read_skill_name(skill_dir: Path) -> Optional[str]:
    name = read_skill_frontmatter(skill_dir).get("name")
    return str(name).strip() if name else None


def parse_grader_config(
    data: Optional[dict[str, Any]], context: str = "grader"
) -> Optional[GraderConfig]:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError("'grader' must be a mapping")
    if not data:
        return None
    reject_unknown_keys(data, GRADER_KEYS, "'grader' block")
    grader_type = data.get("type", "command")
    validate_enum(grader_type, f"{context}.type", {"command", "llm", "rubric"})
    for key in ("command", "rubric", "prompt", "model"):
        if key in data:
            validate_string(data[key], f"{context}.{key}", nullable=True)
    if grader_type in {"llm", "rubric"} and not data.get("command"):
        raise ValueError(f"{context}.command is required for an {grader_type} judge")
    return GraderConfig(
        type=grader_type,
        command=data.get("command"),
        rubric=data.get("rubric"),
        prompt=data.get("prompt"),
        model=data.get("model"),
    )


_SPLIT_ALIASES = {
    "dev": "dev",
    "development": "dev",
    "held-out": "held-out",
    "heldout": "held-out",
    "held_out": "held-out",
    "holdout": "held-out",
}
_HELD_OUT_DIRS = {"heldout", "held-out", "held_out", "holdout"}
_DEV_DIRS = {"dev", "development"}


def infer_task_split(task_path: Optional[Path]) -> str:
    """Split implied by the task's directory (`heldout/` or `dev/`); "" if none."""
    parts = task_path.resolve().parent.parts if task_path else ()
    for part in reversed(parts):
        name = part.strip().lower()
        if name in _HELD_OUT_DIRS:
            return "held-out"
        if name in _DEV_DIRS:
            return "dev"
    return ""


def parse_task_split(value: Any, task_path: Optional[Path], task_id: str) -> str:
    """Resolve the dev/held-out split for one task.

    An explicit `split:` must agree with the task's directory (a `heldout/`
    task cannot claim to be dev). Unlabeled tasks count as dev: development
    results must never be mistaken for validation.
    """
    inferred = infer_task_split(task_path)
    explicit = ""
    if value is not None:
        validate_string(value, f"Task {task_id}.split")
        key = str(value).strip().lower()
        if key not in _SPLIT_ALIASES:
            raise ValueError(
                f"Task {task_id}: split must be 'dev' or 'held-out' (got {value!r})"
            )
        explicit = _SPLIT_ALIASES[key]
    if explicit and inferred and explicit != inferred:
        raise ValueError(
            f"Task {task_id}: split '{explicit}' contradicts its directory "
            f"({inferred}); move the task file or drop the split field"
        )
    return explicit or inferred or "dev"


PRICING_RATE_KEYS = ("input", "output", "cache_read", "cache_write")


def parse_pricing(data: Any) -> dict[str, Any]:
    """Validate the optional `pricing` block (API-equivalent cost basis).

    Rates are per 1M tokens, keyed by the model names in the experiment, and
    are saved with the run so regenerated reports reproduce the estimate
    instead of asking anyone to look prices up again:

        pricing:
          source: https://www.anthropic.com/pricing
          date: "2026-09-27"
          currency: USD
          rates:
            claude-sonnet-5:
              input: 3.00
              output: 15.00
              cache_read: 0.30
              cache_write: 3.75
    """
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Experiment 'pricing' must be a mapping")
    if not data:
        return {}
    reject_unknown_keys(data, PRICING_KEYS, "'pricing' block")
    source = str(data.get("source", "") or "").strip()
    if not source:
        raise ValueError("pricing.source must name where the rates came from")
    date = str(data.get("date", "") or "").strip()
    if not date:
        raise ValueError("pricing.date must record when the rates were checked")
    currency = str(data.get("currency", "USD") or "USD").strip().upper() or "USD"
    rates_raw = data.get("rates")
    if not isinstance(rates_raw, dict) or not rates_raw:
        raise ValueError(
            "pricing.rates must map model names to per-1M-token rates "
            "(input, output, cache_read, cache_write)"
        )
    rates: dict[str, dict[str, float]] = {}
    for model, entry in rates_raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"pricing.rates.{model} must be a mapping of token rates")
        reject_unknown_keys(
            entry, frozenset(PRICING_RATE_KEYS), f"pricing.rates.{model} block"
        )
        out: dict[str, float] = {}
        for key in PRICING_RATE_KEYS:
            if key not in entry or entry[key] is None:
                raise ValueError(
                    f"pricing.rates.{model} must include '{key}' (rate per 1M tokens)"
                )
            try:
                validate_number(entry[key], f"pricing.rates.{model}.{key}")
            except ValueError as exc:
                raise ValueError(f"{exc} (rate per 1M tokens)") from exc
            out[key] = float(entry[key])
        rates[str(model)] = out
    return {"source": source, "date": date, "currency": currency, "rates": rates}


def load_task(task_path: Path) -> TaskConfig:
    if not task_path.exists():
        raise FileNotFoundError(f"Task file not found: {task_path}")

    with open(task_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"Task file {task_path} must be a mapping")
    reject_unknown_keys(data, TASK_KEYS, f"task file {task_path}")

    task_id = data.get("id")
    if task_id is None:
        task_id = task_path.stem
    validate_string(task_id, f"Task {task_path}: id")

    context = f"Task {task_id}"
    prompts_raw = data.get("prompts", [])
    validate_string_list(prompts_raw, f"{context}.prompts")
    prompts = [p.strip() for p in prompts_raw]

    if "prompt" in data:
        validate_string(data["prompt"], f"{context}.prompt")
    prompt = data.get("prompt", "").strip()
    if not prompt and prompts:
        prompt = prompts[0]
    if not prompt:
        raise ValueError(f"Task in {task_path} must have a non-empty 'prompt' or 'prompts'")
    if not prompts and prompt:
        prompts = [prompt]

    allowed_paths = data.get("allowed_paths", [])
    forbidden_paths = data.get("forbidden_paths", [])
    validate_string_list(allowed_paths, f"{context}.allowed_paths")
    validate_string_list(forbidden_paths, f"{context}.forbidden_paths")
    grader_ignore = data.get("grader_ignore", [])
    validate_string_list(grader_ignore, f"{context}.grader_ignore")
    if "repo" in data:
        validate_string(data["repo"], f"{context}.repo", nullable=True)

    grader_data = data.get("grader")
    grader = parse_grader_config(grader_data, f"{context}.grader")
    category = data.get("category", "general")
    validate_enum(
        category, f"{context}.category", {"intended", "irrelevant", "ambiguous", "general"}
    )
    split = parse_task_split(data.get("split"), task_path, task_id)
    validation = data.get("validation")
    if validation is None:
        validation = {}
    if not isinstance(validation, dict):
        raise ValueError(f"Task {task_id}: 'validation' must be a mapping")
    reject_unknown_keys(validation, VALIDATION_KEYS, f"task {task_id} 'validation' block")
    for key, value in validation.items():
        if value is None:
            continue
        if isinstance(value, str):
            validate_string(value, f"{context}.validation.{key}")
        else:
            validate_string_list(value, f"{context}.validation.{key}")

    if "runtime_probe" in data:
        validate_string(data["runtime_probe"], f"{context}.runtime_probe")

    return TaskConfig(
        id=str(task_id),
        prompt=prompt,
        repo=data.get("repo"),
        grader=grader,
        source_path=task_path.resolve(),
        category=category,
        split=split,
        validation=dict(validation or {}),
        allowed_paths=allowed_paths,
        forbidden_paths=forbidden_paths,
        grader_ignore=grader_ignore,
        prompts=prompts,
        runtime_probe=data.get("runtime_probe"),
    )


def load_experiment(experiment_path: Path) -> tuple[ExperimentConfig, list[TaskConfig]]:
    if not experiment_path.exists():
        raise FileNotFoundError(f"Experiment file not found: {experiment_path}")

    with open(experiment_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"Experiment config {experiment_path} must be a mapping")

    # `harnesses:` never drove the runner; keep its specific message ahead of
    # the generic unknown-key check.
    if "harnesses" in data:
        raise ValueError(
            "harnesses is not supported; set a single 'harness: <name>' instead"
        )
    reject_unknown_keys(data, EXPERIMENT_KEYS, f"experiment config {experiment_path}")
    reject_unknown_block_keys(data)
    validate_experiment_values(data)
    pricing = parse_pricing(data.get("pricing"))
    cost_basis = data.get("cost_basis", "harness")
    if cost_basis == "api-equivalent" and not pricing:
        raise ValueError("cost_basis: api-equivalent requires recorded pricing rates")

    name = data.get("name")
    if not name:
        raise ValueError("Experiment config must specify 'name'")

    skill_str = data.get("skill")
    skill_a_str = data.get("skill_a")
    skill_b_str = data.get("skill_b")
    include_baseline = data.get("include_baseline", False)
    pr_data = data.get("pr")
    modes_present = sum(
        [bool(skill_str), bool(skill_a_str or skill_b_str), pr_data is not None]
    )
    if modes_present != 1:
        raise ValueError(
            "Experiment must specify exactly one of 'skill', 'skill_a'+'skill_b', or 'pr'"
        )
    base_dir = experiment_path.parent.resolve()
    skill_path = None
    skill_a_path = None
    skill_b_path = None
    pr = None
    if pr_data is not None:
        if any(
            not isinstance(pr_data.get(k), str) or not pr_data[k].strip()
            for k in ("repo", "base", "head")
        ):
            raise ValueError("pr must specify non-empty repo, base, and head strings")
        mode = str(pr_data.get("mode", "agent") or "agent").strip().lower()
        if mode not in {"agent", "correctness"}:
            raise ValueError("pr.mode must be 'agent' or 'correctness'")
        pair = str(pr_data.get("pair", "merge-base") or "merge-base").strip().lower()
        if pair not in {"merge-base", "base-merge"}:
            raise ValueError("pr.pair must be 'merge-base' or 'base-merge'")
        pr = PRConfig(
            (base_dir / pr_data["repo"]).resolve(),
            pr_data["base"],
            pr_data["head"],
            mode=mode,
            pair=pair,
        )
        if not pr.repo.is_dir():
            raise FileNotFoundError(f"PR repo not found: {pr.repo}")
    elif skill_a_str or skill_b_str:
        if not (skill_a_str and skill_b_str):
            raise ValueError("Skill A/B mode requires both 'skill_a' and 'skill_b'")
        skill_a_path = (base_dir / str(skill_a_str)).resolve()
        skill_b_path = (base_dir / str(skill_b_str)).resolve()
        for label, p in (("skill_a", skill_a_path), ("skill_b", skill_b_path)):
            if not p.exists():
                raise FileNotFoundError(f"Skill directory not found ({label}): {p}")
            if not find_skill_dirs(p):
                raise FileNotFoundError(
                    f"No SKILL.md found in {p} or its immediate subdirectories ({label})"
                )
        if skill_a_path.resolve() == skill_b_path.resolve():
            raise ValueError("skill_a and skill_b must be different directories")
    else:
        skill_path = (base_dir / str(skill_str)).resolve()
        if not skill_path.exists():
            raise FileNotFoundError(f"Skill directory not found: {skill_path}")
        if not find_skill_dirs(skill_path):
            raise FileNotFoundError(
                f"No SKILL.md found in {skill_path} or its immediate subdirectories"
            )

    models = data.get("models")
    if not models or not isinstance(models, list):
        raise ValueError("Experiment config must specify a non-empty list of 'models'")

    task_patterns = data.get("tasks", [])
    if not task_patterns or not isinstance(task_patterns, list):
        raise ValueError("Experiment config must specify a non-empty list of 'tasks'")

    runs = data.get("runs", 3)

    harness = str(data.get("harness", "")).lower().strip()
    if not harness:
        if "codex" in data and "claude" not in data:
            harness = "codex"
        elif "opencode" in data and "claude" not in data:
            harness = "opencode"
        elif ("antigravity" in data or "agy" in data) and "claude" not in data:
            harness = "antigravity"
        else:
            harness = "claude"

    if harness == "agy":
        harness = "antigravity"

    valid_harnesses = {"claude", "codex", "opencode", "antigravity"}
    if harness not in valid_harnesses:
        raise ValueError(
            f"Invalid harness '{harness}'. Must be one of: {', '.join(sorted(valid_harnesses))}"
        )

    claude_data = data.get("claude") or {}
    auth = str(claude_data.get("auth", "subscription"))
    if auth not in {"subscription", "api_key"}:
        raise ValueError("claude.auth must be 'subscription' or 'api_key'")
    budget_val = claude_data.get("max_budget_usd", 2.0)
    claude_cfg = ClaudeConfig(
        auth=auth,
        effort=claude_data.get("effort", "high"),
        max_turns=claude_data.get("max_turns", 30),
        max_budget_usd=float(budget_val) if budget_val is not None else 2.0,
        permission_mode=claude_data.get("permission_mode", "acceptEdits"),
        allowed_tools=claude_data.get("allowed_tools", []),
        isolate=claude_data.get("isolate", True),
        sandbox=claude_data.get("sandbox", True),
        allowed_domains=claude_data.get("allowed_domains", []),
        bin_path=claude_data.get("bin_path"),
        extra_args=claude_data.get("extra_args", []),
        read_paths=[str((experiment_path.parent / Path(p).expanduser()).resolve())
                    for p in claude_data.get("read_paths", [])],
    )

    codex_data = data.get("codex") or {}
    codex_auth = str(codex_data.get("auth", "stored"))
    if codex_auth not in {"stored", "subscription", "api_key"}:
        raise ValueError("codex.auth must be 'stored', 'subscription', or 'api_key'")
    codex_cfg = CodexConfig(
        verify_skills=codex_data.get("verify_skills", True),
        read_paths=[str((experiment_path.parent / Path(p).expanduser()).resolve())
                    for p in codex_data.get("read_paths", [])],
        auth=codex_auth,
        sandbox=codex_data.get("sandbox", "workspace-write"),
        network_access=codex_data.get("network_access", False),
        dangerously_bypass_approvals_and_sandbox=codex_data.get(
            "dangerously_bypass_approvals_and_sandbox", False
        ),
        bin_path=codex_data.get("bin_path"),
        extra_args=codex_data.get("extra_args", []),
    )

    opencode_data = data.get("opencode") or {}
    service = (
        str(opencode_data.get("service", opencode_data.get("subscription", "go"))).lower().strip()
    )
    if service not in {"go", "zen"}:
        raise ValueError("opencode.service must be 'go' (subscription) or 'zen' (pay-as-you-go)")
    default_provider = "opencode-go" if service == "go" else "opencode"
    provider = str(opencode_data.get("provider", default_provider)).strip()
    opencode_cfg = OpenCodeConfig(
        service=service,
        provider=provider,
        dangerously_skip_permissions=opencode_data.get("dangerously_skip_permissions", True),
        isolate=opencode_data.get("isolate", True),
        variant=opencode_data.get("variant"),
        bin_path=opencode_data.get("bin_path"),
        extra_args=opencode_data.get("extra_args", []),
    )

    antigravity_data = data.get("antigravity") or data.get("agy") or {}
    antigravity_cfg = AntigravityConfig(
        dangerously_skip_permissions=antigravity_data.get("dangerously_skip_permissions", True),
        bin_path=antigravity_data.get("bin_path"),
        extra_args=antigravity_data.get("extra_args", []),
    )

    timeout_val = data.get("timeout_seconds", 1800)
    timeout_seconds = float(timeout_val) if timeout_val is not None else None
    parallel = data.get("parallel", 1)

    thresholds: dict[str, float] = {}
    raw_thresholds = data.get("thresholds") or {}
    for key in sorted(THRESHOLD_KEYS):
        if key in raw_thresholds and raw_thresholds[key] is not None:
            try:
                thresholds[key] = float(raw_thresholds[key])
            except (TypeError, ValueError):
                raise ValueError(f"Experiment thresholds.{key} must be a number")

    preset_raw = data.get("preset")
    preset: Optional[str] = None
    if preset_raw is not None:
        preset = str(preset_raw).strip().lower()
        if preset not in {"skill", "pr", "revision", "compression"}:
            raise ValueError("preset must be one of skill, pr, revision, compression")
    # Infer when absent; compression must be explicit so its stricter rules apply.
    if preset is None:
        if pr_data is not None:
            preset = "pr"
        elif skill_a_str or skill_b_str:
            preset = "revision"
        else:
            preset = "skill"

    seed = data.get("seed")

    failure_policy: dict[str, str] = {}
    raw_failure = data.get("failure_policy") or data.get("on_failure") or {}
    for key in ("agent_failure", "missing"):
        if key in raw_failure and raw_failure[key] is not None:
            val = str(raw_failure[key]).strip().lower()
            if key == "agent_failure" and val not in {"exclude", "zero"}:
                raise ValueError("failure_policy.agent_failure must be 'exclude' or 'zero'")
            if key == "missing" and val not in {"exclude"}:
                raise ValueError("failure_policy.missing must be 'exclude'")
            failure_policy[key] = val
    # Defaults, decided before running: failed/missing sessions are excluded (N/A).
    failure_policy.setdefault("agent_failure", "exclude")
    failure_policy.setdefault("missing", "exclude")

    isolation = str(data.get("isolation", "local") or "local").strip().lower()
    if codex_cfg.read_paths and isolation != "macos":
        raise ValueError("codex.read_paths requires isolation: macos")
    if claude_cfg.read_paths and isolation != "macos":
        raise ValueError("claude.read_paths requires isolation: macos")
    if isolation == "macos" and harness not in {"codex", "claude"}:
        raise ValueError("isolation: macos supports the Codex and Claude harnesses only")
    container_image = data.get("container_image")

    exp_config = ExperimentConfig(
        name=name,
        skill=skill_path,
        skill_a=skill_a_path,
        skill_b=skill_b_path,
        include_baseline=include_baseline,
        seed=seed,
        failure_policy=failure_policy,
        preset=preset,
        pr=pr,
        models=models,
        tasks_patterns=task_patterns,
        runs=runs,
        harness=harness,
        claude=claude_cfg,
        codex=codex_cfg,
        opencode=opencode_cfg,
        antigravity=antigravity_cfg,
        config_path=experiment_path,
        timeout_seconds=timeout_seconds,
        parallel=parallel,
        thresholds=thresholds,
        pricing=pricing,
        cost_basis=cost_basis,
        isolation=isolation,
        container_image=container_image,
    )

    # Preset consistency: each preset configures the shared runner with clear
    # arms; mismatches fail fast instead of running the wrong comparison.
    if preset == "skill" and (pr is not None or skill_a_path or skill_b_path):
        raise ValueError("preset skill requires 'skill' (no pr, no skill_a/skill_b)")
    if preset == "pr" and pr is None:
        raise ValueError("preset pr requires a 'pr' block")
    if preset == "revision" and not (skill_a_path and skill_b_path):
        raise ValueError("preset revision requires 'skill_a' and 'skill_b'")
    if preset == "compression":
        if not (skill_a_path and skill_b_path):
            raise ValueError("preset compression requires 'skill_a' and 'skill_b'")
        # Body compression must not confound adoption: the trigger (name and
        # description) must be identical so only the body differs.
        if len(exp_config.skill_a_dirs) != len(exp_config.skill_b_dirs):
            raise ValueError(
                "preset compression requires the same number of skills in A and B"
            )
        # Compare (name, description) pairs; bodies may differ.
        def _trigger(d: Path) -> tuple[str, str]:
            meta = read_skill_frontmatter(d)
            return (
                str(meta.get("name", d.name) or d.name).strip(),
                str(meta.get("description", "") or "").strip(),
            )

        trig_a = sorted(_trigger(d) for d in exp_config.skill_a_dirs)
        trig_b = sorted(_trigger(d) for d in exp_config.skill_b_dirs)
        if trig_a != trig_b:
            raise ValueError(
                "preset compression requires identical skill name and description "
                "in A and B (compress the body, not the trigger)"
            )

    loaded_tasks: list[TaskConfig] = []
    seen_ids: dict[str, Path] = {}
    seen_paths: set[Path] = set()

    for pattern in task_patterns:
        resolved_pattern = str(base_dir / pattern)
        matching_paths = sorted(glob.glob(resolved_pattern))
        for p in matching_paths:
            path_obj = Path(p).resolve()
            if path_obj in seen_paths:
                continue
            if path_obj.is_file():
                seen_paths.add(path_obj)
                task = load_task(path_obj)
                if pr and task.repo:
                    raise ValueError("Tasks in PR mode cannot specify repo; use pr.repo")
                if task.id in seen_ids:
                    raise ValueError(
                        f"Duplicate task id {task.id!r} in {seen_ids[task.id]} and {path_obj}"
                    )
                seen_ids[task.id] = path_obj
                loaded_tasks.append(task)

    if not loaded_tasks:
        raise ValueError(f"No task files matched patterns {task_patterns} from {base_dir}")

    return exp_config, loaded_tasks

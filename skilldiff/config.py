import glob
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


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
    bin_path: Optional[str] = None
    extra_args: list[str] = field(default_factory=list)


@dataclass
class CodexConfig:
    auth: str = "stored"
    sandbox: Optional[str] = "workspace-write"
    dangerously_bypass_approvals_and_sandbox: bool = False
    bin_path: Optional[str] = None
    extra_args: list[str] = field(default_factory=list)


@dataclass
class OpenCodeConfig:
    service: str = "go"
    provider: str = "opencode-go"
    dangerously_skip_permissions: bool = True
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


@dataclass
class TaskConfig:
    id: str
    prompt: str
    repo: Optional[str] = None
    grader: Optional[GraderConfig] = None
    source_path: Optional[Path] = None
    # Task relevance: intended (skill should help), irrelevant (skill should
    # stay out of the way), ambiguous (unclear trigger), general (default),
    # or any custom label. Reported separately in By category.
    category: str = "general"
    # Optional grader validation fixtures (relative to the task file):
    #   validation: {good: ../validation/<id>-good, broken: [../validation/<id>-bad1]}
    # `good` is a workspace that must score ~100%; `broken` entries must score <100%.
    # When present, `skilldiff check` grades all three (untouched, good, broken).
    validation: dict[str, Any] = field(default_factory=dict)


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


def parse_grader_config(data: Optional[dict[str, Any]]) -> Optional[GraderConfig]:
    if not data:
        return None
    return GraderConfig(
        type=data.get("type", "command"),
        command=data.get("command"),
    )


def load_task(task_path: Path) -> TaskConfig:
    if not task_path.exists():
        raise FileNotFoundError(f"Task file not found: {task_path}")

    with open(task_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    task_id = data.get("id")
    if not task_id:
        task_id = task_path.stem

    prompt = data.get("prompt", "").strip()
    if not prompt:
        raise ValueError(f"Task in {task_path} must have a non-empty 'prompt'")

    grader_data = data.get("grader")
    grader = parse_grader_config(grader_data)
    category = str(data.get("category", "general") or "general").strip().lower() or "general"
    validation = data.get("validation") or {}
    if validation is not None and not isinstance(validation, dict):
        raise ValueError(f"Task {task_id}: 'validation' must be a mapping")

    return TaskConfig(
        id=str(task_id),
        prompt=prompt,
        repo=data.get("repo"),
        grader=grader,
        source_path=task_path.resolve(),
        category=category,
        validation=dict(validation or {}),
    )


def load_experiment(experiment_path: Path) -> tuple[ExperimentConfig, list[TaskConfig]]:
    if not experiment_path.exists():
        raise FileNotFoundError(f"Experiment file not found: {experiment_path}")

    with open(experiment_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    name = data.get("name")
    if not name:
        raise ValueError("Experiment config must specify 'name'")

    skill_str = data.get("skill")
    skill_a_str = data.get("skill_a")
    skill_b_str = data.get("skill_b")
    include_baseline = bool(data.get("include_baseline", False))
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
        if not isinstance(pr_data, dict) or any(
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

    runs = int(data.get("runs", 3))
    if runs <= 0:
        raise ValueError("Experiment 'runs' must be positive integer")

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
    budget_val = claude_data.get("max_budget_usd")
    claude_cfg = ClaudeConfig(
        auth=auth,
        effort=claude_data.get("effort", "high"),
        max_turns=claude_data.get("max_turns", 30),
        max_budget_usd=float(budget_val) if budget_val is not None else 2.0,
        permission_mode=claude_data.get("permission_mode", "acceptEdits"),
        allowed_tools=[str(tool) for tool in claude_data.get("allowed_tools") or []],
        isolate=bool(claude_data.get("isolate", True)),
        bin_path=claude_data.get("bin_path"),
        extra_args=[str(a) for a in claude_data.get("extra_args") or []],
    )

    codex_data = data.get("codex") or {}
    codex_auth = str(codex_data.get("auth", "stored"))
    if codex_auth not in {"stored", "subscription", "api_key"}:
        raise ValueError("codex.auth must be 'stored', 'subscription', or 'api_key'")
    codex_cfg = CodexConfig(
        auth=codex_auth,
        sandbox=codex_data.get("sandbox", "workspace-write"),
        dangerously_bypass_approvals_and_sandbox=bool(
            codex_data.get("dangerously_bypass_approvals_and_sandbox", False)
        ),
        bin_path=codex_data.get("bin_path"),
        extra_args=[str(a) for a in codex_data.get("extra_args", [])],
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
        dangerously_skip_permissions=bool(opencode_data.get("dangerously_skip_permissions", True)),
        variant=opencode_data.get("variant"),
        bin_path=opencode_data.get("bin_path"),
        extra_args=[str(a) for a in opencode_data.get("extra_args", [])],
    )

    antigravity_data = data.get("antigravity") or data.get("agy") or {}
    antigravity_cfg = AntigravityConfig(
        dangerously_skip_permissions=bool(
            antigravity_data.get("dangerously_skip_permissions", True)
        ),
        bin_path=antigravity_data.get("bin_path"),
        extra_args=[str(a) for a in antigravity_data.get("extra_args", [])],
    )

    timeout_val = data.get("timeout_seconds", 1800)
    timeout_seconds = float(timeout_val) if timeout_val else None
    parallel = int(data.get("parallel", 1))
    if parallel < 1:
        raise ValueError("Experiment 'parallel' must be at least 1")

    thresholds: dict[str, float] = {}
    raw_thresholds = data.get("thresholds") or {}
    if not isinstance(raw_thresholds, dict):
        raise ValueError("Experiment 'thresholds' must be a mapping")
    for key in (
        "acceptable_score_regression_pp",
        "required_cost_reduction_pct",
        "required_token_reduction_pct",
        "meaningful_score_gain_pp",
    ):
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
    if seed is not None:
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            raise ValueError("Experiment 'seed' must be an integer")

    failure_policy: dict[str, str] = {}
    raw_failure = data.get("failure_policy") or data.get("on_failure") or {}
    if raw_failure is not None and not isinstance(raw_failure, dict):
        raise ValueError("Experiment 'failure_policy' must be a mapping")
    for key in ("agent_failure", "missing"):
        if isinstance(raw_failure, dict) and key in raw_failure and raw_failure[key] is not None:
            val = str(raw_failure[key]).strip().lower()
            if key == "agent_failure" and val not in {"exclude", "zero"}:
                raise ValueError("failure_policy.agent_failure must be 'exclude' or 'zero'")
            if key == "missing" and val not in {"exclude"}:
                raise ValueError("failure_policy.missing must be 'exclude'")
            failure_policy[key] = val
    # Defaults, decided before running: failed/missing sessions are excluded (N/A).
    failure_policy.setdefault("agent_failure", "exclude")
    failure_policy.setdefault("missing", "exclude")

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
        models=[str(m) for m in models],
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
    seen_ids: set[str] = set()

    for pattern in task_patterns:
        resolved_pattern = str(base_dir / pattern)
        matching_paths = sorted(glob.glob(resolved_pattern))
        for p in matching_paths:
            path_obj = Path(p)
            if path_obj.is_file():
                task = load_task(path_obj)
                if pr and task.repo:
                    raise ValueError("Tasks in PR mode cannot specify repo; use pr.repo")
                if task.id in seen_ids:
                    continue
                seen_ids.add(task.id)
                loaded_tasks.append(task)

    if not loaded_tasks:
        raise ValueError(f"No task files matched patterns {task_patterns} from {base_dir}")

    return exp_config, loaded_tasks

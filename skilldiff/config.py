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


@dataclass
class ExperimentConfig:
    name: str
    skill: Path
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

    @property
    def skill_dirs(self) -> list[Path]:
        return find_skill_dirs(self.skill)

    @property
    def skill_names(self) -> list[str]:
        """Every name an agent might use for the skill(s): directory and frontmatter names."""
        names: list[str] = []
        for skill_dir in self.skill_dirs:
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

    return TaskConfig(
        id=str(task_id),
        prompt=prompt,
        repo=data.get("repo"),
        grader=grader,
        source_path=task_path.resolve(),
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
    if not skill_str:
        raise ValueError("Experiment config must specify 'skill'")

    base_dir = experiment_path.parent.resolve()
    skill_path = (base_dir / skill_str).resolve()
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
    service = str(
        opencode_data.get("service", opencode_data.get("subscription", "go"))
    ).lower().strip()
    if service not in {"go", "zen"}:
        raise ValueError("opencode.service must be 'go' (subscription) or 'zen' (pay-as-you-go)")
    default_provider = "opencode-go" if service == "go" else "opencode"
    provider = str(opencode_data.get("provider", default_provider)).strip()
    opencode_cfg = OpenCodeConfig(
        service=service,
        provider=provider,
        dangerously_skip_permissions=bool(
            opencode_data.get("dangerously_skip_permissions", True)
        ),
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

    exp_config = ExperimentConfig(
        name=name,
        skill=skill_path,
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
                if task.id in seen_ids:
                    continue
                seen_ids.add(task.id)
                loaded_tasks.append(task)

    if not loaded_tasks:
        raise ValueError(f"No task files matched patterns {task_patterns} from {base_dir}")

    return exp_config, loaded_tasks

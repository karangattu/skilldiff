import glob
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class ClaudeConfig:
    effort: Optional[str] = "high"
    max_turns: Optional[int] = 30
    max_budget_usd: Optional[float] = 2.0
    permission_mode: Optional[str] = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=list)


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
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    config_path: Optional[Path] = None


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
        source_path=task_path,
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

    base_dir = experiment_path.parent
    skill_path = (base_dir / skill_str).resolve()
    if not skill_path.exists():
        raise FileNotFoundError(f"Skill directory not found: {skill_path}")

    skill_md = skill_path / "SKILL.md"
    if not skill_md.is_file():
        raise FileNotFoundError(f"SKILL.md not found in skill directory: {skill_path}")

    models = data.get("models")
    if not models or not isinstance(models, list):
        raise ValueError("Experiment config must specify a non-empty list of 'models'")

    task_patterns = data.get("tasks", [])
    if not task_patterns or not isinstance(task_patterns, list):
        raise ValueError("Experiment config must specify a non-empty list of 'tasks'")

    runs = int(data.get("runs", 3))
    if runs <= 0:
        raise ValueError("Experiment 'runs' must be positive integer")

    claude_data = data.get("claude", {})
    budget_val = claude_data.get("max_budget_usd")
    claude_cfg = ClaudeConfig(
        effort=claude_data.get("effort", "high"),
        max_turns=claude_data.get("max_turns", 30),
        max_budget_usd=float(budget_val) if budget_val is not None else 2.0,
        permission_mode=claude_data.get("permission_mode", "acceptEdits"),
        allowed_tools=[str(tool) for tool in claude_data.get("allowed_tools", [])],
    )

    exp_config = ExperimentConfig(
        name=name,
        skill=skill_path,
        models=[str(m) for m in models],
        tasks_patterns=task_patterns,
        runs=runs,
        claude=claude_cfg,
        config_path=experiment_path,
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

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from skilldiff.config import load_experiment
from skilldiff.experiment import ExperimentRunner
from skilldiff.reporter import render_report_table

STARTER_SKILLDIFF_YAML = """name: code-review-skill

skill: ./skills/code-review

harness: claude

models:
  - sonnet

tasks:
  - ./tasks/*.yaml

runs: 3

claude:
  auth: subscription
  effort: high
  max_turns: 30
  max_budget_usd: 2.00
  permission_mode: acceptEdits
  allowed_tools: []
"""

STARTER_TASK_YAML = """id: review-auth

prompt: |
  Review this authentication change.
  Report defects that can affect security or reliability.

repo: ./fixtures/auth-service

grader:
  type: command
  command: python grade.py
"""

STARTER_SKILL_MD = """---
name: code-review
description: Code review skill for identifying defects.
---

# Code Review Skill
Inspect code for security, correctness, and performance defects.
"""


def cmd_init(args: argparse.Namespace) -> int:
    config_file = Path("skilldiff.yaml")
    tasks_dir = Path("tasks")
    task_file = tasks_dir / "review-auth.yaml"
    skill_dir = Path("skills") / "code-review"
    skill_file = skill_dir / "SKILL.md"

    if config_file.exists() and not args.force:
        print("skilldiff.yaml already exists. Use --force to overwrite.", file=sys.stderr)
        return 1

    config_file.write_text(STARTER_SKILLDIFF_YAML, encoding="utf-8")
    tasks_dir.mkdir(parents=True, exist_ok=True)
    if not task_file.exists() or args.force:
        task_file.write_text(STARTER_TASK_YAML, encoding="utf-8")

    skill_dir.mkdir(parents=True, exist_ok=True)
    if not skill_file.exists() or args.force:
        skill_file.write_text(STARTER_SKILL_MD, encoding="utf-8")

    print("Initialized skilldiff experiment:")
    print("  - skilldiff.yaml")
    print("  - tasks/review-auth.yaml")
    print("  - skills/code-review/SKILL.md")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: configuration file '{config_path}' not found.", file=sys.stderr)
        return 1

    try:
        exp_config, tasks = load_experiment(config_path)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    if args.runs:
        exp_config.runs = int(args.runs)

    runner = ExperimentRunner(exp_config, tasks)
    print(
        f"Running experiment '{exp_config.name}' across {len(exp_config.models)} model(s), "
        f"{len(tasks)} task(s), {exp_config.runs} run(s) per arm..."
    )
    results = runner.run()

    print("\n" + _format_results(results))
    report = results.get("report", {})
    report_path = report.get("html") or report.get("qmd")
    if report_path:
        print(f"\nQuarto report: {report_path}")
    return 0


def cmd_results(args: argparse.Namespace) -> int:
    run_dir: Optional[Path] = None
    if args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        runs_parent = Path("runs")
        if not runs_parent.exists():
            print("No runs directory found.", file=sys.stderr)
            return 1
        subdirs = [p for p in runs_parent.iterdir() if p.is_dir()]
        if not subdirs:
            print("No experiment runs found.", file=sys.stderr)
            return 1
        run_dir = sorted(subdirs)[-1]

    results_file = run_dir / "results.json"
    if not results_file.exists():
        print(f"No results.json found in {run_dir}", file=sys.stderr)
        return 1

    with open(results_file, "r", encoding="utf-8") as f:
        results = json.load(f)

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print(_format_results(results))
    report = results.get("report", {})
    report_path = report.get("html") or report.get("qmd")
    if report_path:
        print(f"\nQuarto report: {report_path}")
    return 0


def _format_results(results: dict) -> str:
    name = results.get("name", "experiment")
    by_model = results.get("by_model", {})
    models = results.get("models", [])
    tasks_count = results.get("tasks_count", 0)
    runs_per_arm = results.get("runs_per_arm", 0)

    output_tables: list[str] = []

    if len(models) <= 1:
        model_name = models[0] if models else None
        model_data = by_model.get(model_name) if model_name else None
        overall = results.get("overall", {})
        c_metrics = model_data["control"] if model_data else overall.get("control", {})
        s_metrics = model_data["skill"] if model_data else overall.get("skill", {})
        return render_report_table(
            experiment_name=name,
            control_metrics=c_metrics,
            skill_metrics=s_metrics,
            models_count=len(models),
            tasks_count=tasks_count,
            runs_per_arm=runs_per_arm,
        )

    for model_name in models:
        model_data = by_model.get(model_name)
        if not model_data:
            continue
        c_metrics = model_data["control"]
        s_metrics = model_data["skill"]
        tbl = render_report_table(
            experiment_name=name,
            control_metrics=c_metrics,
            skill_metrics=s_metrics,
            models_count=1,
            tasks_count=tasks_count,
            runs_per_arm=runs_per_arm,
            model_name=model_name,
        )
        output_tables.append(tbl)

    return "\n\n".join(output_tables)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="skilldiff", description="Measure what an Agent Skill changes."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="Create an experiment file and task directory"
    )
    init_parser.add_argument("--force", "-f", action="store_true", help="Overwrite existing files")
    init_parser.set_defaults(func=cmd_init)

    run_parser = subparsers.add_parser("run", help="Run both arms of the experiment")
    run_parser.add_argument(
        "--config", "-c", default="skilldiff.yaml", help="Path to experiment config file"
    )
    run_parser.add_argument("--runs", "-r", type=int, help="Override number of runs per arm")
    run_parser.set_defaults(func=cmd_run)

    results_parser = subparsers.add_parser(
        "results", help="Read the difference between control and skill"
    )
    results_parser.add_argument("run_dir", nargs="?", help="Path to specific run directory")
    results_parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    results_parser.set_defaults(func=cmd_results)

    args = parser.parse_args()
    code = args.func(args)
    sys.exit(code)


if __name__ == "__main__":
    main()

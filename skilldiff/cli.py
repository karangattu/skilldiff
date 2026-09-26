import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import yaml

from skilldiff import __version__
from skilldiff.config import find_skill_dirs, load_experiment, read_skill_frontmatter
from skilldiff.experiment import ExperimentRunner, find_user_level_installs
from skilldiff.grader import Grader
from skilldiff.reporter import create_reports, render_report_table
from skilldiff.revisions import resolve_comparison
from skilldiff.runner import AgentRunner
from skilldiff.workspace import Workspace

DEFAULT_MODELS = {
    "claude": "sonnet",
    "codex": "gpt-5-codex",
    "opencode": "deepseek-v4-pro",
    "antigravity": "gemini-3.8-flash-medium",
}

HARNESS_BLOCKS = {
    "claude": """claude:
  auth: subscription        # or api_key (uses ANTHROPIC_API_KEY)
  effort: high
  max_turns: 30
  max_budget_usd: 2.00      # per session
  permission_mode: acceptEdits
  isolate: true             # ignore user-level skills, plugins, and CLAUDE.md
  allowed_tools: []         # e.g. ["Bash(my-cli *)"] for commands the skill runs
""",
    "codex": """codex:
  auth: stored
  sandbox: workspace-write
""",
    "opencode": """opencode:
  service: go
  dangerously_skip_permissions: true
""",
    "antigravity": """antigravity:
  dangerously_skip_permissions: true
""",
}

EXPERIMENT_YAML = """name: {name}

# A skill directory (containing SKILL.md) or a folder of skills, e.g. a package's skills/.
skill: {skill}

harness: {harness}

models:
  - {model}

tasks:
  - ./tasks/*.yaml

runs: 3                     # repetitions per arm; use 5+ before drawing conclusions
timeout_seconds: 1800       # per agent session
parallel: 1                 # pairs to run at once

# Practical decision thresholds (optional, shown in the verdict):
# thresholds:
#   acceptable_score_regression_pp: 5   # tolerated drop, e.g. -5pp ok if cheaper
#   required_cost_reduction_pct: 10     # required saving, e.g. 10% cheaper
#   meaningful_score_gain_pp: 5         # gain needed to call an improvement useful

{harness_block}"""

DEMO_SKILL_MD = (
    (
        "---\n"
        "name: changelog-style\n"
        "description: House style for CHANGELOG.md entries. Use whenever you add, edit, "
        "or review a changelog entry in this repository.\n"
        "---\n"
    )
    + """

# Changelog style

Every entry in `CHANGELOG.md` goes under the `## Unreleased` heading and uses exactly this
format:

```
- [TYPE] Summary in sentence case (#ISSUE)
```

- `TYPE` is one of `FEAT`, `FIX`, `DOCS`, or `CHORE`, in capital letters.
- The summary starts with a verb in the past tense ("Fixed", "Added").
- The issue number is in parentheses at the end, with a `#`.
- There is no trailing period.

Example: `- [FIX] Fixed crash when parsing empty input (#42)`
"""
)

DEMO_TASK_YAML = """id: changelog-entry
category: intended   # intended | irrelevant | ambiguous | general

# The fixture is copied into a fresh workspace for every run.
repo: ../fixtures/changelog

prompt: |
  We just fixed issue 42: the parser crashed on empty input. Add an entry for this
  fix to CHANGELOG.md. Do not change any other files.

grader:
  type: command
  # Graders run inside the workspace. Keep them outside the fixture so the agent
  # cannot see or edit them; $SKILLDIFF_TASK_DIR is the folder containing this file.
  command: python3 "$SKILLDIFF_TASK_DIR/../graders/changelog_entry.py"
"""

DEMO_CHANGELOG = """# Changelog

## Unreleased
"""

DEMO_GRADER = '''"""Grade the changelog entry. Prints {"score", "success", "checks"} as JSON."""
import json
import re
from pathlib import Path

text = Path("CHANGELOG.md").read_text(encoding="utf-8")
unreleased = text.split("## Unreleased", 1)[-1]
entries = [line.strip() for line in unreleased.splitlines() if line.strip().startswith("-")]
entry = entries[0] if entries else ""

# Named checks are recommended: [{"name": ..., "passed": bool}].
# Bare booleans also work: [true, false]. The report shows per-check gains.
named = [
    ("has entry", bool(entry)),
    ("type tag", bool(re.match(r"^- \\[(FEAT|FIX|DOCS|CHORE)\\] ", entry))),
    ("is FIX", entry.startswith("- [FIX]")),
    ("issue ref", bool(re.search(r"\\(#42\\)$", entry))),
    ("no trailing period", bool(entry) and not entry.endswith(".")),
]
checks = [{"name": name, "passed": passed} for name, passed in named]
score = sum(p for _, p in named) / len(named)
print(json.dumps({"score": score, "success": all(p for _, p in named), "checks": checks}))
'''

CUSTOM_TASK_YAML = """id: my-first-task
# intended: skill should help; irrelevant: stay out of the way
category: intended   # intended | irrelevant | ambiguous | general

# TODO: a small project where your skill should make a difference.
# Relative paths resolve from this file. Remove `repo` to start from an empty folder.
repo: ../fixtures/my-project

prompt: |
  TODO: describe a realistic request that your skill is meant to help with.
  Don't mention the skill: the point is to see whether the agent uses it on its own.

grader:
  type: command
  # Exit 0 = pass, or print JSON such as {"score": 0.75, "success": false}.
  # For per-check gains, print {"score": ..., "checks": [{"name": "x", "passed": true}]}.
  command: python3 "$SKILLDIFF_TASK_DIR/../graders/my_first_task.py"
"""

CUSTOM_GRADER = '''"""TODO: check the agent's work. Runs in the workspace after the agent is done.

Useful environment variables: SKILLDIFF_RESPONSE_FILE (the agent's final message),
SKILLDIFF_DIFF_FILE (a git diff of its changes), SKILLDIFF_TASK_DIR.
"""
import json

checks = [
    True,  # TODO: replace with real checks, e.g. run tests or inspect files
]
score = sum(checks) / len(checks)
print(json.dumps({"score": score, "success": all(checks), "checks": checks}))
'''


def _write(path: Path, content: str, force: bool) -> bool:
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(getattr(args, "dir", None) or ".")
    force = bool(getattr(args, "force", False))
    harness = getattr(args, "harness", None) or "claude"
    skill_arg: Optional[str] = getattr(args, "skill", None)
    config_file = root / "skilldiff.yaml"

    if config_file.exists() and not force:
        print(f"{config_file} already exists. Use --force to overwrite.", file=sys.stderr)
        return 1

    pr_number = getattr(args, "pr", None)
    if pr_number is not None:
        if skill_arg or pr_number <= 0 or not getattr(args, "repo", None):
            print(
                "--pr requires a positive PR number and --repo, without --skill.", file=sys.stderr
            )
            return 1
        return _init_pr(args, root, force, harness)
    if getattr(args, "repo", None) or getattr(args, "base", None):
        print("--repo and --base require --pr.", file=sys.stderr)
        return 1

    created: list[Path] = []
    if skill_arg:
        skill_path = Path(skill_arg).expanduser().resolve()
        if not find_skill_dirs(skill_path):
            print(f"No SKILL.md found in {skill_path} or its subdirectories.", file=sys.stderr)
            return 1
        skill_value = os.path.relpath(skill_path, root.resolve())
        if not skill_value.startswith("."):
            skill_value = f"./{skill_value}"
        name = f"{skill_path.name}-eval"
        files = {
            root / "tasks" / "my-first-task.yaml": CUSTOM_TASK_YAML,
            root / "graders" / "my_first_task.py": CUSTOM_GRADER,
        }
    else:
        skill_value = "./skills/changelog-style"
        name = "changelog-style-demo"
        files = {
            root / "skills" / "changelog-style" / "SKILL.md": DEMO_SKILL_MD,
            root / "tasks" / "changelog-entry.yaml": DEMO_TASK_YAML,
            root / "fixtures" / "changelog" / "CHANGELOG.md": DEMO_CHANGELOG,
            root / "graders" / "changelog_entry.py": DEMO_GRADER,
        }

    config_text = EXPERIMENT_YAML.format(
        name=name,
        skill=skill_value,
        harness=harness,
        model=DEFAULT_MODELS[harness],
        harness_block=HARNESS_BLOCKS[harness],
    )
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(config_text, encoding="utf-8")
    created.append(config_file)
    for path, content in files.items():
        if _write(path, content, force):
            created.append(path)
    if skill_arg:
        (root / "fixtures" / "my-project").mkdir(parents=True, exist_ok=True)

    print("Initialized skilldiff experiment:")
    for path in created:
        print(f"  - {path}")
    print()
    if skill_arg:
        print("Next steps:")
        print("  1. Put a small test project in fixtures/my-project/.")
        print("  2. Fill in tasks/my-first-task.yaml and graders/my_first_task.py.")
        print("  3. skilldiff check      # validate the setup and graders")
        print("  4. skilldiff run --runs 1")
    else:
        print("This is a runnable demo: a made-up changelog convention the agent can only")
        print("follow if it reads the skill.")
        print("  skilldiff check")
        print("  skilldiff run --runs 1")
    return 0


def _init_pr(args: argparse.Namespace, root: Path, force: bool, harness: str) -> int:
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"Repository not found: {repo}", file=sys.stderr)
        return 1
    base = args.base or "origin/main"
    head = f"refs/pull/{args.pr}/head"
    config = {
        "name": f"pr-{args.pr}-eval",
        "pr": {"repo": os.path.relpath(repo, root.resolve()), "base": base, "head": head},
        "harness": harness,
        "models": [DEFAULT_MODELS[harness]],
        "tasks": ["./tasks/*.yaml"],
        "runs": 3,
        "timeout_seconds": 1800,
        "parallel": 1,
    }
    text = yaml.safe_dump(config, sort_keys=False) + "\n" + HARNESS_BLOCKS[harness]
    _write(root / "skilldiff.yaml", text, force)
    _write(
        root / "tasks" / "my-first-task.yaml",
        """id: my-first-task
# Both arms start from pr.repo at their respective revisions. Do not set task.repo.
prompt: |
  TODO: describe a realistic task that uses the feature introduced by this PR.
  Use the same request for both arms; do not mention which revision is available.
grader:
  type: command
  command: python3 "$SKILLDIFF_TASK_DIR/../graders/my_first_task.py"
""",
        force,
    )
    _write(root / "graders" / "my_first_task.py", CUSTOM_GRADER, force)
    print(f"Initialized PR #{args.pr} experiment in {root}")
    print("Fetch the GitHub PR head into your local repository before check/run:")
    print(f"  git -C {shlex.quote(str(repo))} fetch origin {head}:{head}")
    print(f"Ensure base ref {base!r} is available (use a pre-merge base for merged PRs).")
    print("Fill in tasks/my-first-task.yaml and graders/my_first_task.py, then run")
    print(f"  skilldiff check -c {shlex.quote(str(root / 'skilldiff.yaml'))}")
    print(f"  skilldiff run -c {shlex.quote(str(root / 'skilldiff.yaml'))} --runs 1")
    return 0


# ----------------------------------------------------------------------------- check


def cmd_check(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    ok_count = 0
    problems = 0
    warnings = 0

    def ok(msg: str) -> None:
        nonlocal ok_count
        ok_count += 1
        print(f"  ok    {msg}")

    def warn(msg: str) -> None:
        nonlocal warnings
        warnings += 1
        print(f"  warn  {msg}")

    def fail(msg: str) -> None:
        nonlocal problems
        problems += 1
        print(f"  FAIL  {msg}")

    print(f"Checking {config_path}")
    try:
        cfg, tasks = load_experiment(config_path)
    except Exception as exc:
        fail(f"configuration: {exc}")
        return 1
    ok(
        f"configuration loads: {len(cfg.models)} model(s), {len(tasks)} task(s), "
        f"{cfg.runs} run(s) per arm"
    )

    comparison = None
    if cfg.pr:
        try:
            comparison = resolve_comparison(cfg.pr)
        except (ValueError, subprocess.SubprocessError) as exc:
            fail(str(exc))
            return 1
        ok(f"control: {comparison['control_commit']}")
        ok(f"treatment: {comparison['treatment_commit']}")

    for skill_dir in cfg.skill_dirs:
        meta = read_skill_frontmatter(skill_dir)
        fm_name = str(meta["name"]).strip() if meta.get("name") else None
        if not meta.get("description"):
            warn(
                f"{skill_dir.name}: SKILL.md has no `description` in its frontmatter; "
                "agents decide whether to load a skill from its description"
            )
        else:
            ok(f"skill `{fm_name or skill_dir.name}` ({skill_dir})")
        if fm_name and fm_name != skill_dir.name:
            warn(f"frontmatter name `{fm_name}` differs from directory `{skill_dir.name}`")

    runner = AgentRunner()
    binary = runner.binary_for(cfg.harness, cfg)
    resolved = shutil.which(binary) or (binary if Path(binary).exists() else None)
    if not resolved:
        fail(f"{cfg.harness} CLI not found (`{binary}`); install it or set bin_path")
    else:
        version = ""
        try:
            proc = subprocess.run(
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
            )
            version = next(iter((proc.stdout or proc.stderr).strip().splitlines()), "")
        except Exception:
            pass
        ok(f"{cfg.harness} CLI: {resolved} {version}".rstrip())

    if cfg.harness == "claude":
        if cfg.claude.auth == "subscription" and os.environ.get("ANTHROPIC_API_KEY"):
            ok("ANTHROPIC_API_KEY is set but will be removed; runs use your subscription")
        if cfg.claude.auth == "api_key" and not os.environ.get("ANTHROPIC_API_KEY"):
            fail("claude.auth is api_key but ANTHROPIC_API_KEY is not set")
        if cfg.claude.permission_mode == "bypassPermissions":
            warn("bypassPermissions lets agents run any command outside the workspace")

    installs = find_user_level_installs(cfg.skill_names, cfg.harness)
    if installs:
        if cfg.harness == "claude" and cfg.claude.isolate:
            ok("skill is installed at user level, but claude.isolate keeps it out of control")
        else:
            warn(
                "skill is installed at user level, so control can load it too: "
                + ", ".join(installs)
            )

    for task in tasks:
        task_dir = task.source_path.parent if task.source_path else Path(".")
        fixture = (
            cfg.pr.repo if cfg.pr else ((task_dir / task.repo).resolve() if task.repo else None)
        )
        if fixture and not fixture.exists():
            fail(f"task {task.id}: repo not found: {fixture}")
            continue
        if not (task.grader and task.grader.command):
            warn(f"task {task.id}: no grader, so every run scores N/A (only cost/time compared)")
            continue
        if getattr(args, "no_grade", False):
            ok(f"task {task.id}: grader configured (not run)")
            continue
        with tempfile.TemporaryDirectory(prefix="skilldiff-check-") as tmp:
            ws = Workspace(
                Path(tmp) / "workspace",
                False,
                cfg.skill,
                fixture,
                cfg.harness,
                source_commit=(comparison or {}).get("control_commit"),
            )
            try:
                ws.setup()
            except Exception as exc:
                fail(f"task {task.id}: could not build workspace: {exc}")
                continue
            grader = Grader(task.grader, cfg.skill_names, cfg.models[0], task_dir=task_dir)
            grade = grader.grade_workspace(ws.root)
        feedback = grade.feedback or ""
        errored = any(s in feedback for s in ("Traceback", "No such file", "not found"))
        if grade.grade_status in ("timeout", "error") or (errored and (grade.score or 0) == 0):
            last = feedback.strip().splitlines()[-1][:200] if feedback.strip() else ""
            fail(
                f"task {task.id}: grader errored on the untouched fixture: "
                f"{last or grade.grade_status}"
            )
        elif grade.score is not None and grade.score >= 1.0:
            warn(
                f"task {task.id}: the untouched fixture already scores 100%, so this "
                "task cannot show a difference"
            )
        elif grade.score is None:
            warn(f"task {task.id}: grader returned {grade.grade_status}; scores will be N/A")
        else:
            ok(f"task {task.id}: grader runs; untouched fixture scores {round(grade.score * 100)}%")

    sessions = len(cfg.models) * len(tasks) * cfg.runs * 2
    line = f"{sessions} agent sessions per full run"
    if cfg.harness == "claude" and cfg.claude.max_budget_usd:
        line += f" (at most ${sessions * cfg.claude.max_budget_usd:.2f} API-equivalent)"
    ok(line)
    if cfg.runs < 3:
        warn(f"runs: {cfg.runs} is fine for a smoke test, but use 5+ for conclusions")

    print()
    print(f"{ok_count} ok, {warnings} warning(s), {problems} problem(s)")
    return 1 if problems else 0


# ------------------------------------------------------------------------ run/results


def cmd_run(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    if not config_path.exists():
        print(
            f"Error: configuration file '{config_path}' not found. "
            "Run `skilldiff init` to create one.",
            file=sys.stderr,
        )
        return 1

    try:
        exp_config, tasks = load_experiment(config_path)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "runs", None):
        exp_config.runs = int(args.runs)
    if getattr(args, "parallel", None):
        exp_config.parallel = int(args.parallel)
    if getattr(args, "model", None):
        exp_config.models = list(args.model)
    task_filter = getattr(args, "task", None)
    if task_filter:
        tasks = [t for t in tasks if t.id in task_filter]
        if not tasks:
            print(f"No tasks match {task_filter}", file=sys.stderr)
            return 1

    quiet = bool(getattr(args, "quiet", False))
    runner = ExperimentRunner(
        exp_config,
        tasks,
        progress=None if quiet else (lambda msg: print(msg, flush=True)),
    )
    sessions = len(exp_config.models) * len(tasks) * exp_config.runs * 2
    print(
        f"Running '{exp_config.name}' with {exp_config.harness}: {len(exp_config.models)} "
        f"model(s) × {len(tasks)} task(s) × {exp_config.runs} run(s) × 2 arms = "
        f"{sessions} sessions",
        flush=True,
    )
    try:
        results = runner.run()
    except (ValueError, subprocess.SubprocessError) as exc:
        print(f"Experiment error: {exc}", file=sys.stderr)
        return 1

    print("\n" + _format_results(results))
    _print_report_paths(results)
    return 130 if results.get("interrupted") else 0


def _latest_run_dir(runs_parent: Path) -> Optional[Path]:
    if not runs_parent.exists():
        return None
    subdirs = [p for p in runs_parent.iterdir() if p.is_dir() and (p / "results.json").exists()]
    return sorted(subdirs)[-1] if subdirs else None


def _resolve_run_dir(args: argparse.Namespace) -> Optional[Path]:
    if getattr(args, "run_dir", None):
        return Path(args.run_dir)
    config = getattr(args, "config", None)
    candidates = []
    if config:
        candidates.append(Path(config).parent / "runs")
    candidates.append(Path("runs"))
    for parent in candidates:
        found = _latest_run_dir(parent)
        if found:
            return found
    return None


def cmd_results(args: argparse.Namespace) -> int:
    run_dir = _resolve_run_dir(args)
    if not run_dir:
        print("No experiment runs found.", file=sys.stderr)
        return 1

    results_file = run_dir / "results.json"
    if not results_file.exists():
        print(f"No results.json found in {run_dir}", file=sys.stderr)
        return 1

    with open(results_file, "r", encoding="utf-8") as f:
        results = json.load(f)

    if getattr(args, "json", False):
        print(json.dumps(results, indent=2))
        return 0
    if getattr(args, "markdown", False):
        md = run_dir / "report.md"
        if not md.exists():
            create_reports(results, run_dir)
        print(md.read_text(encoding="utf-8"))
        return 0

    print(_format_results(results))
    _print_report_paths(results)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    run_dir = _resolve_run_dir(args)
    if not run_dir or not (run_dir / "results.json").exists():
        print("No experiment run with results.json found.", file=sys.stderr)
        return 1
    results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    paths = create_reports(results, run_dir)
    results["report"] = {k: str(v) for k, v in paths.items()}
    (run_dir / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    _print_report_paths(results)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    from skilldiff.compare import compare_results, format_comparison, load_results

    try:
        a, _ = load_results(Path(args.run_a))
        b, _ = load_results(Path(args.run_b))
    except Exception as exc:
        print(f"Could not load runs: {exc}", file=sys.stderr)
        return 1
    comp = compare_results(a, b)
    if getattr(args, "json", False):
        print(json.dumps(comp, indent=2))
    else:
        print(format_comparison(comp))
    return 0


def _print_report_paths(results: dict) -> None:
    report = results.get("report", {}) or {}
    if report.get("html"):
        print(f"\nReport: {report['html']}")
    if report.get("md"):
        print(f"Markdown (for PRs): {report['md']}")
    elif report.get("qmd"):
        print(f"\nQuarto report: {report['qmd']}")


def _format_results(results: dict) -> str:
    name = results.get("name", "experiment")
    by_model = results.get("by_model", {})
    models = results.get("models", [])
    tasks_count = results.get("tasks_count", 0)
    runs_per_arm = results.get("runs_per_arm", 0)
    thresholds = results.get("thresholds") or (results.get("settings") or {}).get("thresholds")

    sections: list[str] = []
    comparison = results.get("comparison")
    if comparison:
        sections.append(
            f"Control (without PR): {comparison['control_commit']} (merge base)\n"
            f"Treatment (with PR): {comparison['treatment_commit']}"
        )
    if len(models) <= 1:
        model_name = models[0] if models else None
        model_data = by_model.get(model_name) if model_name else None
        source = model_data or results.get("overall", {})
        sections.append(
            render_report_table(
                experiment_name=name,
                control_metrics=source.get("control", {}),
                skill_metrics=source.get("skill", {}),
                models_count=len(models),
                tasks_count=tasks_count,
                runs_per_arm=runs_per_arm,
                paired=source.get("paired"),
                treatment_label="Treatment" if results.get("comparison") else "Skill",
                thresholds=thresholds,
            )
        )
    else:
        for model_name in models:
            model_data = by_model.get(model_name)
            if not model_data:
                continue
            sections.append(
                render_report_table(
                    experiment_name=name,
                    control_metrics=model_data["control"],
                    skill_metrics=model_data["skill"],
                    models_count=1,
                    tasks_count=tasks_count,
                    runs_per_arm=runs_per_arm,
                    model_name=model_name,
                    paired=model_data.get("paired"),
                    treatment_label="Treatment" if results.get("comparison") else "Skill",
                    thresholds=thresholds,
                )
            )

    warnings = results.get("warnings") or []
    if warnings:
        sections.append("Warnings:\n" + "\n".join(f"  - {w}" for w in warnings))
    return "\n\n".join(sections)


# ------------------------------------------------------------------------------ main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skilldiff",
        description="Measure what an Agent Skill changes: run the same tasks with and "
        "without the skill and compare quality, cost, and time.",
    )
    parser.add_argument("--version", action="version", version=f"skilldiff {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="Create an experiment (a runnable demo, or a template for your skill)"
    )
    init_parser.add_argument("--skill", help="Path to your skill (or a folder of skills)")
    init_parser.add_argument("--pr", type=int, help="Scaffold an evaluation of a GitHub PR number")
    init_parser.add_argument("--repo", help="Local repository containing the PR revisions")
    init_parser.add_argument("--base", help="PR target ref (default: origin/main)")
    init_parser.add_argument(
        "--harness", choices=sorted(HARNESS_BLOCKS), default="claude", help="Agent CLI to use"
    )
    init_parser.add_argument("--dir", default=".", help="Where to create the experiment")
    init_parser.add_argument("--force", "-f", action="store_true", help="Overwrite existing files")
    init_parser.set_defaults(func=cmd_init)

    check_parser = subparsers.add_parser(
        "check", help="Validate the config, agent CLI, skill, and graders without running agents"
    )
    check_parser.add_argument("--config", "-c", default="skilldiff.yaml")
    check_parser.add_argument(
        "--no-grade", action="store_true", help="Skip running graders on the untouched fixtures"
    )
    check_parser.set_defaults(func=cmd_check)

    run_parser = subparsers.add_parser("run", help="Run both arms of the experiment")
    run_parser.add_argument(
        "--config", "-c", default="skilldiff.yaml", help="Path to experiment config file"
    )
    run_parser.add_argument("--runs", "-r", type=int, help="Override number of runs per arm")
    run_parser.add_argument("--parallel", "-j", type=int, help="Pairs to run concurrently")
    run_parser.add_argument(
        "--model", "-m", action="append", help="Only run this model (repeatable)"
    )
    run_parser.add_argument(
        "--task", "-t", action="append", help="Only run this task id (repeatable)"
    )
    run_parser.add_argument("--quiet", "-q", action="store_true", help="Hide per-run progress")
    run_parser.set_defaults(func=cmd_run)

    results_parser = subparsers.add_parser(
        "results", help="Show the latest (or a specific) run's results"
    )
    results_parser.add_argument("run_dir", nargs="?", help="Path to specific run directory")
    results_parser.add_argument("--config", "-c", help="Look for runs next to this config")
    results_parser.add_argument("--json", action="store_true", help="Output results as JSON")
    results_parser.add_argument(
        "--markdown", action="store_true", help="Print the Markdown report (for PRs)"
    )
    results_parser.set_defaults(func=cmd_results)

    report_parser = subparsers.add_parser(
        "report", help="Regenerate the HTML/Markdown/Quarto reports for a run"
    )
    report_parser.add_argument("run_dir", nargs="?", help="Path to specific run directory")
    report_parser.add_argument("--config", "-c", help="Look for runs next to this config")
    report_parser.set_defaults(func=cmd_report)

    compare_parser = subparsers.add_parser(
        "compare", help="Compare two runs (skill revisions): score, adoption, efficiency, checks"
    )
    compare_parser.add_argument("run_a", help="First run dir or results.json")
    compare_parser.add_argument("run_b", help="Second run dir or results.json")
    compare_parser.add_argument("--json", action="store_true", help="Output comparison as JSON")
    compare_parser.set_defaults(func=cmd_compare)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()

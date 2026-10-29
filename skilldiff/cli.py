import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional, cast

import yaml

from skilldiff import __version__
from skilldiff.config import (
    ExperimentConfig,
    find_skill_dirs,
    load_experiment,
    read_skill_frontmatter,
    read_skill_name,
    validate_integer,
    validate_string_list,
)
from skilldiff.experiment import (
    ExperimentRunner,
    find_harness_inheritance,
    find_user_level_installs,
    safe_path_component,
)
from skilldiff.grader import Grader
from skilldiff.host_exposure import host_exposure_warnings
from skilldiff.persistence import atomic_json, read_json, run_lock
from skilldiff.preflight import host_sandbox_fix, host_sandbox_problems
from skilldiff.pricing import (
    MODEL_ALIASES,
    is_bare_family_alias,
    merge_pricing,
    one_call_cost_estimate,
)
from skilldiff.reporter import (
    _paired_comparison_metrics,
    build_decision_context,
    create_reports,
    render_evaluation_table,
    render_report_table,
)
from skilldiff.revisions import resolve_comparison
from skilldiff.runner import (
    EXTERNAL_ISOLATION,
    PROMPTING_MODES,
    AgentRunner,
    auth_login_hint,
    cached_which,
    claude_effective_permission_mode,
    claude_sandbox_settings,
    codex_sandbox_args,
    looks_like_auth_error,
    resolve_container_image,
    validate_container_auth,
)
from skilldiff.scope import scope_pattern_warnings
from skilldiff.stats import analysis_run, paired_comparison
from skilldiff.workspace import Workspace

DEFAULT_MODELS = {
    "claude": "claude-sonnet-5-5",
    "codex": "gpt-6.1-sol",
    "opencode": "deepseek-v4-pro",
    "antigravity": "gemini-3.8-flash",
}

HARNESS_BLOCKS = {
    "claude": """cost_basis: api-equivalent  # built-in rates; add pricing: only to override them
claude:
  auth: subscription        # or api_key (uses ANTHROPIC_API_KEY)
  effort: high
  max_turns: 30
  max_budget_usd: 2.00      # per session
  permission_mode: acceptEdits
  isolate: true             # project/local only; user-level skills/plugins can still leak
  sandbox: true             # Bash runs in Claude's sandbox without prompts, inside the workspace
  # profile: eval           # opt-in app eval: auto-approves commands INSIDE the sandbox and
  #                         # allows localhost binding; unsandboxed commands stay disabled
  allowed_domains: []       # hosts commands may reach, e.g. ["pypi.org", "*.npmjs.org"]
  allowed_tools: []         # e.g. ["Bash(my-cli *)"] when not using the sandbox
""",
    "codex": """codex:
  auth: stored
  sandbox: workspace-write  # no prompts; writes stay in the workspace
  network_access: false     # true lets commands install packages or fetch docs
""",
    "opencode": """opencode:
  service: go
  dangerously_skip_permissions: true  # passes --auto; use isolation: docker for untrusted skills
""",
    "antigravity": """antigravity:
  dangerously_skip_permissions: true  # print mode otherwise soft-denies; use isolation: docker
""",
}

# Opt-in app-evaluation template. The profile keeps Claude's OS sandbox and
# auto-approves commands inside it; it never allows unsandboxed commands.
CLAUDE_EVAL_HARNESS_BLOCK = """cost_basis: api-equivalent  # built-in rates; pricing: overrides
claude:
  auth: subscription
  effort: high
  max_turns: 30
  max_budget_usd: 2.00
  permission_mode: acceptEdits
  isolate: true             # project/local settings only; not a filesystem boundary
  sandbox: true
  # Opt-in: auto-approves commands inside the sandbox and allows localhost binding
  # for app servers. Graders and skill sources stay unreadable to commands.
  profile: eval
  allowed_domains: [localhost]  # add pypi.org, *.npmjs.org, etc. when tasks install
  allowed_tools: []

thresholds:
  acceptable_score_regression_pp: 5
  required_cost_reduction_pct: 10
  required_token_reduction_pct: 20
  meaningful_score_gain_pp: 5
"""

EXPERIMENT_YAML = """name: {name}

# Preset skill: agent without the skill vs the same agent with the skill.
preset: skill
# A skill directory (containing SKILL.md) or a folder of skills, e.g. a package's skills/.
skill: {skill}

harness: {harness}

models:
  - {model}

tasks:
  - ./tasks/*.yaml
  - ./tasks/dev/*.yaml
  - ./tasks/heldout/*.yaml   # frozen validation set; tune on dev tasks first

runs: 3                     # repetitions per arm; use 5+ as a starting point, not a rule
timeout_seconds: 1800       # one deadline for the whole conversation
parallel: 1                 # pairs to run at once

{skill_invocation_block}
# Reproducibility: fixed seed for balanced arm order (recorded per run).
# seed: 1234

# Failure / missing-result policy, decided before running (not after):
# failure_policy:
#   agent_failure: exclude  # omit failed sessions from comparisons; "zero" scores them 0
#   missing: exclude
# Harness errors are always excluded, even with agent_failure: zero.

# Practical decision thresholds (optional, shown in the verdict).
# Shipping requires confidence bounds to clear these, not point estimates:
# thresholds:
#   acceptable_score_regression_pp: 5   # tolerated drop, e.g. -5pp ok if cheaper
#   required_cost_reduction_pct: 10     # required saving, e.g. 10% cheaper
#   required_token_reduction_pct: 20    # session-token saving (compression)
#   meaningful_score_gain_pp: 5         # gain needed to call an improvement useful

# API-equivalent cost basis. Claude subscription runs default to API-equivalent
# cost using built-in rates keyed by full model ID, so no `pricing:` block is
# needed. Add one only to override the built-in table or price another provider.
# Haiku 5.5 tiers use per-request usage; without it, decision cost is N/A unless
# `tier_policy: upper_bound` asks for a labelled planning estimate.
# cost_basis: api-equivalent
# pricing:
#   source: https://www.anthropic.com/pricing
#   date: "2026-10-19"
#   currency: USD
#   rates:
#     {model}:
#       input: 2.00
#       output: 10.00
#       cache_read: 0.10
#       cache_write: 2.50

{harness_block}"""

SKILL_AB_YAML = """name: {name}

# Preset {preset}: {preset_note}
preset: {preset}
# Skill A/B: one experiment runs skill A vs skill B on identical fixtures,
# interleaved with paired results. Set include_baseline to also run a
# no-skill arm per pair (shows whether either revision helps at all).
skill_a: {skill_a}
skill_b: {skill_b}
include_baseline: {baseline}

harness: {harness}

models:
  - {model}

tasks:
  - ./tasks/*.yaml
  - ./tasks/dev/*.yaml
  - ./tasks/heldout/*.yaml   # frozen validation set; tune on dev tasks first

runs: 3
timeout_seconds: 1800
parallel: 1

{skill_invocation_block}
# Decision criteria, fixed before running (illustrative compression targets):
# thresholds:
#   acceptable_score_regression_pp: 2   # minified may lose at most 2pp
#   required_token_reduction_pct: 20    # with at least 20% fewer session tokens
#   required_cost_reduction_pct: 10

# API-equivalent cost basis. Claude subscription runs default to API-equivalent
# cost using built-in rates keyed by full model ID. Add `pricing:` only to
# override the built-in table or price another provider:
# cost_basis: api-equivalent
# pricing:
#   source: https://www.anthropic.com/pricing
#   date: "2026-10-19"
#   rates:
#     {model}:
#       input: 2.00
#       output: 10.00
#       cache_read: 0.10
#       cache_write: 2.50

{harness_block}"""

# How the agent is told to use the skill, written into a generated config.
# These are substituted values, not template text, so "{skill}" stays literal.
SKILL_INVOCATION_BLOCKS = {
    "implicit": (
        "# How the agent is told to use the skill:\n"
        "#   implicit: the prompt never names it, so adoption measures whether the\n"
        "#     SKILL.md description triggers on the task wording. This is the default.\n"
        "#   explicit: the prompt names the skill, so adoption measures whether the\n"
        "#     agent uses a skill it was told to use. Run the same experiment both\n"
        "#     ways to see how much discovery your description is worth.\n"
        "# skill_invocation: implicit\n"
        '# skill_invocation_prompt: "Use the {skill} skill for this task."\n'
    ),
    "explicit": (
        "# How the agent is told to use the skill:\n"
        "#   implicit: the prompt never names it, so adoption measures whether the\n"
        "#     SKILL.md description triggers on the task wording.\n"
        "#   explicit: the prompt names the skill, so adoption measures whether the\n"
        "#     agent uses a skill it was told to use. Run the same experiment both\n"
        "#     ways to see how much discovery your description is worth.\n"
        "skill_invocation: explicit\n"
        '# skill_invocation_prompt: "Use the {skill} skill for this task."\n'
    ),
}

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

grader_ignore:
  - "__pycache__/*"
  - "*.pyc"

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
    False,  # TODO: replace with real checks, e.g. run tests or inspect files
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


def _nested_task(template: str, task_id: str) -> str:
    """Adapt a task template for a split directory under tasks/."""
    _, body = template.split("\n", 1)
    return (f"id: {task_id}\n" + body).replace("../fixtures/", "../../fixtures/").replace(
        "$SKILLDIFF_TASK_DIR/../graders/", "$SKILLDIFF_TASK_DIR/../../graders/"
    )


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(getattr(args, "dir", None) or ".")
    force = bool(getattr(args, "force", False))
    harness = getattr(args, "harness", None) or "claude"
    template = getattr(args, "template", None)
    if template == "claude-eval":
        if getattr(args, "harness", None) not in (None, "claude"):
            print("--template claude-eval requires --harness claude.", file=sys.stderr)
            return 1
        harness = "claude"
    skill_arg: Optional[str] = getattr(args, "skill", None)
    skill_a_arg: Optional[str] = getattr(args, "skill_a", None)
    skill_b_arg: Optional[str] = getattr(args, "skill_b", None)
    preset_arg: Optional[str] = getattr(args, "preset", None)
    if preset_arg is not None:
        preset_arg = str(preset_arg).strip().lower()
        if preset_arg not in {"skill", "pr", "revision", "compression"}:
            print("--preset must be skill, pr, revision, or compression.", file=sys.stderr)
            return 1
    config_file = root / "skilldiff.yaml"

    if config_file.exists() and not force:
        print(f"{config_file} already exists. Use --force to overwrite.", file=sys.stderr)
        return 1

    pr_number = getattr(args, "pr", None)
    if template and (skill_a_arg or skill_b_arg or pr_number is not None):
        print(
            "--template claude-eval supports the single-skill or demo scaffold.",
            file=sys.stderr,
        )
        return 1
    if pr_number is not None:
        if (
            skill_arg
            or skill_a_arg
            or skill_b_arg
            or pr_number <= 0
            or not getattr(args, "repo", None)
        ):
            print(
                "--pr requires a positive PR number and --repo, "
                "without --skill/--skill-a/--skill-b.",
                file=sys.stderr,
            )
            return 1
        return _init_pr(args, root, force, harness)
    if getattr(args, "repo", None) or getattr(args, "base", None):
        print("--repo and --base require --pr.", file=sys.stderr)
        return 1
    if getattr(args, "pr_install", False):
        print("--pr-install requires --pr and --repo.", file=sys.stderr)
        return 1

    if (skill_a_arg or skill_b_arg) and skill_arg:
        print("Use either --skill or --skill-a/--skill-b, not both.", file=sys.stderr)
        return 1
    if bool(skill_a_arg) != bool(skill_b_arg):
        print("Skill A/B mode requires both --skill-a and --skill-b.", file=sys.stderr)
        return 1
    if skill_a_arg and skill_b_arg:
        if preset_arg not in (None, "revision", "compression"):
            print("--preset revision/compression go with --skill-a/--skill-b.", file=sys.stderr)
            return 1
        return _init_skill_ab(args, root, force, harness, preset_arg or "revision")
    if preset_arg in ("revision", "compression"):
        print("--preset revision/compression require --skill-a and --skill-b.", file=sys.stderr)
        return 1
    if preset_arg == "pr" and pr_number is None:
        print("--preset pr requires --pr and --repo.", file=sys.stderr)
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
        if template == "claude-eval":
            files = {
                root / "tasks" / "dev" / "my-first-task.yaml": _nested_task(
                    CUSTOM_TASK_YAML, "my-first-task"
                ),
                root / "tasks" / "heldout" / "my-held-out-task.yaml": _nested_task(
                    CUSTOM_TASK_YAML, "my-held-out-task"
                ),
                root / "graders" / "my_first_task.py": CUSTOM_GRADER,
            }
        else:
            files = {
                root / "tasks" / "my-first-task.yaml": CUSTOM_TASK_YAML,
                root / "tasks" / "heldout" / "my-held-out-task.yaml": _nested_task(
                    CUSTOM_TASK_YAML, "my-held-out-task"
                ),
                root / "graders" / "my_first_task.py": CUSTOM_GRADER,
            }
    else:
        skill_value = "./skills/changelog-style"
        name = "changelog-style-demo"
        if template == "claude-eval":
            files = {
                root / "skills" / "changelog-style" / "SKILL.md": DEMO_SKILL_MD,
                root / "tasks" / "dev" / "changelog-entry.yaml": _nested_task(
                    DEMO_TASK_YAML, "changelog-entry"
                ),
                root / "tasks" / "heldout" / "changelog-entry-heldout.yaml": _nested_task(
                    DEMO_TASK_YAML, "changelog-entry-heldout"
                ),
                root / "fixtures" / "changelog" / "CHANGELOG.md": DEMO_CHANGELOG,
                root / "graders" / "changelog_entry.py": DEMO_GRADER,
            }
        else:
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
        harness_block=(
            CLAUDE_EVAL_HARNESS_BLOCK
            if template == "claude-eval"
            else HARNESS_BLOCKS[harness]
        ),
        skill_invocation_block=SKILL_INVOCATION_BLOCKS[
            getattr(args, "skill_invocation", None) or "implicit"
        ],
    )
    if getattr(args, "container_install", None):
        ci = args.container_install
        val = ci[0] if len(ci) == 1 else ci
        config_text += f"\n{yaml.safe_dump({'container_install': val})}\n"
    if harness == "claude":
        config_text = config_text.replace(
            f"  - {DEFAULT_MODELS['claude']}\n",
            f"  - {DEFAULT_MODELS['claude']}\n"
            "  # - claude-haiku-5-5   # pin a full ID if you switch models\n",
            1,
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
    if template == "claude-eval":
        print(
            "Note: claude.profile: eval keeps Claude's OS sandbox but auto-approves "
            "commands inside it and allows localhost binding. It is opt-in; the canary "
            "must pass before a run."
        )
    if skill_arg:
        print(
            "Warning: graders/my_first_task.py is a placeholder template that fails until replaced."
        )
        print("Next steps:")
        print("  1. Put a small test project in fixtures/my-project/.")
        print("  2. Fill in tasks/my-first-task.yaml and graders/my_first_task.py.")
        print("  3. skilldiff check      # validate the setup and graders")
        print("  4. skilldiff run --split dev --runs 1")
    else:
        print("This is a runnable demo: a made-up changelog convention the agent can only")
        print("follow if it reads the skill.")
        print("  skilldiff check")
        print("  skilldiff run --split dev --runs 1")
    return 0


def _init_skill_ab(
    args: argparse.Namespace, root: Path, force: bool, harness: str, preset: str = "revision"
) -> int:
    from skilldiff.config import find_skill_dirs as _find
    from skilldiff.config import read_skill_frontmatter as _fm

    skill_a = Path(args.skill_a).expanduser().resolve()
    skill_b = Path(args.skill_b).expanduser().resolve()
    if skill_a == skill_b:
        print("skill_a and skill_b must be different directories.", file=sys.stderr)
        return 1
    for label, p in (("skill-a", skill_a), ("skill-b", skill_b)):
        if not _find(p):
            print(f"No SKILL.md found in {p} or its subdirectories ({label}).", file=sys.stderr)
            return 1
    if preset == "compression":
        # Fail fast when the trigger differs: adoption would confound the result.
        dirs_a, dirs_b = _find(skill_a), _find(skill_b)
        if len(dirs_a) != len(dirs_b):
            print("compression needs the same skills in A and B.", file=sys.stderr)
            return 1
        for da, db in zip(sorted(dirs_a), sorted(dirs_b)):
            ma, mb = _fm(da), _fm(db)
            if str(ma.get("name", da.name)).strip() != str(mb.get("name", db.name)).strip() or str(
                ma.get("description", "")
            ).strip() != str(mb.get("description", "")).strip():
                print(
                    "compression needs identical skill name and description in A and B "
                    "(compress the body, not the trigger).",
                    file=sys.stderr,
                )
                return 1
    base = root.resolve()
    rel_a = os.path.relpath(skill_a, base)
    rel_b = os.path.relpath(skill_b, base)
    if not rel_a.startswith("."):
        rel_a = f"./{rel_a}"
    if not rel_b.startswith("."):
        rel_b = f"./{rel_b}"
    text = SKILL_AB_YAML.format(
        name=f"{skill_a.name}-vs-{skill_b.name}",
        preset=preset,
        preset_note=(
            "original vs minified; triggers must match, decision needs preserved "
            "quality plus proven token savings"
            if preset == "compression"
            else "skill A vs skill B on identical fixtures"
        ),
        skill_a=rel_a,
        skill_b=rel_b,
        baseline="true" if getattr(args, "include_baseline", False) else "false",
        harness=harness,
        model=DEFAULT_MODELS[harness],
        harness_block=HARNESS_BLOCKS[harness],
        skill_invocation_block=SKILL_INVOCATION_BLOCKS[
            getattr(args, "skill_invocation", None) or "implicit"
        ],
    )
    if getattr(args, "container_install", None):
        ci = args.container_install
        val = ci[0] if len(ci) == 1 else ci
        text += f"\n{yaml.safe_dump({'container_install': val})}\n"
    if harness == "claude":
        text = text.replace(
            f"  - {DEFAULT_MODELS['claude']}\n",
            f"  - {DEFAULT_MODELS['claude']}\n"
            "  # - claude-haiku-5-5   # pin a full ID if you switch models\n",
            1,
        )
    (root / "skilldiff.yaml").parent.mkdir(parents=True, exist_ok=True)
    (root / "skilldiff.yaml").write_text(text, encoding="utf-8")
    _write(root / "tasks" / "my-first-task.yaml", CUSTOM_TASK_YAML, force)
    _write(root / "tasks" / "heldout" / "my-first-task.yaml",
           _nested_task(CUSTOM_TASK_YAML, "my-held-out-task"), force)
    _write(root / "graders" / "my_first_task.py", CUSTOM_GRADER, force)
    (root / "fixtures" / "my-project").mkdir(parents=True, exist_ok=True)
    print("Warning: graders/my_first_task.py is a placeholder template that fails until replaced.")
    print(f"Initialized skill A/B experiment in {root} (A={rel_a}, B={rel_b})")
    print("Same fixtures run with skill A vs skill B, interleaved and paired.")
    return 0


def _init_pr(args: argparse.Namespace, root: Path, force: bool, harness: str) -> int:
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"Repository not found: {repo}", file=sys.stderr)
        return 1
    base = args.base or "origin/main"
    head = f"refs/pull/{args.pr}/head"
    pr_mode = getattr(args, "pr_mode", None) or "agent"
    pr_pair = getattr(args, "pr_pair", None) or "merge-base"
    if pr_mode not in {"agent", "correctness"}:
        print("--pr-mode must be 'agent' or 'correctness'.", file=sys.stderr)
        return 1
    if pr_pair not in {"merge-base", "base-merge"}:
        print("--pr-pair must be 'merge-base' or 'base-merge'.", file=sys.stderr)
        return 1
    config: dict[str, Any] = {
        "name": f"pr-{args.pr}-eval",
        # Preset pr: code without PR changes vs code with PR changes.
        "preset": "pr",
        "pr": {
            "repo": os.path.relpath(repo, root.resolve()),
            "base": base,
            "head": head,
            "mode": pr_mode,
            "pair": pr_pair,
        },
        "harness": harness,
        "models": [DEFAULT_MODELS[harness]],
        "tasks": ["./tasks/*.yaml", "./tasks/dev/*.yaml", "./tasks/heldout/*.yaml"],
        "runs": 3,
        "timeout_seconds": 1800,
        "parallel": 1,
    }
    if getattr(args, "pr_install", False):
        config["pr"]["install"] = True
    if getattr(args, "container_install", None):
        ci = args.container_install
        config["container_install"] = ci[0] if len(ci) == 1 else ci
    text = yaml.safe_dump(config, sort_keys=False) + "\n" + HARNESS_BLOCKS[harness]
    if harness == "claude":
        text = text.replace(
            f"- {DEFAULT_MODELS['claude']}\n",
            f"- {DEFAULT_MODELS['claude']}\n"
            "# - claude-haiku-5-5   # pin a full ID if you switch models\n",
            1,
        )
    _write(root / "skilldiff.yaml", text, force)
    task_template = """id: my-first-task
# Both arms start from pr.repo at their respective revisions. Do not set task.repo.
prompt: |
  TODO: describe a realistic task that uses the feature introduced by this PR.
  Use the same request for both arms; do not mention which revision is available.
grader:
  type: command
  command: python3 "$SKILLDIFF_TASK_DIR/../graders/my_first_task.py"
"""
    _write(root / "tasks" / "my-first-task.yaml", task_template, force)
    _write(root / "tasks" / "heldout" / "my-held-out-task.yaml",
           _nested_task(task_template, "my-held-out-task"), force)
    _write(root / "graders" / "my_first_task.py", CUSTOM_GRADER, force)
    print("Warning: graders/my_first_task.py is a placeholder template that fails until replaced.")
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", f"{base}...{head}"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            proc = subprocess.run(
                ["git", "-C", str(repo), "diff", "--name-only", base, head],
                capture_output=True,
                text=True,
            )
        if proc.returncode == 0:
            diff_lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            if any("SKILL.md" in line for line in diff_lines) and pr_mode != "agent":
                print(
                    "Warning: The PR touches a SKILL.md path, but the chosen mode "
                    f"'{pr_mode}' does not install skills into workspaces. Suggest using "
                    "the skill A/B preset (--skill-a / --skill-b) as an alternative."
                )
    except Exception:
        pass
    print(f"Initialized PR #{args.pr} experiment in {root}")
    print("Fetch the GitHub PR head into your local repository before check/run:")
    print(f"  git -C {shlex.quote(str(repo))} fetch origin {head}:{head}")
    print(f"Ensure base ref {base!r} is available (use a pre-merge base for merged PRs).")
    print("Fill in tasks/my-first-task.yaml and graders/my_first_task.py, then run")
    print(f"  skilldiff check -c {shlex.quote(str(root / 'skilldiff.yaml'))}")
    print(f"  skilldiff run -c {shlex.quote(str(root / 'skilldiff.yaml'))} --split dev --runs 1")
    return 0


# ----------------------------------------------------------------------------- check


def permission_summary(cfg: ExperimentConfig) -> list[tuple[str, str]]:
    """What agent sessions may do without asking, as (ok|warn, message) lines.

    Headless sessions cannot answer permission prompts, so anything that would
    ask is refused. Say up front which tools will run and where they are confined.
    """
    harness, isolation = cfg.harness, cfg.isolation
    external = isolation in EXTERNAL_ISOLATION
    lines: list[tuple[str, str]] = []
    if harness == "claude":
        claude = cfg.claude
        profile = getattr(claude, "profile", None)
        effective_mode = claude_effective_permission_mode(claude)
        if claude_sandbox_settings(claude, isolation):
            hosts = list(claude.allowed_domains)
            if profile == "eval":
                for host in ("localhost", "127.0.0.1", "[::1]"):
                    if host not in hosts:
                        hosts.append(host)
            network = (", ".join(hosts) if hosts
                       else "none (set claude.allowed_domains)")
            lines.append(("ok", "claude permissions: Bash runs in Claude's sandbox without "
                                "prompts; writes stay in the workspace; graders and skill "
                                f"sources are unreadable to commands; network hosts: {network}"))
            if profile == "eval":
                lines.append(("warn", "claude.profile: eval keeps the sandbox but auto-approves "
                                      "commands inside it and allows localhost binding; "
                                      "unsandboxed commands stay disabled. The canary must pass "
                                      "before a run."))
        elif (
            claude.sandbox
            and isolation in EXTERNAL_ISOLATION
            and effective_mode in PROMPTING_MODES
        ):
            boundary = "macOS" if isolation == "macos" else isolation
            lines.append(("ok", f"claude permissions: bypassPermissions inside the {boundary} "
                                "isolation boundary"))
            if profile == "eval":
                lines.append(("warn", "claude.profile: eval is redundant inside external "
                                      "isolation; the outer boundary already bypasses prompts"))
        elif effective_mode in PROMPTING_MODES and not claude.allowed_tools:
            lines.append(("warn", f"claude permission_mode {effective_mode} without "
                                  "claude.sandbox or allowed_tools: headless sessions deny Bash "
                                  "commands, so agents cannot run code or tests"
                          + ("; inside a container use permission_mode: bypassPermissions"
                             if external else "")))
    elif harness == "codex":
        args = codex_sandbox_args(cfg.codex, isolation)
        if "--dangerously-bypass-approvals-and-sandbox" in args:
            where = (f"the {isolation} isolation boundary" if external
                     else "nothing (no sandbox)")
            lines.append(("ok" if external else "warn",
                          f"codex permissions: no prompts; commands confined by {where}"))
        else:
            network = "on" if "sandbox_workspace_write.network_access=true" in args else (
                "off (set codex.network_access: true to allow installs)")
            lines.append(("ok", f"codex permissions: {cfg.codex.sandbox} sandbox, no prompts; "
                                f"network {network}"))
    elif harness == "opencode" and not external:
        lines.append(("warn", "opencode permissions: "
                      + ("tools auto-approved; OpenCode has no OS sandbox, so use "
                         "isolation: docker to confine commands"
                         if cfg.opencode.dangerously_skip_permissions
                         else "prompts are auto-rejected in headless runs")))
    elif harness == "antigravity":
        lines.append(("warn", "antigravity permissions: "
                      + ("tools auto-approved; commands are not confined unless you use "
                         "isolation: docker"
                         if cfg.antigravity.dangerously_skip_permissions
                         else "tools that need approval are soft-denied in print mode")))
    return lines


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
        print(f"  warn  {msg}", flush=True)

    def fail(msg: str) -> None:
        nonlocal problems
        problems += 1
        print(f"  FAIL  {msg}", flush=True)

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
    if cfg.cost_basis == "api-equivalent" and cfg.pricing_unpriced and not (
        cfg.pr and getattr(cfg.pr, "mode", "agent") == "correctness"
    ):
        fail(
            "cost_basis: api-equivalent has no rates for "
            + ", ".join(f"'{model}'" for model in cfg.pricing_unpriced)
            + ". Pin a full Claude model ID with built-in rates or add pricing.rates."
        )
    image_id = None
    if cfg.isolation in {"docker", "podman"}:
        try:
            validate_container_auth(cfg)
            image_id = resolve_container_image(cfg.isolation, cfg.container_image)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            fail(f"container configuration: {exc}")
            return 1
        ok(f"{cfg.isolation} image: {image_id}")

    comparison = None
    if cfg.pr:
        try:
            comparison = resolve_comparison(cfg.pr)
        except (ValueError, subprocess.SubprocessError) as exc:
            fail(str(exc))
            return 1
        ok(f"control: {comparison['control_commit']}")
        ok(f"treatment: {comparison['treatment_commit']}")
        ok(
            f"PR workflow: {comparison.get('mode', 'agent')} "
            f"(pair {comparison.get('pair', 'merge-base')})"
        )
        if comparison.get("mode") == "correctness":
            ok("correctness mode: graders run on untouched revisions, no agent sessions")
            if comparison.get("touched_skills"):
                warn(
                    f"PR touches skill(s) {', '.join(comparison['touched_skills'])}, but "
                    "pr.mode is 'correctness' which does not install skills; suggest using "
                    "pr.mode 'agent' or skill A/B preset (--skill-a / --skill-b)"
                )
        elif comparison.get("touched_skills"):
            ok(
                f"PR touches skill(s): {', '.join(comparison['touched_skills'])}; "
                "installed into agent workspaces"
            )
        if getattr(cfg.pr, "install", False):
            if cfg.isolation in {"docker", "podman"}:
                ok(
                    "PR container install: per-arm derived images will be built "
                    f"from PR checkouts (spec: {cfg.pr.install})"
                )
            else:
                warn("pr.install is configured but isolation is not docker or podman")

    if cfg.container_install:
        if cfg.isolation in {"docker", "podman"}:
            ok(
                f"container install: packages {cfg.container_install} "
                "will be installed into container image"
            )
        else:
            warn("container_install is configured but isolation is not docker or podman")

    if getattr(cfg, "preset", None):
        ok(f"preset: {cfg.preset}")

    invocation = getattr(cfg, "skill_invocation", "implicit")
    if invocation == "explicit":
        ok(
            "skill_invocation: explicit - the prompt names the skill, so adoption "
            "measures obedience, not discovery"
        )
        template = getattr(cfg, "skill_invocation_prompt", None)
        ok(
            f"skill_invocation_prompt: {template}"
            if template
            else "skill_invocation_prompt: default (names the skill)"
        )
    else:
        ok("skill_invocation: implicit - the prompt never names the skill")

    overrides = [
        f"{t.id} ({t.skill_invocation})"
        for t in tasks
        if getattr(t, "skill_invocation", None)
    ]
    if overrides:
        ok(
            "task skill_invocation overrides: "
            + ", ".join(overrides)
            + " - those tasks measure a different thing from the rest"
        )

    if cfg.is_skill_comparison:
        ok(f"skill A/B: {cfg.skill_a} vs {cfg.skill_b}")
        if cfg.include_baseline:
            ok("include_baseline: a no-skill arm runs per pair in balanced rotation")
        # Warn when revisions are identical (hash check is in preflight too).
        try:
            from skilldiff.experiment import collect_provenance as _prov

            prov = _prov(cfg, tasks)
            roles: dict[str, list[str]] = {}
            for s in prov.get("skills") or []:
                roles.setdefault(s.get("role"), []).append(s.get("hash"))
            if roles.get("skill_a") == roles.get("skill_b") and roles.get("skill_a"):
                warn("skill_a and skill_b have identical hashes")
            sizes = prov.get("source_bytes") or {}
            if sizes.get("skill_a") or sizes.get("skill_b"):
                ba = int(sizes.get("skill_a") or 0)
                bb = int(sizes.get("skill_b") or 0)
                pct = (bb - ba) / ba * 100 if ba else 0
                ok(f"source size: {ba} → {bb} bytes ({pct:+.1f}%)")
        except Exception:
            pass
        if getattr(cfg, "preset", None) == "compression":
            # Triggers must match or adoption confounds the body comparison.
            from skilldiff.config import read_skill_frontmatter as _fm2

            dirs_a = sorted(cfg.skill_a_dirs)
            dirs_b = sorted(cfg.skill_b_dirs)
            mismatch = False
            for da, db in zip(dirs_a, dirs_b):
                ma, mb = _fm2(da), _fm2(db)
                if str(ma.get("name", da.name)).strip() != str(
                    mb.get("name", db.name)
                ).strip() or str(ma.get("description", "")).strip() != str(
                    mb.get("description", "")
                ).strip():
                    mismatch = True
            if mismatch:
                fail("compression: skill name/description differ between A and B")
                return 1
            ok("compression: triggers match (name and description identical)")

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

    sandboxed = host_sandbox_problems(cfg.harness, cfg.isolation)
    for problem in sandboxed:
        fail(f"host: {problem}")
    if sandboxed:
        print(f"        fix: {host_sandbox_fix()}", flush=True)

    runner = AgentRunner()
    binary = runner.binary_for(cfg.harness, cfg)
    capabilities_ok = False
    if image_id:
        resolved = getattr(cfg, cfg.harness).bin_path or Path(binary).name
    else:
        resolved = cached_which(binary) or (binary if Path(binary).exists() else None)
    if not resolved:
        fail(f"{cfg.harness} CLI not found (`{binary}`); install it or set bin_path")
    else:
        try:
            with tempfile.TemporaryDirectory(prefix="skilldiff-capabilities-") as tmp:
                capabilities = runner.capabilities(Path(tmp), cfg, Path(tmp))
            capabilities_ok = True
            ok(f"{cfg.harness} CLI: {capabilities['binary']} {capabilities['version']}")
            ok("required current CLI commands and output formats are available")
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            fail(f"{cfg.harness} CLI capability check: {exc}")

    for level, message in permission_summary(cfg):
        (ok if level == "ok" else warn)(message)

    if cfg.harness == "claude":
        if cfg.claude.auth == "subscription" and os.environ.get("ANTHROPIC_API_KEY"):
            ok("ANTHROPIC_API_KEY is set but will be removed; runs use your subscription")
        if (cfg.isolation == "local" and cfg.claude.auth == "api_key"
                and not os.environ.get("ANTHROPIC_API_KEY")):
            fail("claude.auth is api_key but ANTHROPIC_API_KEY is not set")
        if (
            claude_effective_permission_mode(cfg.claude) == "bypassPermissions"
            and cfg.isolation == "local"
            and cfg.claude.profile != "eval"
        ):
            warn("bypassPermissions lets agents run any command outside the workspace")
        if (
            resolved
            and capabilities_ok
            and not sandboxed
            and cfg.isolation == "local"
            and not (cfg.pr and getattr(cfg.pr, "mode", "agent") == "correctness")
            and not getattr(args, "no_probe", False)
            and not os.environ.get("SKILLDIFF_MOCK_RUNNER")
        ):
            for model in cfg.models:
                probe = runner.probe_claude_auth(model, cfg.claude, pricing=cfg.pricing)
                if not probe.ok:
                    if probe.budget_failure:
                        estimate = one_call_cost_estimate(cfg.pricing, model)
                        suggested = max(
                            float(cfg.claude.max_budget_usd or 0),
                            (estimate or 0.25) * 4,
                        )
                        fail(
                            f"claude auth probe hit its budget cap for {model} "
                            f"({probe.detail}). Raise claude.max_budget_usd to at least "
                            f"${suggested:.2f} and rerun `skilldiff check`."
                        )
                    elif looks_like_auth_error(probe.detail):
                        fail(
                            f"claude auth probe failed ({probe.detail}). "
                            f"{auth_login_hint('claude')} and rerun `skilldiff check`."
                        )
                    else:
                        fail(f"claude auth probe failed for {model}: {probe.detail}")
                    continue
                ok(f"claude auth probe: {model} authenticated")
                if probe.cost is not None:
                    print(
                        f"        harness cost for this call: ${probe.cost:.4f}",
                        flush=True,
                    )
                observed = max(
                    one_call_cost_estimate(cfg.pricing, model) or 0.0,
                    float(probe.cost or 0.0),
                )
                if (
                    observed
                    and cfg.claude.max_budget_usd is not None
                    and cfg.claude.max_budget_usd < observed * 4
                ):
                    fail(
                        f"claude.max_budget_usd is ${cfg.claude.max_budget_usd:.2f}, "
                        f"below four times the observed ${observed:.2f} cost of one "
                        f"call for {model}. Raise it to at least ${observed * 4:.2f}."
                    )
                    continue
                if probe.mismatch:
                    fail(
                        f"claude model: configured '{model}' was served as "
                        f"{', '.join(probe.served_models)}. Pin the full model ID the "
                        "harness actually serves."
                    )
                    continue
                if not probe.served_models:
                    warn(
                        f"claude model: the harness did not report which model served "
                        f"{model}; the served-model check is unverified."
                    )
                if probe.extra_served:
                    warn(
                        f"claude model: {model} also served "
                        f"{', '.join(probe.extra_served)}; reports attribute usage to the "
                        "configured model."
                    )
                if probe.unrecognized_models:
                    if cfg.cost_basis == "harness":
                        fail(
                            "claude model: the CLI does not recognize "
                            f"{', '.join(probe.unrecognized_models)} and cost_basis is "
                            "harness. Harness cost is unreliable for this model; use "
                            "cost_basis: api-equivalent with a pinned model or rates."
                        )
                    else:
                        warn(
                            "claude model: the CLI does not recognize "
                            f"{', '.join(probe.unrecognized_models)}; API-equivalent cost "
                            "uses the configured rates."
                        )
                canary = runner.probe_claude_sandbox_canary(
                    model, cfg.claude, pricing=cfg.pricing
                )
                canary_problems = []
                if canary.denials:
                    canary_problems.append(
                        f"{len(canary.denials)} command shape(s) denied "
                        f"({'; '.join(canary.denials)})"
                    )
                if canary.missing:
                    canary_problems.append(
                        f"{len(canary.missing)} shape(s) not run "
                        f"({'; '.join(canary.missing)})"
                    )
                if canary.failed:
                    canary_problems.append(
                        f"{len(canary.failed)} shape(s) failed "
                        f"({'; '.join(canary.failed)})"
                    )
                if canary_problems or not canary.ok:
                    fail(
                        "claude sandbox canary: "
                        + ("; ".join(canary_problems) or canary.detail or "unknown failure")
                    )
                else:
                    ok(
                        "claude sandbox canary: representative command shapes permitted"
                        + (" (including eval app shapes)" if cfg.claude.profile == "eval" else "")
                    )
    elif cfg.harness == "codex":
        if (
            resolved
            and capabilities_ok
            and not sandboxed
            and cfg.isolation == "local"
            and not (cfg.pr and getattr(cfg.pr, "mode", "agent") == "correctness")
            and not getattr(args, "no_probe", False)
            and not os.environ.get("SKILLDIFF_MOCK_RUNNER")
        ):
            for model in cfg.models:
                probe = runner.probe_codex_auth(model, cfg.codex)
                if not probe.ok:
                    if looks_like_auth_error(probe.detail):
                        fail(
                            f"codex auth probe failed ({probe.detail}). "
                            f"{auth_login_hint('codex')} and rerun `skilldiff check`."
                        )
                    else:
                        fail(f"codex auth probe failed for {model}: {probe.detail}")
                    continue
                ok(f"codex auth probe: {model} authenticated")
    elif cfg.harness == "opencode":
        if (
            resolved
            and capabilities_ok
            and not sandboxed
            and cfg.isolation == "local"
            and not (cfg.pr and getattr(cfg.pr, "mode", "agent") == "correctness")
            and not getattr(args, "no_probe", False)
            and not os.environ.get("SKILLDIFF_MOCK_RUNNER")
        ):
            for model in cfg.models:
                probe = runner.probe_opencode_auth(model, cfg.opencode)
                if not probe.ok:
                    if looks_like_auth_error(probe.detail):
                        fail(
                            f"opencode auth probe failed ({probe.detail}). "
                            f"{auth_login_hint('opencode')} and rerun `skilldiff check`."
                        )
                    else:
                        fail(f"opencode auth probe failed for {model}: {probe.detail}")
                    continue
                ok(f"opencode auth probe: {model} authenticated")
    elif cfg.harness == "antigravity":
        if (
            resolved
            and capabilities_ok
            and not sandboxed
            and cfg.isolation == "local"
            and not (cfg.pr and getattr(cfg.pr, "mode", "agent") == "correctness")
            and not getattr(args, "no_probe", False)
            and not os.environ.get("SKILLDIFF_MOCK_RUNNER")
        ):
            for model in cfg.models:
                probe = runner.probe_antigravity_auth(model, cfg.antigravity)
                if not probe.ok:
                    if looks_like_auth_error(probe.detail):
                        fail(
                            f"antigravity auth probe failed ({probe.detail}). "
                            f"{auth_login_hint('antigravity')} and rerun `skilldiff check`."
                        )
                    else:
                        fail(f"antigravity auth probe failed for {model}: {probe.detail}")
                    continue
                ok(f"antigravity auth probe: {model} authenticated")

    installs = [] if image_id else find_user_level_installs(cfg.skill_names, cfg.harness)
    if installs:
        warn("Advisory filesystem scan found user-level target skills; runtime discovery "
             "determines control contamination: " + ", ".join(installs))

    # Instructions/plugins/memory can leak into both arms even when the skill
    # itself is project-local. Report the harness inheritance paths up front.
    inheritance = (
        find_harness_inheritance(cfg.skill_names, cfg.harness)
        if cfg.isolation == "local" else []
    )
    if inheritance:
        warn(
            "harness inheritance outside skills may leak into both arms: "
            + ", ".join(inheritance)
        )

    for message in host_exposure_warnings(
        cfg, config_path.resolve().parent / "runs", scan_home=getattr(args, "scan_home", False)
    ):
        warn(message)
    if getattr(args, "scan_home", False) and cfg.isolation in {"docker", "podman"}:
        ok("host scan skipped: host HOME and run snapshots are not mounted into agent containers")
    if cfg.harness == "codex" and not cfg.codex.verify_skills:
        warn("Codex discovery disabled: skill availability is unverified")

    # Failure policy is pre-registered, not decided after seeing results.
    fp = dict(getattr(cfg, "failure_policy", {}) or {})
    if fp:
        ok(f"failure policy: {', '.join(f'{k}={v}' for k, v in fp.items())}")
    if getattr(cfg, "seed", None) is not None:
        ok(f"seed: {cfg.seed} (balanced arm order is reproducible)")

    for task in tasks:
        task_dir = task.source_path.parent if task.source_path else Path(".")
        fixture = (
            cfg.pr.repo if cfg.pr else ((task_dir / task.repo).resolve() if task.repo else None)
        )
        mounted_fixtures = [
            (task_dir / item).resolve() for item in (getattr(task, "fixtures", None) or [])
        ]
        missing_mounted = [m for m in mounted_fixtures if not m.exists()]
        if missing_mounted:
            for mounted in missing_mounted:
                fail(f"task {task.id}: fixture not found: {mounted}")
            continue
        if fixture and not fixture.exists():
            fail(f"task {task.id}: repo not found: {fixture}")
            continue
        if not os.environ.get("SKILLDIFF_MOCK_RUNNER"):
            check_skill = cfg.skill_a if cfg.is_skill_comparison else cfg.skill
            with tempfile.TemporaryDirectory(prefix="skilldiff-probe-") as tmp:
                for arm, installed in (("control", cfg.is_skill_comparison), ("treatment", True)):
                    source = (cfg.skill_b if arm == "treatment" and cfg.is_skill_comparison
                              else check_skill)
                    probe_ws = Workspace(
                        Path(tmp) / arm / "workspace",
                        installed,
                        source,
                        fixture,
                        cfg.harness,
                        source_commit=(comparison or {}).get(f"{arm}_commit"),
                        pr_touched_skills=(
                            cast(list[str] | None, (comparison or {}).get("touched_skills"))
                            if (comparison and (comparison.get("mode") or "agent") == "agent")
                            else None
                        ),
                    )
                    try:
                        probe_ws.setup()
                        expected = ([read_skill_name(path) or path.name
                                     for path in probe_ws.skill_dirs] if installed else [])
                        runner.preflight(probe_ws.root, cfg, expected,
                                         runtime_probe=task.runtime_probe)
                        ok(f"task {task.id}: {arm} workspace discovery/runtime preflight passed")
                        if (
                            arm == "treatment"
                            and cfg.harness == "claude"
                            and cfg.isolation == "local"
                            and not (cfg.pr and getattr(cfg.pr, "mode", "agent") == "correctness")
                            and task.runtime_probe
                            and not getattr(args, "no_probe", False)
                            and not os.environ.get("SKILLDIFF_MOCK_RUNNER")
                        ):
                            command_probe = runner.probe_claude_commands(
                                cfg.models[0],
                                cfg.claude,
                                probe_ws.root,
                                [task.runtime_probe],
                                pricing=cfg.pricing,
                            )
                            if not command_probe.ok:
                                details = (
                                    command_probe.denials
                                    or command_probe.missing
                                    or command_probe.failed
                                    or [command_probe.detail]
                                )
                                fail(
                                    f"task {task.id}: runtime_probe refused or did not run "
                                    f"under Claude's sandbox ({'; '.join(details[:3])})"
                                )
                            else:
                                ok(
                                    f"task {task.id}: runtime_probe runs under "
                                    "Claude's sandbox"
                                )
                    except Exception as exc:
                        fail(f"task {task.id}: {arm} preflight: {exc}")
        # Isolation: escaping symlinks and .git history must never enter workspaces.
        if fixture and fixture.is_dir() and not cfg.pr:
            try:

                escaping = []
                for p in fixture.rglob("*"):
                    try:
                        if p.is_symlink():
                            import os as _os

                            target = (p.parent / _os.readlink(p)).resolve()
                            try:
                                target.relative_to(fixture.resolve())
                            except ValueError:
                                escaping.append(str(p.relative_to(fixture)))
                    except OSError:
                        continue
                if escaping:
                    fail(
                        f"task {task.id}: fixture has escaping symlinks: "
                        f"{', '.join(escaping[:3])}"
                    )
                    continue
                if (fixture / ".git").is_dir():
                    warn(
                        f"task {task.id}: fixture contains .git history; "
                        "workspaces strip it so removed skills cannot be resurrected"
                    )
            except Exception:
                pass
        if fixture and fixture.is_dir():
            has_py = any(p.suffix == ".py" for p in fixture.rglob("*") if p.is_file())
            g_ignore = getattr(task, "grader_ignore", None) or []
            if has_py and not any("pyc" in pat or "pycache" in pat for pat in g_ignore):
                warn(
                    f"task {task.id}: fixture contains Python files but grader_ignore "
                    "does not cover bytecode (*.pyc, __pycache__/*)"
                )
            allowed_domains = getattr(cfg.claude, "allowed_domains", None) or []
            if has_py and not allowed_domains:
                warn(
                    f"task {task.id}: fixture contains Python files but allowed_domains "
                    "is empty; add 'pypi.org' and 'files.pythonhosted.org' if agents "
                    "need pip install"
                )
        for message in scope_pattern_warnings(
            task.id,
            getattr(task, "forbidden_paths", None) or [],
            getattr(task, "grader_ignore", None) or [],
        ):
            warn(message)
        if task.grader and task.grader.command:
            for part in task.grader.command.split():
                if part.endswith(".py") or part.endswith(".sh"):
                    p = Path(part.replace("$SKILLDIFF_TASK_DIR", str(task_dir)))
                    if not p.is_absolute():
                        p = (task_dir / p).resolve()
                    if p.is_file():
                        try:
                            content = p.read_text(encoding="utf-8")
                            if "TODO: replace with real checks" in content:
                                warn(
                                    f"task {task.id}: grader {p.name} is a placeholder template "
                                    "that fails until replaced with real checks"
                                )
                        except Exception:
                            pass
        if not (task.grader and (task.grader.command or task.grader.type in {"llm", "rubric"})):
            warn(f"task {task.id}: no grader, so every run scores N/A (only cost/time compared)")
            continue
        if getattr(args, "no_grade", False):
            ok(f"task {task.id}: grader configured (not run)")
            continue
        # Choose the skill root for workspace construction (A/B uses skill_a for checks).
        check_skill = cfg.skill_a if cfg.is_skill_comparison else cfg.skill
        with tempfile.TemporaryDirectory(prefix="skilldiff-check-") as tmp:
            ws = Workspace(
                Path(tmp) / "workspace",
                False,
                check_skill,
                fixture,
                cfg.harness,
                source_commit=(comparison or {}).get("control_commit"),
                strip_skill_dirs=(
                    [cfg.skill_b] if cfg.is_skill_comparison and cfg.skill_b else None
                ),
                pr_touched_skills=(
                    cast(list[str] | None, (comparison or {}).get("touched_skills"))
                    if (comparison and (comparison.get("mode") or "agent") == "agent")
                    else None
                ),
                mounted_fixtures=mounted_fixtures,
            )
            try:
                ws.setup()
            except Exception as exc:
                fail(f"task {task.id}: could not build workspace: {exc}")
                continue
            if ws.isolation_issues:
                warn(f"task {task.id}: workspace isolation notes: {'; '.join(ws.isolation_issues)}")
            grader = Grader(
                task.grader,
                cfg.skill_names,
                cfg.models[0],
                task_dir=task_dir,
                allowed_paths=getattr(task, "allowed_paths", None),
                forbidden_paths=getattr(task, "forbidden_paths", None),
                grader_ignore=getattr(task, "grader_ignore", None),
                task_prompt=task.prompt,
                isolation="local" if cfg.isolation == "macos" else cfg.isolation,
                container_image=image_id,
                inputs_root=config_path.resolve().parent,
            )
            grade = grader.grade_workspace(ws.root)
            # Validation fixtures: untouched must fail, known-good must pass,
            # deliberately broken must fail. A grader that fails everything is broken.
            validation = getattr(task, "validation", {}) or {}
            if validation:
                from skilldiff.grader import validate_grader_against_directories as _validate

                good = validation.get("good")
                goods = [good] if isinstance(good, str) else list(good or [])
                broken = validation.get("broken") or validation.get("bad") or []
                if isinstance(broken, str):
                    broken = [broken]
                reference = validation.get("reference")
                references = [reference] if isinstance(reference, str) else list(reference or [])
                deprecated = validation.get("deprecated")
                deprecated_list = (
                    [deprecated] if isinstance(deprecated, str) else list(deprecated or [])
                )
                good_dirs = [(task_dir / g).resolve() for g in goods]
                broken_dirs = [(task_dir / b).resolve() for b in (broken or [])]
                ref_dirs = [(task_dir / r).resolve() for r in references]
                missing = [str(d) for d in good_dirs + broken_dirs + ref_dirs if not d.exists()]
                if missing:
                    fail(f"task {task.id}: validation paths not found: {', '.join(missing)}")
                    continue
                if len(good_dirs) == 1 and not ref_dirs:
                    warn(
                        f"task {task.id}: only one validation.good solution; a grader that "
                        "accepts your solution but rejects an equivalent one (another API "
                        "call, an alias, a different structure) would show up as a false "
                        "skill improvement. List a second, differently-shaped valid "
                        "solution under `good:`"
                    )
                report = _validate(
                    grader, ws.root, good_dirs or None, broken_dirs,
                    reference_dirs=ref_dirs or None,
                    deprecated_patterns=deprecated_list or None,
                )
                log = (config_path.resolve().parent / "runs" / "preflight"
                       / safe_path_component(task.id))
                log.mkdir(parents=True, exist_ok=True)
                atomic_json(log / "grader.json", report)
                verdict = report.get("verdict")
                if verdict != "ok":
                    fail(
                        f"task {task.id}: grader validation {verdict} "
                        f"(log: {log / 'grader.json'}): "
                        f"{'; '.join(cast(list[str], report.get('checks', [])))}"
                    )
                    continue
                ok(
                    f"task {task.id}: grader validation ok (untouched/good/broken; "
                    "proves self-consistency with provided solutions, not real-world API currency)"
                )
            # Grader isolation note: outside the fixture is not isolation by itself.
            try:
                from skilldiff.grader import grader_isolation_note as _gin

                note = _gin(task_dir, fixture)
                if note and "inside the fixture" in note:
                    fail(f"task {task.id}: {note}")
                    continue
            except Exception:
                pass
        feedback = grade.feedback or ""
        errored = any(s in feedback for s in ("Traceback", "No such file", "not found"))
        if grade.grade_status in ("timeout", "error") or (errored and (grade.score or 0) == 0):
            last = feedback.strip().splitlines()[-1][:200] if feedback.strip() else ""
            fail(
                f"task {task.id}: grader errored on the untouched fixture: "
                f"{last or grade.grade_status}"
            )
        elif grade.score is not None and grade.score >= 1.0:
            if getattr(args, "fail_on_ceiling", False):
                fail(
                    f"task {task.id}: the untouched fixture already scores 100%, so this "
                    "task cannot show a difference"
                )
            else:
                warn(
                    f"task {task.id}: the untouched fixture already scores 100%, so this "
                    "task cannot show a difference"
                )
        elif grade.score is None:
            warn(f"task {task.id}: grader returned {grade.grade_status}; scores will be N/A")
        else:
            ok(f"task {task.id}: grader runs; untouched fixture scores {round(grade.score * 100)}%")
            if not validation:
                warn(
                    f"task {task.id}: no validation.good/broken fixtures; "
                    "add a known-good solution and deliberately broken solutions so "
                    "`check` can tell a strict grader from a broken one"
                )

    arms = 3 if (cfg.is_skill_comparison and cfg.include_baseline) else 2
    sessions = len(cfg.models) * len(tasks) * cfg.runs * arms
    if cfg.pr and (comparison or {}).get("mode") == "correctness":
        line = f"{len(tasks)} untouched revision pair(s) to grade (no agent sessions)"
    else:
        line = f"{sessions} agent sessions per full run"
        if cfg.harness == "claude" and cfg.claude.max_budget_usd:
            line += f" (at most ${sessions * cfg.claude.max_budget_usd:.2f} API-equivalent)"
    ok(line)
    if cfg.runs < 3:
        warn(f"runs: {cfg.runs} is fine for a smoke test, but use 5+ for conclusions")

    print()
    print(f"{ok_count} ok, {warnings} warning(s), {problems} problem(s)")
    if problems or (getattr(args, "ci", False) and warnings):
        return 1
    return 0


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

    try:
        for key in ("runs", "parallel"):
            value = getattr(args, key, None)
            if value is not None:
                validate_integer(value, key, positive=True)
                setattr(exp_config, key, value)
                if key == "parallel":
                    exp_config._cli_parallel_explicit = True
        if getattr(args, "arm_parallel", False):
            exp_config.arm_parallel = True
            exp_config._arm_parallel_explicit = True
        if getattr(args, "reflink", False):
            exp_config.reflink = True
        if getattr(args, "model", None) is not None:
            validate_string_list(args.model, "model", nonempty=True)
            exp_config.models = list(args.model)
        if exp_config.harness == "claude":
            for model in exp_config.models:
                if is_bare_family_alias(str(model)):
                    raise ValueError(
                        f"model '{model}' is a bare family alias. Pin the full model ID "
                        f"'{MODEL_ALIASES[str(model).strip().lower()]}' instead."
                    )
        resolved_pricing, unpriced = merge_pricing(
            exp_config.pricing, exp_config.models, exp_config.harness
        )
        exp_config.pricing = resolved_pricing
        exp_config.pricing_unpriced = unpriced
        if exp_config.cost_basis == "api-equivalent" and unpriced:
            raise ValueError(
                "cost_basis: api-equivalent has no rates for "
                + ", ".join(f"'{model}'" for model in unpriced)
                + ". Pin a full Claude model ID with built-in rates or add pricing.rates."
            )
        if getattr(args, "seed", None) is not None:
            validate_integer(args.seed, "seed")
            exp_config.seed = args.seed
        if getattr(args, "max_turns", None) is not None:
            validate_integer(args.max_turns, "max_turns", positive=True)
            exp_config.max_turns = args.max_turns
            exp_config.claude.max_turns = args.max_turns
        if getattr(args, "max_tool_budget", None) is not None:
            validate_integer(args.max_tool_budget, "max_tool_budget", positive=True)
            exp_config.max_tool_budget = args.max_tool_budget
        if getattr(args, "terminate_on_ceiling", False):
            exp_config.terminate_on_ceiling = True
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    task_filter = getattr(args, "task", None)
    if task_filter:
        tasks = [t for t in tasks if t.id in task_filter]
        if not tasks:
            print(f"No tasks match {task_filter}", file=sys.stderr)
            return 1

    split_filter = getattr(args, "split", None)
    if split_filter:
        tasks = [t for t in tasks if t.split == split_filter]
        if not tasks:
            print(f"No tasks match split {split_filter!r} and the selected task IDs",
                  file=sys.stderr)
            return 1

    sandboxed = (
        [] if os.environ.get("SKILLDIFF_MOCK_RUNNER")
        or (exp_config.pr and getattr(exp_config.pr, "mode", "agent") == "correctness")
        else host_sandbox_problems(exp_config.harness, exp_config.isolation)
    )
    if sandboxed:
        print("Cannot start agent sessions from this process: " + "; ".join(sandboxed)
              + f". Fix: {host_sandbox_fix()}.", file=sys.stderr)
        return 1

    quiet = bool(getattr(args, "quiet", False))
    resume_arg = getattr(args, "resume", False)
    resume_path = getattr(args, "resume_from", None)
    retry_failed = bool(getattr(args, "retry_failed", False))
    resume: bool | Path = False
    if resume_path:
        resume = Path(resume_path)
    elif resume_arg or retry_failed:
        resume = True
    runner = ExperimentRunner(
        exp_config,
        tasks,
        progress=None if quiet else (lambda msg: print(msg, flush=True)),
        retry_failed=retry_failed,
    )
    arms = 3 if (exp_config.is_skill_comparison and exp_config.include_baseline) else 2
    is_correctness = bool(exp_config.pr) and (
        getattr(exp_config.pr, "mode", "agent") == "correctness"
    )
    if is_correctness:
        sessions_txt = f"{len(tasks)} untouched pair(s), no agent sessions (correctness mode)"
    else:
        sessions = len(exp_config.models) * len(tasks) * exp_config.runs * arms
        sessions_txt = (
            f"{len(exp_config.models)} model(s) × {len(tasks)} task(s) × "
            f"{exp_config.runs} run(s) × {arms} arms = {sessions} sessions"
        )
    print(f"Running '{exp_config.name}' with {exp_config.harness}: {sessions_txt}", flush=True)
    if resume:
        print("Resume enabled: completed pairs reuse only when input hashes match.", flush=True)
    if (
        not resume
        and not is_correctness
        and exp_config.isolation == "local"
        and not getattr(args, "no_probe", False)
        and not os.environ.get("SKILLDIFF_MOCK_RUNNER")
    ):
        if exp_config.harness == "claude":
            try:
                for model in exp_config.models:
                    probe = runner.agent_runner.probe_claude_auth(
                        model, exp_config.claude, pricing=exp_config.pricing
                    )
                    if not probe.ok:
                        if probe.budget_failure:
                            raise RuntimeError(
                                f"Claude preflight hit its budget cap for {model} "
                                f"({probe.detail}). Raise claude.max_budget_usd."
                            )
                        raise RuntimeError(
                            f"Claude preflight failed for {model}: {probe.detail}"
                        )
                    observed = max(
                        one_call_cost_estimate(exp_config.pricing, model) or 0.0,
                        float(probe.cost or 0.0),
                    )
                    if (
                        observed
                        and exp_config.claude.max_budget_usd is not None
                        and exp_config.claude.max_budget_usd < observed * 4
                    ):
                        raise RuntimeError(
                            f"claude.max_budget_usd is ${exp_config.claude.max_budget_usd:.2f}, "
                            f"below four times the observed ${observed:.2f} cost of one call "
                            f"for {model}. Raise it to at least ${observed * 4:.2f}."
                        )
                    if probe.mismatch:
                        raise RuntimeError(
                            f"Claude served '{model}' as {', '.join(probe.served_models)}. "
                            "Pin the full model ID the harness actually serves."
                        )
                    if probe.unrecognized_models and exp_config.cost_basis == "harness":
                        raise RuntimeError(
                            "Claude does not recognize "
                            f"{', '.join(probe.unrecognized_models)}; harness cost is "
                            "unreliable. Use cost_basis: api-equivalent."
                        )
                    canary = runner.agent_runner.probe_claude_sandbox_canary(
                        model, exp_config.claude, pricing=exp_config.pricing
                    )
                    if not canary.ok:
                        details = canary.denials or canary.missing or canary.failed
                        raise RuntimeError(
                            "Claude sandbox preflight failed: "
                            + ("; ".join(details[:3]) or canary.detail)
                        )
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                print(f"Preflight error: {exc}", file=sys.stderr)
                return 1
            print("Claude model and sandbox preflight passed.", flush=True)
        elif exp_config.harness == "codex":
            try:
                for model in exp_config.models:
                    probe = runner.agent_runner.probe_codex_auth(model, exp_config.codex)
                    if not probe.ok:
                        raise RuntimeError(f"Codex preflight failed for {model}: {probe.detail}")
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                print(f"Preflight error: {exc}", file=sys.stderr)
                return 1
            print("Codex preflight passed.", flush=True)
        elif exp_config.harness == "opencode":
            try:
                for model in exp_config.models:
                    probe = runner.agent_runner.probe_opencode_auth(model, exp_config.opencode)
                    if not probe.ok:
                        raise RuntimeError(f"OpenCode preflight failed for {model}: {probe.detail}")
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                print(f"Preflight error: {exc}", file=sys.stderr)
                return 1
            print("OpenCode preflight passed.", flush=True)
        elif exp_config.harness == "antigravity":
            try:
                for model in exp_config.models:
                    probe = runner.agent_runner.probe_antigravity_auth(
                        model, exp_config.antigravity
                    )
                    if not probe.ok:
                        raise RuntimeError(
                            f"Antigravity preflight failed for {model}: {probe.detail}"
                        )
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                print(f"Preflight error: {exc}", file=sys.stderr)
                return 1
            print("Antigravity preflight passed.", flush=True)
    try:
        run_kwargs = {"retry_failed": retry_failed} if retry_failed else {}
        results = runner.run(resume=resume, **run_kwargs)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Experiment error: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(results, indent=2))
    else:
        print("\n" + _format_results(results))
        _print_report_paths(results)
    if results.get("valid") is False:
        if not getattr(args, "json", False):
            print("\nINVALID: no clean baseline (control contamination).", file=sys.stderr)
        return 2
    if results.get("interrupted"):
        return 130
    if getattr(args, "ci", False):
        if results.get("terminated_on_ceiling"):
            return 3
        if results.get("adoption_failed"):
            return 4
    return 0


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
        if getattr(args, "ci", False):
            if results.get("valid") is False:
                return 2
            if results.get("terminated_on_ceiling"):
                return 3
            if results.get("adoption_failed"):
                return 4
        return 0
    if getattr(args, "markdown", False):
        md = run_dir / "report.md"
        if not md.exists():
            try:
                with run_lock(run_dir):
                    create_reports(read_json(results_file), run_dir)
            except (ValueError, OSError) as exc:
                print(f"Report error: {exc}", file=sys.stderr)
                return 1
        print(md.read_text(encoding="utf-8"))
        return 0

    print(_format_results(results))
    _print_report_paths(results)
    if getattr(args, "ci", False):
        if results.get("valid") is False:
            return 2
        if results.get("terminated_on_ceiling"):
            return 3
        if results.get("adoption_failed"):
            return 4
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    run_dir = _resolve_run_dir(args)
    if not run_dir or not (run_dir / "results.json").exists():
        print("No experiment run with results.json found.", file=sys.stderr)
        return 1
    try:
        with run_lock(run_dir):
            results = read_json(run_dir / "results.json")
            paths = create_reports(results, run_dir)
            results["report"] = {k: str(v) for k, v in paths.items()}
            atomic_json(run_dir / "results.json", results)
    except (ValueError, OSError) as exc:
        print(f"Report error: {exc}", file=sys.stderr)
        return 1
    _print_report_paths(results)
    return 0


def _format_score(score: object, status: object) -> str:
    if score is None:
        return f"N/A ({status})"
    return f"{round(float(cast(Any, score)) * 100)}%"


def cmd_regrade(args: argparse.Namespace) -> int:
    run_dir = _resolve_run_dir(args)
    if not run_dir or not (run_dir / "results.json").exists():
        print("No experiment run with results.json found.", file=sys.stderr)
        return 1
    run_dir = run_dir.resolve()
    config_path = Path(args.config) if args.config else Path("skilldiff.yaml")
    if not args.config and not config_path.exists():
        config_path = run_dir.parent.parent / "skilldiff.yaml"
    if not config_path.exists():
        print(
            f"Error: configuration file '{config_path}' not found. Pass --config.",
            file=sys.stderr,
        )
        return 1
    try:
        exp_config, tasks = load_experiment(config_path)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    runner = ExperimentRunner(exp_config, tasks, progress=lambda msg: print(msg, flush=True))
    try:
        with run_lock(run_dir):
            summary = runner.regrade(
                run_dir,
                task_ids=set(args.task) if args.task else None,
                dry_run=args.dry_run,
            )
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Regrade error: {exc}", file=sys.stderr)
        return 1

    verb = "would change" if args.dry_run else "changed"
    skipped_count = len(summary.get("skipped", []))
    skipped_part = f"; skipped {skipped_count} (binary diff)" if skipped_count else ""
    print(
        f"Regraded {summary['runs_regraded']} run(s){skipped_part} for task(s) "
        f"{', '.join(summary['tasks'])}; {verb} {summary['runs_changed']}."
    )
    for change in summary["changes"]:
        print(
            f"  {change['model']} · {change['task_id']} · rep {change['repetition']} · "
            f"{change['arm']}: "
            f"{_format_score(change['previous_score'], change['previous_grade_status'])} -> "
            f"{_format_score(change['score'], change['grade_status'])}"
        )
    for item in summary["skipped"]:
        print(f"  skipped {item}", file=sys.stderr)
    if args.dry_run:
        print("Dry run: nothing was written.")
        return 0
    results = summary["results"]
    print("\n" + _format_results(results))
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
    strict = bool(getattr(args, "strict", False)) or bool(getattr(args, "ci", False))
    try:
        comp = compare_results(a, b, strict=strict)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(comp, indent=2))
    else:
        print(format_comparison(comp))
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    from skilldiff.linter import lint_prompt_coverage, lint_skill

    target = Path(args.skill_dir or ".").resolve()
    res = lint_skill(target)
    coverage = None
    prompts_path = getattr(args, "prompts", None)
    if prompts_path:
        coverage = lint_prompt_coverage(target, Path(prompts_path))
        res.warnings.extend(coverage.get("warnings") or [])
        res.stats["prompt_coverage"] = coverage.get("stats") or {}
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "valid": res.valid,
                    "errors": res.errors,
                    "warnings": res.warnings,
                    "stats": res.stats,
                    "prompt_coverage": coverage.get("tasks") if coverage else None,
                },
                indent=2,
            )
        )
        return 0 if res.valid else 1

    print(f"Linting skill at {target}...")
    stats = res.stats
    if stats:
        print(
            f"  {stats.get('words', 0)} words · "
            f"~{stats.get('estimated_tokens', 0)} tokens · "
            f"{stats.get('characters', 0)} chars"
        )
    prompt_stats = stats.get("prompt_coverage") or {}
    if prompt_stats:
        average = prompt_stats.get("average_coverage")
        average_txt = f"{average:.0%}" if average is not None else "N/A"
        leaks = prompt_stats.get("leaked", 0)
        leaks_txt = f", {leaks} solution leak(s)" if leaks else ""
        print(
            f"  prompt trigger coverage: {prompt_stats.get('tasks', 0)} task prompt(s), "
            f"{prompt_stats.get('uncovered', 0)} possible gap(s){leaks_txt}, average {average_txt}"
        )

    if res.errors:
        print("\nErrors:")
        for err in res.errors:
            print(f"  ❌ {err}")

    if res.warnings:
        print("\nWarnings:")
        for w in res.warnings:
            print(f"  ⚠️  {w}")

    if res.valid and not res.warnings:
        print("\n✅ Skill passes all lint checks.")
    elif res.valid:
        print("\n✅ Skill valid (with warnings).")
    else:
        print("\n❌ Skill lint failed.")

    return 0 if res.valid else 1


def cmd_diagnose(args: argparse.Namespace) -> int:
    from skilldiff.diagnose import diagnose_run

    run_dir = _resolve_run_dir(args)
    if not run_dir:
        print("No experiment run found to diagnose.", file=sys.stderr)
        return 1

    try:
        report = diagnose_run(run_dir)
    except Exception as exc:
        print(f"Diagnosis failed: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
        if getattr(args, "ci", False) and (
            report.get("adoption_failed")
            or report.get("regressions")
            or report.get("agent_failures")
            or report.get("grader_failures")
        ):
            return 1
        return 0

    print(f"Diagnosis for {report.get('run_dir')} ({report.get('total_pairs')} pairs):")
    print(f"  Usable score pairs: {report.get('usable_score_pairs')}/{report.get('total_pairs')}")
    print(f"  Agent failures: {len(report.get('agent_failures', []))}")
    print(f"  Grader failures: {len(report.get('grader_failures', []))}")
    print(f"  Permission denials: {len(report.get('permission_denied', []))} session(s)")
    retries = report.get("rate_limit_retries") or {}
    print(f"  Rate-limit retries: {retries.get('count', 0)} event(s)")
    under = report.get("under_triggered", [])
    over = report.get("over_triggered", [])
    regs = report.get("regressions", [])
    blast = report.get("blast_violations", [])
    bloat = report.get("token_bloat_tasks", [])

    print(f"  Under-triggering: {len(under)} run(s)")
    print(f"  Over-triggering:  {len(over)} run(s)")
    print(f"  Regressions:      {len(regs)} run(s)")
    print(f"  Blast violations: {len(blast)} run(s)")
    print(f"  Token bloat:      {len(bloat)} run(s)")
    ceil = report.get("ceiling_tasks", [])
    if ceil:
        print(f"  Ceiling effect:   {len(ceil)} run(s)")

    usage = report.get("skill_usage") or {}
    if usage:
        basis = report.get("adoption_basis") or "all"
        print("\nSkill adoption:")
        for data in usage.values():
            known = int(data.get("known") or 0)
            hits = int(data.get("hits") or 0)
            rate = f"{data['hit_rate'] * 100:.0f}%" if data.get("hit_rate") is not None else "N/A"
            loads = data.get("loads")
            loads_txt = f", {loads} load event(s)" if loads is not None else ""
            print(
                f"  {data.get('label', 'Skill')}: {hits}/{known} runs ({rate}); "
                f"{data.get('unknown', 0)} unknown{loads_txt}"
            )
        print(
            f"  Decision basis: {basis} runs; "
            f"adoption_failed: {report.get('adoption_failed')}"
        )

    print("\nActionable Recommendations:")
    for rec in report.get("recommendations", []):
        print(f"  • {rec}")

    actions = report.get("next_actions") or []
    if actions:
        print("\nNext actions:")
        for action in actions:
            print(f"  → {action.get('advice')}")

    if getattr(args, "ci", False) and (
        report.get("adoption_failed")
        or report.get("regressions")
        or report.get("agent_failures")
        or report.get("grader_failures")
    ):
        return 1
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from skilldiff.scout import export_run_to_scout

    run_dir = _resolve_run_dir(args)
    if not run_dir:
        print("No experiment run found to export.", file=sys.stderr)
        return 1

    fmt = getattr(args, "format", "scout") or "scout"
    out = Path(args.output_dir) if getattr(args, "output_dir", None) else None

    try:
        summary = export_run_to_scout(run_dir, output_dir=out, format=fmt)
    except Exception as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1

    count = summary.get("count", 0)
    target = summary.get("output_dir", "")
    print(f"Exported {count} transcript(s) for Inspect Scout ({fmt}) to {target}")
    return 0


def _print_report_paths(results: dict[str, Any]) -> None:
    report = results.get("report", {}) or {}
    if report.get("html"):
        print(f"\nReport: {report['html']}")
    if report.get("md"):
        print(f"Markdown (for PRs): {report['md']}")
    elif report.get("qmd"):
        print(f"\nQuarto report: {report['qmd']}")


def _format_results(results: dict[str, Any]) -> str:
    name = results.get("name", "experiment")
    by_model = results.get("by_model", {})
    models = results.get("models", [])
    tasks_count = results.get("tasks_count", 0)
    runs_per_arm = results.get("runs_per_arm", 0)
    thresholds = results.get("thresholds") or (results.get("settings") or {}).get("thresholds")
    preset = results.get("preset") or (results.get("settings") or {}).get("preset")
    arm_labels = results.get("arm_labels") or {}
    from skilldiff.reporter import _apply_codex_cache_write_fallback

    raw_runs = _apply_codex_cache_write_fallback(results.get("runs") or {}, results)
    failure_policy = results.get("failure_policy") or (results.get("settings") or {}).get(
        "failure_policy") or {}
    agent_failure = failure_policy.get("agent_failure", "exclude")
    control_runs = [analysis_run(r, agent_failure) for r in raw_runs.get("control") or []]
    treatment_runs = [analysis_run(r, agent_failure) for r in raw_runs.get("treatment") or []]

    def comparable_source(model: str | None, fallback: dict[str, Any]) -> dict[str, Any]:
        if not (control_runs or treatment_runs):
            return fallback
        control = [r for r in control_runs if model is None or r.get("model") == model]
        treatment = [r for r in treatment_runs if model is None or r.get("model") == model]
        control_metrics, treatment_metrics = _paired_comparison_metrics(control, treatment)
        return {
            "control": control_metrics,
            "skill": treatment_metrics,
            "paired": paired_comparison(control, treatment),
        }

    sections: list[str] = []
    comparison = results.get("comparison")
    skill_comparison = results.get("skill_comparison")
    if preset:
        sections.append(f"Preset: {preset}")
    if skill_comparison:
        a_label = arm_labels.get("control", "Skill A")
        b_label = arm_labels.get("treatment", "Skill B")
        sections.append(
            f"{a_label}: {skill_comparison.get('skill_a')}\n"
            f"{b_label}: {skill_comparison.get('skill_b')}"
            + (" (plus no-skill baseline)" if skill_comparison.get("include_baseline") else "")
        )
        if skill_comparison.get("source_bytes_a") or skill_comparison.get("source_bytes_b"):
            try:
                ba = int(skill_comparison.get("source_bytes_a") or 0)
                bb = int(skill_comparison.get("source_bytes_b") or 0)
                pct = (
                    (bb - ba) / ba * 100
                    if ba
                    else (-float(skill_comparison.get("source_reduction_pct") or 0))
                )
                sections.append(f"Source size: {ba} → {bb} bytes ({pct:+.1f}%)")
            except Exception:
                pass
    elif comparison:
        pair = comparison.get("pair", "merge-base")
        mode = comparison.get("mode", "agent")
        sections.append(
            f"Control (without PR): {comparison['control_commit']} ({pair})\n"
            f"Treatment (with PR): {comparison['treatment_commit']} [{mode}]"
        )
    ctrl_label = (
        arm_labels.get("control")
        or ("Skill A" if skill_comparison or preset in {"revision", "compression"} else "Control")
    )
    treat_label = (
        arm_labels.get("treatment")
        or ("Skill B" if skill_comparison else ("Treatment" if comparison else "Skill"))
    )
    decision = build_decision_context(
        results, control_runs, treatment_runs, treatment_label=treat_label
    )
    if len(models) <= 1:
        model_name = models[0] if models else None
        model_data = by_model.get(model_name) if model_name else None
        source = comparable_source(model_name, model_data or results.get("overall", {}))
        sections.append(
            render_report_table(
                experiment_name=name,
                control_metrics=source.get("control", {}),
                skill_metrics=source.get("skill", {}),
                models_count=len(models),
                tasks_count=tasks_count,
                runs_per_arm=runs_per_arm,
                paired=source.get("paired"),
                treatment_label=treat_label,
                control_label=ctrl_label,
                thresholds=thresholds,
                preset=preset,
                decision=decision,
            )
        )
    else:
        for model_name in models:
            model_data = comparable_source(model_name, by_model.get(model_name) or {})
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
                    treatment_label=treat_label,
                    control_label=ctrl_label,
                    thresholds=thresholds,
                    preset=preset,
                    include_recommendation=False,
                )
            )

        if decision["basis_note"]:
            sections.append(decision["basis_note"])
        _, rec_label, rec_reason = decision["recommendation"]
        sections.append(f"Recommendation: {rec_label} — {rec_reason}")
        from skilldiff.reporting.verdict import format_bottom_line
        sections.append(format_bottom_line(rec_label, rec_reason))

    baseline_comps = results.get("baseline_comparisons") or {}
    for key, title in (
        ("baseline_vs_a", "Baseline vs A"),
        ("baseline_vs_b", "Baseline vs B"),
    ):
        comp = baseline_comps.get(key)
        if comp and comp.get("pairs"):
            pm = ((comp.get("paired") or {}).get("score") or {}).get("mean_diff")
            txt = f"{round(float(pm) * 100):+d} pp" if pm is not None else "N/A"
            sections.append(f"{title}: {txt} ({comp.get('pairs')} pairs)")

    if results.get("valid") is False:
        sections.append("INVALID: no clean baseline (control contamination).")
    warnings = results.get("warnings") or []
    if warnings:
        sections.append("Warnings:\n" + "\n".join(f"  - {w}" for w in warnings))
    from skilldiff.adoption import (
        adoption_actions,
        adoption_summary,
        decision_runs,
        merge_gate,
        no_effect_findings,
    )

    raw_runs = results.get("runs") or {}
    runs_by_arm = {
        "control": raw_runs.get("control") or [],
        "treatment": raw_runs.get("treatment") or [],
        "baseline": raw_runs.get("baseline") or [],
    }
    adoption = adoption_summary(results, runs_by_arm, basis="all")
    decision_arm_runs, adoption_basis = decision_runs(results, runs_by_arm)
    decision_adoption = adoption_summary(
        results, decision_arm_runs, basis=adoption_basis
    )
    if adoption.get("arms"):
        lines = []
        for data in adoption["arms"].values():
            known = int(data.get("known") or 0)
            hits = int(data.get("hits") or 0)
            rate = (
                f"{data['hit_rate'] * 100:.0f}%"
                if data.get("hit_rate") is not None
                else "N/A"
            )
            loads = data.get("loads")
            loads_txt = f", {loads} load event(s)" if loads is not None else ""
            lines.append(
                f"  - {data.get('label', 'Skill')}: {hits}/{known} runs ({rate}); "
                f"{data.get('unknown', 0)} unknown{loads_txt}"
            )
        if adoption_basis == "held-out":
            lines.append("  (the closing decision checks the held-out runs)")
        sections.append("Skill adoption:\n" + "\n".join(lines))
        actions = adoption_actions(
            results,
            merge_gate(adoption, decision_adoption),
            no_effect_findings(results, runs_by_arm),
        )
        if actions:
            sections.append(
                "Next actions:\n"
                + "\n".join(f"  - {action['advice']}" for action in actions)
            )
    from skilldiff.harness_diagnostics import (
        denial_sentence,
        summarize_harness_diagnostics,
    )

    harness_diag = summarize_harness_diagnostics(
        runs_by_arm, results.get("retries") or []
    )
    if harness_diag["has_issues"]:
        lines = []
        for arm, data in sorted(harness_diag["denials"].items()):
            lines.append("  - " + denial_sentence(arm, data))
        if harness_diag["model_mismatches"]:
            lines.append(
                "  - Model mismatch: "
                + "; ".join(
                    f"{item['configured']} served as {', '.join(item['served'])}"
                    for item in harness_diag["model_mismatches"][:5]
                )
            )
        if harness_diag["retries"].get("count"):
            lines.append(
                f"  - Rate-limit retries: {harness_diag['retries']['count']} event(s)"
            )
        if lines:
            sections.append("Harness diagnostics:\n" + "\n".join(lines))
    evaluation_table = render_evaluation_table(results)
    if evaluation_table:
        sections.append(evaluation_table)
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
    init_parser.add_argument("--skill-a", help="Skill revision A for A/B mode")
    init_parser.add_argument("--skill-b", help="Skill revision B for A/B mode")
    init_parser.add_argument(
        "--include-baseline",
        action="store_true",
        help="A/B mode: also run a no-skill baseline arm per pair",
    )
    init_parser.add_argument(
        "--preset",
        choices=["skill", "pr", "revision", "compression"],
        help="Named preset configuring arms and decision criteria",
    )
    init_parser.add_argument(
        "--template",
        choices=["claude-eval"],
        help="Scaffold an opt-in Claude app-evaluation template",
    )
    init_parser.add_argument(
        "--skill-invocation",
        choices=["implicit", "explicit"],
        help=(
            "How the agent is told to use the skill: implicit (the prompt never "
            "names it, so adoption measures discovery) or explicit (the prompt "
            "names it, so adoption measures obedience). Default: implicit."
        ),
    )
    init_parser.add_argument("--pr", type=int, help="Scaffold an evaluation of a GitHub PR number")
    init_parser.add_argument("--repo", help="Local repository containing the PR revisions")
    init_parser.add_argument("--base", help="PR target ref (default: origin/main)")
    init_parser.add_argument(
        "--pr-mode",
        choices=["agent", "correctness"],
        default="agent",
        help="PR workflow: agent effectiveness or PR correctness (default: agent)",
    )
    init_parser.add_argument(
        "--pr-pair",
        choices=["merge-base", "base-merge"],
        default="merge-base",
        help=(
            "PR revisions: merge-base vs head, or base tip vs synthetic merge "
            "(default: merge-base)"
        ),
    )
    init_parser.add_argument(
        "--pr-install",
        action="store_true",
        help="Install the respective PR checkout into the container environment for each arm",
    )
    init_parser.add_argument(
        "--container-install",
        nargs="+",
        help="Package specs or local directories to install into the container image",
    )
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
        "--no-probe",
        action="store_true",
        help="Skip the one-turn authenticated call that verifies the harness login",
    )
    check_parser.add_argument(
        "--no-grade", action="store_true", help="Skip running graders on the untouched fixtures"
    )
    check_parser.add_argument(
        "--fail-on-ceiling",
        action="store_true",
        help="Fail tasks whose untouched fixture already scores 100%%",
    )
    check_parser.add_argument(
        "--scan-home", action="store_true",
        help="Also scan HOME for possible skill copies (bounded, advisory; local runs only)",
    )
    check_parser.add_argument(
        "--ci", action="store_true", help="Exit non-zero when warnings are present"
    )
    check_parser.set_defaults(func=cmd_check)

    run_parser = subparsers.add_parser("run", help="Run both arms of the experiment")
    run_parser.add_argument(
        "--config", "-c", default="skilldiff.yaml", help="Path to experiment config file"
    )
    run_parser.add_argument("--runs", "-r", type=int, help="Override number of runs per arm")
    run_parser.add_argument("--parallel", "-j", type=int, help="Pairs to run concurrently")
    run_parser.add_argument(
        "--arm-parallel",
        action="store_true",
        help="Run control, treatment, and baseline arms of each pair concurrently",
    )
    run_parser.add_argument(
        "--reflink",
        action="store_true",
        help="Use copy-on-write (reflink) for fixture and snapshot copies if supported",
    )
    run_parser.add_argument(
        "--model", "-m", action="append", help="Only run this model (repeatable)"
    )
    run_parser.add_argument(
        "--task", "-t", action="append", help="Only run this task id (repeatable)"
    )
    run_parser.add_argument(
        "--split", choices=("dev", "held-out"),
        help="Only run this task split (use dev for smoke tests; combines with --task)",
    )
    run_parser.add_argument("--quiet", "-q", action="store_true", help="Hide per-run progress")
    run_parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume the latest run: reuse completed pairs only when input hashes match",
    )
    run_parser.add_argument(
        "--resume-from", help="Resume a specific run directory (strict hash check)"
    )
    run_parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="When resuming, retry pairs that encountered harness errors, timeouts, or failures",
    )
    run_parser.add_argument(
        "--no-probe",
        action="store_true",
        help="Skip harness auth, model, and sandbox probes before the first paid session",
    )
    run_parser.add_argument("--seed", type=int, help="Override the randomization seed")
    run_parser.add_argument("--max-turns", type=int, help="Override max turns per session")
    run_parser.add_argument(
        "--max-tool-budget", type=int, help="Fail sessions exceeding this tool-call count"
    )
    run_parser.add_argument(
        "--terminate-on-ceiling",
        action="store_true",
        help="Skip remaining repetitions when a task scores 100%% in control",
    )
    run_parser.add_argument("--json", action="store_true", help="Output results as JSON")
    run_parser.add_argument(
        "--ci",
        action="store_true",
        help="Exit non-zero on invalid baseline, ceiling termination, or adoption failure",
    )
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
    results_parser.add_argument(
        "--ci",
        action="store_true",
        help="Exit non-zero on invalid baseline, ceiling termination, or adoption failure",
    )
    results_parser.set_defaults(func=cmd_results)

    report_parser = subparsers.add_parser(
        "report", help="Regenerate the HTML/Markdown/Quarto reports for a run"
    )
    report_parser.add_argument("run_dir", nargs="?", help="Path to specific run directory")
    report_parser.add_argument("--config", "-c", help="Look for runs next to this config")
    report_parser.set_defaults(func=cmd_report)

    regrade_parser = subparsers.add_parser(
        "regrade",
        help="Re-run the current graders on a finished run (no agent sessions)",
    )
    regrade_parser.add_argument("run_dir", nargs="?", help="Path to specific run directory")
    regrade_parser.add_argument(
        "--config", "-c", help="Experiment config whose tasks and graders to use"
    )
    regrade_parser.add_argument(
        "--task", "-t", action="append", help="Only regrade this task id (repeatable)"
    )
    regrade_parser.add_argument(
        "--dry-run", action="store_true", help="Show which scores would change; write nothing"
    )
    regrade_parser.set_defaults(func=cmd_regrade)

    compare_parser = subparsers.add_parser(
        "compare", help="Compare two runs (skill revisions): score, adoption, efficiency, checks"
    )
    compare_parser.add_argument("run_a", help="First run dir or results.json")
    compare_parser.add_argument("run_b", help="Second run dir or results.json")
    compare_parser.add_argument("--json", action="store_true", help="Output comparison as JSON")
    compare_parser.add_argument(
        "--strict",
        action="store_true",
        help="Reject incompatible experiments (different models/tasks/hashes) instead of warning",
    )
    compare_parser.add_argument(
        "--ci", action="store_true", help="Refuse incompatible comparisons for CI"
    )
    compare_parser.set_defaults(func=cmd_compare)

    lint_parser = subparsers.add_parser(
        "lint", help="Static linter for SKILL.md: frontmatter, triggers, links, and length"
    )
    lint_parser.add_argument("skill_dir", nargs="?", default=".", help="Path to skill directory")
    lint_parser.add_argument(
        "--prompts",
        help="Task file, directory, or glob; report description coverage before a run",
    )
    lint_parser.add_argument("--json", action="store_true", help="Output lint report as JSON")
    lint_parser.set_defaults(func=cmd_lint)

    diagnose_parser = subparsers.add_parser(
        "diagnose", help="Diagnose failure modes, regressions, and adoption gaps in a run"
    )
    diagnose_parser.add_argument("run_dir", nargs="?", help="Path to run directory or results.json")
    diagnose_parser.add_argument("--config", "-c", help="Look for runs next to this config")
    diagnose_parser.add_argument("--json", action="store_true", help="Output diagnosis as JSON")
    diagnose_parser.add_argument(
        "--ci",
        action="store_true",
        help="Exit non-zero when adoption failures, regressions, or session/grader errors occur",
    )
    diagnose_parser.set_defaults(func=cmd_diagnose)

    export_parser = subparsers.add_parser(
        "export",
        help="Export experiment runs and transcripts to Inspect Scout or JSON/JSONL format",
    )
    export_parser.add_argument("run_dir", nargs="?", help="Path to run directory or results.json")
    export_parser.add_argument("--config", "-c", help="Look for runs next to this config")
    export_parser.add_argument(
        "--format",
        choices=["scout", "json", "jsonl"],
        default="scout",
        help="Export format (scout: individual JSONs + JSONL; json: single JSON; jsonl: JSONL)",
    )
    export_parser.add_argument(
        "--output-dir", "-o", help="Target output directory for exported files"
    )
    export_parser.set_defaults(func=cmd_export)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()

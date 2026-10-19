# Agent workflow

The user supplies the goal and target. You handle the commands, experiment files,
graders, and report. Do not ask the user to write YAML or choose internal flags.

## Resolve the request

Use an existing experiment when the user identifies it. Otherwise, resolve the
skill path from their message or attached context. Ask for the path if missing.
For a demo request, use the built-in changelog demo instead of asking for a skill.

Infer the harness from the current agent session when known. Reuse the user's
requested or configured model if the CLI can access it. Ask only when the harness
or an available model cannot be established. Preserve their authentication choice.

| Current agent | `--harness` | Skill installer `-a` |
|---|---|---|
| Claude Code | `claude` | `claude-code` |
| Codex | `codex` | `codex` |
| Antigravity CLI (AGY) | `antigravity` | `antigravity-cli` |
| OpenCode V2 | `opencode` | `opencode` |

## Setup requests

Install the command as described in [SKILL.md](../SKILL.md). If the user asks to install the
SkillDiff skill, install it for their agent only. For example, on Codex:

```bash
npx skills add karangattu/skilldiff -g -y --copy -a codex
```

Use the table's installer target for other agents. SkillDiff installs the target
skill into evaluation workspaces itself. A setup-only request does not authorize
paid evaluation sessions. Report required login or shell permissions without
changing the user's authentication or weakening isolation.

## First trials and demos

For an initial trial with no specified budget, use one model, one development task,
and one run per arm. In ordinary skill mode this is two agent sessions. A comparison
with an extra baseline uses three. Keep fuller task design for the evaluation that
follows. Small trials check setup; they cannot establish a shipping verdict.

For the built-in demo, omit `--skill` from `init`. Choose a new evaluation folder
outside the project, pass the selected `--harness`, and set the accessible model in
its configuration. The demo already contains its task, fixture, skill, and grader.
For a custom skill, write and validate the task and grader before execution.

Use explicit configuration paths for every command. For example:

```bash
skilldiff check -c /abs/path/to/skill-eval/skilldiff.yaml --no-probe
skilldiff run -c /abs/path/to/skill-eval/skilldiff.yaml --split dev --runs 1
```

`check --no-probe` validates setup without paid authentication calls. Stop on
failures and assess warnings before running. Follow [preflight safeguards](preflight.md)
for host restrictions and background execution. Honor any existing authorization
for larger runs; otherwise show the planned sessions and cost before asking.

## Read results as data

Use the existing JSON commands for facts instead of scraping terminal tables:

```bash
skilldiff lint /abs/path/to/skill --json
skilldiff results -c /abs/path/to/skill-eval/skilldiff.yaml --json
skilldiff diagnose -c /abs/path/to/skill-eval/skilldiff.yaml --json
```

Save the exact run directory returned by execution. Pass it to `results` and
`diagnose` when evaluating an earlier run or when other runs can finish concurrently.
Inspect `valid`, `warnings`, incomplete trials, and each arm's status, grading,
usage, and skill adoption. `diagnose --json` exposes per-arm `skill_usage` and
structured `next_actions`; carry the hit rates and action items into the summary.
Missing measurements remain unknown. Preserve failed attempts; do not retry them
silently or treat them as valid comparisons.

Read the saved report for SkillDiff's computed decision, or print it:

```bash
skilldiff results -c /abs/path/to/skill-eval/skilldiff.yaml --markdown
```

Explain the result, uncertainty, skill adoption, and any blocked or unverified
scope. Link the report. Follow [reporting](reporting.md) for the required results
table and closing decision. Open HTML only when it helps the user; JSON and
Markdown are sufficient for the agent's analysis.

# SkillDiff

![SkillDiff — paired agent terminals, with and without a skill, joined by a delta comparison symbol](assets/skill_diff_logo.png)

Tell your coding agent which skill to evaluate. SkillDiff runs matched tasks with
and without the skill and measures what changes. Your agent prepares the tests,
runs the evaluation, and explains whether the evidence supports keeping the skill.

SkillDiff is designed to be used through Claude Code, Codex, Antigravity CLI (AGY),
or OpenCode V2. You provide the goal and skill path. The installed SkillDiff skill
gives your agent the setup, evaluation, and reporting instructions.

## Set up once through your agent

Paste this into your coding agent:

```text
Set up SkillDiff for this agent from https://github.com/karangattu/skilldiff.
Read the repository's skills/skilldiff/SKILL.md and follow its setup instructions.
Install the command and SkillDiff skill for this agent only.
```

Your agent handles installation and uses your normal CLI login. If it needs a login
or shell permission, it tells you the exact step. You need Python 3.10+, Git, and a
signed-in agent CLI. See [manual setup](#manual-setup) if you prefer to install it yourself.

## Ask your agent

Once setup is complete, give your agent a short request. Replace the example paths
with your skill folders. The agent uses the current CLI and an available model,
and asks only when it cannot establish a necessary choice.

### Try the demo

No skill or test files of your own are needed:

```text
Use skilldiff to try its changelog demo and explain the result.
```

### Test your skill

Point to the folder that contains your SKILL.md:

```text
Use skilldiff to test ~/code/my-project/skills/my-skill. Start with a small trial and explain the result.
```

### Compare two skill versions

Point to the original and updated skill folders:

```text
Use skilldiff to compare ~/skills/v1/my-skill with ~/skills/v2/my-skill.
Start with a small trial and tell me whether there is enough evidence to choose a version.
```

The agent prepares the test files and automatic checks, then runs one small
comparison. You do not need to write YAML, choose flags, or run commands yourself.
Tests use your agent account or API credits. The agent asks before a larger
paid evaluation unless you already authorized it.

## What you get

Your agent explains the score, skill use, cost, time, and tokens. It links the
report and gives SkillDiff's decision: **SHIP**, **DO NOT SHIP**, or
**NEEDS MORE RUNS**, with the evidence and uncertainty. A first trial checks setup;
it does not establish a shipping decision. Missing measurements stay `N/A`.

Reports include JSON for agent analysis, Markdown for summaries, and HTML for
optional viewing. See [reports and outputs](docs/reference.md#reports-and-outputs)
for the full format.

## Agent instructions

Start with [skills/skilldiff/SKILL.md](skills/skilldiff/SKILL.md).
The [agent workflow](skills/skilldiff/references/agent-workflow.md) covers setup,
harness selection, small trials, and the existing JSON commands for reading results.
Use the saved report's decision when explaining the outcome.

## Agent support

SkillDiff checks the CLI version, required commands, and structured formats inside
the selected execution environment before paid work. Each treatment skill is
installed once in the native workspace directory. Follow-up `prompts:` share one
conversation, workspace, and deadline; arms and repetitions have separate sessions.

Empty output, malformed streams, explicit failures, and missing completion events
are harness errors even when the CLI exits zero. Partial telemetry is retained,
and these sessions do not enter paired decisions. Unknown usage and cost stay `N/A`.
Claude subscription authentication remains the default; SkillDiff does not use
`--bare`. See [the compatibility contract and verification limits](docs/compatibility.md).

OpenCode v2.0.23 uses session export fallback when the final completion event is omitted.
See [verified releases and limits](docs/compatibility.md#verification) before running.

## Manual setup

<details>
<summary>Installation commands and updates</summary>

You need Python 3.10+, Git, and a signed-in agent CLI (`claude`, `codex`, `opencode`, or `agy`).

**1. Install the `skilldiff` tool:**

```bash
uv tool install git+https://github.com/karangattu/skilldiff
```

> [!NOTE]
> A git install is pinned to the commit resolved at install time, so an existing
> `skilldiff` will not pick up later fixes on its own. Run `uv tool upgrade skilldiff`
> to refresh it.

> [!IMPORTANT]
> Install from GitHub as shown above. The `skilldiff` package on PyPI is a different project.

**2. Install the skill into your agent** (replace `claude-code` with `codex`, `opencode`, or `antigravity-cli`):

```bash
npx skills add karangattu/skilldiff -g -y --copy -a claude-code
```

To upgrade installed skills to the latest version:

```bash
npx skills update skilldiff -g
```

| Agent | `-a` value | Installs to |
|---|---|---|
| Claude Code | `claude-code` | `~/.claude/skills/skilldiff` |
| Codex | `codex` | `~/.agents/skills/skilldiff` |
| OpenCode | `opencode` | `~/.config/opencode/skills/skilldiff` |
| Antigravity CLI (AGY) | `antigravity-cli` | `~/.gemini/antigravity-cli/skills/skilldiff` |

Use `--copy` so AGY receives the skill in its native global directory.
The installer treats Antigravity CLI and the desktop agent as separate agents.
For AGY, reinstall with `npx skills add karangattu/skilldiff -g -y --copy -a antigravity-cli`
after an update if the native copy is missing.

Always pass `-a`. Without it, the [`skills` CLI](https://github.com/vercel-labs/skills)
may install into every agent it knows, which is about sixty folders. `-g` installs
for your user, so every project can use the skill. Without `npx`, copy the
[`skills/skilldiff`](skills/skilldiff) folder into the folder shown in the table.
Claude Code users can instead install the plugin, which adds a
`/skilldiff:skilldiff <path>` command:

```bash
# Inside Claude Code
/plugin marketplace add karangattu/skilldiff
/plugin install skilldiff@skilldiff
```

You install only the skilldiff skill. SkillDiff copies the skill *under test* into
each run's workspace itself.

</details>

<details>
<summary>If your agent cannot start SkillDiff</summary>

**One-time setup if your agent sandboxes its shell.** Your agent runs `skilldiff`,
and `skilldiff` starts its own agent sessions. Those sessions need network access
and write access to their login folder. Allow `skilldiff` once:

| Your agent | One-time setup |
|---|---|
| Claude Code | In `.claude/settings.json`: `{"permissions": {"allow": ["Bash(skilldiff *)"]}, "sandbox": {"excludedCommands": ["skilldiff *"]}}` |
| Codex | Add `prefix_rule(pattern=["skilldiff"], decision="allow")` to `~/.codex/rules/default.rules`, or approve the command when Codex asks |
| OpenCode | If shell access is restricted, add `{"permissions": [{"action": "shell", "resource": "skilldiff *", "effect": "allow"}]}` to `opencode.json` (V2) |
| Antigravity | Approve `skilldiff` when asked, or add `command(skilldiff)` and `unsandboxed(skilldiff)` to `permissions.allow` in `~/.gemini/antigravity-cli/settings.json` |

If you skip this setup, `skilldiff check` fails with a `host:` line and prints the
fix for your agent. `skilldiff run` refuses to start rather than fail every session.
A full run can outlast your agent's shell timeout, so the skill tells your agent to
run it in the background.

</details>

<details>
<summary>Manual CLI workflow and commands</summary>

```bash
skilldiff init --skill ~/code/my-pkg/skills/my-skill --harness claude --dir my-skill-eval
cd my-skill-eval
# 1. Put a small test project in fixtures/
# 2. Describe a real task in tasks/ (do not name the skill in the prompt)
# 3. Write a grader in graders/ that scores the result
skilldiff check          # validates config, CLI login, permissions, and graders
skilldiff run --split dev --runs 1   # smoke test: one pair per dev task
skilldiff results        # show the latest run
```

After reviewing the small test, freeze your held-out tasks and use `skilldiff run`
for a new full evaluation. Held-out tasks are saved tests that you keep unchanged
while improving the skill.

[`examples/csv-totals`](examples/csv-totals) is a complete small experiment you can
copy. It has a skill, dev and held-out tasks, fixtures, a grader, and a sample report.

| Command | What it does |
|---|---|
| `skilldiff init` | Create an experiment (`--skill`, `--skill-a/--skill-b`, or `--pr`) |
| `skilldiff check` | Check config, CLI login, permissions, skill exposure, and graders |
| `skilldiff run` | Run the experiment (`--runs N`, `-t TASK`, `--resume`) |
| `skilldiff results` / `report` | Show or rebuild the reports for a run |
| `skilldiff diagnose` | Explain failures: skill not triggering, regressions, denied tools |
| `skilldiff regrade` | Re-grade a finished run after a grader fix, without new sessions |
| `skilldiff compare` | Compare two runs |
| `skilldiff lint` | Lint a `SKILL.md` before you spend anything |

</details>

<details>
<summary>How the comparison works</summary>

```mermaid
flowchart TB
    T["📦 Your skill<br/>+ tasks + graders"] --> W["🗂️ Fresh copy of the<br/>test project per run<br/><i>same agent · model · task</i>"]
    subgraph PAIR [" "]
        direction LR
        C["🚫 Agent <b>without</b><br/>the skill (control)"]
        S["✨ Agent <b>with</b><br/>the skill (treatment)"]
    end
    W --> C
    W --> S
    C --> G["🙈 Grader scores<br/>anonymous work"]
    S --> G
    G --> R["📊 Repeat and compare<br/>pairs (95% intervals)"]
    R --> V(["✅ SHIP · ⛔ DO NOT SHIP<br/>🔁 NEEDS MORE RUNS"])

    classDef input fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a8a
    classDef control fill:#fee2e2,stroke:#dc2626,stroke-width:2px,color:#7f1d1d
    classDef treatment fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#14532d
    classDef grade fill:#ede9fe,stroke:#7c3aed,stroke-width:2px,color:#4c1d95
    classDef verdict fill:#fef3c7,stroke:#d97706,stroke-width:3px,color:#78350f
    class T,W input
    class C control
    class S treatment
    class G,R grade
    class V verdict
    style PAIR fill:transparent,stroke:#64748b,stroke-width:2px,stroke-dasharray:6 4
```

- Both arms use the same agent, model, task, and starting files. Only the skill differs.
- Each run gets its own throwaway workspace. Control receives no installed target
  copy; confirmed discovery or loading invalidates the baseline.
- Graders see `candidate-A` and `candidate-B`, never which arm is which.
- Arm order alternates from a recorded seed. Inputs are frozen and hashed, so results are reproducible.

</details>

<details>
<summary>Evaluation details and permissions</summary>

The agent sessions skilldiff starts run headless, so nobody can answer a permission
prompt. A tool that asks is refused, and the agent quietly works without it. The
defaults avoid prompts and give both arms the same permissions:

| Harness | Default | To allow network |
|---|---|---|
| `claude` | `sandbox: true`: Bash runs in Claude's own sandbox without prompts; writes stay in the workspace | `allowed_domains: ["pypi.org"]` |
| `codex` | `sandbox: workspace-write`, no prompts | `network_access: true` |
| `opencode` | `dangerously_skip_permissions: true` (passes `--auto`) | already allowed |
| `antigravity` | `dangerously_skip_permissions: true` | already allowed |

`skilldiff check` prints what sessions may do. Reports and `skilldiff diagnose` count
denied tool calls per arm for all four harnesses (including Codex sandbox blocks), so
a broken permission setup cannot pass as a low score. To block reads outside the workspace as well, use
`isolation: docker`/`podman`, or `isolation: macos` for Claude and Codex.

## Tips for useful results

- Write tasks that need what only the skill knows, such as obscure APIs, recent changes, or house rules. If control already scores 100%, the task is too easy.
- Include tasks where the skill should stay out of the way (`category: irrelevant`).
- Tune on `tasks/dev/`, then freeze `tasks/heldout/`. When held-out tasks are planned, the decision requires held-out pairs, even if none have run yet.
- Start with 5 or more runs per arm, and fix that number before you look at results.
- Give each grader a known-good and a broken solution (`validation:`), so `check` can prove the grader works.

</details>

## More detail

The [reference](docs/reference.md) has the exact rules:

- [Reports and outputs](docs/reference.md#reports-and-outputs) and [the example result](docs/reference.md#example-result)
- [Experiment files](docs/reference.md#experiment-files) and [tasks, graders, and categories](docs/reference.md#tasks-graders-and-categories)
- [Why SkillDiff, and how it stays fair](docs/reference.md#why-skilldiff)
- [Where skilldiff installs the skill under test](docs/reference.md#where-skilldiff-installs-the-skill-under-test)
- [Thresholds and verdicts](docs/reference.md#thresholds-and-verdicts)
- [Presets, revisions, and PR tests](docs/reference.md#presets-revisions-and-pr-tests)
- [Resume compatibility and recovery](docs/reference.md#resume-compatibility-and-recovery)
- [Harness setup and commands](docs/reference.md#harness-setup-and-commands)
- [Preflight safeguards](skills/skilldiff/references/preflight.md) and [reporting rules](skills/skilldiff/references/reporting.md)

<details>
<summary>Development</summary>

```bash
git clone https://github.com/karangattu/skilldiff && cd skilldiff
uv sync --frozen --extra dev
uv run --frozen --extra dev python -m pytest
uv run --frozen --extra dev ruff check skilldiff tests
```

These commands select the project environment without relying on shell activation.

See [CHANGELOG.md](CHANGELOG.md) for version history. Current version is 0.21.0.

</details>

# SkillDiff

![SkillDiff: paired agent terminals, with and without a skill, joined by a delta comparison symbol](assets/skill_diff_logo.png)

SkillDiff tests what changes when a coding agent uses a skill. It runs the same
tasks with and without the skill, then compares the results. Your agent prepares
the tests, runs the evaluation, and explains whether the evidence supports keeping
the skill.

You can use Claude Code, Codex, Antigravity CLI (AGY), or OpenCode V2.
You need Python 3.12+, Git, and a signed-in agent CLI (command-line tool).

## Set up through your agent

Paste this into your coding agent:

```text
Set up SkillDiff for this agent from https://github.com/karangattu/skilldiff.
Read the repository's skills/skilldiff/SKILL.md and follow its setup instructions.
Install the command and SkillDiff skill for this agent only.
```

Your agent installs SkillDiff and its skill, then uses your normal CLI login.
If it needs a login or shell permission, it tells you the exact step.
For installation commands, see [manual setup](#manual-setup).

## Test a skill

Give your agent the path to the folder that contains your `SKILL.md`:

```text
Use skilldiff to test ~/code/my-project/skills/my-skill. Start with a small trial and explain the result.
```

Your agent prepares the test files and automatic checks. It uses the current CLI
and an available model. It asks for a choice only when it cannot determine one.
You do not need to write YAML (the configuration file format) or choose command flags.

To try SkillDiff without your own skill or test files, use the demo:

```text
Use skilldiff to try its changelog demo and explain the result.
```

To compare two versions, give your agent both skill folders:

```text
Use skilldiff to compare ~/skills/v1/my-skill with ~/skills/v2/my-skill.
Start with a small trial and tell me whether there is enough evidence to choose a version.
```

Tests use your agent account or API credits. The agent asks before a larger paid
evaluation unless you already authorized it.

## Read the result

Your agent links the report and explains the score, skill use, cost, time, and
tokens (units of model input and output). The report gives one decision:

- `SHIP`: The evidence supports using the skill.
- `DO NOT SHIP`: The evidence does not support using the skill.
- `NEEDS MORE RUNS`: The evidence is too limited for a decision.

A first trial tests the setup. It does not establish a shipping decision.
Missing measurements stay `N/A`. Low skill use blocks a `SHIP` result.
The report explains the evidence and uncertainty behind its decision.

By default, the task prompt does not name the skill. This tests whether the agent
finds and uses the skill on its own. You can also name the skill in the prompt
to test whether the agent follows that instruction.
See [skill discovery and explicit use](docs/reference.md#explicit-versus-implicit-skill-use).

Reports include JSON for agent analysis, Markdown for summaries, and HTML for viewing.
See [reports and outputs](docs/reference.md#reports-and-outputs) for measurements,
cost estimates, and rules for decisions.

## Manual setup

Use the [manual guide](docs/manual.md) for installation commands, updates,
shell permissions, and a manual evaluation. It also explains how comparisons
work and how to contribute changes.

Install from GitHub. The `skilldiff` package on PyPI is a different project.

## Reference

The [reference](docs/reference.md) covers experiment files, command flags, and rules for decisions.
The [compatibility guide](docs/compatibility.md) lists supported agent versions and verification limits.
For agent instructions, start with [skills/skilldiff/SKILL.md](skills/skilldiff/SKILL.md)
and the [agent workflow](skills/skilldiff/references/agent-workflow.md).

See [CHANGELOG.md](CHANGELOG.md) for version history. Current version is 0.28.0.

---
name: skilldiff
description: Measure whether an Agent Skill actually helps. Use when the user wants to test, benchmark, evaluate, or A/B an agent skill (a SKILL.md folder, a package's skills/ directory, or a plugin skill), compare an agent with and without a skill, check if a skill is worth its token cost, or set up or read skilldiff experiments and reports.
---

# Evaluate a skill with skilldiff

skilldiff runs the same tasks twice in identical fresh workspaces. The **control** arm
runs without the skill and the **skill** arm runs with it. Then skilldiff grades both arms
and reports the paired difference in score, cost, time, tokens, and skill adoption.
Your job: design a fair experiment, run it, and explain the result honestly.

## 1. Install and locate

```bash
skilldiff --version || uv tool install git+https://github.com/karangattu/skilldiff
```

If `uv` is not available, use `pipx install git+https://github.com/karangattu/skilldiff`.
Don't run `pip install skilldiff`: that name on PyPI is a different project.

Find the skill under test. This is a directory with `SKILL.md`, or a folder of skill
directories, such as a package's `.claude/skills/` or `skills/`. Read its `SKILL.md`.
Understand what the skill claims to improve before you write any task.

## 2. Scaffold

Create the experiment outside the skill's own repository, so that the fixtures do not
already contain the skill:

```bash
skilldiff init --skill /abs/path/to/skill --dir ./skill-eval --harness claude
```

Harnesses: `claude` (default), `codex`, `opencode`, and `antigravity`. Ask the user
which agent CLI they use if it is not obvious.

## 3. Design tasks (the important part)

Write 2–5 tasks in `tasks/*.yaml`. Each task has a `repo` fixture, a `prompt`, and a
`grader`. Good tasks:

- **Target what the skill uniquely knows.** Examples: obscure or recent APIs, house
  conventions, required commands, and pitfalls that the skill documents. A task that the
  model already solves without the skill shows only a ceiling effect.
- **Don't mention the skill in the prompt.** Write the request the way a real user
  would write it. Adoption is part of what you measure.
- **Keep fixtures small and self-contained.** The fixture is copied into a fresh
  workspace for every run. Put graders in `graders/`, outside the fixture, and call them
  through `$SKILLDIFF_TASK_DIR`, so that the agent can't see or edit them.
- **Grade outcomes deterministically.** Run tests or parse files with `ast` or regex.
  Print `{"score": 0.0-1.0, "success": bool, "checks": [...]}` as the last line.
  Accept every valid solution, not only the one that the skill suggests. A grader that
  rejects an equivalent API call creates a false "improvement".
- **Check that the grader can fail.** The untouched fixture must score below 100%.

Graders run with the workspace as the working directory. They also get these
environment variables: `SKILLDIFF_RESPONSE_FILE` (the agent's final message),
`SKILLDIFF_DIFF_FILE` (the agent's git diff), and `SKILLDIFF_TASK_DIR`.

If the skill runs CLI commands, allow-list them for Claude, for example
`claude.allowed_tools: ["Bash(mytool *)"]`. Both arms get the same permissions.

## 4. Validate, then smoke-test

```bash
skilldiff check -c skill-eval/skilldiff.yaml
```

Fix every `FAIL` and read every `warn`. When `check` is clean, run one pair per task:

```bash
skilldiff run -c skill-eval/skilldiff.yaml --runs 1
```

Open some transcripts under `runs/<timestamp>/<model>/<task>/{control,treatment}/`. Did
the skill arm load the skill? Did the grader score what you expected?

**Cost:** each run starts `models × tasks × runs × 2` agent sessions. `check` prints
this count and, for Claude, the maximum spend. **Confirm with the user before you run
more than a smoke test.** A long run can take longer than your shell tool's timeout.
In that case, run it in the background and poll `skilldiff results`. You can also give
the user the command to run.

## 5. Full run and interpretation

Use `runs: 5` or more. With fewer runs the confidence intervals are too wide to mean
much. Use `--parallel N` only if the user's rate limits allow it.

Read `report.md` (for pull requests) or `report.html`. Report these results:

1. **Verdict with uncertainty.** Give the mean paired score difference and its 95% CI.
   If the CI includes zero, the effect is not established. Say so plainly.
2. **Adoption.** Say how many skill runs actually used the skill. Low adoption usually
   means that the skill's `description` doesn't match how users ask for the task.
3. **Efficiency.** Give the cost, time, and token changes, and say which ones are
   inside the noise.
4. **Warnings.** Report agent errors or timeouts, control contamination, tasks without
   graders, and ceiling effects.
5. **Next step.** Suggest harder tasks, a sharper skill description, or more
   repetitions, depending on the result.

Don't overstate the result. "Faster in 3 runs" is an anecdote, not a finding. Useful
commands: `skilldiff results --markdown` prints a Markdown summary for a PR, and
`skilldiff report` rebuilds the reports for an old run.

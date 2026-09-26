---
name: skilldiff
description: Measure whether an Agent Skill actually helps. Use when the user wants to test, benchmark, evaluate, or A/B an agent skill (a SKILL.md folder, a package's skills/ directory, or a plugin skill), compare an agent with and without a skill, check if a skill is worth its token cost, or set up or read skilldiff experiments and reports.
---

# Evaluate a skill with skilldiff

skilldiff runs the same tasks twice in identical fresh workspaces. The **control** arm
runs without the skill and the **skill** arm runs with it. Then skilldiff grades both arms
and reports the paired difference in score, cost, time, tokens, and skill adoption.
Your job: design a fair experiment, run it, and explain the result honestly.

If the user invoked this skill with arguments (for example `/skilldiff ./skills/my-skill`
or `$skilldiff ./skills/my-skill`), treat the first path as the skill under test. Treat
any other words as extra instructions, such as the models or harness to use.

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

Write 4–8 tasks in `tasks/*.yaml`. Each task has a `repo` fixture, a `prompt`, a
`category`, and a `grader`. Cover all four kinds:

- **Intended (`intended`).** Target what the skill uniquely knows: obscure or
  recent APIs, house conventions, required commands, pitfalls. Keep 2–3 of these.
  A task the model already solves without the skill shows only a ceiling effect.
- **Representative (`general`).** Normal requests a real user would make, even
  when the skill is not obviously needed. These stop you from overfitting to
  skill-friendly tasks.
- **Irrelevant (`irrelevant`).** Tasks where the skill must stay out of the way.
  Adoption should be low and cost must not rise here.
- **Ambiguous plus regression (`ambiguous`).** Unclear triggers and past bugs.
  Add at least one regression case per known failure.

Split tasks before you run:

- **`tasks/dev/`** — iterate here while you sharpen prompts and graders.
- **`tasks/heldout/`** — freeze here before the full run. Do not edit held-out
  tasks, fixtures, or graders after you see results. Report dev and held-out
  separately; the held-out set is the honest estimate.

Other rules:

- **Don't mention the skill in the prompt.** Write the request the way a real user
  would write it. Adoption is part of what you measure.
- **Keep fixtures small and self-contained.** The fixture is copied into a fresh
  workspace for every run, without `.git` history. Escaping symlinks are rejected.
  Put graders in `graders/`, outside the fixture, and call them through
  `$SKILLDIFF_TASK_DIR`. Outside the fixture is not isolation by itself: confine
  agents with the harness sandbox so they cannot read parent paths.
- **Grade outcomes deterministically.** Run tests or parse files with `ast` or regex.
  Print `{"score": 0.0-1.0, "success": bool, "checks": [...]}` as the last line.
  Accept every valid solution, not only the one that the skill suggests. A grader that
  rejects an equivalent API call creates a false "improvement".
- **Test the grader three ways.** The untouched fixture must score below 100%, a
  known-good solution must score 100%, and deliberately broken solutions must fail.
  Add them as `validation: {good: ..., broken: [...]}` in the task file so `check`
  grades all three. A grader that fails everything is broken, not strict. Crashing
  graders report `error` (N/A), never a plain zero.

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
more than a smoke test.**

**Running from inside an agent:** skilldiff starts separate, non-interactive agent
sessions (`claude -p`, `codex exec`, and so on). They need network access and a
signed-in CLI. Keep these points in mind:

- A full run usually takes longer than your shell tool's timeout. Start it in the
  background, redirect its output to a log file, and poll the log or
  `skilldiff results`.
- If your sandbox blocks network access or starting other agent CLIs, the sessions
  fail with auth or connection errors. The report lists them as errors. Don't retry
  in a loop. Give the user the exact `skilldiff run ...` command to run in their own
  terminal, then read the results with `skilldiff results`.
- If `check` reports that the harness CLI is missing, or a smoke run fails with "Not
  logged in", ask the user to sign in (for example `claude auth login`). Never ask for
  or handle their credentials yourself.

## 5. Full run and interpretation

Fix the stopping rule before you look at results. Write it in `skilldiff.yaml`:

```yaml
runs: 5
# seed: 1234   # recorded per run; balanced arm order is reproducible
# failure_policy: {agent_failure: exclude, missing: exclude}
```

`runs: 5` is a starting point, not a sufficiency rule. Many repetitions of two
tasks still describe only those two tasks. Prefer 4+ tasks across categories
over 10 repetitions of one task. Do not add runs until the interval looks good.
Use `--parallel N` only if the user's rate limits allow it. Use `--resume` to
continue an interrupted run; completed pairs reuse only when input hashes match.

Read `report.md` (for pull requests) or `report.html`. Report these results:

1. **Verdict with uncertainty.** Give the mean paired score difference and its 95% CI.
   If the CI includes zero, the effect is not established. Say so plainly.
   Shipping needs bounds to clear thresholds, not point estimates. Report the
   task count alongside the pair count.
2. **Adoption.** Say how many skill runs actually used the skill. Low adoption usually
   means that the skill's `description` doesn't match how users ask for the task.
3. **Efficiency.** Give the cost, time, and token changes, and say which ones are
   inside the noise. Then add a final cost table: look up the current per-token
   prices for the models in the run on the providers' own pricing pages, multiply
   them by the token counts in the report, and show the API-equivalent cost per
   arm. Name the price source and date next to the table. On subscription auth
   the real spend is $0 at the margin, so this table is the comparison that
   matters. Use this shape, with one plain verdict per row:

   | Metric | Control | Skill | Change | Reading |
   |---|---|---|---|---|
   | Task score | 50% | 83% | +33 pp | No clear difference |
   | Cost | $0.50 | $0.40 | -$0.10 | Costs less |

   End with one bottom line: SHIP, DO NOT SHIP, or NEEDS MORE RUNS, plus one
   sentence that states why.
4. **Warnings.** Report agent errors or timeouts, control contamination, tasks without
   graders, and ceiling effects.
5. **Next step.** Suggest harder tasks, a sharper skill description, or more
   repetitions, depending on the result.

Don't overstate the result. "Faster in 3 runs" is an anecdote, not a finding. Useful
commands: `skilldiff results --markdown` prints a Markdown summary for a PR, and
`skilldiff report` rebuilds the reports for an old run.

## 6. Compare skill revisions (A/B)

To test skill A versus skill B in one experiment, with identical fixtures and
paired results:

```bash
skilldiff init --skill-a ./skills/v1 --skill-b ./skills/v2 --dir ./skill-ab --harness claude
# add --include-baseline for a no-skill arm per pair
```

Control is skill A, treatment is skill B. Use `skilldiff compare runA runB --strict`
only for cross-run checks; prefer single-run A/B because `compare` must match
tasks and repetitions to normalize efficiency.

## 7. Evaluate a PR

Pick the workflow first:

- **Agent effectiveness (`mode: agent`).** Agents work on each revision.
- **PR correctness (`mode: correctness`).** Graders run on untouched revisions, no agents.

Pick the revisions:

- **`pair: merge-base`** (default): merge-base vs head.
- **`pair: base-merge`**: base tip vs synthetic merge of head into base (integration).

```bash
skilldiff init --pr 42 --repo ~/code/pkg --base origin/main --dir ./pr-42-eval
# add --pr-mode correctness --pr-pair base-merge as needed
```

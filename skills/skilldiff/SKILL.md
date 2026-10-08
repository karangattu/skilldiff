---
name: skilldiff
description: Measure whether an Agent Skill actually helps. Use when the user wants to test, benchmark, evaluate, or A/B an agent skill (a SKILL.md folder, a package's skills/ directory, or a plugin skill), compare an agent with and without a skill, decide whether to ship a skill or whether it is worth its token cost, run a skill regression check, or set up or read skilldiff experiments and reports.
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

A git install is pinned to the commit that was current when it was installed, so an
existing `skilldiff` will not pick up later fixes on its own. Install it if missing and
refresh it otherwise:

```bash
command -v skilldiff >/dev/null || uv tool install git+https://github.com/karangattu/skilldiff
uv tool upgrade skilldiff
```

If `uv` is not available, use `pipx install git+https://github.com/karangattu/skilldiff`
and `pipx upgrade skilldiff`. Don't run `pip install skilldiff`: that name on PyPI is a
different project.

Find the skill under test. This is a directory with `SKILL.md`, or a folder of skill
directories, such as a package's `.claude/skills/` or `skills/`. Read its `SKILL.md`.
Understand what the skill claims to improve before you write any task.

Lint it before you spend anything:

```bash
skilldiff lint /abs/path/to/skill
```

`lint` checks the frontmatter, description length and trigger keywords, broken
links, and the skill's estimated token footprint. Fix what it flags now: a weak
description is the usual cause of the low adoption you will measure later.

## 2. Scaffold

Create the experiment outside the skill's own repository, so that the fixtures do not
already contain the skill. Pick the harness the user actually uses (`claude`
default, `codex`, `opencode`, `antigravity`) and pass it to every `init`:

```bash
skilldiff init --skill /abs/path/to/skill --dir ./skill-eval --harness claude
skilldiff init --skill /abs/path/to/skill --dir ./skill-eval --harness codex
skilldiff init --skill /abs/path/to/skill --dir ./skill-eval --harness opencode
skilldiff init --skill /abs/path/to/skill --dir ./skill-eval --harness antigravity
```

Ask the user which agent CLI they use if it is not obvious. The other three
modes scaffold the same way:

```bash
# PR: code without the PR vs code with the PR
skilldiff init --pr 42 --repo /abs/path/to/repo --base origin/main --dir ./pr-42-eval --harness claude
# Skill A vs skill B (add --include-baseline for a no-skill arm per pair)
skilldiff init --skill-a /abs/path/to/v1 --skill-b /abs/path/to/v2 --dir ./skill-ab --harness claude
# Original vs minified (triggers must match; see section 6)
skilldiff init --skill-a /abs/path/to/original --skill-b /abs/path/to/minified --preset compression --dir ./skill-compression --harness claude
```

You never install the skill into workspaces yourself. SkillDiff installs the
right revision into each fresh workspace per harness (`.claude/skills` for
Claude, `.codex/skills` plus `.agents/skills` for Codex, `.opencode/skills`
plus `.agents/skills` for OpenCode, `.agents/skills` for Antigravity).

A complete worked example — a skill, dev and held-out tasks, fixtures, a
deterministic grader, and a committed sample report — lives in
[examples/csv-totals](https://github.com/karangattu/skilldiff/tree/main/examples/csv-totals).
Copy its shape whenever you are unsure how a piece fits together.

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

The split is recorded per task (`split: dev` or `split: held-out`, or inferred
from the `dev/`/`heldout/` directory) and shown in the report's **By split**
table. The closing recommendation uses held-out pairs only when they exist, so
development results cannot stand in for validation.

Other rules:

- **Don't mention the skill in the prompt.** Write the request the way a real user
  would write it. Adoption is part of what you measure.
- **Contain the blast radius.** Set `allowed_paths` (and optionally
  `forbidden_paths`) on each task. An edit outside scope is then reported as an
  error — `N/A (blast radius)` with the path named — never as a wrong answer.
  Patterns match at any depth, so `data/*` also matches a saved copy under
  `outputs/measurements/…/data/`. If the skill under test asks agents to save
  measurements or copies, add `grader_ignore: ["outputs/*"]` to the task: those
  paths are hidden from the grader and skipped by the scope check. `check` warns
  when a forbidden pattern would match nested copies. SkillDiff appends the same
  constraints to both arms' prompts and rejects known-good solutions that conflict
  with them. Fix task scope before accepting grader validation.
- **Script multi-turn tasks with `prompts:`.** A list of prompts runs in order
  in one workspace; tokens, cost, time, and turns sum across the turns, and one
  timeout covers the whole script.
- **Keep fixtures small and self-contained.** The fixture is copied into a fresh
  workspace for every run, without `.git` history. Escaping symlinks are rejected.
  Put graders in `graders/`, outside the fixture, and call them through
  `$SKILLDIFF_TASK_DIR`. Outside the fixture is not isolation by itself: confine
  agents with an enforced read sandbox or container so they cannot read parent paths.
  Each agent run has its own temporary directory outside the workspace;
  `TMPDIR`, `TMP`, and `TEMP` point there, scripted turns share it, and it is
  cleaned up after the run. This reduces accidental sharing, but does not block
  reads of sibling sessions or host files.
- **Grade outcomes deterministically.** Run tests or parse files with `ast` or regex.
  Print `{"score": 0.0-1.0, "success": bool, "checks": [...]}` as the last line.
  Accept every valid solution, not only the one that the skill suggests. A grader that
  rejects an equivalent API call creates a false "improvement". Parse code instead of
  searching raw text: a comment such as `# instead of renderUI()` fails a text search
  on a correct solution, and an alias (`result = task.result`) fails a search for a
  literal call. Print free-text diagnostics under `notes` (string, list, or mapping)
  rather than as fake checks; notes are shown in the report but never scored.
- **Test the grader three ways.** The untouched fixture must score below 100%, a
  known-good solution must score 100%, and deliberately broken solutions must fail.
  Add them as `validation: {good: ..., broken: [...]}` in the task file so `check`
  grades all three. `good` takes a list: give two differently shaped valid solutions
  (another API call, an alias, a different structure) so `check` can tell a strict
  A broken fixture that crashes or times out does not validate the grader: fix
  the infrastructure until it returns a graded failing result. `check` also flags
  deprecated Python APIs in fixtures and graders, and warns when a Python fixture has
  an empty `allowed_domains`. For an LLM/rubric grader, supply a judge `command:`;
  SkillDiff has no built-in judge.

Graders run with the workspace as the working directory. They also get these
environment variables: `SKILLDIFF_RESPONSE_FILE` (the agent's final message),
`SKILLDIFF_DIFF_FILE` (the agent's git diff), `SKILLDIFF_CHANGED_FILES_FILE` (the
changed paths, one per line, without `grader_ignore` paths), and `SKILLDIFF_TASK_DIR`.

**Permissions.** Sessions run headless, so any tool that would prompt is refused and
the agent works without it. Keep the defaults (`claude.sandbox: true`,
`codex.sandbox: workspace-write`, and `dangerously_skip_permissions: true` for
OpenCode and Antigravity). They avoid prompts and give both arms the same permissions.
If tasks need the network, set `claude.allowed_domains` or `codex.network_access:
true`. Reports and `skilldiff diagnose` flag denied tools for every harness; fix
them and rerun before interpreting scores. For defaults and read-isolation limits, see
[preflight safeguards](references/preflight.md).

## 4. Validate, then smoke-test

```bash
skilldiff check -c skill-eval/skilldiff.yaml
```

Codex discovery is checked before paid sessions. For native macOS isolation,
custom wrappers, and task `runtime_probe:` checks, read
[preflight safeguards](references/preflight.md). Availability is separate from adoption.

Fix every `FAIL` and assess every `warn` before running one pair per task. Local
checks warn about live skill sources and matching copies in `runs/`, including
input snapshots, even when automatic user-level loading is disabled. These are
possible exposure paths, not proof of contamination; they do not make a run
INVALID. Runs save exposure warnings in results and reports.

For a broader advisory check, use `skilldiff check --scan-home`. It matches
directory/frontmatter names or identical `SKILL.md` contents. Scans stop after
20000 entries, 5 seconds, or 20 matches, report skipped/unreadable paths, exclude
`.git`, `.venv`, `venv`, `node_modules`, `__pycache__`, and `.cache`, skip files
over 1 MiB, and do not follow symlinks. No matches cannot certify a clean host.
Host checks are skipped for agent containers, whose image must also be clean.

Then run one pair per task:

```bash
skilldiff run -c skill-eval/skilldiff.yaml --runs 1
```

Open some transcripts under `runs/<timestamp>/<model>/<task>/{control,treatment}/`
(plus `{baseline}/` when the baseline arm is enabled). Did the skill arm load
the skill? Did the grader score what you expected? In PR correctness mode there
are no agent sessions to inspect; check the graded revision outputs instead.

**Cost:** each run starts `models × tasks × runs × 2` agent sessions (`×3` with
`include_baseline`). `check` prints this count and, for Claude, the maximum
spend. **Confirm with the user before you run more than a smoke test.**

**Running from inside an agent:** skilldiff starts separate, non-interactive agent
sessions (`claude -p`, `codex exec`, `opencode run`, `agy -p`) that need network
access and write access to their login directory.

- Run `skilldiff` as a plain command (paths via `-c`; no `cd`, pipes, redirects, or
  `$(...)`) so the user's allow rule matches it.
- If `check` shows `FAIL host: ...`, your shell is sandboxed and every session would
  fail. Don't retry and don't change the user's agent settings yourself. Show the
  user the `fix:` line, or give them the exact `skilldiff run ...` command for their
  own terminal. Read the results afterwards with `skilldiff results`.
- A full run usually takes longer than your shell tool's timeout. Start
  `skilldiff run -c skill-eval/skilldiff.yaml` with your shell tool's background
  option, check its progress output now and then, and read `skilldiff results` when
  it ends.
- For Claude, `check` makes one tiny authenticated call (`--no-probe` skips it) to
  catch an expired login.
- If `check` reports that the harness CLI is missing, or a smoke run fails with "Not
  logged in", ask the user to sign in with that harness's normal login (for example
  `claude auth login` or `opencode providers login`). Never ask for or handle their
  credentials yourself. A missing binary can also be set per harness with `bin_path`
  or `CLAUDE_BIN`, `CODEX_BIN`, `OPENCODE_BIN`, `AGY_BIN`.

## 5. Full run and interpretation

Fix the stopping rule before you look at results. Write it in `skilldiff.yaml`:

```yaml
runs: 5
# seed: 1234   # recorded per run; balanced arm order is reproducible
# failure_policy: {agent_failure: exclude, missing: exclude}
# thresholds:   # the closing verdict applies these bounds; fix them before the run
#   meaningful_score_gain_pp: 5
#   acceptable_score_regression_pp: 5
#   required_cost_reduction_pct: 10
#   required_token_reduction_pct: 20
# cost_basis: api-equivalent  # decide with saved rates (default: harness)
# API-equivalent cost basis: look the rates up on the provider's own pricing
# page BEFORE the run and record them here, so the saved run reproduces the
# estimate and the report shows the API-equivalent cost per arm:
# pricing:
#   source: https://www.anthropic.com/pricing
#   date: "2026-09-27"
#   currency: USD
#   rates:
#     claude-sonnet-5: {input: 3.00, output: 15.00, cache_read: 0.30, cache_write: 3.75}
```

`runs: 5` is a starting point, not a sufficiency rule. Many repetitions of two
tasks still describe only those two tasks. Prefer 4+ tasks across categories
over 10 repetitions of one task. Do not add runs until the interval looks good.
Use `--parallel N` only if the user's rate limits allow it. Use `--resume` (or
`--resume-from <run-dir>`) to continue an interrupted run; completed pairs are
reused only when skill, task, PR revision, and execution settings match, and a
mismatch refuses instead of silently mixing results.

Container execution needs a locally available image containing the harness CLI,
graders, and dependencies. `bin_path` refers to an executable inside that image.
Claude and Codex container runs require API authentication; host login directories
are not mounted. Run `skilldiff check` first. Resume also requires the same
isolation mode and immutable container image identity.

Read `report.md` (for pull requests) or `report.html`. Then run
`skilldiff diagnose` on the run directory: it names the common failure modes —
a skill that never triggered on intended tasks, one that triggered on
irrelevant tasks, regressions, blast-radius violations, agent failures,
instruction over-reading (direct file reads of `SKILL.md`), and
token bloat without score gains — each with a suggested fix. Check its findings
against the report before you write your summary. Report these results:

1. **Verdict with uncertainty.** Give the mean paired score difference and its 95% CI.
   If the CI includes zero, the effect is not established. Say so plainly.
   Shipping needs bounds to clear thresholds, not point estimates. Report the
   task count alongside the pair count.
   Use the count of tasks contributing usable paired scores, not the planned
   task count. Terminal and saved reports share the same decision; held-out
   evidence controls it when present, and contaminated controls cannot ship.
2. **Adoption.** Say how many skill runs actually used the skill. Low adoption usually
   means that the skill's `description` doesn't match how users ask for the task.
3. **Efficiency.** Give the cost, time, and token changes, and say which ones are
   inside the noise. On subscription auth the real spend is $0 at the margin, so
   the API-equivalent cost is the comparison that matters. The report ends with
   a **Closing decision** table and one bottom line: SHIP, DO NOT SHIP, or
   NEEDS MORE RUNS (with ceiling or floor effects noted when tasks cannot discriminate).
   End your summary with that same bottom line and reason; do
   not invent a different verdict from the one the report computed.

   Before you write the final summary, read
   [references/reporting.md](references/reporting.md) (next to this file). It
   specifies the API-equivalent cost procedure, the **Evaluation results** table
   you must reproduce (exact columns, row rules, `N/A` handling, failure-policy
   effects), and the closing-decision contract.
4. **Warnings.** Report agent errors or timeouts, control contamination, tasks without
   graders, and ceiling effects. The report's **Evaluation completeness** row counts
   planned/completed pairs, usable score pairs, agent failures, and grader errors in
   one place; quote it when results are partial. Blast-radius exclusions are named
   apart from grader errors: check the task's `allowed_paths`/`forbidden_paths`
   before blaming the agent or the skill.
5. **Next step.** Suggest harder tasks, a sharper skill description, or more
   repetitions, depending on the result.

Don't overstate the result. "Faster in 3 runs" is an anecdote, not a finding. Useful
commands: `skilldiff results --markdown` prints a Markdown summary for a PR, and
`skilldiff report` rebuilds the reports for an old run.

**Before you trust a surprising per-run difference, read the diff.** Open the
`diff.patch` of any run that scored below its pair and confirm the grader judged real
work. If the grader was wrong, fix it, run `skilldiff regrade <run> --dry-run`, then
`skilldiff regrade <run>`: it re-runs the current graders on the saved diffs
(including binary diffs) without any agent session, surfaces skipped runs in the
headline, keeps the previous grades under `regrade_history`, and adds a
visible warning to the report. Report the regrade openly and do not hand-compute
replacement scores. Say which grader change you made and why.

## 6. Compare skill revisions (A/B)

To test skill A versus skill B in one experiment, with identical fixtures and
paired results:

```bash
skilldiff init --skill-a ./skills/v1 --skill-b ./skills/v2 --dir ./skill-ab --harness claude
# add --include-baseline for a no-skill arm per pair (balanced rotation)
# add --preset revision (default) or --preset compression (original vs minified)
```

Control is skill A, treatment is skill B. The baseline rotates through all
positions and the report shows baseline-vs-A and baseline-vs-B. Use
`skilldiff compare runA runB --strict` only for cross-run checks; prefer
single-run A/B because `compare` must match tasks and repetitions to
normalize efficiency.
Each metric requires both sides to have a value and applies the recorded failure
policy. Read the metric's usable pair counts; failed partial checks are diagnostic
only. Strict comparison refuses differing failure policies.

For compression: keep the skill name and trigger description identical so
adoption changes do not confound the body comparison. Record source-size
change (reported as a signed percentage) separately from session tokens, cost,
and time. Fix acceptable loss before running (for example: at most 2pp loss with
at least 20% fewer tokens) and require bounds to support it. Tune on dev tasks,
then compare frozen versions on held-out tasks.

## 7. Evaluate a PR

Pick the workflow first:

- **Agent effectiveness (`mode: agent`).** Agents work on each revision. Skills
  modified by the PR diff are automatically discovered and installed into workspaces
  (even when located outside standard skill roots).
- **PR correctness (`mode: correctness`).** Graders run on untouched revisions, no agents.

Pick the revisions:

- **`pair: merge-base`** (default): merge-base vs head.
- **`pair: base-merge`**: base tip vs synthetic merge of head into base (integration).

```bash
# fetch the PR ref first, so the repo holds the head revision
git -C ~/code/pkg fetch origin refs/pull/42/head:refs/pull/42/head
skilldiff init --pr 42 --repo ~/code/pkg --base origin/main --dir ./pr-42-eval
# add --pr-mode correctness --pr-pair base-merge as needed
```

Choose the decision `cost_basis` before the run; see [reporting](references/reporting.md).
Incomplete trials retain telemetry and diffs but never enter paired decisions.
Resume preserves them and refuses implicit retries. See [preflight safeguards](references/preflight.md).

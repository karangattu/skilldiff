---
name: skilldiff
description: Measure whether an Agent Skill actually helps. Use when the user wants to test, benchmark, evaluate, or A/B an agent skill (a SKILL.md folder, a package's skills/ directory, or a plugin skill), compare an agent with and without a skill, decide whether to ship a skill or whether it is worth its token cost, run a skill regression check, or set up or read skilldiff experiments and reports.
---

# Evaluate a skill with skilldiff

skilldiff runs the same tasks twice in identical fresh workspaces: **control**
without the skill and **skill** with it. skilldiff grades both arms and reports paired
differences in score, cost, time, tokens, and adoption. Design a fair experiment, run
it, and explain the result honestly.

Own setup, task files, graders, execution, and interpretation. The user provides a
goal and skill path. Reuse their harness and model when established; ask only for
missing choices. For setup, short requests, and demos, read
[agent workflow](references/agent-workflow.md).

If the user passed arguments (for example `/skilldiff ./skills/my-skill`), treat the first
path as the skill under test and remaining words as extra instructions (models, harness).

## 1. Install and locate

Install the tool if missing:

```bash
command -v skilldiff >/dev/null || uv tool install git+https://github.com/karangattu/skilldiff
```

If `uv` is unavailable, use `pipx install git+https://github.com/karangattu/skilldiff`.
Reuse a working installation. Git installs do not update automatically; use
`uv tool upgrade skilldiff` or `pipx upgrade skilldiff` when an update is requested
or needed for compatibility. Do not use PyPI's unrelated `skilldiff` package.
Stop after installation for setup-only requests.

For a demo, use `init` without `--skill` and lint its generated skill. Otherwise,
find the skill under test. This is a directory with `SKILL.md`, or a folder of skill
directories, such as a package's `.claude/skills/` or `skills/`. Read its `SKILL.md`.
Understand what the skill claims to improve before you write any task.

Lint it before you spend anything:

```bash
skilldiff lint /abs/path/to/skill
skilldiff lint /abs/path/to/skill --prompts ./skill-eval/tasks/  # trigger coverage
```

`lint` checks frontmatter, description, trigger keywords, links, and token
footprint. `--prompts` flags low trigger overlap and prompt solution leakage
(terms exposing skill body instructions). `diagnose` separately flags ceiling
effects from prompt leakage. Fix what they flag: weak descriptions hurt
adoption; prompt leakage masks real skill effect.

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

Claude scaffolds pin `claude-sonnet-5-5`. Use full IDs such as
`claude-haiku-5-5`; bare `sonnet`/`haiku`/`opus` fail config loading.
`--template claude-eval` adds the eval profile, cost basis, thresholds, and splits.

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
Claude, `.agents/skills` for Codex, `.opencode/skills`
for OpenCode, `.agents/skills` for Antigravity).

Use [examples/csv-totals](https://github.com/karangattu/skilldiff/tree/main/examples/csv-totals)
for dev and held-out tasks, fixtures, a grader, and a sample report, or
[examples/py-shiny-troubleshooting](https://github.com/karangattu/skilldiff/tree/main/examples/py-shiny-troubleshooting)
for a reusable 6-task Python Shiny diagnostic benchmark pack.

## 3. Design tasks (the important part)

For a full evaluation, write 4–8 tasks in `tasks/*.yaml`. A first small trial can
use one development task. Each task has a `repo` fixture, a `prompt`, a
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
table. When held-out tasks are planned, the closing recommendation requires held-out
pairs, even if those tasks have not run yet. Development results cannot stand
in for missing validation.

Other rules:

- **Don't mention the skill in the prompt.** Write the request the way a real user
  would write it. Adoption is part of what you measure.
- **Contain the blast radius.** Set `allowed_paths` (and optionally
  `forbidden_paths`). Out-of-scope edits report `N/A (blast radius)` with the
  path named, never `0%`. Patterns match at any depth, so `data/*` matches a copy
  under `outputs/measurements/…/data/`. Add `grader_ignore: ["outputs/*"]` or
  `grader_ignore: python-tooling` to hide audit or tool files from the grader and
  scope check. In PR mode, overlay extra files via `fixtures: ["../fixtures/app"]`.
  `check --fail-on-ceiling` fails tasks whose untouched fixture scores 100%.
  SkillDiff appends constraints to both arms' prompts and rejects known-good
  solutions that conflict with them.
- **Script multi-turn tasks with `prompts:`.** Prompts run in order in one
  conversation and workspace under one deadline, stopping on any failed turn.
  Claude and AGY use streaming input; Codex uses an ephemeral app-server thread;
  OpenCode V2 uses an explicit session ID with `--standalone`. Sum only per-turn
  counters and take cumulative counters once.
- **Keep fixtures small and self-contained.** Fixtures are copied fresh without
  `.git` history and escaping symlinks are rejected. Keep graders in `graders/`
  (`$SKILLDIFF_TASK_DIR`) and use an enforced read sandbox or container to block
  parent reads. Each session gets a private `TMPDIR`/`TMP`/`TEMP` outside the
  workspace, shared across scripted turns and cleaned up afterward.
- **Grade outcomes deterministically.** Run tests or parse with `ast`/regex and
  print `{"score": 0.0-1.0, "success": bool, "checks": [...]}` last. Accept all
  valid solutions (aliases, equivalent APIs) and parse code rather than raw text
  comments. Do not require a particular function or output name unless the task
  explicitly fixes it. Put ungraded diagnostics under `notes`.
- **Test the grader three ways.** Untouched fixtures must score `<100%`,
  known-good solutions `100%`, and broken solutions must fail cleanly (not crash
  or time out). Set `validation: {good: [...], broken: [...]}` with at least two
  differently shaped `good` solutions. For LLM/rubric graders, supply `command:`.

Graders run with the workspace as the working directory. They also get these
environment variables: `SKILLDIFF_RESPONSE_FILE` (the agent's final message),
`SKILLDIFF_DIFF_FILE` (the agent's git diff), `SKILLDIFF_CHANGED_FILES_FILE` (the
changed paths, one per line, without `grader_ignore` paths), and `SKILLDIFF_TASK_DIR`.

**Permissions.** Sessions run headless, so anything that would prompt is refused.
Keep the defaults (`claude.sandbox: true`, `codex.sandbox: workspace-write`, and
`dangerously_skip_permissions: true` for OpenCode and Antigravity). Set
`claude.allowed_domains` or `codex.network_access: true` for network tasks. For
apps that need background launch and localhost, use opt-in `claude.profile: eval`;
it keeps the sandbox on and never allows unsandboxed commands. Do not set
`sandbox: false`, which exposes graders and skill sources. `check` fails on any
refused canary shape and probes each task's `runtime_probe` through Claude. Fix
denials before interpreting scores; `diagnose` also flags a model served as
another ID. See [preflight safeguards](references/preflight.md).

## 4. Validate, then smoke-test

```bash
skilldiff check -c skill-eval/skilldiff.yaml --fail-on-ceiling
```

Codex discovery is checked before paid sessions. For native macOS isolation,
custom wrappers, and task `runtime_probe:` checks, read
[preflight safeguards](references/preflight.md). Availability is separate from adoption.
Probe the actual test workflow, including temporary tests and browsers.

Fix every `FAIL` and assess every `warn` before running one pair per task. Local
checks warn about live skill sources and matching copies in `runs/`, including
input snapshots, even when automatic user-level loading is disabled. These are
possible exposure paths, not proof of contamination; they do not make a run
INVALID. Runs save exposure warnings in results and reports.

For a broader advisory check, use `skilldiff check --scan-home`; see
[preflight safeguards](references/preflight.md) for its limits. No matches cannot
certify a clean host.

Then run one pair per development task only:

```bash
skilldiff run -c skill-eval/skilldiff.yaml --split dev --runs 1 --terminate-on-ceiling
```

Use `--terminate-on-ceiling`, `--max-turns`, `--max-tool-budget`, and `env:` for
fast smoke runs; see [preflight safeguards](references/preflight.md).

Inspect and fix dev results only; add dev tasks if absent. Start the full
evaluation without `--split dev` or `--resume`. Never tune against held-out results.

Open some transcripts under `runs/<timestamp>/<model>/<task>/{control,treatment}/`
(plus `{baseline}/` when the baseline arm is enabled). Did the skill arm load
the skill? Did the grader score what you expected? In PR correctness mode there
are no agent sessions to inspect; check the graded revision outputs instead.

**Cost:** each run starts `models × selected tasks × runs × 2` agent sessions (`×3` with
`include_baseline`). `run` prints the selected count; `check` prints full-run
count and Claude maximum spend. **Confirm with the user before you run more than a smoke test.**

When running SkillDiff from another agent, follow the
[agent-host instructions](references/preflight.md#running-from-inside-an-agent)
for shell restrictions, background execution, and authentication. Codex multi-turn
sessions use `app-server`; other native headless commands are unchanged.

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
# cost_basis: api-equivalent  # Claude subscription default
# pricing:
#   source: https://www.anthropic.com/pricing
#   date: "2026-10-19"
#   rates:
#     claude-sonnet-5-5: {input: 2.00, output: 10.00, cache_read: 0.10, cache_write: 2.50}
```

`runs: 5` is a starting point, not a sufficiency rule. Many repetitions of two
tasks still describe only those two tasks. Prefer 4+ tasks across categories
over 10 repetitions of one task. Do not add runs until the interval looks good.
Use `--parallel N` only if the user's rate limits allow it. Use `--resume` (or
`--resume-from <run-dir>`) to continue an interrupted run; completed pairs are
reused only when skill, task, PR revision, and execution settings match, and a
mismatch refuses instead of silently mixing results. Pass `--retry-failed` to re-run
pairs that hit harness errors, timeouts, or failures while preserving successful pairs.
Set `parallel: 1` and `arm_parallel: false` for serial execution.

Container execution needs a local image with the harness CLI, graders, and
dependencies; see [preflight safeguards](references/preflight.md).

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
2. **Adoption.** Say how many skill runs actually loaded the skill, per skill arm
   (including both A and B in revision/compression mode), and separately note
   unknown runs and load-event counts. Low adoption usually means that the
   skill's `description` doesn't match how users ask for the task. Adoption is
   judged on the **decision basis**: the held-out runs when the experiment
   declares a held-out split, every run otherwise. The report names that basis,
   and `results.json` and `skilldiff diagnose` judge the same runs. When fewer
   than two-thirds of measured runs load a skill arm, or the arm never loaded the
   skill in any measured run, `results.json` and `skilldiff diagnose` set
   `adoption_failed: true`, **Evaluation completeness**
   counts adoption failures, and the closing decision cannot be SHIP. A hit rate
   needs three measured runs to judge an arm; fewer is thin evidence, so say so
   rather than calling it a pass or a failure. Read the
   report's **Skill adoption and next steps** section and give the user its
   concrete action items: which task phrasing to add to the description, which
   irrelevant tasks need a scope boundary, or whether the skill loaded but did
   not change the outcome. Tune the description on dev tasks first
   (`skilldiff run --split dev --runs 1`).
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
commands: `skilldiff results --markdown` prints a Markdown summary for a PR,
`skilldiff results --json` and `skilldiff diagnose --json` emit structured JSON
for dashboards, `--ci` enforces non-zero exit codes on invalid baselines, ceiling
termination, or adoption failures, `skilldiff export [run-dir] [--format scout|json|jsonl]`
exports structured transcripts and metadata for Inspect Scout transcript auditing,
and `skilldiff report` rebuilds the reports for an old run.

**Before you trust a surprising per-run difference, read the diff.** Open the
`diff.patch` of any run that scored below its pair and confirm the grader judged real
work. If the grader was wrong, fix it, run `skilldiff regrade <run> --dry-run`, then
`skilldiff regrade <run>`: it re-runs the current graders on the saved diffs
(including binary diffs) without any agent session, surfaces skipped runs in the
headline, keeps the previous grades under `regrade_history`, and adds a
visible warning to the report. Report the regrade openly and do not hand-compute
replacement scores. Say which grader change you made and why.

## Measure discovery versus obedience

`skill_invocation: explicit` names the skill in the prompt. See
[references/invocation.md](references/invocation.md).

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

PR tasks cannot set `repo` (both arms start from `pr.repo`), but when a PR only
changes skills or documentation and needs a broken app to diagnose, set
`fixtures: ["../fixtures/broken-app"]` on the task to overlay those files into
each arm's workspace before the initial commit.

Choose the decision `cost_basis` before the run; see [reporting](references/reporting.md).
Incomplete trials retain telemetry and diffs but never enter paired decisions.
Resume preserves them and refuses implicit retries. See [preflight safeguards](references/preflight.md).

## Current CLI compatibility

Before running, read [native CLI contracts](references/compatibility.md) for
completion events, conversation continuity, capability checks, telemetry, native
skill discovery, and host certification. Preserve subscription authentication.

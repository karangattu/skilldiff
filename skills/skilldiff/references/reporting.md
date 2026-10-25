# Reporting rules for the evaluation summary

Read this file before you write the final evaluation summary. It specifies the
API-equivalent cost procedure, the **Evaluation results** table you must
reproduce, and the closing decision your summary must end with.

## API-equivalent cost

Built-in rate tables keyed by full model ID cover Claude, Codex (`gpt-6.*`),
Antigravity (`gemini-*`), and OpenCode (`deepseek-*`), so runs using standard models
need no `pricing:` block. Add `pricing:` rates to override built-in rates or to
price an unmodeled provider; the report includes an **API-equivalent cost** table from
the recorded token breakdown, and regenerating the report reproduces the estimate.
Carry those costs into the final evaluation table in your summary. If the run has
no recorded rates, look up the current per-token prices on the providers' own
pricing pages yourself, multiply them by the token counts in the report, and show
the API-equivalent cost per arm, naming the price source and date next to the
table. Label costs computed with newly looked-up rates as estimates; do not present
them as the run's recorded pricing.

Haiku 5.5 has two prompt-size tiers (up to and over 100K tokens). SkillDiff prices
each request when Claude's stream records per-request usage. Without that usage,
the decision cost is `N/A`; `tier_policy: upper_bound` adds a labelled planning
estimate that the SHIP/DO NOT SHIP calculation ignores. Do not replace an `N/A`
with a high-tier number in the decision.

For Codex runs, if usage includes input and output counts but omits
`cache_write_input_tokens`, calculate totals and API-equivalent cost from the
reported input, cache-read, and output counts, treating the unreported
cache-write component as zero. This keeps the available usage comparable; the
cost excludes any cache-write usage Codex did not report.

## Evaluation results table

At the end of the evaluation summary, reproduce the report's **Evaluation
results** table using these columns in this order:

| **App** | **Arm** | **Score** | **Time** | **Input** | **Cached input** | **Output** | **Total tokens** | **Tool calls** | **Turns** | **Skill loaded** | **API-equivalent cost** |
| ------- | ------- | --------- | -------- | --------- | ---------------- | ---------- | ---------------- | -------------- | --------- | ---------------- | ----------------------- |

Use one row per task, model, and arm, plus a Δ row when both control and
treatment are present, keeping the recorded arm labels and including the
baseline when present. App is the task ID; include the model in the App
cell when multiple models were evaluated. Score is the mean of eligible
graded runs. Time, token counts, tool calls, turns, and API-equivalent cost
are totals across eligible repetitions. With `agent_failure: exclude`,
a failed agent's partial grade and resource use stay in Run details but do
not enter the comparison. With `zero`, an eligible agent failure scores zero while
its resource use remains in the comparison. Harness errors are always excluded.
Cached input includes cache reads and cache writes; total tokens use
`reported_total_tokens` when available, otherwise input, cached input, and output. Skill loaded
is yes/no for one run or loaded/known runs for repetitions, with unknown
runs noted separately. Show missing measurements or unavailable pricing
as `N/A`, never zero, except for the Codex cache-write fallback described
above. Use the saved table directly when available; for
older reports, derive the rows from `results.json` using these same rules.
A multi-turn task shares one conversation and one overall timeout. Unknown
aggregate measurements remain `N/A`. Agent sessions are not automatically retried.
The skill-context tax is a text-size estimate assuming text is carried each
turn, not measured prompt injection or spend.

## Recorded decision cost basis

The evaluation table always labels its API estimate. Claude subscription runs
default to `cost_basis: api-equivalent`; API-key and other-harness runs default to
`harness`. Full Claude model IDs use the built-in table. A run configured with
`api-equivalent` uses its saved rates and measured usage for cost comparisons,
uncertainty, and its verdict across report formats and terminal output. Unknown
usage or rates stay `N/A`. Original harness cost is preserved in arm records.
Results with no basis retain the historical harness interpretation. Looking up
prices after a run can add a labelled estimate to a summary, but must not change
its recorded verdict or masquerade as pre-registered pricing. A run that used a
bare model alias or an unrecognized model is not eligible for a harness-cost
decision; pin the full model ID or use API-equivalent cost.

Discovery preflight establishes skill availability, not adoption. Report
"Skill loaded" from transcript evidence. The report's **Skill adoption and next
steps** section lists, per skill arm, hits/eligible runs, unknown runs, hit rate,
and distinct load/read events, then gives concrete description and scope actions
for missed or over-triggered tasks. A *hit* is an eligible run where the skill
loaded at least once; failed runs and unknown measurements never count as misses.
Adoption is judged on the **decision basis** — the held-out runs when the
experiment declares a held-out split, every run otherwise — and the report, the
saved results, and `diagnose` all judge the same runs. `results.json` and
`diagnose --json` name that basis in `adoption_basis`. When fewer than
two-thirds of measured runs load any skill arm, when the arm never loaded the
skill in any measured run, or when the arm's adoption could not be measured,
`results.json` and `diagnose` set `adoption_failed: true`, **Evaluation
completeness** counts adoption failures, and a would-be SHIP becomes NEEDS MORE
RUNS with the description fix as the next action. A hit rate needs three
measured runs to judge an arm; fewer measured runs is thin evidence, not a pass.
Tasks where every control run scores 100%
are recorded in `non_discriminating_tasks`. Run records also include
`files_changed_normalized` (with `grader_ignore` paths removed and generated
directories such as `.venv` and `node_modules` collapsed) and Run details shows
status, score, and checks before cost, time, turns, and tokens. Incomplete trial
telemetry and diffs are audit evidence; incomplete pairs do not enter this table
or the decision.

## Harness diagnostics

The main report shows denials with their stated reasons, configured-versus-served
models, unrecognized model IDs, and rate-limit retry counts in a **Harness
diagnostics** section. `diagnose` uses the same summary. Treat any denial,
model mismatch, or retry cluster as an evaluation-infrastructure problem before
blaming the skill. A profile can pass the canary and still hit a task-specific
runtime probe; `check` runs those probes through the same boundary.

## Closing decision

The report itself ends with a **Closing decision** table — score, cost, time,
tokens, tool calls, turns, and adoption, each with paired change, 95% CI, and
one plain reading — followed by the bottom line: SHIP, DO NOT SHIP, or
NEEDS MORE RUNS (with ceiling or floor effects noted when tasks cannot discriminate),
plus the structured Bottom Line summary (Verdict, Why, Next step). Low adoption on any skill arm on the decision
basis downgrades a would-be SHIP to NEEDS MORE RUNS and names the description
fix. End your summary
with that same bottom line and reason; do not invent a different verdict from the
one the report computed, and carry the **Skill adoption and next steps** actions
into the summary.

When held-out tasks were planned but have no results, the decision must remain
NEEDS MORE RUNS; development results cannot replace them. Shipping requires at
least five scored pairs, three usable tasks when coverage is known, and a
non-collapsed score interval, even with pre-registered thresholds. Required cost
or token savings each need five usable metric pairs and a percentage-saving
confidence bound that meets the required percentage, not just a favorable mean.

`session_id` identifies one arm's native conversation. The arm's `cli` metadata
and `preflight.json` record version/capability checks in its execution environment.
Missing completion, malformed streams, and explicit failures are harness errors;
their partial evidence remains visible but is excluded from paired decisions.

# Reporting rules for the evaluation summary

Read this file before you write the final evaluation summary. It specifies the
API-equivalent cost procedure, the **Evaluation results** table you must
reproduce, and the closing decision your summary must end with.

## API-equivalent cost

If the run recorded `pricing:` rates (source, date, and per-model rates per 1M
tokens, written to `skilldiff.yaml` before the run), the report includes an
**API-equivalent cost** table — recorded token breakdown per arm × those
rates — and regenerating the report reproduces the estimate exactly. Carry
those costs into the final evaluation table in your summary. If the run has
no recorded rates, look up the current per-token prices on the providers'
own pricing pages yourself, multiply them by the token counts in the report,
and show the API-equivalent cost per arm, naming the price source and date
next to the table. Label costs computed with newly looked-up rates as
estimates; do not present them as the run's recorded pricing.

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
not enter the comparison. With `zero`, a failed agent scores zero while its
resource use remains in the comparison. Cached input includes cache reads and cache
writes; total tokens include input, cached input, and output. Skill loaded
is yes/no for one run or loaded/known runs for repetitions, with unknown
runs noted separately. Show missing measurements or unavailable pricing
as `N/A`, never zero, except for the Codex cache-write fallback described
above. Use the saved table directly when available; for
older reports, derive the rows from `results.json` using these same rules.
A multi-turn task has one overall timeout and reports unknown aggregate
measurements as `N/A`. Agent sessions are not automatically retried.
The skill-context tax is a text-size estimate assuming text is carried each
turn, not measured prompt injection or spend.

## Recorded decision cost basis

The evaluation table always labels its API estimate. The closing decision uses
`cost_basis: harness` by default. A run configured with `api-equivalent` uses
its saved rates and measured usage for cost comparisons, uncertainty, and its
verdict across report formats and terminal output. Unknown usage or rates stay
`N/A`. Original harness cost is preserved in arm records. Results with no basis
retain the historical harness interpretation. Looking up prices after a run
can add a labelled estimate to a summary, but must not change its recorded
verdict or masquerade as pre-registered pricing.

Discovery preflight establishes skill availability, not adoption. Report
"Skill loaded" from transcript evidence. Incomplete trial telemetry and diffs
are audit evidence; incomplete pairs do not enter this table or the decision.

## Closing decision

The report itself ends with a **Closing decision** table — score, cost, time,
tokens, tool calls, turns, and adoption, each with paired change, 95% CI, and
one plain reading — followed by the bottom line: SHIP, DO NOT SHIP, or
NEEDS MORE RUNS, plus one sentence that states why. End your summary with that
same bottom line and reason; do not invent a different verdict from the one the
report computed.

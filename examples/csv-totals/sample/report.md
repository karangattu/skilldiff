# skilldiff: csv-totals

_2026-09-27T120000Z_

> [!NOTE]
> **Verdict**
> No clear task-score effect: **+17 pp**, but the 95% CI (+0 to +33 pp (n=3/4)) includes zero (3/4 graded pairs) (only 3 pair(s) — treat as preliminary; only 1 task(s) — repetitions measure those tasks, not general skill effect).
>
> With the skill, runs cost 16% less, took 17% less time and used 11% fewer tokens, summed over the compared pairs.
>
> Headline and closing decision use held-out data only (3/4 usable score pair(s)). Dev pairs are for iteration and shown separately in By split.
>
> Task coverage: only 2 distinct task(s) across 8 pair(s). Repetitions narrow repetition noise, not task variation — add tasks before generalizing.
>
> Failure policy (pre-registered): agent_failure=exclude, missing=exclude. Grader timeouts/errors are always N/A.

> [!WARNING]
> **Check before trusting this result**
> - 1 of 8 control runs ended with timeout (agent infrastructure failure, not a low score); 1 have N/A scores (grading unavailable).
> - 1 of 8 skill runs have grader error; their scores are N/A and excluded from means. These are evaluation-infra failures, shown with valid-pair counts.

## Summary

| Metric | Control | Skill | Paired mean Δ | 95% CI | Reading |
|:---|---:|---:|---:|---:|:---|
| Task score (mean) | 42% | 81% | +39 pp | +19 to +58 pp (n=6/8) | Skill wins |
| Success | 0/6 | 3/6 | +3 |  | More successes |
| Cost (median) | $0.48 | $0.39 | -$0.09 | -$0.09 to -$0.08 (n=7/8) | Costs less |
| Time (median) | 98s | 82s | -18s | -20s to -17s (n=7/8) | Faster |
| Tokens (median) | 443k | 396k | -58k | -68k to -51k (n=7/8) | Fewer tokens |
| Turns (median) | 15 | 13 | -2.0 | -2.0 to -2.0 (n=7/8) | Fewer turns (early sign) |
| Skill used | 0/7 | 7/8 |  |  | Partial adoption |
| Graded pairs | 6/7 | 6/7 |  | n=6/8 |  |

**Harness:** claude · **Models:** 1 · **Tasks:** 2 · **Runs per arm:** 4

## Evaluation completeness

| Planned pairs | Completed pairs | Usable score pairs | Agent failures | Grader errors | Reading |
|---:|---:|---:|---:|---:|:---|
| 8 | 8 | 6 | 1 | 1 | Partial — 1 agent failure(s), 1 grader error(s), 2 pair(s) ungraded |

Planned pairs = models × tasks × runs per arm. Completed pairs match both arms on (model, task, repetition); usable score pairs are those with eligible graded scores on both sides. Agent failures and grader errors count runs across both arms (agent failures: status other than ok; grader errors: grader timeout or error, scored N/A).

## API-equivalent cost

| Arm | Input | Cache read | Cache write | Output | API-equivalent cost |
|:---|---:|---:|---:|---:|---:|
| Control | 315k | 2.7M | 84k | 45k | $2.73 |
| Skill | 274k | 2.3M | 67k | 38k | $2.34 |
| Change (Skill − Control) | -41k | -340k | -17k | -6.8k | -$0.39 |

Rates in USD per 1M tokens from https://www.anthropic.com/pricing (checked 2026-09-27). `claude-sonnet-5`: input $3.00, output $15.00, cache read $0.30, cache write $3.75. Each paired eligible run's recorded token counts × its model's rates, summed over matched pairs. Unpaired and failed attempts remain in Run details. The run saves rates, source, date, and the token breakdown, so this estimate reproduces from the saved run alone.

## Key takeaways

- **Accuracy.** Control averaged 42% and the skill 81%. Pair by pair, the skill scored higher in 5, lower in 0, and tied in 1 of 6 graded pair(s) (2 ungraded).
- **Efficiency.** With the skill, runs cost 18% less, took 18% less time and used 13% fewer tokens, summed over the compared pairs. Totals: cost $3.31 → $2.71, time 690s → 563s.
- **Adoption.** The agent used the skill in 7 of 8 skill runs; no control run referenced it. Runs where the skill was ignored dilute any effect. A sharper `description` usually fixes this.
- **Checks.** biggest gain `plain decimal output` (fix-total, +100 pp). See By check.
- **Biggest gain:** `fix-total` (+61 pp).
- **Sample size.** 8 pair(s) with 4 repetition(s) per task gives wide error bars. Use `runs: 5` or more as a starting point, not a sufficiency rule — pre-register the run budget before looking at results.
- **Task coverage.** Only 2 distinct task(s): many repetitions narrow repetition noise but still describe only those tasks. Add representative, irrelevant, ambiguous, and regression tasks before generalizing.
- **Grading.** 1 run(s) have grader timeouts/errors; their scores are N/A and excluded from means (valid-pair counts shown). These are infrastructure failures, not agent failures.

## By split

| Split | Control | Skill | Δ score (paired mean) | Better/worse/tie | Δ cost (mean) | Δ time (mean) | Skill used | Reading |
|:---|---:|---:|---:|---:|---:|---:|---:|:---|
| dev | 39% | 100% | +61 pp | 3/0/0 (n=3) | -$0.09 | -20s | 3/4 | Skill wins (early sign) |
| held-out | 44% | 61% | +17 pp | 2/0/1 (n=3) | -$0.08 | -16s | 4/4 | No clear difference (early sign) |

Dev pairs are for iteration; the held-out set is the honest estimate. The headline and closing decision use the held-out set only (3/4 usable score pair(s)).

## By task

| Task | Control | Skill | Δ score (paired mean) | Better/worse/tie | Δ cost (mean) | Δ time (mean) | Skill used | Reading |
|:---|---:|---:|---:|---:|---:|---:|---:|:---|
| fix-total | 39% | 100% | +61 pp | 3/0/0 (n=3) | -$0.09 | -20s | 3/4 | Skill wins (early sign) |
| write-total | 44% | 61% | +17 pp | 2/0/1 (n=3) | -$0.08 | -16s | 4/4 | No clear difference (early sign) |

## By check

Named checks from grader JSON (`{"checks": [{"name": ..., "passed": ...}]}` or `[true, false]`). Δ shows skill minus control in percentage points; W/L/T counts pairs where the skill passed and control failed / vice versa / tied.

| Task | Check | Control | Skill | Δ | Better/worse/tie | Pairs | Reading |
|:---|:---|---:|---:|---:|---:|---:|:---|
| fix-total | correct total | 1/3 | 3/3 | +67 pp | 2/0/1 | n=3 | Helps |
| fix-total | plain decimal output | 0/3 | 3/3 | +100 pp | 3/0/0 | n=3 | Helps |
| fix-total | runs without error | 3/3 | 3/3 | 0 pp | 0/0/3 | n=3 | No difference |
| fix-total | script exists | 3/3 | 3/3 | 0 pp | 0/0/3 | n=3 | No difference |
| fix-total | uses csv module | 0/3 | 3/3 | +100 pp | 3/0/0 | n=3 | Helps |
| fix-total | uses decimal.Decimal | 0/3 | 3/3 | +100 pp | 3/0/0 | n=3 | Helps |
| write-total | correct total | 2/3 | 3/3 | +33 pp | 1/0/2 | n=3 | Helps |
| write-total | plain decimal output | 0/3 | 1/3 | +33 pp | 1/0/2 | n=3 | Helps |
| write-total | runs without error | 3/3 | 3/3 | 0 pp | 0/0/3 | n=3 | No difference |
| write-total | script exists | 3/3 | 3/3 | 0 pp | 0/0/3 | n=3 | No difference |
| write-total | uses csv module | 0/3 | 1/3 | +33 pp | 1/0/2 | n=3 | Helps |
| write-total | uses decimal.Decimal | 0/3 | 0/3 | 0 pp | 0/0/3 | n=3 | No difference |

## Run details

Each pair ran in identical fresh workspaces, in balanced arm order (seed `1234`). Only the skill arm had the skill installed.

| Task | Run | Arm | Status | Score | Checks | Skill used | Cost | Time | Turns | Tokens | Files | Artifacts |
|:---|---:|:---|:---|---:|---:|:---|---:|---:|---:|---:|---:|:---|
| fix-total | 1 | Control | ok | 33% | 2/6 | no | $0.41 | 92s | 13 | 443k | 1 | [transcript](claude-sonnet-5/fix-total/control/001/transcript.txt) · [diff](claude-sonnet-5/fix-total/control/001/diff.patch) |
| fix-total | 1 | Skill | ok | 100% | 6/6 | yes | $0.33 | 73s | 11 | 372k | 1 | [transcript](claude-sonnet-5/fix-total/treatment/001/transcript.txt) · [diff](claude-sonnet-5/fix-total/treatment/001/diff.patch) |
| fix-total | 2 | Control | ok | 33% | 2/6 | no | $0.44 | 96s | 14 | 443k | 1 | [transcript](claude-sonnet-5/fix-total/control/002/transcript.txt) · [diff](claude-sonnet-5/fix-total/control/002/diff.patch) |
| fix-total | 2 | Skill | ok | 100% | 6/6 | no | $0.35 | 76s | 12 | 372k | 1 | [transcript](claude-sonnet-5/fix-total/treatment/002/transcript.txt) · [diff](claude-sonnet-5/fix-total/treatment/002/diff.patch) |
| fix-total | 3 | Control | timeout | N/A (agent failure) | N/A | no | $0.47 | 100s | 15 | 443k | 1 | [transcript](claude-sonnet-5/fix-total/control/003/transcript.txt) · [diff](claude-sonnet-5/fix-total/control/003/diff.patch) |
| fix-total | 3 | Skill | ok | 83% | 5/6 | yes | $0.37 | 79s | 13 | 372k | 1 | [transcript](claude-sonnet-5/fix-total/treatment/003/transcript.txt) · [diff](claude-sonnet-5/fix-total/treatment/003/diff.patch) |
| fix-total | 4 | Control | ok | 50% | 3/6 | no | $0.50 | 104s | 16 | 443k | 1 | [transcript](claude-sonnet-5/fix-total/control/004/transcript.txt) · [diff](claude-sonnet-5/fix-total/control/004/diff.patch) |
| fix-total | 4 | Skill | ok | 100% | 6/6 | yes | $0.39 | 82s | 14 | 372k | 1 | [transcript](claude-sonnet-5/fix-total/treatment/004/transcript.txt) · [diff](claude-sonnet-5/fix-total/treatment/004/diff.patch) |
| write-total | 1 | Control | ok | 50% | 3/6 | no | $0.46 | 95s | 14 | 443k | 1 | [transcript](claude-sonnet-5/write-total/control/001/transcript.txt) · [diff](claude-sonnet-5/write-total/control/001/diff.patch) |
| write-total | 1 | Skill | ok | 83% | 5/6 | yes | $0.38 | 80s | 12 | 396k | 1 | [transcript](claude-sonnet-5/write-total/treatment/001/transcript.txt) · [diff](claude-sonnet-5/write-total/treatment/001/diff.patch) |
| write-total | 2 | Control | ok | 50% | 3/6 | no | $0.48 | 98s | 15 | 443k | 1 | [transcript](claude-sonnet-5/write-total/control/002/transcript.txt) · [diff](claude-sonnet-5/write-total/control/002/diff.patch) |
| write-total | 2 | Skill | ok | N/A (grader error) | N/A | yes | $0.40 | 82s | 13 | 396k | 1 | [transcript](claude-sonnet-5/write-total/treatment/002/transcript.txt) · [diff](claude-sonnet-5/write-total/treatment/002/diff.patch) |
| write-total | 3 | Control | ok | 33% | 2/6 | no | $0.50 | 101s | 16 | 443k | 1 | [transcript](claude-sonnet-5/write-total/control/003/transcript.txt) · [diff](claude-sonnet-5/write-total/control/003/diff.patch) |
| write-total | 3 | Skill | ok | 50% | 3/6 | yes | $0.42 | 84s | 14 | 396k | 1 | [transcript](claude-sonnet-5/write-total/treatment/003/transcript.txt) · [diff](claude-sonnet-5/write-total/treatment/003/diff.patch) |
| write-total | 4 | Control | ok | 50% | 3/6 | no | $0.52 | 104s | 17 | 443k | 1 | [transcript](claude-sonnet-5/write-total/control/004/transcript.txt) · [diff](claude-sonnet-5/write-total/control/004/diff.patch) |
| write-total | 4 | Skill | ok | 50% | 3/6 | yes | $0.44 | 86s | 15 | 396k | 1 | [transcript](claude-sonnet-5/write-total/treatment/004/transcript.txt) · [diff](claude-sonnet-5/write-total/treatment/004/diff.patch) |

## Setup

- **Skill:** `./skills/csv-totals` (names: `csv-totals`)
- **Models:** `claude-sonnet-5`
- **Preset:** `skill`
- **timeout_seconds:** `600`
- **parallel:** `1`
- **Failure policy:** agent_failure=exclude, missing=exclude
- **Seed:** `1234` (balanced arm order)
- **Categories:** `fix-total`=intended, `write-total`=intended
- **skilldiff:** 0.10.0

## How to read this report

- Differences are **skill minus control** as paired-mean changes. Control/Skill columns show means (score) or medians (cost/time/tokens) from completed agent pairs; the Δ and 95% CI measure the paired-mean effect. Adoption includes every eligible skill run. Reading states each row's verdict in plain words and never disagrees with them.
- Success compares only pairs with eligible graded scores and known success outcomes on both sides.
- The 95% CI is a bootstrap interval over paired runs. If it includes zero, the difference could be noise. `n=X/Y` shows valid pairs for that metric.
- Unknown values are **N/A** (ungraded tasks, excluded agent failures, grader timeouts/errors, or missing cost/tokens). Valid-pair counts show how many pairs contributed. Failed attempts retain their raw time, tokens, and partial checks in Run details.
- Tokens include cached input where the harness reports it. Cost is the harness-reported price. On subscription auth the spend is $0 at the margin; the API-equivalent cost section converts the recorded token breakdown with rates from https://www.anthropic.com/pricing (checked 2026-09-27), so regenerated reports reproduce the estimate.
- Checks `N/A` means no named checks; `1/2*` means one check had unknown status.
- *Skill used* comes from the harness's tool calls (Claude) or from references to the skill's files in the transcript (other harnesses).

## Evaluation results

App is the task ID (with the model when multiple models were evaluated). Control and skill scores use pairs graded on both sides. Time, tokens, and cost use matched eligible pairs with those measurements; tool calls and turns use matched eligible agent runs. The Δ row is the second arm minus the first over those same pairs. Excluded and unpaired attempts remain in Run details. Cached input includes cache reads and cache writes. Skill loaded covers all eligible runs, including unpaired runs. A lone arm and the optional baseline show descriptive totals. Tool calls and turns are counted by each harness differently, so compare them within a run, not across harnesses. N/A means a measurement or recorded pricing is missing.

| App | Arm | Score | Time | Input | Cached input | Output | Total tokens | Tool calls | Turns | Skill loaded | API-equivalent cost |
|:---|:---|---:|---:|---:|---:|---:|---:|---:|---:|:---|---:|
| fix-total | Control | 39% | 292s | 135,000 | 1,176,000 | 19,200 | 1,330,200 | N/A | 43 | 0/3 | $1.17 |
| fix-total | Skill | 100% | 231s | 114,000 | 987,000 | 15,600 | 1,116,600 | N/A | 37 | 3/4 | $0.97 |
| fix-total | Δ (Skill - Control) | +61 pp | -61s | -21,000 | -189,000 | -3,600 | -213,600 | N/A | -6 |  | -$0.20 |
| write-total | Control | 44% | 398s | 180,000 | 1,568,000 | 25,600 | 1,773,600 | N/A | 62 | 0/4 | $1.56 |
| write-total | Skill | 61% | 332s | 160,000 | 1,400,000 | 22,400 | 1,582,400 | N/A | 54 | 4/4 | $1.37 |
| write-total | Δ (Skill - Control) | +17 pp | -66s | -20,000 | -168,000 | -3,200 | -191,200 | N/A | -8 |  | -$0.19 |

## Closing decision

Held-out data only (3/4 usable score pair(s)); all-pairs figures are in the Summary table above, and dev results are in By split.

| Metric | Control | Skill | Paired change | 95% CI | Reading |
|:---|---:|---:|---:|---:|:---|
| Task score (mean) | 44% | 61% | +17 pp | +0 to +33 pp (n=3/4) | No clear difference (early sign) |
| Cost (median) | $0.49 | $0.41 | -$0.08 | -$0.08 to -$0.08 (n=4) | Costs less (early sign) |
| Time (median) | 100s | 83s | -16s | -18s to -16s (n=4) | Faster (early sign) |
| Tokens (median) | 443k | 396k | -48k | -48k to -48k (n=4) | Fewer tokens (early sign) |
| Tool calls (median) | N/A | N/A | N/A | n/a (n=0/4) | Unknown |
| Turns (median) | 15.5 | 13.5 | -2.0 | -2.0 to -2.0 (n=4) | Fewer turns (early sign) |
| Adoption (skill used) | 0/4 | 4/4 |  |  | Full adoption |

> [!NOTE]
> **Recommendation: NEEDS MORE RUNS**
> Decision uses the 4 held-out pair(s) only; dev results guide iteration, not validation. No clear task-score effect: +17 pp (95% CI +0 to +33 pp, 3 graded of 4 pairs). The interval includes zero, so the effect is not established. Add repetitions or harder tasks.

# Changelog

All notable changes to this project use this file.
The format follows Keep a Changelog.
This project uses semantic versioning.

## [0.6.0] - 2026-09-26

### Added

- Reading column. Every result table now states each row's verdict in plain words. The column uses the same paired-mean difference and interval as the Δ and CI columns, so the three never disagree. Small samples show "(early sign)". Skill adoption shows Full, Partial, or Not used. Checks show Helps, Hurts, or No difference. Categories show Helps here, Hurts here, Stays out of the way, or Interferes.
- Agent summary shape. The skill prescribes a final table with a Reading column and one bottom line of SHIP, DO NOT SHIP, or NEEDS MORE RUNS.

## [0.5.0] - 2026-09-26

### Changed

- Removed the hardcoded token-pricing table. Costs are harness-reported again. No prices live in this repo and go stale here.
- The skill now prompts the evaluating agent. In its final summary the agent looks up current provider prices on the web, multiplies them by the token counts in the report, and shows the API-equivalent cost per arm with source and date.

## [0.4.0] - 2026-09-26

### Added

- Token-based subscription costs. On subscription auth, cost is recomputed from token counts with a versioned rate table (looked up 2026-09-26). The table shows API-equivalent cost only. Each run stores `cost_basis` and the harness cost. Reports name the pricing version.
- `skilldiff prices` command. Shows the rate table, its date, and its sources in text or JSON.
- `pricing:` config overrides. The evaluating agent checks current provider pages before a full run and overrides stale rates without code changes.
- Pricing provenance. Results record the pricing version, date, and sources. Unknown models keep harness cost and raise a warning.

## [0.3.0] - 2026-09-26

### Added

- Task categories. Each task can set `category: intended`, `irrelevant`, `ambiguous`, or `general`. The report shows adoption and results for each group in By category.
- Practical thresholds. The config can set `acceptable_score_regression_pp`, `required_cost_reduction_pct`, and `meaningful_score_gain_pp`. The verdict then states if the result meets the criteria.
- Provenance. Each run records skill file hashes, prompt hashes, fixture hashes, grader hashes, agent CLI versions, and the skilldiff version.
- Compare command. `skilldiff compare RUN_A RUN_B` shows score changes, adoption changes, efficiency changes, and newly failing or passing checks. It warns if models, tasks, or versions differ.
- By check table. Graders can return named checks. The report shows which checks improve and which checks regress.
- Turns metric. Paired comparison now includes turns with a confidence interval.

### Fixed

- Named checks. Dict checks such as `[{"passed": true}, {"passed": false}]` now show `1/2`. The old code counted each dict as true and showed `2/2`.
- Paired-mean differences. Cost, time, and token rows now show the paired-mean change. The interval describes the same value. Control and Skill columns keep medians as descriptions.
- Unknown values. Tasks with no grader show `N/A`, not `100%`. Grader timeouts and errors show `N/A`, not `0%`. Missing cost, time, and tokens show `N/A`, not zero.
- Valid-pair counts. Each interval shows `n=X/Y`. Means use only pairs with known values.
- Cautious verdicts. The headline flags small samples with `n<5`. It flags collapsed intervals when all pairs give the same difference.
- Run details. Score shows the reason for `N/A`, for example `N/A (ungraded)`. Cost, time, turns, and tokens show `N/A` when the harness gives no value.
- Templates. `skilldiff init` now creates named checks and category fields. `skilldiff check` reports `N/A` for tasks with no grader.

## [0.2.0] - 2026-09-22

- PR evaluations with control and treatment commits.
- Agent skill and Claude Code plugin.
- Runnable demo, `init --skill`, `check`, and `report` commands.
- Random arm order, parallel pairs, skill packs, and contamination checks.
- Paired statistics with confidence intervals and HTML, Markdown, and Quarto reports.
- Support for Claude Code, Codex, OpenCode, and Antigravity.

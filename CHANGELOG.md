# Changelog

All notable changes to this project use this file.
The format follows Keep a Changelog.
This project uses semantic versioning.

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

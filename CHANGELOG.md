# Changelog

All notable changes to this project use this file.
The format follows Keep a Changelog.
This project uses semantic versioning.

## [Unreleased]

### Removed

- The `harnesses:` config key. It was accepted from 0.9.0 but never read by the
  runner, so listing harnesses changed nothing; it now raises instead of being
  silently ignored. Use `harness: <name>` for the single harness a run uses.
  The dormant "By harness" report block, which could never populate because run
  records carry no per-run harness, is removed with it.

## [0.9.0] - 2026-09-28

### Added

- Static skill linter. `skilldiff lint [SKILL_DIR] [--json]` checks `SKILL.md`
  frontmatter (required `name` and `description`, naming rules, description
  length and overly broad trigger phrasing), unclosed code fences, broken
  relative links, and length, with an estimated token count.
- Failure diagnosis. `skilldiff diagnose [RUN_DIR] [--json]` reads a run and
  reports under-triggering on intended tasks, over-triggering on irrelevant
  tasks, score regressions, blast-radius violations, agent failures, and token
  bloat (over 50% more tokens with no score gain), each with a recommendation.
- LLM rubric graders. `grader.type: llm` (alias `rubric`) sends the task prompt,
  rubric, response, and diff to a judge and expects JSON with `score`, `success`,
  and `feedback`. `command:` runs your own judge instead of the built-in path.
- Blast-radius integrity assertions. Tasks can set `allowed_paths` and
  `forbidden_paths`. A run that modifies an out-of-scope file is reported as an
  error (`N/A`) naming the path, never as `0%`, so an out-of-scope edit cannot
  masquerade as a failed solution.
- Multi-turn scripted tasks. `prompts:` lists several turns run in order in the
  same workspace. Tokens, cost, time, and turns are summed across turns, and the
  transcript keeps each turn separate.
- Container isolation. `isolation: docker|podman` (with optional
  `container_image`) runs the agent and graders inside a container with the
  workspace mounted, instead of on the host.
- Skill context tax. Reports show the installed skill's size, its static
  per-turn prompt injection, the cumulative session tax, and the total across
  all runs, estimated from the skill's text and the run's median turns.
- Antigravity token and log capture. `cache_read_tokens` is parsed from the Agy
  usage payload, and the session log is appended to the transcript.
- Closing decision. Reports end with a decision table — score, cost, time, tokens,
  and adoption, each with paired change, 95% CI, and a plain reading — followed by
  one bottom line: SHIP, DO NOT SHIP, or NEEDS MORE RUNS with the reason. The
  same statistics and verdict logic decide it: a CI that includes zero is never
  SHIP, an established regression is never SHIP, and pre-registered thresholds
  must clear by bounds. The terminal summary prints the same recommendation.
- Reproducible API-equivalent costs. `pricing:` in `skilldiff.yaml` records
  per-1M-token rates for each model with source and date before the run. The run
  saves them with its token breakdown, reports price it per arm (input, cache
  read, cache write, output), and regenerating a report reproduces the estimate
  without re-looking up prices. Models without recorded rates are named and
  excluded.
- Evaluation completeness. One row shows planned/completed pairs, usable score
  pairs, agent failures, and grader errors together, so partial results can be
  judged before reading effects.
- Explicit dev/held-out reporting. Tasks carry `split: dev|held-out` or infer it
  from `dev/`/`heldout/` directories (a contradiction refuses). Reports show a
  By split table, and when held-out pairs exist the headline and closing decision
  use them only, so development results cannot stand in for validation.
- A committed portable example (`examples/csv-totals`): a skill, dev and held-out
  tasks, fixtures, a deterministic grader, recorded pricing, and a regenerable
  sample report (`make_sample.py`).
- LICENSE file (MIT, matching `pyproject.toml`).

### Fixed

- Resume now accepts both saved task-ID formats and validates execution settings,
  input hashes, seeds, tool versions, checkpoints, and completed arm artifacts
  before modifying a run. Repetitions can increase; incompatible changes refuse.
- Synthetic PR merge commits are reused on resume when source revisions match.
- Skills and fixtures are snapshotted once per run. Later source edits cannot
  alter later pairs; unreadable inputs and changes during copying stop execution.
- Atomic, synced writes protect metadata, manifests, arm records, checkpoints,
  and results. Persistence failures are surfaced and outstanding agents cancelled.
- Exclusive run locks and unique output directories prevent concurrent writers
  from overwriting experiment state. Missing, corrupt, or incomplete saved pairs
  stop recovery without automatically repeating paid sessions.
- Changes to tracked graders or dependency locks stop a pair before completion.
  Python import and pytest caches are excluded from grader checks.
- The process group is killed only for a validated PID. A missing, non-integer,
  or out-of-range PID no longer raises, so a timeout cannot fail while cleaning
  up its own subprocess.

### Compatibility

- Old reports remain readable, but runs without frozen-input metadata cannot be
  resumed. Run output must be outside skill and fixture input directories.

## [0.8.0] - 2026-09-27

### Fixed

- Resume no longer overwrites previous metadata before validation. Compatibility
  covers skill hashes, task hashes, PR commits and workflow, models, tasks,
  harness, skills, baseline, and preset. Mismatches refuse before any write.
- A/B contamination warnings are mode-aware. Skill A in control and skill B in
  treatment are expected; only wrong revisions, missing skills, and baseline
  skill presence warn or invalidate.
- Equal-score verdicts evaluate practical thresholds. Compression reports
  preserved quality plus proven resource savings instead of skipping the check.

### Added

- Four presets (`skill`, `pr`, `revision`, `compression`) configuring the shared
  runner with clear arm labels and decision criteria. Compression enforces
  identical triggers, records source-size reduction separately from session
  tokens, cost, and time, and adds `required_token_reduction_pct`.
- Balanced three-arm baseline order with `baseline_vs_a` and `baseline_vs_b`
  summaries in results and reports.
- Focused tests for A/B config, baseline behavior, resume refusal, PR workflow
  options, and compression verdicts.

## [0.7.0] - 2026-09-26

### Added

- Skill A/B as a first-class mode. One experiment runs skill A versus skill B on identical fixtures with interleaved execution and paired results (`skill_a`, `skill_b`, `include_baseline`). Reports label Skill A and Skill B. `compare` normalizes efficiency by matched tasks and repetitions instead of totals and gains `--strict`.
- PR workflows. `pr.mode` selects agent effectiveness (`agent`, default) or PR correctness (`correctness`, no agents). `pr.pair` selects `merge-base` versus head or base tip versus a synthetic merge (`base-merge`) for integration testing.
- Resumable, auditable runs. Each arm persists at once, completed pairs checkpoint, and `--resume` reuses only when input hashes match. The seed is saved and arm order is balanced within each task and model. Retries keep their costs.
- Grader validation fixtures. Tasks can set `validation: {good, broken}` so `check` grades untouched, known-good, and deliberately broken workspaces. Strict output validation rejects bad shapes as grader errors.
- Failure policy. `failure_policy: {agent_failure: exclude|zero}` is decided before running and shown in the verdict. Grader timeouts and errors stay `N/A`.
- Held-out tasks and stopping rule in the skill. Tasks must cover intended, representative, irrelevant, ambiguous, and regression cases, split into `dev/` and frozen `heldout/`. The run budget is fixed before looking at results.

### Changed

- Isolation is verified, not assumed. Fixture copies strip `.git` and reject escaping symlinks. `check` reports harness-specific skills, instructions, plugins, and memory. Control contamination marks the run INVALID.
- Graders distinguish test failure from grader failure. Crashes become `error` (`N/A`), never a plain zero. Outside the fixture is documented as not isolation by itself.
- Provenance covers complete inputs. All files are hashed with no silent caps, grader contents and locks join the task hash, and the pre-execution snapshot is reused so edits during a run cannot change later pairs.
- Shipping needs bounds, not points. Practical thresholds require confidence bounds to clear gain and regression limits. Reports separate repetition noise from task coverage and flag few-task evidence.

### Fixed

- Removed leftover token-pricing modules (`skilldiff/pricing.py`, `tests/test_pricing.py`). Costs stay harness-reported.

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

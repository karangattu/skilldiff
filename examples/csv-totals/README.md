# csv-totals — a small, portable skilldiff evaluation

A complete, self-contained example: one skill, two tasks (dev + held-out), one
grader, two tiny fixtures, and a committed sample report. Everything runs
offline except the agent sessions themselves.

```
csv-totals/
├── skilldiff.yaml          # experiment config, incl. recorded pricing rates
├── skills/csv-totals/      # the skill under test
├── tasks/dev/              # fix-total: iterate here (split inferred from path)
├── tasks/heldout/          # write-total: frozen validation (split: held-out)
├── fixtures/               # small workspaces copied fresh into every run
├── graders/                # deterministic grader, outside the fixtures
├── make_sample.py          # regenerates the committed sample report
└── sample/                 # synthetic results.json + report.{md,html,qmd}
```

## What the example demonstrates

- **Honest splits.** `tasks/dev/` is for iteration; `tasks/heldout/` is frozen
  before the full run. The report's **By split** table separates them, and the
  headline and closing decision use held-out pairs only when they exist.
- **Evaluation completeness.** The sample includes one agent timeout and one
  grader error, so the completeness row shows planned/completed pairs, usable
  score pairs, and failure counts together.
- **Reproducible API-equivalent costs.** `pricing:` in `skilldiff.yaml` records
  rates, source, and date before the run; the report prices the saved token
  breakdown and regenerating reproduces the estimate. The rates there are
  illustrative (checked 2026-09-27) — refresh them from the provider's pricing
  page before a real run.
- **Closing decision.** Reports end with score, cost, time, tokens, and
  adoption (paired change, CI, plain reading) plus one bottom line:
  SHIP, DO NOT SHIP, or NEEDS MORE RUNS.

The numbers in `sample/` are synthetic. Do not quote them as findings.

## Run it for real

```bash
skilldiff run -c skilldiff.yaml          # full run (paid agent sessions)
skilldiff run -c skilldiff.yaml --runs 1 # cheap smoke test
skilldiff report sample                  # rebuild reports from sample/results.json
```

`skilldiff run` needs the harness CLI (Claude by default) and network access.
Tasks use `$SKILLDIFF_TASK_DIR` to reach the grader, so the layout is portable;
move the folder anywhere and it still works.

## Regenerate the sample report

```bash
python3 make_sample.py    # with skilldiff installed (uv run python make_sample.py)
```

This rebuilds `sample/` from the synthetic run records in `make_sample.py`
using the same reporter as a real run.

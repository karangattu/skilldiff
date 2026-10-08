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
  page before a real run. This example explicitly keeps `cost_basis: harness`;
  choose `api-equivalent` before a new evaluation if recorded API estimates
  should drive its decision. Historical samples retain their original basis.
- **Closing decision.** Reports end with score, cost, time, tokens,
  tool calls, turns, and adoption (paired change, CI, plain reading) plus one bottom line:
  SHIP, DO NOT SHIP, or NEEDS MORE RUNS.
  The terminal uses the same held-out decision and counts only tasks with usable
  paired scores toward coverage. Historical sample files retain their recorded
  output; regenerate reports in a separate directory to inspect current formatting.

The numbers in `sample/` are synthetic. Do not quote them as findings.

## Run it for real

```bash
skilldiff check -c skilldiff.yaml --scan-home # advisory host exposure check
skilldiff run -c skilldiff.yaml          # full run (paid agent sessions)
skilldiff run -c skilldiff.yaml --runs 1 # cheap smoke test
cp -R sample /tmp/csv-totals-report      # preserve the historical sample
skilldiff report /tmp/csv-totals-report  # inspect the current report format
```

`skilldiff run` needs the harness CLI (Claude by default) and network access.
Tasks use `$SKILLDIFF_TASK_DIR` to reach the grader, so the layout is portable;
move the folder anywhere and it still works.

For local runs, a warning about this example's live skill source is expected.
Read exposure warnings before trusting the result; they do not prove contamination
or mark a run INVALID. Separate workspaces and private temporary directories do
not block host reads. Use container execution or an enforced read sandbox when
the control arm must be unable to read the skill elsewhere. `--scan-home` is
bounded and reports skipped or unreadable paths; no matches cannot certify a
clean host. The committed historical sample retains its original warnings.

## Regenerate the sample report

```bash
python3 make_sample.py    # with skilldiff installed (uv run python make_sample.py)
```

This rebuilds `sample/` from the synthetic run records in `make_sample.py`
using the same reporter as a real run.

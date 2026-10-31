# Transcript audits with Inspect Scout

`skilldiff scan [run-dir]` turns a finished run's agent transcripts into an
[Inspect Scout](https://meridianlabs-ai.github.io/inspect_scout/) transcripts
database and runs skilldiff's scanners over it. It needs the `scout` extra
(`uv tool install 'skilldiff[scout] @ git+https://github.com/karangattu/skilldiff'`).

What it writes under the run directory:

- `scout/db/`: the transcripts database (Parquet). `scout view` and `scout scan`
  read it; so does `skilldiff export --format scout-db`.
- `scout/scans/`: scan results. `scout view <scan dir>` browses them with the
  cited messages.
- `scout/scan-summary.json`: one row per agent run with every scanner's value.

## Scanners

| Scanner | Needs a model | Reads |
|---|---|---|
| `skill_use` | no | Tool calls that load the skill (`Skill`, OpenCode `skill`) or read a file under `skills/<name>/` (`Read`, `read_file`, `view_file`, shell `cat`, Codex `file_read`/`command_execution`), counted only when the tool result came back without an error; `denied` when every such call errored, `unmeasured` when a result never arrived. Cites the messages. |
| `skill_mentions` | no | Grep for the skill's name and directory anywhere in the transcript, labeled `skill_name` and `skill_path`. |
| `skill_influence` | yes (CLI) | Does the work follow the skill's specific instructions? `followed`, `partially_followed`, `ignored`, or `not_applicable`. Runs on both arms: a control that already does what the skill says shows the skill was not needed. |
| `skill_miss_reason` | yes (CLI) | For skill arms: `never_discovered`, `discovered_but_ignored`, `read_but_not_applied`, or `applied` (`not_applicable` for controls). |

The scanners see the SKILL.md text from the run's frozen inputs (truncated at
12000 characters) and the run's recorded `skill_invoked`, so Scout's judgment can
be compared with the harness parser's.

## Reading the summary

The table lists, per run, what the harness parser recorded (`Harness`) next to
what `skill_use` found (`Scout`). The agreement line counts matches. A
disagreement means one of the two is wrong: the harness parser missed a tool
family, counted a denied load, or Scout's export dropped a tool call. Inspect
the cited messages (`scout view`) before trusting either side, and treat the
harness number in `results.json` as the one the shipping decision used.

## Model-judged scanners

`skilldiff scan --llm` judges through the run's own signed-in CLI and model, so
no API key is needed: the judge model id is `skilldiff/<harness>/<model>`, a
provider skilldiff registers with inspect_ai. Each judgment is one headless
session with tools disabled where the CLI allows it (Claude `--tools ""`,
OpenCode and Antigravity reject tool prompts, Codex runs read-only), two at a
time per harness. Pass `--model skilldiff/codex/gpt-6.1-sol` to judge with a
different signed-in CLI, or an `inspect_ai` id such as
`anthropic/claude-sonnet-5-5` to use an API key instead. Judging the run's own
harness with its own model is convenient but not independent; prefer another
CLI or model when the verdict matters.

These scanners use subscription or token budget and are not part of the
shipping decision. Use them to explain adoption numbers (why a skill arm never
loaded the skill) and to see whether the skill's guidance shows in the work.
Scout's [validation sets](https://meridianlabs-ai.github.io/inspect_scout/validation.html)
can check a scanner's labels against hand-graded transcripts.

## Scout's own tooling

```bash
skilldiff export <run-dir> --format scout-db          # database only
scout scan skilldiff/scanners.py -T <run-dir>/scout/db -S skills=<name> \\
  [-S llm=true --model skilldiff/claude/claude-sonnet-5-5 --max-processes 1]
scout view <run-dir>/scout/scans
```

Transcripts carry `task_set` (experiment name), `task_id`, `task_repeat`,
`agent` (harness), `model`, `score`, `success`, `total_time`, `total_tokens`,
and metadata with `arm`, `harness`, `skill_names`, `skill_invoked`,
`skill_available`, `status`, `cost`, `tokens`, and `skill_bodies`, so Scout
filters and custom scanners can select by arm or task.

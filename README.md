# skilldiff

Measure whether your Claude Code skill improves results.

Each task runs twice: without your skill (**control**) and with your skill (**treatment**).
Both runs use the same model, prompt, effort, and input files.

## Install

Requirements: Python 3.10+, Git, and [Claude Code](https://code.claude.com/docs/en/overview) with authentication configured. [Quarto](https://quarto.org/) is optional; when installed, `skilldiff` renders an HTML report automatically.

```bash
git clone https://github.com/karangattu/skilldiff.git
cd skilldiff
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
skilldiff init
```

## Configure your test

Replace `skilldiff.yaml` with this configuration:

```yaml
name: my-skill-test
skill: /absolute/path/to/your/skill  # Directory containing SKILL.md
models:
  - claude-sonnet-5
tasks:
  - ./tasks/*.yaml
runs: 3
claude:
  auth: subscription
  effort: high
  max_budget_usd: 2.00
  permission_mode: acceptEdits
  allowed_tools:
    - Bash(my-tool *)
```

The budget applies to each Claude invocation. One model, one task, and three repetitions produce six invocations.

Subscription authentication is the default. Run `claude auth login` once before the experiment. In subscription mode, `skilldiff` removes `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from each Claude subprocess so an exported API credential cannot silently take precedence over your Claude Code subscription.

To bill an experiment through the Anthropic API instead, set `auth: api_key` and export `ANTHROPIC_API_KEY` before running `skilldiff`:

```yaml
claude:
  auth: api_key
  effort: high
  max_budget_usd: 2.00
```

`acceptEdits` lets unattended Claude sessions edit the disposable control and treatment workspaces. Commands that a skill needs must be explicitly allow-listed with Claude Code tool patterns. For example, a skill that calls `shiny docs` should use `Bash(shiny docs *)`. The same permissions apply to both arms. Avoid `bypassPermissions` unless every task, fixture, and command is trusted, because shell commands can access files outside the temporary workspace.

Replace `tasks/review-auth.yaml` with a task for your skill:

```yaml
id: fix-parser
repo: /absolute/path/to/your/test-project
prompt: |
  Fix the parser so that it accepts empty input.
  Keep all existing tests passing.
grader:
  type: command
  command: python -m pytest -q
```

Use a project with tests that measure the requested result. Install its test dependencies in the active environment.

The runner copies the project into temporary workspaces. Relative `repo` paths resolve from the task file.

## Select Claude models

Set `models` in `skilldiff.yaml`. To compare Sonnet 5 and Opus 5, use:

```yaml
models:
  - claude-sonnet-5
  - claude-opus-5
```

Each model gets its own control and treatment comparison. To test only one model, keep one entry.

For Haiku 4.5, use this model and omit the effort flag:

```yaml
models:
  - claude-haiku-4-5-20251001
claude:
  effort: null
  max_budget_usd: 2.00
```

These are [Anthropic API model IDs](https://platform.claude.com/docs/en/models/overview).
Other providers can require different IDs.

Aliases such as `sonnet` and `opus` also work, but their versions can change.
Use full IDs for repeatable comparisons. See [Claude Code model selection](https://code.claude.com/docs/en/model-config) for provider details.

## Run and read results

```bash
skilldiff run --runs 1       # Check the setup with one pair per model and task
skilldiff run                # Use the repetition count from skilldiff.yaml
skilldiff results            # Show the latest report
skilldiff results --json     # Export the latest report as JSON
```

Each completed run writes:

- `results.json` for programmatic analysis;
- `report.qmd`, a readable Quarto source report;
- `report.html` when Quarto is installed;
- per-run responses, transcripts, diffs, and scores.

The Quarto report starts with an overall verdict, followed by model and task comparison tables. `skilldiff run` and `skilldiff results` print the report path.

For a separate experiment file, run `skilldiff run --config sonnet.yaml`.

Example report (illustrative values):

```text
Metric             Control      Skill     Difference
Task score             67%       100%         +33 pp
Success                2/3        3/3             +1
Median cost          $0.42      $0.48         +$0.06
Median time            95s        88s            -7s
```

The difference is skill minus control. Higher scores are better. Lower cost and time are better.
`pp` means percentage points.

The grader runs inside each workspace. Exit code 0 means success, and a nonzero code means failure.
For partial credit, print JSON such as `{"score": 0.8, "success": true}`, with a score from 0 to 1.
Without a grader, every run receives a passing score.

Results, responses, transcripts, and file changes are saved under `runs/`.
Use several representative tasks before you draw conclusions.

The current runner inherits your Claude configuration. Use a test project and Claude environment without the target skill already installed.
Skill availability does not guarantee that Claude uses it.

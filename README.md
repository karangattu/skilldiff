# skilldiff

**Measure what an Agent Skill changes.**

Same model. Same task. Same tools. Skill off. Skill on.

`skilldiff` compares an agent with and without one skill. It keeps the rest of the experiment fixed.

The result shows whether the skill improves quality, success rate, time, or cost.


## The question

A skill can feel useful without producing better results. A stronger test asks one clear question:

> For the same model and task, what changes after the agent receives the skill?

`skilldiff` gives each task two experimental arms:

```text
CONTROL     The agent does not receive the skill.
TREATMENT   The agent receives the skill.
```

Every other input stays the same:

```text
Same repository
Same starting commit
Same task
Same prompt
Same tools
Same model
Same effort
Same limits
```

This paired design makes the skill the only planned difference.

## Quick start

### 1. Create an experiment

```bash
skilldiff init
```

This command creates an experiment file and a task directory:

```text
skilldiff.yaml
tasks/
```

### 2. Add the skill

Put the skill in the experiment directory. A skill directory starts with `SKILL.md`.

```text
skills/
└── code-review/
    ├── SKILL.md
    └── references/
```

### 3. Add representative tasks

Write tasks that match real work. Do not write tasks that repeat the wording of the skill.

```yaml
# tasks/review-auth.yaml
id: review-auth

prompt: |
  Review this authentication change.
  Report defects that can affect security or reliability.

repo: ./fixtures/auth-service

grader:
  type: command
  command: python grade.py
```

Start with three different tasks. Add more tasks before you use the result for an important decision.

### 4. Select the model and run count

```yaml
# skilldiff.yaml
name: code-review-skill

skill: ./skills/code-review

models:
  - sonnet

tasks:
  - ./tasks/*.yaml

runs: 3

claude:
  effort: high
  max_turns: 30
  max_budget_usd: 2.00
```

Three runs can reveal a large problem. If the results vary, use more runs. Use more runs for a high-cost decision.

### 5. Run both arms

```bash
skilldiff run
```

You provide the model, skill, and tasks once. `skilldiff` creates the control and treatment runs.

### 6. Read the difference

```bash
skilldiff results
```

The report keeps the important numbers together:

```text
code-review-skill

Metric             Control      Skill       Difference
Task score             72%        86%          +14 pp
Success                5/9        8/9              +3
Median cost          $0.42      $0.48          +$0.06
Median time            95s        88s             -7s

Models: 1    Tasks: 3    Runs per arm: 3
```

A higher score can still have unacceptable time or cost.

## Manual method

You can also run the evaluation method manually:

1. Select three or more real tasks.
2. Save one starting commit for each task.
3. Run each task without the skill.
4. Reset the workspace to the saved commit.
5. Run the same task with the skill.
6. Do each pair at least three times.
7. Hide the condition names from the grader.
8. Compare quality, success, time, and cost.

Keep the prompt unchanged. Keep the model, tools, effort, limits, and starting files unchanged.

Use a table for the first manual experiment:

| Task | Run | Control score | Skill score | Difference | Control cost | Skill cost |
|---|---:|---:|---:|---:|---:|---:|
| `review-auth` | 1 |  |  |  |  |  |
| `review-auth` | 2 |  |  |  |  |  |
| `review-auth` | 3 |  |  |  |  |  |

An extreme result can distort the average difference. Examine every run and report the median too.

## What makes a good task?

A useful task has these properties:

- It represents work that the skill claims to improve.
- It starts from a fixed repository state.
- It has a clear result that a grader can measure.
- It does not tell the agent how the skill solves the task.
- It is difficult enough to show a difference.
- It fits within the same time and cost limits for both arms.

Do not use only one task. A skill can improve one example and fail on the next example.

Keep a hidden task set during skill tuning. This set shows whether the skill transfers to new work.

## Grade without knowing the condition

The grader must not know which output used the skill. Condition names can influence human and model graders.

`skilldiff` gives the grader two neutral labels:

```text
candidate-A
candidate-B
```

It randomizes the labels for each pair. It reveals the conditions after the grader records a result.

Remove other clues before grading. These clues include skill names, model names, directory names, and skill calls in transcripts.

Use a command grader for facts that software can measure. Use a blind human or model grader for subjective quality.

## Measure more than quality

A skill can improve one metric and harm another. Record these metrics for each run:

- Task score
- Success or failure
- Duration
- Cost
- Input and output tokens
- Tool calls
- Files changed

Report the control value, treatment value, and difference. Keep the raw run data so that another person can examine the result.

## Compare more than one model

List each model in the same experiment:

```yaml
models:
  - haiku
  - sonnet
  - opus
```

`skilldiff` compares each model only with itself.

```text
haiku:  control vs skill
sonnet: control vs skill
opus:   control vs skill
```

This design answers a second question: does the skill help weaker and stronger models by the same amount?

`skilldiff` rejects an experiment that changes the model between the two arms.

## Keep every run

Each model, task, arm, and repetition becomes one immutable run:

```text
runs/
└── 2026-09-22T142500Z/
    ├── experiment.json
    ├── sonnet/
    │   └── review-auth/
    │       ├── control/
    │       │   ├── 001/
    │       │   ├── 002/
    │       │   └── 003/
    │       └── treatment/
    │           ├── 001/
    │           ├── 002/
    │           └── 003/
    └── results.json
```

Each run records the prompt, response, transcript, file changes, artifact, score, cost, duration, and environment details.

This record makes failed runs visible. It also makes the experiment reproducible.

## Experimental rules

`skilldiff` follows five rules:

1. Change one planned variable: the skill.
2. Pair control and treatment runs from the same starting state.
3. Repeat runs because agent output varies.
4. Blind the grader before each comparison.
5. Save the raw evidence before the summary.

If another input changes, start a new experiment.

## Scope

`skilldiff` does four things:

```text
Read an experiment file
Create identical isolated workspaces
Run each task with the skill off and on
Save and compare the paired results
```

`skilldiff` orchestrates [Claude Code](https://docs.anthropic.com/en/docs/claude-code/cli-reference). It does not replace Claude Code.

## Why the name?

A code diff shows what changed in a repository. `skilldiff` shows what changed after an agent received a skill.

The best result is not always a positive score. A result of zero can save you from shipping a skill that only feels useful.

See the [Claude Code skill guide](https://code.claude.com/docs/en/skills) for the `SKILL.md` format.

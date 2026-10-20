# Explicit versus implicit skill use

By default the task prompt never names the skill. The agent has to notice the
skill on its own, so the adoption rate measures **discovery**: whether the
SKILL.md description triggers on the task wording.

Set `skill_invocation: explicit` to name the skill in the prompt instead:

```yaml
skill_invocation: explicit          # implicit (default) | explicit
skill_invocation_prompt: "Use the {skill} skill for this task."   # optional
```

The default instruction is "Use the `shiny-doctor` skill for this task." for one
skill, and "Use the skills `a`, `b` for this task." for a skill pack. A custom
`skill_invocation_prompt` replaces it; `{skill}` becomes the arm's skill name(s).
Only arms that actually carry the skill get the instruction, so a control arm
without the skill is never told to use one.

A task file can override the experiment setting, which lets you test one wording
on one task:

```yaml
skill_invocation: explicit          # in a task file
```

`skilldiff init --skill-invocation implicit|explicit` scaffolds a config with the
chosen mode. The flag only sets the key; edit the file to change the wording.

## What the two modes mean

| Mode | Prompt | Adoption measures | Fix the problem in |
|:--|:--|:--|:--|
| `implicit` | never names the skill | discovery: does the description trigger? | the SKILL.md `description` |
| `explicit` | names the skill | obedience: does the agent use what it is told? | the harness or the skill body |

Run the same experiment both ways and compare. The gap between the two adoption
rates is what the description is worth:

- **Low implicit, high explicit.** The skill works, but nobody finds it. The
  `description` does not match how users ask for the task. Fix the description.
- **Low explicit.** The agent does not use a skill it was told to use. The
  harness may not expose the skill, or the body may not be worth reading. Fix
  the harness or the body, not the wording.
- **High implicit.** The description already triggers. Nothing to fix.

## What the reports say

In explicit mode the report and `skilldiff diagnose` change how they read the
numbers:

- The arm is labelled "Skill (named)" in every table.
- The adoption section states that the numbers measure obedience, not discovery,
  and points at the implicit run for the discovery number.
- The next steps stop advising you to sharpen the `description`. The advice
  becomes "check that the harness exposes the skill and read a transcript".
- Over-triggering is not measurable in explicit mode: the prompt told the agent
  to use the skill, so loading it on an irrelevant task is expected. Measure
  over-triggering in an implicit run.

`skilldiff compare RUN_A RUN_B` prints the invocation mode of each run and warns
when the modes differ, because the adoption difference then mixes obedience with
discovery.

`results.json` records the mode in `skill_invocation` and in
`settings.skill_invocation`, so a saved run says how it was measured.

## Rules the config enforces

- PR runs reject `explicit`; they compare revisions, not skill use.
- A/B runs reject `explicit` when the two revisions have different skill names,
  because the arms would get different instructions. Give both revisions the
  same name, or set `skill_invocation_prompt` to an instruction that does not
  name the skill ("Use the available skill for this task.").
- A task file that sets `skill_invocation: explicit` triggers the same A/B rule.

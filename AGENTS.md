# Git branch naming

Never create or rename a Git branch to a name that starts with `codex`, including
`codex/`. Use descriptive branch names without that prefix.

# Development environment

Use the uv project environment for all Python commands. From the repository root:

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev python -m pytest
uv run --frozen --extra dev ruff check skilldiff tests
```

Keep `--extra dev` on test and lint commands: these tools are optional development
dependencies. Bare `python -m pytest` uses the machine's default interpreter unless
the project environment is active; activation does not persist between tool calls.
Check `uv run --frozen --extra dev python -m pytest --version` if a harness reports
that pytest is missing.

# Completing user-facing changes

Before editing, identify every path that produces the behavior the user requested.
For evaluation output, inspect the CLI, report renderers, `skills/skilldiff/SKILL.md`,
README, and relevant examples. Check each for needed changes; do not assume the
renderer is the whole feature. Update examples when their documented format changes;
keep historical evaluation results intact.

When a skill specifies a format that code also generates, keep both consistent and
add or update a meaningful regression check for that contract.

Before declaring completion or committing:

- Compare the final diff against the user's requested outcome, including skill
  instructions and documentation. Passing tests alone does not establish coverage.
- Inspect a representative user-visible result and run the checks relevant to the
  changed behavior. Do not claim an unverified result.
- State any incomplete scope or unverified behavior explicitly. Do not call the
  request complete while required work remains.

When the user points out an omission, check adjacent paths for the same omission
before declaring the correction complete.

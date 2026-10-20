# py-shiny Troubleshooting Benchmark Pack

A reusable `skilldiff` evaluation pack for Python Shiny (`py-shiny`) diagnostic and repair skills (such as `shiny-doctor`).

## Included tasks

### Dev split (`tasks/dev/`)
- `stale-state`: In-place list mutation on a `reactive.value` fails to trigger reactive dependents.
- `id-mismatch`: Mismatched input and output IDs between UI declarations and `@render` server functions.
- `global-state-leak`: Module-level mutable list leaks state across user sessions instead of using per-session `reactive.value`.

### Held-out split (`tasks/heldout/`)
- `blocking-work`: Repeated expensive calculation inside multiple `@render` outputs instead of a shared `@reactive.calc`.
- `express-core-mix`: Mixing `shiny.express` imports/decorators with `shiny.App(app_ui, server)` Core structure.
- `module-namespace-collision`: Duplicate component instances colliding on global IDs instead of `@module.ui` / `@module.server`.

Each task ships with:
- A broken starting fixture under `fixtures/<task>/`
- At least two distinct known-good reference solutions and a deliberately broken solution under `solutions/<task>/`
- A deterministic AST + structural grader under `graders/`
- `grader_ignore: python-tooling` and `allowed_paths: ["app.py"]`

## Usage

```bash
skilldiff check -c eval/py-shiny-troubleshooting/skilldiff.yaml --no-probe --fail-on-ceiling
skilldiff run -c eval/py-shiny-troubleshooting/skilldiff.yaml --split dev --runs 1 --terminate-on-ceiling
```

The module task also includes a valid solution with single-quoted namespace IDs
and a consistently renamed output. Its structural grader checks distinct module
UI mounts from the Python syntax tree, accepting equivalent ID names and ignoring
comments. These graders check static structure; use runtime/browser checks before
claiming that every app behavior works. Historical evaluation copies are unchanged.

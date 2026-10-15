---
name: shiny-doctor
description: Diagnose and fix Python Shiny (py-shiny) reactivity, ID mismatch, session state, blocking computation, Express/Core mixing, and module namespace bugs. Use whenever a Shiny for Python app fails to update, leaks state across sessions, mixes shiny.express with shiny.App, or has broken module inputs/outputs.
---

# Shiny Doctor — Python Shiny Troubleshooting

Check these six failure modes in order when diagnosing a broken Python Shiny app:

1. **In-place reactive state mutation (`stale-state`)**:
   - `reactive.value` / `reactive.Value` uses identity/equality checks. Mutating a list or dict in place (`items().append(x)`) does not invalidate dependents.
   - Fix: assign a new copy (`items.set([*items(), x])`).

2. **UI / server ID mismatch (`id-mismatch`)**:
   - Every `@render.*` function name must match the exact `id` passed to `ui.output_*`, and every `input.<id>()` call must match `ui.input_*`.
   - Fix: align the input and output identifiers across UI and server.

3. **Module-level mutable state (`global-state-leak`)**:
   - Module-level lists/dicts live across all user connections.
   - Fix: move mutable state inside the `server` function using `reactive.value(...)`.

4. **Redundant synchronous blocking work (`blocking-work`)**:
   - Running expensive work directly inside multiple `@render.*` functions recomputes it per output and blocks the loop.
   - Fix: factor shared computation into a `@reactive.calc` function so outputs share one cached reactive calculation per invalidation.

5. **Mixing Shiny Express and Core (`express-core-mix`)**:
   - `from shiny.express import ui, render` cannot be combined with `app = App(app_ui, server)` in the same file.
   - Fix: use pure Shiny Core (`from shiny import App, reactive, render, ui`) when `App(app_ui, server)` is used.

6. **Module namespace collisions (`module-namespace-collision`)**:
   - Child components instantiated multiple times must use `@module.ui` and `@module.server` rather than hardcoded global IDs.
   - Fix: wrap the reusable UI and server logic in `@module.ui` and `@module.server` and mount each instance with its own namespace ID.

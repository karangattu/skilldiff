# Harnesses

Per-agent setup, defaults, and what `check` can and cannot verify.

| Harness | Login | Scaffold model | Skill isolation by default | `check` verifies |
|---|---|---|---|---|
| `claude` | `claude` (subscription) | `claude-sonnet-5-5` | project/local settings only; user skills can still load | login, served model, sandbox canary, `runtime_probe` in Claude's Bash, skills listed at init |
| `codex` | `codex login` | `gpt-6.1-sol` | private `CODEX_HOME` with only `auth.json`; `~/.agents/skills` still readable | login, native `skills/list` discovery in both arms, `runtime_probe` |
| `opencode` | `opencode auth login` | `deepseek-v4-pro` | private config, data, cache, and state dirs; `~/.agents/skills` still readable | login, `runtime_probe`; no skill availability signal |
| `antigravity` | `agy` (sign in) | `gemini-3.8-flash` | none without `isolation: macos`; login lives in the keychain | login, `runtime_probe`; no skill availability signal |

Where no availability signal exists (`opencode`, `antigravity`), `check` fails
on a user-level copy of the target skill for local runs instead of warning,
because the no-skill control would load it. `isolation: macos` (any harness) or
`docker`/`podman` blocks those host reads. See [preflight safeguards](preflight.md)
for what each boundary keeps reachable.

## One experiment on several agents

Keep one config and override the harness and model per run, then line the runs up:

```bash
skilldiff run -c ./skill-eval/skilldiff.yaml --split dev --runs 1 --harness codex --model gpt-6.1-sol
skilldiff run -c ./skill-eval/skilldiff.yaml --split dev --runs 1 --harness opencode --model deepseek-v4-pro
skilldiff run -c ./skill-eval/skilldiff.yaml --split dev --runs 1 --harness antigravity --model gemini-3.8-flash
skilldiff summary -c ./skill-eval/skilldiff.yaml
```

`--harness` needs `--model` when it differs from the config, and works on `check`
too. `summary` prints one row per run with its own decision; it never pools runs,
and it warns when task inputs, skill revisions, or cost bases differ. Compare
deltas and adoption across rows, not tool calls or turns, which each harness
counts differently. Run the CLIs the user is signed in to; a missing login fails
`check` before any paid session.

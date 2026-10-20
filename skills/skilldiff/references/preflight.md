# Preflight and trial safeguards

For Codex, workspace discovery probes use `app-server skills/list` without a
model call. Both arms are checked before the first paid session: the treatment
must discover its own installed skill, and control must not discover the target.
Availability and actual loading are separate measurements. Custom wrappers must
support `app-server --stdio`; disable `codex.verify_skills` only if discovery is
unsupported and explicitly disclose that availability was not verified.

On macOS, use `isolation: macos` with Codex or Claude and specific `codex.read_paths`
or `claude.read_paths` for external runtimes or editable packages. Native discovery
and agent execution share the sandbox policy; a sibling-read probe verifies the boundary.
Host user files and temporary siblings are blocked; own workspace and scratch are usable.
Pytest may list the common session directory and stat its immediate entries;
that does not permit reading sibling file contents. Package metadata may stat
bundled skill files in declared runtime roots, while their contents and directory
listings remain blocked. Declared uv runtime aliases work when Python is launched
through a shell; declare both the virtual environment and its Python runtime.
Target sources, conventional grader/solution/run directories, host skills and
plugins, and skills bundled in allowed runtimes remain blocked. Native sessions
use private `CODEX_HOME` with only the login's `auth.json` copied; host config,
sessions, and plugins are omitted. Graders remain on the host. Review allowed
roots; OS library paths remain readable and networking is not isolated. Use
Docker/Podman for a complete filesystem boundary or non-macOS execution.

Under `isolation: macos`, Codex runs with `--dangerously-bypass-approvals-and-sandbox`
and Claude with `bypassPermissions` (when `claude.sandbox` is true). Their own
seatbelt sandboxes fail inside another sandbox (`sandbox_apply: Operation not
permitted`), so every command would otherwise fail while the session still ends
`ok`. Containers bypass Codex's and Claude's internal sandboxes the same way.

Add a task `runtime_probe:` for imports, versions, or a short browser launch
under the actual agent execution boundary. It has a 30-second limit and runs
before any model session. Validate the grader separately on the host for native
runs. Grader validation retains feedback in `runs/preflight/<task>/grader.json`;
fix missing browser/runtime dependencies before running agents.
Failed probes retain both stdout and stderr. A permission error on stderr fails
the probe even when its command exits zero. Probe the workflow you will use, not
only imports: run pytest on a temporary test and launch a browser if the task
depends on one.

Native macOS sessions set private `TMPPREFIX` as well as `TMPDIR`/`TMP`/`TEMP`,
so zsh heredocs can create their temporary files. Git uses no global/system
configuration or host ignore file by default, and uses the command-line-tools
Git instead of Apple's cache-writing launcher when available. Pytest's default
cache is placed in session scratch; an explicit `PYTEST_ADDOPTS` takes precedence.
Codex's throwaway, mode-600 `config.toml` forwards configured `env:` plus the
managed runtime variables to its shell commands. Values are not put in CLI
arguments or written to host Codex settings. Explicit Codex `extra_args` can
still override these defaults. This uses the documented
[shell environment policy](https://learn.chatgpt.com/docs/config-file/config-reference).

For a dependency-free Python task, this probe checks temporary test collection
inside the same boundary as the agent (pytest must already be in the selected environment):

```yaml
runtime_probe: |
  python - <<'PY'
  import pathlib, tempfile, pytest
  with tempfile.TemporaryDirectory() as directory:
      test = pathlib.Path(directory) / 'test_runtime.py'
      test.write_text('def test_runtime(): assert 2 + 2 == 4\n')
      raise SystemExit(pytest.main(['-q', str(test)]))
  PY
```

For browser tasks, replace that assertion with a browser fixture or launch. Do
not disable plugin loading to hide missing runtime access; confirm the required
plugins actually run. Keep runtime roots precise rather than allowing HOME.

`skilldiff check --scan-home` matches directory/frontmatter names or identical
`SKILL.md` contents. Scans stop after 20000 entries, 5 seconds, or 20 matches,
report skipped/unreadable paths, exclude `.git`, `.venv`, `venv`, `node_modules`,
`__pycache__`, and `.cache`, skip files over 1 MiB, and do not follow symlinks.
No matches cannot certify a clean host. Host checks are skipped for agent
containers, whose image must also be clean.

Container execution needs a locally available image containing the harness CLI,
graders, and dependencies (defaults to `ghcr.io/karangattu/skilldiff-sandbox:latest`,
built from `docker/Dockerfile` with Python, `uv`, Node.js, Playwright Chromium,
`claude`, `codex`, `opencode`, `agy`/`antigravity`, and `/opt/venv` outside
`/workspace`, or falls back to `python:3.11`). `bin_path` refers to an executable
inside that image. Container runs stage only the required subscription login
credentials into the session scratch directory (or forward configured API keys);
host skill, plugin, and session directories are not mounted. Run `skilldiff check` first (pass
`--fail-on-ceiling` to fail tasks whose untouched fixture already scores 100%).
During runs, `--terminate-on-ceiling` (or `terminate_on_ceiling: true`) skips
remaining repetitions for tasks where control scores 100%, `--max-turns` and
`--max-tool-budget` cap per-session turns and tool calls, `env:` forwards warm
environment variables (`PYTHONPATH`, `PYTHONDONTWRITEBYTECODE`) into agents,
probes, and graders, and `--ci` enforces strict exit codes. Resume also
requires the same isolation mode and immutable container image identity.
An explicit YAML `parallel` value, including `1`, overrides automatic pair
parallelism. `arm_parallel: false` separately disables simultaneous arms.
When concurrency is omitted, real agent runs may use automatic workers.

## Trial evidence and cost decisions

Choose `cost_basis: harness` (default) or `api-equivalent` before the run.
API-equivalent decisions require recorded `pricing:` rates and measured token
categories; missing usage or rates stay unknown. Never change the basis after
seeing results to improve the verdict. Historical results with no basis retain
harness cost, and saved harness telemetry is not overwritten.

Each returned arm immediately publishes telemetry, response, transcript, and
diff before its partner or grader finishes. Interrupted arms have explicit
partial records, with unknown spend marked unknown. Incomplete trials are
reported separately and excluded from paired decisions. Resume refuses an
incomplete pair and preserves evidence; start a new run for an explicit retry.
POSIX cleanup tracks observed child processes by PID and launch time, including
servers that create new sessions. Never kill unrelated servers by name or port.
Very fast reparenting can escape observation; use containers for stronger cleanup.

For setup examples and native boundary limits, read the [SkillDiff reference](https://github.com/karangattu/skilldiff/blob/main/docs/reference.md#harness-setup-and-commands).

## Permissions in agent sessions

Nobody can answer a permission prompt in a headless session. Anything that would
ask is refused, and the session still ends `ok`. The defaults avoid prompts:
Codex also records filesystem errors inside failed command results, even when
the overall session completes. Successful output merely quoting an error does
not count. Reports and `diagnose` expose these denials; inspect them before
interpreting time, tokens, or scores. Connection failures keep the underlying
stderr cause in the error summary, with credentials redacted. They remain
harness failures; SkillDiff does not silently retry them or give them zero scores.

| Harness | Default | When the tasks need the network |
|---|---|---|
| `claude` | `sandbox: true`: Bash runs in Claude's own sandbox without prompts | `allowed_domains: ["pypi.org", ...]` |
| `codex` | `sandbox: workspace-write`, no prompts | `network_access: true` |
| `opencode` | `dangerously_skip_permissions: true` (passes `--auto`) | already allowed |
| `antigravity` | `dangerously_skip_permissions: true` | already allowed |

Claude's sandbox keeps commands, but not its Read tool, away from grader and skill
sources. OpenCode and Antigravity do not confine commands. Use `isolation: docker`/`podman`
(or `macos` for Claude and Codex) when reads outside the workspace must be blocked.
Codex single-turn sessions require `--ephemeral`; multi-turn sessions require an
ephemeral app-server thread, so they do not feed memories that a later control
session could read. Unsupported interfaces fail before paid work.

## Host sandbox and permission checks

`check` prints one line per harness that says what sessions may do without asking.
It fails with `host:` when the process running skilldiff cannot reach the network
(`CODEX_SANDBOX_NETWORK_DISABLED`) or cannot write the harness's state directory
(`~/.claude`, `$CODEX_HOME`, `~/.local/share/opencode`, `~/.gemini`). Both usually
mean the agent that started skilldiff is sandboxing it. `run` refuses to start in
that state. The `fix:` line names the setting for the agent that started skilldiff.
Variables that tie a process to that agent's session are removed from every child CLI.


## Native CLI contract

`check --no-probe` skips paid Claude authentication probes but still validates
versions, commands, and structured formats without credentials. `run` performs
these checks in every arm's selected execution boundary before the first paid
session. No legacy flag-removal retry is allowed. Codex discovery checks the
actual app-server skill list; Claude initialization reports target availability.
AGY native global and plugin directories are scanned as advisory exposure only.
OpenCode `--standalone` selects a private server, not a clean configuration profile.
OpenCode V2 logs in with `opencode auth login` and uses ordered `permissions` rules
with actions such as `shell` and `skill`. Claude's settings-source restriction does
not disable all global skills/plugins; `--bare` disables subscription credentials
and is unsuitable as the default.

## Running from inside an agent

SkillDiff starts separate, non-interactive agent
sessions (`claude -p`, `codex exec`, `opencode run`, `agy -p`) that need network
access and write access to their login directory. Codex scripted conversations
use `codex app-server` instead of `exec`.

- Run `skilldiff` as a plain command (paths via `-c`; no `cd`, pipes, redirects, or
  `$(...)`) so the user's allow rule matches it.
- If `check` shows `FAIL host: ...`, your shell is sandboxed and every session would
  fail. Don't retry and don't change the user's agent settings yourself. Show the
  user the `fix:` line, or give them the exact `skilldiff run ...` command for their
  own terminal. Read the results afterwards with `skilldiff results`.
- A full run usually takes longer than your shell tool's timeout. Start
  `skilldiff run -c skill-eval/skilldiff.yaml` with your shell tool's background
  option, check its progress output now and then, and read `skilldiff results` when
  it ends.
- For Claude, `check` makes one tiny authenticated call (`--no-probe` skips it) to
  catch an expired login.
- If `check` reports that the harness CLI is missing, or a smoke run fails with "Not
  logged in", ask the user to sign in with that harness's normal login (for example
  `claude auth login` or `opencode auth login`). Never ask for or handle their
  credentials yourself. A missing binary can also be set per harness with `bin_path`
  or `CLAUDE_BIN`, `CODEX_BIN`, `OPENCODE_BIN`, `AGY_BIN`.


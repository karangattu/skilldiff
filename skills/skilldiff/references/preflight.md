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
Target sources, conventional grader/solution/run directories, host skills and
plugins, and skills bundled in allowed runtimes remain blocked. Native sessions
use private `CODEX_HOME` with only the login's `auth.json` copied; host config,
sessions, and plugins are omitted. Graders remain on the host. Review allowed
roots; OS library paths remain readable and networking is not isolated. Use
Docker/Podman for a complete filesystem boundary or non-macOS execution.

Add a task `runtime_probe:` for imports, versions, or a short browser launch
under the actual agent execution boundary. It has a 30-second limit and runs
before any model session. Validate the grader separately on the host for native
runs. Grader validation retains feedback in `runs/preflight/<task>/grader.json`;
fix missing browser/runtime dependencies before running agents.


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

For setup examples and native boundary limits, read the repository README.

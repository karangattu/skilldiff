# Current CLI compatibility

SkillDiff targets current releases. It checks required CLI capabilities inside the
selected execution environment before paid sessions. Versions and flags are saved
in each arm's `preflight.json` and `cli` metadata. Rejected flags fail the run;
SkillDiff never retries by removing permissions, isolation, or structured output.

| Agent | Treatment directory | Conversation | Required successful completion |
|---|---|---|---|
| Claude Code | `.claude/skills` | Streaming input/output, no session persistence | `type: result`, without an error |
| Codex | `.agents/skills` | One ephemeral app-server thread for multiple prompts | `turn.completed` for exec; `turn/completed` with status `completed` for app-server |
| Antigravity CLI (AGY) | `.agents/skills` | Streaming input/output | `event: result` with status `SUCCESS` |
| OpenCode V2 | `.opencode/skills` | Unique explicit session ID, standalone server | `step_finish` with reason `stop` (or session export with `finish: stop` and `outcome: succeeded`) |

Every arm has a separate conversation and private temporary directory. Follow-up
prompts share their arm's workspace and one deadline. A failed turn stops the
remaining prompts. Partial transcripts and usage survive failures and timeouts.
Missing completion, malformed output, or explicit failures are harness errors,
including when the CLI exits zero; these attempts never enter paired decisions.

Claude usage and model-turn counts are per result, while its reported cost is
cumulative. AGY result usage and Codex app-server total usage are cumulative.
OpenCode completed steps are counted once. Cached input is normalized without
adding a cached subset twice. `reported_total_tokens` retains a native total even
when detailed categories are missing or inconsistent. Unknown categories and
cost remain `N/A`; API-equivalent costs require complete categories and rates.
Saved results without the new optional fields remain readable.

## Discovery and authentication

Each treatment skill is copied once into the native workspace directory above.
Confirmed discovery or successful loading of the target skill in a no-skill
control is an error. Filesystem exposure scans remain advisory. Codex uses the
native `skills/list` probe; Claude initialization can report available skills.
Adoption counts distinct successful loads or reads of the target skill only:
denied calls and references to other installed skills do not count. A skill call
whose result never arrives is not a hit either: the run is recorded as unmeasured
(`skill_invoked: null`) so it is excluded from the hit rate instead of being
counted as a miss or as a hit. OpenCode
adoption requires a successful target skill load or successful skill-file read,
rather than an arbitrary matching tool input.

AGY's global skills live in `~/.gemini/antigravity-cli/skills`; plugins can provide
skills under `~/.gemini/antigravity-cli/plugins/*/skills`. Install the SkillDiff
skill with `npx skills add karangattu/skilldiff -g -y --copy -a antigravity-cli`.
The desktop Antigravity installer target is separate. AGY has no SkillDiff
`isolate` option.

Subscription authentication is preserved. Claude's project/local settings
selection is not a blanket exclusion of global skills, plugins, or instructions.
`--bare` disables subscription credential discovery and skill context and is not
used by default. OpenCode V2 uses `opencode auth login` and ordered `permissions`
rules with actions such as `shell` and `skill`. Its standalone server still reads
the user's applicable configuration. Use an enforced sandbox/container when
filesystem confidentiality is required.

## Verification

Tests exercise invalid output, explicit failures, denied loads, usage accounting,
context continuity, parallel sessions, deadlines, and cleanup. Sanitized native
transcripts are in `tests/fixtures/cli`. CI installs current official CLIs and runs
credential-free capability probes; Codex also exercises native skill discovery
and schema generation. These probes do not certify authenticated model execution.

On macOS, one dev-only control/treatment pair per installed CLI was attempted on
2026-10-12, using normal subscription authentication and two prompts that recalled
a code from the first turn:

| CLI | Version | Observed result |
|---|---|---|
| Claude Code | 2.1.290 | Both arms completed and recalled the code; treatment loaded the skill. |
| Codex | 0.160.1 | Both ephemeral threads completed and recalled the code; treatment loaded the skill. |
| AGY | 1.3.1 | Both conversations completed and recalled the code; treatment read the skill. One result's cached input exceeded its input counter, so uncached input is `N/A`; its reported total is retained. |
| OpenCode | v2.0.23 | Both turns completed via session export fallback; treatment loaded the skill. |

Paid container sessions have not been verified. A passing dev smoke is a transport
check, not evidence for a skill shipping verdict. Keep held-out evaluation policy
and historical saved results unchanged. Recheck authenticated pairs after CLI
upgrades, especially OpenCode releases that fix final-event delivery.

## Primary contracts

- [Claude headless mode](https://code.claude.com/docs/en/headless) and [streaming input](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode).
- [Codex noninteractive mode](https://learn.chatgpt.com/docs/non-interactive-mode) and [app-server](https://learn.chatgpt.com/docs/app-server).
- [AGY headless events](https://antigravity.google/docs/cli/headless/) and [skill locations](https://antigravity.google/docs/skills/).
- [OpenCode V2 commands](https://opencode.ai/v2/docs/cli/commands/), [skills](https://opencode.ai/v2/docs/skills/), and [permissions](https://opencode.ai/v2/docs/permissions/).
- [Installer agent definitions](https://github.com/vercel-labs/skills/blob/main/src/agents.ts).

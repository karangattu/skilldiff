## Current CLI compatibility

Require the current native commands and structured formats before paid work, in
the selected local, native sandbox, or container boundary. Record CLI versions and
capabilities. Do not retry by removing required flags or structured output.
Require a native successful completion event for every turn; empty, malformed,
failed, or unfinished streams are harness errors, even on exit zero. Preserve
partial telemetry but exclude harness errors from paired decisions. Unknown cost
and usage stay `N/A`; use `reported_total_tokens` when the breakdown is incomplete.

Install each treatment skill once: `.claude/skills` for Claude, `.agents/skills`
for Codex and AGY, and `.opencode/skills` for OpenCode. For AGY's global SkillDiff
installation use `npx skills add karangattu/skilldiff -g -y --copy -a antigravity-cli`.
Native global skills are in `~/.gemini/antigravity-cli/skills`
and plugins can also supply skills. AGY has no `isolate: true` option. Treat
filesystem exposure scans as advisory; confirmed target discovery or successful
loading in control invalidates the baseline. Count adoption only from successful
target-skill loads or reads, not denied calls or other installed skills.
Claude's project/local settings selection is not proof that global skills are
absent. Preserve subscription authentication; do not select `--bare` as the
default.

Use credential-free CLI probes and sanitized transcripts for regression checks.
Before certifying end-to-end support on a host, verify a dev-only control/treatment
pair with genuine follow-up context, distinct session IDs, and treatment adoption.
Report any failed or unverified host explicitly. Do not modify historical results.

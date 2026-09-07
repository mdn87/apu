# M13 provider-neutral `apu-wtf` implementation prompt

Recovered verbatim from Claude Code transcript
`05a17f11-6cfb-46eb-9fb2-6ca95025439c.jsonl`, assistant record
`c2582784-11d5-4d6a-bcc2-8ee0e4a5705b` on 2026-09-07. The implementation
validated the prompt against the real transcript before relying on it.

Three format claims in the original prompt were corrected from that evidence:

- Child-agent logs are nested below `<session-id>/subagents/`, not beside the
  primary transcript.
- `message.stop_reason: "end_turn"` is an explicit completion signal. A final
  assistant response is complete, not incomplete.
- One primary desktop transcript can carry several `cwd` values, including
  changes between parallel tool results. The latest explicit `cwd` is the
  current binding; evidence is limited to records with that normalized `cwd`.

---

# Task: make `apu-event`, `apu-wtf`, and `apu-intervene` provider-neutral

Repository: `C:\Users\Matt\Desktop\MyDocs\apu` (package `agent-policy-updater` 0.9.0,
console scripts in `pyproject.toml` lines 25–28). Run `python -m pip install -e ".[dev]"`
and `pytest` before you change anything; the suite must be green when you finish.

## Problem

The M10 behavioral pressure watcher (`primary-agent-autonomy-loss`) only attributes
incidents to **Codex** JSONL sessions. Evidence of the coupling:

- `src/apu/behavior_watch.py:702` `select_codex_session(...)` is the only session selector.
- `src/apu/behavior_watch.py:1031` `mark_incident(...)` calls `select_codex_session` and
  `ingest_codex_trace` unconditionally, so `apu-event`, `apu-wtf`, and `apu-intervene`
  return `no_attribution` for every non-Codex session.
- `src/apu/behavior_cli.py:110` and `:213` print "Codex" as if it were the only provider.
- `src/apu/behavior_watch.py:1379` `intervene(...)` resumes only through `codex exec resume`
  / `codex resume`.

The evidence layer (M11) is already provider-neutral: `src/apu/evidence.py:66`
`_HOOK_EVENTS` maps Claude Code lifecycle hooks (`PreToolUse`, `PermissionDenied`, `Stop`,
…) into the same contract, and `apu behavior audit --provider claude-code` reads them.
The incident, diagnosis, and intervention layers do not use that neutrality.

Goal: an operator in a Claude Code session (or any future provider) can run
`apu-event "<what went wrong>"`, `apu-wtf`, and `apu-intervene` and get the same
strict, private, typed attribution that Codex sessions get today. Codex behaviour
must not regress.

## Design requirements

1. **Provider adapter protocol.** Introduce a `SessionProvider` protocol (suggested
   module `src/apu/behavior_providers/`) with at least: `name`, `trace_root(env, home)`,
   `iter_trace_paths(root)`, `peek(path) -> (session_id, cwd)`,
   `parse(path) -> SessionTrace`, `is_run_incomplete(trace)`, `resume_capabilities()`,
   and `build_resume(trace, instruction, execute) -> ResumePlan`. Move the existing
   Codex logic (`codex_trace_root`, `_peek_session`, `_parse_session`,
   `_available_trace_paths`) behind a `CodexProvider` without changing its behaviour.
   `SessionTrace` (`behavior_watch.py:214`) gains a `provider: str` field.

2. **Claude Code provider.** Implement `ClaudeCodeProvider` against the real on-disk
   format. Verify each fact below against a real transcript before relying on it
   (there is one at `%USERPROFILE%\.claude\projects\C--Users-Matt-Desktop-MyDocs\*.jsonl`;
   read it only to confirm structure, never copy message bodies into fixtures or state):
   - Root: `~/.claude/projects/<slug>/<session-id>.jsonl`; `CLAUDE_CONFIG_DIR` overrides
     `~/.claude`. The slug is the absolute cwd with every non-alphanumeric character
     replaced by `-` (e.g. `C:\Users\Matt\Desktop\MyDocs` → `C--Users-Matt-Desktop-MyDocs`).
     Do not trust the slug alone; the records carry `cwd` and `sessionId`, use those for
     the exact normalized-cwd binding (`normalized_cwd_key`).
   - Records are one JSON object per line with `type` in `user | assistant | system |
     summary | …`, plus `uuid`, `parentUuid`, `timestamp` (ISO 8601 UTC), `cwd`,
     `sessionId`, `entrypoint` (e.g. `claude-desktop`, `cli`), `version`.
     `message.content` is a string or a list of blocks (`text`, `tool_use`,
     `tool_result` with `tool_use_id` and optional `is_error`). Tool outcomes also
     appear in `toolUseResult`.
   - There are no explicit task-started/completed events. Define "active" as the last
     record younger than `_MAX_SESSION_AGE_SECONDS`, and "incomplete run" as: the last
     `tool_use` has no matching `tool_result`, or the last record is an `assistant`
     record with no later `user` record.
   - Permission denials appear as `tool_result` blocks whose text comes from the harness
     or from a user-authored hook. Hash them; do not store them.
   - Subagent transcripts, if present, live beside the parent; treat them as part of the
     parent session, not as candidates.
   - Resume: interactive `claude --resume <session-id>`; non-interactive
     `claude -p --resume <session-id> "<instruction>"`. Confirm the flags with
     `claude --help` and record what you found in the code comment. If a mode is not
     supported, `apu-intervene` records and prints the continuation exactly as it does
     for Codex Desktop today.

3. **Selection across providers.** `select_session(provider=None, ...)` tries every
   registered provider. Exactly one provider with exactly one fresh exact-cwd active
   session → selected. More than one provider qualifying → new reason code
   `ambiguous_provider` (add it to `_NO_ATTRIBUTION_REASONS`, `behavior_watch.py:36`).
   `--provider codex|claude-code` on `apu-event`, `apu-wtf`, `apu-intervene`, and
   `apu-watch` forces one provider. Keep `--session-id`, `--trace-root`, `--cwd`.
   `SelectionProvenance` gains `provider`. Selector health (`selector-health.json`) is
   recorded per provider.

4. **Incident / diagnosis / evidence binding.** Incident and diagnosis artifacts carry
   `provider`. `mark_incident` ingests through the provider's normalizer so evidence
   records for a Claude Code incident use `provider: "claude-code"` and the
   `behavior/evidence/v2` route. `validate_session_binding` (`behavior_watch.py:881`)
   must re-check provider + session id + cwd before any resume, for every provider.
   `_effective_codex_surfaces` becomes provider-aware: for Claude Code the active
   instruction surfaces are the effective `CLAUDE.md` stack plus `~/.claude/settings.json`
   hook and permission sections; hash them, never store bodies.

5. **Two new detector codes**, with fixtures, because the incident that motivated this
   task was neither of the existing categories:
   - `request-substitution` (signal): the agent did a different task than the one asked,
     e.g. designed or built something new when told to run or record with an existing
     tool, or renamed/relocated work without being asked. Detect from the operator's
     description (`apu-event` text) and, where the trace allows, from an assistant turn
     that ends with a plan or a question when the user turn was an imperative.
   - `operator-designed-gate` (barrier): a tool denial whose text originates from a
     user-authored hook or from `permissions.deny` in the provider's settings. It is a
     barrier (suppresses automatic intervention) and must not be scored as
     `invented-gate`. Distinguish it from harness permission prompts.

6. **User-facing text.** Remove every "Codex" assumption from `behavior_cli.py`,
   `README.md` ("Behavioral pressure watch"), `RUNBOOK.md` ("Diagnose a live
   autonomy-loss incident"), and `roadmap.md` (add an "M13 — Provider-neutral incident
   attribution" entry; do not rewrite M10/M11 history). `apu-watch` health output names
   the providers it can attribute. Update `CHANGELOG.md`.

## Privacy and safety invariants (unchanged, enforced by tests)

- No message bodies, reasoning, tool inputs/outputs, command text, base instructions,
  or environment content enter `APU_HOME`. Only hashes, counts, labels, record ranges,
  reason codes, and surface paths/hashes.
- Credential-shaped strings are rejected from descriptions (`find_secret_spans`).
- No background service. No writes to provider log directories. No durable policy
  mutation from the watcher path; `durable_policy_mutation` stays exactly `false`.
- Never fall back to a session from another cwd or another provider.

## Tests to add (`tests/`)

- Synthetic Claude Code JSONL fixtures (hand-written, generic content): active
  incomplete run; completed run; stale run; two active runs in the same cwd
  (`ambiguous_active_candidates`); one Codex + one Claude Code active run in the same
  cwd (`ambiguous_provider`); `--provider` override resolving it; Windows and POSIX cwd
  normalization; `CLAUDE_CONFIG_DIR` override; unparsable line handling.
- `apu-event` → `apu-wtf` → `apu-intervene --dry-run` end-to-end on a Claude Code fixture,
  asserting `provider` in the incident, diagnosis, evidence, and selector-health files.
- Privacy assertions: grep every artifact written during the tests for fixture message
  text and for a seeded fake credential; both must be absent.
- Detector tests for `request-substitution` and `operator-designed-gate`.
- Existing Codex tests (`tests/test_behavior_watch.py`, `test_behavior_cli.py`,
  `test_behavior_evidence.py`, `test_behavior_audit.py`) pass unchanged.

## Acceptance

From a shell whose cwd is `C:\Users\Matt\Desktop\MyDocs`, with a Claude Code session
active in that directory and no Codex session:

```console
apu-event "asked me to run apu-wtf and record a failure; it designed a new command instead"
apu-wtf --json
apu-intervene --dry-run
apu-watch
```

`apu-event` attributes the Claude Code session; `apu-wtf --json` shows
`"provider": "claude-code"` and includes `request-substitution` in
`observed_signals`; `apu-intervene --dry-run` prints a `claude --resume` continuation
without launching it; `apu-watch` lists both providers. `pytest` and `apu validate`
are green.

## Working rules

- Small, reviewable commits in conventional-commit format, one concern each.
- Do not touch `build/`, `.venv/`, or anything under `.Codex/` except your own handoff.
- If a Claude Code format fact above turns out to be wrong, follow the real transcript
  and record the correction in the module docstring and in your final summary.
- Report residuals explicitly: what is not covered, what was assumed, what to verify
  live.

## Out of scope

Adapters for Gemini, Cursor, or other providers (the protocol must make them possible,
but do not implement them). Background watchers. Any change to the plan/review/apply
policy path.

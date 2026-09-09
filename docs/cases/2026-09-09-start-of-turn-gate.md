# Intervention case: the start-of-turn approval gate

- **Selected:** 2026-09-09
- **Board step:** `apu:select-intervention-case`
- **Status:** case selected, before state recorded, improvement defined;
  no intervention applied yet
- **Next step:** `apu:prove-intervention-effect` chooses and applies one
  narrow change, then re-measures against the definition below

## Executive summary

The case is the operator-authored start-of-turn approval gate that governs
every Claude Code session on this machine. It is wired in the global Claude
surface and enforced by a hook script. The gate is not a model-invented
approval habit, so a prompt rewrite cannot fix it; only a change to the gate's
own rules can. That makes it a clean intervention target: one surface, one
mechanism, a measurable cost, and a measurable safety property that must not
regress.

The before state was captured from a live session in the `apu` repository on
2026-09-09 with APU 0.9.0, plus the 2026-09-07 incident record as historical
context. The definition of improvement is a set of counts that the behavior
evidence plane already records, so the next step can re-measure without new
instrumentation beyond one small detector.

## Case selection

### Repository and surface

| Item | Value |
| --- | --- |
| Measured repository | `C:\Users\Matt\Desktop\MyDocs\apu` (session cwd binding) |
| Intervention surface | global Claude surface: `~/.claude/CLAUDE.md`, `~/.claude/settings.json` hook wiring, `~/.claude/hooks/speak-response.mjs` (gate mode) |
| Provider | claude-code |
| Mechanism | `UserPromptSubmit` hook classifies each prompt as approval, question, or new request; `PreToolUse` hook denies every tool outside a read-only allowlist while the state is `pending` |

The finding's source is a global surface, not a file in the repository. APU's
system audit covers global surfaces explicitly, and the behavior evidence is
bound to the repository where the session ran, so the case is measured in
`apu` and intervened on in `~/.claude`. Neither `~/.claude` nor `voice-send`
is a Git repository, so the intervention will need its own before/after
snapshot (`apu snapshot create`) rather than a commit for reversibility.

### Why this case

1. It recurs. The 2026-09-07 incident record names the gate as the amplifier
   that turned one misread command into seven turns with no file change, and
   it re-armed twice in the 2026-09-09 session on near-miss phrasing.
2. It is measurable today. Per-turn tool names, turn boundaries, and gate
   state transitions are already recorded without message bodies.
3. The change is narrow and reversible. The gate's rules live in one script
   and one settings file; the fix is a rule change, not an instruction
   rewrite across many surfaces.
4. It has a safety property that must be preserved, which is exactly what the
   "prove the intervention" step needs to argue: fewer wasted turns without a
   single unapproved mutation.

### Runner-up cases considered

- `unconditional-approval-gate` (high, medium confidence) on the superpowers
  `brainstorming` skill, line 3. Real, but a third-party skill; a fix there
  is a vendored patch and its behavior is hard to observe.
- Fourteen `duplicate-instruction` findings across the MyDocs root stack.
  Real, but their behavioral effect is diffuse and the consolidation campaign
  already owns them.

## Findings on record

### Static audit (`apu audit .`, APU 0.9.0)

Nine findings in the effective stack seen from `apu`, none on the gate:

| Category | Count | Surface |
| --- | --- | --- |
| microtask-planning | 4 | superpowers `writing-plans`, `executing-plans` |
| unconditional-approval-gate | 2 | superpowers `brainstorming` line 3 |
| per-task-review-loop | 2 | superpowers `requesting-code-review` line 15 |
| sensitive-material-exposure | 1 | runpod `runpodctl` skill line 57 |

Gap: the static audit reads `settings.json` as a surface but does not follow
hook commands into the scripts they run, so the gate itself is invisible to
the static audit. This is recorded as a residual, not fixed here.

### Behavior diagnosis (`apu-event` then `apu-wtf`, session in `apu`)

| Item | Value |
| --- | --- |
| Incident | `incident-7dc16d62184e411aa1ed2770c8f49f59` |
| Diagnosis | `diagnosis-66d1b05d9bbe41158f54f11b4b74b25a` |
| Status | `likely-autonomy-loss` |
| Observed signals | `reversible-choice-escalation`, `tool-failure-observed` |
| Possible barriers | none |
| Rank 1 source | `~/.claude/settings.json` line 137, `stop-pressure`, score 100 |
| Rank 2 source | `~/.claude/CLAUDE.md`, 22 lines, `approval-pressure`, `stop-pressure`, `workflow-gate`, score 69 |
| Rank 3 source | runtime behavior, `tool-failure-without-proven-fallback`, score 60 |
| Recommended intervention | `resume-instruction`, `durable_policy_mutation: false` |

Two honest caveats about this diagnosis:

- Line 137 of `settings.json` is the literal `"Stop": [` hook key. The
  `stop-pressure` match is a keyword false positive. The gate wiring is at the
  `UserPromptSubmit` and `PreToolUse` entries that call the hook script, and
  the diagnosis does not rank them.
- The `operator-designed-gate` barrier did not fire because no tool was
  denied: the agent complied with the gate by not attempting any mutating
  tool. The gate's cost therefore shows up as read-only-only turns, not as
  denials. The improvement definition below measures that directly.

### Bounded behavior audit (`apu behavior audit --provider claude-code`)

Before ingestion the audit discovered zero Claude sessions. After
`apu-event` ingested the session it reported one session, three findings
(`reversible-choice-escalation`, `tool-failure-observed`,
`orphaned-tool-result`), severities low 1 and medium 2. Audit id
`behavior-audit-f81d1cb8c6224cb5a0611e39fb1ddfd7`.

Gap: the bounded audit discovers Codex transcripts directly but only
already-ingested evidence for Claude Code. Without a prior `apu-event` or hook
ingestion, a Claude session is invisible to it.

## Before state

### Live session, 2026-09-09 (session `03cd60ec-54ce-4df9-9bb6-290817b17fa3`)

Counts come from the evidence plane record for this session; no message
content was read to produce them.

| Turn | Started (UTC) | Tools requested | Mutating tools | Gate state at start |
| --- | --- | --- | --- | --- |
| 1 | 05:13:34 | 15 (Read 7, Grep 5, Glob 3) | 0 | pending (new request) |
| 2 | 05:16:26 | 5 (Read 3, Grep 2) | 0 | pending (re-armed on "sounds OK") |
| 3 | 05:20:22 | 3 (Read 1, Grep 1, Glob 1) | 0 | pending (re-armed on a statement) |
| 4 | 05:25:52 | 17 Bash (git-read 2, shell 15) | first at 05:26:09 | approved ("go ahead") |

Derived measures:

| Measure | Value |
| --- | --- |
| Elapsed from first actionable prompt to first mutation | 12 min 35 s |
| Turns with zero mutating tools while a plan was pending | 3 |
| Approval near-misses (affirmation that did not open the gate) | 1 ("sounds OK for this task") |
| Prompts that re-armed the gate without a new imperative | 2 |
| Tool denials by the gate | 0 |
| Mutations before approval | 0 |

Turns 2 and 3 were genuine questions from the operator, so not all of that
time is gate cost. The gate cost is the near-miss in turn 2, which would have
opened the gate under a wider approval vocabulary, and the re-arms, which
forced the plan to be restated instead of carried forward.

### Historical, 2026-09-07 (session `05a17f11-6cfb-46eb-9fb2-6ca95025439c`)

From the incident record `incidents/2026-09-07-claude-compaction-request-substitution.md`:

| Measure | Value |
| --- | --- |
| Turns with no file change | 7 |
| Gate denials recorded | 2 (records 4050 and 4281) |
| Denied write explicitly requested by the operator | 1 (the incident dump) |

This session cannot be re-ingested: `apu-event --session-id` returns
`no_attribution` with reason `stale_trace`, which is the intended freshness
bound. The incident record is the evidence of record for it.

### Gate rules in force (content of the hook, summarized)

- Approval phrases: a fixed list starting with "go ahead", "proceed",
  "approved", "do it", "make it so"; short "yes"/"ok" only when the whole
  message is six words or fewer. "Sounds OK", "fine", "over", and "your
  turn" are not approvals.
- A question leaves the gate state alone.
- Any other prompt writes `pending` and re-arms the gate, even when a plan was
  just approved and the turn has completed.
- While the agent is mid-turn on an approved plan, a non-halt message is a
  steer; halt words ("stop", "wait", "hold on", "cancel") withdraw approval.
- Read-only allowlist: Read, Glob, Grep, LS, ToolSearch, ListSkills,
  ListAgents, AskUserQuestion, WebFetch, WebSearch, TodoWrite, TodoRead,
  ListMcpResourcesTool, ReadMcpResourceTool, Monitor, TaskOutput.

## Definition of improvement

The intervention is judged on a session in the `apu` repository that starts
with an actionable request and includes at least one operator affirmation
that is not on today's approval list.

| Id | Measure | Before | Target | Source |
| --- | --- | --- | --- | --- |
| I1 | Approval near-misses per session | 1 | 0 | gate hook log: prompts classified `pending` whose bare command begins with an affirmation |
| I2 | Prompts that re-arm the gate without a new imperative | 2 | 0 | gate state file transitions `approved -> pending` on non-halt, non-imperative prompts |
| I3 | Turns with zero mutating tools while a plan is pending, per objective | 3 | at most 1 | evidence plane: per-turn `tool_name` counts between `turn.started` and `turn.completed` |
| I4 | Gate denials of an explicitly requested local note or incident write | 1 (2026-09-07) | 0 | `operator-designed-gate` barrier count in the diagnosis |
| S1 | Mutations before an approval in a gate-armed session | 0 | 0 (must hold) | evidence plane: first mutating `tool.requested` precedes an `approved` gate transition |
| S2 | Halt words still withdraw approval | yes | yes (must hold) | gate state file: `pending` after a halt word |

I1 through I4 are the improvement. S1 and S2 are the safety properties; an
intervention that improves I1 to I4 by weakening S1 or S2 fails.

Candidate narrow changes for the next step to choose from, in order of
preference:

1. Bind approval to the objective: after an approval, a completed turn does
   not re-arm the gate; only a halt word or a new imperative does. This
   targets I2 and I3.
2. Widen the affirmation list to include "sounds ok", "sounds good to me",
   "fine", "fine by me", and a bare turn cue ("over", "your turn") when it
   follows a plan. This targets I1.
3. Exempt an explicitly requested write to `.claude/` notes or an incident
   record from the deny list. This targets I4.

Each is a change to `speak-response.mjs` gate mode only. None touches
`CLAUDE.md`, and none changes what counts as a halt.

### How the next step measures it

1. `apu snapshot create --label before-gate-intervention` covering
   `~/.claude`.
2. Apply one candidate change.
3. Run one scripted session in `apu` with the same prompt shape as
   2026-09-09: actionable request, an off-list affirmation, a question, an
   on-list approval, then a halt word.
4. `apu-event ... --provider claude-code` while the session is fresh, then
   `apu-wtf --provider claude-code --json` and
   `apu behavior audit --provider claude-code --json`.
5. Compute I1 to S2 from the evidence file and the gate state files. I3 needs
   a small detector or script: read-only-only turns while the gate state was
   `pending`. Adding it as a detector in the bounded audit is in scope for the
   next step; it was not added here.

## Residuals found while selecting the case

These are gaps, not fixed in this step:

- `apu-wtf` ignores `--provider`, `--session-id`, and `--cwd` whenever a
  latest incident already exists, and then fails with
  `diagnosis provider mismatch` against that stale incident. On this machine
  the stale incident was a Codex run from 2026-08-15 in another project. The
  documented operator procedure `apu-wtf --provider claude-code` therefore
  fails until a fresh `apu-event` replaces the latest incident.
- The bounded behavior audit has no Claude Code transcript discovery; it only
  sees Claude sessions that were already ingested.
- The static audit does not follow hook commands into scripts, so hook-enforced
  gates are invisible to it.
- The diagnosis ranks the literal `"Stop"` hook key as a `stop-pressure`
  source; the pattern needs a JSON-aware exclusion.
- The `operator-designed-gate` barrier only fires on an actual denial, so a
  compliant agent under a gate produces no barrier evidence.

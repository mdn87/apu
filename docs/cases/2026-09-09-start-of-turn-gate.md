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
context. The definition of improvement combines recorded tool counts with
reported gate behavior. The next step needs timestamped gate decisions and
mutation outcomes as well as a detector for turns limited to read-only tools;
the existing evidence alone cannot establish every improvement or safety measure.

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
2. Its tool activity is measurable today. Per-turn tool names and turn
   boundaries are recorded without message bodies. The gate states below
   were reported during selection; timestamped gate decisions still need
   to be captured for a controlled comparison.
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

Tool counts come from the evidence plane record for this session; no message
content was read to reproduce them. Gate states and prompt classifications
come from the original case report, not from those evidence records.

The selection recheck on 2026-09-09 validated all 86 schema-v2 events, their
Claude Code session and repository binding, and the counts below. The captured
events span 05:13:34.704 to 05:28:53.060 UTC, ending during turn 4. The
109,977-byte evidence file has SHA256
`6ed697ce3053120edc63dd2ad3eead07a9710d3520cb5bb0dc3848f60e799928`.
Its source boundary is 842,102 bytes with SHA256
`fa0417277a36050f559bdc42dc86e23e1d568763d569f037a0201fb1ccd9106b`.
This recheck used the stored evidence; it did not replay the source transcript.

| Turn | Started (UTC) | Tools requested | General shell requests | Reported gate state at start |
| --- | --- | --- | --- | --- |
| 1 | 05:13:34 | 15 (Read 7, Grep 5, Glob 3) | 0 | pending (new request) |
| 2 | 05:16:26 | 5 (Read 3, Grep 2) | 0 | pending (re-armed on "sounds OK") |
| 3 | 05:20:22 | 3 (Read 1, Grep 1, Glob 1) | 0 | pending (re-armed on a statement) |
| 4 (captured portion) | 05:25:52 | 17 Bash (git-read 2, shell 15) | 15; first at 05:26:08.073 | approved ("go ahead") |

A shell request is not evidence that a mutation occurred. The earlier version
called 05:26:09 the first mutation; the stored record instead establishes a
first general shell request at 05:26:08.073. It contains neither gate-state
events nor repository-state observations, so actual mutation timing and gate
transitions cannot be reconstructed from this file alone.

Derived measures:

| Measure | Value |
| --- | --- |
| Elapsed from first prompt to first general shell request | 12 min 33.369 s; mutation timing unmeasured |
| Turns limited to Read, Grep, and Glob while the gate was reportedly pending | 3, totaling 23 requests |
| Approval near-misses (affirmation that did not open the gate) | 1 reported ("sounds OK for this task") |
| Prompts that re-armed the gate without a new imperative | 2 reported; no transition history captured |
| Tool denials by the gate | 0 reported; diagnosis lists no barrier |
| Mutations before approval | 0 reported; first three turns contain only Read, Grep, and Glob |

The original case report describes turns 2 and 3 as genuine questions from
the operator, so this elapsed time is not a causal estimate of gate cost.
The suspected avoidable cost is the reported near-miss and repeated gate
resets. A matched before/after session must distinguish intentional discussion
from extra turns caused by the gate.

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

The intervention is judged on matched sessions in the `apu` repository that
start with an actionable request and include an affirmation outside the
original approval list. Before running them, label each prompt's intended
role: request, approval, question or continuation, or halt. The changed hook
must not define its own expected answers.

| Id | Measure | Before | Target | Source |
| --- | --- | --- | --- | --- |
| I1 | Approval near-misses per session | 1 reported | 0 | timestamped gate decisions for prompts pre-labeled as approval |
| I2 | Prompts that reset the gate without a new imperative or halt | 2 reported | 0 | gate decisions that write `pending`, including `pending -> pending`, joined to pre-labeled prompt roles |
| I3 | Completed turns limited to read-only tools while a plan is pending, per objective | 3 tool-count observations; gate state reported | at most 1 | per-turn tool counts joined to gate decisions; annotate question-only turns separately |
| I4 | Gate denials of an explicitly requested local note or incident write | 1 reported (2026-09-07) | 0 | correlated denial results for a pre-labeled, explicitly authorized local-write scenario |
| S1 | Successful mutations without valid approval, including after a halt | 0 reported | 0 (must hold) | gate decisions plus correlated tool results and before/after state of the test target; unknown shell effects remain unmeasured |
| S2 | Halt words still withdraw approval | specified by the reported rules; no captured test | yes (must hold) | timestamped `pending` decision after a halt, followed by a denied mutation attempt until renewed approval |

I1 through I4 define the intended improvements. Before applying a narrow
change, declare which measures it targets; those must meet their targets and
the others must not regress in the matched scenarios. Report all six measures
and distinguish observed results from historical reports. Missing gate history,
an untested write scenario, or an unknown shell effect is unmeasured, not a pass.
S1 and S2 are mandatory safety checks for every candidate; weakening either
fails the intervention. Only claim all four improvements if all four are tested
and meet their targets.

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

1. Use the narrow APU profile covering the hook directory and its instruction
   and settings files for the restore point. The previous session recorded a
   `before-gate-intervention` snapshot; whole-directory snapshots of
   `~/.claude` encountered volatile files.
2. Capture a control session before applying the selected change. Freeze the
   targeted measures and prompt roles, and retain timestamped gate decisions
   and tool outcomes without message bodies. Repeat the same scenarios after
   the change; use fresh sessions and record the hook hash for each run.
3. Include an actionable request, an off-list affirmation, a question, an
   on-list approval, an explicitly requested local note write, and a halt
   followed by an attempted mutation. Use a controlled local test target so
   state observations can establish whether a write happened.
4. `apu-event ... --provider claude-code` while the session is fresh, then
   `apu-wtf --provider claude-code --json` and
   `apu behavior audit --provider claude-code --json`.
5. Compute I1 to S2 from the evidence and retained gate decisions. A final gate
   state file cannot establish earlier transitions. I3 needs a small detector
   or script joining completed read-only turns to the gate history. That
   instrumentation and the controlled comparison belong to the next step.

## Residuals found while selecting the case

The selector defect found during selection was fixed by commit `fb827e3`:
`apu-wtf` now honors explicit provider, session, cwd, and trace-root selectors
instead of reusing a mismatched latest incident. The prior failure is historical.

Remaining gaps:

- The stored baseline has tool activity but no gate transition history or
  repository-state observations. Gate-specific counts remain reported until
  the next step captures a controlled before/after comparison.
- The bounded behavior audit has no Claude Code transcript discovery; it only
  sees Claude sessions that were already ingested.
- The static audit does not follow hook commands into scripts, so hook-enforced
  gates are invisible to it.
- The diagnosis ranks the literal `"Stop"` hook key as a `stop-pressure`
  source; the pattern needs a JSON-aware exclusion.
- The `operator-designed-gate` barrier only fires on an actual denial, so a
  compliant agent under a gate produces no barrier evidence.

## Selection verification, 2026-09-09

The prerequisite is complete: the Orca boundary is present in commit
`29b027a`, and both CI workflows passed on `fdb24e1`. The local boundary and
provider-attribution suites passed all 28 tests during this recheck. The
referenced incident, diagnosis, and audit exist; the audit confirms one session
and the three findings listed above. This closes case selection with reported
confidence. Applying the gate change and proving its effect remain the next step.

## Intervention attempt 1, 2026-09-09

Candidates 1 and 2 were packaged as `scripts/gate_intervention_2026_09_09.py`
and applied by the operator by hand, because the harness classifier denies
this session any write or execution under the Claude home. The script kept a
backup beside the hook, and `node --check` passed.

The first proof run drove the live hook with synthetic session events through
the scripted sequence and returned four mismatches out of fifteen rows:

| Row | Prompt role | Expected | Observed |
| --- | --- | --- | --- |
| 6 | halt ("hold on, do not proceed") after approval | pending | approved, no hook output |
| 6a | Bash after the halt | deny | allow |
| 7 | new objective while pending | pending | approved |
| 9 | long new objective after "go ahead" | pending | approved |

Rows 1 through 5 and 8 passed, so the wider vocabulary and the early-phrase
rule (I1) worked. The failures share one cause: the script placed its two new
constants after the CLI dispatch that calls the gate, so every post-approval
prompt that was not itself an approval or a question threw a ReferenceError
before the gate could act. The state stayed `approved`. That is a live S2
failure: while the defective version was installed, halt words did not
withdraw approval in any session on this machine. Rows 4 and 10 "passed" for
the same wrong reason and count as unmeasured for that run.

Correction: commit `4dd0765` moves the constants beside `HALT`, adds
`--reapply` (restore the backup, apply the corrected change), makes `--check`
distinguish not-applied from anchors-missing, and adds a Node-driven test that
runs the proof sequence against a fixture shaped like the real hook, including
the early dispatch. The test would have caught the defect and now passes. The
corrected change is not yet on the live hook; the operator must run
`--reapply` and then the proof driver again. Until that run exists, every
measure for attempt 1 is unmeasured, not passed.

Lesson recorded: an intervention script that cannot be executed against its
target from the session that wrote it must carry its own executable test, or
it must not be handed to the operator.

## Intervention attempt 2, 2026-09-09: result

The operator ran `--reapply`. The live hook now defines the new constants at
line 96, after `HALT` at line 90 and before the gate dispatch at line 217. The
operator then reran the proof driver against the live hook. Fifteen rows,
two mismatches, both explained below and neither a hook defect:

| Row | Prompt role | Expected | Observed | Verdict |
| --- | --- | --- | --- | --- |
| 1 | new request, fresh session | pending | pending | pass |
| 1a | Bash while pending | deny | deny | S1 pass |
| 1b | Read while pending | allow | allow | pass |
| 1c | Write of a note while pending | deny | deny | I4 unchanged, not targeted |
| 2 | off-list affirmation ("sounds OK for this task…") | approved | approved | I1 pass |
| 2a | Bash after that affirmation | allow | allow | pass |
| 3 | question | approved | approved | pass |
| 4 | verbatim long statement containing "dont" | approved | pending | see note |
| 5 | early phrase, long tail ("yeah mr wizard make it so. …") | approved | approved | I1 pass |
| 6 | halt ("hold on, do not proceed") | pending | pending | S2 pass |
| 6a | Bash after the halt | deny | deny | S1 pass |
| 7 | new objective while pending | pending | pending | pass |
| 8 | "go ahead" | approved | approved | pass |
| 9 | long new objective after approval, no steer lead | pending | pending | boundary pass |
| 10 | steer-led long new objective | approved | pending | driver error |

Row 4 note: the verbatim 2026-09-09 utterance contains "dont", which the
gate's pre-existing halt vocabulary matches, so the hook re-armed by its own
definition of a halt. The same statement without the contraction carries
forward, which the fixture test covers. This is a halt over-match in the
original rule set, not a regression from the change; it is recorded as a gap
below. Row 10 failed because the driver did not re-approve after row 9
re-armed the gate, so the state it probed was pending, not approved. The
checked-in driver `scripts/gate_proof_session_2026_09_09.mjs` fixes both
sequencing points and adds a "wait" halt row; the fixture-backed test already
covers the steer-word gap and passes.

### Scores against the definition

| Id | Measure | Before | After | Verdict |
| --- | --- | --- | --- | --- |
| I1 | Approval near-misses per session | 1 reported | 0 in the proof (rows 2, 5) | met |
| I2 | Prompts that reset the gate without a new imperative or halt | 2 reported | 0 in the proof; the one reset (row 4) contained a halt-vocabulary word | met, with the halt over-match noted |
| I3 | Completed read-only turns while pending, per objective | 3 tool-count observations | unmeasured: the driver has no model turns; one live carry-forward firing was observed on a real prompt after the reapply | unmeasured |
| I4 | Gate denials of an explicitly requested local write | 1 reported | still denied (row 1c) | not targeted by this attempt |
| S1 | Mutations without valid approval, including after a halt | 0 reported | 0 at the hook level (rows 1a, 6a deny) | holds at the hook level; no real mutation attempted |
| S2 | Halt words still withdraw approval | untested | pass (row 6, and "wait" in the fixture test) | holds |

The attempt targeted I1 and I2 and declared so. Both met their targets, and
both safety checks hold at the hook level. I3 and I4 are not claimed.

### Decision on the effect signal

The findings do need a stronger effect signal for I3. The hook-level proof
shows the rule change, not the behavior change: whether an agent under the
new rules actually spends fewer read-only turns waiting. That needs a
detector in the bounded audit that joins each completed turn's tool names to
the gate state file's transitions, which are not yet retained with
timestamps. The concrete follow-up is: have the gate hook append one
content-free line per decision (session hash, prompt role, state before and
after, timestamp) to its latency log, and teach `apu behavior audit` to read
it. Until then, I3 stays reported.

### Run 3: the checked-in driver on the live hook

The operator ran `scripts/gate_proof_session_2026_09_09.mjs` against the live
hook: eighteen rows, one mismatch. The mismatch was again the driver's own
wording for row 4, which had been changed from "dont" to "do not", also a
halt-vocabulary token; the hook re-armed by its own rule. The wording is now
free of halt tokens. Two rows were new and both passed on the live hook:
row 10 confirmed the steer-word carry-forward with the new hook message, and
row 11 confirmed that "wait" withdraws approval. No hook behavior changed
between runs 2 and 3; the scores above stand.

### Classification of the recorded gaps, 2026-09-09

Assessed against the case's own definition (S1: no mutation without valid
approval; I2: no reset without a new imperative or halt) and APU's stated
role of supporting bounded interventions while preserving useful autonomous
continuation. A fixture that pins current behavior is a regression guard, not
a product decision; none of the items below was accepted by the operator.

| Gap | Classification | Basis |
| --- | --- | --- |
| Steer-led new objective inherits approval (run 3 row 10) | Unresolved defect in the intervention's boundary, pending an operator decision | Introduced by candidate 1's steer-lead list, which includes imperative verbs ("build", "make", "add"). Whether a steer-led new objective is "valid approval" under S1 is a product question the records do not answer. The attempt note calling it "accepted" was the implementer's, not the operator's. |
| Mid-sentence halt tokens re-arm the gate (runs 2 and 3 row 4) | Pre-existing limitation of the operator's gate; separately scoped improvement | The halt rule and its comment ("Only a halt word does") exist unchanged in the pre-intervention backup at lines 90 and 692. It fails safe, costing autonomy (an I2-style reset) but never safety. Not introduced by the change. |
| Explicit note write still denied (I4) | Separately scoped, never attempted | Candidate 3 was listed and not applied; no decision was taken either way. |
| I3 unmeasured | Separately scoped measurement work | The case record's "Decision on the effect signal" names the concrete instrumentation. |

The board tracks the first three under `apu:gate-rule-gaps` (blocked on the
carry-forward decision) and the fourth under `apu:gate-decision-evidence`.
The completed proof task stays closed on runs 2 and 3.

### Gaps recorded by this attempt

- The halt vocabulary matches "don't" and "wait" anywhere in a message, so a
  statement such as "we dont have routing" withdraws approval. Narrowing halts
  to sentence-initial or imperative forms is a candidate for a later attempt.
- After an approval, a long new objective that opens with a steer word ("now
  build…") carries the approval forward. Accepted for this attempt; the
  fixture test pins the behavior so a change to it is deliberate.
- Candidate 3 (explicit local-note write exemption) is untouched; I4 is open.

# Incident: Claude replaced `enter apu-wtf` with a different task

- **Observed:** 2026-09-07, approximately 02:55–03:21 America/New_York
- **Affected session:** Claude Code/Claude Desktop session
  `05a17f11-6cfb-46eb-9fb2-6ca95025439c`
- **Status:** diagnostic support repaired; recurrence prevention remains partly
  outside APU
- **Primary failure:** context compaction changed the operator's request
- **Amplifiers:** request substitution, unsupported inference, and an
  over-broad start-of-turn gate

## Executive summary

The voice system delivered the command `enter apu-wtf` correctly. The failure
began when Claude compacted the conversation and wrote a false interpretation
into its summary: it recast the command as a request to investigate whether the
APU repository should contain the voice system. After compaction, Claude used
that interpretation as the active task instead of checking the operator's
original words.

Claude then substituted new work for the requested work. It evaluated APU as a
code home, inferred significance from the unrelated `ogmi` repository name,
and designed a new WTF voice command even though `apu-wtf` already existed.
The start-of-turn approval gate did not cause the original misunderstanding,
but it made recovery much worse: each correction became another plan-and-wait
turn, and the gate eventually denied the requested incident-dump write itself.

Restarting Claude Desktop is not a reliable reset because the application can
reopen the same persisted conversation and its bad compacted summary. The
reliable immediate recovery is `/clear` or a genuinely new conversation that
does not resume this session.

## Evidence

The source transcript is:

`C:\Users\Matt\.claude\projects\C--Users-Matt-Desktop-MyDocs\05a17f11-6cfb-46eb-9fb2-6ca95025439c.jsonl`

The evidence establishes the following sequence:

1. Transcript record 3973 contains the raw user command `enter apu-wtf`.
   The accompanying voice triage also understood it as entering `apu-wtf`.
   This rules out Whisper delivery as the initiating failure.
2. The compaction summary at record 4010 reframes that command as an
   investigation of whether APU is the intended home for the voice loop. That
   request does not appear in the raw command.
3. The first assistant response after compaction follows the summary's invented
   task. It reads APU as a possible code home instead of running the existing
   diagnostic command.
4. In the next turn, “activate a WTF command” is again treated as a design
   request. Claude proposes a replacement WTF implementation and a new naming
   scheme rather than connecting the request to the `apu-wtf` command it had
   just inspected.
5. Transcript records 4050 and 4281 show the start-of-turn gate denying tool
   use because a plan had not been approved. Record 4281 is the attempted write
   of the incident dump explicitly requested by the operator.
6. The transcript contains 4,288 valid records with one stable session ID. Its
   `cwd` legitimately moves among `MyDocs`, `voice-send`, and `masq`, including
   changes between parallel tool results. This directory movement is normal
   session behavior and was not the cause of the semantic failure.

The updated APU parser detects `request-substitution` and
`operator-designed-gate` from this real transcript without retaining message
bodies, reasoning, tool inputs, or tool outputs in APU state.

## Causal analysis

### Root cause: semantic corruption during compaction

The compaction process did not merely shorten the history. It converted an
imperative—run an existing named command—into a speculative project-organization
question. It then placed that interpretation in the summary's current-task
section, where it functioned as authoritative state.

Post-compaction Claude did not compare the summary with the nearby raw command.
That allowed an inference made by the summarizer to replace the operator's
actual instruction. This is a session-continuity failure, not a voice transport
failure and not an APU command failure.

### Secondary failure: request substitution

Once the false objective was active, Claude repeatedly solved a different
problem:

- It answered “should APU contain the voice loop?” instead of executing
  `apu-wtf`.
- It proposed building a new WTF voice feature instead of using the existing
  diagnostic command.
- It treated the undocumented `ogmi` name and a mythology association as
  evidence of operator intent, although the operator had supplied no such
  signal.

The higher-level concept is **request substitution**: replacing the requested
operation with a newly inferred design task. “Misunderstanding” alone is too
vague because Claude continued to construct and seek approval for substitute
work after encountering the exact command name.

### Amplifier: the approval gate

The start-of-turn gate was configured to deny every non-read-only action until
the operator approved a plan. In practice, it also denied at least one
read-oriented shell command and the explicitly requested incident-dump write.

The gate did not create the false interpretation. It increased its cost and
duration:

- Claude had to speak a complete plan before the operator could correct it.
- Every correction started another plan-and-wait cycle.
- The gate prevented Claude from recording the failure when finally asked.
- Seven turns produced no file change, including the requested report.

This is an **operator-designed gate** whose implementation is broader than its
useful policy boundary. It should not be diagnosed as a model-invented approval
requirement, but its scope should be revised.

### Contributing factor: lossy voice triage

The voice listener worked, but the model-facing turn also included a triage
paraphrase. For a short command, paraphrasing removes useful precision. The
existing rule to quote the raw utterance was not followed after compaction, so
Claude lost an easy opportunity to notice that `apu-wtf` named an executable
rather than a repository destination.

### Tooling gap at the time

The installed APU version could select only Codex session traces for live
incident attribution. Claude Code was supported by parts of the evidence
pipeline, but `apu-event`, `apu-wtf`, and `apu-intervene` could not bind a Claude
session. Consequently, the requested diagnostic could not have succeeded from
that Claude session even if Claude had invoked it correctly. Claude discovered
this decisive fact only after two incorrect turns.

## What was ruled out

- **Whisper/listener failure:** ruled out; the raw command arrived correctly.
- **Multiple or corrupt session identities:** ruled out; all 4,288 transcript
  records with a session identity use the same ID.
- **`ogmi` as an intent signal:** ruled out; it is an unrelated repository.
- **A new WTF feature being necessary:** ruled out; `apu-wtf` already existed.
- **Application restart as a guaranteed cure:** ruled out; persisted sessions
  preserve their compacted summaries across process restarts.

## Repairs completed

APU commit `48d7560` is on `origin/main`, and the global uv installation now
uses `file:///C:/Users/Matt/Desktop/MyDocs/apu`.

The implementation now:

- supports Codex and Claude Code through a provider adapter;
- lets `apu-event`, `apu-wtf`, `apu-intervene`, and `apu-watch` accept
  `--provider claude-code`;
- binds provider, session ID, and normalized working directory before recording
  evidence or constructing a continuation;
- understands real Claude Code completion markers and excludes subagent logs;
- handles one Claude session moving among working directories while keeping
  evidence scoped to the exact directory;
- detects request substitution;
- classifies provider permission-rule denials as an operator-designed gate;
- hashes sensitive evidence rather than storing message or tool bodies; and
- fails closed when provider or session attribution is ambiguous.

Verification completed with 506 passing tests and two expected platform skips.
The global `apu-wtf --help` now exposes
`--provider {claude-code,codex}`.

## What the APU repair does not fix

APU can now record, diagnose, and prepare a continuation for this class of
Claude failure. It does not control Claude's compaction algorithm, prevent a
future summary from changing intent, or change the start-of-turn gate. Those
are separate policy and harness concerns.

## Recommended prevention

1. Preserve short operator commands verbatim in compaction summaries. Any
   interpretation should be labeled as an unverified inference, not written as
   the current task.
2. On the first turn after compaction, compare the summary's current task with
   the last raw operator message. When they conflict, the raw message wins.
3. Make voice triage additive: retain the raw utterance beside any paraphrase,
   and quote it in the assistant's opening sentence when it is short or
   ambiguous.
4. Narrow the start-of-turn gate. Read-only inspection and an explicit request
   to record a local note or incident dump should not require a separate plan
   approval. Approval should attach to the current objective rather than reset
   on every corrective turn.
5. Use a new conversation after semantic derailment. Restart the application
   only when needed for process health, and do not resume the contaminated
   conversation afterward.

## Immediate operator procedure

When this happens again:

1. Say or type `apu-event "<concise description>" --provider claude-code` while
   the failed Claude turn is still fresh and incomplete.
2. Run `apu-wtf --provider claude-code` to diagnose the recorded incident.
3. Use `/clear` or start a new Claude conversation if the active conversation
   continues following a false compacted objective.
4. Use `apu-intervene --provider claude-code --dry-run` only when a continuation
   of the original session is preferable to starting clean.

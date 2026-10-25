# Codex structured results in history analysis

APU's next intervention should correct its interpretation of structured Codex
tool results. A bounded review found 68 results with explicit nonzero command
exit codes that APU classified as `tool.completed`. Until this is corrected,
a clean Codex history audit can hide recorded command failures.

This is an evidence-quality case, rather than a new approval rule for agents.
The implementation can use existing history and local fixtures without a live
operator session. Selected on 2026-10-23 after Matt requested history analysis
or useful work requiring minimal interaction.

## History examined

The sample contains ten Codex and ten Claude Code transcripts across ten
workspaces under MyDocs. Selection took the most recently modified eligible
files from the previous fourteen days, excluding files modified within ten
minutes, Claude subagent logs, files over 20 MiB, and files outside MyDocs.
The aggregate source limit was 128 MiB; 59,454,100 bytes were ingested.
Three recently active files and one file reaching a source-byte cap were
skipped. The collector combines per-file and aggregate limits into one skip
reason; the retained metadata cannot establish which cap that file reached.

APU ingested 3,210 events. Nineteen sessions and 3,194 events qualified for
the retrospective audit, and all those events matched their source records
and transcript-prefix hashes. One recently modified Claude transcript held
older events and fell outside the audit window. The selection includes both
operator and automated sessions; ten minutes without modification is not
proof that a session has ended.

The existing APU ingestion and audit APIs produced the results in project-local
state. No provider transcript, global configuration, or instruction was changed.
Both providers were explicitly ingested, then audited as existing evidence
with raw discovery disabled and no gate log. Claude requires this because the
audit discovers existing Claude evidence, not raw Claude history. Workspace
names in the JSON are relative to MyDocs; `.` means MyDocs itself. The initial
APU-only thirty-day query found just one session, so it was not used as the
basis for a broad behavioral conclusion.

## Findings and interpretation

| Existing detector | Findings | Interpretation |
| --- | ---: | --- |
| Unresolved tool request | 4 | Missing result correlations warrant inspection; they do not prove abandoned work. |
| Completion after stale gate | 4 | The detector saw a mutation after a successful test. This concerns test freshness, not the retired approval-gate measurement. |
| Repeated permission denial | 1 | Three denial events in one Claude session; authorization and intervening instructions need review before blaming retries. |
| Orphaned tool result | 2 | Results without matching requests may reflect transcript boundaries or attribution gaps. |

All eleven findings came from Claude Code sessions; Codex produced none.
These eleven findings are leads, not eleven confirmed agent mistakes. No
operator incident assertions were imported, and current instruction hashes
cannot establish which instruction caused historical behavior. Test-freshness
work was not selected, in keeping with Matt's preference.

The stronger finding is in APU itself. `src/apu/evidence.py` checks for failure
text only when a Codex result is a string. Recent transcripts instead contain
lists of `input_text` blocks. Extracting the text alone would still miss JSON
`"exit_code": 1`: the legacy patterns match prose `exit code: 1`, not JSON keys.
The failure pattern also omits negative exits even though the extraction
pattern accepts them. Inside 68 blocks, a JSON command-result envelope
has an explicit nonzero `exit_code`, together with `chunk_id`, `output`,
`original_token_count`, and `wall_time_seconds`. All 68 were recorded as
`tool.completed` with no captured exit code, across six
Codex sessions. The count excludes nested wrappers and mere textual mentions
of an exit code. No raw output was copied into the case evidence.

A nonzero exit is a recorded process status, not proof of harmful behavior:
for example, a search can return one when it finds no matches. Correct status
capture must precede any interpretation of retries or agent effectiveness.
The live watcher has another parser in `behavior_watch._parse_session` that
only handles string `function_call_output` failures. Correcting the evidence
adapter alone would leave this observer inconsistent.

## Selected intervention

The board item `apu:structured-codex-results` implements typed result blocks
and recognized command-result envelopes in the Codex evidence adapter. It must retain only safe status metadata and hashes.
Align the evidence adapter and live watcher on the same supported result
contract, covering both function and custom tool outputs.

The implementation must distinguish terminal command results from running
sessions, missing status, image blocks, quoted examples, and arbitrary JSON
documents. Multiple results in one outer tool response need explicit aggregate
semantics; an outer script finishing does not guarantee that every command
it reported succeeded. Preserve legacy string handling and Claude behavior.

Existing evidence is append-only. A changed projection can create a second
event for the same source record or conflict with a prior projection. Define
replay and compatibility behavior before changing ingestion; do not silently
rewrite historical evidence or claim the old audit was corrected.

### Replay requirements

Code inspection identifies three distinct hazards, not a demonstrated loss
of stored evidence:

- `_event` and `_logical_event_key` include `event_type`. Reprojecting a
  completed result as failed creates a different identity; `read_evidence`
  retains both. Consumers count both events and select a result by iteration
  order, which can depend on the event-ID hash rather than correction intent.
- Observation-only changes leave a same-version event ID unchanged and are
  skipped by `append_evidence_events`. Cross-version records with the same
  logical key but different observations cause `read_evidence` to raise
  `cross-version evidence projections disagree`. Snapshot metadata also
  participates in this comparison, including when a transcript grows.
- `behavior_audit._already_ingested_unchanged` compares byte boundaries, not
  adapter semantics. An unchanged source can reuse the old projection.

The implemented v3 reader selects one effective Codex result by source record,
retaining old records while permitting only status and exit-code corrections.
Transcript ingestion uses a version-specific reuse check. Matt approved this
format change on 2026-10-24; existing files are never rewritten. Fresh and
mixed-version replay are both covered by acceptance checks.

### Detector implications and scope

`reconcile_evidence` and `behavior_audit._evidence_findings` flag repeated
failed results with identical input hashes. They do not establish whether
intervening evidence justified repetition. Watcher diagnosis can label these
signals `likely-autonomy-loss` when barrier and recovery branches do not
take precedence. These are heuristic labels, not proof of agent misconduct.
Acceptance must inspect changes in those findings, including benign nonzero
statuses such as search misses, before drawing a behavioral conclusion.

Two related limitations are recorded for separate work, not added to this
implementation's scope: `_command_class` recognizes neither `exec_command`
nor its `cmd` argument, and the stale-test detector requires an observed
successful test before it can report a later mutation. Correcting result
status alone cannot establish complete command or test-freshness coverage.
The present review does not quantify their impact on this sample.

Update, 2026-10-24: direct `exec_command` and `functions.exec_command` requests
now use `cmd` for command classification. Existing missing classifications can
be replayed without rewriting stored evidence or duplicating effective events.
The successful-test prerequisite remains unchanged. The available APU transcript
uses orchestration calls named `exec`, so it provides no direct execution-command
sample for measuring this change; detector effects are covered by controlled
regression cases.

## Acceptance evidence

Use synthetic, content-free examples to verify the supported formats, then
replay the captured source boundaries into a separate project-local state
directory. The before baseline is 68 explicit nonzero command results
classified as completed. The after result must recognize all supported
envelopes in that set, retain their exit codes, and explain any deliberate
exclusions. Zero, missing, and running statuses must remain distinct from
failure. Verify replay against prior evidence as well as fresh ingestion.
Cover negative exits, boolean pseudo-integers, observation-only corrections,
growing transcript snapshots, and live-watcher agreement. Compare reconciliation
and audit findings across all ten Codex sessions before and after the change.
Report new leads separately from confirmed behavior changes.

Recognize envelopes by their supported producer format and typed fields;
never search recursively through command stdout or stderr for status. Keep
the digest of the full result while retaining only allowed status metadata.
A missing exit code alone does not prove that a process is running, and an
outer tool response completing does not establish command success. V3 stores unknown/running in observation status. Nonzero siblings dominate;
otherwise unknown siblings dominate running, and running dominates success. Argon's proposed exact key allowlist and a
`tool.completed` fallback for missing status are suggestions, not an adopted
contract: they could reject valid evolving envelopes or imply success.

The case evidence is [the history analysis JSON](2026-10-23-history-analysis.json).
It includes source-prefix hashes, record hashes and line numbers for the
68 results, aggregate audit counts, selection limits, and report hashes.
Detailed normalized evidence and audit reports remain in the ignored local
directory `build/recent-history-2026-10-23/`. Raw prompts, commands, outputs,
and credentials are not part of the tracked evidence.

## Independent review incorporated

AGY Gemini 4 Argon High reviewed baseline commit
`a036c8c577e43d4ceddc5e5b1cd752598e71b1ec` through Autowork run
`20261024-012948-aaefb94d3fdf`. The review confirmed the retained artifact
arithmetic and the 68-result classification. It could not independently
recompute raw-source hashes or inspect every normalized record within its
read-only tool limits. This is corroboration of the analysis artifacts, not
a second source verification run.

This revision incorporates the duplicate parser, JSON-format mismatch,
replay hazards, downstream interpretation risks, and documentation corrections.
It qualifies the review's claim of corruption as a projection/reader risk,
without asserting physical evidence damage or a measured failure probability.
The review's parser and schema suggestions were design inputs; that documentation
revision did not change runtime code. The implementation below followed it.

## Implementation and replay results

Claude Opus accepted the revised case with minor corrections in Autowork run
`20261024-014842-8120547bffed`. Its main additions were a complete status
baseline and comparison of watcher diagnoses. Matt subsequently approved the
v3 evidence format. The implementation uses one shared parser for evidence
and the live watcher; v1/v2 writers retain their historical interpretation.

The captured ten Codex transcript prefixes were verified against their saved
hashes. Across 786 result records, direct command envelopes contain 494 zero
exits, 68 nonzero exits (all exactly 1), and 169 missing exits. The 169 missing
exits also carry a valid session ID and are recognized as running commands.
Some records contain multiple envelopes. The 68 image blocks are a separate
count. See [the extended baseline](2026-10-24-structured-result-baseline.json).

All 68 baseline failures now retain exit code 1 and become `tool.failed`.
At the outer response level, the 786 results aggregate to 68 failed, 13
completed, and 705 unknown. Opaque sibling text prevents a claim that the
whole response succeeded or describes only a running process; individual
recognized envelopes still have explicit status. Zero, running, missing,
boolean, negative, and mixed statuses have focused fixture coverage.

Replay into fresh and mixed v2/v3 state produces identical effective events
across all ten sessions. Repeating ingestion appends nothing; existing v2
bytes remain unchanged. Observation-only corrections, growing transcript
snapshots, and unrelated projection conflicts have regression coverage.
Unknown/running responses pair with their tool request as returned responses,
but never count as successful tests. Process-lifecycle correlation across
subsequent polling calls remains outside this case.

The final watcher comparison adds a failure signal in six sessions and changes
zero diagnostic labels. Codex single and repeated command-failure signals
remain visible, but cannot alone establish autonomy loss or permit recovery.
Independent behavioral signals, explicit recovery requests, and legitimate
barriers retain their existing precedence. This prevents the five extra
heuristic diagnoses observed in the partial implementation.

The comparison used analytical incidents with captured settings and signals,
without operator assertions, current instruction surfaces, or interventions.
The audit replay also omitted current instruction-surface inventory; source
verification and detector execution were unchanged. All 1,641 Codex events
verified against their captured source boundaries. No new Codex audit findings
appeared. Zero findings still reflect incomplete command-classification and
test-freshness coverage and are not proof that the sessions had no problems.

See [the implementation replay](2026-10-24-structured-result-replay.json) for
per-session results, preservation checks, detector counts, and diagnosis
comparison. Runtime changes preserve content minimization. Legacy evidence,
provider transcripts, and live global configuration were not modified.

The separate Dias note-path exemption remains open and was not applied. *(Update 2026-10-25: applied through `apu apply`; see "Part b installed" in the 2026-09-09 case.)*

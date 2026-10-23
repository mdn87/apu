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
Three recently active files and one oversized file were skipped.

APU ingested 3,210 events. Nineteen sessions and 3,194 events qualified for
the retrospective audit, and all those events matched their source records
and transcript-prefix hashes. One recently modified Claude transcript held
older events and fell outside the audit window. The selection includes both
operator and automated sessions; ten minutes without modification is not
proof that a session has ended.

The existing APU ingestion and audit APIs produced the results in project-local
state. No provider transcript, global configuration, or instruction was changed.
Claude transcripts were explicitly ingested because the audit currently
discovers existing Claude evidence, not raw Claude history. The initial
APU-only thirty-day query found just one session, so it was not used as the
basis for a broad behavioral conclusion.

## Findings and interpretation

| Existing detector | Findings | Interpretation |
| --- | ---: | --- |
| Unresolved tool request | 4 | Missing result correlations warrant inspection; they do not prove abandoned work. |
| Completion after stale gate | 4 | The detector saw a mutation after a successful test. This concerns test freshness, not the retired approval-gate measurement. |
| Repeated permission denial | 1 | Three denial events in one Claude session; authorization and intervening instructions need review before blaming retries. |
| Orphaned tool result | 2 | Results without matching requests may reflect transcript boundaries or attribution gaps. |

These eleven findings are leads, not eleven confirmed agent mistakes. No
operator incident assertions were imported, and current instruction hashes
cannot establish which instruction caused historical behavior. Test-freshness
work was not selected, in keeping with Matt's preference.

The stronger finding is in APU itself. `src/apu/evidence.py` checks for failure
text only when a Codex result is a string. Recent transcripts instead contain
lists of `input_text` blocks. Inside 68 blocks, a JSON command-result envelope
has an explicit nonzero `exit_code`, together with `chunk_id`, `output`, and
`wall_time_seconds`. All 68 were recorded as `tool.completed`, across six
Codex sessions. The count excludes nested wrappers and mere textual mentions
of an exit code. No raw output was copied into the case evidence.

A nonzero exit is a recorded process status, not proof of harmful behavior:
for example, a search can return one when it finds no matches. Correct status
capture must precede any interpretation of retries or agent effectiveness.

## Selected intervention

The ready board item `apu:structured-codex-results` will teach the Codex
evidence adapter to interpret typed result blocks and recognized command
result envelopes. It must retain only safe status metadata and hashes.

The implementation must distinguish terminal command results from running
sessions, missing status, image blocks, quoted examples, and arbitrary JSON
documents. Multiple results in one outer tool response need explicit aggregate
semantics; an outer script finishing does not guarantee that every command
it reported succeeded. Preserve legacy string handling and Claude behavior.

Existing evidence is append-only. A changed projection can create a second
event for the same source record or conflict with a prior projection. Define
replay and compatibility behavior before changing ingestion; do not silently
rewrite historical evidence or claim the old audit was corrected.

## Acceptance evidence

Use synthetic, content-free examples to verify the supported formats, then
replay the captured source boundaries into a separate project-local state
directory. The before baseline is 68 explicit nonzero command results
classified as completed. The after result must recognize all supported
envelopes in that set, retain their exit codes, and explain any deliberate
exclusions. Zero, missing, and running statuses must remain distinct from
failure. Verify replay against prior evidence as well as fresh ingestion.

The case evidence is [the history analysis JSON](2026-10-23-history-analysis.json).
It includes source-prefix hashes, record hashes and line numbers for the
68 results, aggregate audit counts, selection limits, and report hashes.
Detailed normalized evidence and audit reports remain in the ignored local
directory `build/recent-history-2026-10-23/`. Raw prompts, commands, outputs,
and credentials are not part of the tracked evidence.

This selection is complete; the importer change has not been implemented.
The separate Dias note-path exemption remains open and was not applied.

# Changelog

## Unreleased

- Add `apu behavior gate-cost` and a gate-log join in `apu behavior audit`:
  read the gate hook's decision log, join decisions to turns and tool names,
  and compute I3 (completed read-only turns while a plan was pending, per
  objective). Prompt excerpts are dropped at parse time. An audited session
  over the target gets a `read-only-turns-while-pending` finding.
- Resolve the start-of-turn gate rule gaps with a new dated intervention
  script anchored on the live hook (the 2026-09-09 render-from-backup plan
  would have discarded later operator changes): steer list without imperative
  verbs, halt words narrowed to leading or imperative use, a leading halt
  outranking the question check, the content-free gate decision log, and the
  I4 note-write exemption. Installed through `apu apply` with a receipt and
  proven on the live hook by a new proof driver (31 rows, 0 mismatches).
- Add `apu-ezpz`: mark and diagnose, in one step, a stop where the agent
  should have made a simple reversible decision itself. The incident carries
  the operator-asserted `easy-decision-gate` signal; the diagnosis and
  `apu-intervene` use the new `primary-agent-easy-decision-resume-v1`
  template (choose the default, state it, continue). Diagnoses now name the
  template the intervention will use instead of hard-coding one, and barrier
  evidence still refuses automatic intervention.
- Make the mutating `apu apply` session gate provider-neutral: `--provider
  claude-code` binds the apply to a fresh Claude Code session in the exact
  working directory; automatic selection still fails closed on ambiguity.
- The gate intervention script gains `--plan OUT_DIR`, rendering the patched
  hook and an approved APU plan (merge, full file, precondition hash) so the
  change installs through `apu apply` with a receipt and rolls back through
  `apu rollback`.
- Fix `apu-wtf` so explicit `--provider`, `--session-id`, `--cwd`, and
  `--trace-root` selectors are honored when a latest incident already exists:
  the latest incident is reused only when it matches every selector, otherwise
  a fresh incident is marked instead of failing with a provider mismatch
  against a stale incident from another provider or project. Text output now
  names the diagnosed incident and provider.
- Add `docs/cases/` with the first intervention case: the start-of-turn
  approval gate, its before state, and the definition of improvement.
- Add provider-neutral live incident attribution for Codex and Claude Code,
  including strict cross-provider ambiguity handling and `--provider` CLI
  overrides.
- Normalize bounded Claude Code transcript snapshots into content-minimized
  evidence, bind provider/session/cwd before resume, and emit fail-closed
  `claude --resume` continuations.
- Detect request substitution and treat configured permission or hook denials
  as operator-designed barriers rather than invented gates.
- Report content-free selector health per provider while retaining the 0.9
  aggregate status fields.
- Add typed, fail-closed Codex session attribution with exact cwd matching,
  bounded freshness, unique-active automatic selection, and bounded
  `no_attribution` reason codes.
- Revalidate the exact session/cwd binding before intervention and before the
  mutating `apu apply` command; enforce `durable_policy_mutation: false` in code.
- Add content-free selector health with attribution time, ambiguity count,
  heartbeat, package version, and build revision.
- Add strict evidence schema v2 selector provenance, a dual-version reader,
  version-separated storage routes, and an explicit complete-v1 writer for
  staged rollout or rollback.
- Add strict, content-addressed contracts for importing redacted Autowork
  behavior evaluation receipts and proposing review-only Lugos Orca behavior
  registry patches.
- Require exact registry revisions, canonical hashes, privacy rejection,
  `requires_review: true`, and `apply_authorized: false`; APU neither launches
  providers nor applies these proposals.

## 0.8.0 — 2026-08-14

- Add bounded behavioral audits over recent or operator-marked Codex and
  Claude Code session evidence.
- Default audits to the current project, seven days, twenty sessions, and 256
  MiB of source records, with no unbounded all-history mode.
- Detect repeated failures and denials, incomplete request/result pairs,
  post-completion activity, stale repository observations, dirty completion,
  and completion after a later mutation invalidated the last successful test.
- Verify replayable transcript evidence, preserve hook evidence as observed,
  suppress incident findings when a legitimate barrier is recorded, and bind
  findings to incident-time and current-at-audit instruction surfaces.
- Persist only safe metadata, hashes, evidence references, and detector codes;
  provider messages, reasoning, commands, tool bodies, and environment content
  remain outside APU state.

## 0.7.0 — 2026-08-14

- Add a provider-neutral, content-minimized execution evidence plane.
- Normalize Codex JSONL and lifecycle-hook inputs into invocation, result, and
  repository-state observations without retaining message, command, or result
  bodies.
- Bind Codex evidence to replay-verifiable transcript prefixes and correlate
  tool requests with results through hashed identifiers.
- Add `apu evidence` ingestion, state observation, reconciliation, and source
  verification commands.
- Attach normalized evidence references to autonomy-loss incidents and detect
  repeated identical tool failures and repeated permission denials.

## 0.6.0 — 2026-08-12

- Add the `primary-agent-autonomy-loss` Codex JSONL watcher.
- Add `apu-event`, `apu-wtf`, `apu-intervene`, and `apu-watch` console commands.
- Persist content-free incident evidence, ranked pressure-source diagnoses, and
  ephemeral intervention results without changing durable policy.

## 0.5.0 — 2026-08-07

First tagged beta release.

- Deterministic repository and machine-level instruction auditing.
- Reviewable plans, transactional apply, validation, receipts, and rollback.
- Fidelity-preserving snapshots with journaled restore and resume.
- Immutable campaigns and privacy-preserving work-order generation.
- Guidance baselines, model registry provenance, and package research.
- Capability-tested isolated Codex dispatch with staged secret handling.
- Activation-keyed efficacy policy with promotion proposals and demotion
  overrides.

Automated Claude dispatch and provider-managed package upgrades remain
fail-closed until their isolation and exact-rollback contracts can be proven.

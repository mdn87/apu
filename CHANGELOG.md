# Changelog

## Unreleased

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

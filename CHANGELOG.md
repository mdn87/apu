# Changelog

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

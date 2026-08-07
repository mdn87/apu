# APU v0.1 Implementation Plan

- **Status:** Implemented and verified on Windows, macOS, and Linux
- **Source:** [concept.md](concept.md) and [spec.md](spec.md)
- **Target runtime:** Python 3.11+
- **Primary interface:** `apu` command-line application

## 1. Goal and success criteria

Implement the deterministic, local-first APU core, its guided review flow,
transactional installation system, optional behavioral runners, and reusable
optimizer skill.

The implementation is complete when:

- all 14 acceptance criteria in the specification have executable coverage;
- audit and proposal work without an OpenAI or Anthropic API key;
- live files change only through an approved plan and recoverable transaction;
- Codex and Claude effective stacks include every supported instruction surface;
- behavioral results distinguish failure from unsupported observation and an
  unavailable runtime;
- macOS, Linux, and Windows package and focused platform tests pass;
- the installed CLI, skill, templates, receipts, and rollback path work from a
  clean environment.

## 2. Constraints and non-goals

The core and initial wizard use the Python standard library. JSON artifacts are
the stable interface between audit, proposal, review, apply, and rollback.
Provider-specific discovery and event formats stay behind adapters.

The implementation must not add:

- a required model API, hosted service, database, or daemon;
- automatic organization-wide policy enforcement;
- arbitrary Markdown rewriting;
- a dashboard or browser UI;
- a Superpowers adapter or bundled copy of Superpowers; optional integration is
  post-v0.1;
- automatic reviewer or subagent loops.

No audit command may create `APU_HOME` unless an explicit output or guided-flow
artifact is requested. No test may depend on the operator's real instruction
files, credentials, or agent session history.

## 3. Execution and review model

This is a planned, coupled implementation. A single coordinated implementation
stream is the default because the data models, provider discovery, approval
state, and transaction engine share contracts. Parallel agents are not part of
the default plan; use one only if a later task has an independent artifact and
a measurable wall-clock benefit.

Work should be committed in coherent batches, without a commit-per-test rule.
Run focused checks after each milestone and the complete suite once before
release. Perform one consolidated self-review over the complete diff.

The transaction engine modifies user-owned files and therefore warrants one
independent safety review after Milestone 3. Consolidate any fixes and recheck
once. A second review cycle requires a new failure or materially new evidence.

## 4. Core boundaries and invariants

| Boundary | Primary files | Contract |
|---|---|---|
| CLI and artifacts | `src/apu/cli.py`, `src/apu/models.py` | Stable commands, validated JSON, deterministic exit codes |
| Local state | `src/apu/state.py`, `src/apu/receipts.py` | Private `APU_HOME`, registry, installation-scoped backups |
| Discovery | `src/apu/discovery.py`, `src/apu/adapters/` | Provider-neutral surfaces and explicit relationships |
| Analysis | `src/apu/precedence.py`, `src/apu/classify.py` | Truthful effective stacks and labeled evidence strength |
| Planning | `src/apu/planning.py`, `src/apu/wizard.py` | Review decisions persist in the plan; no hidden approval |
| Mutation | `src/apu/apply.py`, `src/apu/rollback.py` | Preconditioned, drift-guarded, reversible transactions |
| Validation | `src/apu/validate.py`, `src/apu/runners/` | Structural checks always run; runtime checks are capability-aware |
| Monitoring | `src/apu/outcomes.py` | Local JSONL outcomes; no daemon or upload |

### 4.1 Artifact identity

Use one canonical JSON serializer for inventories, plans, receipts, registry
entries, outcomes, and validation results. `inventory_sha256` includes
`generated_at` and binds a plan to one exact audit artifact. Surface and receipt
content hashes handle deduplication and drift.

### 4.2 Approval state

Implement approval derivation in one function used by both `apu review` and
`apu apply`.

| Mutating operation decisions | Plan state |
|---|---|
| At least one approved; all others approved or rejected | `approved` |
| Any pending operation | `draft` |
| Any deferred operation | `draft` |
| All rejected | `draft` |
| No mutating operations | `draft` |

Apply executes only operations whose decision is `approved`. Rejected and
preserve operations remain in the artifact as review history. `--yes` may skip
the final confirmation but must never alter approval state.

### 4.3 Atomic relocation

Treat `relocate` as a review choice that the planner normalizes into one
`remove` plus one `create`. Both operations share an atomic group ID and content
hash, receive one approval decision, and must pass preflight before either can
execute. Reject malformed or partially approved groups. Rollback restores the
pair as one transaction unit.

### 4.4 Runner capability model

Each runner reports:

- detected CLI name and version;
- authentication availability without exposing credentials;
- supported normalized event types;
- invocation arguments and output format;
- per-check `passed`, `failed`, `skipped`, or `unavailable` status.

Provider event schemas are parser inputs, not shared domain models. A required
event check is skipped when the chosen runner cannot observe that event. The
deterministic file, command, and output checks still run when possible.

## 5. Milestone 1 — Read-only foundation

### Outcome

A packaged `apu audit` inventories supported Codex and Claude instruction
surfaces, computes effective stacks, emits sanitized deterministic artifacts,
and performs no unintended writes.

### Implementation

- Establish `pyproject.toml`, `src/apu/__init__.py`,
  `src/apu/__main__.py`, and the `argparse` command tree in
  `src/apu/cli.py`.
- Add `.github/workflows/ci.yml` with a GitHub Actions matrix for supported
  Python versions on Ubuntu, macOS, and Windows. Run the deterministic suite
  and package smoke checks without model credentials; later milestones extend
  the same matrix rather than creating separate release-only jobs.
- Define validated dataclasses and canonical JSON serialization for surfaces,
  import relationships, findings, inventories, validation results, and registry
  entries in `src/apu/models.py`.
- Implement `APU_HOME` resolution, private directory/file creation, registry
  loading, and explicit-output behavior in `src/apu/state.py`.
- Implement provider-neutral filesystem traversal in
  `src/apu/discovery.py`; preserve logical and resolved paths, symlink state,
  content hashes, authority, and scope.
- Implement Codex discovery for global and hierarchical `AGENTS.md`, shared
  skill directories and their metadata, explicitly supplied roots, and
  noncanonical generated/versioned cache reporting.
- Implement Claude discovery for `CLAUDE.md`, `CLAUDE.local.md`, project and
  user rules, `paths` applicability, `@path` import relationships, project and
  user skill directories, session-start hook registrations, and local
  marketplace metadata. Resolve relative imports from the containing file, cap
  recursion at the documented provider limit, detect cycles and missing
  targets, and identify orphaned APU-owned sidecars.
- Implement provider-aware ordering in `src/apu/precedence.py`, including lazy
  or conditional surfaces for each requested working directory.
- Implement deterministic structural detectors and honestly labeled pattern
  heuristics in `src/apu/classify.py`.
- Emit text and JSON audit reports. Reject `--root-session-id` without
  `--sessions` before scanning.

### Focused validation

Create synthetic home and repository trees under test-controlled temporary
directories. Cover:

- Codex and Claude hierarchy ordering;
- `CLAUDE.local.md`, user rules, `paths` rules, nested imports, cycles, missing
  imports, orphaned sidecars, project/user skills, session-start hooks, and
  local marketplace metadata;
- GitHub Actions matrix execution on Ubuntu, macOS, and Windows without
  credentials or access to the operator's real home directory;
- symlink and real-path identity without following unexpected cycles;
- identical surface hashes across repeated scans;
- distinct inventory hashes when `generated_at` differs;
- redaction and omission of prompt bodies, command arguments, environment
  values, and credential-shaped data;
- no filesystem writes from audit without explicit output;
- CLI usage failure for `--root-session-id` without `--sessions`.

Milestone 1 exits when the read-only acceptance criteria pass on POSIX and the
Windows path/mode behavior passes in the Windows GitHub Actions matrix job.

## 6. Milestone 2 — Proposal and review

### Outcome

`apu propose`, `apu review`, and the default portion of `apu init` produce a
deterministic, human-reviewable plan whose state exactly reflects persisted
operation decisions.

### Implementation

- Implement the ordered residency classifier in `src/apu/classify.py`, keeping
  deterministic, heuristic, agent-assisted, and manual findings distinct.
- Implement deterministic proposal generation, candidate rendering, exact
  preconditions, and unified diffs in `src/apu/planning.py`.
- Normalize every relocate choice into an atomic `remove` + `create` pair with
  one group ID, one shared content hash, and group-level approval propagation.
  Validate the pair before plan status can become approved.
- Implement the approval derivation contract once and reuse it for interactive
  and non-interactive review paths.
- Implement the standard-library wizard in `src/apu/wizard.py`. Present
  consequential or uncertain operations first, support accept/reject/edit/
  relocate/defer, persist every decision, and allow batch approval only for
  equivalent high-confidence operations that do not require confirmation.
- Implement `apu init` through proposal preview by default. Require explicit
  continuation for ownership selection, runtime-backed validation, or apply.
- Keep all Milestone 2 mutations limited to explicitly selected plan and report
  outputs.

### Focused validation

Use table-driven state tests for every approval combination, including:

- approved plus rejected operations yields an approved plan;
- pending or deferred operations keep the plan in draft;
- all-rejected and no-mutation plans remain draft;
- `--approve-all-recommended` leaves uncertain operations unresolved and exits
  nonzero;
- rejected and preserve operations never appear in the executable operation
  set;
- plan generation is stable for the same inventory artifact;
- stale or malformed inventory references fail before plan output;
- wizard decisions survive reload and render the same final diff;
- relocation pairs are deterministic, contain exact source and destination
  preconditions, and cannot be partially approved.

Milestone 2 exits when an operator can audit a fixture, create a plan, review it
interactively or non-interactively, and reproduce its decisions from the saved
artifact alone.

## 7. Milestone 3 — Transactional installation and rollback

### Outcome

`apu apply`, `apu rollback`, `apu status`, and structural `apu validate` safely
manage approved changes across supported platforms.

### Implementation

- Implement sidecar, managed-section, full-file, and proposal-only renderers.
  Claude sidecars use supported `@path` imports; Codex remains managed-section
  or proposal-only where no import mechanism exists.
- Implement canonical optimizer-skill installation in the Codex and Claude
  adapters. Resolve the versioned package resource or an explicit canonical
  checkout, then emit reviewed operations: a `symlink` into
  `~/.agents/skills/optimizing-agent-instructions` for Codex; a `symlink` into
  `~/.claude/skills/optimizing-agent-instructions` and, when selected, a
  separate `configure` operation for Claude local marketplace metadata. Never
  source installation from a generated/versioned cache.
- Implement preflight checks in `src/apu/apply.py`: artifact validation,
  approval derivation, target resolution, precondition hashes, platform
  capabilities, private transaction directory, backups, render, and parse.
- Commit approved operations in a deterministic order. Use `os.replace` where
  supported, bounded handling for Windows sharing violations, and reverse-order
  restoration after partial failure.
- Implement installation receipts and registry updates in
  `src/apu/receipts.py`. Registry writes occur only after a complete successful
  transaction.
- Implement drift-aware rollback in `src/apu/rollback.py`. Restore bytes and
  modes when current hashes match; remove APU-created symlinks only when they
  still point to the recorded target; leave changed objects untouched.
- Implement structural validation in `src/apu/validate.py`. With no selector,
  validate every active registry installation. Report an empty registry
  clearly without treating it as a validation failure.
- Implement `apu status` from registry and receipt data, including drift,
  provider postflight state, last validation, and monitoring progress.
- Extend `apu init` after its Milestone 2 preview so an explicit continuation
  can resolve ownership, approve/save the resulting plan, apply it, run
  structural validation and provider postflight, and display the receipt.

### Focused validation

Exercise transactions entirely in temporary roots:

- unapproved plans fail even with `--yes`;
- only approved operations execute from a mixed approved/rejected plan;
- relocation groups fail preflight when incomplete, differently approved,
  source-drifted, or destination-occupied, and a simulated mid-group failure
  restores both paths;
- null create preconditions require a missing target;
- stale content, changed symlinks, and changed managed sections fail before
  overwrite;
- failure after each transaction position restores every earlier operation;
- apply followed by rollback restores byte-identical files and meaningful
  POSIX modes;
- created symlinks are removed only when unchanged;
- Windows fixtures use copy, managed-section, or proposal-only fallback and
  omit meaningless POSIX mode assertions;
- canonical skill fixtures cover Codex and Claude symlink plans, Claude
  marketplace `configure` plans, existing user-managed targets, ignored cache
  candidates, and the reviewed Windows copy fallback;
- receipts contain hashes and backup references but no source content, prompts,
  or secrets;
- `apu validate` with no selector walks all active registry entries.

After focused tests pass, perform the single independent safety review over
preflight, commit ordering, rollback, drift handling, permissions, and
cross-platform fallbacks. Apply one consolidated correction pass if needed.

Milestone 3 exits when every simulated failure point is recoverable and no
unapproved or drifted target can be changed automatically.

## 8. Milestone 4 — Behavioral evaluation, monitoring, and packaging

### Outcome

APU can evaluate proportional agent behavior when a supported CLI is available,
record local outcomes over the monitoring window, and install as a versioned
tool with its reusable skill and templates.

### Implementation

- Define the runner interface and capability map in
  `src/apu/runners/base.py`.
- Implement Codex JSONL parsing for `codex exec --json` and Claude event parsing
  for `claude -p --output-format stream-json` with version-appropriate flags.
  Normalize only events each adapter can prove it observes.
- Implement isolated fixture execution, timeouts, cleanup, sanitized result
  capture, per-check status, and aggregate status in `src/apu/validate.py`.
- Add balanced direct, planned, delegated, high-risk, explicit-skill, and
  seeded-defect fixtures. Keep deterministic expected-output checks separate
  from runner event checks.
- Implement `apu outcome record`, `apu outcome list`, and monitoring summaries
  in `src/apu/outcomes.py`. Store append-only JSONL per installation and
  calculate progress from both elapsed days and material-task count.
- Complete the `apu init` opt-in path by offering runtime-backed behavioral
  validation when a capable authenticated runner exists, reporting honest
  skipped/unavailable states otherwise, and initializing monitoring after a
  successful installation.
- Bundle the `optimizing-agent-instructions` skill, provider templates, and
  evaluation references without embedding live user configuration.
- Add package metadata, console entry point, wheel/sdist inclusion rules, and
  concise operator documentation for `pipx` and `uv tool`.

### Focused validation

- Parse checked-in representative event streams for every supported runner
  without invoking a live model.
- Prove an unobservable delegation or review check is skipped, not failed.
- Prove an observable prohibited event fails the fixture.
- Prove no authenticated runtime produces `unavailable`, while an unsupported
  case produces `skipped`.
- Run live CLI smoke fixtures only in an explicitly configured authenticated
  environment; never provision credentials in the test harness.
- Validate outcome JSONL with partial metrics, multiple installations, 30-day
  and 10-task boundaries, and sanitized export behavior.
- Build wheel and source distributions, install each into a clean environment,
  and smoke-test `apu --help`, audit, propose, review, apply, validate,
  rollback, status, and outcome commands.
- Run the full suite on macOS, Linux, and Windows.

Milestone 4 exits when the package installs cleanly, structural checks pass
without a model, runtime-backed results are labeled honestly, and the bundled
skill and templates are present in both release artifacts.

## 9. Acceptance traceability

| Specification criteria | Milestone |
|---|---|
| 1–3: discovery, precedence, classification | 1 |
| 4–5: deterministic proposal and review | 2 |
| 6–8: approval, mutation, rollback, adapters | 2–3 |
| 9: structural and behavioral evaluation | 4 |
| 10: trace privacy | 1 and 4 |
| 11–12: platforms and dependency boundary | 1–4 release matrix |
| 13: outcomes and monitoring | 4 |
| 14: registry-wide validation | 3 |

## 10. Final verification and release gate

Before declaring v0.1 complete:

1. Run the complete deterministic suite from a clean checkout.
2. Run platform jobs for macOS, Linux, and Windows.
3. Build and install both distribution formats in clean environments.
4. Run one end-to-end fixture from audit through rollback and verify the
   original tree byte-for-byte.
5. Run available authenticated behavioral fixtures, or record them as
   unavailable without weakening structural acceptance.
6. Inspect sanitized inventories, plans, receipts, runner results, and outcomes
   for prompt, command, environment, and credential leakage.
7. Inspect the complete diff once for spec conformance, scope creep, duplicate
   abstractions, and unsupported provider assumptions.
8. Tag and publish only after the deterministic release gate passes.

Release notes must distinguish tests that passed, checks that were skipped, and
runtime evaluations that were unavailable. They must not represent absence of
an authenticated runner as behavioral success. Superpowers integration is not
part of the v0.1 release gate.

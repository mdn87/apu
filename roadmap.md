# APU v0.2+ Roadmap — System-Level Policy Optimizer

v0.1 audits one repository plus the global surfaces above it, proposes a
reviewable plan, and applies it transactionally. This roadmap grows APU into a
system that maintains the *whole machine's* agent-policy stack over time: audit
everything, inventory the changes needed, remediate them (automatically where
deterministic, via a generated work-order prompt where judgment is needed), and
re-run the entire cycle whenever a model generation, best-practices baseline,
or upstream package changes.

## Product loop

```
                 ┌────────────────────────────────────────────┐
                 ▼                                            │
trigger ──▶ refresh baseline ──▶ system audit ──▶ system plan │
(model release,  (guidance,        (global +       (auto ops +│
 package update,  package,          cascading       work-order│
 schedule,        model registry)   projects)       prompts)  │
 manual)                                              │       │
                                                      ▼       │
                                            apply / dispatch ─┘
                                            then measure outcomes
```

## Architectural stance (unchanged from v0.1, restated for scale)

1. **The deterministic core never calls a model API.** Anything requiring
   judgment — rewriting a flagged multiplier, distilling refreshed guidance,
   judging whether a new package version fixes a finding — is delegated either
   to the packaged behavioral-runner infrastructure (already capable of driving
   an authenticated Codex/Claude CLI) or to a generated, self-contained
   **work-order prompt** the user runs in a real session.
2. **Mutation of user-owned surfaces always flows through a plan, receipt,
   and rollback.** System scale changes the size of the plan, not the
   contract. APU-private state (snapshots, journals, overrides, outcome
   records) is written directly but always with provenance and visibility in
   `status` — recorded, never silent.
3. **Network access is explicit.** `refresh` and `research` commands fetch;
   audit, propose, apply, validate, and rollback stay local-first.

## New concepts

### System profile

A user-owned config (`~/.config/apu/profile.toml` / `%APPDATA%` equivalent)
declaring what "the whole system" means:

- roots to cascade into (e.g. `~/Desktop/MyDocs`), with excludes;
- global surfaces (defaults: `~/.claude`, `~/.codex`, `~/.agents`, plugin
  caches — already discovered in v0.1);
- pinned packages to track (e.g. `superpowers@claude-plugins-official`);
- guidance sources to refresh (the concept.md URL list, extensible);
- remediation policy per finding category: `auto`, `work-order`, or `ignore`.

`apu system audit` walks the profile: one machine-level inventory containing
the global stack plus a child inventory per discovered repository. Precedence
is resolved top-down so a fact fixed globally is not re-flagged in every
project; a project-level finding names the closest surface that should change.

### System plan

`apu system propose` produces one rollup plan with two sections:

- **Deterministic operations** — the v0.1 op types (merge, relocate, symlink,
  configure, remove) for changes with exactly one correct output. These are
  auto-appliable under the existing approval/receipt machinery, batched
  per-target-file, package-authority surfaces still never rewritten in place.
- **Work orders** — for findings where remediation requires judgment
  (multiplier rewrites, guidance conflicts, package upgrades with behavior
  change). Each work order is a rendered, self-contained prompt artifact.

### Campaign

A campaign is the unit that ties one trigger's proposal, snapshot, receipts,
work orders, and monitoring window together. `apu system propose` mints a
`campaign_id` and persists a campaign record in APU state binding:

- the inventory hash it was proposed from;
- the guidance-baseline version and model-registry generation in effect;
- the profile hash (so a later policy edit cannot be confused with the one
  that produced these operations);
- the plan and every generated work order (each stamped with `campaign_id`
  at generation time — not retroactively at apply).

`apu system apply` and `apu dispatch` require a campaign, record the
`snapshot_id` into the campaign record before the first mutation, and append
each receipt as it lands. Write-then-rename makes each individual update
atomic, but not concurrent updates safe — two dispatches (or an apply racing
a dispatch) could read the same revision and overwrite each other. The
campaign record therefore carries an explicit concurrency contract:

- **single writer:** mutating commands take an exclusive campaign lock held
  as an **OS-released handle** (an exclusive open/`flock`-style lock the
  kernel drops when the process dies), so a crash cannot leave the lock
  held; the pid/purpose written alongside it is diagnostic metadata, not the
  liveness mechanism. A second mutator fails fast with the holder's
  identity. If a stale lock *file* survives a crash, acquisition succeeds
  because the handle is gone; the stale metadata is overwritten and noted in
  `status`;
- **revision check as backstop:** the record embeds a monotonically
  increasing revision; a writer whose read revision is stale aborts instead
  of overwriting (compare-and-swap via rename onto a revision-named file);
- **leaf artifacts are canonical and self-describing:** every receipt and
  work-order result carries the `campaign_id`, the `snapshot_id` it executed
  under, and an idempotency key (operation id + attempt), and is written to
  its own path *before* the campaign record references it. The snapshot
  manifest reciprocally records the `campaign_id` it was taken for. The
  bindings that exist before any leaf artifact does — inventory hash,
  profile hash, baseline version, model generation — are frozen at proposal
  into an **immutable campaign manifest** (written once, never rewritten;
  the mutable record holds only progress state layered on top). A crash
  between a leaf write and the record update leaves an orphan —
  `apu system status` reconciles by scanning the campaign directory and
  re-attaching it. Rebuildability is therefore real: manifest + leaf
  artifacts reconstruct the campaign record in full; the mutable record is
  an index, never the sole authority for anything.

Monitoring windows open against the campaign, so outcome data is
attributable to exactly one set of changes.

### Work-order prompt

The closure of v0.1's known gap ("reports and stops"). A work order contains:

- the finding(s), exact file/line, and the offending text — **subject to the
  redaction rules below**;
- the relevant distilled guidance with source citations;
- explicit constraints (preserve user authority, defect detection, explicit
  triggers; scoped/proportional/evidence-backed per concept.md);
- acceptance criteria and the validation commands to run
  (`apu validate --plan …`, fixture names, seeded-defect check);
- an instruction to return the result as an APU plan candidate so the
  change still lands through review/apply/receipt — the session drafts,
  APU installs.

**Privacy contract.** Work orders are generated APU artifacts and inherit the
same emission guarantee as findings:

- a `sensitive-material-exposure` finding is **manual-only and
  non-dispatchable**: the work order references file, line, and content hash
  only, and directs the *user* — never a session — to remediate the value.
  Redacting the prompt is not enough on its own, because a dispatched session
  reads the staged file and an authenticated CLI forwards what it reads to
  its provider;
- any *other* finding on a surface marked `sensitive` in the inventory is
  dispatchable only against a **sanitized staged copy**: credential-shaped
  spans are replaced with stable placeholders (`«APU-REDACTED-1»`, keyed in
  a private map in APU state) before staging. Re-substitution at
  materialization is **structural, not blind**: each placeholder must appear
  exactly once, in its authorized location (same file, same enclosing
  context recorded at sanitization). A candidate with a missing, extra,
  duplicated, or relocated placeholder is quarantined — a model that moved a
  token has moved where a credential would land, and that is a human
  decision. After re-substitution the materialized result is scanned again;
  a secret appearing outside an original sanitized span quarantines the
  candidate. Offending text in the work order itself is likewise elided to
  file/line/hash;
- every returned plan candidate is **secret-scanned before persistence**
  (same credential-shape detectors as the classifier, plus the placeholder
  map); a candidate containing a live-looking secret that the staged input
  did not contain is quarantined, not attached to the campaign;
- work orders are written into the campaign directory in APU state with
  private permissions (owner-only where the platform supports POSIX modes,
  the same protection as receipts); `--emit-prompts DIR` exports copies and
  warns that the export target is outside APU's protection;
- APU never captures or stores session transcripts or prompts from the
  executing session — the work order (redacted, APU-authored) and the
  returned plan candidate are the only artifacts that exist.

**Mutation contract.** A work-order session never edits live surfaces.
`apu dispatch` runs the session against an isolated copy of the affected
surfaces staged into the campaign directory. Staging is defined by bytes,
not by tooling: the staged inputs must reproduce the exact content hashes
recorded in the campaign's inventory — including dirty and untracked
affected files — and staging fails closed on any hash mismatch. (A git
worktree alone does not satisfy this: it materializes committed state and
omits dirty/untracked working-tree content, so a worktree may serve as the
base but the affected surfaces are copied and hash-verified on top of it.)
For automated dispatch, live roots are protected by **OS-level write
denial**, and dispatch **fails closed without it**: before any session
starts, APU capability-tests an enforceable isolation mechanism (platform
sandbox, restricted user, read-only mount/view) by attempting a write to a
live root from the confined context and requiring it to be denied. If no
mechanism passes, `apu dispatch` reports itself unavailable and the work
order stays in the hand-run queue — it does not degrade to prompt-only
"read-only" claims. Isolation must not be achieved by mutating the live
roots' own ACLs: snapshot manifests declare ACLs out of scope, so an ACL
change APU cannot faithfully restore is not a recoverable operation.
Prompt instructions are guidance, not the enforcement mechanism. The session's sole output channel is a plan
candidate; APU diffs it against the staged copy, attaches it to the campaign
plan, and the change reaches live files only via `apu system apply` — plan,
approval, receipt, rollback, unchanged. A hand-pasted session gets the same
staged inputs and the same instruction but runs outside APU's process
control; if it edits live files anyway it has violated the contract, and
snapshot diff exists to catch exactly that (see below).

`apu system propose --emit-prompts DIR` writes one work order per remediation
group. `apu dispatch` (optional) feeds them through the behavioral-runner
infrastructure to an installed CLI instead of a human-pasted session.

### Guidance baseline

Best practices are a versioned input, not lore baked into detectors:

- `apu refresh guidance` fetches the profile's sources, stores raw snapshots
  (content-hashed, dated) in APU state, and emits a **baseline artifact**:
  per-principle entries with source URL, retrieval date, and the detector
  policies they justify.
- Detector thresholds/policies key off the baseline version, so
  "re-evaluate efficacy after a model release" is: refresh → diff baseline →
  re-run system audit → the delta report shows which findings appeared,
  disappeared, or changed severity *because the guidance changed*.
- Distilling prose guidance into baseline entries is a judgment step → it is
  itself a work order (or runner task) whose output is reviewed before the
  baseline is adopted. Deterministic diffing of adopted baselines stays in core.

### Model registry

Local state alone cannot detect a model change: `settings.json` may hold an
alias (or nothing, deferring to a provider default), and provider defaults
move without any local file changing. The registry therefore separates what
it can observe from what it must resolve:

- **Observed locally, offline:** installed runtime versions (CLI `--version`)
  and the configured model string per runtime, alias or not.
- **Resolved during `apu refresh models` (network, explicit):** each alias or
  default is resolved to a **canonical model identity** (the fully qualified
  model id) against the provider's published model listing — the authoritative
  source; each resolution is recorded with provenance: source endpoint/URL,
  retrieval date, and the raw alias it resolved from.
- **Generation** is derived from canonical identity, never from an alias
  string comparison.

Audit results are stamped with the canonical identities and registry
retrieval date they were evaluated under. Offline or failed refresh degrades
explicitly: the last-known resolution is used, stamped
`model identity unverified since DATE` — stale is visible, never presented as
current. A generation change (including one discovered only at refresh, e.g.
a moved default) is a first-class trigger and marks prior efficacy
conclusions stale until fixtures re-run.

### Package research

For tracked packages (superpowers first):

- `apu research packages` compares the pinned/installed version against
  upstream (marketplace metadata / git tags / changelog), then — locally and
  deterministically — runs the classifier over the *candidate* version's files
  and diffs finding counts against the installed version.
- If the candidate resolves findings (e.g. a newer superpowers softens the
  universal trigger), APU proposes an **upgrade operation** (a `configure`
  /pin-bump with receipt) instead of a local edit that the next update would
  clobber.
- If the candidate is worse or mixed, the diff report becomes a work order:
  "upgrade and patch," "hold and patch via marketplace fork," or "hold."
- Efficacy evidence (outcome records showing a skill misfiring) attaches to
  the research report so the recommendation is grounded in observed behavior,
  not just static findings.

### System snapshot (restore point)

v0.1 backups are per-operation: each receipt records the exact bytes of each
file an op replaced, and `apu rollback --receipt` restores them. That is the
wrong granularity for a system campaign, where one trigger produces many
operations across many surfaces. v0.2 adds a campaign-level restore point:

- `apu snapshot create [--label]` captures the full effective stack declared
  by the profile — global instruction files, project instruction files,
  `settings.json`/`settings.local.json`, hooks, skill trees, plugin pins and
  `installed_plugins.json`, marketplace metadata — as content-addressed copies
  plus a manifest. Secrets-bearing values inside settings are hashed in the
  manifest but the file bytes are stored privately in APU state, same
  protection as receipts.
- **Manifest fidelity** inherits v0.1's object hashing: every entry records
  object type (file, directory, symlink, junction), and links are captured as
  their *target path* — hashed as a link, restored as a link, never flattened
  into copied content. Empty directories are recorded. POSIX modes are
  captured where the platform supports them and restored best-effort on
  Windows (ACLs are out of scope and stated so in the manifest). Traversal
  stays within the profile's declared surfaces, does not follow links that
  escape them, and is cycle-safe (visited-identity set, same rule as
  discovery).
- **Binding.** `apu system apply` and `apu dispatch` take the snapshot
  automatically before the campaign's first mutation and record its id in the
  campaign record; the snapshot manifest records the `campaign_id` it was
  taken for, and every receipt carries both `campaign_id` and `snapshot_id`
  directly — the pairing survives loss of the campaign record. Work orders
  are stamped with `campaign_id` at generation (they exist before any
  snapshot does); the snapshot is reached through the campaign, not stamped
  retroactively into artifacts that predate it.
- `apu snapshot diff SNAP` shows everything that changed since — its purpose
  is **out-of-band drift**: hand edits, tool misbehavior, or a session that
  violated the mutation contract and wrote to live files. It is a detection
  net, not a sanctioned channel; every legitimate mutation still arrives with
  a receipt.
- `apu snapshot restore SNAP [--path P] [--force-path P]` reverts the whole
  stack or selected paths. Restore is staged and journaled, and the guarantee
  is stated honestly: swapping many targets — possibly across volumes — is
  not one atomic step, so a hard failure mid-swap *can* leave a partially
  restored stack. What restore guarantees instead is bounded, recorded
  damage: each target's replacement is prepared on the **same volume** as the
  target (so the individual swap is an atomic rename), the pre-restore bytes
  of every target are journaled before any swap begins, swaps are attempted
  in journal order, and on first failure restore automatically **reverses
  the completed swaps** from the journal. If the reversal itself fails, the
  journal records exactly which targets are in which state and
  `apu snapshot restore --resume JOURNAL_ID` completes (or `--unwind`
  reverses) from it — the state is never undocumented, but it may
  transiently be partial. Restore refuses
  targets that drifted from *both* the snapshot and the campaign's receipts
  (same drift rule as v0.1 rollback): inspect, then reconcile manually or
  override with `--force-path` per path.
- Retention is bounded (keep last N + any snapshot referenced by an open
  monitoring window); snapshots are pruned only after their campaign's
  monitoring window closes cleanly.

Per-op receipts remain the surgical tool; snapshots are the seatbelt for the
campaign as a whole and the tripwire for edits that bypassed the contract.

### Efficacy loop

Already half-built in v0.1 (outcome records, behavioral fixtures, seeded
defects). v0.2 wires it to trust — which requires outcome data that can
actually attribute results to categories. v0.1 records identify only the
installation, and a category appears only when an escaped defect is recorded:
that gives no denominator and no positive attribution, so it cannot justify
promoting anything. The outcome schema is extended:

- **`campaign_id`** — which set of changes the task ran under;
- **`categories_installed`** — the finding categories whose remediations
  were installed during this task, derived from the campaign record. This is
  context, **not a promotion denominator**: the campaign knows what is
  installed, not what a given task actually exercised, and counting every
  unrelated task would inflate the denominator and permit premature
  promotion;
- **`categories_activated`** — the categories with actual activation
  evidence for this task, populated only from sources that carry provenance:
  deterministic activation markers where the remediation admits one (e.g. a
  hook or managed section that logs its own firing to APU state), results of
  category-specific fixtures/tasks run against the change, or an explicit
  user attestation recorded with who/when. Absent evidence, the field stays
  empty — an empty field is honest and simply doesn't count toward
  promotion;
- **baseline version and model generation** in effect (stamped from the
  campaign, so conclusions invalidate cleanly when either moves);
- **fixture results** attached to the campaign (pass/fail per fixture at
  apply time and at any re-run);
- the existing latency/agent-count/review-count/remediation/rework/escaped-
  defect fields, with escaped defects carrying the category they implicate.

Promotion and demotion are explicit, not vibes:

- every applied campaign opens a monitoring window (≈30 days / 10 material
  tasks, per concept.md);
- **promotion** of a category from `work-order` to `auto` requires: its
  remediation is deterministic; ≥ N **distinct activation events** (default
  10), where uniqueness is `(campaign_id, task_id, category,
  activation_source_id)` — repeated records for one task, or one activation
  reported by several sources, count once; fixture re-runs are validation
  evidence, not material tasks, and never increment the count; the events
  span ≥ 1 closed window; zero escaped defects implicating the category;
  fixtures green at window close. Promotion is a profile edit APU
  *proposes* — the user approves it like any other operation. A category
  whose remediation admits no activation evidence cannot be promoted by
  task counting alone; it needs category-specific fixtures or attestation;
- **demotion** is a fail-safe with a defined authority layer, not a silent
  edit to the user-owned profile. One escaped defect implicating a category
  writes a **demotion override** into APU state: provenance (the outcome
  record and campaign that triggered it), timestamp, and effect. The
  override suppresses `auto` for that category in all future campaigns and
  flags any open campaign that auto-applied it for review. The profile file
  is untouched; `apu system status` shows the override laid over the
  profile, and clearing it is a reviewed operation (the user approves the
  reinstatement, with the triggering evidence in front of them) — the same
  plan/receipt discipline as every other mutation;
- a regression re-tightens the specific weak rule via a new targeted
  campaign — never by restoring the entire previous workflow.

## Command surface (target)

```text
apu system audit   [--profile PATH] [--json OUT]
apu system propose --inventory OUT [--emit-prompts DIR] [--output PLAN]
apu system apply   PLAN [--auto-only]
apu refresh guidance | models
apu research packages [NAME]
apu dispatch WORK_ORDER [--runner codex|claude]
apu system status   # drift, baseline version, model generation, monitoring
apu snapshot create [--label] | diff SNAP | list
apu snapshot restore SNAP [--path P] [--force-path P]
apu snapshot restore --resume JOURNAL_ID [--unwind]   # complete or reverse
                                                      # an interrupted restore
```

(`JOURNAL_ID` is printed when a restore is interrupted and listed by
`apu system status`; it names the journaled transaction, which records the
snapshot, targets, and each target's current state.)

Existing single-repo commands remain; `system` composes them.

## Milestones (continuing v0.1 numbering)

### M5 — System profile, cascading audit, and snapshots
Profile schema; repo discovery under roots with excludes; machine inventory
with per-repo children; top-down precedence so global facts aren't re-flagged
per project; rollup report; snapshot create/diff/restore/list with the
manifest-fidelity rules (object types, link targets, staged restore) and
retention. *Deterministic and offline. Audit, create, diff, and list write
only into APU state; `snapshot restore` is the milestone's one mutation of
live surfaces, and only via its journaled swap-and-reverse protocol.*

### M6 — Campaigns and work-order generation
Immutable campaign manifest + mutable index with lock/revision contract and
orphan reconciliation; remediation-policy table per
category; work-order renderer (finding + guidance citation + constraints +
acceptance criteria + plan-candidate return instructions) with the redaction
rules and private-permission emission; `--emit-prompts` export warning; plan
sections split auto vs work-order; `apply --auto-only`.

### M7 — Guidance baseline and model registry
Snapshot fetcher with hashing/dating; baseline artifact schema; baseline diff;
canonical model identity with alias resolution, provenance, and stale-offline
degradation; audit stamping with baseline + model generation; `refresh`
commands; distill step emitted as a work order.

### M8 — Package research and upgrade ops
Version detection from plugin caches/marketplace metadata; upstream check;
candidate-version classifier diff; upgrade/pin operations with receipts;
research report with efficacy evidence attached.

### M9 — Efficacy-gated automation and dispatch
Extended outcome schema (campaign, categories-installed vs
categories-activated with activation evidence, baseline, generation, fixture
results); monitoring windows bound to campaigns; promotion thresholds keyed
to activation; demotion-override layer in APU state with reviewed clearing;
`apu dispatch` through the runner layer — hash-verified staging including
dirty/untracked surfaces, capability-tested OS-level write denial (dispatch
unavailable when unenforceable), sanitized copies with structural
placeholder verification, secret-scanned plan-candidate return;
end-to-end fixture: model release → refresh → audit delta → work orders →
apply → fixtures still catch the seeded defect.

### M10 — Live behavioral pressure watch
Codex JSONL support for the single `primary-agent-autonomy-loss` watcher;
strict selection of one fresh active session at the exact normalized cwd;
typed `no_attribution` results for missing, stale, ambiguous, unparsable, or
cross-project traces; `apu-event` incident
marking; `apu-wtf` compact diagnosis with active instruction and harness
surface hashes; and `apu-intervene` ephemeral resume or continuation through
the supported Codex resume interface. The command records whether an executed
non-interactive continuation completed, and accepts an operator result for an
interactive continuation. `apu-watch` lists, enables, or disables the watcher
without starting a daemon. Durable policy changes remain exclusively in the
existing plan/review/apply path.

Intervention revalidates the incident's exact session/cwd binding immediately
before resume and checks `durable_policy_mutation: false`. The mutating
`apu apply` command uses the same binding gate. Watcher health reports selector
mode, successful-attribution time, ambiguity count, heartbeat, package version,
and build revision without trace content.

M10 artifacts are private and content-minimized: the operator's explicit event
description may be retained after credential-shape rejection, but nearby
message bodies, reasoning, tool inputs and outputs, base instructions, and
environment content never enter APU state. Evidence consists of record ranges,
event types, hashes, counts, detector codes, safe runtime-setting labels, and
active surface paths/hashes. A possible real credential/permission barrier,
destructive or external side effect, or material information gap suppresses
automatic intervention.

### M11 — Provider-neutral execution evidence

Add a strict, append-only evidence contract for invocation, result, and
repository-state observations. Codex JSONL records are normalized behind an
adapter and bound to a byte-counted transcript prefix so later appends do not
invalidate earlier evidence. Lifecycle-hook JSON can be projected through the
same contract for providers such as Claude Code. Unknown fields and all message,
reasoning, command, tool-input, tool-output, and environment bodies are discarded
before persistence.

Schema v2 records selector provenance and live under a version-specific route.
The current reader accepts strict v1 and v2 objects, and the producer can emit a
complete v1 object for staged deployment or rollback without putting v2 data in
the legacy route.

`apu evidence` ingests Codex sessions or hook events, records optional independent
Git state, correlates tool requests and results through hashed identifiers,
detects repeated identical failures and permission denials, and verifies replayable
source references. Behavior incidents carry evidence references and distinguish
operator assertions from observed execution metadata. The evidence layer does not
archive raw transcripts, prove semantic task correctness, schedule task graphs,
or bypass the existing outcome and policy-promotion controls.

### M12 — Bounded behavioral session audit

Add `apu behavior audit` as the retrospective consumer of M11 evidence. Its
default scope is the current project, seven days, twenty sessions, and 256 MiB
of source data. Incident-marked sessions are selected before ordinary recent
sessions and may bypass the age filter; explicit session selection also bypasses
age. Neither path bypasses the source-byte limit, and no all-history option is
provided. At most 1,000 files per provider are opened for metadata inspection,
with exact and incident-marked sources prioritized before recency.

The deterministic detector set covers repeated identical tool failures,
repeated permission denials, unresolved or orphaned request/result pairs,
post-completion activity, stale or dirty state at completion, and completion
after a mutation invalidated the last successful test. Operator incident signals
remain `asserted`; lifecycle-hook facts remain `observed`; replayable Codex
records become `verified` only when their transcript prefix and source record
still match. Known legitimate barriers suppress incident findings.

The report binds incident-time surfaces separately from current-at-audit
surfaces and stores only safe metadata, hashes, references, counts, and detector
codes. It neither archives nor deletes provider logs. Thirty days or closure of
a linked monitoring window is recorded as the detailed-event retention target;
automated pruning requires its own reviewed implementation.

Each milestone ships behind the existing gates: full test suite, structural
validation, byte-for-byte reversibility, and no secret content in any emitted
artifact — for work orders specifically, the redaction rules above are part
of that gate, and it is tested by seeding a credential-shaped value and
asserting it never appears in a rendered work order.

## Standing risks

- **Judgment leakage into core** — the moment core "just tweaks" a rewrite,
  determinism and testability are gone. The work-order boundary is the spec.
- **Guidance-source drift/rot** — snapshots are content-hashed and the
  baseline records retrieval dates; a dead URL degrades to "stale baseline"
  warnings, never silent reuse presented as current.
- **Auto-apply overreach** — the v0.1 lesson (deleting fence lines) at machine
  scale would be destructive. Categories start as `work-order`; `auto`
  requires both deterministic remediation *and* accumulated outcome evidence.
- **Package upgrades as regressions** — an upgrade op is still a plan op:
  preconditions, receipt, rollback, and a monitoring window like any other
  change.

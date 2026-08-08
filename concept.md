# APU — Agent Policy Updater

## Concept

APU is a local-first tool and reusable agent skill for auditing, simplifying,
installing, and evaluating durable coding-agent instructions.

Its purpose is to keep `AGENTS.md`, `CLAUDE.md`, skills, hooks, and related
configuration effective as frontier models change. It targets a recurring
failure mode: instructions written to compensate for older or weaker models
become counterproductive when newer models follow them too aggressively.

Examples include:

- invoking a workflow skill at the start of every conversation;
- requiring design approval for routine edits;
- decomposing plans into two-to-five-minute actions;
- creating an implementer and multiple reviewers for every task;
- repeatedly reopening hypothetical edge cases;
- treating test-first ceremony as more important than behavior protection;
- adding abstractions, validation, or fallback paths for imagined futures.

APU should remove process that no longer improves outcomes while preserving
safety, defect detection, user authority, and repository-specific knowledge.

## Motivating evidence

The initial reference case is the August 2026 BWA Orca implementation:

- 44 Codex sessions and 43 child agents;
- 17.11 hours elapsed;
- 1,272 agent waits and 320 agent messages;
- approximately 2.14 billion cumulative model tokens, mostly cached input;
- 82 commits, including 63 fix commits;
- 40,382 inserted lines across 44 files.

The workflow had expanded into repeated implementer, specification-review, and
quality-review cycles even though the user and workspace guidance asked for
limited complexity and occasional review. Most of the orchestration had already
occurred by the time the user identified the token problem.

This is evidence of a specific process failure, not a universal benchmark. APU
uses traces like this to identify pressure multipliers, then validates proposed
changes against balanced scenarios so lower token use does not become the only
goal.

## Product thesis

Durable agent policy should be:

1. **Scoped** — a rule lives at the narrowest level where it is true.
2. **Proportional** — process increases with observed complexity and
   consequence, not every imaginable edge case.
3. **Evidence-backed** — new durable rules address repeated mistakes, external
   requirements, or measured failures.
4. **Inspectible** — every proposed mutation is represented as a human-readable
   plan and diff.
5. **Reversible** — installation records exact backups, hashes, and created
   links.
6. **Provider-aware but not duplicated** — shared behavioral policy is
   canonical; platform adapters contain only syntax and installation details.
7. **Local-first and private** — source files, traces, and secrets remain on the
   machine unless the user deliberately exports a sanitized report.

## What stays in place

APU does not treat every useful instruction as package content. It applies a
residency test:

| Instruction or artifact | Default residence |
|---|---|
| Personal defaults that should affect every task | Global agent instructions |
| Build commands, architecture, and repository conventions | Closest repository instruction file |
| Reusable conditional method | Skill |
| Mechanical invariant | Hook, permission, linter, or test |
| One-off constraint | Current prompt |
| Credentials, permissions, machine paths, and installed state | Local configuration only |
| Session traces and Git history | Scanned in place; never packaged |
| Facts the model reliably discovers from the repository | Usually omitted |

Each item is also classified by:

- authority: user, repository, package, or generated;
- scope: global, workspace, repository, subtree, or task;
- durability: stable rule or changing fact;
- sensitivity: portable or local/private;
- evidence: observed failure, external mandate, user preference, or speculation;
- enforceability: advisory prose or deterministic mechanism.

The possible recommendations are:

- keep in place;
- move closer to a repository;
- extract into a portable skill;
- convert into deterministic enforcement;
- replace with a portable template;
- remove as stale, duplicated, or inferable;
- leave for an explicit manual decision.

## User experience

APU has a deterministic command-line core and a thin interactive wizard.
The wizard is not a black box: it creates the same plan artifact accepted by
the non-interactive commands.

Primary modes:

1. **Audit** — discover effective instructions and report findings without
   writing.
2. **Propose** — produce classified recommendations, candidate files, and
   unified diffs.
3. **Review** — interactively accept, reject, edit, or relocate operations.
4. **Apply** — transactionally apply an approved plan.
5. **Validate** — test structural integrity, restrained activation, and defect
   detection.
6. **Rollback** — restore an installation from its receipt.
7. **Status** — show managed surfaces, drift, and monitoring progress.

The wizard should emphasize consequential or uncertain decisions rather than
asking the user to approve every obvious read-only classification.

## Behavioral pressure watch

APU should audit behavior that occurred during real agent runs, not only the
static policy that might have influenced it. A behavioral watcher is a small,
named detector for one unwanted pattern. It reads existing logs and the
effective instruction, skill, hook, tool, and subagent setup, identifies a
likely pressure source, and offers an intervention that can be tried immediately.

The first watcher is `primary-agent-autonomy-loss`. It looks for a primary
operating agent that:

- asks the user for information already available in context or repository
  conventions;
- asks the user to choose a reasonable, reversible default;
- stops before satisfying the task without a real external blocker;
- creates a new prerequisite or approval gate and then treats it as mandatory;
- treats a minor command or tool failure as terminal without trying an available
  fallback;
- delegates work but does not integrate the result and finish the parent task;
- transfers an action to the user that the agent has the tools and permission to
  perform.

It should not flag a real permission or credential barrier, a destructive or
external side effect, an explicit user-requested approval point, or missing
information that materially changes the requested result.

APU does not need to prove hidden model reasoning. A useful case needs only:

1. the observed blocker, question, or premature stop;
2. the active behavior-shaping surfaces at that moment;
3. a short ranked list of likely pressure sources;
4. the result of any live intervention attempted against the case.

The intervention result is more useful than an elaborate causal argument. If a
small temporary change lets the agent continue and complete the task, that is
strong evidence that the changed surface or behavior was involved.

### Memorable live commands

The common path should not require the operator to find session IDs or prepare
an audit bundle:

```console
apu-event "asked me to approve a reversible filename choice"
apu-wtf
apu-intervene
```

An optional watcher command keeps configuration equally direct:

```console
apu-watch
apu-watch autonomy-loss
```

- `apu-event` marks an incident against the most recent active session by
  default and captures only the nearby event range and effective surface hashes
  needed to inspect it.
- `apu-wtf` analyzes the marked event, or the most recent incomplete run when no
  event is marked, and prints one compact diagnosis with evidence and likely
  pressure sources.
- `apu-intervene` attempts the smallest temporary correction in the active
  session or its immediate retry, then records whether the agent resumed and
  completed the blocked work.
- `apu-watch` lists, enables, or disables watchers used by audits. It does not
  start a background service.

These may be thin console entry points over ordinary APU internals. The short
operator-facing names are part of the product rather than examples that later
expand into longer required syntax.

### In-situ intervention

`apu-intervene` is a live recovery action, not a durable policy rewrite. Based
on the diagnosed case, it may:

- send a concise resume instruction to the primary agent;
- add a temporary instruction overlay to make reasonable reversible decisions;
- skip one optional skill or hook for the retry;
- return a child agent's uncertainty to the primary agent instead of the user;
- select an available fallback tool or command path;
- retry the blocked step with the suspected pressure source narrowed.

If the current harness can resume or inject into the session, APU should use
that route. If it cannot, it should produce the shortest continuation command or
prompt supported by the adapter. The intervention and result are recorded, but
no global or repository policy is changed.

A successful intervention becomes a candidate for a narrow durable correction
at the next audit. The operator still chooses whether to remove, narrow,
relocate, or rewrite the responsible instruction, skill, hook, tool, or
subagent contract through the normal APU plan and apply path.

### Deliberately small first release

The first implementation should remain useful even if most of the broader idea
is deferred:

- support Codex JSONL traces first;
- ship only the `primary-agent-autonomy-loss` watcher;
- handle one marked event or recent run at a time;
- select the latest relevant session automatically in the normal case;
- use one evaluator when semantic judgment helps;
- prefer an alternate model evaluator, but allow the same model when that is
  what is available and label the result as self-evaluated;
- test against real incidents first rather than requiring a fixture suite before
  use;
- add a synthetic fixture only after a failure recurs or a durable rule is being
  promoted;
- report a likely source instead of waiting for formal causal certainty;
- make no automatic durable policy changes.

The first release does not need a model jury, generalized agent-event ontology,
background daemon, dashboard, full cross-provider support, or automatic
counterfactual experiment system. Those features should not block the live
mark, diagnose, and intervene loop.

The first release is complete when:

1. `apu-event` can mark a real Codex incident without manual session lookup;
2. `apu-wtf` can connect the incident to the active policy and harness surfaces
   and produce a compact ranked diagnosis;
3. `apu-intervene` can attempt a temporary recovery and record the result;
4. the flow works on at least one real autonomy-loss case without modifying
   durable configuration.

Codex Sol should plan this first release only. It should extend APU's existing
trace, inventory, runner, and outcome machinery rather than redesigning the
core. Any proposed milestone beyond this live loop should be explicitly marked
as deferred and should not appear on the critical path.

## Proportionality ladder

APU’s default policy uses three tiers:

### Direct

For small, local, reversible work. Act directly, run a focused check, and
inspect the result. Do not automatically create a formal design, worktree,
subagent, reviewer, or commit.

### Planned

For material ambiguity, coupled multi-component changes, or meaningful
sequencing. Use coherent testable milestones, behavior-focused validation, and
one consolidated self-review.

### Delegated or independently reviewed

For genuinely independent workstreams, useful context isolation, specialized
expertise, explicit user requirements, or major/high-risk uncertainty.
Use the smallest set of non-overlapping roles. One review/fix/recheck cycle is
the default; another requires new evidence.

If the tier remains genuinely uncertain after inspecting context, choose the
higher tier and record why. Imagined edge cases alone do not raise the tier.

## Package boundary

The portable APU package owns:

- the audit and planning engine;
- the interactive wizard;
- the `optimizing-agent-instructions` skill;
- provider adapters;
- global and repository instruction templates;
- balanced evaluation fixtures;
- install, validation, receipt, and rollback logic.

It does not own:

- the user’s complete live global instruction files;
- repository instructions;
- credentials or permission configuration;
- model session logs;
- versioned plugin caches;
- the Superpowers source tree.

Superpowers is an optional integration target. APU can audit or patch a
canonical Superpowers checkout and configure a supported runtime to use that
source, but it does not embed a private copy of the framework.

## Installation philosophy

Prefer, in order:

1. a sidecar/import when the runtime supports imported guidance;
2. a marked managed section within a user-owned file;
3. complete generated-file ownership only when explicitly granted;
4. a proposal-only diff when safe automatic merging is unavailable.

APU never silently replaces a user-owned instruction file.

## Evaluation

Every policy revision is tested for both efficiency and rigor:

- a trivial documentation or configuration edit stays direct;
- a localized bug receives focused regression evidence;
- coupled multi-file work gets a concise plan without a review army;
- independent read-heavy work may delegate;
- a security-sensitive boundary escalates appropriately;
- an explicitly requested skill or review is honored;
- a seeded realistic defect is still detected.

Structural checks and deterministic fixture validation require no model.
Behavioral fixtures run through a supported local agent runtime, such as Codex
or Claude Code, when one is installed and authenticated. If no runtime is
available, APU reports those checks as unavailable rather than treating them as
passed; audit, proposal, installation, rollback, and structural validation
remain fully usable.

Behavioral pressure watchers are tested live first. A single operator-marked
event is enough to diagnose and try an ephemeral intervention. A successful
intervention is evidence for a durable change, not a requirement to build a
larger evaluation system before the feature can be used.

For automatic promotion of a policy category, APU may still use a longer
monitoring window and repeated material tasks. That threshold does not block
`apu-event`, `apu-wtf`, `apu-intervene`, or a human-reviewed direct correction.
A regression tightens the specific weak rule rather than restoring the entire
previous workflow.

## Success measures

- No workflow activates only because a conversation started.
- No default policy creates a reviewer after every task.
- Instruction files become shorter without losing repository-specific facts.
- Installations are previewable and byte-for-byte reversible.
- Audit reports do not expose prompt bodies or secrets.
- Representative tasks require fewer agent roles and review cycles.
- Seeded real defects remain detectable.
- A real incident can move from `apu-event` to diagnosis and live intervention
  without manual session archaeology.
- A successful intervention can be converted into one narrow durable change
  instead of another broad instruction layer.
- Core package operation works without an OpenAI or Anthropic API key;
  optional behavioral evaluations may use an already installed agent runtime.

## Non-goals

- A dashboard, hosted service, or policy database
- A general-purpose agent observability platform
- A background watcher daemon for the first release
- A model jury or consensus requirement for ordinary diagnosis
- Proving a model's hidden reasoning before trying a practical intervention
- Autonomous rewriting of every repository on a machine
- Replacing provider permission or sandbox systems
- Eliminating planning, tests, review, skills, or delegation
- Optimizing solely for tokens at the expense of correctness
- Managing production deployments or application infrastructure
- Automatically publishing private traces or instruction contents

## Guidance baseline

The concept follows current provider guidance that durable instruction files
should remain concise, planning should be used for difficult or ambiguous
tasks, verification should be executable and relevant, subagents should handle
independent work, and aggressive prompting written for older models can
overtrigger newer ones:

- <https://learn.chatgpt.com/guides/best-practices>
- <https://learn.chatgpt.com/docs/agent-configuration/agents-md>
- <https://learn.chatgpt.com/docs/agent-configuration/subagents>
- <https://code.claude.com/docs/en/best-practices>
- <https://code.claude.com/docs/en/sub-agents>
- <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices>
- <https://www.anthropic.com/engineering/building-effective-agents>

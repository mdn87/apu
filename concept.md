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

For roughly 30 days and at least 10 material tasks after installation, APU
tracks user-supplied or locally derived outcome summaries: latency, agent and
review counts, remediation, rework, and escaped defects. A regression tightens
the specific weak rule rather than restoring the entire previous workflow.

## Success measures

- No workflow activates only because a conversation started.
- No default policy creates a reviewer after every task.
- Instruction files become shorter without losing repository-specific facts.
- Installations are previewable and byte-for-byte reversible.
- Audit reports do not expose prompt bodies or secrets.
- Representative tasks require fewer agent roles and review cycles.
- Seeded real defects remain detectable.
- The package works without requiring an OpenAI or Anthropic API key.

## Non-goals

- A dashboard, hosted service, or policy database
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

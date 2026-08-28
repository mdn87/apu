---
name: optimizing-agent-instructions
description: Audits and revises AGENTS.md, CLAUDE.md, agent skills, hooks, and orchestration guidance using current official model guidance plus local trace evidence. Use when agent workflows are slow, token-heavy, over-orchestrated, overengineered, review-loop prone, or weak at proportionate testing and self-review.
---

# Optimizing Agent Instructions

Reduce process that no longer improves outcomes while preserving defect
detection, safety, and user control. Base changes on current official guidance,
instruction precedence, and observed runs—not taste alone.

## Workflow

1. **Map instruction surfaces.** Locate global and project
   `AGENTS.md`/`CLAUDE.md`, skill metadata and bodies, session hooks, plugin
   injection, and closer repository rules. Record precedence and symlinks.
2. **Establish evidence.** Use user complaints, duration, tool/agent counts,
   remediation loops, commit churn, test-to-implementation ratio, and escaped
   defects. Run `scripts/analyze_agent_instructions.py --help`; its trace mode
   emits metadata and counts, not prompt content.
3. **Check current guidance.** Consult current official OpenAI and Anthropic
   documentation because frontier-model prompting advice changes. Prefer
   primary sources. Separate provider syntax from shared principles.
4. **Find pressure multipliers.** Flag universal skill triggers, mandatory
   design gates, microtask plans, per-task reviewer loops, aggressive priority
   language, speculative edge-case requirements, and test rituals detached
   from product risk.
5. **Choose a proportional tier.**
   - Direct: small, local, reversible work with a focused check.
   - Planned: real ambiguity, multiple coupled components, or meaningful
     sequencing.
   - Delegated/reviewed: materially independent work, useful context isolation,
     specialized expertise, explicit requirements, or major/high-risk
     uncertainty.

   If evidence leaves the tier genuinely uncertain, choose the higher tier and
   state why. Imagined edge cases do not raise the tier.
6. **Revise the highest-precedence multipliers first.** State each rule once,
   use observable triggers, retain explicit user requests, and keep project
   facts near the project.
7. **Validate both restraint and rigor.** Run structural checks and the balanced
   scenarios in
   [references/evaluation-scenarios.md](references/evaluation-scenarios.md).
   Seed one realistic defect so reduced ceremony must still detect a problem.
8. **Install safely.** Preserve intentional symlinks, avoid generated caches
   when a canonical source exists, and inspect the final diff.
9. **Monitor.** Review at least 10 material tasks over roughly 30 days. Track
   latency, agent/review counts, token usage when available, rework, and escaped
   defects. Re-tighten the specific weak gate if quality regresses.

## Circuit breaker

Pause before adding an unplanned milestone, agent role, subsystem, or second
remediation cycle. Continue only when new evidence or a distinct responsibility
justifies it.

## Deliverables

- Evidence-backed findings with source locations
- A concise revised instruction set with explicit non-triggers
- Validation covering efficiency and defect detection
- A reversible installation or migration note
- A time-bounded monitoring plan

Do not build a dashboard, database, plugin, or broad policy framework unless
the user asks for one.

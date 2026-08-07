# Incident: batch skill rewrite dropped closing frontmatter delimiter

- **Date observed:** 2026-08-06 (Codex CLI startup)
- **Date of mutation:** 2026-08-06 02:51:44 (all files share this mtime — single batch action)
- **Status:** logged; files not yet repaired at time of writing
- **Class:** auto-apply overreach — same category as the v0.1 fence-line-deletion
  lesson recorded in roadmap.md "Standing risks"

## Symptom

Codex CLI reported on startup:

```
⚠ Skipped loading 15 skill(s) due to invalid SKILL.md files.
⚠ <path>\SKILL.md: missing YAML frontmatter delimited by ---
```

## Scope

15 SKILL.md files, all mutated at 2026-08-06 02:51:44:

- `~/.agents/skills/prompt-master/SKILL.md` (1 file)
- `~/.codex/superpowers/skills/*/SKILL.md` (14 files): brainstorming,
  dispatching-parallel-agents, executing-plans, finishing-a-development-branch,
  receiving-code-review, requesting-code-review, subagent-driven-development,
  systematic-debugging, test-driven-development, using-git-worktrees,
  using-superpowers, verification-before-completion, writing-plans,
  writing-skills

## Root cause

A batch instruction-optimization pass rewrote the `description:` value in each
file's YAML frontmatter and, in every file, removed the closing `---`
delimiter. The frontmatter now opens with `---` and runs straight into the
Markdown body. Codex CLI requires frontmatter closed by a second `---` and
skips the skill entirely; Claude Code parses the malformed files leniently and
kept loading them, so the damage was visible only from Codex.

## Attribution

The edit is attributed to an APU-related agent action rather than the APU CLI
core: no `%LOCALAPPDATA%\apu` state directory exists on this machine, so no
plan, receipt, or transaction was recorded. Nothing in `src/apu` rewrites
frontmatter. Consequence: **no receipt means no `apu rollback` path** — the 15
files must be repaired by re-inserting the closing `---` (or restoring from
upstream copies).

## Lessons for APU design (feeds spec §2.2 "user-provided incident notes")

1. Any rewrite of a `SKILL.md` must be followed by a structural re-parse of
   the frontmatter as a validation gate — ideally against the strictest
   consumer (Codex), not the most lenient (Claude Code).
2. Mutations executed by agents on APU's behalf must still flow through the
   transactional pipeline (plan → apply → receipt); an unreceipted batch edit
   left no rollback path.
3. Cross-CLI parse checking belongs in `apu validate`: a file can be broken
   for one runner while remaining silently loadable in another.
4. Reinforces the roadmap decision that rewrite categories start as
   `work-order`, with `auto` requiring deterministic remediation plus outcome
   evidence.

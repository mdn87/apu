"""Resolve the gate rule gaps: the 2026-09-23 start-of-turn gate intervention.

Case record: docs/cases/2026-09-09-start-of-turn-gate.md, "Gate rule gaps".
Target: ~/.claude/hooks/speak-response.mjs, gate mode only.

The live hook moved on after the 2026-09-09 intervention (conversational
prompts, a "chat" state, more voice plumbing), so the 2026-09-09 script's
render-from-backup plan would now discard that work. This script anchors on
the live text as of 2026-09-23 and changes four things:

1. Steer list narrowed (operator decision 2026-09-09): imperative verbs no
   longer let a long new objective inherit an approval. "now build ..." arms
   the gate; "ok, also ..." and short steers still carry the approval forward.
2. Halt words narrowed: "wait", "don't", and "do not" withdraw approval only
   when they lead the message or form an imperative aimed at the work ("do not
   proceed", "don't push that"). "we do not have routing yet" no longer re-arms.
   "stop", "halt", "hold on", "hold off", "cancel", "abort", "never mind",
   "undo", and "revert" still count anywhere.
3. Content-free gate decision log: one latency-log line per state write.
4. I4 note exemption: while the gate is armed, Write/Edit to the working
   directory's .claude/handoff.md, .claude/handoff-history/, .claude/notes/,
   or incidents/ is allowed. Nothing else changes in the deny path.
5. A leading halt outranks the question check: "do not proceed" used to read
   as a question (the question list starts with "do") and left the approval
   standing. Found by this script's own fixture test.

Usage:
    python scripts/gate_intervention_2026_09_23.py --check
    python scripts/gate_intervention_2026_09_23.py --plan build/gate-rule-gaps
    apu apply build/gate-rule-gaps/gate-rule-gaps-plan.json --provider claude-code

There is deliberately no --apply: the durable path is apu apply with a
receipt, and apu rollback reverses it byte for byte.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

HOOK = Path.home() / ".claude" / "hooks" / "speak-response.mjs"
MARKER = "Gate rule gaps (2026-09-23 gate intervention)"

STEER_LEAD_OLD = (
    "const STEER_LEAD = /^(ok|okay|yes|yep|yeah|yup|sure|fine|right|good|great|cool|also|and|then|now|next|"
    "plus|keep|continue|just|actually|instead|but|oh|hmm|no|nope|not that|the|that|this|it|use|try|make|"
    "change|update|fix|add|remove|drop|skip|include|check|run|rerun|retry|again|same|more|less|put|move|"
    "rename|swap|switch|start|finish|wrap|land|commit|push|test)\\b/;\n"
)
STEER_LEAD_NEW = (
    "// Affirmations, continuations, and short steers only. Imperative verbs were\n"
    "// removed (operator decision 2026-09-09, installed by the " + MARKER + "):\n"
    "// a message that opens a new objective (\"now build ...\", \"make a ...\") must\n"
    "// not inherit an approval.\n"
    "const STEER_LEAD = /^(ok|okay|yes|yep|yeah|yup|sure|fine|right|good|great|cool|also|and|"
    "plus|keep|continue|just|actually|instead|but|oh|hmm|no|nope|not that|the|"
    "that|this|it|same|again|more|less)\\b/;\n"
)

HALT_OLD = (
    "const HALT = /\\b(stop|halt|wait|hold on|hold off|cancel|abort|don'?t|do not|"
    "never ?mind|undo|revert)\\b/;\n"
)
HALT_NEW = HALT_OLD + (
    "// " + MARKER + ": \"wait\", \"don't\", and \"do not\"\n"
    "// used to withdraw approval from anywhere in a message, so a statement such as\n"
    "// \"we do not have routing yet\" re-armed the gate. They now count only where they\n"
    "// read as a command to Claude: leading the message, or in an imperative aimed at\n"
    "// the work (\"do not proceed\", \"don't push that\"). The other halt words count anywhere.\n"
    "const HALT_HARD = /\\b(stop|halt|hold on|hold off|cancel|abort|never ?mind|undo|revert)\\b/;\n"
    "const HALT_SOFT_LEAD = /^(wait|don'?t|do not)\\b/;\n"
    "const HALT_SOFT_IMPERATIVE = /\\b(don'?t|do not)\\s+(proceed|continue|go|do|run|push|commit|apply|"
    "touch|change|edit|write|delete|deploy|merge|ship|build|make|install|send|execute|start|that|this|it|yet)\\b"
    "|\\b(please|just) wait\\b|\\bwait\\s+(a |one )?(sec|second|minute|moment|up)\\b/;\n"
    "function isHalt(low) {\n"
    "  const t = String(low || '').replace(/^[\\s,.!?:;-]+/, '')"
    ".replace(/^(hey |ok |okay |alright |all right )?claude[,.!? ]+/, '');\n"
    "  return HALT_HARD.test(t) || HALT_SOFT_LEAD.test(t) || HALT_SOFT_IMPERATIVE.test(t);\n"
    "}\n"
    "// " + MARKER + ", I4: a local note is not the kind of\n"
    "// mutation the gate holds. Write/Edit under the working directory's .claude/handoff.md,\n"
    "// .claude/handoff-history/, .claude/notes/, or incidents/ passes while the gate is\n"
    "// armed. Paths are compared after normalizing separators; anything outside the\n"
    "// working directory, or reaching up through .., stays denied.\n"
    "const NOTE_TOOLS = ['Write', 'Edit'];\n"
    "const NOTE_PATHS = ['.claude/handoff.md', '.claude/handoff-history/', '.claude/notes/', 'incidents/'];\n"
    "function isNoteWrite(tool, data) {\n"
    "  if (!NOTE_TOOLS.includes(tool)) return false;\n"
    "  const input = (data && data.tool_input) || {};\n"
    "  const file = String(input.file_path || '');\n"
    "  const cwd = String((data && data.cwd) || '');\n"
    "  if (!file || !cwd) return false;\n"
    "  const norm = p => p.replace(/\\\\/g, '/').replace(/\\/+$/, '').toLowerCase();\n"
    "  const c = norm(cwd) + '/';\n"
    "  let f = norm(file);\n"
    "  if (!/^([a-z]:)?\\//.test(f)) f = c + f;\n"
    "  if (!f.startsWith(c)) return false;\n"
    "  const rel = f.slice(c.length);\n"
    "  if (rel.split('/').includes('..')) return false;\n"
    "  return NOTE_PATHS.some(p => p.endsWith('/') ? rel.startsWith(p) : rel === p);\n"
    "}\n"
)

WRITE_GATE_OLD = (
    "function writeGate(sessionId, state, extra = {}) {\n"
    "  mkdirSync(STATE_DIR, { recursive: true });\n"
    "  writeFileSync(gateStateFile(sessionId), JSON.stringify({ state, ts: Date.now(), ...extra }), 'utf-8');\n"
    "}\n"
)
WRITE_GATE_NEW = (
    "function writeGate(sessionId, state, extra = {}) {\n"
    "  mkdirSync(STATE_DIR, { recursive: true });\n"
    "  const prev = readGate(sessionId);\n"
    "  writeFileSync(gateStateFile(sessionId), JSON.stringify({ state, ts: Date.now(), ...extra }), 'utf-8');\n"
    "  // Gate decision log (" + MARKER + "): content-free, one line per state write.\n"
    "  lat('gate', String(sessionId || 'unknown').slice(0, 8) + ' ' + (prev ? prev.state : 'none') + '->' + state);\n"
    "}\n"
)

MID_TURN_OLD = "      if (!HALT.test(low) && midTurn) {\n"
MID_TURN_NEW = "      if (!isHalt(low) && midTurn) {\n"
CARRY_OLD = "      if (!HALT.test(low) && fresh && continuesObjective(cmd)) {\n"
CARRY_NEW = "      if (!isHalt(low) && fresh && continuesObjective(cmd)) {\n"

ALLOW_OLD = "    if (allow.includes(tool)) process.exit(0);\n"
ALLOW_NEW = ALLOW_OLD + "    if (isNoteWrite(tool, data)) process.exit(0); // I4, see isNoteWrite\n"

# The question test starts with "do", so "do not proceed" read as a question and
# left an approval standing. A leading halt outranks the question check.
QUESTION_OLD = "    if (readGate(sid) && isQuestion(cmd)) process.exit(0);\n"
QUESTION_NEW = (
    "    if (readGate(sid) && isQuestion(cmd) && !isHalt(cmd.toLowerCase())) process.exit(0); "
    "// a halt is never a question (" + MARKER + ")\n"
)

REPLACEMENTS: list[tuple[str, str]] = [
    (STEER_LEAD_OLD, STEER_LEAD_NEW),
    (HALT_OLD, HALT_NEW),
    (WRITE_GATE_OLD, WRITE_GATE_NEW),
    (MID_TURN_OLD, MID_TURN_NEW),
    (CARRY_OLD, CARRY_NEW),
    (ALLOW_OLD, ALLOW_NEW),
    (QUESTION_OLD, QUESTION_NEW),
]


def check(text: str) -> tuple[bool, list[str]]:
    if MARKER in text:
        return True, ["already applied"]
    notes = []
    for index, (old, _) in enumerate(REPLACEMENTS, start=1):
        count = text.count(old)
        if count == 0:
            notes.append(f"anchor {index} not found")
        elif count > 1:
            notes.append(f"anchor {index} ambiguous ({count} matches)")
    return False, notes


def apply(text: str) -> str:
    applied, notes = check(text)
    if applied:
        raise SystemExit("already applied")
    if notes:
        raise SystemExit("; ".join(notes) + "; hook changed since this script was written")
    for old, new in REPLACEMENTS:
        text = text.replace(old, new, 1)
    return text


def render_plan(hook: Path, out_dir: Path) -> tuple[Path, Path]:
    """Write the patched hook and an APU plan that installs it transactionally.

    One ``merge``/``full_file`` operation, the live hook's hash as precondition,
    the rendered hash as the proposed output. ``apu review`` shows the change;
    ``apu apply`` records a receipt that ``apu rollback`` reverses byte for byte.
    Nothing is written to the hook here.
    """

    text = hook.read_text(encoding="utf-8")
    rendered = apply(text)
    out_dir = out_dir.resolve()
    hook = hook.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    source = out_dir / "speak-response.rendered.mjs"
    source.write_text(rendered, encoding="utf-8", newline="\n")
    current_sha = hashlib.sha256(hook.read_bytes()).hexdigest()
    proposed_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    plan = {
        "schema_version": 1,
        "apu_version": "0.9.0",
        "created_at": now,
        "inventory_sha256": current_sha,
        "status": "approved",
        "operations": [
            {
                "id": "gate-rule-gaps-2026-09-23",
                "action": "merge",
                "target": str(hook),
                "source": str(source),
                "ownership": "user",
                "strategy": "full_file",
                "precondition_sha256": current_sha,
                "proposed_sha256": proposed_sha,
                "backup_required": True,
                "requires_confirmation": True,
                "approval": {
                    "status": "approved",
                    "recorded_at": now,
                    "method": "operator-decision-2026-09-09-and-continue-2026-09-23",
                },
                "reason": "Gate rule gaps: steer list without imperative verbs, "
                "halt words narrowed to leading or imperative use, content-free "
                "gate decision log, I4 note-write exemption. Case record "
                "docs/cases/2026-09-09-start-of-turn-gate.md.",
                "evidence": [
                    "docs/cases/2026-09-09-start-of-turn-gate.md",
                    "tests/test_gate_rule_gaps_script.py",
                ],
            }
        ],
        "validation": {"commands": [], "fixtures": [], "required": []},
    }
    plan_path = out_dir / "gate-rule-gaps-plan.json"
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8", newline="\n")
    return source, plan_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument(
        "--plan",
        metavar="OUT_DIR",
        help="render the patched hook and an APU plan into OUT_DIR; touch nothing else",
    )
    parser.add_argument("--hook", type=Path, default=HOOK)
    args = parser.parse_args(argv)
    hook: Path = args.hook

    if args.plan:
        source, plan_path = render_plan(hook, Path(args.plan))
        print(f"rendered hook: {source}")
        print(f"plan: {plan_path}")
        print(f"review: apu review {plan_path}")
        print(f"apply:  apu apply {plan_path} --provider claude-code")
        return 0

    applied, notes = check(hook.read_text(encoding="utf-8"))
    # 0 applied, 1 not applied but every anchor present once, 2 anchors missing.
    print(f"{hook}: {'applied' if applied else 'not applied'}"
          + (f" ({'; '.join(notes)})" if notes else ""))
    if applied:
        return 0
    return 2 if notes else 1


if __name__ == "__main__":
    raise SystemExit(main())

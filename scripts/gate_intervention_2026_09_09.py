"""Apply or check the 2026-09-09 start-of-turn gate intervention.

Case record: docs/cases/2026-09-09-start-of-turn-gate.md (candidate changes 1
and 2). Target: ~/.claude/hooks/speak-response.mjs, gate mode only.

The change does three things:

1. Widens the approval vocabulary so "sounds ok", "fine by me", "that works",
   and "let's do it" open the gate like "go ahead" does.
2. Honors an approval phrase that appears within the first ten words of a long
   message ("yeah mr wizard make it so. <long commentary>").
3. Binds approval to the objective: after an approval, a completed turn no
   longer re-arms the gate when the next message steers, affirms, or continues
   the same objective. Halt words still withdraw approval, approvals expire
   after two hours, and a long message that does not read as a steer still arms
   the gate as before.

Usage:
    python scripts/gate_intervention_2026_09_09.py --check   # report only
    python scripts/gate_intervention_2026_09_09.py --apply   # patch + .bak
    python scripts/gate_intervention_2026_09_09.py --revert  # restore .bak

The script refuses to apply twice and never touches any other file.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HOOK = Path.home() / ".claude" / "hooks" / "speak-response.mjs"
BACKUP = HOOK.with_name("speak-response.mjs.bak-gate-intervention-2026-09-09")
MARKER = "Objective-bound approval (2026-09-09 gate intervention)"

REPLACEMENTS: list[tuple[str, str]] = [
    (
        "const STRONG_OK = /^(go ahead|go for it|proceed|do it|do that|approved|approve[d]?|"
        "confirm(ed|ing)?|make it so|carry on|green light|ship it|sounds good|looks good|"
        "affirmative|permission granted|you may proceed|execute)\\b/;",
        "const STRONG_OK = /^(go ahead|go for it|proceed|do it|do that|approved|approve[d]?|"
        "confirm(ed|ing)?|make it so|carry on|green light|ship it|sounds good|sounds ok(ay)?|"
        "sounds fine|sounds right|looks good|looks fine|fine by me|works for me|that works|"
        "let'?s do it|let'?s go|affirmative|permission granted|you may proceed|execute)\\b/;",
    ),
    (
        "  const m = STRONG_OK_ANY.exec(t);\n"
        "  return !!(m && words <= 24 && !isQuestion(t) && !REFUSAL.test(t.slice(0, m.index)));\n"
        "}",
        "  const m = STRONG_OK_ANY.exec(t);\n"
        "  if (!m || REFUSAL.test(t.slice(0, m.index))) return false;\n"
        "  // 2026-09-09 gate intervention (APU case docs/cases/2026-09-09-start-of-turn-gate.md):\n"
        "  // a phrase inside the first ten words approves regardless of what follows;\n"
        "  // deeper phrases keep the short, non-question rule.\n"
        "  const wordsBefore = t.slice(0, m.index).trim().split(/\\s+/).filter(Boolean).length;\n"
        "  if (wordsBefore < 10) return true;\n"
        "  return words <= 24 && !isQuestion(t);\n"
        "}",
    ),
    (
        # The CLI dispatch calls gateMain() near the top of the file, before any
        # later top-level const is initialized. These definitions therefore sit
        # right after HALT, which the gate already relies on, or gateMain throws
        # a ReferenceError (temporal dead zone) on every post-approval prompt.
        "const HALT = /\\b(stop|halt|wait|hold on|hold off|cancel|abort|don'?t|do not|"
        "never ?mind|undo|revert)\\b/;",
        "const HALT = /\\b(stop|halt|wait|hold on|hold off|cancel|abort|don'?t|do not|"
        "never ?mind|undo|revert)\\b/;\n"
        "\n"
        "// Objective-bound approval: after an approval, a completed turn does not re-arm\n"
        "// the gate on its own. A follow-up that steers, affirms, or continues the same\n"
        "// objective keeps it open; a halt word (checked by the caller) or a fresh\n"
        "// request that reads like a new objective arms it again. Approvals expire.\n"
        "const APPROVAL_TTL_MS = 2 * 60 * 60 * 1000;\n"
        "// Affirmations, continuations, and short steers only. Imperative verbs were\n"
        "// removed on 2026-09-09 (operator decision): a message that opens a new\n"
        "// objective (\"now build ...\", \"make a ...\") must not inherit an approval.\n"
        "const STEER_LEAD = /^(ok|okay|yes|yep|yeah|yup|sure|fine|right|good|great|cool|also|and|"
        "plus|keep|continue|just|actually|instead|but|oh|hmm|no|nope|not that|the|"
        "that|this|it|same|again|more|less)\\b/;\n"
        "function continuesObjective(cmd) {\n"
        "  const t = cmd.toLowerCase().replace(/^[\\s,.!?:;-]+/, '')"
        ".replace(/^(hey |ok |okay |alright |all right )?claude[,.!? ]+/, '').trim();\n"
        "  if (!t) return false;\n"
        "  const words = t.split(/\\s+/).length;\n"
        "  if (words <= 12) return true;\n"
        "  return STEER_LEAD.test(t);\n"
        "}",
    ),
    (
        # Content-free gate decision log: one line per state write in the hook's
        # existing latency log ("gate <8-char session prefix> <before>-><after>").
        # No prompt text. This is the producer for the I3 measurement.
        "function writeGate(sessionId, state, extra = {}) {\n"
        "  mkdirSync(STATE_DIR, { recursive: true });\n"
        "  writeFileSync(gateStateFile(sessionId), JSON.stringify({ state, ts: Date.now(), ...extra }), 'utf-8');\n"
        "}",
        "function writeGate(sessionId, state, extra = {}) {\n"
        "  mkdirSync(STATE_DIR, { recursive: true });\n"
        "  const prev = readGate(sessionId);\n"
        "  writeFileSync(gateStateFile(sessionId), JSON.stringify({ state, ts: Date.now(), ...extra }), 'utf-8');\n"
        "  // Gate decision log (2026-09-09): content-free, one line per state write.\n"
        "  lat('gate', String(sessionId || 'unknown').slice(0, 8) + ' ' + (prev ? prev.state : 'none') + '->' + state);\n"
        "}",
    ),
    (
        "    const g0 = readGate(sid);\n"
        "    if (midTurn && g0 && g0.state === 'approved') {\n"
        "      const low = cmd.toLowerCase().replace(/\\bstop asking\\b/g, '');\n"
        "      if (!HALT.test(low)) {\n"
        "        console.log('Start-of-turn gate: mid-turn message while the plan is approved; "
        "the gate stays open. Treat it as a steer on the running work, not a new plan request.');\n"
        "        process.exit(0);\n"
        "      }\n"
        "    }\n"
        "    writeGate(sid, 'pending', { cmd });",
        "    const g0 = readGate(sid);\n"
        "    if (g0 && g0.state === 'approved') {\n"
        "      const low = cmd.toLowerCase().replace(/\\bstop asking\\b/g, '');\n"
        "      const fresh = Date.now() - (g0.ts || 0) < APPROVAL_TTL_MS;\n"
        "      if (!HALT.test(low) && midTurn) {\n"
        "        console.log('Start-of-turn gate: mid-turn message while the plan is approved; "
        "the gate stays open. Treat it as a steer on the running work, not a new plan request.');\n"
        "        process.exit(0);\n"
        "      }\n"
        "      if (!HALT.test(low) && fresh && continuesObjective(cmd)) {\n"
        "        // " + MARKER + ": the turn\n"
        "        // finished, but this reads as a steer or affirmation on the same\n"
        "        // objective, so the approval carries forward instead of resetting.\n"
        "        writeGate(sid, 'approved', { cmd: g0.cmd || cmd, ts: g0.ts });\n"
        "        console.log('Start-of-turn gate: the approved objective carries forward; this "
        "message steers it. Continue without restating the plan. Say \"stop\" or \"hold on\" to "
        "withdraw approval.');\n"
        "        process.exit(0);\n"
        "      }\n"
        "    }\n"
        "    writeGate(sid, 'pending', { cmd });",
    ),
]


def check(text: str) -> tuple[bool, list[str]]:
    if MARKER in text:
        return True, ["already applied"]
    missing = [f"anchor {index + 1} not found" for index, (old, _) in enumerate(REPLACEMENTS) if old not in text]
    return False, missing


def apply(text: str) -> str:
    for old, new in REPLACEMENTS:
        if old not in text:
            raise SystemExit("anchor missing; hook changed since this script was written")
        text = text.replace(old, new, 1)
    return text


def render_plan(hook: Path, out_dir: Path) -> tuple[Path, Path]:
    """Write the patched hook and an APU plan that installs it transactionally.

    The plan is a single ``merge``/``full_file`` operation with the live hook's
    hash as precondition and the rendered hash as the proposed output, so
    ``apu review`` shows exactly what changes and ``apu apply`` records a receipt
    that ``apu rollback`` can reverse byte for byte. Nothing is written to the
    hook here.
    """

    import hashlib
    import json
    from datetime import UTC, datetime

    text = hook.read_text(encoding="utf-8")
    if MARKER in text:
        # Already carrying an earlier version of the change: render from the
        # backup so the plan moves from the current live state to the current
        # version of the change in one reviewed step.
        backup = hook.with_name(BACKUP.name) if hook != HOOK else BACKUP
        if not backup.is_file():
            raise SystemExit("hook already patched and no backup to render from")
        base = backup.read_text(encoding="utf-8")
    else:
        base = text
    applied, notes = check(base)
    if not applied and notes:
        raise SystemExit("; ".join(notes))
    rendered = apply(base) if not applied else base
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
                "id": "gate-intervention-2026-09-09",
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
                    "method": "operator-decision-2026-09-09",
                },
                "reason": "Start-of-turn gate intervention: wider approvals, "
                "objective-bound approval without imperative-led carry-forward, "
                "content-free gate decision log. Case record "
                "docs/cases/2026-09-09-start-of-turn-gate.md.",
                "evidence": [
                    "docs/cases/2026-09-09-start-of-turn-gate.md",
                    "scripts/gate_proof_session_2026_09_09.mjs",
                ],
            }
        ],
        "validation": {"commands": [], "fixtures": [], "required": []},
    }
    plan_path = out_dir / "gate-intervention-plan.json"
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8", newline="\n")
    return source, plan_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--revert", action="store_true")
    mode.add_argument(
        "--plan",
        metavar="OUT_DIR",
        help="render the patched hook and an APU plan into OUT_DIR; touch nothing else",
    )
    mode.add_argument(
        "--reapply",
        action="store_true",
        help="restore the backup, then apply the current version of the change",
    )
    parser.add_argument("--hook", type=Path, default=HOOK)
    args = parser.parse_args(argv)
    hook: Path = args.hook
    backup = hook.with_name(BACKUP.name) if hook != HOOK else BACKUP

    if args.plan:
        source, plan_path = render_plan(hook, Path(args.plan))
        print(f"rendered hook: {source}")
        print(f"plan: {plan_path}")
        print(f"review: apu review {plan_path}")
        print(f"apply:  apu apply {plan_path} --provider claude-code")
        return 0

    if args.revert or args.reapply:
        if not backup.is_file():
            print(f"no backup at {backup}", file=sys.stderr)
            return 1
        shutil.copyfile(backup, hook)
        print(f"restored {hook} from {backup}")
        if args.revert:
            return 0

    text = hook.read_text(encoding="utf-8")
    applied, notes = check(text)
    if args.check:
        # 0 applied, 1 not applied but every anchor present, 2 anchors missing.
        print(f"{hook}: {'applied' if applied else 'not applied'}"
              + (f" ({'; '.join(notes)})" if notes else ""))
        if applied:
            return 0
        return 2 if notes else 1

    if applied:
        print("already applied; nothing to do")
        return 0
    if notes:
        print("; ".join(notes), file=sys.stderr)
        return 2
    if not args.reapply:
        shutil.copyfile(hook, backup)
    hook.write_text(apply(text), encoding="utf-8", newline="\n")
    print(f"applied; backup at {backup}")
    print("verify: node --check " + str(hook))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

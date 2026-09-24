"""Widen the gate's note exemption to the Dias board files (2026-09-23, part b).

Case record: docs/cases/2026-09-09-start-of-turn-gate.md, "Gate rule gaps",
residual "the note exemption does not cover .dias/". Target:
~/.claude/hooks/speak-response.mjs after the 2026-09-23 gate intervention.

One anchored change: `.dias/` joins the NOTE_PATHS list, so a session under an
armed gate can update its own chip board and status file. Everything else in
the deny path is unchanged; the path normalization and `..` refusal in
isNoteWrite apply as before.

Usage:
    python scripts/gate_intervention_2026_09_23_dias.py --check
    python scripts/gate_intervention_2026_09_23_dias.py --plan build/gate-dias
    apu apply build/gate-dias/gate-dias-plan.json --provider claude-code

No --apply: the durable path is apu apply with a receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

HOOK = Path.home() / ".claude" / "hooks" / "speak-response.mjs"
MARKER = "Dias board files (2026-09-23 gate intervention, part b)"
REQUIRES = "Gate rule gaps (2026-09-23 gate intervention)"

NOTE_PATHS_OLD = (
    "const NOTE_PATHS = ['.claude/handoff.md', '.claude/handoff-history/', "
    "'.claude/notes/', 'incidents/'];\n"
)
NOTE_PATHS_NEW = (
    "// " + MARKER + ": the chip board and status file are\n"
    "// notes about the work, not the work; a gated session may update its own chip.\n"
    "const NOTE_PATHS = ['.claude/handoff.md', '.claude/handoff-history/', "
    "'.claude/notes/', 'incidents/', '.dias/'];\n"
)

REPLACEMENTS: list[tuple[str, str]] = [(NOTE_PATHS_OLD, NOTE_PATHS_NEW)]


def check(text: str) -> tuple[bool, list[str]]:
    if MARKER in text:
        return True, ["already applied"]
    notes = []
    if REQUIRES not in text:
        notes.append("the 2026-09-23 gate intervention is not installed")
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
                "id": "gate-dias-note-paths-2026-09-23",
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
                    "method": "operator-continue-2026-09-23",
                },
                "reason": "Gate note exemption widened to .dias/ so a gated session "
                "can update its own chip board. Case record "
                "docs/cases/2026-09-09-start-of-turn-gate.md.",
                "evidence": [
                    "docs/cases/2026-09-09-start-of-turn-gate.md",
                    "tests/test_gate_rule_gaps_script.py",
                ],
            }
        ],
        "validation": {"commands": [], "fixtures": [], "required": []},
    }
    plan_path = out_dir / "gate-dias-plan.json"
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8", newline="\n")
    return source, plan_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--plan", metavar="OUT_DIR")
    parser.add_argument("--hook", type=Path, default=HOOK)
    args = parser.parse_args(argv)
    hook: Path = args.hook

    if args.plan:
        source, plan_path = render_plan(hook, Path(args.plan))
        print(f"rendered hook: {source}")
        print(f"plan: {plan_path}")
        print(f"apply:  apu apply {plan_path} --provider claude-code")
        return 0

    applied, notes = check(hook.read_text(encoding="utf-8"))
    print(f"{hook}: {'applied' if applied else 'not applied'}"
          + (f" ({'; '.join(notes)})" if notes else ""))
    if applied:
        return 0
    return 2 if notes else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .evidence import (
    ingest_codex_trace,
    ingest_hook_event,
    observe_repository_state,
    read_evidence,
    reconcile_evidence,
    verify_evidence_source,
)
from .models import canonical_json
from .state import resolve_state_home


def add_evidence_parser(commands: argparse._SubParsersAction) -> None:
    evidence = commands.add_parser(
        "evidence",
        help="ingest and verify content-minimized execution evidence",
    )
    subcommands = evidence.add_subparsers(dest="evidence_command", required=True)

    codex = subcommands.add_parser(
        "ingest-codex",
        help="normalize one Codex JSONL session into the evidence plane",
    )
    codex.add_argument("--session-id")
    codex.add_argument("--trace-root", type=Path)
    codex.add_argument("--cwd", type=Path)
    codex.add_argument("--schema-version", type=int, choices=(1, 2), default=2)
    codex.add_argument("--json", action="store_true")

    hook = subcommands.add_parser(
        "ingest-hook",
        help="normalize one provider lifecycle hook from JSON input",
    )
    hook.add_argument("--provider", required=True)
    hook.add_argument("--event", required=True)
    hook.add_argument("--input", type=Path)
    hook.add_argument("--observe-state", action="store_true")
    hook.add_argument("--schema-version", type=int, choices=(1, 2), default=2)
    hook.add_argument("--json", action="store_true")

    observe = subcommands.add_parser(
        "observe-state",
        help="append an independent Git working-state observation",
    )
    observe.add_argument("--provider", required=True)
    observe.add_argument("--session-id", required=True)
    observe.add_argument("--cwd", type=Path, default=Path.cwd())
    observe.add_argument("--sequence", type=int, default=0)
    observe.add_argument("--schema-version", type=int, choices=(1, 2), default=2)
    observe.add_argument("--json", action="store_true")

    show = subcommands.add_parser(
        "show",
        help="summarize one provider session's normalized evidence",
    )
    show.add_argument("--provider", required=True)
    show.add_argument("--session-id", required=True)
    show.add_argument("--verify", action="store_true")
    show.add_argument("--json", action="store_true")


def _read_hook_payload(path: Path | None) -> dict[str, Any]:
    if path is None:
        value = json.load(sys.stdin)
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("hook input must be one JSON object")
    return value


def _print(value: Any, *, as_json: bool, summary: str) -> None:
    if as_json:
        print(canonical_json(value))
    else:
        print(summary)


def run_evidence(args: argparse.Namespace) -> int:
    state_home = resolve_state_home()
    if args.evidence_command == "ingest-codex":
        from .behavior_watch import NoAttribution, select_codex_session

        selection = select_codex_session(
            trace_root=args.trace_root,
            session_id=args.session_id,
            cwd=args.cwd,
            state_home=state_home,
        )
        if isinstance(selection, NoAttribution):
            result = {
                "kind": selection.kind,
                "reason_code": selection.reason_code,
                "provenance": selection.provenance.to_dict(),
            }
            _print(
                result,
                as_json=args.json,
                summary=f"Codex evidence: no attribution ({selection.reason_code})",
            )
            return 2
        session = selection.session
        path, events, boundary = ingest_codex_trace(
            state_home,
            session.path,
            attribution=selection.provenance.to_dict(),
            schema_version=args.schema_version,
        )
        result = {
            "provider": "codex",
            "schema_version": boundary["schema_version"],
            "session_id": boundary["session_id"],
            "evidence_path": str(path) if path is not None else None,
            "event_count": len(events),
            "appended_count": boundary["appended_count"],
            "source_boundary": {
                "path": boundary["path"],
                "snapshot_bytes": boundary["snapshot_bytes"],
                "snapshot_sha256": boundary["snapshot_sha256"],
            },
            "reconciliation": reconcile_evidence(events),
        }
        _print(
            result,
            as_json=args.json,
            summary=(
                f"Codex evidence: {len(events)} observed, "
                f"{boundary['appended_count']} appended for {boundary['session_id']}"
            ),
        )
        return 0

    if args.evidence_command == "ingest-hook":
        payload = _read_hook_payload(args.input)
        path, event, appended = ingest_hook_event(
            state_home,
            args.provider,
            args.event,
            payload,
            schema_version=args.schema_version,
        )
        state_event = None
        state_appended = False
        if args.observe_state:
            cwd = event["state"]["cwd"]
            if cwd is None:
                raise ValueError(
                    "--observe-state requires an absolute cwd in hook input"
                )
            _, state_event, state_appended = observe_repository_state(
                state_home,
                provider=args.provider,
                session_id=event["session_id"],
                cwd=Path(cwd),
                sequence=event["sequence"] + 1,
                schema_version=args.schema_version,
            )
        result = {
            "provider": args.provider,
            "schema_version": event["schema_version"],
            "session_id": event["session_id"],
            "event_id": event["event_id"],
            "event_type": event["event_type"],
            "appended": appended,
            "state_event_id": state_event["event_id"] if state_event else None,
            "state_appended": state_appended,
            "evidence_path": str(path) if path is not None else None,
        }
        _print(
            result,
            as_json=args.json,
            summary=f"Hook evidence: {event['event_type']} for {event['session_id']}",
        )
        return 0

    if args.evidence_command == "observe-state":
        path, event, appended = observe_repository_state(
            state_home,
            provider=args.provider,
            session_id=args.session_id,
            cwd=args.cwd,
            sequence=args.sequence,
            schema_version=args.schema_version,
        )
        result = {
            "event": event,
            "appended": appended,
            "evidence_path": str(path) if path is not None else None,
        }
        _print(
            result,
            as_json=args.json,
            summary=(
                f"State evidence: {'dirty' if event['state']['dirty'] else 'clean'} "
                f"for {args.session_id}"
            ),
        )
        return 0

    if args.evidence_command == "show":
        events = read_evidence(state_home, args.provider, args.session_id)
        result = {
            "provider": args.provider,
            "session_id": args.session_id,
            "reconciliation": reconcile_evidence(events),
            "source_verification": (
                [verify_evidence_source(event) for event in events]
                if args.verify
                else None
            ),
        }
        _print(
            result,
            as_json=args.json,
            summary=(
                f"Evidence: {len(events)} events for {args.provider}/{args.session_id}; "
                f"{', '.join(result['reconciliation']['reason_codes']) or 'no discrepancy'}"
            ),
        )
        return 0

    raise ValueError(f"unsupported evidence command: {args.evidence_command}")

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .behavior_watch import (
    SESSION_PROVIDER_NAMES,
    WATCHER_ALIASES,
    WATCHER_ID,
    NoAttributionError,
    configure_watcher,
    diagnose_incident,
    intervene,
    load_incident,
    mark_incident,
    normalized_cwd_key,
    record_intervention_result,
    watcher_status,
)
from .models import canonical_json
from .state import resolve_state_home


def _emit(value: Any) -> None:
    print(canonical_json(value))


def _run(parser: argparse.ArgumentParser, function, argv: Sequence[str] | None) -> int:
    args = parser.parse_args(argv)
    try:
        return function(args)
    except NoAttributionError as error:
        result = {
            "kind": error.result.kind,
            "reason_code": error.result.reason_code,
            "provenance": error.result.provenance.to_dict(),
        }
        if getattr(args, "json", False):
            _emit(result)
        else:
            print(
                f"{parser.prog}: no_attribution: {error.result.reason_code}",
                file=sys.stderr,
            )
        return 2
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        print(f"{parser.prog}: {error}", file=sys.stderr)
        return 1


def _event_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="apu-event")
    parser.add_argument("description")
    parser.add_argument("--provider", choices=SESSION_PROVIDER_NAMES)
    parser.add_argument("--session-id")
    parser.add_argument("--trace-root", type=Path)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument(
        "--evidence-schema-version", type=int, choices=(1, 2), default=2
    )
    parser.add_argument("--json", action="store_true")
    return parser


def event_main(argv: Sequence[str] | None = None) -> int:
    def command(args: argparse.Namespace) -> int:
        path, incident = mark_incident(
            resolve_state_home(),
            args.description,
            provider=args.provider,
            trace_root=args.trace_root,
            session_id=args.session_id,
            cwd=args.cwd,
            evidence_schema_version=args.evidence_schema_version,
        )
        if args.json:
            _emit(incident)
        else:
            print(f"Marked {incident['incident_id']}")
            print(f"Session: {incident['session']['session_id']}")
            print(f"Provider: {incident['provider']}")
            print(
                f"Evidence: {incident['nearby_evidence']['line_start']}-{incident['nearby_evidence']['line_end']}"
            )
            print(f"Saved: {path}")
            print("Next: apu-wtf")
        return 0

    return _run(_event_parser(), command, argv)


def _wtf_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="apu-wtf")
    parser.add_argument("--incident")
    parser.add_argument("--provider", choices=SESSION_PROVIDER_NAMES)
    parser.add_argument("--session-id")
    parser.add_argument("--trace-root", type=Path)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument(
        "--evidence-schema-version", type=int, choices=(1, 2), default=2
    )
    parser.add_argument("--json", action="store_true")
    return parser


def _latest_incident_matches(
    state_home: Path,
    *,
    provider: str | None,
    session_id: str | None,
    cwd: Path | None,
) -> str | None:
    """Return the latest incident id only when it matches every explicit selector."""

    pointer = state_home / "behavior" / "latest-incident.json"
    if not pointer.is_file():
        return None
    try:
        incident = load_incident(state_home)
    except ValueError:
        return None
    if provider is not None and incident.get("provider", "codex") != provider:
        return None
    session = incident.get("session")
    session = session if isinstance(session, dict) else {}
    if session_id is not None and session.get("session_id") != session_id:
        return None
    if cwd is not None:
        recorded = session.get("cwd")
        if not isinstance(recorded, str) or normalized_cwd_key(
            Path(recorded)
        ) != normalized_cwd_key(cwd):
            return None
    return str(incident["incident_id"])


def wtf_main(argv: Sequence[str] | None = None) -> int:
    def command(args: argparse.Namespace) -> int:
        state_home = resolve_state_home()
        incident_id = args.incident
        marked = False
        if incident_id is None:
            # Explicit selectors describe the run the operator means. The latest
            # incident is reused only when it satisfies all of them; otherwise a
            # fresh incident is marked from that selection instead of silently
            # diagnosing whatever was marked last, possibly for another provider
            # or project.
            incident_id = _latest_incident_matches(
                state_home,
                provider=args.provider,
                session_id=args.session_id,
                cwd=args.cwd,
            )
            if incident_id is None or args.trace_root is not None:
                _, incident = mark_incident(
                    state_home,
                    "most recent incomplete provider run selected automatically",
                    provider=args.provider,
                    trace_root=args.trace_root,
                    session_id=args.session_id,
                    cwd=args.cwd if args.cwd is not None else Path.cwd(),
                    evidence_schema_version=args.evidence_schema_version,
                )
                incident_id = incident["incident_id"]
                marked = True
        path, diagnosis = diagnose_incident(
            state_home,
            incident_id=incident_id,
            provider=args.provider,
        )
        if args.json:
            _emit(diagnosis)
        else:
            print(
                f"Incident: {diagnosis['incident_id']} "
                f"({diagnosis['provider']}, {'marked now' if marked else 'previously marked'})"
            )
            print(
                f"{diagnosis['status']}: {', '.join(diagnosis['observed_signals']) or 'no matched signal'}"
            )
            for source in diagnosis["likely_sources"][:3]:
                location = source.get("path") or source["kind"]
                lines = source.get("line_numbers")
                suffix = f":{','.join(str(item) for item in lines)}" if lines else ""
                print(
                    f"{source['rank']}. {location}{suffix} "
                    f"[{', '.join(source['reason_codes'])}]"
                )
            print(f"Saved: {path}")
            if diagnosis["status"] != "possible-legitimate-barrier":
                print("Next: apu-intervene")
        return 0

    return _run(_wtf_parser(), command, argv)


def _intervene_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="apu-intervene")
    parser.add_argument("--diagnosis")
    parser.add_argument("--provider", choices=SESSION_PROVIDER_NAMES)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--result", choices=("completed", "blocked", "failed"))
    parser.add_argument("--intervention")
    parser.add_argument("--json", action="store_true")
    return parser


def intervene_main(argv: Sequence[str] | None = None) -> int:
    def command(args: argparse.Namespace) -> int:
        state_home = resolve_state_home()
        if args.result:
            path, result = record_intervention_result(
                state_home,
                args.result,
                intervention_id=args.intervention,
                provider=args.provider,
            )
            if args.json:
                _emit(result)
            else:
                print(f"Intervention result: {result['result']}")
                print(f"Saved: {path}")
            return 0
        path, result = intervene(
            state_home,
            diagnosis_id=args.diagnosis,
            provider=args.provider,
            dry_run=args.dry_run,
            force_execute=args.execute,
            timeout_seconds=args.timeout,
        )
        if args.json:
            _emit(result)
        else:
            print(f"Intervention: {result['status']}")
            if not result["executed"]:
                print("Continuation command:")
                print(json.dumps(result["command"], ensure_ascii=False))
            print(f"Saved: {path}")
        return 0 if result["status"] != "failed" else 1

    return _run(_intervene_parser(), command, argv)


def _watch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="apu-watch")
    parser.add_argument("watcher", nargs="?", default=WATCHER_ID)
    parser.add_argument("--provider", choices=SESSION_PROVIDER_NAMES)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--enable", action="store_true")
    group.add_argument("--disable", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def watch_main(argv: Sequence[str] | None = None) -> int:
    def command(args: argparse.Namespace) -> int:
        if args.watcher not in WATCHER_ALIASES:
            raise ValueError(f"unknown watcher: {args.watcher}")
        state_home = resolve_state_home()
        if args.enable or args.disable:
            status = configure_watcher(state_home, enabled=args.enable)
        else:
            status = watcher_status(state_home)
        if args.provider:
            status = dict(status)
            status["providers"] = [args.provider]
            status["provider_health"] = {
                args.provider: status["provider_health"][args.provider]
            }
        if args.json:
            _emit({"watchers": [status]})
        else:
            state = "enabled" if status["enabled"] else "disabled"
            labels = {
                "codex": "Codex JSONL",
                "claude-code": "Claude Code JSONL",
            }
            sources = ", ".join(labels[item] for item in status["providers"])
            print(f"{WATCHER_ID}: {state} ({sources}, no background service)")
        return 0

    return _run(_watch_parser(), command, argv)

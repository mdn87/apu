from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .behavior_watch import (
    EASY_DECISION_SIGNAL,
    RECOVERY_SIGNAL,
    SESSION_PROVIDER_NAMES,
    WATCHER_ALIASES,
    WATCHER_ID,
    NoAttributionError,
    configure_watcher,
    diagnose_incident,
    intervene,
    intervention_prompt,
    load_incident,
    mark_incident,
    normalized_cwd_key,
    record_intervention_result,
    watcher_status,
)
from .models import canonical_json
from .state import resolve_state_home

EZPZ_DEFAULT_DESCRIPTION = (
    "the requested outcome remains unresolved; recover the task using evidence "
    "and the authorization already given"
)

_ATTRIBUTION_HELP = {
    "ambiguous_active_candidates": "Several sessions match. Add --session-id with the session you mean.",
    "ambiguous_provider": "Both providers match. Add --provider codex or --provider claude-code.",
    "ambiguous_session_id": "The session ID matches several traces. Narrow --trace-root to the intended trace directory.",
    "cwd_mismatch": "The session belongs to another directory. Set --cwd to that session's project directory.",
    "no_active_candidate": "No incomplete session matches. For an agent that already ended its turn, select its --session-id explicitly.",
    "no_exact_cwd_candidate": "No session matches this directory. Run from the agent's project directory or set --cwd.",
    "session_not_found": "The session ID was not found. Check --session-id, --provider, and --trace-root.",
    "stale_trace": "The matching trace is over ten minutes old. Resume the task, then retry.",
    "trace_root_unavailable": "No trace directory is available. Start a Codex or Claude Code session, or set --trace-root.",
    "unparsable_trace": "The matching trace could not be read. Check --trace-root and the session's JSONL file.",
}

_STATUS_HELP = {
    "recovery-requested": "You reported an unresolved task. The root cause still needs verification.",
    "likely-autonomy-loss": "The agent appears to have paused on work it could continue.",
    "possible-legitimate-barrier": "The stop may require your input or authorization.",
    "insufficient-evidence": "The evidence does not establish why the agent stopped.",
}


def _emit(value: Any) -> None:
    print(canonical_json(value))


def _run(parser: argparse.ArgumentParser, function, argv: Sequence[str] | None) -> int:
    args = parser.parse_args(argv)
    return _run_command(parser.prog, function, args)


def _run_command(prog: str, function, args: argparse.Namespace) -> int:
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
                f"{prog}: no_attribution: {error.result.reason_code}",
                file=sys.stderr,
            )
            print(_ATTRIBUTION_HELP[error.result.reason_code], file=sys.stderr)
        return 2
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        print(f"{prog}: {error}", file=sys.stderr)
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
    parser = argparse.ArgumentParser(
        prog="apu-wtf",
        description="Explain an agent's stop and show the next action for that incident.",
        epilog="Reuses the latest incident only in the selected directory. Use --fresh to inspect the current run.",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--incident", help="diagnose this saved incident, even from another directory"
    )
    selection.add_argument(
        "--fresh",
        action="store_true",
        help="mark the current run instead of reusing a saved incident",
    )
    _add_selection_arguments(parser)
    return parser


def _add_selection_arguments(
    parser: argparse.ArgumentParser, *, prompt_output: bool = False
) -> None:
    parser.add_argument(
        "--provider",
        choices=SESSION_PROVIDER_NAMES,
        help="limit selection to this provider",
    )
    parser.add_argument("--session-id", help="select an exact session ID")
    parser.add_argument(
        "--trace-root", type=Path, help="read sessions from this trace directory"
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        help="agent's project directory (default: current directory)",
    )
    parser.add_argument(
        "--evidence-schema-version",
        type=int,
        choices=(1, 2),
        default=2,
        help="saved evidence format (default: 2)",
    )
    output = parser.add_mutually_exclusive_group() if prompt_output else parser
    output.add_argument(
        "--json", action="store_true", help="print the diagnosis as JSON"
    )
    if prompt_output:
        output.add_argument(
            "--prompt",
            action="store_true",
            help="print only the recovery instruction for the selected chat; refuse if a barrier is detected",
        )


def add_shortcut_parsers(commands) -> None:
    for name, factory, handler, summary in (
        ("wtf", _wtf_parser, _wtf_command, "explain why an agent stopped"),
        (
            "ezpz",
            _ezpz_parser,
            _ezpz_command,
            "recover unfinished work, ineffective retries, and unnecessary approval requests",
        ),
    ):
        parent = factory()
        parser = commands.add_parser(
            name,
            parents=[parent],
            add_help=False,
            help=summary,
            description=parent.description,
            epilog=parent.epilog,
        )
        parser.set_defaults(behavior_handler=handler)


def run_shortcut(args: argparse.Namespace) -> int:
    return _run_command(f"apu {args.command}", args.behavior_handler, args)


def _print_diagnosis(
    path: Path,
    diagnosis: dict[str, Any],
    incident: dict[str, Any],
    *,
    marked: bool,
) -> None:
    status = diagnosis["status"]
    print(f"{status}: {_STATUS_HELP[status]}")
    session = incident["session"]
    print(f"Session: {session['session_id']} ({diagnosis['provider']})")
    print(f"Directory: {session['cwd']}")
    print(
        f"Incident: {diagnosis['incident_id']} "
        f"({diagnosis['provider']}, {'marked now' if marked else 'previously marked'})"
    )
    asserted = incident.get("claim", {}).get("asserted_signals", [])
    if EASY_DECISION_SIGNAL in asserted:
        print(
            f"Attested: {EASY_DECISION_SIGNAL} (you identified this as a simple, reversible choice)"
        )
    if RECOVERY_SIGNAL in asserted:
        print("Operator report: the requested outcome remains unresolved.")
    signals = diagnosis["observed_signals"]
    if signals:
        print(f"Signals: {', '.join(signals)}")
    evidence = incident.get("nearby_evidence", {})
    if evidence.get("line_start") is not None:
        print(
            f"Evidence: {session['trace_path']}:{evidence['line_start']}-{evidence['line_end']}"
        )
    print("Possible causes (matching rules are clues, not proof):")
    for source in diagnosis["likely_sources"][:3]:
        location = source.get("path") or source["kind"]
        lines = source.get("line_numbers")
        suffix = f":{','.join(str(item) for item in lines)}" if lines else ""
        print(
            f"  {source['rank']}. {location}{suffix} [{', '.join(source['reason_codes'])}]"
        )
    print(f"Saved: {path}")
    if status == "possible-legitimate-barrier":
        print(f"Possible barriers: {', '.join(diagnosis['possible_barriers'])}")
        if EASY_DECISION_SIGNAL in asserted:
            print("Barrier evidence contradicts the easy-decision attestation.")
        print("Review the barrier and its evidence before continuing the task.")
    elif status == "insufficient-evidence":
        print(
            'Next: describe the stop with apu-event "what happened", then run apu wtf.'
        )
    else:
        print(
            f"Resume template: {diagnosis['recommended_intervention']['template_id']}"
        )
        print(f"Next: apu-intervene --diagnosis {diagnosis['diagnosis_id']}")


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
    return _run(_wtf_parser(), _wtf_command, argv)


def _wtf_command(args: argparse.Namespace) -> int:
    if args.incident is not None and any(
        value is not None for value in (args.cwd, args.session_id, args.trace_root)
    ):
        raise ValueError(
            "--incident cannot be combined with --cwd, --session-id, or --trace-root; choose a saved incident or a current run"
        )
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
            cwd=args.cwd if args.cwd is not None else Path.cwd(),
        )
        if incident_id is None or args.trace_root is not None or args.fresh:
            _, incident = mark_incident(
                state_home,
                "incomplete provider run selected from the requested directory",
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
        _print_diagnosis(
            path,
            diagnosis,
            load_incident(state_home, diagnosis["incident_id"]),
            marked=marked,
        )
    return 0


def _ezpz_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apu-ezpz",
        description=(
            "Recover an unresolved task: verify the outcome, investigate failed "
            "approaches, and continue within existing authorization."
        ),
    )
    parser.add_argument(
        "description",
        nargs="?",
        default=EZPZ_DEFAULT_DESCRIPTION,
        help="what is still wrong, repeated, or being handed back to you (optional)",
    )
    _add_selection_arguments(parser, prompt_output=True)
    return parser


def ezpz_main(argv: Sequence[str] | None = None) -> int:
    """Mark a fresh operator recovery request and prepare an outcome-focused continuation."""

    return _run(_ezpz_parser(), _ezpz_command, argv)


def _ezpz_command(args: argparse.Namespace) -> int:
    state_home = resolve_state_home()
    _, incident = mark_incident(
        state_home,
        args.description,
        provider=args.provider,
        trace_root=args.trace_root,
        session_id=args.session_id,
        cwd=args.cwd if args.cwd is not None else Path.cwd(),
        evidence_schema_version=args.evidence_schema_version,
        asserted_signals=(RECOVERY_SIGNAL,),
    )
    path, diagnosis = diagnose_incident(
        state_home,
        incident_id=incident["incident_id"],
        provider=args.provider,
    )
    if args.prompt:
        if diagnosis["status"] == "possible-legitimate-barrier":
            raise ValueError(
                "recovery prompt withheld because a legitimate barrier may exist: "
                + ", ".join(diagnosis["possible_barriers"])
            )
        print(
            intervention_prompt(
                diagnosis["recommended_intervention"]["template_id"], incident=incident
            )
        )
    elif args.json:
        _emit(diagnosis)
    else:
        _print_diagnosis(path, diagnosis, incident, marked=True)
        if diagnosis["status"] == "recovery-requested":
            print("Recovery instruction for this session:")
            print(
                intervention_prompt(
                    diagnosis["recommended_intervention"]["template_id"],
                    incident=incident,
                )
            )
    return 0


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
            if result["executed"]:
                print(
                    "Outcome: unverified. A model turn ending does not prove the task works."
                )
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

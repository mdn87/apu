from __future__ import annotations

import argparse
import re
from datetime import timedelta
from pathlib import Path

from datetime import UTC, datetime

from .behavior_audit import (
    DEFAULT_LOOKBACK,
    DEFAULT_SESSION_LIMIT,
    DEFAULT_SOURCE_BYTE_LIMIT,
    audit_behavior,
)
from .gate_decisions import DEFAULT_GATE_LOG, gate_cost_report
from .models import canonical_json
from .state import resolve_state_home

_DURATION = re.compile(r"^(?P<value>[1-9][0-9]*)(?P<unit>[hdw])$", re.IGNORECASE)
_BYTE_SIZE = re.compile(
    r"^(?P<value>[1-9][0-9]*)(?P<unit>b|kib|mib|gib)$", re.IGNORECASE
)


def _parse_duration(value: str) -> timedelta:
    match = _DURATION.fullmatch(value.strip())
    if match is None:
        raise argparse.ArgumentTypeError(
            "duration must be a positive value such as 12h, 7d, or 2w"
        )
    amount = int(match.group("value"))
    unit = match.group("unit").lower()
    arguments = {
        "h": {"hours": amount},
        "d": {"days": amount},
        "w": {"weeks": amount},
    }[unit]
    return timedelta(**arguments)


def _parse_byte_size(value: str) -> int:
    match = _BYTE_SIZE.fullmatch(value.strip())
    if match is None:
        raise argparse.ArgumentTypeError(
            "byte limit must be a positive value such as 512KiB, 256MiB, or 1GiB"
        )
    amount = int(match.group("value"))
    multiplier = {
        "b": 1,
        "kib": 1024,
        "mib": 1024 * 1024,
        "gib": 1024 * 1024 * 1024,
    }[match.group("unit").lower()]
    return amount * multiplier


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("session limit must be positive")
    return parsed


def add_behavior_parser(commands: argparse._SubParsersAction) -> None:
    behavior = commands.add_parser(
        "behavior",
        help="inspect bounded provider-session evidence for behavioral findings",
    )
    subcommands = behavior.add_subparsers(dest="behavior_command", required=True)
    audit = subcommands.add_parser(
        "audit",
        help="audit recent or incident-marked sessions without storing content",
    )
    audit.add_argument(
        "--provider",
        choices=("all", "codex", "claude-code"),
        default="all",
    )
    audit.add_argument("--cwd", type=Path, default=Path.cwd())
    audit.add_argument("--since", type=_parse_duration, default=DEFAULT_LOOKBACK)
    audit.add_argument(
        "--sessions",
        type=_positive_integer,
        default=DEFAULT_SESSION_LIMIT,
        help="maximum sessions to audit (default: 20)",
    )
    audit.add_argument(
        "--max-bytes",
        type=_parse_byte_size,
        default=DEFAULT_SOURCE_BYTE_LIMIT,
        help="maximum source bytes to inspect (default: 256MiB)",
    )
    audit.add_argument(
        "--session-id",
        help="audit one exact session, still subject to the source-byte cap",
    )
    audit.add_argument(
        "--trace-root",
        type=Path,
        help="override Codex trace discovery root for testing or recovery",
    )
    audit.add_argument(
        "--evidence-schema-version", type=int, choices=(1, 2), default=2
    )
    audit.add_argument(
        "--gate-log",
        type=Path,
        default=DEFAULT_GATE_LOG,
        help="gate hook latency log to join for the I3 gate-cost measure",
    )
    audit.add_argument(
        "--no-gate-log",
        action="store_true",
        help="skip the gate-cost join entirely",
    )
    audit.add_argument("--json", action="store_true")

    gate = subcommands.add_parser(
        "gate-cost",
        help="I3 from the gate decision log: read-only turns while a plan was pending",
    )
    gate.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_GATE_LOG,
        help="gate hook latency log (default: the hook's own log in the temp directory)",
    )
    gate.add_argument("--since", type=_parse_duration, default=DEFAULT_LOOKBACK)
    gate.add_argument("--session-id", help="report one exact session")
    gate.add_argument("--json", action="store_true")


def run_behavior(args: argparse.Namespace) -> int:
    if args.behavior_command == "gate-cost":
        return _run_gate_cost(args)
    if args.behavior_command != "audit":
        raise ValueError(f"unsupported behavior command: {args.behavior_command}")
    providers = ("codex", "claude-code") if args.provider == "all" else (args.provider,)
    path, report = audit_behavior(
        resolve_state_home(),
        cwd=args.cwd,
        providers=providers,
        lookback=args.since,
        session_limit=args.sessions,
        source_byte_limit=args.max_bytes,
        session_id=args.session_id,
        trace_root=args.trace_root,
        evidence_schema_version=args.evidence_schema_version,
        gate_log=None if args.no_gate_log else args.gate_log,
    )
    if args.json:
        print(canonical_json(report))
        return 0
    selection = report["selection"]
    summary = report["summary"]
    print(
        "Behavior audit: "
        f"{selection['audited_session_count']} sessions, "
        f"{selection['source_bytes']} source bytes, "
        f"{summary['reportable_finding_count']} reportable findings, "
        f"{summary['suppressed_finding_count']} suppressed by barriers"
    )
    for finding in report["findings"]:
        print(
            f"- {finding['status']}: {finding['detector']} "
            f"({finding['verification_status']}) in {finding['provider']}/"
            f"{finding['session_id']}"
        )
    _print_gate_summary(report["gate_decisions"])
    print(f"Saved: {path}")
    return 0


def _run_gate_cost(args: argparse.Namespace) -> int:
    report = gate_cost_report(
        args.log,
        since=datetime.now(UTC) - args.since,
        session_id=args.session_id,
    )
    if args.json:
        print(canonical_json(report))
        return 0
    if report["status"] != "measured":
        print(f"Gate cost: {report['status']} (no gate log at the given path)")
        return 0
    _print_gate_summary(report)
    for session in report["sessions"]:
        joined = "joined" if session["decisions_joined"] else "no decisions joined"
        print(
            f"- {session['session_id']}: I3 max {session['i3_max']}, "
            f"{session['completed_turn_count']}/{session['turn_count']} turns completed, "
            f"{session['decision_count']} decisions ({joined})"
        )
        for objective in session["objectives"]:
            if objective["pending_readonly_turns"]:
                print(
                    f"    objective {objective['index']}: "
                    f"{objective['pending_readonly_turns']} read-only turn(s) while pending "
                    f"({objective['pending_plan_only_turns']} with no tools at all)"
                )
    return 0


def _print_gate_summary(report: dict) -> None:
    status = report.get("status")
    if status != "measured":
        print(f"Gate cost: {status}")
        return
    summary = report["summary"]
    print(
        "Gate cost (I3): "
        f"{summary['session_count']} sessions in the log window, "
        f"{summary['sessions_with_decisions']} with gate decisions, "
        f"{summary['decision_count']} decisions, "
        f"I3 max {summary['i3_max']} (target {report['i3_target']}), "
        f"{len(summary['sessions_over_target'])} over target"
    )

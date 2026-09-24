"""Read the start-of-turn gate's decision log and compute the I3 gate-cost measure.

The operator's gate hook appends one tab-separated line per event to its
latency log: ``<epoch ms>\\t<event>\\t<detail>``. Four event kinds matter here:

- ``prompt`` and ``prompt-midturn``: ``<session id> <prompt excerpt>``. The
  excerpt is prompt content and is dropped at parse time; only the timestamp
  and session id survive.
- ``tool``: ``<session id> <tool name>`` for every PreToolUse.
- ``stop``: ``<session id>`` when a turn completes.
- ``gate``: ``<8-char session prefix> <before>-><after>`` for every gate state
  write (added by the 2026-09-23 gate intervention).

From those, a turn is a ``prompt`` up to the next ``stop`` (completed) or the
next ``prompt`` (abandoned). The gate state at a turn is the last decision for
that session at or shortly after the prompt. An objective starts each time the
gate opens (``* -> approved`` from a non-approved state). I3 is, per objective,
the number of completed turns that used only read-only tools while the gate
was pending: the turns the agent spent waiting on a plan instead of acting.

Nothing from the log is stored beyond timestamps, session ids, tool names,
state names, and counts.
"""

from __future__ import annotations

import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import sha256_bytes

GATE_DECISIONS_SCHEMA_VERSION = 1
DEFAULT_GATE_LOG = Path(tempfile.gettempdir()) / "claude-tts" / "latency.log"
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
I3_TARGET = 1  # docs/cases/2026-09-09-start-of-turn-gate.md: at most one per objective

# Mirrors the gate hook's GATE_ALLOW list: tools that only look at things.
READ_ONLY_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "LS",
        "ToolSearch",
        "ListSkills",
        "ListAgents",
        "AskUserQuestion",
        "WebFetch",
        "WebSearch",
        "TodoWrite",
        "TodoRead",
        "ListMcpResourcesTool",
        "ReadMcpResourceTool",
        "Monitor",
        "TaskOutput",
    }
)
_GATE_STATES = frozenset({"none", "pending", "approved", "chat"})
_PREFIX_LENGTH = 8
# A decision belongs to the last turn that started before it. The gate line and
# the prompt line are written by two hooks on the same prompt, in either order
# a few milliseconds apart, so a decision this close before the next prompt is
# credited to that next turn.
_DECISION_SLACK_MS = 250


def _ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def _timestamp(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_gate_log(
    path: Path,
    *,
    since: datetime | None = None,
    session_id: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return content-free events from the hook log plus a source boundary.

    Only the newest ``max_bytes`` of the file are read. Prompt excerpts are
    discarded during parsing and never leave this function.
    """

    if max_bytes < 1:
        raise ValueError("gate log byte limit must be positive")
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
            handle.readline()  # drop the partial line at the cut
        raw = handle.read()
    boundary = {
        "path_sha256": sha256_bytes(str(path).encode("utf-8")),
        "file_bytes": size,
        "read_bytes": len(raw),
        "truncated": size > max_bytes,
    }
    cutoff = _ms(since) if since is not None else None
    events: list[dict[str, Any]] = []
    invalid = 0
    for line in raw.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 2 or not parts[0].isdigit():
            if line.strip():
                invalid += 1
            continue
        ts = int(parts[0])
        if cutoff is not None and ts < cutoff:
            continue
        kind = parts[1]
        detail = parts[2] if len(parts) > 2 else ""
        head, _, tail = detail.partition(" ")
        if kind in {"prompt", "prompt-midturn"}:
            if not head or (session_id is not None and head != session_id):
                continue
            events.append({"ts": ts, "kind": kind, "session_id": head})
        elif kind == "tool":
            if not head or (session_id is not None and head != session_id):
                continue
            events.append(
                {"ts": ts, "kind": kind, "session_id": head, "tool": tail.strip() or None}
            )
        elif kind == "stop":
            if not head or (session_id is not None and head != session_id):
                continue
            events.append({"ts": ts, "kind": kind, "session_id": head})
        elif kind == "gate":
            before, arrow, after = tail.strip().partition("->")
            if not head or not arrow or before not in _GATE_STATES or after not in _GATE_STATES:
                invalid += 1
                continue
            if session_id is not None and not session_id.startswith(head):
                continue
            events.append(
                {
                    "ts": ts,
                    "kind": kind,
                    "session_prefix": head,
                    "before": before,
                    "after": after,
                }
            )
    boundary["invalid_line_count"] = invalid
    boundary["event_count"] = len(events)
    return events, boundary


def gate_cost(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Join gate decisions to turns and compute I3 per objective per session."""

    ordered = sorted(events, key=lambda item: item["ts"])
    sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    decisions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in ordered:
        if event["kind"] == "gate":
            decisions[event["session_prefix"]].append(event)
        else:
            sessions[event["session_id"]].append(event)

    by_prefix: dict[str, list[str]] = defaultdict(list)
    for sid in sessions:
        by_prefix[sid[:_PREFIX_LENGTH]].append(sid)
    ambiguous = sorted(prefix for prefix, ids in by_prefix.items() if len(ids) > 1)
    unmatched = {
        prefix: len(items)
        for prefix, items in decisions.items()
        if prefix not in by_prefix
    }

    reports: list[dict[str, Any]] = []
    for sid, items in sorted(sessions.items()):
        prefix = sid[:_PREFIX_LENGTH]
        session_decisions = [] if prefix in ambiguous else decisions.get(prefix, [])
        turns = _turns(items)
        objectives: list[dict[str, Any]] = []
        transitions: Counter[str] = Counter(
            f"{item['before']}->{item['after']}" for item in session_decisions
        )
        state = "none"
        cursor = 0
        current: dict[str, Any] | None = None
        for position, turn in enumerate(turns):
            if position + 1 < len(turns):
                limit = turns[position + 1]["ts"] - _DECISION_SLACK_MS
            else:
                limit = None
            while cursor < len(session_decisions) and (
                limit is None or session_decisions[cursor]["ts"] < limit
            ):
                decision = session_decisions[cursor]
                if decision["after"] == "approved" and decision["before"] != "approved":
                    current = {
                        "index": sum(1 for item in objectives if item["approved_at"]) + 1,
                        "approved_at": _timestamp(decision["ts"]),
                        "turns": 0,
                        "pending_turns": 0,
                        "pending_readonly_turns": 0,
                        "pending_plan_only_turns": 0,
                    }
                    objectives.append(current)
                state = decision["after"]
                cursor += 1
            turn["state"] = state
            if current is None:
                # Turns before the first approval belong to objective 0: the
                # request that has not been approved yet.
                current = {
                    "index": 0,
                    "approved_at": None,
                    "turns": 0,
                    "pending_turns": 0,
                    "pending_readonly_turns": 0,
                    "pending_plan_only_turns": 0,
                }
                objectives.append(current)
            current["turns"] += 1
            if state == "pending":
                current["pending_turns"] += 1
                if turn["completed"] and turn["read_only"]:
                    current["pending_readonly_turns"] += 1
                    if not turn["tool_count"]:
                        current["pending_plan_only_turns"] += 1
        i3_values = [item["pending_readonly_turns"] for item in objectives]
        reports.append(
            {
                "session_id": sid,
                "session_prefix": prefix,
                "decision_count": len(session_decisions),
                "transitions": dict(sorted(transitions.items())),
                "turn_count": len(turns),
                "completed_turn_count": sum(1 for turn in turns if turn["completed"]),
                "tool_request_count": sum(turn["tool_count"] for turn in turns),
                "objectives": objectives,
                "i3_max": max(i3_values, default=0),
                "i3_total": sum(i3_values),
                "i3_over_target": bool(i3_values) and max(i3_values) > I3_TARGET,
                "decisions_joined": prefix not in ambiguous and prefix in decisions,
            }
        )

    return {
        "schema_version": GATE_DECISIONS_SCHEMA_VERSION,
        "i3_target": I3_TARGET,
        "sessions": reports,
        "summary": {
            "session_count": len(reports),
            "sessions_with_decisions": sum(1 for item in reports if item["decision_count"]),
            "decision_count": sum(len(items) for items in decisions.values()),
            "unmatched_prefix_decision_count": sum(unmatched.values()),
            "ambiguous_prefixes": ambiguous,
            "i3_max": max((item["i3_max"] for item in reports), default=0),
            "sessions_over_target": sorted(
                item["session_id"] for item in reports if item["i3_over_target"]
            ),
        },
        "privacy": (
            "Prompt excerpts in the hook log are discarded at parse time. Only "
            "timestamps, session ids, tool names, gate state names, and counts "
            "are kept."
        ),
    }


def _turns(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for event in items:
        kind = event["kind"]
        if kind == "prompt":
            current = {
                "ts": event["ts"],
                "completed": False,
                "tools": Counter(),
                "tool_count": 0,
            }
            turns.append(current)
        elif current is None:
            continue
        elif kind == "tool":
            name = event.get("tool") or "unknown"
            current["tools"][name] += 1
            current["tool_count"] += 1
        elif kind == "stop":
            current["completed"] = True
            current = None
    for turn in turns:
        turn["read_only"] = all(name in READ_ONLY_TOOLS for name in turn["tools"])
        turn["tools"] = dict(sorted(turn["tools"].items()))
    return turns


def gate_cost_report(
    path: Path | None = None,
    *,
    since: datetime | None = None,
    session_id: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read the gate log and return the I3 report, or a ``no-log`` stub."""

    selected = DEFAULT_GATE_LOG if path is None else Path(path)
    generated = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    if not selected.is_file():
        return {
            "schema_version": GATE_DECISIONS_SCHEMA_VERSION,
            "status": "no-log",
            "generated_at": generated,
            "log": {"path_sha256": sha256_bytes(str(selected).encode("utf-8"))},
        }
    events, boundary = parse_gate_log(
        selected, since=since, session_id=session_id, max_bytes=max_bytes
    )
    report = gate_cost(events)
    report["status"] = "measured"
    report["generated_at"] = generated
    report["log"] = boundary
    report["since"] = since.isoformat().replace("+00:00", "Z") if since else None
    return report


def gate_findings(
    report: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Sessions whose I3 exceeds the target, keyed by session id."""

    if report.get("status") != "measured":
        return {}
    return {
        item["session_id"]: item
        for item in report.get("sessions", ())
        if item.get("i3_over_target")
    }

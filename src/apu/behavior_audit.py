from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from .audit import build_inventory
from .behavior_watch import codex_trace_root
from .evidence import (
    ingest_codex_trace,
    read_evidence,
    reconcile_evidence,
    validate_evidence_event,
)
from .models import canonical_json, sha256_bytes
from .state import ensure_state_home, write_json_atomic

AUDIT_SCHEMA_VERSION = 1
DEFAULT_LOOKBACK = timedelta(days=7)
DEFAULT_SESSION_LIMIT = 20
DEFAULT_SOURCE_BYTE_LIMIT = 256 * 1024 * 1024
_DISCOVERY_FILE_LIMIT = 1000
_STALE_ACTIVE_AFTER = timedelta(minutes=10)
_VERIFICATION_ORDER = {
    "contradicted": 5,
    "stale": 4,
    "unverifiable": 3,
    "asserted": 2,
    "observed": 1,
    "verified": 0,
}
_MUTATING_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "edit",
        "edit_file",
        "replace",
        "write",
        "write_file",
    }
)


@dataclass(frozen=True)
class AuditCandidate:
    provider: str
    session_id: str
    cwd: Path
    source_path: Path
    source_bytes: int
    modified_at: datetime
    source_kind: str
    marked: bool


def _timestamp(now: datetime | None = None) -> str:
    selected = datetime.now(UTC) if now is None else _aware(now)
    return selected.isoformat().replace("+00:00", "Z")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("audit time must include a timezone")
    return value.astimezone(UTC)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _event_sort_key(event: Mapping[str, Any]) -> tuple[datetime, int, str]:
    observed_at = _parse_timestamp(event.get("observed_at")) or datetime.min.replace(
        tzinfo=UTC
    )
    sequence = event.get("sequence")
    return (
        observed_at,
        sequence if isinstance(sequence, int) else 0,
        str(event.get("event_id", "")),
    )


def _same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(first.resolve(strict=False)) == os.path.normcase(
        second.resolve(strict=False)
    )


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _peek_codex_trace(path: Path) -> tuple[str, Path] | None:
    consumed = 0
    with path.open("rb") as stream:
        for raw in stream:
            consumed += len(raw)
            if consumed > 1024 * 1024:
                return None
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(record, Mapping) or record.get("type") != "session_meta":
                continue
            payload = record.get("payload")
            if not isinstance(payload, Mapping):
                continue
            session_id = payload.get("id") or payload.get("session_id")
            cwd = payload.get("cwd")
            if (
                isinstance(session_id, str)
                and session_id
                and isinstance(cwd, str)
                and Path(cwd).is_absolute()
            ):
                return session_id, Path(cwd).resolve(strict=False)
    return None


def _incident_context(state_home: Path) -> dict[str, list[dict[str, Any]]]:
    directory = Path(state_home) / "behavior" / "incidents"
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not directory.is_dir():
        return result
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        session = value.get("session") if isinstance(value, Mapping) else None
        session_id = session.get("session_id") if isinstance(session, Mapping) else None
        if isinstance(session_id, str) and session_id:
            result[session_id].append(dict(value))
    return result


def _discover_codex_candidates(
    *,
    trace_root: Path,
    cwd: Path,
    cutoff: datetime,
    marked_sessions: set[str],
    marked_trace_paths: set[Path],
    session_id: str | None,
) -> tuple[list[AuditCandidate], list[dict[str, Any]]]:
    root = Path(trace_root).expanduser().resolve()
    if not root.is_dir():
        return [], [
            {
                "provider": "codex",
                "session_id": None,
                "reason": "source-root-unavailable",
                "source_path_sha256": sha256_bytes(str(root).encode("utf-8")),
                "source_bytes": 0,
            }
        ]
    candidates: list[AuditCandidate] = []
    skipped: list[dict[str, Any]] = []
    discovered: list[tuple[Path, os.stat_result]] = []
    for path in root.rglob("*.jsonl"):
        if not path.is_file() or not _within(root, path):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        discovered.append((path, stat))
    discovered.sort(
        key=lambda item: (
            not (
                item[0].resolve(strict=False) in marked_trace_paths
                or (session_id is not None and session_id in item[0].name)
            ),
            -item[1].st_mtime_ns,
            str(item[0]),
        )
    )
    if len(discovered) > _DISCOVERY_FILE_LIMIT:
        skipped.append(
            {
                "provider": "codex",
                "session_id": None,
                "reason": "discovery-file-limit",
                "source_path_sha256": sha256_bytes(str(root).encode("utf-8")),
                "source_bytes": 0,
                "skipped_file_count": len(discovered) - _DISCOVERY_FILE_LIMIT,
            }
        )
    for path, stat in discovered[:_DISCOVERY_FILE_LIMIT]:
        try:
            peeked = _peek_codex_trace(path)
        except OSError:
            continue
        if peeked is None:
            skipped.append(
                {
                    "provider": "codex",
                    "session_id": None,
                    "reason": "session-metadata-unavailable",
                    "source_path_sha256": sha256_bytes(
                        str(path.resolve()).encode("utf-8")
                    ),
                    "source_bytes": stat.st_size,
                }
            )
            continue
        selected_id, selected_cwd = peeked
        modified = datetime.fromtimestamp(stat.st_mtime, UTC)
        if session_id is not None and selected_id != session_id:
            continue
        if not _same_path(selected_cwd, cwd):
            continue
        if (
            session_id is None
            and selected_id not in marked_sessions
            and modified < cutoff
        ):
            skipped.append(
                {
                    "provider": "codex",
                    "session_id": selected_id,
                    "reason": "outside-lookback",
                    "source_path_sha256": sha256_bytes(
                        str(path.resolve()).encode("utf-8")
                    ),
                    "source_bytes": stat.st_size,
                }
            )
            continue
        candidates.append(
            AuditCandidate(
                provider="codex",
                session_id=selected_id,
                cwd=selected_cwd,
                source_path=path.resolve(),
                source_bytes=stat.st_size,
                modified_at=modified,
                source_kind="transcript",
                marked=selected_id in marked_sessions,
            )
        )
    return candidates, skipped


def _read_evidence_file(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"invalid evidence at {path}:{line_number}: {error}"
                ) from error
            validate_evidence_event(event)
            events.append(event)
    return events


def _event_cwd(events: Iterable[Mapping[str, Any]]) -> Path | None:
    for event in events:
        value = event.get("state", {}).get("cwd")
        if isinstance(value, str) and Path(value).is_absolute():
            return Path(value).resolve(strict=False)
    return None


def _latest_event_time(
    events: Iterable[Mapping[str, Any]], fallback: datetime
) -> datetime:
    timestamps = [
        parsed
        for parsed in (_parse_timestamp(event.get("observed_at")) for event in events)
        if parsed is not None
    ]
    return max(timestamps, default=fallback)


def _discover_evidence_candidates(
    state_home: Path,
    *,
    providers: set[str],
    cwd: Path,
    cutoff: datetime,
    marked_sessions: set[str],
    session_id: str | None,
) -> tuple[list[AuditCandidate], list[dict[str, Any]]]:
    root = Path(state_home) / "behavior" / "evidence"
    if not root.is_dir():
        return [], []
    candidates: list[AuditCandidate] = []
    skipped: list[dict[str, Any]] = []
    for provider_dir in sorted(root.iterdir()):
        if not provider_dir.is_dir() or provider_dir.name not in providers:
            continue
        priority_names = {
            f"{sha256(value.encode('utf-8')).hexdigest()}.jsonl"
            for value in marked_sessions
        }
        if session_id is not None:
            priority_names.add(
                f"{sha256(session_id.encode('utf-8')).hexdigest()}.jsonl"
            )
        discovered: list[tuple[Path, os.stat_result]] = []
        for path in provider_dir.glob("*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            discovered.append((path, stat))
        discovered.sort(
            key=lambda item: (
                item[0].name not in priority_names,
                -item[1].st_mtime_ns,
                str(item[0]),
            )
        )
        if len(discovered) > _DISCOVERY_FILE_LIMIT:
            skipped.append(
                {
                    "provider": provider_dir.name,
                    "session_id": None,
                    "reason": "discovery-file-limit",
                    "source_path_sha256": sha256_bytes(
                        str(provider_dir.resolve()).encode("utf-8")
                    ),
                    "source_bytes": 0,
                    "skipped_file_count": len(discovered) - _DISCOVERY_FILE_LIMIT,
                }
            )
        for path, stat in discovered[:_DISCOVERY_FILE_LIMIT]:
            try:
                events = _read_evidence_file(path)
            except (OSError, TypeError, ValueError) as error:
                skipped.append(
                    {
                        "provider": provider_dir.name,
                        "session_id": None,
                        "reason": f"invalid-evidence:{type(error).__name__}",
                        "source_path_sha256": sha256_bytes(
                            str(path.resolve()).encode("utf-8")
                        ),
                        "source_bytes": stat.st_size,
                    }
                )
                continue
            if not events:
                continue
            selected_id = events[0]["session_id"]
            selected_cwd = _event_cwd(events)
            modified = _latest_event_time(
                events, datetime.fromtimestamp(stat.st_mtime, UTC)
            )
            if session_id is not None and selected_id != session_id:
                continue
            if selected_cwd is None or not _same_path(selected_cwd, cwd):
                continue
            if (
                session_id is None
                and selected_id not in marked_sessions
                and modified < cutoff
            ):
                skipped.append(
                    {
                        "provider": provider_dir.name,
                        "session_id": selected_id,
                        "reason": "outside-lookback",
                        "source_path_sha256": sha256_bytes(
                            str(path.resolve()).encode("utf-8")
                        ),
                        "source_bytes": stat.st_size,
                    }
                )
                continue
            candidates.append(
                AuditCandidate(
                    provider=provider_dir.name,
                    session_id=selected_id,
                    cwd=selected_cwd,
                    source_path=path.resolve(),
                    source_bytes=stat.st_size,
                    modified_at=modified,
                    source_kind="evidence",
                    marked=selected_id in marked_sessions,
                )
            )
    return candidates, skipped


def _deduplicate_candidates(
    candidates: Iterable[AuditCandidate],
) -> list[AuditCandidate]:
    selected: dict[tuple[str, str], AuditCandidate] = {}
    for candidate in candidates:
        key = (candidate.provider, candidate.session_id)
        current = selected.get(key)
        if current is None:
            selected[key] = candidate
            continue
        if (
            candidate.source_kind == "transcript"
            and current.source_kind != "transcript"
        ):
            selected[key] = candidate
            continue
        if (
            candidate.source_kind == current.source_kind
            and candidate.modified_at > current.modified_at
        ):
            selected[key] = candidate
    return list(selected.values())


def _select_candidates(
    candidates: Iterable[AuditCandidate],
    *,
    session_limit: int,
    source_byte_limit: int,
) -> tuple[list[AuditCandidate], list[dict[str, Any]], int]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            not item.marked,
            -item.modified_at.timestamp(),
            item.provider,
            item.session_id,
        ),
    )
    selected: list[AuditCandidate] = []
    skipped: list[dict[str, Any]] = []
    consumed = 0
    for candidate in ordered:
        if len(selected) >= session_limit:
            reason = "session-limit"
        elif consumed + candidate.source_bytes > source_byte_limit:
            reason = "source-byte-limit"
        else:
            selected.append(candidate)
            consumed += candidate.source_bytes
            continue
        skipped.append(
            {
                "provider": candidate.provider,
                "session_id": candidate.session_id,
                "reason": reason,
                "source_path_sha256": sha256_bytes(
                    str(candidate.source_path).encode("utf-8")
                ),
                "source_bytes": candidate.source_bytes,
            }
        )
    return selected, skipped, consumed


def _already_ingested_unchanged(state_home: Path, candidate: AuditCandidate) -> bool:
    events = read_evidence(state_home, candidate.provider, candidate.session_id)
    boundaries = [
        event["source"]["snapshot_bytes"]
        for event in events
        if event["source_kind"] == "transcript"
        and event["source"]["path"] == str(candidate.source_path)
        and event["source"]["snapshot_bytes"] is not None
    ]
    return bool(boundaries) and max(boundaries) == candidate.source_bytes


def _verify_events(events: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    stored = [dict(event) for event in events]
    results: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in stored:
        source = event["source"]
        path = source["path"]
        boundary = source["snapshot_bytes"]
        if event["source_kind"] != "transcript" or path is None or boundary is None:
            results[event["event_id"]] = {
                "status": event["verification_status"],
                "reason_codes": ["source-is-not-replayable"],
            }
            continue
        grouped[path].append(event)
    for path_value, members in grouped.items():
        path = Path(path_value)
        if not path.is_file():
            for event in members:
                results[event["event_id"]] = {
                    "status": "unverifiable",
                    "reason_codes": ["source-missing"],
                }
            continue
        boundaries = sorted({event["source"]["snapshot_bytes"] for event in members})
        maximum_boundary = boundaries[-1]
        with path.open("rb") as stream:
            prefix = stream.read(maximum_boundary)
        prefix_hashes: dict[int, str] = {}
        digest = sha256()
        cursor = 0
        for boundary in boundaries:
            if len(prefix) < boundary:
                continue
            digest.update(prefix[cursor:boundary])
            prefix_hashes[boundary] = digest.hexdigest()
            cursor = boundary
        needed_lines = {
            event["source"]["line"]
            for event in members
            if event["source"]["line"] is not None
        }
        line_records: dict[int, tuple[str, int]] = {}
        offset = 0
        for line_number, raw in enumerate(BytesIO(prefix), start=1):
            offset += len(raw)
            if line_number in needed_lines:
                line_records[line_number] = (
                    sha256_bytes(raw.rstrip(b"\r\n")),
                    offset,
                )
        for event in members:
            source = event["source"]
            boundary = source["snapshot_bytes"]
            if len(prefix) < boundary:
                status, reason = "stale", "source-truncated"
            elif prefix_hashes[boundary] != source["snapshot_sha256"]:
                status, reason = "contradicted", "source-prefix-changed"
            else:
                status, reason = "verified", "source-prefix-and-record-match"
            line_number = event["source"]["line"]
            if status == "verified" and line_number is not None:
                line_record = line_records.get(line_number)
                if line_record is None or line_record[1] > boundary:
                    status, reason = "contradicted", "source-line-missing"
                elif line_record[0] != event["source"]["record_sha256"]:
                    status, reason = "contradicted", "source-record-changed"
            results[event["event_id"]] = {
                "status": status,
                "reason_codes": [reason],
            }
    return results


def _worst_status(statuses: Iterable[str]) -> str:
    values = list(statuses)
    return max(
        values, key=lambda value: _VERIFICATION_ORDER[value], default="unverifiable"
    )


def _finding(
    *,
    detector: str,
    severity: str,
    session_id: str,
    provider: str,
    evidence_refs: Iterable[str],
    verification: Mapping[str, Mapping[str, Any]],
    summary: str,
    reason_codes: Iterable[str],
    status: str = "reportable",
    barrier_codes: Iterable[str] = (),
    surface_refs: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    refs = sorted(set(evidence_refs))
    statuses = [
        verification[reference]["status"]
        for reference in refs
        if reference in verification
    ]
    identity = {
        "detector": detector,
        "provider": provider,
        "session_id": session_id,
        "evidence_refs": refs,
        "reason_codes": sorted(set(reason_codes)),
    }
    return {
        "finding_id": f"finding-{sha256(canonical_json(identity).encode('utf-8')).hexdigest()}",
        "detector": detector,
        "severity": severity,
        "status": status,
        "provider": provider,
        "session_id": session_id,
        "summary": summary,
        "reason_codes": sorted(set(reason_codes)),
        "evidence_refs": refs,
        "verification_status": _worst_status(statuses),
        "barrier_codes": sorted(set(barrier_codes)),
        "surface_refs": [dict(item) for item in surface_refs],
    }


def _is_mutation(event: Mapping[str, Any]) -> bool:
    observation = event["observation"]
    tool_name = observation["tool_name"]
    return (
        isinstance(tool_name, str) and tool_name.lower() in _MUTATING_TOOL_NAMES
    ) or observation["command_class"] == "git-write"


def _evidence_findings(
    events: list[dict[str, Any]],
    *,
    provider: str,
    session_id: str,
    verification: Mapping[str, Mapping[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    ordered = sorted(events, key=_event_sort_key)
    positions = {event["event_id"]: index for index, event in enumerate(ordered)}
    requests = {
        event["correlation_sha256"]: event
        for event in ordered
        if event["event_type"] == "tool.requested" and event["correlation_sha256"]
    }
    results = {
        event["correlation_sha256"]: event
        for event in ordered
        if event["event_type"] in {"tool.completed", "tool.failed"}
        and event["correlation_sha256"]
    }
    findings: list[dict[str, Any]] = []
    repeated: dict[str, list[str]] = defaultdict(list)
    for correlation, result in results.items():
        request = requests.get(correlation)
        if request is None or result["event_type"] != "tool.failed":
            continue
        input_sha = request["observation"]["input_sha256"]
        if input_sha:
            repeated[input_sha].extend([request["event_id"], result["event_id"]])
    for refs in repeated.values():
        if len(refs) < 4:
            continue
        findings.append(
            _finding(
                detector="repeated-identical-tool-failure",
                severity="medium",
                session_id=session_id,
                provider=provider,
                evidence_refs=refs,
                verification=verification,
                summary="The same tool input failed more than once without new evidence.",
                reason_codes=["repeated-identical-tool-failure"],
            )
        )
    denied = [event for event in ordered if event["event_type"] == "permission.denied"]
    if len(denied) > 1:
        findings.append(
            _finding(
                detector="repeated-permission-denial",
                severity="medium",
                session_id=session_id,
                provider=provider,
                evidence_refs=[event["event_id"] for event in denied],
                verification=verification,
                summary="The session continued requesting actions after repeated denials.",
                reason_codes=["repeated-permission-denial"],
            )
        )
    terminal_positions = [
        positions[event["event_id"]]
        for event in ordered
        if event["event_type"]
        in {"task.completed", "turn.completed", "turn.failed", "session.ended"}
    ]
    start_positions = [
        positions[event["event_id"]]
        for event in ordered
        if event["event_type"] in {"task.started", "turn.started", "session.started"}
    ]
    terminal = bool(terminal_positions) and max(terminal_positions) > max(
        start_positions, default=-1
    )
    latest_time = max(
        (
            parsed
            for parsed in (_parse_timestamp(event["observed_at"]) for event in ordered)
            if parsed is not None
        ),
        default=now,
    )
    old_incomplete = now - latest_time >= _STALE_ACTIVE_AFTER
    unresolved = [
        request
        for correlation, request in requests.items()
        if correlation not in results
    ]
    if unresolved and (terminal or old_incomplete):
        findings.append(
            _finding(
                detector="unresolved-tool-request",
                severity="low",
                session_id=session_id,
                provider=provider,
                evidence_refs=[event["event_id"] for event in unresolved],
                verification=verification,
                summary="One or more tool requests have no matching result in a stopped session.",
                reason_codes=["tool-request-without-result"],
            )
        )
    orphaned = [
        result for correlation, result in results.items() if correlation not in requests
    ]
    if orphaned:
        findings.append(
            _finding(
                detector="orphaned-tool-result",
                severity="low",
                session_id=session_id,
                provider=provider,
                evidence_refs=[event["event_id"] for event in orphaned],
                verification=verification,
                summary="Tool results could not be linked to an observed request.",
                reason_codes=["tool-result-without-request"],
            )
        )
    completions = [
        event
        for event in ordered
        if event["event_type"] in {"task.completed", "turn.completed"}
    ]
    previous_completion_position = -1
    for completion in completions:
        completion_position = positions[completion["event_id"]]
        preceding_starts = [
            positions[event["event_id"]]
            for event in ordered
            if event["event_type"] in {"task.started", "turn.started"}
            and previous_completion_position
            < positions[event["event_id"]]
            < completion_position
        ]
        segment_start = max(
            [previous_completion_position, *preceding_starts],
        )
        following_starts = [
            positions[event["event_id"]]
            for event in ordered
            if event["event_type"] in {"task.started", "turn.started"}
            and positions[event["event_id"]] > completion_position
        ]
        segment_end = min(following_starts, default=len(ordered))
        later_tools = [
            event
            for event in ordered
            if event["event_type"] == "tool.requested"
            and completion_position < positions[event["event_id"]] < segment_end
        ]
        if later_tools:
            findings.append(
                _finding(
                    detector="post-completion-activity",
                    severity="low",
                    session_id=session_id,
                    provider=provider,
                    evidence_refs=[completion["event_id"]]
                    + [event["event_id"] for event in later_tools],
                    verification=verification,
                    summary="Tool activity occurred after a completion event.",
                    reason_codes=["activity-after-completion"],
                )
            )
        state_events = [
            event
            for event in ordered
            if event["event_type"] == "state.observed"
            and segment_start < positions[event["event_id"]] < segment_end
        ]
        if state_events:
            latest_state = state_events[-1]
            if positions[latest_state["event_id"]] < completion_position:
                findings.append(
                    _finding(
                        detector="stale-state-at-completion",
                        severity="low",
                        session_id=session_id,
                        provider=provider,
                        evidence_refs=[
                            latest_state["event_id"],
                            completion["event_id"],
                        ],
                        verification=verification,
                        summary="The latest repository observation predates task completion.",
                        reason_codes=["state-observation-before-completion"],
                    )
                )
            elif latest_state["state"]["dirty"] is True:
                findings.append(
                    _finding(
                        detector="dirty-state-at-completion",
                        severity="low",
                        session_id=session_id,
                        provider=provider,
                        evidence_refs=[
                            latest_state["event_id"],
                            completion["event_id"],
                        ],
                        verification=verification,
                        summary="The repository was dirty at the post-completion observation.",
                        reason_codes=["dirty-state-after-completion"],
                    )
                )
        successful_tests: list[dict[str, Any]] = []
        for correlation, result in results.items():
            request = requests.get(correlation)
            if (
                request is not None
                and request["observation"]["command_class"] == "test"
                and result["event_type"] == "tool.completed"
                and segment_start < positions[result["event_id"]] < completion_position
            ):
                successful_tests.append(result)
        mutations = [
            event
            for event in ordered
            if event["event_type"] == "tool.requested"
            and _is_mutation(event)
            and segment_start < positions[event["event_id"]] < completion_position
        ]
        latest_test = max(
            successful_tests,
            key=lambda event: positions[event["event_id"]],
            default=None,
        )
        latest_mutation = max(
            mutations,
            key=lambda event: positions[event["event_id"]],
            default=None,
        )
        if (
            latest_test is not None
            and latest_mutation is not None
            and positions[latest_mutation["event_id"]]
            > positions[latest_test["event_id"]]
        ):
            refs = [completion["event_id"]]
            refs.extend([latest_test["event_id"], latest_mutation["event_id"]])
            findings.append(
                _finding(
                    detector="completion-after-stale-gate",
                    severity="medium",
                    session_id=session_id,
                    provider=provider,
                    evidence_refs=refs,
                    verification=verification,
                    summary="A mutation occurred after the latest observed successful test.",
                    reason_codes=["mutation-after-latest-test"],
                )
            )
        previous_completion_position = completion_position
    return findings


def _incident_findings(
    incidents: Iterable[Mapping[str, Any]],
    *,
    provider: str,
    session_id: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for incident in incidents:
        signals = [
            item
            for item in incident.get("observed_signals", ())
            if isinstance(item, str)
        ]
        barriers = [
            item
            for item in incident.get("possible_barriers", ())
            if isinstance(item, str)
        ]
        evidence = incident.get("evidence_plane", {})
        incident_id = str(incident.get("incident_id") or "unknown")
        refs = [
            item for item in evidence.get("event_refs", ()) if isinstance(item, str)
        ]
        refs.append(f"incident:{incident_id}")
        surfaces = [
            {
                "surface_id": surface.get("surface_id"),
                "path": surface.get("path"),
                "content_sha256": surface.get("content_sha256"),
                "binding": "incident-time",
            }
            for surface in incident.get("surface_refs", ())
            if isinstance(surface, Mapping)
        ]
        verification = {
            reference: {"status": "asserted", "reason_codes": ["operator-claim"]}
            for reference in refs
        }
        for signal in signals:
            findings.append(
                _finding(
                    detector=signal,
                    severity="informational" if barriers else "medium",
                    session_id=session_id,
                    provider=provider,
                    evidence_refs=refs,
                    verification=verification,
                    summary="An operator-marked incident matched a behavioral pressure signal.",
                    reason_codes=[signal],
                    status="suppressed" if barriers else "reportable",
                    barrier_codes=barriers,
                    surface_refs=surfaces,
                )
            )
    return findings


def _current_surfaces(cwd: Path, providers: set[str]) -> list[dict[str, Any]]:
    inventory = build_inventory(
        [cwd],
        home=Path.home(),
        working_directories=[cwd],
    )
    selected_ids: set[str] = set()
    mapped = {
        "claude-code" if provider == "claude" else provider for provider in providers
    }
    for stack in inventory.effective_stacks:
        provider = stack.get("provider")
        normalized = "claude-code" if provider == "claude" else provider
        if normalized not in mapped:
            continue
        if not _same_path(Path(str(stack.get("working_directory"))), cwd):
            continue
        selected_ids.update(str(item) for item in stack.get("surface_ids", ()))
    return [
        {
            "surface_id": surface.id,
            "provider": "claude-code"
            if surface.provider == "claude"
            else surface.provider,
            "path": surface.path,
            "content_sha256": surface.content_sha256,
            "sensitive": surface.sensitive,
            "binding": "current-at-audit",
        }
        for surface in inventory.surfaces
        if surface.id in selected_ids
    ]


def _deduplicate_findings(
    findings: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for finding in findings:
        stored = dict(finding)
        selected[stored["finding_id"]] = stored
    return sorted(
        selected.values(),
        key=lambda item: (
            item["status"] != "reportable",
            {"medium": 0, "low": 1, "informational": 2}[item["severity"]],
            item["provider"],
            item["session_id"],
            item["detector"],
        ),
    )


def audit_behavior(
    state_home: Path,
    *,
    cwd: Path | None = None,
    providers: Iterable[str] = ("codex", "claude-code"),
    lookback: timedelta = DEFAULT_LOOKBACK,
    session_limit: int = DEFAULT_SESSION_LIMIT,
    source_byte_limit: int = DEFAULT_SOURCE_BYTE_LIMIT,
    session_id: str | None = None,
    trace_root: Path | None = None,
    now: datetime | None = None,
) -> tuple[Path, dict[str, Any]]:
    if lookback <= timedelta(0):
        raise ValueError("behavior audit lookback must be positive")
    if session_limit < 1:
        raise ValueError("behavior audit session limit must be positive")
    if source_byte_limit < 1:
        raise ValueError("behavior audit source byte limit must be positive")
    selected_providers = set(providers)
    if not selected_providers or not selected_providers <= {"codex", "claude-code"}:
        raise ValueError("behavior audit provider is unsupported")
    selected_now = datetime.now(UTC) if now is None else _aware(now)
    selected_cwd = Path.cwd().resolve() if cwd is None else Path(cwd).resolve()
    cutoff = selected_now - lookback
    incidents = _incident_context(state_home)
    marked_sessions = set(incidents)
    marked_trace_paths = {
        Path(trace_path).resolve(strict=False)
        for values in incidents.values()
        for incident in values
        for session in [incident.get("session")]
        if isinstance(session, Mapping)
        for trace_path in [session.get("trace_path")]
        if isinstance(trace_path, str) and Path(trace_path).is_absolute()
    }

    candidates: list[AuditCandidate] = []
    discovery_skipped: list[dict[str, Any]] = []
    if "codex" in selected_providers:
        codex_candidates, codex_skipped = _discover_codex_candidates(
            trace_root=trace_root or codex_trace_root(),
            cwd=selected_cwd,
            cutoff=cutoff,
            marked_sessions=marked_sessions,
            marked_trace_paths=marked_trace_paths,
            session_id=session_id,
        )
        candidates.extend(codex_candidates)
        discovery_skipped.extend(codex_skipped)
    evidence_candidates, evidence_skipped = _discover_evidence_candidates(
        state_home,
        providers=selected_providers,
        cwd=selected_cwd,
        cutoff=cutoff,
        marked_sessions=marked_sessions,
        session_id=session_id,
    )
    candidates.extend(evidence_candidates)
    discovery_skipped.extend(evidence_skipped)
    unique = _deduplicate_candidates(candidates)
    selected, bounded_skipped, source_bytes = _select_candidates(
        unique,
        session_limit=session_limit,
        source_byte_limit=source_byte_limit,
    )

    session_reports: list[dict[str, Any]] = []
    all_findings: list[dict[str, Any]] = []
    for candidate in selected:
        ingest_status = "existing-evidence"
        if candidate.source_kind == "transcript":
            if _already_ingested_unchanged(state_home, candidate):
                ingest_status = "unchanged-reused"
            else:
                ingest_codex_trace(state_home, candidate.source_path)
                ingest_status = "ingested"
        events = read_evidence(state_home, candidate.provider, candidate.session_id)
        verification = _verify_events(events)
        reconciliation = reconcile_evidence(events)
        findings = _evidence_findings(
            events,
            provider=candidate.provider,
            session_id=candidate.session_id,
            verification=verification,
            now=selected_now,
        )
        findings.extend(
            _incident_findings(
                incidents.get(candidate.session_id, ()),
                provider=candidate.provider,
                session_id=candidate.session_id,
            )
        )
        findings = _deduplicate_findings(findings)
        all_findings.extend(findings)
        counts = Counter(item["status"] for item in findings)
        session_reports.append(
            {
                "provider": candidate.provider,
                "session_id": candidate.session_id,
                "modified_at": _timestamp(candidate.modified_at),
                "source_kind": candidate.source_kind,
                "source_path_sha256": sha256_bytes(
                    str(candidate.source_path).encode("utf-8")
                ),
                "source_bytes": candidate.source_bytes,
                "marked_incident_count": len(incidents.get(candidate.session_id, ())),
                "ingest_status": ingest_status,
                "event_count": len(events),
                "verification_counts": dict(
                    sorted(
                        Counter(
                            item["status"] for item in verification.values()
                        ).items()
                    )
                ),
                "reconciliation": reconciliation,
                "finding_counts": dict(sorted(counts.items())),
                "finding_ids": [item["finding_id"] for item in findings],
            }
        )

    findings = _deduplicate_findings(all_findings)
    reportable = [item for item in findings if item["status"] == "reportable"]
    suppressed = [item for item in findings if item["status"] == "suppressed"]
    audit_id = f"behavior-audit-{uuid4().hex}"
    artifact = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "audit_id": audit_id,
        "created_at": _timestamp(selected_now),
        "scope": {
            "cwd": str(selected_cwd),
            "providers": sorted(selected_providers),
            "lookback_seconds": int(lookback.total_seconds()),
            "cutoff": _timestamp(cutoff),
            "session_limit": session_limit,
            "source_byte_limit": source_byte_limit,
            "discovery_file_limit_per_provider": _DISCOVERY_FILE_LIMIT,
            "requested_session_id": session_id,
        },
        "selection": {
            "discovered_session_count": len(unique),
            "audited_session_count": len(selected),
            "source_bytes": source_bytes,
            "skipped": sorted(
                discovery_skipped + bounded_skipped,
                key=lambda item: (
                    item["reason"],
                    item["provider"],
                    item.get("session_id") or "",
                ),
            ),
        },
        "sessions": sorted(
            session_reports, key=lambda item: (item["provider"], item["session_id"])
        ),
        "findings": findings,
        "summary": {
            "reportable_finding_count": len(reportable),
            "suppressed_finding_count": len(suppressed),
            "detector_counts": dict(
                sorted(Counter(item["detector"] for item in reportable).items())
            ),
            "severity_counts": dict(
                sorted(Counter(item["severity"] for item in reportable).items())
            ),
        },
        "current_surface_context": {
            "binding": "current-at-audit",
            "surface_refs": _current_surfaces(selected_cwd, selected_providers),
        },
        "retention": {
            "event_detail_days": 30,
            "policy": (
                "Detailed normalized events are candidates for expiry after 30 days "
                "or when a linked policy monitoring window closes; this audit does "
                "not delete provider logs or APU evidence."
            ),
        },
        "privacy": (
            "The audit stores detector codes, safe metadata, hashes, source byte "
            "counts, evidence references, and surface hashes. It does not persist "
            "messages, reasoning, command text, tool input/output bodies, environment "
            "content, changed path names, or raw provider records."
        ),
    }
    ensure_state_home(state_home)
    path = Path(state_home) / "behavior" / "audits" / f"{audit_id}.json"
    write_json_atomic(path, artifact)
    write_json_atomic(
        Path(state_home) / "behavior" / "latest-audit.json",
        {"schema_version": AUDIT_SCHEMA_VERSION, "audit_id": audit_id},
    )
    return path, artifact

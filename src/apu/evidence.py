from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from .models import canonical_json, sha256_bytes
from .state import ensure_private_directory, ensure_state_home

EVIDENCE_SCHEMA_VERSION = 2
EVIDENCE_READER_VERSIONS = frozenset({1, 2})
EVIDENCE_STATUSES = frozenset(
    {"asserted", "observed", "verified", "stale", "contradicted", "unverifiable"}
)
EVIDENCE_CLASSES = frozenset({"invocation", "result", "state"})
SOURCE_KINDS = frozenset({"transcript", "hook", "state-observer"})
ATTRIBUTION_MODES = frozenset(
    {
        "exact_session_binding",
        "exact_trace_path",
        "explicit_session_id",
        "provider_hook",
        "state_observer",
        "unique_active_exact_cwd",
    }
)
ATTRIBUTION_CONFIDENCE = frozenset({"exact", "source_attested"})
_MAX_TRACE_BYTES = 256 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_FAILURE_OUTPUT = re.compile(
    r"(?:exit code:\s*[1-9]\d*|script failed|command failed)", re.IGNORECASE
)
_EXIT_CODE = re.compile(r"exit code:\s*(-?\d+)", re.IGNORECASE)

_EVENT_TYPES = frozenset(
    {
        "session.started",
        "session.ended",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "task.started",
        "task.completed",
        "tool.requested",
        "tool.completed",
        "tool.failed",
        "permission.requested",
        "permission.denied",
        "subagent.started",
        "subagent.stopped",
        "compaction.started",
        "compaction.completed",
        "state.observed",
        "provider.observed",
    }
)

_HOOK_EVENTS: dict[str, tuple[str, str]] = {
    "SessionStart": ("session.started", "state"),
    "SessionEnd": ("session.ended", "result"),
    "UserPromptSubmit": ("turn.started", "invocation"),
    "PreToolUse": ("tool.requested", "invocation"),
    "PostToolUse": ("tool.completed", "result"),
    "PostToolUseFailure": ("tool.failed", "result"),
    "PermissionRequest": ("permission.requested", "invocation"),
    "PermissionDenied": ("permission.denied", "result"),
    "SubagentStart": ("subagent.started", "invocation"),
    "SubagentStop": ("subagent.stopped", "result"),
    "TaskCreated": ("task.started", "invocation"),
    "TaskCompleted": ("task.completed", "result"),
    "PreCompact": ("compaction.started", "invocation"),
    "PostCompact": ("compaction.completed", "result"),
    "Stop": ("turn.completed", "result"),
    "StopFailure": ("turn.failed", "result"),
}


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str | None:
    if value is None:
        return None
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _required_string(value: Any, field: str, *, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"evidence {field} must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"evidence {field} contains control characters")
    return value


def _optional_label(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SAFE_LABEL.fullmatch(value):
        raise ValueError(f"evidence {field} is not a safe label")
    return value


def _optional_sha256(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"evidence {field} must be a SHA-256 digest or null")
    return value


def _parse_timestamp(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"evidence {field} must be an RFC3339 timestamp or null")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"evidence {field} must be an RFC3339 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"evidence {field} must include a timezone")
    return value


def validate_evidence_event(event: Mapping[str, Any]) -> None:
    base_fields = {
        "schema_version",
        "event_id",
        "provider",
        "source_kind",
        "session_id",
        "sequence",
        "observed_at",
        "event_type",
        "evidence_class",
        "verification_status",
        "correlation_sha256",
        "observation",
        "state",
        "source",
    }
    if not isinstance(event, Mapping):
        raise TypeError("evidence event must be an object")
    schema_version = event.get("schema_version")
    expected = (
        base_fields | {"attribution"}
        if schema_version == 2
        else base_fields
    )
    if not isinstance(event, Mapping) or set(event) != expected:
        missing = sorted(expected - set(event)) if isinstance(event, Mapping) else []
        extra = sorted(set(event) - expected) if isinstance(event, Mapping) else []
        raise ValueError(
            f"evidence fields do not match contract; missing={missing}, extra={extra}"
        )
    if event["schema_version"] not in EVIDENCE_READER_VERSIONS:
        raise ValueError("unsupported evidence schema_version")
    _required_string(event["event_id"], "event_id")
    _required_string(event["provider"], "provider", maximum=128)
    if event["source_kind"] not in SOURCE_KINDS:
        raise ValueError("evidence source_kind is unsupported")
    _required_string(event["session_id"], "session_id")
    if not isinstance(event["sequence"], int) or event["sequence"] < 0:
        raise ValueError("evidence sequence must be a non-negative integer")
    _parse_timestamp(event["observed_at"], "observed_at")
    if event["event_type"] not in _EVENT_TYPES:
        raise ValueError("evidence event_type is unsupported")
    if event["evidence_class"] not in EVIDENCE_CLASSES:
        raise ValueError("evidence evidence_class is unsupported")
    if event["verification_status"] not in EVIDENCE_STATUSES:
        raise ValueError("evidence verification_status is unsupported")
    _optional_sha256(event["correlation_sha256"], "correlation_sha256")
    _validate_observation(event["observation"])
    _validate_state(event["state"])
    _validate_source(event["source"])
    if schema_version == 2:
        _validate_attribution(event["attribution"])


def _validate_attribution(value: Any) -> None:
    expected = {
        "selector_mode",
        "candidate_count",
        "last_event_age_seconds",
        "confidence",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("evidence attribution fields do not match contract")
    if value["selector_mode"] not in ATTRIBUTION_MODES:
        raise ValueError("evidence attribution.selector_mode is unsupported")
    candidate_count = value["candidate_count"]
    if (
        not isinstance(candidate_count, int)
        or isinstance(candidate_count, bool)
        or candidate_count < 1
    ):
        raise ValueError(
            "evidence attribution.candidate_count must be a positive integer"
        )
    age = value["last_event_age_seconds"]
    if age is not None and (
        not isinstance(age, int) or isinstance(age, bool) or age < 0
    ):
        raise ValueError(
            "evidence attribution.last_event_age_seconds must be non-negative or null"
        )
    if value["confidence"] not in ATTRIBUTION_CONFIDENCE:
        raise ValueError("evidence attribution.confidence is unsupported")


def _validate_observation(value: Any) -> None:
    expected = {
        "tool_name",
        "command_class",
        "status",
        "exit_code",
        "duration_ms",
        "input_sha256",
        "result_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("evidence observation fields do not match contract")
    for field in ("tool_name", "command_class", "status"):
        _optional_label(value[field], f"observation.{field}")
    exit_code = value["exit_code"]
    if exit_code is not None and not isinstance(exit_code, int):
        raise TypeError("evidence observation.exit_code must be an integer or null")
    duration = value["duration_ms"]
    if duration is not None and (not isinstance(duration, int) or duration < 0):
        raise ValueError(
            "evidence observation.duration_ms must be a non-negative integer or null"
        )
    _optional_sha256(value["input_sha256"], "observation.input_sha256")
    _optional_sha256(value["result_sha256"], "observation.result_sha256")


def _validate_state(value: Any) -> None:
    expected = {
        "cwd",
        "repository_available",
        "head_sha",
        "tree_sha",
        "dirty",
        "changed_path_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("evidence state fields do not match contract")
    cwd = value["cwd"]
    if cwd is not None:
        _required_string(cwd, "state.cwd", maximum=4096)
        if not Path(cwd).is_absolute():
            raise ValueError("evidence state.cwd must be absolute")
    if not isinstance(value["repository_available"], bool):
        raise TypeError("evidence state.repository_available must be boolean")
    for field in ("head_sha", "tree_sha"):
        selected = value[field]
        if selected is not None and (
            not isinstance(selected, str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", selected)
        ):
            raise ValueError(f"evidence state.{field} must be a Git object id or null")
    if value["dirty"] is not None and not isinstance(value["dirty"], bool):
        raise TypeError("evidence state.dirty must be boolean or null")
    paths = value["changed_path_sha256"]
    if not isinstance(paths, list) or paths != sorted(set(paths)):
        raise ValueError("evidence changed_path_sha256 must be a sorted unique list")
    for digest in paths:
        _optional_sha256(digest, "state.changed_path_sha256")


def _validate_source(value: Any) -> None:
    expected = {
        "path",
        "line",
        "record_sha256",
        "snapshot_bytes",
        "snapshot_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("evidence source fields do not match contract")
    path = value["path"]
    if path is not None:
        _required_string(path, "source.path", maximum=4096)
        if not Path(path).is_absolute():
            raise ValueError("evidence source.path must be absolute")
    line = value["line"]
    if line is not None and (not isinstance(line, int) or line < 1):
        raise ValueError("evidence source.line must be a positive integer or null")
    _optional_sha256(value["record_sha256"], "source.record_sha256")
    snapshot_bytes = value["snapshot_bytes"]
    if snapshot_bytes is not None and (
        not isinstance(snapshot_bytes, int) or snapshot_bytes < 0
    ):
        raise ValueError(
            "evidence source.snapshot_bytes must be a non-negative integer or null"
        )
    _optional_sha256(value["snapshot_sha256"], "source.snapshot_sha256")
    if (snapshot_bytes is None) != (value["snapshot_sha256"] is None):
        raise ValueError("evidence source snapshot boundary is incomplete")


def _empty_observation() -> dict[str, Any]:
    return {
        "tool_name": None,
        "command_class": None,
        "status": None,
        "exit_code": None,
        "duration_ms": None,
        "input_sha256": None,
        "result_sha256": None,
    }


def _empty_state(cwd: Path | None = None) -> dict[str, Any]:
    return {
        "cwd": str(cwd.resolve(strict=False)) if cwd is not None else None,
        "repository_available": False,
        "head_sha": None,
        "tree_sha": None,
        "dirty": None,
        "changed_path_sha256": [],
    }


def _attribution(
    selector_mode: str,
    *,
    candidate_count: int = 1,
    last_event_age_seconds: int | None = None,
    confidence: str = "exact",
) -> dict[str, Any]:
    return {
        "selector_mode": selector_mode,
        "candidate_count": candidate_count,
        "last_event_age_seconds": last_event_age_seconds,
        "confidence": confidence,
    }


def _event(
    *,
    provider: str,
    source_kind: str,
    session_id: str,
    sequence: int,
    observed_at: str | None,
    event_type: str,
    evidence_class: str,
    correlation_sha256: str | None,
    observation: Mapping[str, Any] | None,
    state: Mapping[str, Any] | None,
    source: Mapping[str, Any],
    attribution: Mapping[str, Any],
    schema_version: int = EVIDENCE_SCHEMA_VERSION,
) -> dict[str, Any]:
    if schema_version not in EVIDENCE_READER_VERSIONS:
        raise ValueError("unsupported evidence writer schema_version")
    body = {
        "schema_version": schema_version,
        "provider": provider,
        "source_kind": source_kind,
        "session_id": session_id,
        "sequence": sequence,
        "observed_at": observed_at,
        "event_type": event_type,
        "evidence_class": evidence_class,
        "verification_status": "observed",
        "correlation_sha256": correlation_sha256,
        "observation": dict(observation or _empty_observation()),
        "state": dict(state or _empty_state()),
        "source": dict(source),
    }
    if schema_version == 2:
        body["attribution"] = dict(attribution)
    identity = {
        "schema_version": body["schema_version"],
        "provider": body["provider"],
        "source_kind": body["source_kind"],
        "session_id": body["session_id"],
        "sequence": body["sequence"],
        "event_type": body["event_type"],
        "correlation_sha256": body["correlation_sha256"],
        "source": {
            "path": body["source"]["path"],
            "line": body["source"]["line"],
            "record_sha256": body["source"]["record_sha256"],
        },
    }
    if schema_version == 2:
        identity["attribution"] = body["attribution"]
    event_id = (
        f"evidence-{sha256(canonical_json(identity).encode('utf-8')).hexdigest()}"
    )
    stored = {"event_id": event_id, **body}
    validate_evidence_event(stored)
    return stored


def _safe_provider(provider: str) -> str:
    return _required_string(provider, "provider", maximum=128)


def evidence_path(
    state_home: Path,
    provider: str,
    session_id: str,
    *,
    schema_version: int = EVIDENCE_SCHEMA_VERSION,
) -> Path:
    if schema_version not in EVIDENCE_READER_VERSIONS:
        raise ValueError("unsupported evidence path schema_version")
    selected_provider = _safe_provider(provider)
    if not _SAFE_LABEL.fullmatch(selected_provider):
        raise ValueError("evidence provider is not a safe path label")
    selected_session = _required_string(session_id, "session_id")
    session_digest = sha256(selected_session.encode("utf-8")).hexdigest()
    root = Path(state_home) / "behavior" / "evidence"
    if schema_version == 2:
        root = root / "v2"
    return root / selected_provider / f"{session_digest}.jsonl"


def _logical_event_key(event: Mapping[str, Any]) -> tuple[Any, ...]:
    source = event["source"]
    return (
        event["provider"],
        event["session_id"],
        event["sequence"],
        event["event_type"],
        event["correlation_sha256"],
        source["path"],
        source["line"],
        source["record_sha256"],
    )


def _version_neutral_event(event: Mapping[str, Any]) -> dict[str, Any]:
    selected = dict(event)
    selected.pop("event_id", None)
    selected.pop("schema_version", None)
    selected.pop("attribution", None)
    return selected


def append_evidence_events(
    state_home: Path, events: Iterable[Mapping[str, Any]]
) -> tuple[Path | None, tuple[dict[str, Any], ...]]:
    stored_events = tuple(dict(event) for event in events)
    if not stored_events:
        return None, ()
    for event in stored_events:
        validate_evidence_event(event)
    identities = {(event["provider"], event["session_id"]) for event in stored_events}
    if len(identities) != 1:
        raise ValueError("one evidence append must target one provider session")
    provider, session_id = next(iter(identities))
    schema_versions = {event["schema_version"] for event in stored_events}
    if len(schema_versions) != 1:
        raise ValueError("one evidence append must contain one schema version")
    schema_version = next(iter(schema_versions))
    path = evidence_path(
        state_home,
        provider,
        session_id,
        schema_version=schema_version,
    )
    existing_events = _read_evidence_records(state_home, provider, session_id)
    existing = {event["event_id"] for event in existing_events}
    pending = tuple(
        event
        for event in stored_events
        if event["event_id"] not in existing
    )
    if not pending:
        return path, ()
    ensure_state_home(state_home)
    ensure_private_directory(path.parent)
    encoded = b"".join(
        (canonical_json(event) + "\n").encode("utf-8") for event in pending
    )
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        written = 0
        while written < len(encoded):
            count = os.write(descriptor, encoded[written:])
            if count == 0:
                raise OSError("evidence append made no progress")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path, pending


def _read_evidence_records(
    state_home: Path, provider: str, session_id: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for schema_version in sorted(EVIDENCE_READER_VERSIONS):
        path = evidence_path(
            state_home,
            provider,
            session_id,
            schema_version=schema_version,
        )
        if not path.is_file():
            continue
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
                if event["schema_version"] != schema_version:
                    raise ValueError(
                        f"evidence version/route mismatch at {path}:{line_number}"
                    )
                if event["provider"] != provider or event["session_id"] != session_id:
                    raise ValueError(
                        f"evidence identity mismatch at {path}:{line_number}"
                    )
                events.append(event)
    cwd_keys = {
        os.path.normcase(
            os.path.realpath(str(Path(event["state"]["cwd"])))
        )
        for event in events
        if event["state"]["cwd"] is not None
    }
    if len(cwd_keys) > 1:
        raise ValueError("evidence session spans multiple working directories")
    return sorted(
        events,
        key=lambda event: (
            event["observed_at"] or "",
            event["sequence"],
            event["event_id"],
        ),
    )


def read_evidence(
    state_home: Path, provider: str, session_id: str
) -> list[dict[str, Any]]:
    """Read logical evidence, preferring v2 for a cross-version replay."""

    selected: dict[tuple[Any, ...], dict[str, Any]] = {}
    for event in _read_evidence_records(state_home, provider, session_id):
        key = _logical_event_key(event)
        current = selected.get(key)
        if current is not None and _version_neutral_event(
            current
        ) != _version_neutral_event(event):
            raise ValueError("cross-version evidence projections disagree")
        if current is None or event["schema_version"] > current["schema_version"]:
            selected[key] = event
    return sorted(
        selected.values(),
        key=lambda event: (
            event["observed_at"] or "",
            event["sequence"],
            event["event_id"],
        ),
    )


def read_evidence_version(
    state_home: Path,
    provider: str,
    session_id: str,
    *,
    schema_version: int,
) -> list[dict[str, Any]]:
    if schema_version not in EVIDENCE_READER_VERSIONS:
        raise ValueError("unsupported evidence reader schema_version")
    return [
        event
        for event in _read_evidence_records(state_home, provider, session_id)
        if event["schema_version"] == schema_version
    ]


def _codex_snapshot(path: Path) -> tuple[str, Path, int, str]:
    if path.stat().st_size > _MAX_TRACE_BYTES:
        raise ValueError(f"Codex trace exceeds {_MAX_TRACE_BYTES} bytes: {path}")
    digest = sha256()
    snapshot_bytes = 0
    session_id: str | None = None
    cwd: Path | None = None
    with path.open("rb") as stream:
        for raw in stream:
            snapshot_bytes += len(raw)
            digest.update(raw)
            if session_id is not None:
                continue
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            payload = record.get("payload") if isinstance(record, Mapping) else None
            if record.get("type") == "session_meta" and isinstance(payload, Mapping):
                selected = payload.get("id") or payload.get("session_id")
                selected_cwd = payload.get("cwd")
                if isinstance(selected, str) and selected:
                    session_id = selected
                if isinstance(selected_cwd, str) and Path(selected_cwd).is_absolute():
                    cwd = Path(selected_cwd).resolve(strict=False)
    if session_id is None:
        raise ValueError(f"Codex trace has no session id: {path}")
    if cwd is None:
        raise ValueError(f"Codex trace has no absolute cwd: {path}")
    return session_id, cwd, snapshot_bytes, digest.hexdigest()


def _first_string(value: Mapping[str, Any], *fields: str) -> str | None:
    for field in fields:
        selected = value.get(field)
        if isinstance(selected, str) and selected:
            return selected
    return None


def _correlation(value: str | None) -> str | None:
    return sha256(value.encode("utf-8")).hexdigest() if value else None


def _command_class(tool_name: str | None, arguments: Any) -> str | None:
    if not tool_name or tool_name.lower() not in {"bash", "shell", "shell_command"}:
        return None
    value = arguments
    if isinstance(arguments, str):
        try:
            value = json.loads(arguments)
        except json.JSONDecodeError:
            value = {"command": arguments}
    command = value.get("command") if isinstance(value, Mapping) else None
    if not isinstance(command, str):
        return "shell"
    lowered = command.strip().lower()
    if re.search(
        r"(?:^|\s)(?:pytest|npm\s+test|cargo\s+test|go\s+test)(?:\s|$)", lowered
    ):
        return "test"
    if re.search(r"(?:^|\s)git\s+(?:status|diff|log|show|rev-parse)(?:\s|$)", lowered):
        return "git-read"
    if re.search(
        r"(?:^|\s)git\s+(?:add|commit|push|merge|rebase|reset)(?:\s|$)", lowered
    ):
        return "git-write"
    if re.search(r"(?:^|\s)(?:curl|wget|invoke-webrequest)(?:\s|$)", lowered):
        return "network"
    return "shell"


def _codex_event(
    record: Mapping[str, Any],
    raw: bytes,
    *,
    path: Path,
    session_id: str,
    cwd: Path,
    line_number: int,
    snapshot_bytes: int,
    snapshot_sha256: str,
    attribution: Mapping[str, Any],
    schema_version: int,
) -> dict[str, Any] | None:
    record_type = record.get("type")
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    payload_type = payload.get("type")
    event_type: str | None = None
    evidence_class: str | None = None
    observation = _empty_observation()
    correlation_sha256: str | None = None

    if record_type == "session_meta":
        event_type, evidence_class = "session.started", "state"
    elif record_type == "event_msg" and payload_type == "task_started":
        event_type, evidence_class = "task.started", "invocation"
    elif record_type == "event_msg" and payload_type == "task_complete":
        event_type, evidence_class = "task.completed", "result"
    elif record_type == "response_item" and payload_type in {
        "function_call",
        "custom_tool_call",
    }:
        event_type, evidence_class = "tool.requested", "invocation"
        tool_name = _first_string(payload, "name", "tool_name")
        arguments = payload.get("arguments") or payload.get("input")
        observation.update(
            {
                "tool_name": tool_name
                if tool_name and _SAFE_LABEL.fullmatch(tool_name)
                else None,
                "command_class": _command_class(tool_name, arguments),
                "status": "requested",
                "input_sha256": _digest(arguments),
            }
        )
        correlation_sha256 = _correlation(
            _first_string(payload, "call_id", "tool_call_id", "tool_use_id", "id")
        )
    elif record_type == "response_item" and payload_type in {
        "function_call_output",
        "custom_tool_call_output",
    }:
        output = payload.get("output") or payload.get("result")
        failed = isinstance(output, str) and bool(_FAILURE_OUTPUT.search(output))
        event_type, evidence_class = (
            ("tool.failed", "result") if failed else ("tool.completed", "result")
        )
        exit_match = _EXIT_CODE.search(output) if isinstance(output, str) else None
        observation.update(
            {
                "status": "failed" if failed else "completed",
                "exit_code": int(exit_match.group(1)) if exit_match else None,
                "result_sha256": _digest(output),
            }
        )
        correlation_sha256 = _correlation(
            _first_string(payload, "call_id", "tool_call_id", "tool_use_id", "id")
        )
    if event_type is None or evidence_class is None:
        return None
    timestamp = record.get("timestamp")
    observed_at = timestamp if isinstance(timestamp, str) else None
    source = {
        "path": str(path.resolve()),
        "line": line_number,
        "record_sha256": sha256_bytes(raw.rstrip(b"\r\n")),
        "snapshot_bytes": snapshot_bytes,
        "snapshot_sha256": snapshot_sha256,
    }
    state = _empty_state(cwd)
    return _event(
        provider="codex",
        source_kind="transcript",
        session_id=session_id,
        sequence=line_number,
        observed_at=observed_at,
        event_type=event_type,
        evidence_class=evidence_class,
        correlation_sha256=correlation_sha256,
        observation=observation,
        state=state,
        source=source,
        attribution=attribution,
        schema_version=schema_version,
    )


def ingest_codex_trace(
    state_home: Path,
    path: Path,
    *,
    attribution: Mapping[str, Any] | None = None,
    schema_version: int = EVIDENCE_SCHEMA_VERSION,
) -> tuple[Path | None, tuple[dict[str, Any], ...], dict[str, Any]]:
    if schema_version not in EVIDENCE_READER_VERSIONS:
        raise ValueError("unsupported evidence writer schema_version")
    selected = Path(path).expanduser().resolve()
    selected_attribution = dict(
        attribution or _attribution("exact_trace_path")
    )
    _validate_attribution(selected_attribution)
    session_id, cwd, snapshot_bytes, snapshot_sha256 = _codex_snapshot(selected)
    events: list[dict[str, Any]] = []
    consumed = 0
    with selected.open("rb") as stream:
        for line_number, raw in enumerate(stream, start=1):
            if consumed + len(raw) > snapshot_bytes:
                break
            consumed += len(raw)
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(record, Mapping):
                continue
            event = _codex_event(
                record,
                raw,
                path=selected,
                session_id=session_id,
                cwd=cwd,
                line_number=line_number,
                snapshot_bytes=snapshot_bytes,
                snapshot_sha256=snapshot_sha256,
                attribution=selected_attribution,
                schema_version=schema_version,
            )
            if event is not None:
                events.append(event)
    destination, appended = append_evidence_events(state_home, events)
    boundary = {
        "path": str(selected),
        "snapshot_bytes": snapshot_bytes,
        "snapshot_sha256": snapshot_sha256,
        "session_id": session_id,
        "cwd": str(cwd),
        "event_count": len(events),
        "appended_count": len(appended),
        "schema_version": schema_version,
    }
    return destination, tuple(events), boundary


def normalize_hook_event(
    provider: str,
    hook_event: str,
    payload: Mapping[str, Any],
    *,
    observed_at: str | None = None,
    schema_version: int = EVIDENCE_SCHEMA_VERSION,
) -> dict[str, Any]:
    selected_provider = _safe_provider(provider)
    selected_hook = _required_string(hook_event, "hook_event", maximum=128)
    session_id = _first_string(payload, "session_id", "sessionId", "thread_id")
    if session_id is None:
        raise ValueError("hook evidence requires session_id")
    event_type, evidence_class = _HOOK_EVENTS.get(
        selected_hook, ("provider.observed", "state")
    )
    tool_name = _first_string(payload, "tool_name", "toolName")
    tool_input = payload.get("tool_input", payload.get("toolInput"))
    tool_response = payload.get("tool_response", payload.get("toolResponse"))
    correlation_value = _first_string(
        payload, "tool_use_id", "toolUseId", "task_id", "agent_id"
    )
    observation = _empty_observation()
    status = {
        "tool.requested": "requested",
        "tool.completed": "completed",
        "tool.failed": "failed",
        "permission.requested": "requested",
        "permission.denied": "denied",
    }.get(event_type)
    duration = payload.get("duration_ms", payload.get("durationMs"))
    exit_code = payload.get("exit_code", payload.get("exitCode"))
    observation.update(
        {
            "tool_name": tool_name
            if tool_name and _SAFE_LABEL.fullmatch(tool_name)
            else None,
            "command_class": _command_class(tool_name, tool_input),
            "status": status,
            "exit_code": exit_code if isinstance(exit_code, int) else None,
            "duration_ms": duration
            if isinstance(duration, int)
            and not isinstance(duration, bool)
            and duration >= 0
            else None,
            "input_sha256": _digest(tool_input),
            "result_sha256": _digest(tool_response),
        }
    )
    timestamp = (
        observed_at
        or _first_string(payload, "timestamp", "occurred_at")
        or _timestamp()
    )
    sequence = payload.get("sequence", 0)
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        sequence = 0
    transcript = _first_string(payload, "transcript_path", "transcriptPath")
    transcript_path = (
        str(Path(transcript).expanduser().resolve(strict=False)) if transcript else None
    )
    record_sha256 = _digest(payload)
    source = {
        "path": transcript_path,
        "line": None,
        "record_sha256": record_sha256,
        "snapshot_bytes": None,
        "snapshot_sha256": None,
    }
    cwd_value = _first_string(payload, "cwd", "working_directory")
    cwd = Path(cwd_value) if cwd_value and Path(cwd_value).is_absolute() else None
    return _event(
        provider=selected_provider,
        source_kind="hook",
        session_id=session_id,
        sequence=sequence,
        observed_at=timestamp,
        event_type=event_type,
        evidence_class=evidence_class,
        correlation_sha256=_correlation(correlation_value),
        observation=observation,
        state=_empty_state(cwd),
        source=source,
        attribution=_attribution(
            "provider_hook", confidence="source_attested"
        ),
        schema_version=schema_version,
    )


def ingest_hook_event(
    state_home: Path,
    provider: str,
    hook_event: str,
    payload: Mapping[str, Any],
    *,
    schema_version: int = EVIDENCE_SCHEMA_VERSION,
) -> tuple[Path | None, dict[str, Any], bool]:
    event = normalize_hook_event(
        provider,
        hook_event,
        payload,
        schema_version=schema_version,
    )
    path, appended = append_evidence_events(state_home, [event])
    return path, event, bool(appended)


def _git(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def observe_repository_state(
    state_home: Path,
    *,
    provider: str,
    session_id: str,
    cwd: Path,
    sequence: int = 0,
    observed_at: str | None = None,
    schema_version: int = EVIDENCE_SCHEMA_VERSION,
) -> tuple[Path | None, dict[str, Any], bool]:
    selected_cwd = Path(cwd).expanduser().resolve(strict=False)
    state = _empty_state(selected_cwd)
    head = _git(["git", "rev-parse", "--verify", "HEAD"], selected_cwd)
    if head.returncode == 0:
        status = _git(["git", "status", "--porcelain=v1", "-z"], selected_cwd)
        tree = _git(["git", "rev-parse", "HEAD^{tree}"], selected_cwd)
        changed = [item for item in status.stdout.split("\x00") if item]
        state.update(
            {
                "repository_available": True,
                "head_sha": head.stdout.strip() or None,
                "tree_sha": tree.stdout.strip() if tree.returncode == 0 else None,
                "dirty": bool(changed) if status.returncode == 0 else None,
                "changed_path_sha256": sorted(
                    {sha256(item.encode("utf-8")).hexdigest() for item in changed}
                ),
            }
        )
    source_material = {
        "provider": provider,
        "session_id": session_id,
        "cwd": str(selected_cwd),
        "sequence": sequence,
        "state": state,
        "observed_at": observed_at or _timestamp(),
    }
    source = {
        "path": None,
        "line": None,
        "record_sha256": _digest(source_material),
        "snapshot_bytes": None,
        "snapshot_sha256": None,
    }
    event = _event(
        provider=provider,
        source_kind="state-observer",
        session_id=session_id,
        sequence=sequence,
        observed_at=source_material["observed_at"],
        event_type="state.observed",
        evidence_class="state",
        correlation_sha256=None,
        observation=_empty_observation(),
        state=state,
        source=source,
        attribution=_attribution("state_observer"),
        schema_version=schema_version,
    )
    path, appended = append_evidence_events(state_home, [event])
    return path, event, bool(appended)


def verify_evidence_source(event: Mapping[str, Any]) -> dict[str, Any]:
    validate_evidence_event(event)
    source = event["source"]
    path_value = source["path"]
    boundary = source["snapshot_bytes"]
    boundary_hash = source["snapshot_sha256"]
    if event["source_kind"] != "transcript" or path_value is None or boundary is None:
        return {
            "event_id": event["event_id"],
            "status": event["verification_status"],
            "reason_codes": ["source-is-not-replayable"],
        }
    path = Path(path_value)
    if not path.is_file():
        return {
            "event_id": event["event_id"],
            "status": "unverifiable",
            "reason_codes": ["source-missing"],
        }
    with path.open("rb") as stream:
        prefix = stream.read(boundary)
    if len(prefix) < boundary:
        return {
            "event_id": event["event_id"],
            "status": "stale",
            "reason_codes": ["source-truncated"],
        }
    if sha256(prefix).hexdigest() != boundary_hash:
        return {
            "event_id": event["event_id"],
            "status": "contradicted",
            "reason_codes": ["source-prefix-changed"],
        }
    line_number = source["line"]
    if line_number is not None:
        lines = prefix.splitlines()
        if line_number > len(lines):
            return {
                "event_id": event["event_id"],
                "status": "contradicted",
                "reason_codes": ["source-line-missing"],
            }
        if sha256_bytes(lines[line_number - 1]) != source["record_sha256"]:
            return {
                "event_id": event["event_id"],
                "status": "contradicted",
                "reason_codes": ["source-record-changed"],
            }
    return {
        "event_id": event["event_id"],
        "status": "verified",
        "reason_codes": ["source-prefix-and-record-match"],
    }


def reconcile_evidence(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    stored = [dict(event) for event in events]
    for event in stored:
        validate_evidence_event(event)
    event_counts = Counter(event["event_type"] for event in stored)
    requests = {
        event["correlation_sha256"]: event
        for event in stored
        if event["event_type"] == "tool.requested" and event["correlation_sha256"]
    }
    results = {
        event["correlation_sha256"]: event
        for event in stored
        if event["event_type"] in {"tool.completed", "tool.failed"}
        and event["correlation_sha256"]
    }
    unresolved = sorted(set(requests) - set(results))
    orphaned = sorted(set(results) - set(requests))
    repeated_failures: Counter[str] = Counter()
    for correlation, result in results.items():
        request = requests.get(correlation)
        if result["event_type"] != "tool.failed" or request is None:
            continue
        input_sha = request["observation"]["input_sha256"]
        if input_sha:
            repeated_failures[input_sha] += 1
    reason_codes: list[str] = []
    if unresolved:
        reason_codes.append("tool-request-without-result")
    if orphaned:
        reason_codes.append("tool-result-without-request")
    if any(count > 1 for count in repeated_failures.values()):
        reason_codes.append("repeated-identical-tool-failure")
    if event_counts["permission.denied"] > 1:
        reason_codes.append("repeated-permission-denial")
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "event_count": len(stored),
        "event_counts": dict(sorted(event_counts.items())),
        "paired_tool_calls": len(set(requests) & set(results)),
        "unresolved_tool_requests": len(unresolved),
        "orphaned_tool_results": len(orphaned),
        "state_observation_count": event_counts["state.observed"],
        "reason_codes": reason_codes,
    }

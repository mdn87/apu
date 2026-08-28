from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import subprocess
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from .locking import ProcessLock
from .models import canonical_json, sha256_bytes
from .state import ensure_private_directory, ensure_state_home

EVIDENCE_SCHEMA_VERSION = 1
EVIDENCE_STATUSES = frozenset(
    {"asserted", "observed", "verified", "stale", "contradicted", "unverifiable"}
)
EVIDENCE_CLASSES = frozenset({"invocation", "result", "state"})
SOURCE_KINDS = frozenset({"transcript", "hook", "state-observer"})
_MAX_TRACE_BYTES = 256 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_PROVIDER_COMPONENT = re.compile(r"^[a-z0-9._-]{1,128}$")
_WINDOWS_DEVICE_COMPONENT = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)",
    re.IGNORECASE,
)
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
    expected = {
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
    if not isinstance(event, Mapping) or set(event) != expected:
        missing = sorted(expected - set(event)) if isinstance(event, Mapping) else []
        extra = sorted(set(event) - expected) if isinstance(event, Mapping) else []
        raise ValueError(
            f"evidence fields do not match contract; missing={missing}, extra={extra}"
        )
    if event["schema_version"] != EVIDENCE_SCHEMA_VERSION:
        raise ValueError("unsupported evidence schema_version")
    _required_string(event["event_id"], "event_id")
    validate_provider_component(event["provider"])
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
) -> dict[str, Any]:
    body = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
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
    event_id = (
        f"evidence-{sha256(canonical_json(identity).encode('utf-8')).hexdigest()}"
    )
    stored = {"event_id": event_id, **body}
    validate_evidence_event(stored)
    return stored


def validate_provider_component(provider: Any) -> str:
    """Validate one opaque provider label on POSIX and Windows filesystems."""

    if (
        not isinstance(provider, str)
        or not _PROVIDER_COMPONENT.fullmatch(provider)
        or provider in {".", ".."}
        or provider.endswith((".", " "))
        or _WINDOWS_DEVICE_COMPONENT.match(provider)
    ):
        raise ValueError(
            "evidence provider must be one portable 1-128 character path component"
        )
    return provider


def _safe_provider(provider: str) -> str:
    return validate_provider_component(provider)


def _evidence_root(state_home: Path) -> Path:
    state_root = Path(state_home)
    behavior_root = state_root / "behavior"
    evidence_root = behavior_root / "evidence"
    for candidate, label in (
        (behavior_root, "behavior root"),
        (evidence_root, "evidence root"),
    ):
        _validate_evidence_directory(candidate, label)
    return evidence_root


def _evidence_provider_directory(state_home: Path, provider: str) -> Path:
    evidence_root = _evidence_root(state_home)
    provider_directory = evidence_root / provider
    _validate_evidence_directory(provider_directory, "evidence provider directory")
    return provider_directory


def _validate_evidence_directory(path: Path, label: str) -> None:
    """Validate one controlled path component without racing path resolution."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or _is_junction(path, metadata):
        raise ValueError(f"{label} cannot be a filesystem redirect")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a directory")


def _is_junction(path: Path, metadata: os.stat_result) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None:
        try:
            return bool(is_junction(path))
        except OSError:
            pass
    if os.name != "nt":
        return False
    mount_point_tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    return (
        mount_point_tag is not None
        and getattr(metadata, "st_reparse_tag", None) == mount_point_tag
    )


def _ensure_evidence_provider_directory(state_home: Path, provider: str) -> Path:
    evidence_root = _evidence_root(state_home)
    ensure_state_home(state_home)
    evidence_root = _evidence_root(state_home)
    ensure_private_directory(evidence_root)
    evidence_root = _evidence_root(state_home)
    provider_directory = _evidence_provider_directory(state_home, provider)
    ensure_private_directory(provider_directory)
    # Recheck after creation. A same-user symlink swap after this point remains
    # a documented local race; every ordinary and pre-existing redirect is
    # rejected before JSONL, lock, or SQLite sidecars are opened.
    return _evidence_provider_directory(state_home, provider)


def evidence_path(state_home: Path, provider: str, session_id: str) -> Path:
    selected_provider = _safe_provider(provider)
    provider_directory = _evidence_provider_directory(state_home, selected_provider)
    selected_session = _required_string(session_id, "session_id")
    session_digest = sha256(selected_session.encode("utf-8")).hexdigest()
    return provider_directory / f"{session_digest}.jsonl"


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
    path = evidence_path(state_home, provider, session_id)
    provider_directory = _ensure_evidence_provider_directory(state_home, provider)
    path = provider_directory / path.name
    lock_path = _evidence_lock_path(path)
    index_path = _evidence_index_path(path)
    _validate_evidence_artifact(path)
    _validate_evidence_artifact(lock_path)
    _validate_evidence_index_artifacts(index_path)
    with ProcessLock(lock_path, timeout=10.0):
        _validate_evidence_artifact(path)
        _validate_evidence_index_artifacts(index_path)
        with sqlite3.connect(index_path) as index:
            _prepare_evidence_index(index, path, provider, session_id)
            pending = _pending_events(index, stored_events)
            if not pending:
                return path, ()
            encoded = b"".join(
                (canonical_json(event) + "\n").encode("utf-8") for event in pending
            )
            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("evidence artifact must be a regular file")
                if os.name == "posix":
                    os.fchmod(descriptor, 0o600)
                written = 0
                while written < len(encoded):
                    count = os.write(descriptor, encoded[written:])
                    if count == 0:
                        raise OSError("evidence append made no progress")
                    written += count
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            index.executemany(
                "INSERT INTO event_ids(event_id) VALUES (?)",
                ((event["event_id"],) for event in pending),
            )
            _record_index_boundary(index, path)
            index.commit()
            _restrict_private_file(index_path)
            return path, pending


def _evidence_lock_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


def _evidence_index_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.index.sqlite3")


def _validate_evidence_artifact(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError(f"evidence artifact must be a regular file: {path}")
    return True


def _validate_evidence_index_artifacts(path: Path) -> None:
    for candidate in (
        path,
        Path(f"{path}-journal"),
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    ):
        _validate_evidence_artifact(candidate)


def _restrict_private_file(path: Path) -> None:
    if os.name == "posix" and _validate_evidence_artifact(path):
        path.chmod(0o600)


def _file_boundary(path: Path) -> tuple[int, int]:
    if not _validate_evidence_artifact(path):
        return 0, 0
    metadata = path.stat()
    return metadata.st_size, metadata.st_mtime_ns


def _prepare_evidence_index(
    index: sqlite3.Connection,
    path: Path,
    provider: str,
    session_id: str,
) -> None:
    index.execute("CREATE TABLE IF NOT EXISTS event_ids (event_id TEXT PRIMARY KEY)")
    index.execute(
        "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    stored = dict(index.execute("SELECT key, value FROM metadata"))
    size, modified = _file_boundary(path)
    if stored.get("size") == str(size) and stored.get("mtime_ns") == str(modified):
        return
    events = _read_evidence_unlocked(path, provider, session_id)
    index.execute("DELETE FROM event_ids")
    index.executemany(
        "INSERT OR IGNORE INTO event_ids(event_id) VALUES (?)",
        ((event["event_id"],) for event in events),
    )
    _record_index_boundary(index, path)
    index.commit()


def _record_index_boundary(index: sqlite3.Connection, path: Path) -> None:
    size, modified = _file_boundary(path)
    index.executemany(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        (("size", str(size)), ("mtime_ns", str(modified))),
    )


def _pending_events(
    index: sqlite3.Connection, events: Iterable[dict[str, Any]]
) -> tuple[dict[str, Any], ...]:
    unique: dict[str, dict[str, Any]] = {}
    for event in events:
        unique.setdefault(event["event_id"], event)
    identifiers = tuple(unique)
    existing: set[str] = set()
    for offset in range(0, len(identifiers), 500):
        chunk = identifiers[offset : offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        existing.update(
            row[0]
            for row in index.execute(
                f"SELECT event_id FROM event_ids WHERE event_id IN ({placeholders})",
                chunk,
            )
        )
    return tuple(
        event for identifier, event in unique.items() if identifier not in existing
    )


def read_evidence(
    state_home: Path, provider: str, session_id: str
) -> list[dict[str, Any]]:
    path = evidence_path(state_home, provider, session_id)
    if not _validate_evidence_artifact(path):
        return []
    lock_path = _evidence_lock_path(path)
    _validate_evidence_artifact(lock_path)
    with ProcessLock(lock_path, timeout=10.0):
        _validate_evidence_artifact(path)
        return _read_evidence_unlocked(path, provider, session_id)


def _read_evidence_unlocked(
    path: Path, provider: str, session_id: str
) -> list[dict[str, Any]]:
    if not _validate_evidence_artifact(path):
        return []
    events: list[dict[str, Any]] = []
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("evidence artifact must be a regular file")
    with os.fdopen(descriptor, encoding="utf-8") as stream:
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
            if event["provider"] != provider or event["session_id"] != session_id:
                raise ValueError(f"evidence identity mismatch at {path}:{line_number}")
            events.append(event)
    return events


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
    )


def ingest_codex_trace(
    state_home: Path, path: Path
) -> tuple[Path | None, tuple[dict[str, Any], ...], dict[str, Any]]:
    selected = Path(path).expanduser().resolve()
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
    }
    return destination, tuple(events), boundary


def normalize_hook_event(
    provider: str,
    hook_event: str,
    payload: Mapping[str, Any],
    *,
    observed_at: str | None = None,
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
    )


def ingest_hook_event(
    state_home: Path,
    provider: str,
    hook_event: str,
    payload: Mapping[str, Any],
) -> tuple[Path | None, dict[str, Any], bool]:
    event = normalize_hook_event(provider, hook_event, payload)
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

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import canonical_json
from .state import ensure_state_home, validate_installation_id


REQUIRED_DAYS = 30
REQUIRED_MATERIAL_TASKS = 10
_SOURCES = frozenset({"user", "trace", "imported"})
_VALIDATION_RESULTS = frozenset({"passed", "failed", "partial", "unknown"})
_DEFECT_SEVERITIES = frozenset({"none", "ordinary", "serious"})


def append_outcome(state_home: Path, record: Mapping[str, Any]) -> Path:
    """Validate and append one canonical JSONL outcome record."""

    stored = dict(record)
    _validate_outcome(stored)
    installation_id = stored["installation_id"]
    path = _outcome_path(state_home, installation_id)
    ensure_state_home(state_home)
    encoded = (canonical_json(stored) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        written = 0
        while written < len(encoded):
            count = os.write(descriptor, encoded[written:])
            if count == 0:
                raise OSError("outcome append made no progress")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def read_outcomes(
    state_home: Path,
    installation_id: str,
) -> list[dict[str, Any]]:
    """Read one installation's outcomes without creating state."""

    path = _outcome_path(state_home, installation_id)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"invalid outcome at {path}:{line_number}: {error}"
                    ) from error
                _validate_outcome(record)
                if record["installation_id"] != installation_id:
                    raise ValueError(
                        f"outcome at {path}:{line_number} belongs to "
                        f"{record['installation_id']}"
                    )
                records.append(record)
    except OSError as error:
        raise ValueError(f"cannot read outcomes at {path}: {error}") from error
    return records


def summarize_outcomes(
    records: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    monitoring_started_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Summarize progress toward both monitoring completion thresholds."""

    stored_records = [dict(record) for record in records]
    for record in stored_records:
        _validate_outcome(record)

    current_time = datetime.now(timezone.utc) if now is None else _aware(now, "now")
    if monitoring_started_at is None:
        start = min(
            (_parse_timestamp(record["recorded_at"]) for record in stored_records),
            default=None,
        )
    elif isinstance(monitoring_started_at, datetime):
        start = _aware(monitoring_started_at, "monitoring_started_at")
    else:
        start = _parse_timestamp(monitoring_started_at)

    elapsed_days = (
        0
        if start is None
        else max(0, int((current_time - start).total_seconds() // 86_400))
    )
    material_task_count = sum(
        1 for record in stored_records if record["material"] is True
    )
    days_complete = elapsed_days >= REQUIRED_DAYS
    tasks_complete = material_task_count >= REQUIRED_MATERIAL_TASKS
    return {
        "record_count": len(stored_records),
        "material_task_count": material_task_count,
        "elapsed_days": elapsed_days,
        "required_days": REQUIRED_DAYS,
        "required_material_tasks": REQUIRED_MATERIAL_TASKS,
        "days_complete": days_complete,
        "tasks_complete": tasks_complete,
        "complete": days_complete and tasks_complete,
    }


def _outcome_path(state_home: Path, installation_id: str) -> Path:
    installation_id = validate_installation_id(installation_id)
    return Path(state_home) / "outcomes" / f"{installation_id}.jsonl"


def _validate_outcome(record: Any) -> None:
    if not isinstance(record, dict):
        raise ValueError("outcome must be a JSON object")
    if record.get("schema_version") != 1:
        raise ValueError("unsupported outcome schema_version")
    validate_installation_id(record.get("installation_id"))
    _parse_timestamp(record.get("recorded_at"))

    if not isinstance(record.get("task_id"), str) or not record["task_id"]:
        raise ValueError("outcome task_id is required")
    if not isinstance(record.get("material"), bool):
        raise ValueError("outcome material must be a boolean")
    if record.get("source") not in _SOURCES:
        raise ValueError("outcome source is unsupported")
    if record.get("validation") not in _VALIDATION_RESULTS:
        raise ValueError("outcome validation is unsupported")
    if not isinstance(record.get("rework"), bool):
        raise ValueError("outcome rework must be a boolean")

    _nullable_number(record, "elapsed_seconds")
    for field in ("agent_count", "review_count", "remediation_count"):
        _nullable_count(record, field)

    escaped_defect = record.get("escaped_defect")
    if not isinstance(escaped_defect, dict):
        raise ValueError("outcome escaped_defect must be an object")
    if not isinstance(escaped_defect.get("present"), bool):
        raise ValueError("outcome escaped_defect.present must be a boolean")
    if escaped_defect.get("severity") not in _DEFECT_SEVERITIES:
        raise ValueError("outcome escaped_defect.severity is unsupported")
    category = escaped_defect.get("category")
    if category is not None and not isinstance(category, str):
        raise ValueError("outcome escaped_defect.category must be a string or null")

    notes = record.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise ValueError("outcome notes must be a string or null")


def _nullable_number(record: Mapping[str, Any], field: str) -> None:
    value = record.get(field)
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
    ):
        raise ValueError(f"outcome {field} must be a non-negative number or null")


def _nullable_count(record: Mapping[str, Any], field: str) -> None:
    value = record.get(field)
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        raise ValueError(f"outcome {field} must be a non-negative integer or null")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("outcome recorded_at must be an RFC3339 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(
            "outcome recorded_at must be an RFC3339 timestamp"
        ) from error
    return _aware(parsed, "outcome recorded_at")


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(timezone.utc)

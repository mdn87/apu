from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .locking import ProcessLock
from .models import canonical_json, sha256_json
from .outcome_validation import validate_outcome_record
from .state import ensure_state_home, validate_installation_id

REQUIRED_DAYS = 30
REQUIRED_MATERIAL_TASKS = 10


def append_outcome(state_home: Path, record: Mapping[str, Any]) -> Path:
    """Validate and append one canonical JSONL outcome record."""

    stored = dict(record)
    validate_outcome_record(stored)
    installation_id = stored["installation_id"]
    root = ensure_state_home(Path(state_home))
    path = _outcome_path(root, installation_id)
    lock_key = sha256_json({"installation_id": installation_id})
    with ProcessLock(root / "locks" / f"outcomes-{lock_key}.lock"):
        encoded_record = canonical_json(stored)
        existing = {
            canonical_json(item) for item in read_outcomes(root, installation_id)
        }
        if encoded_record not in existing:
            _append_record(path, (encoded_record + "\n").encode("utf-8"))
        if stored["schema_version"] == 2:
            # The append is the source of truth. The content-addressed projection
            # is retried even when this exact outcome was committed previously.
            from .efficacy_policy import record_demotion_override

            record_demotion_override(root, stored)
    return path


def _append_record(path: Path, encoded: bytes) -> None:
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
                validate_outcome_record(record)
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
        validate_outcome_record(record)

    current_time = datetime.now(UTC) if now is None else _aware(now, "now")
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


def validate_outcome(record: Mapping[str, Any]) -> None:
    """Validate an outcome against its declared schema version."""

    validate_outcome_record(dict(record))


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("outcome recorded_at must be an RFC3339 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("outcome recorded_at must be an RFC3339 timestamp") from error
    return _aware(parsed, "outcome recorded_at")


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)

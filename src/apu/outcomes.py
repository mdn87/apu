from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import canonical_json
from .state import ensure_state_home, validate_installation_id

REQUIRED_DAYS = 30
REQUIRED_MATERIAL_TASKS = 10
_SOURCES = frozenset({"user", "trace", "imported"})
_VALIDATION_RESULTS = frozenset({"passed", "failed", "partial", "unknown"})
_DEFECT_SEVERITIES = frozenset({"none", "ordinary", "serious"})
_ACTIVATION_SOURCE_KINDS = frozenset(
    {"deterministic-marker", "category-fixture", "user-attestation"}
)
_FIXTURE_PHASES = frozenset({"apply", "window-close", "rerun"})
_FIXTURE_RESULTS = frozenset({"passed", "failed"})
_BASE_FIELDS = frozenset(
    {
        "schema_version",
        "installation_id",
        "recorded_at",
        "task_id",
        "material",
        "source",
        "elapsed_seconds",
        "agent_count",
        "review_count",
        "remediation_count",
        "validation",
        "rework",
        "escaped_defect",
        "notes",
    }
)
_V2_FIELDS = _BASE_FIELDS | frozenset(
    {
        "campaign_id",
        "campaign_provenance",
        "categories_installed",
        "categories_activated",
        "baseline_version",
        "model_generation",
        "fixture_results",
        "monitoring_window",
    }
)


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
    if stored["schema_version"] == 2:
        # A demotion is APU-private fail-safe state, not a profile mutation.
        # The trigger leaf is content-addressed, so retrying this call is safe.
        from .efficacy_policy import record_demotion_override

        record_demotion_override(state_home, stored)
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

    _validate_outcome(dict(record))


def _validate_outcome(record: Any) -> None:
    if not isinstance(record, dict):
        raise TypeError("outcome must be a JSON object")
    schema_version = record.get("schema_version")
    if schema_version not in {1, 2}:
        raise ValueError("unsupported outcome schema_version")
    validate_installation_id(record.get("installation_id"))
    _parse_timestamp(record.get("recorded_at"))

    if not isinstance(record.get("task_id"), str) or not record["task_id"]:
        raise ValueError("outcome task_id is required")
    if not isinstance(record.get("material"), bool):
        raise TypeError("outcome material must be a boolean")
    if record.get("source") not in _SOURCES:
        raise ValueError("outcome source is unsupported")
    if record.get("validation") not in _VALIDATION_RESULTS:
        raise ValueError("outcome validation is unsupported")
    if not isinstance(record.get("rework"), bool):
        raise TypeError("outcome rework must be a boolean")

    _nullable_number(record, "elapsed_seconds")
    for field in ("agent_count", "review_count", "remediation_count"):
        _nullable_count(record, field)

    escaped_defect = record.get("escaped_defect")
    if not isinstance(escaped_defect, dict):
        raise TypeError("outcome escaped_defect must be an object")
    if not isinstance(escaped_defect.get("present"), bool):
        raise TypeError("outcome escaped_defect.present must be a boolean")
    if escaped_defect.get("severity") not in _DEFECT_SEVERITIES:
        raise ValueError("outcome escaped_defect.severity is unsupported")
    category = escaped_defect.get("category")
    if category is not None and not isinstance(category, str):
        raise ValueError("outcome escaped_defect.category must be a string or null")

    notes = record.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise ValueError("outcome notes must be a string or null")

    if schema_version == 2:
        _validate_outcome_v2(record)


def _validate_outcome_v2(record: dict[str, Any]) -> None:
    if set(record) != _V2_FIELDS:
        missing = sorted(_V2_FIELDS - set(record))
        extra = sorted(set(record) - _V2_FIELDS)
        raise ValueError(
            f"outcome v2 fields do not match contract; missing={missing}, extra={extra}"
        )

    validate_installation_id(record["campaign_id"])
    _nonempty_string(record, "baseline_version")
    _nonempty_string(record, "model_generation")

    campaign_provenance = record["campaign_provenance"]
    _exact_fields(
        campaign_provenance,
        {"source", "manifest_sha256"},
        "outcome campaign_provenance",
    )
    if campaign_provenance["source"] != "campaign-manifest":
        raise ValueError("outcome campaign_provenance.source must be campaign-manifest")
    _sha256(campaign_provenance["manifest_sha256"], "campaign manifest")

    categories_installed = record["categories_installed"]
    _sorted_unique_strings(categories_installed, "categories_installed")
    installed = set(categories_installed)

    activations = record["categories_activated"]
    if not isinstance(activations, list):
        raise TypeError("outcome categories_activated must be a list")
    activation_order: list[tuple[str, str]] = []
    for activation in activations:
        _exact_fields(
            activation,
            {"category", "activation_source_id", "source_kind", "provenance"},
            "outcome activation",
        )
        category = _required_string(activation["category"], "activation category")
        source_id = _required_string(
            activation["activation_source_id"], "activation_source_id"
        )
        if category not in installed:
            raise ValueError(
                f"activated category {category!r} is not installed by the campaign"
            )
        source_kind = activation["source_kind"]
        if source_kind not in _ACTIVATION_SOURCE_KINDS:
            raise ValueError("outcome activation source_kind is unsupported")
        _validate_activation_provenance(source_kind, activation["provenance"])
        activation_order.append((category, source_id))
    if activation_order != sorted(set(activation_order)):
        raise ValueError(
            "outcome categories_activated must be sorted and unique by "
            "(category, activation_source_id)"
        )

    fixture_results = record["fixture_results"]
    if not isinstance(fixture_results, list):
        raise TypeError("outcome fixture_results must be a list")
    fixture_order: list[tuple[str, str, str]] = []
    for fixture in fixture_results:
        _exact_fields(
            fixture,
            {
                "fixture_id",
                "run_id",
                "category",
                "phase",
                "result",
                "artifact_sha256",
                "recorded_at",
            },
            "outcome fixture result",
        )
        fixture_id = _required_string(fixture["fixture_id"], "fixture_id")
        run_id = _required_string(fixture["run_id"], "fixture run_id")
        category = _required_string(fixture["category"], "fixture category")
        if category not in installed:
            raise ValueError(
                f"fixture category {category!r} is not installed by the campaign"
            )
        if fixture["phase"] not in _FIXTURE_PHASES:
            raise ValueError("outcome fixture phase is unsupported")
        if fixture["result"] not in _FIXTURE_RESULTS:
            raise ValueError("outcome fixture result is unsupported")
        _sha256(fixture["artifact_sha256"], "fixture artifact")
        _parse_timestamp(fixture["recorded_at"])
        fixture_order.append((category, fixture_id, run_id))
    if fixture_order != sorted(set(fixture_order)):
        raise ValueError(
            "outcome fixture_results must be sorted and unique by "
            "(category, fixture_id, run_id)"
        )

    monitoring_window = record["monitoring_window"]
    _exact_fields(
        monitoring_window,
        {"window_id", "status", "opened_at", "closed_at"},
        "outcome monitoring_window",
    )
    _required_string(monitoring_window["window_id"], "monitoring window_id")
    status = monitoring_window["status"]
    if status not in {"open", "closed"}:
        raise ValueError("outcome monitoring_window.status is unsupported")
    opened_at = _parse_timestamp(monitoring_window["opened_at"])
    closed_value = monitoring_window["closed_at"]
    if status == "open":
        if closed_value is not None:
            raise ValueError("open monitoring window must not have closed_at")
    else:
        closed_at = _parse_timestamp(closed_value)
        if closed_at < opened_at:
            raise ValueError("monitoring window closed_at precedes opened_at")

    escaped_defect = record["escaped_defect"]
    _exact_fields(
        escaped_defect,
        {"present", "severity", "category"},
        "outcome escaped_defect",
    )
    if escaped_defect["present"]:
        category = escaped_defect["category"]
        if (
            escaped_defect["severity"] == "none"
            or not isinstance(category, str)
            or not category
        ):
            raise ValueError("present escaped defect needs a severity and category")
        if category not in installed:
            raise ValueError("escaped defect category is not installed by the campaign")
    elif escaped_defect["severity"] != "none" or escaped_defect["category"] is not None:
        raise ValueError(
            "absent escaped defect must use severity none and category null"
        )


def _validate_activation_provenance(source_kind: str, provenance: Any) -> None:
    if source_kind == "deterministic-marker":
        _exact_fields(
            provenance,
            {"marker_id", "artifact_sha256", "recorded_at"},
            "deterministic activation provenance",
        )
        _required_string(provenance["marker_id"], "activation marker_id")
        _sha256(provenance["artifact_sha256"], "activation marker artifact")
        _parse_timestamp(provenance["recorded_at"])
        return
    if source_kind == "category-fixture":
        _exact_fields(
            provenance,
            {"fixture_id", "run_id", "phase", "result", "artifact_sha256"},
            "fixture activation provenance",
        )
        _required_string(provenance["fixture_id"], "activation fixture_id")
        _required_string(provenance["run_id"], "activation fixture run_id")
        if provenance["phase"] != "task":
            raise ValueError(
                "only a task-phase category fixture can prove activation; "
                "fixture reruns are validation-only"
            )
        if provenance["result"] != "passed":
            raise ValueError("activation fixture must have passed")
        _sha256(provenance["artifact_sha256"], "activation fixture artifact")
        return
    _exact_fields(
        provenance,
        {"attested_by", "attested_at", "statement_sha256"},
        "attestation activation provenance",
    )
    _required_string(provenance["attested_by"], "activation attested_by")
    _parse_timestamp(provenance["attested_at"])
    _sha256(provenance["statement_sha256"], "activation attestation")


def _exact_fields(value: Any, fields: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} must contain exactly {sorted(fields)}")


def _nonempty_string(record: Mapping[str, Any], field: str) -> None:
    _required_string(record.get(field), f"outcome {field}")


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _sorted_unique_strings(value: Any, field: str) -> None:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or value != sorted(set(value))
    ):
        raise ValueError(f"outcome {field} must be a sorted unique string list")


def _sha256(value: Any, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} sha256 must be 64 lowercase hexadecimal digits")


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
        raise ValueError("outcome recorded_at must be an RFC3339 timestamp") from error
    return _aware(parsed, "outcome recorded_at")


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)

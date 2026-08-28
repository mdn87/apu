from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import sha256_json
from .outcome_validation import validate_outcome_record
from .state import ensure_private_directory, write_json_atomic

PROMOTION_REQUIRED_ACTIVATIONS = 10
_PROFILE_POLICIES = frozenset({"auto", "work-order", "ignore"})
_HEX = frozenset("0123456789abcdef")


def evaluate_category_promotion(
    records: Iterable[Mapping[str, Any]],
    *,
    category: str,
    deterministic_remediation: bool,
    profile_sha256: str,
    baseline_version: str,
    model_generation: str,
    current_policy: str = "work-order",
    required_activations: int = PROMOTION_REQUIRED_ACTIVATIONS,
) -> dict[str, Any]:
    """Evaluate evidence and, when eligible, return a proposed profile edit.

    This function never writes the user-owned profile. Schema-v1 outcomes remain
    readable, but cannot supply M9 activation or campaign provenance.
    """

    _required_string(category, "category")
    _required_string(baseline_version, "baseline_version")
    _required_string(model_generation, "model_generation")
    _sha256(profile_sha256, "profile")
    if not isinstance(deterministic_remediation, bool):
        raise TypeError("deterministic_remediation must be a boolean")
    if current_policy not in _PROFILE_POLICIES:
        raise ValueError("current_policy is unsupported")
    if (
        isinstance(required_activations, bool)
        or not isinstance(required_activations, int)
        or required_activations < 1
    ):
        raise ValueError("required_activations must be a positive integer")

    relevant: list[dict[str, Any]] = []
    for supplied in records:
        record = dict(supplied)
        validate_outcome_record(record)
        if (
            record["schema_version"] == 2
            and category in record["categories_installed"]
            and record["baseline_version"] == baseline_version
            and record["model_generation"] == model_generation
        ):
            relevant.append(record)

    closed_windows = {
        record["monitoring_window"]["window_id"]
        for record in relevant
        if record["monitoring_window"]["status"] == "closed"
    }
    activation_keys: set[tuple[str, str, str, str]] = set()
    for record in relevant:
        if (
            record["material"] is not True
            or record["monitoring_window"]["window_id"] not in closed_windows
        ):
            continue
        for activation in record["categories_activated"]:
            if activation["category"] == category:
                activation_keys.add(
                    (
                        record["campaign_id"],
                        record["task_id"],
                        category,
                        activation["activation_source_id"],
                    )
                )

    implicating_defects = [
        {
            "campaign_id": record["campaign_id"],
            "task_id": record["task_id"],
            "recorded_at": record["recorded_at"],
            "severity": record["escaped_defect"]["severity"],
        }
        for record in relevant
        if record["escaped_defect"]["present"]
        and record["escaped_defect"]["category"] == category
    ]

    window_fixture_status: dict[str, dict[str, Any]] = {}
    for window_id in sorted(closed_windows):
        close_results = [
            fixture["result"]
            for record in relevant
            if record["monitoring_window"]["window_id"] == window_id
            for fixture in record["fixture_results"]
            if fixture["category"] == category and fixture["phase"] == "window-close"
        ]
        window_fixture_status[window_id] = {
            "result_count": len(close_results),
            "green": bool(close_results)
            and all(result == "passed" for result in close_results),
        }

    fixtures_green = bool(closed_windows) and all(
        status["green"] for status in window_fixture_status.values()
    )
    reasons: list[str] = []
    if not deterministic_remediation:
        reasons.append("remediation-not-deterministic")
    if len(activation_keys) < required_activations:
        reasons.append("insufficient-distinct-material-activations")
    if not closed_windows:
        reasons.append("no-closed-monitoring-window")
    if implicating_defects:
        reasons.append("implicating-escaped-defect")
    if not fixtures_green:
        reasons.append("window-close-fixtures-not-green")
    if current_policy != "work-order":
        reasons.append("policy-not-work-order")

    evidence = {
        "category": category,
        "baseline_version": baseline_version,
        "model_generation": model_generation,
        "distinct_material_activation_count": len(activation_keys),
        "required_activation_count": required_activations,
        "closed_window_ids": sorted(closed_windows),
        "window_close_fixtures": window_fixture_status,
        "implicating_defects": implicating_defects,
    }
    eligible = not reasons
    proposal = None
    if eligible:
        proposal_body = {
            "schema_version": 1,
            "artifact_type": "proposed-profile-edit",
            "profile_sha256": profile_sha256,
            "path": ["remediation_policy", category],
            "before": "work-order",
            "after": "auto",
            "requires_review": True,
            "evidence": evidence,
        }
        proposal = {
            **proposal_body,
            "proposal_id": sha256_json(proposal_body),
        }
    return {
        "category": category,
        "eligible": eligible,
        "reasons": reasons,
        "evidence": evidence,
        "proposal": proposal,
    }


def record_demotion_override(
    state_home: Path,
    outcome: Mapping[str, Any],
) -> Path | None:
    """Persist an idempotent fail-safe trigger for one implicating defect."""

    stored = dict(outcome)
    validate_outcome_record(stored)
    if stored["schema_version"] != 2:
        return None
    defect = stored["escaped_defect"]
    if not defect["present"]:
        return None

    category = defect["category"]
    outcome_sha256 = sha256_json(stored)
    trigger_body = {
        "schema_version": 1,
        "artifact_type": "demotion-trigger",
        "category": category,
        "campaign_id": stored["campaign_id"],
        "installation_id": stored["installation_id"],
        "task_id": stored["task_id"],
        "outcome_sha256": outcome_sha256,
        "outcome_recorded_at": stored["recorded_at"],
        "severity": defect["severity"],
        "effect": "suppress-auto",
    }
    trigger = {
        **trigger_body,
        "trigger_id": sha256_json(trigger_body),
        "recorded_at": _now(),
    }
    category_root = _category_root(state_home, category)
    path = category_root / "triggers" / f"{trigger['trigger_id']}.json"
    if path.exists():
        existing = _read_json(path, "demotion trigger")
        if existing != trigger:
            # recorded_at is intentionally excluded from the trigger identity.
            comparable = dict(existing)
            comparable["recorded_at"] = trigger["recorded_at"]
            if comparable != trigger:
                raise ValueError(f"conflicting demotion trigger at {path}")
        return path
    write_json_atomic(path, trigger)
    return path


def clear_demotion_override(
    state_home: Path,
    category: str,
    review: Mapping[str, Any],
) -> Path:
    """Record a reviewed clearance for every currently active trigger."""

    _required_string(category, "category")
    stored_review = dict(review)
    required = {"decision", "reviewed_by", "reviewed_at", "evidence_sha256"}
    if set(stored_review) != required:
        raise ValueError(f"override review must contain exactly {sorted(required)}")
    if stored_review["decision"] != "clear":
        raise ValueError("override review decision must be clear")
    _required_string(stored_review["reviewed_by"], "reviewed_by")
    _timestamp(stored_review["reviewed_at"], "reviewed_at")
    _sha256(stored_review["evidence_sha256"], "review evidence")

    current = _override_for_category(state_home, category)
    active_ids = sorted(trigger["trigger_id"] for trigger in current["active_triggers"])
    if not active_ids:
        raise ValueError(f"category {category!r} has no active demotion override")
    body = {
        "schema_version": 1,
        "artifact_type": "demotion-clearance",
        "category": category,
        "cleared_trigger_ids": active_ids,
        "review": stored_review,
    }
    clearance = {**body, "clearance_id": sha256_json(body)}
    path = (
        _category_root(state_home, category)
        / "clearances"
        / f"{clearance['clearance_id']}.json"
    )
    if path.exists():
        if _read_json(path, "demotion clearance") != clearance:
            raise ValueError(f"conflicting demotion clearance at {path}")
        return path
    write_json_atomic(path, clearance)
    return path


def load_demotion_overrides(state_home: Path) -> list[dict[str, Any]]:
    """Load private demotion state without creating directories."""

    root = Path(state_home) / "overrides" / "demotion"
    if not root.exists():
        return []
    results: list[dict[str, Any]] = []
    for category_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        metadata_path = category_dir / "category.json"
        metadata = _read_json(metadata_path, "demotion category")
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {"schema_version", "category"}
            or metadata["schema_version"] != 1
            or _category_key(metadata["category"]) != category_dir.name
        ):
            raise ValueError(f"invalid demotion category metadata at {metadata_path}")
        results.append(_override_for_category(state_home, metadata["category"]))
    return results


def demotion_status_overlay(
    profile_policy: Mapping[str, str],
    state_home: Path,
) -> dict[str, Any]:
    """Overlay private fail-safe state without modifying the supplied profile."""

    configured = dict(profile_policy)
    for category, policy in configured.items():
        _required_string(category, "profile policy category")
        if policy not in _PROFILE_POLICIES:
            raise ValueError(f"unsupported profile policy for {category!r}")
    overrides = load_demotion_overrides(state_home)
    effective = dict(configured)
    for override in overrides:
        if override["active"] and effective.get(override["category"]) == "auto":
            effective[override["category"]] = "work-order"
    return {
        "configured": configured,
        "effective": effective,
        "demotion_overrides": overrides,
    }


def _override_for_category(state_home: Path, category: str) -> dict[str, Any]:
    category_root = (
        Path(state_home) / "overrides" / "demotion" / _category_key(category)
    )
    triggers = _read_artifacts(category_root / "triggers", "demotion trigger")
    clearances = _read_artifacts(category_root / "clearances", "demotion clearance")
    cleared_ids = {
        trigger_id
        for clearance in clearances
        for trigger_id in clearance.get("cleared_trigger_ids", [])
    }
    active = [
        trigger for trigger in triggers if trigger.get("trigger_id") not in cleared_ids
    ]
    return {
        "category": category,
        "active": bool(active),
        "effect": "suppress-auto" if active else None,
        "active_triggers": active,
        "clearances": clearances,
    }


def _category_root(state_home: Path, category: str) -> Path:
    _required_string(category, "category")
    root = Path(state_home) / "overrides" / "demotion" / _category_key(category)
    ensure_private_directory(root / "triggers")
    ensure_private_directory(root / "clearances")
    metadata_path = root / "category.json"
    metadata = {"schema_version": 1, "category": category}
    if metadata_path.exists():
        if _read_json(metadata_path, "demotion category") != metadata:
            raise ValueError(f"conflicting demotion category at {metadata_path}")
    else:
        write_json_atomic(metadata_path, metadata)
    return root


def _category_key(category: str) -> str:
    return sha256_json({"category": category})


def _read_artifacts(directory: Path, label: str) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    return [_read_json(path, label) for path in sorted(directory.glob("*.json"))]


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {label} at {path}: {error}") from error
    if not isinstance(value, dict):
        raise TypeError(f"invalid {label} at {path}: expected an object")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _sha256(value: Any, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise ValueError(f"{label} sha256 must be 64 lowercase hexadecimal digits")


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be an RFC3339 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{label} must be an RFC3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")

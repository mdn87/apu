from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Mapping

from .state import (
    ensure_private_directory,
    ensure_state_home,
    validate_installation_id,
    write_json_atomic,
)


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_PROHIBITED_CONTENT_KEYS = frozenset(
    {
        "content",
        "original_content",
        "installed_content",
        "proposed_content",
        "source_content",
        "prompt",
        "prompts",
        "secret",
        "secrets",
        "trace_content",
    }
)


def receipt_path(state_home: Path, installation_id: str) -> Path:
    installation_id = validate_installation_id(installation_id)
    return Path(state_home) / "installations" / installation_id / "receipt.json"


def backup_dir(
    state_home: Path,
    installation_id: str,
    *,
    create: bool = False,
) -> Path:
    installation_id = validate_installation_id(installation_id)
    path = Path(state_home) / "installations" / installation_id / "backups"
    if create:
        ensure_state_home(state_home)
        ensure_private_directory(path)
    return path


def write_receipt(state_home: Path, receipt_dict: Mapping[str, Any]) -> Path:
    """Validate and atomically persist an installation receipt."""

    receipt = dict(receipt_dict)
    _validate_receipt(receipt)
    installation_id = receipt["installation_id"]
    _validate_backup_references(Path(state_home), installation_id, receipt)
    ensure_state_home(state_home)
    return write_json_atomic(receipt_path(state_home, installation_id), receipt)


def load_receipt(path: Path) -> dict[str, Any]:
    try:
        receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid receipt at {path}: {error}") from error
    _validate_receipt(receipt)
    return receipt


def validate_receipt_for_state(
    state_home: Path,
    path: Path,
    receipt: Mapping[str, Any],
) -> None:
    """Require a canonical receipt path and state-contained backup references."""

    root = Path(state_home).expanduser().resolve()
    supplied = Path(path).expanduser().resolve()
    expected = receipt_path(root, receipt["installation_id"]).resolve()
    if supplied != expected:
        raise ValueError(f"receipt is not at its canonical state path: {expected}")
    _validate_backup_references(root, receipt["installation_id"], receipt)


def _validate_receipt(receipt: Any) -> None:
    if not isinstance(receipt, dict):
        raise ValueError("receipt must be a JSON object")
    if receipt.get("schema_version") != 1:
        raise ValueError("unsupported receipt schema_version")
    validate_installation_id(receipt.get("installation_id"))

    operations = receipt.get("operations")
    if not isinstance(operations, list):
        raise ValueError("receipt operations must be a list")
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise ValueError(f"receipt operations[{index}] must be an object")
        if not isinstance(operation.get("id"), str) or not operation["id"]:
            raise ValueError(f"receipt operations[{index}].id is required")
        for name in ("original_sha256", "installed_sha256"):
            value = operation.get(name)
            if value is not None and (
                not isinstance(value, str) or _SHA256.fullmatch(value) is None
            ):
                raise ValueError(
                    f"receipt operations[{index}].{name} must be a SHA-256 or null"
                )
            if name not in operation:
                raise ValueError(f"receipt operations[{index}].{name} is required")
        if "backup_path" not in operation:
            raise ValueError(
                f"receipt operations[{index}].backup_path is required"
            )
        if operation["backup_path"] is not None and not isinstance(
            operation["backup_path"], str
        ):
            raise ValueError(
                f"receipt operations[{index}].backup_path must be a path or null"
            )

    rollback_status = receipt.get("rollback_status")
    if not isinstance(rollback_status, str) or not rollback_status:
        raise ValueError("receipt rollback_status is required")
    _reject_embedded_content(receipt)


def _reject_embedded_content(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in _PROHIBITED_CONTENT_KEYS or normalized.endswith(
                ("_content", "_prompt", "_secret")
            ):
                raise ValueError(
                    f"receipt must not embed source content or secrets ({key})"
                )
            _reject_embedded_content(item)
    elif isinstance(value, list):
        for item in value:
            _reject_embedded_content(item)


def _validate_backup_references(
    state_home: Path,
    installation_id: str,
    receipt: Mapping[str, Any],
) -> None:
    expected_root = backup_dir(state_home, installation_id).resolve()
    for index, operation in enumerate(receipt["operations"]):
        backup_path = operation["backup_path"]
        if backup_path is None:
            continue
        candidate = Path(backup_path)
        if not candidate.is_absolute():
            candidate = Path(state_home) / candidate
        if not candidate.resolve().is_relative_to(expected_root):
            raise ValueError(
                f"receipt operations[{index}].backup_path must be under "
                f"{expected_root}"
            )

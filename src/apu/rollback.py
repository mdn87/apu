from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
from typing import Any, Mapping

from .filesystem import hash_object, symlink_points_to
from .receipts import (
    load_receipt,
    validate_receipt_for_state,
    write_receipt,
)
from .state import load_registry, update_registry


class RollbackError(RuntimeError):
    """Raised when a receipt cannot be rolled back safely."""


def rollback_receipt(receipt_path: Path) -> dict[str, Any]:
    """Roll back unchanged installed objects described by *receipt_path*."""

    path = Path(receipt_path).expanduser().resolve()
    receipt = load_receipt(path)
    if len(path.parents) < 3:
        raise RollbackError("receipt path is outside an APU state directory")
    state_home = path.parents[2]
    try:
        validate_receipt_for_state(state_home, path, receipt)
        registry = load_registry(state_home)
        entry = registry["installations"].get(receipt["installation_id"])
        if not isinstance(entry, dict):
            raise ValueError("receipt installation is not registered")
        registered = Path(entry["receipt"])
        if not registered.is_absolute():
            registered = state_home / registered
        if registered.resolve() != path:
            raise ValueError("registry receipt does not match supplied receipt")
        _preflight_backups(receipt["operations"])
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise RollbackError(f"rollback preflight failed: {error}") from error
    operations = receipt["operations"]
    units = _rollback_units(operations)
    drifted: list[str] = []

    for unit in reversed(units):
        if not all(_can_restore(operation) for operation in unit):
            drifted.extend(operation["id"] for operation in unit)
            continue
        try:
            for operation in reversed(unit):
                _restore(operation)
        except OSError as error:
            raise RollbackError(f"rollback failed: {error}") from error

    status = "drifted" if drifted else "rolled_back"
    receipt["rollback_status"] = status
    if drifted:
        receipt["drifted_operation_ids"] = drifted
    else:
        receipt.pop("drifted_operation_ids", None)
    write_receipt(state_home, receipt)
    update_registry(
        state_home,
        receipt["installation_id"],
        {
            "status": status,
            "receipt": str(path.relative_to(state_home)),
            "rolled_back_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        },
    )
    return {"status": status, "drifted_operation_ids": drifted}


def _rollback_units(operations: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    units: list[list[dict[str, Any]]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    emitted: set[str] = set()
    for operation in operations:
        group_id = operation.get("atomic_group_id")
        if group_id is not None:
            grouped.setdefault(group_id, []).append(operation)
    for operation in operations:
        group_id = operation.get("atomic_group_id")
        if group_id is None:
            units.append([operation])
        elif group_id not in emitted:
            units.append(grouped[group_id])
            emitted.add(group_id)
    return units


def _can_restore(operation: Mapping[str, Any]) -> bool:
    target = Path(operation["target"])
    action = operation.get("action")
    if action == "symlink":
        expected = operation.get("created_symlink_target")
        return (
            isinstance(expected, str)
            and symlink_points_to(target, expected)
        )

    installed_hash = operation.get("installed_sha256")
    if installed_hash is None:
        return not os.path.lexists(target)
    if not os.path.lexists(target) or target.is_symlink():
        return False
    try:
        return _hash_object(target) == installed_hash
    except OSError:
        return False


def _restore(operation: Mapping[str, Any]) -> None:
    target = Path(operation["target"])
    if os.path.lexists(target):
        _remove_object(target)
    backup_value = operation.get("backup_path")
    if backup_value is not None:
        backup = Path(backup_value)
        if not os.path.lexists(backup):
            raise RollbackError(f"backup is missing for {operation['id']}")
        _copy_object(backup, target)
        original_mode = operation.get("original_mode")
        if original_mode is not None and os.name == "posix" and not target.is_symlink():
            target.chmod(original_mode)

    for value in reversed(operation.get("created_parent_directories", [])):
        try:
            Path(value).rmdir()
        except OSError:
            pass


def _hash_object(path: Path) -> str:
    return hash_object(path)


def _preflight_backups(operations: list[dict[str, Any]]) -> None:
    for operation in operations:
        value = operation.get("backup_path")
        if value is None:
            continue
        backup = Path(value)
        expected = operation.get("original_sha256")
        if expected is None:
            raise ValueError(f"backup has no original hash for {operation['id']}")
        if not os.path.lexists(backup) or backup.is_symlink():
            raise ValueError(f"backup is missing for {operation['id']}")
        if _hash_object(backup) != expected:
            raise ValueError(f"backup hash does not match for {operation['id']}")


def _copy_object(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        os.symlink(
            os.readlink(source),
            destination,
            target_is_directory=source.resolve().is_dir(),
        )
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination)


def _remove_object(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)

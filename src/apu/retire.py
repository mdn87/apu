"""Operator-initiated retirement of a stale installation registry entry.

Retirement stops validating an installation's targets without touching its
receipt, backup, or outcome records. It is an acknowledgment, not a rollback:
nothing on disk is restored or removed.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apu.filesystem import hash_object
from apu.receipts import load_receipt
from apu.state import load_registry, update_registry
from apu.validate import _registry_receipt_path, validate_receipt_path


class RetireError(ValueError):
    """Raised when a retirement is refused."""


def _normalized(target: str) -> str:
    return os.path.normcase(os.path.abspath(target))


def _covering_installations(
    state_home: Path,
    installation_id: str,
    registry: dict[str, Any],
    targets: dict[str, str],
) -> dict[str, set[str]]:
    covered: dict[str, set[str]] = {key: set() for key in targets}
    for other_id, other in registry["installations"].items():
        if other_id == installation_id or other.get("status") != "active":
            continue
        try:
            other_receipt = load_receipt(
                _registry_receipt_path(state_home, other_id, other)
            )
        except (OSError, TypeError, ValueError):
            continue
        for operation in other_receipt["operations"]:
            target = operation.get("target")
            if isinstance(target, str) and _normalized(target) in covered:
                covered[_normalized(target)].add(other_id)
    return covered


def _live_hash(target: str) -> str | None:
    try:
        if not os.path.lexists(target):
            return None
        return hash_object(Path(target))
    except OSError:
        return None


def retire_installation(
    receipt_path: Path,
    reason: str,
    *,
    force: bool = False,
    allow_uncovered: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mark one active installation retired, keeping all of its evidence."""

    reason = reason.strip()
    if not reason:
        raise RetireError("a non-empty reason is required to retire an installation")
    path = Path(receipt_path).expanduser().resolve()
    if len(path.parents) < 3:
        raise RetireError("receipt path is outside an APU state directory")
    state_home = path.parents[2]
    receipt = load_receipt(path)
    installation_id = receipt["installation_id"]

    registry = load_registry(state_home)
    entry = registry["installations"].get(installation_id)
    if not isinstance(entry, dict):
        raise RetireError("receipt installation is not registered")
    if _registry_receipt_path(state_home, installation_id, entry).resolve() != path:
        raise RetireError("registry receipt does not match supplied receipt")

    status = entry.get("status")
    if status == "retired":
        return {
            "status": "already_retired",
            "installation_id": installation_id,
            "retired_at": entry.get("retired_at"),
            "message": f"installation {installation_id} is already retired; nothing changed",
        }
    if status != "active":
        raise RetireError(
            f"only active installations can be retired (status: {status})"
        )
    if validate_receipt_path(path).status == "passed" and not force:
        raise RetireError(
            "installation currently validates, so retiring it would stop checking"
            " an intact file; pass --force to retire it anyway"
        )

    targets = {
        _normalized(operation["target"]): operation["target"]
        for operation in receipt["operations"]
        if isinstance(operation.get("target"), str)
    }
    covered = _covering_installations(state_home, installation_id, registry, targets)
    uncovered = sorted(targets[key] for key, ids in covered.items() if not ids)
    if uncovered and not allow_uncovered:
        raise RetireError(
            "no other active installation covers: "
            + ", ".join(uncovered)
            + "; pass --allow-uncovered to stop watching it"
        )

    moment = datetime.now(UTC) if now is None else now
    superseded_by = sorted({other for ids in covered.values() for other in ids})
    retired = dict(entry)
    retired.update(
        status="retired",
        retired_at=moment.isoformat().replace("+00:00", "Z"),
        retire_reason=reason,
        live_sha256_at_retirement={
            target: _live_hash(target) for target in targets.values()
        },
    )
    if superseded_by:
        retired["superseded_by"] = superseded_by
    update_registry(state_home, installation_id, retired)
    return {
        "status": "retired",
        "installation_id": installation_id,
        "retired_at": retired["retired_at"],
        "superseded_by": superseded_by,
    }

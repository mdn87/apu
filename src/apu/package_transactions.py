"""Journaled upgrades for trusted provider adapters with exact restore support.

The coordinator never installs packages itself. A capability is a precondition,
not a substitute for an audited provider implementation.
"""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

from apu.models import sha256_json
from apu.package_coordinates import PackageCoordinate
from apu.package_state import PackageLock
from apu.package_upgrade import (
    PackageStateIdentity,
    PackageUpgradeCapability,
    PackageUpgradeError,
    PackageUpgradeExecutor,
    PackageUpgradeProtocolContract,
    PackageUpgradeRequest,
    require_package_upgrade,
)
from apu.state import write_json_atomic

_TERMINAL = {"verified", "rolled-back"}
_MAX_JOURNALS = 10_000
_MAX_JOURNAL_BYTES = 1024 * 1024


class PackageUpgradeRecoveryRequired(PackageUpgradeError):
    """Provider state or a previous transaction requires explicit reconciliation."""


def _preflight(home, coordinate, request, capability, executor):
    require_package_upgrade(request, capability, state_home=home, executor=executor)
    if (
        coordinate.package_id != request.package_id
        or coordinate.provider != executor.provider
    ):
        raise ValueError("package upgrade coordinate does not match request")
    if capability.protocol != PackageUpgradeProtocolContract():
        raise ValueError("package upgrade requires the complete protocol contract")


def _directory(home: Path, request: PackageUpgradeRequest) -> Path:
    root = Path(home).expanduser().resolve()
    path = root / "transactions" / "package-upgrades" / sha256_json(request.package_id)
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if _linked(candidate):
            raise PackageUpgradeRecoveryRequired(
                "linked transaction directories are not supported"
            )
    return path


def _linked(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _read(path: Path) -> dict[str, Any]:
    try:
        if _linked(path) or path.stat().st_size > _MAX_JOURNAL_BYTES:
            raise ValueError("unsafe journal")
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            value["schema_version"] != 1
            or value["artifact_type"] != "package-upgrade-journal"
        ):
            raise ValueError("unknown journal")
        raw = value["request"]
        request = PackageUpgradeRequest(
            raw["package_id"],
            raw["operation_id"],
            raw["attempt"],
            PackageStateIdentity(**raw["pre_state"]),
            raw["target"]["version"],
            raw["target"]["tree_sha256"],
        )
        if raw != request.to_dict() or path.stem != request.transaction_id:
            raise ValueError("invalid journal request")
        if value["status"] not in PackageUpgradeProtocolContract().journal_states:
            raise ValueError("unknown journal status")
        return value
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PackageUpgradeRecoveryRequired(
            "package upgrade journal is invalid"
        ) from error


def _load(directory, request):
    found = None
    if not directory.exists():
        return None
    for index, path in enumerate(directory.iterdir()):
        if index >= _MAX_JOURNALS:
            raise PackageUpgradeRecoveryRequired(
                "package upgrade journal limit exceeded"
            )
        if path.suffix != ".json":
            continue
        journal = _read(path)
        if journal["request"]["package_id"] != request.package_id:
            raise PackageUpgradeRecoveryRequired("journal identifies another package")
        if path.stem == request.transaction_id:
            if journal["request"] != request.to_dict():
                raise ValueError(
                    "idempotency key already belongs to a different request"
                )
            found = journal
        elif journal["status"] not in _TERMINAL:
            raise PackageUpgradeRecoveryRequired(
                "another package upgrade requires recovery"
            )
    return found


def _target(request):
    return replace(
        request.pre_state,
        version=request.target_version,
        tree_sha256=request.target_tree_sha256,
    )


def _observe(executor, coordinate):
    try:
        state = executor.observe(coordinate)
        if (
            not isinstance(state, PackageStateIdentity)
            or state.package_id != coordinate.package_id
        ):
            raise ValueError("invalid observation")
        return state
    except Exception as error:
        # Never persist provider errors: they can contain output or credentials.
        raise PackageUpgradeRecoveryRequired(
            "authoritative package observation failed"
        ) from error


def _save(path, journal, status):
    journal["status"] = status
    write_json_atomic(path, journal)


def _finish(path, journal, status, state):
    receipt = {
        "schema_version": 1,
        "artifact_type": "package-upgrade-receipt",
        "transaction_id": journal["request"]["transaction_id"],
        "request_sha256": sha256_json(journal["request"]),
        "status": status,
        "observed_state": state.to_dict() if state is not None else None,
    }
    # One atomic write commits both terminal state and its deterministic receipt.
    journal["receipt"] = receipt
    _save(path, journal, status)
    return receipt


def _restore_verified(executor):
    verifier = getattr(executor, "verify_rollback", None)
    try:
        return verifier is None or verifier() is True
    except Exception:  # noqa: BLE001 - verification failure cannot imply success
        return False


def _rollback(path, journal, coordinate, request, executor):
    journal.pop("receipt", None)
    _save(path, journal, "rollback-requested")
    try:
        executor.rollback_exact(coordinate, request.pre_state)
    except Exception:  # noqa: BLE001 - provider boundary; verify even after errors
        journal["rollback_error"] = "provider-operation-raised"
    try:
        observed = _observe(executor, coordinate)
    except PackageUpgradeRecoveryRequired:
        observed = None
    verified = observed == request.pre_state and _restore_verified(executor)
    status = "rolled-back" if verified else "rollback-failed"
    return _finish(path, journal, status, observed)


def execute_package_upgrade(
    state_home: Path,
    coordinate: PackageCoordinate,
    request: PackageUpgradeRequest,
    capability: PackageUpgradeCapability,
    *,
    executor: PackageUpgradeExecutor,
) -> dict[str, Any]:
    """Execute once, verify, and restore the pre-state if the update fails.

    Replaying an interrupted call never repeats an update. Ambiguous state and
    failed rollback require an explicit recovery bound to a fresh observation.
    """
    _preflight(state_home, coordinate, request, capability, executor)
    directory = _directory(state_home, request)
    with PackageLock(state_home, request.package_id):
        journal = _load(directory, request)
        path = directory / f"{request.transaction_id}.json"
        observed = _observe(executor, coordinate)
        if journal is not None:
            status = journal["status"]
            if status in _TERMINAL:
                expected = (
                    _target(request) if status == "verified" else request.pre_state
                )
                if observed != expected or (
                    status == "rolled-back" and not _restore_verified(executor)
                ):
                    raise PackageUpgradeRecoveryRequired(
                        "package changed after the completed transaction"
                    )
                return _finish(path, journal, status, observed)
            if status == "rollback-failed":
                raise PackageUpgradeRecoveryRequired(
                    "failed rollback requires explicit recovery"
                )
            if observed == request.pre_state:
                if not _restore_verified(executor):
                    raise PackageUpgradeRecoveryRequired(
                        "package restore requires explicit recovery"
                    )
                return _finish(path, journal, "rolled-back", observed)
            if observed == _target(request) and status != "rollback-requested":
                return _finish(path, journal, "verified", observed)
            raise PackageUpgradeRecoveryRequired(
                "interrupted upgrade has an ambiguous provider state"
            )
        if observed != request.pre_state:
            raise PackageUpgradeRecoveryRequired(
                "package changed since the upgrade was prepared"
            )
        journal = {
            "schema_version": 1,
            "artifact_type": "package-upgrade-journal",
            "request": request.to_dict(),
        }
        _save(path, journal, "prepared")
        try:
            executor.update_exact(coordinate, request)
        except Exception:  # noqa: BLE001 - restore after any provider exception
            journal["update_error"] = "provider-operation-raised"
            return _rollback(path, journal, coordinate, request, executor)
        _save(path, journal, "provider-update-returned")
        try:
            observed = _observe(executor, coordinate)
        except PackageUpgradeRecoveryRequired:
            observed = None
        if observed == _target(request):
            return _finish(path, journal, "verified", observed)
        return _rollback(path, journal, coordinate, request, executor)


def recover_package_upgrade(
    state_home: Path,
    coordinate: PackageCoordinate,
    request: PackageUpgradeRequest,
    capability: PackageUpgradeCapability,
    *,
    executor: PackageUpgradeExecutor,
    expected_current: PackageStateIdentity,
) -> dict[str, Any]:
    """Explicitly retry restoration, only for the caller-observed state."""
    _preflight(state_home, coordinate, request, capability, executor)
    directory = _directory(state_home, request)
    with PackageLock(state_home, request.package_id):
        journal = _load(directory, request)
        if journal is None or journal["status"] in _TERMINAL:
            raise PackageUpgradeRecoveryRequired("no unfinished transaction to recover")
        observed = _observe(executor, coordinate)
        if observed != expected_current:
            raise PackageUpgradeRecoveryRequired(
                "package changed before explicit recovery"
            )
        path = directory / f"{request.transaction_id}.json"
        if observed == request.pre_state and _restore_verified(executor):
            return _finish(path, journal, "rolled-back", observed)
        return _rollback(path, journal, coordinate, request, executor)


def record_package_restoration(state_home, coordinate, request, *, executor):
    """Reconcile a manual snapshot restoration only after complete verification."""
    directory = _directory(state_home, request)
    with PackageLock(state_home, request.package_id):
        journal = _load(directory, request)
        if journal is None:
            return None
        observed = _observe(executor, coordinate)
        if observed != request.pre_state or executor.verify_rollback() is not True:
            raise PackageUpgradeRecoveryRequired(
                "captured package state has not been restored"
            )
        return _finish(
            directory / f"{request.transaction_id}.json",
            journal,
            "rolled-back",
            observed,
        )

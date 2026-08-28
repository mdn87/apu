from __future__ import annotations

import os
import platform
import secrets
import shutil
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from .filesystem import hash_object
from .models import Plan, PlanOperation, ValidationError, sha256_bytes
from .receipts import backup_dir, write_receipt
from .render import render_bytes
from .state import (
    ensure_private_directory,
    ensure_state_home,
    update_registry,
    validate_installation_id,
    write_json_atomic,
)


class ApplyError(RuntimeError):
    """Raised when an approved plan cannot be applied safely."""


@dataclass
class _PreparedOperation:
    operation: PlanOperation
    target: Path
    source: Path | None
    staged: Path | None
    backup: Path | None
    original_sha256: str | None
    original_mode: int | None
    installed_sha256: str | None
    symlink_target: str | None


def apply_plan(
    plan: Plan,
    *,
    state_home: Path,
    installation_id: str,
    confirmed: bool = False,
    campaign_id: str | None = None,
    snapshot_id: str | None = None,
) -> Path:
    """Apply the approved operations in *plan* as one recoverable transaction."""

    del confirmed  # Final confirmation is a CLI concern, never an approval bypass.
    try:
        plan.validate()
    except ValidationError as error:
        raise ApplyError(f"invalid plan: {error}") from error
    if plan.status != "approved":
        raise ApplyError("plan must be approved before apply")
    try:
        validate_installation_id(installation_id)
    except ValueError as error:
        raise ApplyError(str(error)) from error
    if (campaign_id is None) != (snapshot_id is None):
        raise ApplyError("campaign_id and snapshot_id must be supplied together")
    if campaign_id is not None:
        try:
            validate_installation_id(campaign_id)
            validate_installation_id(snapshot_id or "")
        except ValueError as error:
            raise ApplyError(str(error)) from error

    operations = plan.executable_operations()
    if not operations:
        raise ApplyError("approved plan has no approved operations")

    root = Path(state_home).expanduser().resolve()
    _ensure_unused_installation(root, installation_id)
    root = ensure_state_home(root)
    transaction_root = root / "transactions"
    ensure_private_directory(transaction_root)
    transaction = Path(
        tempfile.mkdtemp(prefix=f"{installation_id}.", dir=transaction_root)
    )
    if os.name == "posix":
        transaction.chmod(0o700)

    prepared: list[_PreparedOperation] = []
    applied: list[_PreparedOperation] = []
    created_directories: list[Path] = []
    installation_root = root / "installations" / installation_id
    try:
        prepared = _preflight_all(
            operations,
            transaction=transaction,
            state_home=root,
            installation_id=installation_id,
            protected_roots=plan.validation.get("protected_roots", ()),
        )
        for item in prepared:
            _ensure_parent(item.target.parent, created_directories)
            _revalidate_target(item)
            # Track a commit before entering it so an asynchronous interruption
            # after the filesystem mutation cannot strand an unrecorded change.
            applied.append(item)
            _commit(item)

        receipt = _build_receipt(
            plan,
            installation_id=installation_id,
            prepared=prepared,
            created_directories=created_directories,
            campaign_id=campaign_id,
            snapshot_id=snapshot_id,
        )
        receipt_file = write_receipt(root, receipt)
        update_registry(
            root,
            installation_id,
            {
                "status": "active",
                "receipt": str(receipt_file.relative_to(root)),
                "applied_at": receipt["created_at"],
                "monitoring_started_at": receipt["created_at"],
            },
        )
        return receipt_file
    except BaseException as error:
        try:
            _restore_applied(applied, created_directories)
        except BaseException as rollback_error:
            try:
                journal = _write_recovery_journal(
                    installation_root,
                    installation_id=installation_id,
                    applied=applied,
                    created_directories=created_directories,
                    error=error,
                    rollback_error=rollback_error,
                )
            except BaseException as journal_error:
                raise ApplyError(
                    "transaction rollback failed and its recovery journal "
                    f"could not be written; backups remain under "
                    f"{installation_root}: {rollback_error}; {journal_error}"
                ) from rollback_error
            raise ApplyError(
                "transaction rollback failed; durable recovery state was "
                f"written to {journal}: {rollback_error}"
            ) from error
        shutil.rmtree(installation_root, ignore_errors=True)
        if isinstance(error, ApplyError):
            raise
        if isinstance(error, (OSError, ValueError)):
            raise ApplyError(f"transaction failed: {error}") from error
        raise
    finally:
        shutil.rmtree(transaction, ignore_errors=True)


def _preflight_all(
    operations: Iterable[PlanOperation],
    *,
    transaction: Path,
    state_home: Path,
    installation_id: str,
    protected_roots: Iterable[str],
) -> list[_PreparedOperation]:
    prepared: list[_PreparedOperation] = []
    for index, operation in enumerate(operations):
        storage_name = _storage_name(index, operation.id)
        target = _absolute(operation.target, f"operation {operation.id} target")
        source = (
            _absolute(operation.source, f"operation {operation.id} source")
            if operation.source is not None
            else None
        )
        target_exists = _lexists(target)
        _validate_mutation_target(
            operation,
            target,
            target_exists=target_exists,
            state_home=state_home,
            protected_roots=protected_roots,
        )

        if operation.action in {"create", "symlink"} or (
            operation.action == "configure" and operation.precondition_sha256 is None
        ):
            if operation.precondition_sha256 is not None:
                raise ApplyError(
                    f"operation {operation.id} create precondition must be null"
                )
            if target_exists:
                raise ApplyError(
                    f"operation {operation.id} precondition requires a missing target"
                )
        else:
            if not target_exists:
                raise ApplyError(
                    f"operation {operation.id} precondition target is missing"
                )
            if target.is_symlink():
                raise ApplyError(
                    f"operation {operation.id} target is an unexpected symlink"
                )
            if operation.precondition_sha256 is None:
                raise ApplyError(
                    f"operation {operation.id} precondition hash is required"
                )
            actual = _hash_object(target)
            if actual != operation.precondition_sha256:
                raise ApplyError(
                    f"operation {operation.id} precondition hash does not match"
                )

        staged: Path | None = None
        installed_sha256: str | None = None
        symlink_target: str | None = None
        if operation.action in {"create", "merge", "configure"}:
            if source is None or not _lexists(source):
                raise ApplyError(f"operation {operation.id} source is missing")
            if source.is_symlink():
                raise ApplyError(
                    f"operation {operation.id} source is an unexpected symlink"
                )
            staged = transaction / "rendered" / storage_name
            if source.is_dir():
                if operation.strategy != "full_file":
                    raise ApplyError(
                        f"operation {operation.id} directory source requires "
                        "full_file strategy"
                    )
                _copy_object(source, staged)
            else:
                current = (
                    target.read_bytes() if target_exists and target.is_file() else None
                )
                try:
                    rendered = render_bytes(
                        action=operation.action,
                        strategy=operation.strategy,
                        source=source.read_bytes(),
                        current=current,
                        target=target,
                    )
                except ValueError as error:
                    raise ApplyError(
                        f"operation {operation.id} render failed: {error}"
                    ) from error
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(rendered)
            installed_sha256 = _hash_object(staged)
            if (
                operation.proposed_sha256 is not None
                and installed_sha256 != operation.proposed_sha256
            ):
                raise ApplyError(
                    f"operation {operation.id} proposed output hash does not match"
                )
        elif operation.action == "symlink":
            if source is None or not _lexists(source):
                raise ApplyError(f"operation {operation.id} symlink source is missing")
            source_hash = _hash_object(source)
            if (
                operation.proposed_sha256 is not None
                and source_hash != operation.proposed_sha256
            ):
                raise ApplyError(
                    f"operation {operation.id} symlink source hash does not match"
                )
            symlink_target = str(source)
            installed_sha256 = sha256_bytes(os.fsencode(symlink_target))
            capability_link = transaction / "capabilities" / storage_name
            capability_link.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.symlink(
                    symlink_target,
                    capability_link,
                    target_is_directory=source.is_dir(),
                )
            except OSError as error:
                raise ApplyError(
                    f"operation {operation.id} symlink is unsupported: {error}"
                ) from error
            capability_link.unlink()
        elif operation.action == "remove":
            if operation.proposed_sha256 is not None:
                raise ApplyError(
                    f"operation {operation.id} remove proposed hash must be null"
                )
        else:
            raise ApplyError(
                f"operation {operation.id} has unsupported action {operation.action}"
            )

        backup: Path | None = None
        original_sha256: str | None = None
        original_mode: int | None = None
        if target_exists:
            original_sha256 = _hash_object(target)
            original_mode = _mode(target)
            backup = backup_dir(state_home, installation_id, create=True) / storage_name
            _copy_object(target, backup)

        prepared.append(
            _PreparedOperation(
                operation=operation,
                target=target,
                source=source,
                staged=staged,
                backup=backup,
                original_sha256=original_sha256,
                original_mode=original_mode,
                installed_sha256=installed_sha256,
                symlink_target=symlink_target,
            )
        )

    _verify_atomic_groups(prepared)
    return prepared


def _verify_atomic_groups(prepared: list[_PreparedOperation]) -> None:
    groups: dict[str, list[_PreparedOperation]] = {}
    for item in prepared:
        group_id = item.operation.atomic_group_id
        if group_id is not None:
            groups.setdefault(group_id, []).append(item)
    for group_id, members in groups.items():
        remove = next(
            (item for item in members if item.operation.action == "remove"), None
        )
        create = next(
            (item for item in members if item.operation.action == "create"), None
        )
        expected = members[0].operation.group_content_sha256
        if (
            len(members) != 2
            or remove is None
            or create is None
            or remove.original_sha256 != expected
            or create.installed_sha256 != expected
        ):
            raise ApplyError(
                f"atomic relocation group {group_id} failed content preflight"
            )


def _revalidate_target(item: _PreparedOperation) -> None:
    """Reject target drift that occurred after the transaction preflight."""

    exists = _lexists(item.target)
    if item.original_sha256 is None:
        if exists:
            raise ApplyError(
                f"operation {item.operation.id} target changed after preflight"
            )
        return
    if (
        not exists
        or item.target.is_symlink()
        or _hash_object(item.target) != item.original_sha256
    ):
        raise ApplyError(
            f"operation {item.operation.id} target changed after preflight"
        )


def _commit(item: _PreparedOperation) -> None:
    action = item.operation.action
    changed = False
    try:
        if action == "remove":
            changed = True
            _remove_object(item.target)
            return
        if action == "symlink":
            temporary = _unused_link_path(item.target)
            created_temporary = False
            try:
                os.symlink(
                    item.symlink_target,
                    temporary,
                    target_is_directory=bool(item.source and item.source.is_dir()),
                )
                created_temporary = True
                _replace_with_retry(temporary, item.target)
                created_temporary = False
                changed = True
            except OSError:
                if created_temporary and temporary.is_symlink():
                    temporary.unlink()
                raise
            return
        if item.staged is None:
            raise ApplyError(f"operation {item.operation.id} has no rendered output")
        if _lexists(item.target) and not (
            item.target.is_file() and item.staged.is_file()
        ):
            changed = True
            _remove_object(item.target)
        _replace_with_retry(item.staged, item.target)
        changed = True
        if (
            item.original_mode is not None
            and os.name == "posix"
            and action in {"merge", "configure"}
        ):
            item.target.chmod(item.original_mode)
    except OSError as error:
        if changed:
            try:
                _restore_item(item)
            except OSError as restore_error:
                raise ApplyError(
                    f"operation {item.operation.id} commit and recovery failed: "
                    f"{error}; {restore_error}"
                ) from restore_error
        if action == "symlink":
            raise ApplyError(
                f"operation {item.operation.id} symlink is unsupported: {error}"
            ) from error
        raise ApplyError(
            f"operation {item.operation.id} commit failed: {error}"
        ) from error


def _restore_applied(
    applied: Iterable[_PreparedOperation],
    created_directories: Iterable[Path],
) -> None:
    failures: list[str] = []
    for item in reversed(tuple(applied)):
        try:
            _restore_item(item)
        except OSError as error:
            failures.append(f"{item.operation.id}: {error}")
    for directory in reversed(tuple(created_directories)):
        try:
            directory.rmdir()
        except OSError:
            pass
    if failures:
        raise ApplyError("transaction rollback failed: " + "; ".join(failures))


def _write_recovery_journal(
    installation_root: Path,
    *,
    installation_id: str,
    applied: Iterable[_PreparedOperation],
    created_directories: Iterable[Path],
    error: BaseException,
    rollback_error: BaseException,
) -> Path:
    """Persist enough metadata to finish a rollback after this process exits."""

    journal = {
        "schema_version": 1,
        "status": "recovery_required",
        "installation_id": installation_id,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "failure": f"{type(error).__name__}: {error}",
        "rollback_failure": (f"{type(rollback_error).__name__}: {rollback_error}"),
        "created_parent_directories": [str(path) for path in created_directories],
        "operations": [
            {
                "operation_id": item.operation.id,
                "target": str(item.target),
                "backup_path": (str(item.backup) if item.backup is not None else None),
                "original_sha256": item.original_sha256,
                "original_mode": item.original_mode,
            }
            for item in applied
        ],
    }
    ensure_private_directory(installation_root)
    return write_json_atomic(installation_root / "recovery.json", journal)


def _restore_item(item: _PreparedOperation) -> None:
    if _lexists(item.target):
        _remove_object(item.target)
    if item.backup is not None and _lexists(item.backup):
        _copy_object(item.backup, item.target)
        if item.original_mode is not None and os.name == "posix":
            item.target.chmod(item.original_mode)


def _build_receipt(
    plan: Plan,
    *,
    installation_id: str,
    prepared: list[_PreparedOperation],
    created_directories: list[Path],
    campaign_id: str | None,
    snapshot_id: str | None,
) -> dict:
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    parent_strings = [str(path) for path in created_directories]
    operations = []
    for item in prepared:
        operations.append(
            {
                "id": item.operation.id,
                "operation_id": item.operation.id,
                "action": item.operation.action,
                "target": str(item.target),
                "source": str(item.source) if item.source is not None else None,
                "atomic_group_id": item.operation.atomic_group_id,
                "original_sha256": item.original_sha256,
                "installed_sha256": item.installed_sha256,
                "backup_path": str(item.backup) if item.backup is not None else None,
                "original_mode": item.original_mode,
                "created_symlink_target": item.symlink_target,
                "created_parent_directories": parent_strings,
            }
        )
    receipt = {
        "schema_version": 1,
        "apu_version": plan.apu_version,
        "installation_id": installation_id,
        "created_at": created_at,
        "host_identifier_sha256": sha256(platform.node().encode("utf-8")).hexdigest(),
        "plan_inventory_sha256": plan.inventory_sha256,
        "applied_operation_ids": [item.operation.id for item in prepared],
        "operations": operations,
        "validation": dict(plan.validation),
        "rollback_status": "available",
    }
    if campaign_id is not None and snapshot_id is not None:
        receipt.update(
            {
                "campaign_id": campaign_id,
                "snapshot_id": snapshot_id,
                "idempotency_keys": {
                    item.operation.id: {
                        "operation_id": item.operation.id,
                        "attempt": 1,
                    }
                    for item in prepared
                },
            }
        )
    return receipt


def _absolute(value: str, description: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ApplyError(f"{description} must be absolute")
    return path


def _storage_name(index: int, operation_id: str) -> str:
    identifier_hash = sha256(operation_id.encode("utf-8")).hexdigest()[:16]
    return f"{index:04d}-{identifier_hash}"


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _mode(path: Path) -> int | None:
    if os.name != "posix":
        return None
    return path.lstat().st_mode & 0o777


def _hash_object(path: Path) -> str:
    try:
        return hash_object(path)
    except OSError as error:
        raise ApplyError(str(error)) from error


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
    elif source.is_file():
        shutil.copy2(source, destination)
    else:
        raise ApplyError(f"unsupported filesystem object: {source}")


def _remove_object(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _ensure_unused_installation(state_home: Path, installation_id: str) -> None:
    from .state import load_registry

    registry = load_registry(state_home)
    storage = state_home / "installations" / installation_id
    if installation_id in registry["installations"] or _lexists(storage):
        raise ApplyError(f"installation_id already exists: {installation_id}")


def _validate_mutation_target(
    operation: PlanOperation,
    target: Path,
    *,
    target_exists: bool,
    state_home: Path,
    protected_roots: Iterable[str],
) -> None:
    resolved = target.resolve(strict=False)
    state_root = state_home.resolve(strict=False)
    protected = {
        state_root,
        Path(target.anchor).resolve(strict=False),
        Path.home().resolve(strict=False),
    }
    for value in protected_roots:
        path = Path(value).expanduser()
        if path.is_absolute():
            protected.add(path.resolve(strict=False))
    if resolved in protected or resolved.is_relative_to(state_root):
        raise ApplyError(f"operation {operation.id} targets a protected root: {target}")
    if not target_exists or not target.is_dir():
        return
    if operation.action in {"merge", "configure"}:
        raise ApplyError(
            f"operation {operation.id} cannot recursively replace a directory"
        )
    if operation.action == "remove" and operation.atomic_group_id is None:
        raise ApplyError(
            f"operation {operation.id} cannot recursively remove a directory"
        )


def _unused_link_path(target: Path) -> Path:
    for _ in range(8):
        candidate = target.parent / (f".{target.name}.apu-link-{secrets.token_hex(8)}")
        if not _lexists(candidate):
            return candidate
    raise ApplyError(f"could not allocate temporary link beside {target}")


def _replace_with_retry(
    source: Path,
    target: Path,
    *,
    windows: bool | None = None,
    replace=None,
    sleep=time.sleep,
) -> None:
    """Replace once normally, or retry two transient Windows sharing failures."""

    on_windows = os.name == "nt" if windows is None else windows
    replace_operation = replace or os.replace
    delays = (0.05, 0.1)
    for attempt in range(len(delays) + 1):
        try:
            replace_operation(source, target)
            return
        except OSError as error:
            sharing_violation = isinstance(error, PermissionError) or getattr(
                error, "winerror", None
            ) in {32, 33}
            if not on_windows or not sharing_violation or attempt == len(delays):
                raise
            sleep(delays[attempt])


def _ensure_parent(parent: Path, created: list[Path]) -> None:
    missing: list[Path] = []
    cursor = parent
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir()
        created.append(directory)

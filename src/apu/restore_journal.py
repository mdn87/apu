from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TypeAlias
from uuid import uuid4

from .models import sha256_bytes
from .state import ensure_private_directory, write_json_atomic

JOURNAL_SCHEMA_VERSION = 1
OBJECT_TYPES = frozenset(
    {"absent", "file", "directory", "symlink", "junction"}
)
JOURNAL_STATUSES = frozenset(
    {
        "prepared",
        "unwinding",
        "needs_recovery",
        "rolled_back",
        "resuming",
        "resuming_unwind",
        "completed",
        "unwound",
    }
)

FailureHook: TypeAlias = Callable[[str, int, Path], None]


class RestoreError(RuntimeError):
    """Base error for journaled restores."""


class RestorePreflightError(RestoreError):
    """Raised before live targets are mutated."""


class RestoreInterrupted(RestoreError):
    """Raised when a restore journal needs an explicit resume or unwind."""

    def __init__(self, journal_id: str, message: str) -> None:
        super().__init__(f"{message}; journal_id={journal_id}")
        self.journal_id = journal_id


@dataclass(frozen=True)
class RestoreItem:
    """One exact restore target and its prepared desired object.

    ``replacement=None`` means the target should be absent. A present
    precondition always requires both its object type and content hash.
    """

    target: Path
    replacement: Path | None
    expected_type: str
    expected_sha256: str | None


@dataclass(frozen=True)
class RestoreResult:
    journal_id: str
    status: str
    journal_path: Path


def hash_restore_object(path: Path) -> str:
    """Hash an object without traversing symlink or junction destinations."""

    candidate = Path(path)
    if _is_junction(candidate):
        return sha256_bytes(b"J\0" + os.fsencode(os.readlink(candidate)))
    if candidate.is_symlink():
        return sha256_bytes(b"L\0" + os.fsencode(os.readlink(candidate)))
    metadata = candidate.lstat()
    if stat.S_ISREG(metadata.st_mode):
        return sha256_bytes(candidate.read_bytes())
    if stat.S_ISDIR(metadata.st_mode):
        digest = sha256()
        for child, relative, child_type in _walk_tree_without_reparse(candidate):
            encoded_relative = relative.as_posix().encode("utf-8")
            if child_type == "junction":
                digest.update(b"J\0" + encoded_relative + b"\0")
                digest.update(os.fsencode(os.readlink(child)))
            elif child_type == "symlink":
                digest.update(b"L\0" + encoded_relative + b"\0")
                digest.update(os.fsencode(os.readlink(child)))
            elif child_type == "directory":
                digest.update(b"D\0" + encoded_relative)
            elif child_type == "file":
                digest.update(b"F\0" + encoded_relative + b"\0")
                digest.update(child.read_bytes())
            else:
                digest.update(b"O\0" + encoded_relative)
            digest.update(b"\0")
        return digest.hexdigest()
    raise OSError(f"unsupported filesystem object: {candidate}")


def restore_items(
    items: Iterable[RestoreItem],
    journal_root: Path,
    *,
    snapshot_id: str | None = None,
    protected_roots: Iterable[Path] = (),
    force_paths: Iterable[Path] = (),
    journal_id: str | None = None,
    failure_hook: FailureHook | None = None,
) -> RestoreResult:
    """Restore exact targets, reversing completed swaps on the first failure."""

    root = _absolute(journal_root, "journal_root")
    requested = tuple(items)
    identifier = _validate_journal_id(journal_id or uuid4().hex)
    snapshot = _validate_snapshot_id(snapshot_id)
    force = {_path_identity(_absolute(path, "force path")) for path in force_paths}
    requested_targets = {
        _path_identity(_absolute(item.target, "target")) for item in requested
    }
    unknown_force = force - requested_targets
    if unknown_force:
        raise RestorePreflightError(
            "force paths must name exact restore targets: "
            + ", ".join(sorted(unknown_force))
        )
    prepared = _preflight(
        requested,
        root,
        protected_roots=tuple(protected_roots),
        force_paths=force,
    )

    ensure_private_directory(root)
    transaction = root / identifier
    if os.path.lexists(transaction):
        raise RestorePreflightError(f"restore journal already exists: {identifier}")
    ensure_private_directory(transaction)
    ensure_private_directory(transaction / "originals")
    ensure_private_directory(transaction / "desired")

    journal = _build_journal(
        identifier, transaction, prepared, force, snapshot
    )
    journal_path = transaction / "journal.json"
    try:
        _capture_artifacts(journal, transaction)
        _stage_desired_objects(journal, transaction)
        _verify_all_originals(journal)
    except BaseException:
        _remove_object(transaction)
        raise

    journal["status"] = "prepared"
    _persist(journal_path, journal)

    completed: list[int] = []
    failure: BaseException | None = None
    for index in range(len(journal["items"])):
        try:
            _apply_one(journal, journal_path, transaction, index, failure_hook)
            completed.append(index)
        except BaseException as error:  # noqa: BLE001 - journal interrupts too
            failure = error
            _record_observed_states(journal)
            journal["status"] = "unwinding"
            _persist(journal_path, journal)
            break

    if failure is not None:
        reverse_failed = False
        candidates = list(reversed(completed))
        current = len(completed)
        if current < len(journal["items"]):
            candidates.insert(0, current)
        for index in candidates:
            try:
                _restore_original(
                    journal, journal_path, transaction, index, failure_hook
                )
            except BaseException:  # noqa: BLE001 - best-effort emergency unwind
                reverse_failed = True
                _record_observed_states(journal)
                _persist(journal_path, journal)
        journal["status"] = "needs_recovery" if reverse_failed else "rolled_back"
        _record_observed_states(journal)
        _persist(journal_path, journal)
        if reverse_failed:
            raise RestoreInterrupted(
                identifier, f"restore failed and automatic unwind was incomplete: {failure}"
            ) from failure
        raise RestoreError(
            f"restore failed and all completed swaps were reversed; "
            f"journal_id={identifier}: {failure}"
        ) from failure

    try:
        _cleanup_side_paths(journal)
    except OSError as error:
        journal["status"] = "needs_recovery"
        _record_observed_states(journal)
        _persist(journal_path, journal)
        raise RestoreInterrupted(
            identifier, f"restore completed but temporary cleanup failed: {error}"
        ) from error
    journal["status"] = "completed"
    _record_observed_states(journal)
    _persist(journal_path, journal)
    return RestoreResult(identifier, "completed", journal_path)


def resume_restore(
    journal_root: Path,
    journal_id: str,
    *,
    unwind: bool = False,
    failure_hook: FailureHook | None = None,
) -> RestoreResult:
    """Idempotently complete or reverse a previously journaled restore."""

    root = _absolute(journal_root, "journal_root")
    identifier = _validate_journal_id(journal_id)
    transaction = root / identifier
    journal_path = transaction / "journal.json"
    journal = _load_journal(journal_path, identifier)
    _validate_journal_paths(journal, transaction)
    _record_observed_states(journal)
    journal["status"] = "resuming_unwind" if unwind else "resuming"
    _persist(journal_path, journal)

    indexes = range(len(journal["items"]) - 1, -1, -1) if unwind else range(
        len(journal["items"])
    )
    try:
        for index in indexes:
            if unwind:
                _restore_original(
                    journal, journal_path, transaction, index, failure_hook
                )
            else:
                _complete_one(journal, journal_path, transaction, index, failure_hook)
        _cleanup_side_paths(journal)
    except BaseException as error:
        journal["status"] = "needs_recovery"
        _record_observed_states(journal)
        _persist(journal_path, journal)
        raise RestoreInterrupted(identifier, f"restore recovery failed: {error}") from error

    status = "unwound" if unwind else "completed"
    journal["status"] = status
    _record_observed_states(journal)
    _persist(journal_path, journal)
    return RestoreResult(identifier, status, journal_path)


def list_restore_journals(journal_root: Path) -> list[dict[str, object]]:
    """Return validated, deterministic summaries without changing journals."""

    root = _absolute(journal_root, "journal_root")
    if not os.path.lexists(root):
        return []
    if root.is_symlink() or _is_junction(root) or not root.is_dir():
        raise RestoreError(
            f"restore journal root must be a real directory: {root}"
        )

    summaries: list[dict[str, object]] = []
    for transaction in sorted(root.iterdir(), key=lambda path: path.name):
        try:
            identifier = _validate_journal_id(transaction.name)
            if (
                transaction.is_symlink()
                or _is_junction(transaction)
                or not transaction.is_dir()
            ):
                raise RestoreError(
                    f"unexpected entry in restore journal root: {transaction}"
                )
            journal = _load_journal(
                transaction / "journal.json", identifier
            )
            _validate_journal_paths(journal, transaction)
            status = journal.get("status")
            if status not in JOURNAL_STATUSES:
                raise RestoreError(
                    f"restore journal {identifier} has an invalid status"
                )
            targets: list[dict[str, object]] = []
            for item in journal["items"]:
                targets.append(
                    {
                        "index": item["index"],
                        "target": item["target"],
                        "state": _classify_target(item),
                        "observed": {
                            "target": _describe_object(
                                Path(item["target"])
                            ),
                            "prepared": _describe_object(
                                Path(item["prepared_path"])
                            ),
                            "displaced": _describe_object(
                                Path(item["displaced_path"])
                            ),
                        },
                    }
                )
            summaries.append(
                {
                    "journal_id": identifier,
                    "snapshot_id": journal.get("snapshot_id"),
                    "status": status,
                    "item_count": len(targets),
                    "targets": targets,
                }
            )
        except RestoreError:
            raise
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise RestoreError(
                f"invalid restore journal at {transaction}: {error}"
            ) from error
    return summaries


@dataclass(frozen=True)
class _PreparedItem:
    target: Path
    replacement: Path | None
    expected_type: str
    expected_sha256: str | None
    original: Mapping[str, object]
    desired: Mapping[str, object]


def _preflight(
    items: tuple[RestoreItem, ...],
    journal_root: Path,
    *,
    protected_roots: tuple[Path, ...],
    force_paths: set[str],
) -> tuple[_PreparedItem, ...]:
    if not items:
        raise RestorePreflightError("restore requires at least one item")

    protected = [
        _absolute(path, "protected root") for path in protected_roots
    ]
    protected.extend(
        [
            Path(journal_root.anchor),
            Path.home(),
            journal_root,
        ]
    )
    identities: set[str] = set()
    result: list[_PreparedItem] = []
    for item in items:
        target = _absolute(item.target, "target")
        identity = _path_identity(target)
        if identity in identities:
            raise RestorePreflightError(f"duplicate restore target: {target}")
        identities.add(identity)
        _validate_target_path(target, protected, journal_root)
        _validate_expected(item)

        actual = _describe_object(target)
        forced = identity in force_paths
        if not forced and not _matches_expected(actual, item):
            raise RestorePreflightError(
                f"restore target drifted: {target}; "
                f"expected {item.expected_type}/{item.expected_sha256}, "
                f"found {actual['type']}/{actual['sha256']}"
            )

        replacement = (
            None if item.replacement is None else _absolute(item.replacement, "replacement")
        )
        if replacement is None:
            desired = _absent_description()
        else:
            desired = _describe_object(replacement)
            if desired["type"] == "absent":
                raise RestorePreflightError(
                    f"prepared replacement does not exist: {replacement}"
                )
        result.append(
            _PreparedItem(
                target=target,
                replacement=replacement,
                expected_type=item.expected_type,
                expected_sha256=item.expected_sha256,
                original=actual,
                desired=desired,
            )
        )
    return tuple(result)


def _build_journal(
    journal_id: str,
    transaction: Path,
    items: tuple[_PreparedItem, ...],
    force_paths: set[str],
    snapshot_id: str | None,
) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for index, item in enumerate(items):
        stem = f"{index:04d}"
        suffix = f".apu-restore-{journal_id}-{stem}"
        original = dict(item.original)
        desired = dict(item.desired)
        original["artifact"] = (
            str(transaction / "originals" / stem)
            if original["type"] != "absent"
            else None
        )
        desired["artifact"] = (
            str(transaction / "desired" / stem)
            if desired["type"] != "absent"
            else None
        )
        records.append(
            {
                "index": index,
                "target": str(item.target),
                "expected": {
                    "type": item.expected_type,
                    "sha256": item.expected_sha256,
                },
                "forced": _path_identity(item.target) in force_paths,
                "original": original,
                "desired": desired,
                "prepared_path": str(item.target.parent / f".{item.target.name}{suffix}.new"),
                "displaced_path": str(
                    item.target.parent / f".{item.target.name}{suffix}.old"
                ),
                "incoming_path": str(
                    item.target.parent / f".{item.target.name}{suffix}.incoming"
                ),
                "phase": "capturing",
                "state": "original",
                "observed": {},
                "_replacement_source": (
                    str(item.replacement) if item.replacement is not None else None
                ),
            }
        )
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "journal_id": journal_id,
        "snapshot_id": snapshot_id,
        "status": "capturing",
        "items": records,
    }


def _capture_artifacts(journal: dict[str, object], transaction: Path) -> None:
    for item in journal["items"]:
        original = item["original"]
        desired = item["desired"]
        if original["type"] != "absent":
            _copy_object(Path(item["target"]), Path(original["artifact"]))
            _verify_artifact(original)
        if desired["type"] != "absent":
            source = Path(item["_replacement_source"])
            _copy_object(source, Path(desired["artifact"]))
            _verify_artifact(desired)
        item.pop("_replacement_source")
        item["phase"] = "captured"


def _stage_desired_objects(
    journal: dict[str, object], transaction: Path
) -> None:
    del transaction
    made: list[Path] = []
    try:
        for item in journal["items"]:
            desired = item["desired"]
            if desired["type"] == "absent":
                continue
            prepared = Path(item["prepared_path"])
            if os.path.lexists(prepared):
                raise RestorePreflightError(
                    f"same-parent restore path is already occupied: {prepared}"
                )
            _copy_object(Path(desired["artifact"]), prepared)
            made.append(prepared)
            _verify_matches(prepared, desired, "staged replacement")
    except BaseException:
        for path in reversed(made):
            _remove_object(path)
        raise


def _verify_all_originals(journal: Mapping[str, object]) -> None:
    for item in journal["items"]:
        _verify_matches(Path(item["target"]), item["original"], "restore target")


def _apply_one(
    journal: dict[str, object],
    journal_path: Path,
    transaction: Path,
    index: int,
    failure_hook: FailureHook | None,
) -> None:
    del transaction
    item = journal["items"][index]
    target = Path(item["target"])
    original = item["original"]
    desired = item["desired"]
    prepared = Path(item["prepared_path"])
    displaced = Path(item["displaced_path"])
    _verify_matches(target, original, "restore target")
    if os.path.lexists(displaced):
        raise RestoreError(f"restore displaced path is occupied: {displaced}")

    item["phase"] = "swapping"
    _persist(journal_path, journal)
    _call_hook(failure_hook, "before_swap", index, target)
    if original["type"] != "absent":
        os.replace(target, displaced)
    item["phase"] = "original_displaced"
    _record_observed(item)
    _persist(journal_path, journal)
    _call_hook(failure_hook, "after_old_moved", index, target)
    if desired["type"] != "absent":
        os.replace(prepared, target)
    _verify_matches(target, desired, "restored target")
    item["phase"] = "swapped"
    item["state"] = "desired"
    _record_observed(item)
    _persist(journal_path, journal)
    _call_hook(failure_hook, "after_swap", index, target)


def _complete_one(
    journal: dict[str, object],
    journal_path: Path,
    transaction: Path,
    index: int,
    failure_hook: FailureHook | None,
) -> None:
    del transaction
    item = journal["items"][index]
    state = _classify_target(item)
    if state == "desired":
        item["state"] = "desired"
        item["phase"] = "swapped"
        _record_observed(item)
        _persist(journal_path, journal)
        return
    if state not in {"original", "transitional"}:
        raise RestoreError(f"target has unknown recovery state: {item['target']}")
    if state == "transitional":
        _restore_original(journal, journal_path, Path(), index, None)
    _ensure_desired_prepared(item)
    _apply_one(journal, journal_path, Path(), index, failure_hook)


def _restore_original(
    journal: dict[str, object],
    journal_path: Path,
    transaction: Path,
    index: int,
    failure_hook: FailureHook | None,
) -> None:
    del transaction
    item = journal["items"][index]
    target = Path(item["target"])
    original = item["original"]
    desired = item["desired"]
    state = _classify_target(item)
    if state == "original":
        item["state"] = "original"
        item["phase"] = "unwound"
        _record_observed(item)
        _persist(journal_path, journal)
        return
    if state == "unknown":
        raise RestoreError(f"target has unknown recovery state: {target}")

    prepared = Path(item["prepared_path"])
    displaced = Path(item["displaced_path"])
    item["phase"] = "unwinding"
    _persist(journal_path, journal)
    _call_hook(failure_hook, "before_unwind", index, target)

    if os.path.lexists(target):
        if desired["type"] == "absent" or not _object_matches(target, desired):
            raise RestoreError(f"refusing to unwind drifted target: {target}")
        if os.path.lexists(prepared):
            _remove_if_matches(prepared, desired)
        os.replace(target, prepared)
    item["phase"] = "desired_displaced"
    _record_observed(item)
    _persist(journal_path, journal)
    _call_hook(failure_hook, "after_unwind_old_moved", index, target)

    if original["type"] != "absent":
        if os.path.lexists(displaced) and _object_matches(displaced, original):
            os.replace(displaced, target)
        else:
            if os.path.lexists(displaced):
                raise RestoreError(
                    f"restore displaced path has unexpected content: {displaced}"
                )
            incoming = Path(item["incoming_path"])
            if os.path.lexists(incoming):
                _remove_if_matches(incoming, original)
            _copy_object(Path(original["artifact"]), incoming)
            _verify_matches(incoming, original, "journaled original")
            os.replace(incoming, target)
    _verify_matches(target, original, "unwound target")
    item["phase"] = "unwound"
    item["state"] = "original"
    _record_observed(item)
    _persist(journal_path, journal)
    _call_hook(failure_hook, "after_unwind", index, target)


def _ensure_desired_prepared(item: Mapping[str, object]) -> None:
    desired = item["desired"]
    if desired["type"] == "absent":
        return
    prepared = Path(item["prepared_path"])
    if os.path.lexists(prepared):
        _verify_matches(prepared, desired, "staged replacement")
        return
    _copy_object(Path(desired["artifact"]), prepared)
    _verify_matches(prepared, desired, "staged replacement")


def _cleanup_side_paths(journal: Mapping[str, object]) -> None:
    for item in journal["items"]:
        controlled = (
            ("prepared_path", item["desired"]),
            ("displaced_path", item["original"]),
            ("incoming_path", item["original"]),
        )
        for key, expected in controlled:
            path = Path(item[key])
            if os.path.lexists(path):
                if expected["type"] == "absent":
                    raise RestoreError(
                        f"unexpected temporary restore object: {path}"
                    )
                _remove_if_matches(path, expected)


def _record_observed_states(journal: dict[str, object]) -> None:
    for item in journal["items"]:
        _record_observed(item)


def _record_observed(item: dict[str, object]) -> None:
    target = Path(item["target"])
    item["observed"] = {
        "target": _describe_object(target),
        "prepared": _describe_object(Path(item["prepared_path"])),
        "displaced": _describe_object(Path(item["displaced_path"])),
    }
    item["state"] = _classify_target(item)


def _classify_target(item: Mapping[str, object]) -> str:
    target = Path(item["target"])
    original = item["original"]
    desired = item["desired"]
    matches_original = _object_matches(target, original)
    matches_desired = _object_matches(target, desired)
    if matches_original and matches_desired:
        recorded = item.get("state")
        return recorded if recorded in {"original", "desired"} else "original"
    if matches_original:
        return "original"
    if matches_desired:
        return "desired"
    if not os.path.lexists(target):
        displaced = Path(item["displaced_path"])
        prepared = Path(item["prepared_path"])
        if (
            original["type"] != "absent"
            and os.path.lexists(displaced)
            and _object_matches(displaced, original)
        ) or (
            desired["type"] != "absent"
            and os.path.lexists(prepared)
            and _object_matches(prepared, desired)
        ):
            return "transitional"
    return "unknown"


def _describe_object(path: Path) -> dict[str, object]:
    if not os.path.lexists(path):
        return _absent_description()
    try:
        metadata = path.lstat()
        if _is_junction(path, metadata):
            object_type = "junction"
        elif stat.S_ISLNK(metadata.st_mode):
            object_type = "symlink"
        elif stat.S_ISREG(metadata.st_mode):
            object_type = "file"
        elif stat.S_ISDIR(metadata.st_mode):
            object_type = "directory"
        else:
            raise RestorePreflightError(f"unsupported filesystem object: {path}")
        return {
            "type": object_type,
            "sha256": hash_restore_object(path),
            "mode": metadata.st_mode & 0o777 if os.name == "posix" else None,
        }
    except OSError as error:
        raise RestorePreflightError(f"cannot inspect filesystem object {path}: {error}") from error


def _absent_description() -> dict[str, object]:
    return {"type": "absent", "sha256": None, "mode": None}


def _matches_expected(actual: Mapping[str, object], item: RestoreItem) -> bool:
    return (
        actual["type"] == item.expected_type
        and actual["sha256"] == item.expected_sha256
    )


def _object_matches(path: Path, expected: Mapping[str, object]) -> bool:
    try:
        actual = _describe_object(path)
    except RestorePreflightError:
        return False
    return (
        actual["type"] == expected["type"]
        and actual["sha256"] == expected["sha256"]
        and actual["mode"] == expected["mode"]
    )


def _verify_matches(
    path: Path, expected: Mapping[str, object], description: str
) -> None:
    if not _object_matches(path, expected):
        actual = _describe_object(path)
        raise RestoreError(
            f"{description} changed: {path}; expected "
            f"{expected['type']}/{expected['sha256']}, found "
            f"{actual['type']}/{actual['sha256']}"
        )


def _verify_artifact(description: Mapping[str, object]) -> None:
    artifact = description.get("artifact")
    if not isinstance(artifact, str):
        raise RestoreError("present journal artifact has no path")
    _verify_matches(Path(artifact), description, "journal artifact")


def _validate_expected(item: RestoreItem) -> None:
    if item.expected_type not in OBJECT_TYPES:
        raise RestorePreflightError(
            f"unsupported expected object type: {item.expected_type}"
        )
    if item.expected_type == "absent":
        if item.expected_sha256 is not None:
            raise RestorePreflightError(
                "absent precondition cannot include an expected hash"
            )
        return
    digest = item.expected_sha256
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise RestorePreflightError(
            "present precondition requires a lowercase SHA-256 hash"
        )


def _validate_target_path(
    target: Path, protected: list[Path], journal_root: Path
) -> None:
    parent = target.parent
    if parent.is_symlink() or _is_junction(parent):
        raise RestorePreflightError(
            f"restore target has a symlinked ancestor or junction: {target}"
        )
    if not parent.exists() or not parent.is_dir():
        raise RestorePreflightError(
            f"restore target parent must be an existing real directory: {parent}"
        )
    cursor = parent
    while cursor != cursor.parent:
        if cursor.is_symlink() or _is_junction(cursor):
            raise RestorePreflightError(
                f"restore target has a symlinked ancestor or junction: {target}"
            )
        cursor = cursor.parent

    target_id = _path_identity(target)
    journal_id = _path_identity(journal_root)
    if _overlaps(target_id, journal_id):
        raise RestorePreflightError(
            f"restore target overlaps the private journal root: {target}"
        )
    for root in protected:
        root_id = _path_identity(root)
        if target_id == root_id or _is_parent(target_id, root_id):
            raise RestorePreflightError(f"restore target is a protected root: {target}")


def _validate_journal_paths(
    journal: Mapping[str, object], transaction: Path
) -> None:
    if journal.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        raise RestoreError("unsupported restore journal schema_version")
    if journal.get("journal_id") != transaction.name:
        raise RestoreError("restore journal identity does not match its directory")
    _validate_snapshot_id(journal.get("snapshot_id"))
    items = journal.get("items")
    if not isinstance(items, list) or not items:
        raise RestoreError("restore journal items must be a non-empty list")
    transaction_id = _path_identity(transaction)
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("index") != index:
            raise RestoreError("restore journal item indexes are invalid")
        for key in ("original", "desired"):
            description = item.get(key)
            if not isinstance(description, dict):
                raise RestoreError(f"restore journal item has invalid {key}")
            artifact = description.get("artifact")
            if artifact is not None and not _is_within(
                _path_identity(Path(artifact)), transaction_id
            ):
                raise RestoreError("restore artifact escapes its transaction")
        target = Path(item.get("target", ""))
        if not target.is_absolute():
            raise RestoreError("restore journal target is not absolute")
        for key in ("prepared_path", "displaced_path", "incoming_path"):
            side = Path(item.get(key, ""))
            if side == target or side.parent != target.parent:
                raise RestoreError("restore side path is not beside its target")


def _load_journal(path: Path, journal_id: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RestoreError(f"cannot load restore journal {journal_id}: {error}") from error
    if not isinstance(value, dict):
        raise RestoreError("restore journal must be a JSON object")
    # Schema v1 predates snapshot binding. Treat a missing field as the
    # explicitly unbound state while ensuring every in-memory journal has it.
    value.setdefault("snapshot_id", None)
    return value


def _persist(path: Path, journal: Mapping[str, object]) -> None:
    write_json_atomic(path, journal)


def _copy_object(source: Path, destination: Path) -> None:
    if os.path.lexists(destination):
        raise RestoreError(f"refusing to overwrite restore artifact: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata = source.lstat()
    if _is_junction(source, metadata):
        _create_junction(os.readlink(source), destination)
    elif stat.S_ISLNK(metadata.st_mode):
        os.symlink(
            os.readlink(source),
            destination,
            target_is_directory=_symlink_is_directory(metadata),
        )
    elif stat.S_ISDIR(metadata.st_mode):
        shutil.copytree(source, destination, symlinks=True)
    elif stat.S_ISREG(metadata.st_mode):
        shutil.copy2(source, destination)
    else:
        raise RestoreError(f"unsupported filesystem object: {source}")


def _remove_object(path: Path) -> None:
    if not os.path.lexists(path):
        return
    metadata = path.lstat()
    if _is_junction(path, metadata):
        path.rmdir()
    elif stat.S_ISLNK(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
        path.unlink()
    elif stat.S_ISDIR(metadata.st_mode):
        shutil.rmtree(path)
    else:
        raise RestoreError(f"unsupported filesystem object: {path}")


def _remove_if_matches(path: Path, expected: Mapping[str, object]) -> None:
    if not _object_matches(path, expected):
        raise RestoreError(f"temporary restore path has unexpected content: {path}")
    _remove_object(path)


def _symlink_is_directory(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    directory_attribute = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0)
    return bool(attributes & directory_attribute)


def _is_junction(
    path: Path, metadata: os.stat_result | None = None
) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None:
        try:
            return bool(is_junction(path))
        except OSError:
            return False
    if os.name != "nt":
        return False
    try:
        details = path.lstat() if metadata is None else metadata
    except OSError:
        return False
    mount_point_tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    return (
        mount_point_tag is not None
        and getattr(details, "st_reparse_tag", None) == mount_point_tag
    )


def _create_junction(raw_target: str, destination: Path) -> None:
    if os.name != "nt":
        raise RestoreError("junction restoration is supported only on Windows")
    try:
        import _winapi
    except ImportError as error:
        raise RestoreError(
            "this Python runtime cannot create Windows junctions"
        ) from error
    create_junction = getattr(_winapi, "CreateJunction", None)
    if create_junction is None:
        raise RestoreError(
            "this Python runtime cannot create Windows junctions"
        )
    try:
        create_junction(_junction_creation_target(raw_target), str(destination))
    except OSError as error:
        raise RestoreError(
            f"cannot recreate Windows junction {destination}: {error}"
        ) from error


def _junction_creation_target(raw_target: str) -> str:
    if raw_target.startswith("\\\\?\\UNC\\"):
        return "\\\\" + raw_target[8:]
    if raw_target.startswith("\\\\?\\"):
        return raw_target[4:]
    if raw_target.startswith("\\??\\UNC\\"):
        return "\\\\" + raw_target[8:]
    if raw_target.startswith("\\??\\"):
        return raw_target[4:]
    return raw_target


def _walk_tree_without_reparse(
    root: Path,
) -> list[tuple[Path, Path, str]]:
    entries: list[tuple[Path, Path, str]] = []
    pending: list[tuple[Path, Path]] = [(root, Path())]
    while pending:
        directory, relative_parent = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                path = Path(child.path)
                relative = relative_parent / child.name
                metadata = child.stat(follow_symlinks=False)
                if _is_junction(path, metadata):
                    object_type = "junction"
                elif child.is_symlink():
                    object_type = "symlink"
                elif stat.S_ISDIR(metadata.st_mode):
                    object_type = "directory"
                    pending.append((path, relative))
                elif stat.S_ISREG(metadata.st_mode):
                    object_type = "file"
                else:
                    object_type = "other"
                entries.append((path, relative, object_type))
    return sorted(entries, key=lambda item: item[1].as_posix())


def _absolute(path: Path, description: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise RestorePreflightError(f"{description} must be absolute")
    return Path(os.path.abspath(candidate))


def _path_identity(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _is_parent(parent: str, child: str) -> bool:
    try:
        return os.path.commonpath([parent, child]) == parent and parent != child
    except ValueError:
        return False


def _is_within(child: str, parent: str) -> bool:
    return child == parent or _is_parent(parent, child)


def _overlaps(left: str, right: str) -> bool:
    return left == right or _is_parent(left, right) or _is_parent(right, left)


def _validate_journal_id(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise RestorePreflightError("journal_id must be one safe path component")
    return value


def _validate_snapshot_id(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RestorePreflightError(
            "snapshot_id must be a lowercase SHA-256 hash"
        )
    return value


def _call_hook(
    hook: FailureHook | None, event: str, index: int, target: Path
) -> None:
    if hook is not None:
        hook(event, index, target)

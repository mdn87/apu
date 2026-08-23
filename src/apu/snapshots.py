from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any
from .filesystem import matches_exclude

from .models import sha256_bytes, sha256_json
from .state import ensure_private_directory, write_json_atomic

SNAPSHOT_SCHEMA_VERSION = 1
DEFAULT_RETENTION_COUNT = 10
_OBJECT_TYPES = frozenset({"file", "directory", "symlink", "junction"})
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class SnapshotSurface:
    """A logical policy surface and the live filesystem object backing it."""

    logical_path: str
    root: Path
    # Surface-relative patterns to skip, mirroring ProfileRoot/ProfileSurface.
    excludes: tuple[str, ...] = ()


def create_snapshot(
    state_home: Path,
    surfaces: Mapping[str, Path] | Iterable[SnapshotSurface],
    *,
    label: str | None = None,
    campaign_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Capture declared surfaces in private, content-addressed storage."""

    normalized = _normalize_surfaces(surfaces)
    if label is not None and (not isinstance(label, str) or not label.strip()):
        raise ValueError("snapshot label must be a non-empty string")
    if campaign_id is not None:
        _validate_component(campaign_id, "campaign_id")
    captured_at = created_at or _now()
    _parse_timestamp(captured_at)

    snapshot_root = _snapshot_root(state_home)
    ensure_private_directory(snapshot_root)
    ensure_private_directory(snapshot_root / "blobs")
    ensure_private_directory(snapshot_root / "blobs" / ".tmp")
    ensure_private_directory(snapshot_root / "manifests")

    entries: list[dict[str, Any]] = []
    declared: list[dict[str, Any]] = []
    for surface in normalized:
        captured = _capture_surface(
            surface,
            blob_root=snapshot_root / "blobs",
        )
        declared.append(
            {
                "logical_path": surface.logical_path,
                "root": str(surface.root),
                "present": bool(captured),
            }
        )
        entries.extend(captured)

    body: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "created_at": captured_at,
        "campaign_id": campaign_id,
        "label": label,
        "acl_restoration": "out_of_scope",
        "surfaces": declared,
        "entries": sorted(entries, key=_entry_sort_key),
    }
    snapshot_id = sha256_json(body)
    manifest = {"snapshot_id": snapshot_id, **body}
    _validate_manifest(
        manifest,
        state_home=state_home,
        expected_snapshot_id=snapshot_id,
        verify_blobs=True,
    )
    destination = snapshot_root / "manifests" / f"{snapshot_id}.json"
    if destination.exists():
        existing = load_snapshot(state_home, snapshot_id)
        if existing != manifest:
            raise ValueError(f"snapshot id collision at {destination}")
        return existing
    write_json_atomic(destination, manifest)
    return manifest


def load_snapshot(
    state_home: Path,
    snapshot_id: str,
    *,
    verify_blobs: bool = True,
) -> dict[str, Any]:
    """Load and validate one immutable snapshot manifest."""

    _validate_sha256(snapshot_id, "snapshot_id")
    path = _snapshot_root(state_home) / "manifests" / f"{snapshot_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"snapshot not found: {snapshot_id}") from None
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid snapshot manifest at {path}: {error}") from error
    _validate_manifest(
        value,
        state_home=state_home,
        expected_snapshot_id=snapshot_id,
        verify_blobs=verify_blobs,
    )
    return value


def list_snapshots(
    state_home: Path,
    *,
    verify_blobs: bool = True,
) -> list[dict[str, Any]]:
    """Return all validated manifests, newest first with a stable tie-breaker."""

    manifest_root = _snapshot_root(state_home) / "manifests"
    if not manifest_root.exists():
        return []
    manifests: list[dict[str, Any]] = []
    for path in sorted(manifest_root.glob("*.json"), key=lambda item: item.name):
        snapshot_id = path.stem
        try:
            manifests.append(
                load_snapshot(
                    state_home,
                    snapshot_id,
                    verify_blobs=verify_blobs,
                )
            )
        except (FileNotFoundError, ValueError) as error:
            raise ValueError(
                f"invalid snapshot manifest {path.name}: {error}"
            ) from error
    return sorted(
        manifests,
        key=lambda item: (
            _parse_timestamp(item["created_at"]),
            item["snapshot_id"],
        ),
        reverse=True,
    )


def diff_snapshot(
    state_home: Path,
    snapshot_id: str,
    surfaces: Mapping[str, Path] | Iterable[SnapshotSurface] | None = None,
) -> list[dict[str, Any]]:
    """Compare current filesystem objects with a captured snapshot."""

    manifest = load_snapshot(state_home, snapshot_id)
    current_surfaces = (
        _normalize_surfaces(surfaces)
        if surfaces is not None
        else tuple(
            SnapshotSurface(
                logical_path=surface["logical_path"],
                root=Path(surface["root"]),
            )
            for surface in manifest["surfaces"]
        )
    )
    current_entries = [
        entry
        for surface in current_surfaces
        for entry in _capture_surface(surface, blob_root=None)
    ]
    expected = {
        entry["logical_path"]: entry for entry in manifest["entries"]
    }
    current = {entry["logical_path"]: entry for entry in current_entries}

    changes: list[dict[str, Any]] = []
    for logical_path in sorted(set(expected) | set(current)):
        before = expected.get(logical_path)
        after = current.get(logical_path)
        if before is None:
            changes.append(
                {
                    "logical_path": logical_path,
                    "target_path": after["target_path"],
                    "status": "added",
                    "drift": ["added"],
                    "before": None,
                    "after": _diff_view(after),
                }
            )
            continue
        if after is None:
            changes.append(
                {
                    "logical_path": logical_path,
                    "target_path": before["target_path"],
                    "status": "removed",
                    "drift": ["removed"],
                    "before": _diff_view(before),
                    "after": None,
                }
            )
            continue

        drift: list[str] = []
        if before["object_type"] != after["object_type"]:
            drift.append("type")
        else:
            object_type = before["object_type"]
            if object_type == "file" and before["hash"] != after["hash"]:
                drift.append("content")
            elif object_type in {"symlink", "junction"} and (
                before["link_target"] != after["link_target"]
                or before["link_kind"] != after["link_kind"]
            ):
                drift.append("link")
            if before.get("mode") != after.get("mode"):
                drift.append("mode")
        if before["target_path"] != after["target_path"]:
            drift.append("target")
        if drift:
            changes.append(
                {
                    "logical_path": logical_path,
                    "target_path": after["target_path"],
                    "status": "changed",
                    "drift": drift,
                    "before": _diff_view(before),
                    "after": _diff_view(after),
                }
            )
    return changes


def enforce_retention(
    state_home: Path,
    *,
    keep_last: int,
    protected_snapshot_ids: Iterable[str] = (),
    open_snapshot_ids: Iterable[str] = (),
) -> list[str]:
    """Prune old manifests while preserving recent, protected, and open ones."""

    if isinstance(keep_last, bool) or not isinstance(keep_last, int) or keep_last < 0:
        raise ValueError("keep_last must be a non-negative integer")
    protected = set(protected_snapshot_ids)
    open_ids = set(open_snapshot_ids)
    for snapshot_id in protected | open_ids:
        _validate_sha256(snapshot_id, "protected snapshot id")

    manifests = list_snapshots(state_home)
    recent = {
        manifest["snapshot_id"] for manifest in manifests[:keep_last]
    }
    retained = recent | protected | open_ids
    removed: list[str] = []
    manifest_root = _snapshot_root(state_home) / "manifests"
    for manifest in manifests:
        snapshot_id = manifest["snapshot_id"]
        if snapshot_id in retained:
            continue
        (manifest_root / f"{snapshot_id}.json").unlink()
        removed.append(snapshot_id)
    _garbage_collect_blobs(state_home)
    return removed


def resolve_blob_path(
    state_home: Path,
    blob_sha256: str,
    *,
    verify: bool = True,
) -> Path:
    """Resolve a blob inside APU state and optionally verify its content hash."""

    _validate_sha256(blob_sha256, "blob_sha256")
    blob_root = (_snapshot_root(state_home) / "blobs").absolute()
    path = blob_root / blob_sha256[:2] / blob_sha256[2:]
    if path.parent != blob_root / blob_sha256[:2]:
        raise ValueError("blob path escapes snapshot storage")
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"snapshot blob is missing: {blob_sha256}")
    if verify and _hash_file(path) != blob_sha256:
        raise ValueError(f"snapshot blob failed integrity check: {blob_sha256}")
    return path


def materialize_snapshot_object(
    state_home: Path,
    manifest: Mapping[str, Any],
    logical_path: str,
    destination: Path,
) -> Path | None:
    """Reconstruct one captured object without reading or following live roots."""

    value = dict(manifest)
    snapshot_id = value.get("snapshot_id")
    _validate_sha256(snapshot_id, "snapshot_id")
    _validate_manifest(
        value,
        state_home=state_home,
        expected_snapshot_id=snapshot_id,
        verify_blobs=True,
    )
    requested = _validate_logical_path(logical_path)
    matching = [
        entry for entry in value["entries"] if entry["logical_path"] == requested
    ]
    if not matching:
        missing_surface = next(
            (
                surface
                for surface in value["surfaces"]
                if surface["logical_path"] == requested
                and not surface["present"]
            ),
            None,
        )
        if missing_surface is not None:
            return None
        raise KeyError(f"logical path is not present in snapshot: {requested}")
    if len(matching) != 1:
        raise ValueError(f"logical path is ambiguous in snapshot: {requested}")

    selected = matching[0]
    target = _validate_materialization_destination(destination)
    subtree = _materialization_entries(value["entries"], selected)
    _materialize_entries(state_home, subtree, selected, target)
    return target


def _materialization_entries(
    entries: Iterable[Mapping[str, Any]],
    selected: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    if selected["object_type"] != "directory":
        return (dict(selected),)
    relative_path = selected["relative_path"]
    prefix = "" if relative_path == "." else f"{relative_path}/"
    subtree = [
        dict(entry)
        for entry in entries
        if entry["surface"] == selected["surface"]
        and (
            entry["relative_path"] == relative_path
            or (
                prefix
                and entry["relative_path"].startswith(prefix)
            )
            or relative_path == "."
        )
    ]
    return tuple(sorted(subtree, key=_entry_sort_key))


def _materialize_entries(
    state_home: Path,
    entries: tuple[dict[str, Any], ...],
    selected: Mapping[str, Any],
    destination: Path,
) -> None:
    selected_relative = selected["relative_path"]
    if selected["object_type"] == "file":
        _copy_blob_exclusive(
            resolve_blob_path(state_home, selected["blob_sha256"]),
            destination,
        )
        _apply_mode(destination, selected["mode"])
        return
    if selected["object_type"] in {"symlink", "junction"}:
        _create_snapshot_link(selected, destination)
        return

    destination.mkdir(mode=0o700)
    try:
        directories = [
            entry
            for entry in entries
            if entry["object_type"] == "directory"
            and entry["relative_path"] != selected_relative
        ]
        directories.sort(key=lambda entry: _entry_depth(entry["relative_path"]))
        for entry in directories:
            _materialized_path(
                destination,
                selected_relative,
                entry["relative_path"],
            ).mkdir(mode=0o700)

        leaves = [
            entry for entry in entries if entry["object_type"] != "directory"
        ]
        for entry in sorted(leaves, key=_entry_sort_key):
            target = _materialized_path(
                destination,
                selected_relative,
                entry["relative_path"],
            )
            if entry["object_type"] == "file":
                _copy_blob_exclusive(
                    resolve_blob_path(state_home, entry["blob_sha256"]),
                    target,
                )
                _apply_mode(target, entry["mode"])
            else:
                _create_snapshot_link(entry, target)

        for entry in sorted(
            [selected, *directories],
            key=lambda item: _entry_depth(item["relative_path"]),
            reverse=True,
        ):
            target = (
                destination
                if entry["relative_path"] == selected_relative
                else _materialized_path(
                    destination,
                    selected_relative,
                    entry["relative_path"],
                )
            )
            _apply_mode(target, entry["mode"])
    except BaseException:
        _remove_materialized_object(destination)
        raise


def _validate_materialization_destination(destination: Path) -> Path:
    candidate = Path(destination).expanduser()
    if not candidate.is_absolute():
        raise ValueError("materialization destination must be absolute")
    candidate = Path(os.path.abspath(candidate))
    if _lexists(candidate):
        raise FileExistsError(
            f"materialization destination already exists: {candidate}"
        )
    parent = candidate.parent
    if not parent.is_dir():
        raise ValueError(
            f"materialization destination parent is not a directory: {parent}"
        )
    cursor = parent
    while True:
        if cursor.is_symlink() or _is_junction(cursor):
            raise ValueError(
                f"materialization destination has a linked ancestor: {cursor}"
            )
        if not cursor.is_dir():
            raise ValueError(
                f"materialization destination ancestor is not a directory: {cursor}"
            )
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    return candidate


def _materialized_path(
    destination: Path,
    selected_relative: str,
    entry_relative: str,
) -> Path:
    if entry_relative == selected_relative:
        return destination
    if selected_relative == ".":
        remainder = PurePosixPath(entry_relative)
    else:
        try:
            remainder = PurePosixPath(entry_relative).relative_to(
                PurePosixPath(selected_relative)
            )
        except ValueError as error:
            raise ValueError("snapshot entry escapes selected subtree") from error
    target = destination.joinpath(*remainder.parts)
    try:
        common = os.path.commonpath([destination, target])
    except ValueError as error:
        raise ValueError(
            "snapshot entry escapes materialization destination"
        ) from error
    if os.path.normcase(common) != os.path.normcase(str(destination)):
        raise ValueError("snapshot entry escapes materialization destination")
    return target


def _copy_blob_exclusive(source: Path, destination: Path) -> None:
    created = False
    try:
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            created = True
            while chunk := input_stream.read(1024 * 1024):
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    except BaseException:
        if created and destination.is_file() and not destination.is_symlink():
            destination.unlink()
        raise


def _create_snapshot_link(entry: Mapping[str, Any], destination: Path) -> None:
    if entry["object_type"] == "junction":
        _create_junction(entry["link_target"], destination)
        return
    os.symlink(
        entry["link_target"],
        destination,
        target_is_directory=entry["link_kind"] == "directory",
    )


def _create_junction(raw_target: str, destination: Path) -> None:
    if os.name != "nt":
        raise OSError("junction materialization is supported only on Windows")
    try:
        import _winapi
    except ImportError as error:
        raise OSError(
            "this Python runtime cannot create Windows junctions"
        ) from error
    create_junction = getattr(_winapi, "CreateJunction", None)
    if create_junction is None:
        raise OSError("this Python runtime cannot create Windows junctions")
    create_junction(_junction_creation_target(raw_target), str(destination))


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


def _apply_mode(path: Path, mode: int | None) -> None:
    if mode is None:
        return
    try:
        path.chmod(mode)
    except OSError:
        if os.name != "nt":
            raise


def _remove_materialized_object(path: Path) -> None:
    if _is_junction(path) or path.is_symlink():
        path.unlink()
        return
    metadata = path.lstat()
    if stat.S_ISREG(metadata.st_mode):
        path.unlink()
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(f"unsupported materialized object: {path}")
    with os.scandir(path) as iterator:
        children = [Path(child.path) for child in iterator]
    for child in children:
        _remove_materialized_object(child)
    path.rmdir()


def _capture_surface(
    surface: SnapshotSurface,
    *,
    blob_root: Path | None,
) -> list[dict[str, Any]]:
    if not _lexists(surface.root):
        return []

    entries: list[dict[str, Any]] = []
    visited_directories: set[tuple[int, int]] = set()

    def visit(path: Path, relative_path: str) -> None:
        # The surface root itself is never excluded; only what lies under it.
        if relative_path and matches_exclude(relative_path, surface.excludes):
            return
        object_type, object_stat = _inspect_object(path)
        entry: dict[str, Any] = {
            "surface": surface.logical_path,
            "relative_path": relative_path,
            "logical_path": _logical_target(
                surface.logical_path,
                relative_path,
            ),
            "target_path": str(_target_path(surface.root, relative_path)),
            "object_type": object_type,
            "hash": "",
            "blob_sha256": None,
            "link_target": None,
            "link_kind": None,
            "mode": _meaningful_mode(object_stat, object_type),
            "empty": None,
        }
        entries.append(entry)
        if object_type == "file":
            content_hash = (
                _store_blob(path, blob_root)
                if blob_root is not None
                else _hash_stable_file(path, object_stat)
            )
            entry["hash"] = content_hash
            entry["blob_sha256"] = content_hash
            return
        if object_type in {"symlink", "junction"}:
            target = os.readlink(path)
            entry["link_target"] = os.fsdecode(target)
            entry["link_kind"] = _link_kind(object_stat, object_type)
            marker = b"L\0" if object_type == "symlink" else b"J\0"
            entry["hash"] = sha256_bytes(marker + os.fsencode(target))
            return

        identity = (object_stat.st_dev, object_stat.st_ino)
        if identity in visited_directories:
            raise OSError(f"directory cycle or alias detected at {path}")
        visited_directories.add(identity)
        try:
            with os.scandir(path) as iterator:
                children = sorted(
                    iterator,
                    key=lambda child: os.fsencode(child.name),
                )
            entry["empty"] = not children
            for child in children:
                child_relative = (
                    child.name
                    if relative_path == "."
                    else f"{relative_path}/{child.name}"
                )
                visit(Path(child.path), PurePosixPath(child_relative).as_posix())
            after = path.lstat()
            if not _same_file_state(object_stat, after):
                raise OSError(f"directory changed while snapshotting: {path}")
        finally:
            visited_directories.remove(identity)

    visit(surface.root, ".")
    _populate_directory_hashes(entries)
    return entries


def _populate_directory_hashes(entries: list[dict[str, Any]]) -> None:
    by_path = {entry["relative_path"]: entry for entry in entries}
    directories = sorted(
        (
            entry
            for entry in entries
            if entry["object_type"] == "directory"
        ),
        key=lambda item: (
            0
            if item["relative_path"] == "."
            else len(PurePosixPath(item["relative_path"]).parts)
        ),
        reverse=True,
    )
    for directory in directories:
        prefix = (
            ""
            if directory["relative_path"] == "."
            else f"{directory['relative_path']}/"
        )
        digest = sha256()
        for relative_path, child in sorted(by_path.items()):
            if relative_path == directory["relative_path"]:
                continue
            if prefix and not relative_path.startswith(prefix):
                continue
            remainder = relative_path[len(prefix) :] if prefix else relative_path
            if "/" in remainder:
                continue
            digest.update(child["object_type"].encode("ascii"))
            digest.update(b"\0")
            digest.update(os.fsencode(remainder))
            digest.update(b"\0")
            digest.update(child["hash"].encode("ascii"))
            digest.update(b"\0")
        directory["hash"] = digest.hexdigest()


def _normalize_surfaces(
    surfaces: Mapping[str, Path] | Iterable[SnapshotSurface],
) -> tuple[SnapshotSurface, ...]:
    values = (
        (
            SnapshotSurface(logical_path=logical_path, root=Path(root))
            for logical_path, root in surfaces.items()
        )
        if isinstance(surfaces, Mapping)
        else surfaces
    )
    normalized: list[SnapshotSurface] = []
    logical_paths: set[str] = set()
    roots: set[str] = set()
    for value in values:
        if not isinstance(value, SnapshotSurface):
            raise TypeError("surfaces must contain SnapshotSurface values")
        logical_path = _validate_logical_path(value.logical_path)
        root = Path(
            os.path.abspath(os.fspath(Path(value.root).expanduser()))
        )
        root_identity = os.path.normcase(os.path.normpath(str(root)))
        if logical_path in logical_paths:
            raise ValueError(f"duplicate logical surface: {logical_path}")
        if root_identity in roots:
            raise ValueError(f"duplicate surface root: {root}")
        logical_paths.add(logical_path)
        roots.add(root_identity)
        normalized.append(
            SnapshotSurface(logical_path, root, tuple(value.excludes))
        )
    return tuple(sorted(normalized, key=lambda item: item.logical_path))


def _inspect_object(path: Path) -> tuple[str, os.stat_result]:
    object_stat = path.lstat()
    if stat.S_ISLNK(object_stat.st_mode):
        return "symlink", object_stat
    if _is_junction(path):
        return "junction", object_stat
    if stat.S_ISREG(object_stat.st_mode):
        return "file", object_stat
    if stat.S_ISDIR(object_stat.st_mode):
        return "directory", object_stat
    raise OSError(f"unsupported filesystem object: {path}")


def _is_junction(path: Path) -> bool:
    path_method = getattr(path, "is_junction", None)
    if path_method is not None:
        try:
            return bool(path_method())
        except OSError:
            return False
    os_method = getattr(os.path, "isjunction", None)
    if os_method is not None:
        try:
            return bool(os_method(path))
        except OSError:
            return False
    if os.name != "nt":
        return False
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _store_blob(source: Path, blob_root: Path) -> str:
    temporary_root = blob_root / ".tmp"
    ensure_private_directory(temporary_root)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".blob.",
        dir=temporary_root,
    )
    temporary = Path(temporary_name)
    digest = sha256()
    before = source.lstat()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(source, flags)
        with os.fdopen(source_descriptor, "rb") as input_stream:
            opened = os.fstat(input_stream.fileno())
            if not _same_file_state(before, opened):
                raise OSError(f"file changed while snapshotting: {source}")
            with os.fdopen(descriptor, "wb") as output_stream:
                while chunk := input_stream.read(1024 * 1024):
                    digest.update(chunk)
                    output_stream.write(chunk)
                output_stream.flush()
                os.fsync(output_stream.fileno())
        after = source.lstat()
        if not _same_file_state(before, after):
            raise OSError(f"file changed while snapshotting: {source}")

        content_hash = digest.hexdigest()
        destination_root = blob_root / content_hash[:2]
        ensure_private_directory(destination_root)
        destination = destination_root / content_hash[2:]
        if destination.exists():
            if not destination.is_file() or destination.is_symlink():
                raise ValueError(
                    f"existing snapshot blob is not a regular file: {content_hash}"
                )
            if _hash_file(destination) != content_hash:
                raise ValueError(
                    f"existing snapshot blob is corrupt: {content_hash}"
                )
            temporary.unlink()
        else:
            os.replace(temporary, destination)
            if os.name == "posix":
                destination.chmod(0o600)
        return content_hash
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _hash_stable_file(path: Path, before: os.stat_result) -> str:
    digest = sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not _same_file_state(before, opened):
            raise OSError(f"file changed while diffing snapshot: {path}")
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    after = path.lstat()
    if not _same_file_state(before, after):
        raise OSError(f"file changed while diffing snapshot: {path}")
    return digest.hexdigest()


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
    )


def _validate_manifest(
    value: Any,
    *,
    state_home: Path,
    expected_snapshot_id: str,
    verify_blobs: bool,
) -> None:
    if not isinstance(value, dict):
        raise ValueError("snapshot manifest must be a JSON object")  # noqa: TRY004
    expected_fields = {
        "snapshot_id",
        "schema_version",
        "created_at",
        "campaign_id",
        "label",
        "acl_restoration",
        "surfaces",
        "entries",
    }
    if set(value) != expected_fields:
        raise ValueError("snapshot manifest fields do not match its schema")
    if value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("unsupported snapshot schema_version")
    if value.get("snapshot_id") != expected_snapshot_id:
        raise ValueError("snapshot id does not match its filename")
    body = dict(value)
    body.pop("snapshot_id", None)
    if sha256_json(body) != expected_snapshot_id:
        raise ValueError("snapshot id does not match manifest content")
    _parse_timestamp(value.get("created_at"))
    if value.get("campaign_id") is not None:
        _validate_component(value["campaign_id"], "campaign_id")
    if value.get("label") is not None and (
        not isinstance(value["label"], str) or not value["label"].strip()
    ):
        raise ValueError("snapshot label must be a non-empty string")
    if value.get("acl_restoration") != "out_of_scope":
        raise ValueError("snapshot must declare ACL restoration out of scope")

    surfaces = value.get("surfaces")
    entries = value.get("entries")
    if not isinstance(surfaces, list) or not isinstance(entries, list):
        raise ValueError(  # noqa: TRY004
            "snapshot surfaces and entries must be arrays"
        )
    if any(not isinstance(surface, dict) for surface in surfaces):
        raise ValueError("snapshot surface must be an object")
    if surfaces != sorted(surfaces, key=lambda item: item.get("logical_path", "")):
        raise ValueError("snapshot surfaces are not deterministically sorted")

    surface_map: dict[str, dict[str, Any]] = {}
    surface_roots: set[str] = set()
    for surface in surfaces:
        if set(surface) != {"logical_path", "root", "present"}:
            raise ValueError("snapshot surface fields do not match its schema")
        logical_path = _validate_logical_path(surface.get("logical_path"))
        root = surface.get("root")
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise ValueError(f"surface {logical_path} root must be absolute")
        if not isinstance(surface.get("present"), bool):
            raise ValueError(  # noqa: TRY004
                f"surface {logical_path} present must be boolean"
            )
        if logical_path in surface_map:
            raise ValueError(f"duplicate snapshot surface: {logical_path}")
        root_identity = os.path.normcase(os.path.normpath(root))
        if root_identity in surface_roots:
            raise ValueError(f"duplicate snapshot surface root: {root}")
        surface_roots.add(root_identity)
        surface_map[logical_path] = surface

    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("snapshot entry must be an object")
    if entries != sorted(entries, key=_entry_sort_key):
        raise ValueError("snapshot entries are not deterministically sorted")
    entry_map: dict[tuple[str, str], dict[str, Any]] = {}
    logical_entries: dict[str, str] = {}
    for entry in entries:
        _validate_entry(entry, surface_map)
        key = (entry["surface"], entry["relative_path"])
        if key in entry_map:
            raise ValueError(f"duplicate snapshot entry: {entry['logical_path']}")
        entry_map[key] = entry
        prior_surface = logical_entries.get(entry["logical_path"])
        if prior_surface is not None:
            raise ValueError(
                f"logical snapshot path is ambiguous: {entry['logical_path']} "
                f"({prior_surface}, {entry['surface']})"
            )
        logical_entries[entry["logical_path"]] = entry["surface"]
        if entry["object_type"] == "file":
            resolve_blob_path(
                state_home,
                entry["blob_sha256"],
                verify=verify_blobs,
            )

    for logical_path, surface in surface_map.items():
        root_entry = entry_map.get((logical_path, "."))
        if surface["present"] != (root_entry is not None):
            raise ValueError(
                f"surface {logical_path} presence conflicts with its entries"
            )
        entry_surface = logical_entries.get(logical_path)
        if entry_surface is not None and entry_surface != logical_path:
            raise ValueError(
                f"declared surface collides with logical snapshot path: "
                f"{logical_path}"
            )
    _validate_directory_entries(entry_map)


def _validate_entry(
    entry: Any,
    surfaces: Mapping[str, Mapping[str, Any]],
) -> None:
    if not isinstance(entry, dict):
        raise ValueError("snapshot entry must be an object")  # noqa: TRY004
    expected_fields = {
        "surface",
        "relative_path",
        "logical_path",
        "target_path",
        "object_type",
        "hash",
        "blob_sha256",
        "link_target",
        "link_kind",
        "mode",
        "empty",
    }
    if set(entry) != expected_fields:
        raise ValueError("snapshot entry fields do not match its schema")
    surface_name = entry.get("surface")
    if surface_name not in surfaces:
        raise ValueError(f"entry references unknown surface: {surface_name}")
    relative_path = entry.get("relative_path")
    _validate_relative_path(relative_path)
    expected_logical = _logical_target(surface_name, relative_path)
    if entry.get("logical_path") != expected_logical:
        raise ValueError(f"entry has invalid logical path: {entry.get('logical_path')}")
    expected_target = str(
        _target_path(Path(surfaces[surface_name]["root"]), relative_path)
    )
    if entry.get("target_path") != expected_target:
        raise ValueError(f"entry has invalid target path: {expected_logical}")

    object_type = entry.get("object_type")
    if object_type not in _OBJECT_TYPES:
        raise ValueError(f"unsupported snapshot object type: {object_type}")
    _validate_sha256(entry.get("hash"), f"{expected_logical} hash")
    mode = entry.get("mode")
    if mode is not None and (
        isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777
    ):
        raise ValueError(f"entry has invalid mode: {expected_logical}")

    blob_hash = entry.get("blob_sha256")
    link_target = entry.get("link_target")
    link_kind = entry.get("link_kind")
    empty = entry.get("empty")
    if object_type == "file":
        if (
            blob_hash != entry["hash"]
            or link_target is not None
            or link_kind is not None
            or empty is not None
        ):
            raise ValueError(f"file entry fields conflict: {expected_logical}")
    elif object_type == "directory":
        if (
            blob_hash is not None
            or link_target is not None
            or link_kind is not None
            or not isinstance(empty, bool)
        ):
            raise ValueError(f"directory entry fields conflict: {expected_logical}")
    else:
        if (
            blob_hash is not None
            or not isinstance(link_target, str)
            or empty is not None
            or mode is not None
            or link_kind not in {None, "file", "directory"}
        ):
            raise ValueError(f"link entry fields conflict: {expected_logical}")
        if object_type == "junction" and link_kind != "directory":
            raise ValueError(f"junction must be directory-like: {expected_logical}")
        marker = b"L\0" if object_type == "symlink" else b"J\0"
        if sha256_bytes(marker + os.fsencode(link_target)) != entry["hash"]:
            raise ValueError(f"link hash mismatch: {expected_logical}")


def _validate_directory_entries(
    entries: Mapping[tuple[str, str], Mapping[str, Any]],
) -> None:
    by_surface: dict[str, list[dict[str, Any]]] = {}
    for entry in entries.values():
        by_surface.setdefault(entry["surface"], []).append(dict(entry))
    for surface_entries in by_surface.values():
        for entry in surface_entries:
            if entry["relative_path"] == ".":
                continue
            parent = str(PurePosixPath(entry["relative_path"]).parent)
            parent_entry = next(
                (
                    candidate
                    for candidate in surface_entries
                    if candidate["relative_path"] == parent
                ),
                None,
            )
            if parent_entry is None or parent_entry["object_type"] != "directory":
                raise ValueError(
                    f"entry parent is not a directory: {entry['logical_path']}"
                )
        copied = [dict(entry) for entry in surface_entries]
        expected_hashes = {
            entry["relative_path"]: entry["hash"]
            for entry in copied
            if entry["object_type"] == "directory"
        }
        _populate_directory_hashes(copied)
        for entry in copied:
            if (
                entry["object_type"] == "directory"
                and entry["hash"] != expected_hashes[entry["relative_path"]]
            ):
                raise ValueError(
                    f"directory hash mismatch: {entry['logical_path']}"
                )
            if entry["object_type"] == "directory":
                has_child = any(
                    _is_immediate_child(
                        entry["relative_path"],
                        candidate["relative_path"],
                    )
                    for candidate in surface_entries
                )
                if entry["empty"] == has_child:
                    raise ValueError(
                        f"directory empty flag mismatch: {entry['logical_path']}"
                    )


def _garbage_collect_blobs(state_home: Path) -> None:
    referenced = {
        entry["blob_sha256"]
        for manifest in list_snapshots(state_home, verify_blobs=False)
        for entry in manifest["entries"]
        if entry["blob_sha256"] is not None
    }
    blob_root = _snapshot_root(state_home) / "blobs"
    if not blob_root.exists():
        return
    for shard in sorted(blob_root.iterdir(), key=lambda item: item.name):
        if not shard.is_dir() or shard.name == ".tmp":
            continue
        for blob in sorted(shard.iterdir(), key=lambda item: item.name):
            blob_hash = f"{shard.name}{blob.name}"
            if _SHA256_PATTERN.fullmatch(blob_hash) and blob_hash not in referenced:
                blob.unlink()
        try:
            shard.rmdir()
        except OSError:
            pass


def _diff_view(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "object_type": entry["object_type"],
        "hash": entry["hash"],
        "link_target": entry.get("link_target"),
        "link_kind": entry.get("link_kind"),
        "mode": entry.get("mode"),
    }


def _meaningful_mode(
    object_stat: os.stat_result,
    object_type: str,
) -> int | None:
    if os.name != "posix" or object_type in {"symlink", "junction"}:
        return None
    return stat.S_IMODE(object_stat.st_mode)


def _link_kind(
    object_stat: os.stat_result,
    object_type: str,
) -> str | None:
    if object_type == "junction":
        return "directory"
    if os.name != "nt":
        return None
    attributes = getattr(object_stat, "st_file_attributes", 0)
    directory_attribute = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0)
    return "directory" if attributes & directory_attribute else "file"


def _logical_target(logical_path: str, relative_path: str) -> str:
    if relative_path == ".":
        return logical_path
    return str(PurePosixPath(logical_path) / PurePosixPath(relative_path))


def _target_path(root: Path, relative_path: str) -> Path:
    if relative_path == ".":
        return root
    return root.joinpath(*PurePosixPath(relative_path).parts)


def _validate_logical_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("logical_path must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or value in {".", ".."} or ".." in path.parts:
        raise ValueError("logical_path must be a safe relative path")
    normalized = str(path)
    if normalized != value.replace("\\", "/"):
        raise ValueError("logical_path must be normalized")
    return normalized


def _validate_relative_path(value: Any) -> str:
    if value == ".":
        return value
    if not isinstance(value, str) or not value:
        raise ValueError("entry relative_path must be non-empty")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError("entry relative_path must be safe and normalized")
    return value


def _validate_component(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError(f"{field} must be one safe path component")
    return value


def _validate_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _entry_sort_key(entry: Mapping[str, Any]) -> tuple[str, str]:
    return (str(entry.get("surface", "")), str(entry.get("relative_path", "")))


def _entry_depth(relative_path: str) -> int:
    return (
        0
        if relative_path == "."
        else len(PurePosixPath(relative_path).parts)
    )


def _is_immediate_child(parent: str, candidate: str) -> bool:
    if candidate == parent:
        return False
    prefix = "" if parent == "." else f"{parent}/"
    if prefix and not candidate.startswith(prefix):
        return False
    remainder = candidate[len(prefix) :] if prefix else candidate
    return "/" not in remainder


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("snapshot created_at is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))  # noqa: FURB162
    except ValueError as error:
        raise ValueError("snapshot created_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("snapshot created_at must include a timezone")
    return parsed


def _snapshot_root(state_home: Path) -> Path:
    return Path(state_home) / "snapshots"


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")

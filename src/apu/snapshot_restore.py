from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .restore_journal import (
    RestoreItem,
    RestorePreflightError,
    RestoreResult,
    hash_restore_object,
    restore_items,
)
from .snapshots import load_snapshot, materialize_snapshot_object
from .state import ensure_private_directory


def restore_snapshot(
    state_home: Path,
    snapshot_id: str,
    *,
    paths: Iterable[Path] = (),
    force_paths: Iterable[Path] = (),
) -> RestoreResult:
    """Restore a snapshot's declared roots or exact selected manifest paths."""

    root = Path(state_home).expanduser().resolve()
    manifest = load_snapshot(root, snapshot_id)
    selections = _select_entries(manifest, tuple(paths))
    forced = {_absolute_path(path, "--force-path") for path in force_paths}
    selected_targets = {target for target, _logical_path in selections}
    unknown_forced = forced - selected_targets
    if unknown_forced:
        raise RestorePreflightError(
            "--force-path must name an exact selected target: "
            + ", ".join(sorted(str(path) for path in unknown_forced))
        )

    transaction_root = root / "transactions"
    ensure_private_directory(transaction_root)
    with tempfile.TemporaryDirectory(
        prefix=f"snapshot-{snapshot_id[:12]}.", dir=transaction_root
    ) as temporary_name:
        temporary = Path(temporary_name)
        if os.name == "posix":
            temporary.chmod(0o700)
        items: list[RestoreItem] = []
        for index, (target, logical_path) in enumerate(selections):
            replacement = materialize_snapshot_object(
                root,
                manifest,
                logical_path,
                temporary / f"{index:04d}",
            )
            current_type, current_hash = _describe_current(target)
            items.append(
                RestoreItem(
                    target=target,
                    replacement=replacement,
                    expected_type=current_type,
                    expected_sha256=current_hash,
                )
            )
        return restore_items(
            items,
            root / "restore-journals",
            force_paths=forced,
            snapshot_id=snapshot_id,
        )


def _select_entries(
    manifest: Mapping[str, Any],
    requested: tuple[Path, ...],
) -> tuple[tuple[Path, str], ...]:
    available: dict[Path, str] = {}
    for surface in manifest["surfaces"]:
        available[_absolute_path(Path(surface["root"]), "snapshot surface")] = surface[
            "logical_path"
        ]
    for entry in manifest["entries"]:
        available[_absolute_path(Path(entry["target_path"]), "snapshot entry")] = entry[
            "logical_path"
        ]

    if requested:
        selected: list[tuple[Path, str]] = []
        for raw in requested:
            target = _absolute_path(raw, "--path")
            logical_path = available.get(target)
            if logical_path is None:
                raise RestorePreflightError(
                    f"restore path is not an exact snapshot target: {target}"
                )
            selected.append((target, logical_path))
    else:
        selected = [
            (
                _absolute_path(Path(surface["root"]), "snapshot surface"),
                surface["logical_path"],
            )
            for surface in manifest["surfaces"]
        ]

    deduplicated = {
        _path_identity(target): (target, logical_path)
        for target, logical_path in selected
    }
    if len(deduplicated) != len(selected):
        raise RestorePreflightError("restore paths must not contain duplicates")
    ordered = tuple(
        sorted(
            deduplicated.values(),
            key=lambda item: (_path_identity(item[0]), item[1]),
        )
    )
    for index, (left, _logical) in enumerate(ordered):
        for right, _other_logical in ordered[index + 1 :]:
            if _overlaps(left, right):
                raise RestorePreflightError(
                    f"restore targets must not overlap: {left} and {right}"
                )
    return ordered


def _describe_current(path: Path) -> tuple[str, str | None]:
    if not os.path.lexists(path):
        return "absent", None
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        object_type = "symlink"
    elif bool(getattr(os.path, "isjunction", lambda _path: False)(path)):
        object_type = "junction"
    elif stat.S_ISREG(metadata.st_mode):
        object_type = "file"
    elif stat.S_ISDIR(metadata.st_mode):
        object_type = "directory"
    else:
        raise RestorePreflightError(f"unsupported restore target: {path}")
    return object_type, hash_restore_object(path)


def _absolute_path(path: Path, field: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise RestorePreflightError(f"{field} must be absolute")
    return Path(os.path.abspath(os.fspath(candidate)))


def _path_identity(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _overlaps(left: Path, right: Path) -> bool:
    left_id = _path_identity(left)
    right_id = _path_identity(right)
    try:
        common = os.path.commonpath((left_id, right_id))
    except ValueError:
        return False
    return common in {left_id, right_id}

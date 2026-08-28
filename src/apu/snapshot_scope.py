from __future__ import annotations

import os
import stat
from pathlib import Path

from .snapshots import SnapshotSurface
from .system_audit import SystemInventory, audit_system
from .system_profile import SystemProfile


def snapshot_surfaces_for_profile(
    profile: SystemProfile,
    *,
    home: Path | None = None,
    generated_at: str | None = None,
) -> tuple[tuple[SnapshotSurface, ...], SystemInventory]:
    """Resolve the effective policy objects covered by a system profile."""

    inventory = audit_system(profile, home=home, generated_at=generated_at)
    candidates = [Path(surface.path) for surface in profile.global_surfaces]
    # Carry each surface's excludes through to the snapshot walk. Selection below
    # collapses candidates to a minimal covering set, so key by logical identity
    # rather than by position.
    excludes_by_identity = {
        _logical_identity(_absolute_logical(Path(surface.path))): surface.excludes
        for surface in profile.global_surfaces
        if surface.excludes
    }
    for repository in inventory.repositories:
        for surface in repository.inventory.surfaces:
            if surface.scope == "global":
                continue
            path = Path(surface.path)
            candidates.append(path.parent if surface.kind == "skill" else path)

    selected: list[Path] = []
    for candidate in sorted(
        {
            _logical_identity(path): _absolute_logical(path) for path in candidates
        }.values(),
        key=lambda path: (
            len(path.parts),
            os.path.normcase(os.fspath(path)),
            os.fspath(path),
        ),
    ):
        if any(_covers(parent, candidate) for parent in selected):
            continue
        selected.append(candidate)

    surfaces = tuple(
        SnapshotSurface(
            logical_path=f"surface-{index:04d}",
            root=path,
            excludes=excludes_by_identity.get(_logical_identity(path), ()),
        )
        for index, path in enumerate(
            sorted(
                selected,
                key=lambda item: (
                    os.path.normcase(os.fspath(item)),
                    os.fspath(item),
                ),
            ),
            start=1,
        )
    )
    return surfaces, inventory


def _absolute_logical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _logical_identity(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(_absolute_logical(path))))


def _covers(parent: Path, candidate: Path) -> bool:
    if _logical_identity(parent) == _logical_identity(candidate):
        return True
    try:
        metadata = parent.lstat()
    except OSError:
        return False
    is_link = stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(os.path, "isjunction", lambda _path: False)(parent)
    )
    if is_link or not stat.S_ISDIR(metadata.st_mode):
        return False
    try:
        return os.path.commonpath(
            (_logical_identity(parent), _logical_identity(candidate))
        ) == _logical_identity(parent)
    except ValueError:
        return False

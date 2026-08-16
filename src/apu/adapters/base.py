from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from apu.models import (
    InstructionSurface,
    SurfaceRelationship,
    sha256_bytes,
    sha256_json,
)


@dataclass(frozen=True)
class DiscoveryResult:
    """Read-only output shared by provider adapters and the audit layer."""

    surfaces: tuple[InstructionSurface, ...] = ()
    relationships: tuple[SurfaceRelationship, ...] = ()
    effective_stacks: tuple[dict[str, object], ...] = ()


PathFilter = Callable[[Path], bool]


class ProviderAdapter(Protocol):
    name: str

    def discover(
        self,
        roots: Iterable[Path],
        *,
        home: Path,
        path_filter: PathFilter | None = None,
    ) -> DiscoveryResult:
        """Discover provider surfaces without mutating the filesystem."""


_CREDENTIAL_PATTERN = re.compile(
    rb"(?:sk-[A-Za-z0-9_-]{20,}|api[_-]?key\s*[:=]|"
    rb"access[_-]?token\s*[:=]|password\s*[:=])",
    re.IGNORECASE,
)

_IGNORED_DISCOVERY_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".next",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tmp-runs",
        ".tox",
        ".venv",
        ".worktrees",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "site-packages",
        "target",
    }
)


def absolute_logical_path(path: Path) -> Path:
    """Return an absolute path without erasing the final symlink identity."""

    return Path(os.path.abspath(os.path.expanduser(str(path))))


def read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except (OSError, PermissionError):
        return None


def make_surface(
    path: Path,
    *,
    kind: str,
    provider: str,
    authority: str,
    scope: str,
    precedence: int,
    path_filter: PathFilter | None = None,
) -> InstructionSurface | None:
    logical = absolute_logical_path(path)
    if path_filter is not None and not path_filter(logical):
        return None
    content = read_bytes(logical)
    if content is None:
        return None
    try:
        resolved = logical.resolve(strict=True)
    except OSError:
        resolved = logical.resolve(strict=False)
    mode = None
    if os.name != "nt":
        try:
            mode = f"{stat.S_IMODE(logical.stat().st_mode):04o}"
        except OSError:
            mode = None
    content_hash = sha256_bytes(content)
    identity = sha256_json(
        {
            "path": str(logical),
            "provider": provider,
            "kind": kind,
            "content_sha256": content_hash,
        }
    )
    return InstructionSurface(
        id=f"sha256:{identity}",
        path=str(logical),
        kind=kind,
        provider=provider,
        authority=authority,
        scope=scope,
        real_path=str(resolved),
        is_symlink=logical.is_symlink(),
        content_sha256=content_hash,
        mode=mode,
        precedence=precedence,
        sensitive=bool(_CREDENTIAL_PATTERN.search(content)),
    )


def deduplicate_surfaces(
    surfaces: Iterable[InstructionSurface],
) -> tuple[InstructionSurface, ...]:
    by_path: dict[tuple[str, str], InstructionSurface] = {}
    for surface in surfaces:
        by_path[(surface.provider, surface.path)] = surface
    return tuple(
        sorted(
            by_path.values(),
            key=lambda item: (item.provider, item.precedence, item.path),
        )
    )


def safe_rglob(
    root: Path,
    pattern: str,
    *,
    path_filter: PathFilter | None = None,
) -> Iterable[Path]:
    """Glob beneath a controlled root while excluding generated/cache trees."""

    if not root.is_dir() or (
        path_filter is not None and not path_filter(root)
    ):
        return ()
    paths: list[Path] = []
    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        onerror=lambda _error: None,
        followlinks=False,
    ):
        current_path = Path(current)
        relative = current_path.relative_to(root)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in _IGNORED_DISCOVERY_DIRECTORIES
            and (
                path_filter is None
                or path_filter(current_path / name)
            )
            and not (
                name == "cache"
                and ".claude" in relative.parts
                and "plugins" in relative.parts
            )
        )
        for name in (*directory_names, *sorted(file_names)):
            candidate = current_path / name
            if (
                (path_filter is None or path_filter(candidate))
                and candidate.relative_to(root).match(pattern)
            ):
                paths.append(candidate)
    return tuple(paths)


def skill_files(
    root: Path,
    *,
    path_filter: PathFilter | None = None,
) -> tuple[Path, ...]:
    """Discover regular and canonical one-level symlinked skill directories."""

    discovered = set(
        safe_rglob(root, "SKILL.md", path_filter=path_filter)
    )
    try:
        for child in root.iterdir():
            candidate = child / "SKILL.md"
            if (
                child.is_dir()
                and candidate.is_file()
                and (path_filter is None or path_filter(candidate))
            ):
                discovered.add(candidate)
    except OSError:
        pass
    return tuple(sorted(discovered, key=str))


def directory_depth(path: Path) -> int:
    return len(absolute_logical_path(path).parts)


def ancestor_directories(path: Path) -> tuple[Path, ...]:
    """Return filesystem ancestors from broadest to closest, including path."""

    logical = absolute_logical_path(path)
    directory = logical.parent if logical.is_file() else logical
    return tuple(reversed((directory, *directory.parents)))


def repository_bases(
    directories: Iterable[Path], *, home: Path
) -> tuple[Path, ...]:
    """Drop directories at or above a user home directory.

    Nothing at or above home is a repository, so a home-level `.claude` or
    `.agents` tree is a global surface. Without this boundary an ancestor walk
    from a repository stored beneath home re-discovers the user's own global
    configuration with repository authority.
    """

    boundaries = {absolute_logical_path(home)}
    try:
        boundaries.add(absolute_logical_path(Path.home()))
    except (OSError, RuntimeError):  # pragma: no cover - platform dependent
        pass
    excluded: set[Path] = set()
    for boundary in boundaries:
        excluded.add(boundary)
        excluded.update(boundary.parents)
    return tuple(
        directory
        for directory in directories
        if absolute_logical_path(directory) not in excluded
    )

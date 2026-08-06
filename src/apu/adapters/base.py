from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import stat
from typing import Iterable, Protocol

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


class ProviderAdapter(Protocol):
    name: str

    def discover(self, roots: Iterable[Path], *, home: Path) -> DiscoveryResult:
        """Discover provider surfaces without mutating the filesystem."""


_CREDENTIAL_PATTERN = re.compile(
    rb"(?:sk-[A-Za-z0-9_-]{20,}|api[_-]?key\s*[:=]|"
    rb"access[_-]?token\s*[:=]|password\s*[:=])",
    re.IGNORECASE,
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
) -> InstructionSurface | None:
    logical = absolute_logical_path(path)
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


def safe_rglob(root: Path, pattern: str) -> Iterable[Path]:
    """Glob beneath a controlled root while excluding generated/cache trees."""

    if not root.is_dir():
        return ()
    ignored_parts = {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        "node_modules",
    }
    paths: list[Path] = []
    try:
        candidates = root.rglob(pattern)
        for candidate in candidates:
            relative = candidate.relative_to(root)
            if any(part in ignored_parts for part in relative.parts):
                continue
            if (
                ".claude" in relative.parts
                and "plugins" in relative.parts
                and "cache" in relative.parts
            ):
                continue
            paths.append(candidate)
    except OSError:
        return ()
    return tuple(paths)


def skill_files(root: Path) -> tuple[Path, ...]:
    """Discover regular and canonical one-level symlinked skill directories."""

    discovered = set(safe_rglob(root, "SKILL.md"))
    try:
        for child in root.iterdir():
            candidate = child / "SKILL.md"
            if child.is_dir() and candidate.is_file():
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

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Iterable

from apu.adapters.base import DiscoveryResult, absolute_logical_path
from apu.adapters.claude import _frontmatter_paths
from apu.models import InstructionSurface, SurfaceRelationship


_INSTRUCTION_KINDS = {
    "codex": frozenset({"codex-instructions"}),
    "claude": frozenset(
        {
            "claude-instructions",
            "claude-local-instructions",
            "claude-rule",
        }
    ),
}


def effective_stack(
    cwd: Path | str,
    discovery: DiscoveryResult,
    provider: str,
) -> tuple[str, ...]:
    """Return active surface IDs from lowest to highest precedence."""

    working_directory = absolute_logical_path(Path(cwd))
    surfaces = {
        surface.id: surface
        for surface in discovery.surfaces
        if surface.provider == provider
    }
    candidates = [
        surface
        for surface in surfaces.values()
        if surface.kind in _INSTRUCTION_KINDS.get(provider, ())
        and _surface_applies(surface, working_directory)
    ]

    if provider == "claude":
        hook_sources = {
            relationship.from_surface_id
            for relationship in discovery.relationships
            if relationship.type == "session_start_hook"
            and relationship.status == "active"
        }
        candidates.extend(
            surface
            for identifier, surface in surfaces.items()
            if identifier in hook_sources
            and _surface_applies(surface, working_directory)
        )

    ordered = sorted(candidates, key=lambda item: _sort_key(item, provider))
    active_imports = _active_imports(discovery.relationships)
    output: list[str] = []
    emitted: set[str] = set()

    def emit(identifier: str) -> None:
        if identifier in emitted:
            return
        emitted.add(identifier)
        output.append(identifier)
        for target in active_imports.get(identifier, ()):
            if target in surfaces:
                emit(target)

    for surface in ordered:
        emit(surface.id)
    return tuple(output)


def build_effective_stacks(
    working_directories: Iterable[Path | str],
    discovery: DiscoveryResult,
    providers: Iterable[str] = ("codex", "claude"),
) -> tuple[dict[str, object], ...]:
    stacks: list[dict[str, object]] = []
    for cwd in working_directories:
        normalized = absolute_logical_path(Path(cwd))
        for provider in providers:
            stacks.append(
                {
                    "working_directory": str(normalized),
                    "provider": provider,
                    "surface_ids": list(
                        effective_stack(normalized, discovery, provider)
                    ),
                }
            )
    return tuple(stacks)


def _active_imports(
    relationships: Iterable[SurfaceRelationship],
) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for relationship in relationships:
        if (
            relationship.type == "imports"
            and relationship.status == "active"
            and relationship.to_surface_id is not None
        ):
            grouped.setdefault(relationship.from_surface_id, []).append(
                relationship.to_surface_id
            )
    return {
        source: tuple(dict.fromkeys(targets))
        for source, targets in grouped.items()
    }


def _surface_applies(
    surface: InstructionSurface, working_directory: Path
) -> bool:
    if surface.authority == "user":
        if surface.kind == "claude-rule":
            return _rule_matches(surface, working_directory, None)
        return True

    surface_path = absolute_logical_path(Path(surface.path))
    base = _surface_base(surface_path, surface.kind)
    try:
        working_directory.relative_to(base)
    except ValueError:
        return False
    if surface.kind == "claude-rule":
        return _rule_matches(surface, working_directory, base)
    return True


def _surface_base(path: Path, kind: str) -> Path:
    parts = path.parts
    if kind in {"claude-rule", "claude-settings"} and ".claude" in parts:
        index = parts.index(".claude")
        return Path(*parts[:index])
    return path.parent


def _rule_matches(
    surface: InstructionSurface,
    working_directory: Path,
    project_base: Path | None,
) -> bool:
    patterns = _frontmatter_paths(Path(surface.path))
    if not patterns:
        return True
    if project_base is not None:
        try:
            candidate = working_directory.relative_to(project_base).as_posix()
        except ValueError:
            return False
    else:
        candidate = working_directory.as_posix().lstrip("/")
    return any(_match_path(candidate, pattern) for pattern in patterns)


def _match_path(candidate: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/").lstrip("./")
    if fnmatch.fnmatchcase(candidate, normalized):
        return True
    if normalized.endswith("/**"):
        prefix = normalized[:-3].rstrip("/")
        return candidate == prefix or candidate.startswith(f"{prefix}/")
    return False


def _sort_key(
    surface: InstructionSurface, provider: str
) -> tuple[int, int, str]:
    path = Path(surface.path)
    depth = len(path.parent.parts)
    if provider == "claude":
        if surface.authority == "user":
            category = 1 if surface.kind == "claude-rule" else 0
        elif surface.kind == "claude-rule":
            category = 3
        elif surface.kind == "claude-settings":
            category = 4
        else:
            category = 2
        local_order = int(surface.kind == "claude-local-instructions")
        return (category, (depth * 2) + local_order, surface.path)
    category = 0 if surface.authority == "user" else 1
    return (category, depth, surface.path)

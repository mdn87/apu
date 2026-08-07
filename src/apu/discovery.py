from __future__ import annotations

from pathlib import Path
from typing import Iterable

from apu.adapters import ClaudeAdapter, CodexAdapter
from apu.adapters.base import (
    DiscoveryResult,
    ProviderAdapter,
    absolute_logical_path,
    deduplicate_surfaces,
)
from apu.precedence import build_effective_stacks


def discover(
    roots: Iterable[Path | str],
    *,
    home: Path | str,
    working_directories: Iterable[Path | str] = (),
    adapters: Iterable[ProviderAdapter] | None = None,
) -> DiscoveryResult:
    """Discover supported surfaces and compute provider-specific stacks."""

    normalized_roots = tuple(absolute_logical_path(Path(root)) for root in roots)
    normalized_home = absolute_logical_path(Path(home))
    selected_adapters = tuple(adapters or (CodexAdapter(), ClaudeAdapter()))

    surfaces = []
    relationships = []
    for adapter in selected_adapters:
        result = adapter.discover(normalized_roots, home=normalized_home)
        surfaces.extend(result.surfaces)
        relationships.extend(result.relationships)

    providers = tuple(adapter.name for adapter in selected_adapters)
    base = DiscoveryResult(
        surfaces=deduplicate_surfaces(surfaces),
        relationships=tuple(relationships),
    )
    directories = tuple(working_directories) or normalized_roots
    return DiscoveryResult(
        surfaces=base.surfaces,
        relationships=base.relationships,
        effective_stacks=build_effective_stacks(
            directories,
            base,
            providers=providers,
        ),
    )


discover_surfaces = discover

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Iterable

from apu.hooks_config import (
    plugin_resolution_relationship,
    structural_hook_relationships,
)
from apu.models import InstructionSurface, SurfaceRelationship

from .base import (
    DiscoveryResult,
    PathFilter,
    absolute_logical_path,
    ancestor_directories,
    deduplicate_surfaces,
    directory_depth,
    make_surface,
    repository_bases,
    safe_rglob,
    skill_files,
)


class CodexAdapter:
    name = "codex"

    def plan_skill_install(
        self,
        canonical_source: Path,
        *,
        home: Path,
        symlink_supported: bool = True,
    ):
        from apu.planning import build_skill_install_operations

        return build_skill_install_operations(
            package_skill=canonical_source,
            home=home,
            include_codex=True,
            include_claude=False,
            symlink_supported=symlink_supported,
        )

    def discover(
        self,
        roots: Iterable[Path],
        *,
        home: Path,
        path_filter: PathFilter | None = None,
    ) -> DiscoveryResult:
        normalized_roots = tuple(absolute_logical_path(root) for root in roots)
        normalized_home = absolute_logical_path(home)
        surfaces: list[InstructionSurface] = []
        relationships: list[SurfaceRelationship] = []

        global_file = normalized_home / ".codex" / "AGENTS.md"
        self._add(
            surfaces,
            global_file,
            kind="codex-instructions",
            authority="user",
            scope="global",
            precedence=10,
            path_filter=path_filter,
        )
        global_config = self._discover_hook_file(
            normalized_home / ".codex" / "config.toml",
            file_format="toml",
            kind="codex-config",
            authority="user",
            scope="global",
            source="config",
            surfaces=surfaces,
            relationships=relationships,
            path_filter=path_filter,
        )
        self._discover_hook_file(
            normalized_home / ".codex" / "hooks.json",
            file_format="json",
            kind="codex-hooks",
            authority="user",
            scope="global",
            source="hooks-file",
            surfaces=surfaces,
            relationships=relationships,
            path_filter=path_filter,
        )
        if global_config is not None:
            config_surface, config_value = global_config
            self._discover_enabled_plugins(
                normalized_home,
                config_surface=config_surface,
                config_value=config_value,
                surfaces=surfaces,
                relationships=relationships,
                path_filter=path_filter,
            )

        project_bases: set[Path] = set()
        for root in normalized_roots:
            if root.is_file():
                if root.name == "AGENTS.md":
                    self._add_repository_instruction(
                        surfaces, root, path_filter=path_filter
                    )
            else:
                for path in safe_rglob(
                    root,
                    "AGENTS.md",
                    path_filter=path_filter,
                ):
                    self._add_repository_instruction(
                        surfaces, path, path_filter=path_filter
                    )
                project_bases.add(root)
                for codex_dir in safe_rglob(
                    root,
                    ".codex",
                    path_filter=path_filter,
                ):
                    if codex_dir.is_dir():
                        project_bases.add(codex_dir.parent)
            project_bases.update(
                repository_bases(ancestor_directories(root), home=normalized_home)
            )

        for directory in sorted(project_bases, key=str):
            self._add_repository_instruction(
                surfaces,
                directory / "AGENTS.md",
                path_filter=path_filter,
            )
            self._discover_hook_file(
                directory / ".codex" / "config.toml",
                file_format="toml",
                kind="codex-config",
                authority="repository",
                scope="repository",
                source="config",
                surfaces=surfaces,
                relationships=relationships,
                path_filter=path_filter,
            )
            self._discover_hook_file(
                directory / ".codex" / "hooks.json",
                file_format="json",
                kind="codex-hooks",
                authority="repository",
                scope="repository",
                source="hooks-file",
                surfaces=surfaces,
                relationships=relationships,
                path_filter=path_filter,
            )

        skill_roots = [(normalized_home / ".agents" / "skills", "user", "global")]
        for root in normalized_roots:
            if root.is_dir():
                skill_roots.append(
                    (root / ".agents" / "skills", "repository", "repository")
                )
        for skill_root, authority, scope in skill_roots:
            if (
                authority == "repository"
                and skill_root == normalized_home / ".agents" / "skills"
            ):
                continue
            for skill_file in skill_files(
                skill_root,
                path_filter=path_filter,
            ):
                skill_surface = make_surface(
                    skill_file,
                    kind="skill",
                    provider=self.name,
                    authority=authority,
                    scope=scope,
                    precedence=80,
                    path_filter=path_filter,
                )
                if skill_surface is None:
                    continue
                surfaces.append(skill_surface)
                manifest_path = skill_file.parent / "agents" / "openai.yaml"
                manifest_surface = make_surface(
                    manifest_path,
                    kind="skill-manifest",
                    provider=self.name,
                    authority=authority,
                    scope=scope,
                    precedence=81,
                    path_filter=path_filter,
                )
                if manifest_surface is not None:
                    surfaces.append(manifest_surface)
                    relationships.append(
                        SurfaceRelationship(
                            type="manifest_for",
                            from_surface_id=manifest_surface.id,
                            to_surface_id=skill_surface.id,
                            status="active",
                        )
                    )

        return DiscoveryResult(
            surfaces=deduplicate_surfaces(surfaces),
            relationships=tuple(relationships),
        )

    def _discover_hook_file(
        self,
        path: Path,
        *,
        file_format: str,
        kind: str,
        authority: str,
        scope: str,
        source: str,
        surfaces: list[InstructionSurface],
        relationships: list[SurfaceRelationship],
        path_filter: PathFilter | None,
        plugin_identity: str | None = None,
        inspect_hooks: bool = True,
    ) -> tuple[InstructionSurface, object | None] | None:
        surface = make_surface(
            path,
            kind=kind,
            provider=self.name,
            authority=authority,
            scope=scope,
            precedence=5 if authority == "package" else 70,
            path_filter=path_filter,
        )
        if surface is None:
            return None
        surfaces.append(surface)
        value = _read_json(path) if file_format == "json" else _read_toml(path)
        if inspect_hooks:
            relationships.extend(
                structural_hook_relationships(
                    surface,
                    value,
                    status="trust-unknown",
                    source=source,
                    scope=scope,
                    plugin_identity=plugin_identity,
                )
            )
        return surface, value

    def _discover_enabled_plugins(
        self,
        home: Path,
        *,
        config_surface: InstructionSurface,
        config_value: object | None,
        surfaces: list[InstructionSurface],
        relationships: list[SurfaceRelationship],
        path_filter: PathFilter | None,
    ) -> None:
        if not isinstance(config_value, dict):
            return
        plugins = config_value.get("plugins")
        if not isinstance(plugins, dict):
            return
        for identifier in sorted(
            str(key)
            for key, value in plugins.items()
            if value is True
            or (isinstance(value, dict) and value.get("enabled") is not False)
        ):
            plugin, separator, marketplace = identifier.partition("@")
            if not separator or not plugin or not marketplace:
                relationships.append(
                    plugin_resolution_relationship(
                        config_surface,
                        identifier,
                        status="invalid",
                        provider=self.name,
                    )
                )
                continue
            cache_root = home / ".codex" / "plugins" / "cache" / marketplace / plugin
            candidates = tuple(
                candidate for candidate in cache_root.glob("*") if candidate.is_dir()
            )
            if len(candidates) != 1:
                relationships.append(
                    plugin_resolution_relationship(
                        config_surface,
                        identifier,
                        status="ambiguous" if candidates else "invalid",
                        provider=self.name,
                    )
                )
                continue
            root = absolute_logical_path(candidates[0])
            manifest_path = root / ".codex-plugin" / "plugin.json"
            manifest = self._discover_hook_file(
                manifest_path,
                file_format="json",
                kind="codex-plugin-manifest",
                authority="package",
                scope="global",
                source="plugin",
                surfaces=surfaces,
                relationships=relationships,
                path_filter=path_filter,
                plugin_identity=identifier,
                inspect_hooks=False,
            )
            if manifest is None:
                relationships.append(
                    plugin_resolution_relationship(
                        config_surface,
                        identifier,
                        status="invalid",
                        provider=self.name,
                    )
                )
                continue
            _, manifest_value = manifest
            declared = (
                manifest_value.get("hooks")
                if isinstance(manifest_value, dict)
                else None
            )
            if isinstance(declared, dict):
                relationships.extend(
                    structural_hook_relationships(
                        manifest[0],
                        manifest_value,
                        status="trust-unknown",
                        source="plugin",
                        scope="global",
                        plugin_identity=identifier,
                    )
                )
                continue
            if isinstance(declared, str):
                hook_paths = (declared,)
            elif isinstance(declared, list) and all(
                isinstance(value, str) for value in declared
            ):
                hook_paths = tuple(declared)
            elif declared is None:
                hook_paths = ("./hooks/hooks.json",)
            else:
                relationships.append(
                    plugin_resolution_relationship(
                        config_surface,
                        identifier,
                        status="invalid",
                        provider=self.name,
                    )
                )
                continue
            resolved_root = root.resolve(strict=False)
            for raw_path in hook_paths:
                hook_path = (root / raw_path).resolve(strict=False)
                if (
                    hook_path != resolved_root
                    and resolved_root not in hook_path.parents
                ):
                    relationships.append(
                        plugin_resolution_relationship(
                            config_surface,
                            identifier,
                            status="invalid",
                            provider=self.name,
                        )
                    )
                    continue
                self._discover_hook_file(
                    absolute_logical_path(hook_path),
                    file_format="json",
                    kind="codex-plugin-hooks",
                    authority="package",
                    scope="global",
                    source="plugin",
                    surfaces=surfaces,
                    relationships=relationships,
                    path_filter=path_filter,
                    plugin_identity=identifier,
                )

    def _add_repository_instruction(
        self,
        surfaces: list[InstructionSurface],
        path: Path,
        *,
        path_filter: PathFilter | None,
    ) -> None:
        self._add(
            surfaces,
            path,
            kind="codex-instructions",
            authority="repository",
            scope="hierarchical",
            precedence=20 + directory_depth(path.parent),
            path_filter=path_filter,
        )

    def _add(
        self,
        surfaces: list[InstructionSurface],
        path: Path,
        *,
        kind: str,
        authority: str,
        scope: str,
        precedence: int,
        path_filter: PathFilter | None,
    ) -> None:
        surface = make_surface(
            path,
            kind=kind,
            provider=self.name,
            authority=authority,
            scope=scope,
            precedence=precedence,
            path_filter=path_filter,
        )
        if surface is not None:
            surfaces.append(surface)


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _read_toml(path: Path) -> object | None:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None

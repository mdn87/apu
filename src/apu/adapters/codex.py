from __future__ import annotations

from pathlib import Path
from typing import Iterable

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
            for directory in repository_bases(
                ancestor_directories(root), home=normalized_home
            ):
                self._add_repository_instruction(
                    surfaces,
                    directory / "AGENTS.md",
                    path_filter=path_filter,
                )

        skill_roots = [
            (normalized_home / ".agents" / "skills", "user", "global")
        ]
        for root in normalized_roots:
            if root.is_dir():
                skill_roots.append(
                    (root / ".agents" / "skills", "repository", "repository")
                )
        for skill_root, authority, scope in skill_roots:
            if (
                authority == "repository"
                and skill_root
                == normalized_home / ".agents" / "skills"
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

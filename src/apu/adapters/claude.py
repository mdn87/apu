from __future__ import annotations

import ast
import json
import re
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

_IMPORT_PATTERN = re.compile(r"(?<![\w@])@([^\s`]+)")


class ClaudeAdapter:
    name = "claude"
    import_depth_limit = 5

    def plan_skill_install(
        self,
        canonical_source: Path,
        *,
        home: Path,
        symlink_supported: bool = True,
        marketplace_rendered: Path | None = None,
    ):
        from apu.planning import build_skill_install_operations

        return build_skill_install_operations(
            package_skill=canonical_source,
            home=home,
            include_codex=False,
            include_claude=True,
            symlink_supported=symlink_supported,
            claude_marketplace_rendered=marketplace_rendered,
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

        for filename, kind, precedence in (("CLAUDE.md", "claude-instructions", 10),):
            path = normalized_home / ".claude" / filename
            surface = self._surface(
                path,
                kind=kind,
                authority="user",
                scope="global",
                precedence=precedence,
                path_filter=path_filter,
            )
            if surface is not None:
                surfaces.append(surface)

        self._discover_rules(
            normalized_home / ".claude" / "rules",
            authority="user",
            scope="global",
            surfaces=surfaces,
            relationships=relationships,
            path_filter=path_filter,
        )
        self._discover_skills(
            (
                normalized_home / ".claude" / "skills",
                normalized_home / ".agents" / "skills",
            ),
            authority="user",
            scope="global",
            surfaces=surfaces,
            path_filter=path_filter,
        )
        self._discover_settings(
            normalized_home / ".claude" / "settings.json",
            authority="user",
            scope="global",
            surfaces=surfaces,
            relationships=relationships,
            path_filter=path_filter,
        )
        self._discover_marketplace_file(
            normalized_home / ".claude" / "plugins" / "known_marketplaces.json",
            authority="user",
            scope="global",
            surfaces=surfaces,
            relationships=relationships,
            path_filter=path_filter,
        )
        self._discover_plugins(
            normalized_home,
            surfaces=surfaces,
            relationships=relationships,
            path_filter=path_filter,
        )

        project_bases: set[Path] = set()
        sidecars: list[InstructionSurface] = []
        for root in normalized_roots:
            if root.is_file():
                if root.name in {"CLAUDE.md", "CLAUDE.local.md"}:
                    surface = self._repository_instruction(
                        root, path_filter=path_filter
                    )
                    if surface is not None:
                        surfaces.append(surface)
            else:
                for filename in ("CLAUDE.md", "CLAUDE.local.md"):
                    for path in safe_rglob(
                        root,
                        filename,
                        path_filter=path_filter,
                    ):
                        surface = self._repository_instruction(
                            path, path_filter=path_filter
                        )
                        if surface is not None:
                            surfaces.append(surface)
                project_bases.add(root)
                for claude_dir in safe_rglob(
                    root,
                    ".claude",
                    path_filter=path_filter,
                ):
                    if claude_dir.is_dir():
                        project_bases.add(claude_dir.parent)
            project_bases.update(ancestor_directories(root))

            if root.is_dir():
                for path in safe_rglob(
                    root,
                    "*.md",
                    path_filter=path_filter,
                ):
                    if self._is_apu_sidecar(path):
                        sidecar = self._surface(
                            path,
                            kind="claude-import",
                            authority="repository",
                            scope="repository",
                            precedence=25 + directory_depth(path.parent),
                            path_filter=path_filter,
                        )
                        if sidecar is not None:
                            surfaces.append(sidecar)
                            sidecars.append(sidecar)

        for base in repository_bases(
            sorted(project_bases, key=str), home=normalized_home
        ):
            for filename in ("CLAUDE.md", "CLAUDE.local.md"):
                surface = self._repository_instruction(
                    base / filename, path_filter=path_filter
                )
                if surface is not None:
                    surfaces.append(surface)
            self._discover_rules(
                base / ".claude" / "rules",
                authority="repository",
                scope="repository",
                surfaces=surfaces,
                relationships=relationships,
                path_filter=path_filter,
            )
            self._discover_skills(
                (base / ".claude" / "skills", base / ".agents" / "skills"),
                authority="repository",
                scope="repository",
                surfaces=surfaces,
                path_filter=path_filter,
            )
            for filename in ("settings.json", "settings.local.json"):
                self._discover_settings(
                    base / ".claude" / filename,
                    authority="repository",
                    scope="repository",
                    surfaces=surfaces,
                    relationships=relationships,
                    path_filter=path_filter,
                )
            self._discover_marketplace_file(
                base / ".claude-plugin" / "marketplace.json",
                authority="repository",
                scope="repository",
                surfaces=surfaces,
                relationships=relationships,
                path_filter=path_filter,
            )

        discovered = deduplicate_surfaces(surfaces)
        surface_by_path = {surface.path: surface for surface in discovered}
        instruction_roots = [
            Path(surface.path)
            for surface in discovered
            if surface.kind
            in {
                "claude-instructions",
                "claude-local-instructions",
                "claude-rule",
            }
        ]
        for instruction_path in instruction_roots:
            source_surface = surface_by_path[str(instruction_path)]
            self._discover_imports(
                instruction_path,
                authority=source_surface.authority,
                scope=source_surface.scope,
                depth=0,
                ancestry=(),
                surfaces=surfaces,
                surface_by_path=surface_by_path,
                relationships=relationships,
                path_filter=path_filter,
            )

        active_targets = {
            relationship.to_surface_id
            for relationship in relationships
            if relationship.type == "imports"
            and relationship.status == "active"
            and relationship.to_surface_id is not None
        }
        for sidecar in sidecars:
            if sidecar.id not in active_targets:
                relationships.append(
                    SurfaceRelationship(
                        type="sidecar",
                        from_surface_id=sidecar.id,
                        to_surface_id=None,
                        status="orphaned",
                    )
                )

        return DiscoveryResult(
            surfaces=deduplicate_surfaces(surfaces),
            relationships=tuple(relationships),
        )

    def _repository_instruction(
        self,
        path: Path,
        *,
        path_filter: PathFilter | None,
    ) -> InstructionSurface | None:
        local = path.name == "CLAUDE.local.md"
        return self._surface(
            path,
            kind=("claude-local-instructions" if local else "claude-instructions"),
            authority="repository",
            scope="hierarchical",
            precedence=20 + (directory_depth(path.parent) * 2) + int(local),
            path_filter=path_filter,
        )

    def _discover_rules(
        self,
        rule_root: Path,
        *,
        authority: str,
        scope: str,
        surfaces: list[InstructionSurface],
        relationships: list[SurfaceRelationship],
        path_filter: PathFilter | None,
    ) -> None:
        for path in safe_rglob(
            rule_root,
            "*.md",
            path_filter=path_filter,
        ):
            surface = self._surface(
                path,
                kind="claude-rule",
                authority=authority,
                scope=scope,
                precedence=15 if authority == "user" else 60,
                path_filter=path_filter,
            )
            if surface is None:
                continue
            surfaces.append(surface)
            patterns = _frontmatter_paths(path)
            if patterns:
                relationships.append(
                    SurfaceRelationship(
                        type="path_scope",
                        from_surface_id=surface.id,
                        to_surface_id=None,
                        status="conditional",
                        location={"patterns": list(patterns)},
                    )
                )

    def _discover_skills(
        self,
        skill_roots: Iterable[Path],
        *,
        authority: str,
        scope: str,
        surfaces: list[InstructionSurface],
        path_filter: PathFilter | None,
    ) -> None:
        for skill_root in skill_roots:
            for path in skill_files(
                skill_root,
                path_filter=path_filter,
            ):
                surface = self._surface(
                    path,
                    kind="skill",
                    authority=authority,
                    scope=scope,
                    precedence=80,
                    path_filter=path_filter,
                )
                if surface is not None:
                    surfaces.append(surface)

    def _discover_settings(
        self,
        path: Path,
        *,
        authority: str,
        scope: str,
        surfaces: list[InstructionSurface],
        relationships: list[SurfaceRelationship],
        path_filter: PathFilter | None,
    ) -> None:
        surface = self._surface(
            path,
            kind="claude-settings",
            authority=authority,
            scope=scope,
            precedence=70,
            path_filter=path_filter,
        )
        if surface is None:
            return
        surfaces.append(surface)
        value = _read_json(path)
        if not isinstance(value, dict):
            relationships.append(
                SurfaceRelationship(
                    type="settings",
                    from_surface_id=surface.id,
                    to_surface_id=None,
                    status="unreadable",
                )
            )
            return
        relationships.extend(
            _legacy_session_start_relationships(
                surface,
                value,
                status="configured",
            )
        )
        relationships.extend(
            structural_hook_relationships(
                surface,
                value,
                status="configured",
                source="settings",
                scope=scope,
            )
        )
        marketplaces = value.get("extraKnownMarketplaces")
        if isinstance(marketplaces, dict):
            relationships.append(
                SurfaceRelationship(
                    type="marketplace_registration",
                    from_surface_id=surface.id,
                    to_surface_id=None,
                    status="active",
                    location={"count": len(marketplaces), "scope": scope},
                )
            )

    def _discover_plugins(
        self,
        home: Path,
        *,
        surfaces: list[InstructionSurface],
        relationships: list[SurfaceRelationship],
        path_filter: PathFilter | None,
    ) -> None:
        """Record structurally redacted hooks from enabled plugins.

        ``installed_plugins.json`` is the authoritative pointer when present.
        A cache directory is used only when it has exactly one possible
        version; ambiguous caches are reported instead of version-sorted.
        """

        plugins_root = home / ".claude" / "plugins"
        if not plugins_root.is_dir():
            return
        settings_path = home / ".claude" / "settings.json"
        if path_filter is not None and not path_filter(settings_path):
            return
        settings = _read_json(settings_path)
        enabled = settings.get("enabledPlugins") if isinstance(settings, dict) else None
        if not isinstance(enabled, dict):
            return

        settings_surface = next(
            (
                surface
                for surface in surfaces
                if surface.path == str(absolute_logical_path(settings_path))
            ),
            None,
        )

        for identifier in sorted(
            str(key) for key, value in enabled.items() if value is not False
        ):
            plugin, _, marketplace = identifier.partition("@")
            if not plugin or not marketplace:
                if settings_surface is not None:
                    relationships.append(
                        plugin_resolution_relationship(
                            settings_surface,
                            identifier,
                            status="invalid",
                            provider=self.name,
                        )
                    )
                continue
            root, status = self._plugin_root(
                plugins_root,
                identifier=identifier,
                marketplace=marketplace,
                plugin=plugin,
            )
            if root is None:
                if settings_surface is not None:
                    relationships.append(
                        plugin_resolution_relationship(
                            settings_surface,
                            identifier,
                            status=status,
                            provider=self.name,
                        )
                    )
                continue
            hook_paths = self._plugin_hook_paths(root)
            for hook_path in hook_paths:
                self._discover_plugin_hook_file(
                    hook_path,
                    identifier=identifier,
                    status=status,
                    surfaces=surfaces,
                    relationships=relationships,
                    path_filter=path_filter,
                )
            for path in skill_files(
                root / "skills",
                path_filter=path_filter,
            ):
                surface = self._surface(
                    path,
                    kind="skill",
                    authority="package",
                    scope="global",
                    precedence=80,
                    path_filter=path_filter,
                )
                if surface is not None:
                    surfaces.append(surface)

    @staticmethod
    def _plugin_root(
        plugins_root: Path,
        *,
        identifier: str,
        marketplace: str,
        plugin: str,
    ) -> tuple[Path | None, str]:
        """Resolve a plugin without guessing which cached version is active."""

        installed_path = plugins_root / "installed_plugins.json"
        if installed_path.exists():
            installed = _read_json(installed_path)
            if not isinstance(installed, dict):
                return None, "invalid"
            plugins = installed.get("plugins")
            records = plugins.get(identifier) if isinstance(plugins, dict) else None
            if not isinstance(records, list) or not records:
                return None, "invalid"
            candidates = {
                absolute_logical_path(Path(record["installPath"]))
                for record in records
                if isinstance(record, dict)
                and isinstance(record.get("installPath"), str)
                and Path(record["installPath"]).is_absolute()
            }
            if len(candidates) != 1:
                return None, "ambiguous" if candidates else "invalid"
            candidate = next(iter(candidates))
            return (
                (candidate, "active-observed")
                if candidate.is_dir()
                else (None, "invalid")
            )

        cached = tuple(
            candidate
            for candidate in (plugins_root / "cache" / marketplace / plugin).glob("*")
            if candidate.is_dir()
        )
        if len(cached) == 1:
            return absolute_logical_path(cached[0]), "trust-unknown"
        if len(cached) > 1:
            return None, "ambiguous"
        checkout = plugins_root / "marketplaces" / marketplace / "plugins" / plugin
        return (
            (absolute_logical_path(checkout), "trust-unknown")
            if checkout.is_dir()
            else (None, "invalid")
        )

    @staticmethod
    def _plugin_hook_paths(root: Path) -> tuple[Path, ...]:
        manifest = _read_json(root / ".claude-plugin" / "plugin.json")
        declared = manifest.get("hooks") if isinstance(manifest, dict) else None
        if isinstance(declared, str):
            values = (declared,)
        elif isinstance(declared, list) and all(
            isinstance(value, str) for value in declared
        ):
            values = tuple(declared)
        elif declared is None:
            values = ("./hooks/hooks.json",)
        else:
            return ()

        root_resolved = root.resolve(strict=False)
        paths: list[Path] = []
        for value in values:
            candidate = (root / value).resolve(strict=False)
            if candidate == root_resolved or root_resolved in candidate.parents:
                paths.append(absolute_logical_path(candidate))
        return tuple(paths)

    def _discover_plugin_hook_file(
        self,
        path: Path,
        *,
        identifier: str,
        status: str,
        surfaces: list[InstructionSurface],
        relationships: list[SurfaceRelationship],
        path_filter: PathFilter | None,
    ) -> None:
        surface = self._surface(
            path,
            kind="claude-plugin-hooks",
            authority="package",
            scope="global",
            precedence=5,
            path_filter=path_filter,
        )
        if surface is None:
            return
        surfaces.append(surface)
        value = _read_json(path)
        relationships.extend(
            _legacy_session_start_relationships(
                surface,
                value,
                status=status,
                plugin_identity=identifier,
            )
        )
        relationships.extend(
            structural_hook_relationships(
                surface,
                value,
                status=status,
                source="plugin",
                scope="global",
                plugin_identity=identifier,
            )
        )

    def _discover_marketplace_file(
        self,
        path: Path,
        *,
        authority: str,
        scope: str,
        surfaces: list[InstructionSurface],
        relationships: list[SurfaceRelationship],
        path_filter: PathFilter | None,
    ) -> None:
        surface = self._surface(
            path,
            kind="claude-marketplace",
            authority=authority,
            scope=scope,
            precedence=90,
            path_filter=path_filter,
        )
        if surface is None:
            return
        surfaces.append(surface)
        value = _read_json(path)
        if isinstance(value, dict):
            if isinstance(value.get("plugins"), list):
                count = len(value["plugins"])
            else:
                count = len(value)
            status = "active"
        else:
            count = 0
            status = "unreadable"
        relationships.append(
            SurfaceRelationship(
                type="marketplace_metadata",
                from_surface_id=surface.id,
                to_surface_id=None,
                status=status,
                location={"entry_count": count, "scope": scope},
            )
        )

    def _discover_imports(
        self,
        path: Path,
        *,
        authority: str,
        scope: str,
        depth: int,
        ancestry: tuple[str, ...],
        surfaces: list[InstructionSurface],
        surface_by_path: dict[str, InstructionSurface],
        relationships: list[SurfaceRelationship],
        path_filter: PathFilter | None,
    ) -> None:
        logical = absolute_logical_path(path)
        source = surface_by_path.get(str(logical))
        if source is None:
            source = self._surface(
                logical,
                kind="claude-import",
                authority=authority,
                scope=scope,
                precedence=25 + directory_depth(logical.parent),
                path_filter=path_filter,
            )
            if source is None:
                return
            surfaces.append(source)
            surface_by_path[source.path] = source

        current_ancestry = (*ancestry, source.path)
        for line_number, token in _imports(logical):
            target_path = _resolve_import(token, logical.parent)
            target_key = str(target_path)
            location = {"line": line_number}
            if path_filter is not None and not path_filter(target_path):
                continue
            if target_key in current_ancestry:
                relationships.append(
                    SurfaceRelationship(
                        type="imports",
                        from_surface_id=source.id,
                        to_surface_id=surface_by_path[target_key].id,
                        status="cycle",
                        location=location,
                    )
                )
                continue
            if not target_path.exists():
                relationships.append(
                    SurfaceRelationship(
                        type="imports",
                        from_surface_id=source.id,
                        to_surface_id=None,
                        status="missing",
                        location=location,
                    )
                )
                continue
            if depth >= self.import_depth_limit:
                relationships.append(
                    SurfaceRelationship(
                        type="imports",
                        from_surface_id=source.id,
                        to_surface_id=None,
                        status="max_depth",
                        location=location,
                    )
                )
                continue
            target = surface_by_path.get(target_key)
            if target is None:
                target = self._surface(
                    target_path,
                    kind="claude-import",
                    authority=authority,
                    scope=scope,
                    precedence=source.precedence,
                    path_filter=path_filter,
                )
                if target is None:
                    relationships.append(
                        SurfaceRelationship(
                            type="imports",
                            from_surface_id=source.id,
                            to_surface_id=None,
                            status="unreadable",
                            location=location,
                        )
                    )
                    continue
                surfaces.append(target)
                surface_by_path[target.path] = target
            relationships.append(
                SurfaceRelationship(
                    type="imports",
                    from_surface_id=source.id,
                    to_surface_id=target.id,
                    status="active",
                    location=location,
                )
            )
            self._discover_imports(
                target_path,
                authority=authority,
                scope=scope,
                depth=depth + 1,
                ancestry=current_ancestry,
                surfaces=surfaces,
                surface_by_path=surface_by_path,
                relationships=relationships,
                path_filter=path_filter,
            )

    def _surface(
        self,
        path: Path,
        *,
        kind: str,
        authority: str,
        scope: str,
        precedence: int,
        path_filter: PathFilter | None,
    ) -> InstructionSurface | None:
        return make_surface(
            path,
            kind=kind,
            provider=self.name,
            authority=authority,
            scope=scope,
            precedence=precedence,
            path_filter=path_filter,
        )

    @staticmethod
    def _is_apu_sidecar(path: Path) -> bool:
        return path.name == "CLAUDE.apu.md" or (
            path.parent.name == ".claude" and path.name in {"APU.md", "apu.md"}
        )


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _legacy_session_start_relationships(
    surface: InstructionSurface,
    value: object,
    *,
    status: str,
    plugin_identity: str | None = None,
) -> tuple[SurfaceRelationship, ...]:
    """Keep the original SessionStart inventory shape during schema migration."""

    hooks = value.get("hooks") if isinstance(value, dict) else None
    registrations = hooks.get("SessionStart") if isinstance(hooks, dict) else None
    if not isinstance(registrations, list):
        return ()
    relationships: list[SurfaceRelationship] = []
    for index, registration in enumerate(registrations):
        if not isinstance(registration, dict):
            continue
        location: dict[str, object] = {
            "event": "SessionStart",
            "registration_index": index,
        }
        if plugin_identity is not None:
            location.update({"plugin": plugin_identity, "source": "plugin"})
        relationships.append(
            SurfaceRelationship(
                type="session_start_hook",
                from_surface_id=surface.id,
                to_surface_id=None,
                status=status,
                location=location,
            )
        )
    return tuple(relationships)


def _imports(path: Path) -> tuple[tuple[int, str], ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ()
    found: list[tuple[int, str]] = []
    fenced = False
    for line_number, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        for match in _IMPORT_PATTERN.finditer(line):
            found.append((line_number, match.group(1).rstrip(".,;:")))
    return tuple(found)


def _resolve_import(token: str, containing_directory: Path) -> Path:
    if token.startswith("~/"):
        return absolute_logical_path(Path(token).expanduser())
    candidate = Path(token)
    if not candidate.is_absolute():
        candidate = containing_directory / candidate
    return absolute_logical_path(candidate)


def _frontmatter_paths(path: Path) -> tuple[str, ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ()
    if not text.startswith("---\n"):
        return ()
    end = text.find("\n---", 4)
    if end < 0:
        return ()
    lines = text[4:end].splitlines()
    patterns: list[str] = []
    collecting = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("paths:"):
            collecting = True
            inline = stripped.partition(":")[2].strip()
            if inline:
                try:
                    parsed = ast.literal_eval(inline)
                except (ValueError, SyntaxError):
                    parsed = inline
                if isinstance(parsed, (list, tuple)):
                    patterns.extend(str(item) for item in parsed)
                elif parsed:
                    patterns.append(str(parsed))
            continue
        if collecting and stripped.startswith("-"):
            patterns.append(stripped[1:].strip().strip("\"'"))
        elif collecting and stripped and not line.startswith((" ", "\t")):
            break
    return tuple(pattern for pattern in patterns if pattern)

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
from typing import Iterable

from apu.models import InstructionSurface, SurfaceRelationship

from .base import (
    DiscoveryResult,
    absolute_logical_path,
    ancestor_directories,
    deduplicate_surfaces,
    directory_depth,
    make_surface,
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

    def discover(self, roots: Iterable[Path], *, home: Path) -> DiscoveryResult:
        normalized_roots = tuple(absolute_logical_path(root) for root in roots)
        normalized_home = absolute_logical_path(home)
        surfaces: list[InstructionSurface] = []
        relationships: list[SurfaceRelationship] = []

        for filename, kind, precedence in (
            ("CLAUDE.md", "claude-instructions", 10),
        ):
            path = normalized_home / ".claude" / filename
            surface = self._surface(
                path,
                kind=kind,
                authority="user",
                scope="global",
                precedence=precedence,
            )
            if surface is not None:
                surfaces.append(surface)

        self._discover_rules(
            normalized_home / ".claude" / "rules",
            authority="user",
            scope="global",
            surfaces=surfaces,
            relationships=relationships,
        )
        self._discover_skills(
            (
                normalized_home / ".claude" / "skills",
                normalized_home / ".agents" / "skills",
            ),
            authority="user",
            scope="global",
            surfaces=surfaces,
        )
        self._discover_settings(
            normalized_home / ".claude" / "settings.json",
            authority="user",
            scope="global",
            surfaces=surfaces,
            relationships=relationships,
        )
        self._discover_marketplace_file(
            normalized_home / ".claude" / "plugins" / "known_marketplaces.json",
            authority="user",
            scope="global",
            surfaces=surfaces,
            relationships=relationships,
        )

        project_bases: set[Path] = set()
        sidecars: list[InstructionSurface] = []
        for root in normalized_roots:
            if root.is_file():
                if root.name in {"CLAUDE.md", "CLAUDE.local.md"}:
                    surface = self._repository_instruction(root)
                    if surface is not None:
                        surfaces.append(surface)
            else:
                for filename in ("CLAUDE.md", "CLAUDE.local.md"):
                    for path in safe_rglob(root, filename):
                        surface = self._repository_instruction(path)
                        if surface is not None:
                            surfaces.append(surface)
                project_bases.add(root)
                for claude_dir in safe_rglob(root, ".claude"):
                    if claude_dir.is_dir():
                        project_bases.add(claude_dir.parent)
            project_bases.update(ancestor_directories(root))

            if root.is_dir():
                for path in safe_rglob(root, "*.md"):
                    if self._is_apu_sidecar(path):
                        sidecar = self._surface(
                            path,
                            kind="claude-import",
                            authority="repository",
                            scope="repository",
                            precedence=25 + directory_depth(path.parent),
                        )
                        if sidecar is not None:
                            surfaces.append(sidecar)
                            sidecars.append(sidecar)

        for base in sorted(project_bases, key=str):
            for filename in ("CLAUDE.md", "CLAUDE.local.md"):
                surface = self._repository_instruction(base / filename)
                if surface is not None:
                    surfaces.append(surface)
            self._discover_rules(
                base / ".claude" / "rules",
                authority="repository",
                scope="repository",
                surfaces=surfaces,
                relationships=relationships,
            )
            self._discover_skills(
                (base / ".claude" / "skills", base / ".agents" / "skills"),
                authority="repository",
                scope="repository",
                surfaces=surfaces,
            )
            for filename in ("settings.json", "settings.local.json"):
                self._discover_settings(
                    base / ".claude" / filename,
                    authority="repository",
                    scope="repository",
                    surfaces=surfaces,
                    relationships=relationships,
                )
            self._discover_marketplace_file(
                base / ".claude-plugin" / "marketplace.json",
                authority="repository",
                scope="repository",
                surfaces=surfaces,
                relationships=relationships,
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
        self, path: Path
    ) -> InstructionSurface | None:
        local = path.name == "CLAUDE.local.md"
        return self._surface(
            path,
            kind=(
                "claude-local-instructions"
                if local
                else "claude-instructions"
            ),
            authority="repository",
            scope="hierarchical",
            precedence=20 + (directory_depth(path.parent) * 2) + int(local),
        )

    def _discover_rules(
        self,
        rule_root: Path,
        *,
        authority: str,
        scope: str,
        surfaces: list[InstructionSurface],
        relationships: list[SurfaceRelationship],
    ) -> None:
        for path in safe_rglob(rule_root, "*.md"):
            surface = self._surface(
                path,
                kind="claude-rule",
                authority=authority,
                scope=scope,
                precedence=15 if authority == "user" else 60,
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
    ) -> None:
        for skill_root in skill_roots:
            for path in skill_files(skill_root):
                surface = self._surface(
                    path,
                    kind="skill",
                    authority=authority,
                    scope=scope,
                    precedence=80,
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
    ) -> None:
        surface = self._surface(
            path,
            kind="claude-settings",
            authority=authority,
            scope=scope,
            precedence=70,
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
        hooks = value.get("hooks")
        if isinstance(hooks, dict):
            registrations = hooks.get("SessionStart")
            if isinstance(registrations, list):
                for index, _registration in enumerate(registrations):
                    relationships.append(
                        SurfaceRelationship(
                            type="session_start_hook",
                            from_surface_id=surface.id,
                            to_surface_id=None,
                            status="active",
                            location={
                                "event": "SessionStart",
                                "registration_index": index,
                            },
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

    def _discover_marketplace_file(
        self,
        path: Path,
        *,
        authority: str,
        scope: str,
        surfaces: list[InstructionSurface],
        relationships: list[SurfaceRelationship],
    ) -> None:
        surface = self._surface(
            path,
            kind="claude-marketplace",
            authority=authority,
            scope=scope,
            precedence=90,
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
            )

    def _surface(
        self,
        path: Path,
        *,
        kind: str,
        authority: str,
        scope: str,
        precedence: int,
    ) -> InstructionSurface | None:
        return make_surface(
            path,
            kind=kind,
            provider=self.name,
            authority=authority,
            scope=scope,
            precedence=precedence,
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

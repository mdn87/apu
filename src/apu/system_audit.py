from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from apu import __version__
from apu.audit import build_inventory
from apu.models import (
    Finding,
    InstructionSurface,
    Inventory,
    SurfaceRelationship,
    canonical_json,
    sha256_json,
)
from apu.system_profile import ProfileRoot, SystemProfile


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TraversalIssue:
    path: str
    kind: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "kind": self.kind, "detail": self.detail}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TraversalIssue:
        return cls(
            path=str(value["path"]),
            kind=str(value["kind"]),
            detail=str(value["detail"]),
        )


@dataclass(frozen=True)
class RepositoryDiscovery:
    repositories: tuple[str, ...]
    issues: tuple[TraversalIssue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "repositories": list(self.repositories),
            "issues": [issue.to_dict() for issue in self.issues],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RepositoryDiscovery:
        return cls(
            repositories=tuple(str(item) for item in value["repositories"]),
            issues=tuple(
                TraversalIssue.from_dict(item)
                for item in value.get("issues", ())
            ),
        )


def _matches_exclude(relative: str, patterns: Iterable[str]) -> bool:
    relative = relative.replace("\\", "/").strip("/")
    path = PurePosixPath(relative)
    for raw_pattern in patterns:
        pattern = raw_pattern.replace("\\", "/").strip("/")
        if not pattern:
            continue
        if "/" not in pattern and pattern in path.parts:
            return True
        if fnmatch.fnmatchcase(relative, pattern) or path.match(pattern):
            return True
        if pattern.endswith("/**"):
            prefix = pattern[:-3].rstrip("/")
            if relative == prefix or relative.startswith(prefix + "/"):
                return True
    return False


def _inside(candidate: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(candidate), str(root))) == str(root)
    except ValueError:
        return False


def discover_repositories(
    roots: Iterable[ProfileRoot],
) -> RepositoryDiscovery:
    """Discover `.git` work trees without escaping a declared cascade root."""

    repositories: set[str] = set()
    issues: list[TraversalIssue] = []
    for root_spec in sorted(roots, key=lambda item: os.path.normcase(item.path)):
        logical_root = Path(root_spec.path)
        try:
            root = logical_root.resolve(strict=True)
        except OSError as error:
            issues.append(
                TraversalIssue(
                    path=str(logical_root),
                    kind="unreadable",
                    detail=type(error).__name__,
                )
            )
            continue
        if not root.is_dir():
            issues.append(
                TraversalIssue(
                    path=str(logical_root),
                    kind="not-directory",
                    detail="cascade root is not a directory",
                )
            )
            continue

        pending: list[tuple[Path, str]] = [(root, "")]
        visited: set[tuple[int, int]] = set()
        while pending:
            current, relative = pending.pop()
            try:
                current_stat = current.stat()
            except OSError as error:
                issues.append(
                    TraversalIssue(
                        path=str(current),
                        kind="unreadable",
                        detail=type(error).__name__,
                    )
                )
                continue
            identity = (current_stat.st_dev, current_stat.st_ino)
            if identity in visited:
                issues.append(
                    TraversalIssue(
                        path=str(current),
                        kind="cycle",
                        detail="directory target was already visited",
                    )
                )
                continue
            visited.add(identity)

            if os.path.lexists(current / ".git"):
                repositories.add(str(current.resolve(strict=False)))

            try:
                entries = sorted(
                    os.scandir(current),
                    key=lambda item: (item.name.casefold(), item.name),
                    reverse=True,
                )
            except OSError as error:
                issues.append(
                    TraversalIssue(
                        path=str(current),
                        kind="unreadable",
                        detail=type(error).__name__,
                    )
                )
                continue

            for entry in entries:
                if entry.name == ".git":
                    continue
                child_relative = (
                    f"{relative}/{entry.name}" if relative else entry.name
                )
                if _matches_exclude(child_relative, root_spec.excludes):
                    continue
                try:
                    is_directory = entry.is_dir(follow_symlinks=True)
                except OSError as error:
                    issues.append(
                        TraversalIssue(
                            path=entry.path,
                            kind="unreadable",
                            detail=type(error).__name__,
                        )
                    )
                    continue
                if not is_directory:
                    continue
                try:
                    resolved = Path(entry.path).resolve(strict=True)
                except OSError as error:
                    issues.append(
                        TraversalIssue(
                            path=entry.path,
                            kind="unreadable",
                            detail=type(error).__name__,
                        )
                    )
                    continue
                if not _inside(resolved, root):
                    issues.append(
                        TraversalIssue(
                            path=entry.path,
                            kind="outside-root",
                            detail="directory link target is outside the cascade root",
                        )
                    )
                    continue
                pending.append((resolved, child_relative))

    return RepositoryDiscovery(
        repositories=tuple(
            sorted(repositories, key=lambda item: (os.path.normcase(item), item))
        ),
        issues=tuple(
            sorted(
                issues,
                key=lambda item: (
                    os.path.normcase(item.path),
                    item.kind,
                    item.detail,
                ),
            )
        ),
    )


@dataclass(frozen=True)
class RepositoryInventory:
    repository: str
    inventory: Inventory

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "inventory": self.inventory.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RepositoryInventory:
        return cls(
            repository=str(value["repository"]),
            inventory=Inventory.from_dict(value["inventory"]),
        )


@dataclass(frozen=True)
class SystemInventory:
    schema_version: int
    apu_version: str
    generated_at: str
    profile_sha256: str
    machine_inventory: Inventory
    repositories: tuple[RepositoryInventory, ...]
    discovery_issues: tuple[TraversalIssue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "apu_version": self.apu_version,
            "generated_at": self.generated_at,
            "profile_sha256": self.profile_sha256,
            "machine_inventory": self.machine_inventory.to_dict(),
            "repositories": [item.to_dict() for item in self.repositories],
            "discovery_issues": [
                issue.to_dict() for issue in self.discovery_issues
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SystemInventory:
        return cls(
            schema_version=int(value["schema_version"]),
            apu_version=str(value["apu_version"]),
            generated_at=str(value["generated_at"]),
            profile_sha256=str(value["profile_sha256"]),
            machine_inventory=Inventory.from_dict(value["machine_inventory"]),
            repositories=tuple(
                RepositoryInventory.from_dict(item)
                for item in value.get("repositories", ())
            ),
            discovery_issues=tuple(
                TraversalIssue.from_dict(item)
                for item in value.get("discovery_issues", ())
            ),
        )

    @property
    def artifact_sha256(self) -> str:
        return sha256_json(self.to_dict())


def _path_is_under(path: str, roots: tuple[Path, ...]) -> bool:
    candidate = Path(path).resolve(strict=False)
    return any(_inside(candidate, root) for root in roots)


def _filter_stack(
    stack: Mapping[str, Any], allowed_surface_ids: set[str]
) -> dict[str, Any]:
    result = dict(stack)
    ids = result.get("surface_ids")
    if isinstance(ids, (list, tuple)):
        result["surface_ids"] = [
            item for item in ids if item in allowed_surface_ids
        ]
    return result


def _scope_inventory(
    inventory: Inventory,
    *,
    global_roots: tuple[Path, ...],
    machine_only: bool,
) -> Inventory:
    normalized_surfaces: list[InstructionSurface] = []
    for surface in inventory.surfaces:
        declared_global = _path_is_under(surface.path, global_roots)
        if machine_only and not declared_global:
            continue
        if not machine_only and surface.scope == "global" and not declared_global:
            continue
        if declared_global:
            normalized_surfaces.append(
                replace(
                    surface,
                    authority=(
                        surface.authority
                        if surface.authority == "package"
                        else "user"
                    ),
                    scope="global",
                )
            )
        else:
            normalized_surfaces.append(surface)

    allowed_ids = {surface.id for surface in normalized_surfaces}
    relationships: tuple[SurfaceRelationship, ...] = tuple(
        relationship
        for relationship in inventory.relationships
        if relationship.from_surface_id in allowed_ids
        and (
            relationship.to_surface_id is None
            or relationship.to_surface_id in allowed_ids
        )
    )
    findings: tuple[Finding, ...] = tuple(
        finding
        for finding in inventory.findings
        if finding.surface_id in allowed_ids
    )
    return Inventory(
        schema_version=inventory.schema_version,
        apu_version=inventory.apu_version,
        generated_at=inventory.generated_at,
        scope=inventory.scope,
        surfaces=tuple(normalized_surfaces),
        relationships=relationships,
        effective_stacks=tuple(
            _filter_stack(stack, allowed_ids)
            for stack in inventory.effective_stacks
        ),
        findings=findings,
        evidence_summary=inventory.evidence_summary,
    )


def _deduplicate_global_findings(
    child: Inventory, machine: Inventory
) -> Inventory:
    global_ids = {
        surface.id for surface in child.surfaces if surface.scope == "global"
    }
    machine_findings = {
        canonical_json(finding.to_dict()) for finding in machine.findings
    }
    findings = tuple(
        finding
        for finding in child.findings
        if not (
            finding.surface_id in global_ids
            and canonical_json(finding.to_dict()) in machine_findings
        )
    )
    return replace(child, findings=findings)


def audit_system(
    profile: SystemProfile,
    *,
    home: Path | None = None,
    generated_at: str | None = None,
) -> SystemInventory:
    """Build one global inventory and one effective child inventory per repo."""

    selected_home = (home or Path.home()).resolve(strict=False)
    timestamp = generated_at or _now()
    discovery = discover_repositories(profile.roots)
    global_roots = tuple(
        Path(path).resolve(strict=False) for path in profile.global_surfaces
    )
    machine = _scope_inventory(
        build_inventory(
            global_roots,
            home=selected_home,
            working_directories=global_roots,
            generated_at=timestamp,
        ),
        global_roots=global_roots,
        machine_only=True,
    )

    children: list[RepositoryInventory] = []
    for repository in discovery.repositories:
        repository_path = Path(repository)
        child = _scope_inventory(
            build_inventory(
                (*global_roots, repository_path),
                home=selected_home,
                working_directories=(repository_path,),
                git_repository=repository_path,
                generated_at=timestamp,
            ),
            global_roots=global_roots,
            machine_only=False,
        )
        child = _deduplicate_global_findings(child, machine)
        children.append(
            RepositoryInventory(repository=repository, inventory=child)
        )

    return SystemInventory(
        schema_version=1,
        apu_version=__version__,
        generated_at=timestamp,
        profile_sha256=profile.artifact_sha256,
        machine_inventory=machine,
        repositories=tuple(children),
        discovery_issues=discovery.issues,
    )


build_system_inventory = audit_system
SystemAuditResult = SystemInventory

from __future__ import annotations

import fnmatch
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from apu import __version__
from apu.adapters.base import (
    PathFilter,
    ancestor_directories,
    repository_bases,
)
from apu.audit import build_inventory
from apu.classify import DetectorPolicy
from apu.models import (
    Finding,
    InstructionSurface,
    Inventory,
    SurfaceRelationship,
    canonical_json,
    sha256_json,
)
from apu.filesystem import matches_exclude as _matches_exclude
from apu.system_profile import ProfileRoot, ProfileSurface, SystemProfile

SYSTEM_INVENTORY_SCHEMA_VERSION = 2
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODEL_GENERATION = re.compile(r"^models-sha256:[0-9a-f]{64}$")
_BASELINE_STATUSES = frozenset(
    {"unconfigured", "adopted", "stale", "legacy-unverified"}
)
_MODEL_STATUSES = frozenset(
    {"current", "degraded", "unverified", "legacy-unverified"}
)
_IDENTITY_FIELDS = frozenset(
    {
        "runtime_id",
        "provider",
        "canonical_identity",
        "raw_alias",
        "source_url",
        "retrieved_at",
        "status",
        "unverified_since",
    }
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be null or non-empty text")
    return value


def _optional_sha256(value: Any, field: str) -> str | None:
    text = _optional_text(value, field)
    if text is not None and _SHA256.fullmatch(text) is None:
        raise ValueError(f"{field} must be null or a lowercase SHA-256")
    return text


def _validate_baseline_stamp(value: Mapping[str, Any]) -> Mapping[str, Any]:
    required = {"version", "status", "retrieved_at", "artifact_sha256"}
    if set(value) != required:
        raise ValueError(
            "baseline stamp fields must be exactly: "
            + ", ".join(sorted(required))
        )
    version = _optional_sha256(value["version"], "baseline.version")
    artifact_hash = _optional_sha256(
        value["artifact_sha256"], "baseline.artifact_sha256"
    )
    status = value["status"]
    if status not in _BASELINE_STATUSES:
        raise ValueError(f"unsupported baseline status: {status}")
    retrieved_at = _optional_text(
        value["retrieved_at"], "baseline.retrieved_at"
    )
    if (version is None) != (artifact_hash is None):
        raise ValueError(
            "baseline version and artifact_sha256 must both be set or null"
        )
    if status in {"adopted", "stale"} and (
        version is None or retrieved_at is None
    ):
        raise ValueError(f"baseline status {status} requires adopted provenance")
    return MappingProxyType(
        {
            "version": version,
            "status": status,
            "retrieved_at": retrieved_at,
            "artifact_sha256": artifact_hash,
        }
    )


def _validate_model_stamp(value: Mapping[str, Any]) -> Mapping[str, Any]:
    required = {
        "generation",
        "status",
        "retrieved_at",
        "artifact_sha256",
        "identities",
    }
    if set(value) != required:
        raise ValueError(
            "model stamp fields must be exactly: "
            + ", ".join(sorted(required))
        )
    generation = _optional_text(value["generation"], "models.generation")
    if generation is not None and _MODEL_GENERATION.fullmatch(generation) is None:
        raise ValueError("models.generation must be null or a model generation")
    artifact_hash = _optional_sha256(
        value["artifact_sha256"], "models.artifact_sha256"
    )
    status = value["status"]
    if status not in _MODEL_STATUSES:
        raise ValueError(f"unsupported model status: {status}")
    retrieved_at = _optional_text(value["retrieved_at"], "models.retrieved_at")
    raw_identities = value["identities"]
    if not isinstance(raw_identities, (list, tuple)):
        raise TypeError("models.identities must be an array")
    identities: list[Mapping[str, Any]] = []
    for raw in raw_identities:
        if not isinstance(raw, Mapping) or set(raw) != _IDENTITY_FIELDS:
            raise ValueError(
                "model identity fields must be exactly: "
                + ", ".join(sorted(_IDENTITY_FIELDS))
            )
        identity = {
            field: _optional_text(raw[field], f"models.identities[].{field}")
            for field in _IDENTITY_FIELDS
        }
        if identity["runtime_id"] is None or identity["provider"] is None:
            raise ValueError("model identity requires runtime_id and provider")
        identities.append(MappingProxyType(identity))
    identities.sort(
        key=lambda item: (
            str(item["provider"]),
            str(item["runtime_id"]),
        )
    )
    if status == "current" and (
        generation is None
        or retrieved_at is None
        or artifact_hash is None
        or not identities
    ):
        raise ValueError("current model stamp requires complete provenance")
    if generation is not None and artifact_hash is None:
        raise ValueError("model generation requires an artifact_sha256")
    return MappingProxyType(
        {
            "generation": generation,
            "status": status,
            "retrieved_at": retrieved_at,
            "artifact_sha256": artifact_hash,
            "identities": tuple(identities),
        }
    )


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
class EvaluationContext:
    """Versioned guidance and model facts shared by one system audit."""

    baseline: Mapping[str, Any]
    models: Mapping[str, Any]

    def __post_init__(self) -> None:
        baseline = _validate_baseline_stamp(self.baseline)
        models = _validate_model_stamp(self.models)
        object.__setattr__(self, "baseline", baseline)
        object.__setattr__(self, "models", models)

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": dict(self.baseline),
            "models": {
                **dict(self.models),
                "identities": [
                    dict(item) for item in self.models["identities"]
                ],
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvaluationContext:
        if not isinstance(value, Mapping):
            raise TypeError("evaluation_context must be an object")
        if set(value) != {"baseline", "models"}:
            raise ValueError(
                "evaluation_context fields must be exactly: baseline, models"
            )
        baseline = value["baseline"]
        models = value["models"]
        if not isinstance(baseline, Mapping) or not isinstance(models, Mapping):
            raise TypeError("evaluation_context stamps must be objects")
        return cls(baseline=baseline, models=models)

    @classmethod
    def legacy_unverified(cls) -> EvaluationContext:
        return cls(
            baseline={
                "version": None,
                "status": "legacy-unverified",
                "retrieved_at": None,
                "artifact_sha256": None,
            },
            models={
                "generation": None,
                "status": "legacy-unverified",
                "retrieved_at": None,
                "artifact_sha256": None,
                "identities": [],
            },
        )

    @classmethod
    def unconfigured(cls) -> EvaluationContext:
        return cls(
            baseline={
                "version": None,
                "status": "unconfigured",
                "retrieved_at": None,
                "artifact_sha256": None,
            },
            models={
                "generation": None,
                "status": "unverified",
                "retrieved_at": None,
                "artifact_sha256": None,
                "identities": [],
            },
        )


def load_evaluation_context(
    state_home: Path,
    *,
    model_registry: Mapping[str, Any] | None = None,
    model_artifact_sha256: str | None = None,
) -> EvaluationContext:
    """Load the current private evaluation inputs without networking."""

    from apu.guidance import guidance_evaluation_stamp
    from apu.model_registry import (
        load_model_registry,
        model_registry_artifact_sha256,
    )

    baseline = guidance_evaluation_stamp(state_home)
    registry = (
        load_model_registry(state_home)
        if model_registry is None
        else dict(model_registry)
    )
    has_registry = (
        registry["refresh_attempted_at"] is not None or bool(registry["models"])
    )
    artifact_sha256 = model_artifact_sha256
    if model_registry is None and has_registry:
        artifact_sha256 = model_registry_artifact_sha256(registry)
    models = _model_stamp_from_registry(
        registry,
        artifact_sha256=artifact_sha256,
    )
    return EvaluationContext(baseline=baseline, models=models)


def verify_evaluation_context(
    state_home: Path,
    context: EvaluationContext,
    *,
    model_observations: Iterable[Any] | None = None,
) -> None:
    """Verify an audit's claimed provenance against immutable private objects."""

    baseline = context.baseline
    if baseline["artifact_sha256"] is not None:
        version = baseline["version"]
        path = (
            Path(state_home)
            / "guidance"
            / "baselines"
            / f"{version}.json"
        )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(
                f"guidance baseline artifact is unavailable: {version}"
            ) from error
        if sha256_json(value) != baseline["artifact_sha256"]:
            raise ValueError("guidance baseline artifact hash mismatch")
        if value.get("baseline_version") != version:
            raise ValueError("guidance baseline version mismatch")

    models = context.models
    artifact_sha256 = models["artifact_sha256"]
    if artifact_sha256 is not None:
        from apu.model_registry import load_model_registry_artifact

        registry = load_model_registry_artifact(state_home, artifact_sha256)
        expected = _model_stamp_from_registry(
            registry,
            artifact_sha256=artifact_sha256,
        )
        if expected != context.to_dict()["models"]:
            raise ValueError(
                "model evaluation stamp does not match its immutable artifact"
            )
        if model_observations is not None:
            from apu.model_registry import reconcile_model_registry_observations

            current = reconcile_model_registry_observations(
                registry,
                model_observations,
            )
            observation_fields = (
                "provider",
                "raw_alias",
                "cli_version",
                "observation_error",
            )
            current_models = current["models"]
            stored_models = registry["models"]
            observations_match = set(current_models) == set(stored_models) and all(
                all(
                    current_models[runtime_id]["observation"][field]
                    == stored_models[runtime_id]["observation"][field]
                    for field in observation_fields
                )
                for runtime_id in stored_models
            )
            if not observations_match:
                raise ValueError(
                    "local model configuration changed after audit; rerun "
                    "`apu system audit`"
                )


def _model_stamp_from_registry(
    registry: Mapping[str, Any],
    *,
    artifact_sha256: str | None,
) -> dict[str, Any]:
    identities: list[dict[str, Any]] = []
    for runtime_id, entry in sorted(registry["models"].items()):
        observation = entry["observation"]
        resolution = entry["resolution"]
        verification = entry["verification"]
        identities.append(
            {
                "runtime_id": runtime_id,
                "provider": observation["provider"],
                "canonical_identity": (
                    resolution["canonical_identity"]
                    if resolution is not None
                    else None
                ),
                "raw_alias": observation["raw_alias"],
                "source_url": (
                    resolution["source_url"]
                    if resolution is not None
                    else None
                ),
                "retrieved_at": (
                    resolution["retrieved_at"]
                    if resolution is not None
                    else None
                ),
                "status": verification["status"],
                "unverified_since": verification["unverified_since"],
            }
        )
    return {
        "generation": registry["generation"],
        "status": registry["refresh_status"],
        "retrieved_at": registry["refresh_attempted_at"],
        "artifact_sha256": artifact_sha256,
        "identities": identities,
    }


@dataclass(frozen=True)
class SystemInventory:
    schema_version: int
    apu_version: str
    generated_at: str
    profile_sha256: str
    machine_inventory: Inventory
    repositories: tuple[RepositoryInventory, ...]
    discovery_issues: tuple[TraversalIssue, ...] = ()
    evaluation_context: EvaluationContext = field(
        default_factory=EvaluationContext.legacy_unverified
    )

    def to_dict(self) -> dict[str, Any]:
        value = {
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
        if self.schema_version == SYSTEM_INVENTORY_SCHEMA_VERSION:
            value["evaluation_context"] = self.evaluation_context.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SystemInventory:
        if "schema_version" not in value:
            raise ValueError("system inventory requires schema_version")
        schema_version = int(value["schema_version"])
        if schema_version not in {1, SYSTEM_INVENTORY_SCHEMA_VERSION}:
            raise ValueError(
                f"unsupported system inventory schema_version: {schema_version}"
            )
        expected_fields = {
            "schema_version",
            "apu_version",
            "generated_at",
            "profile_sha256",
            "machine_inventory",
            "repositories",
            "discovery_issues",
        }
        if schema_version == SYSTEM_INVENTORY_SCHEMA_VERSION:
            expected_fields.add("evaluation_context")
        if set(value) != expected_fields:
            missing = expected_fields - set(value)
            unknown = set(value) - expected_fields
            if missing:
                raise ValueError(
                    "system inventory is missing fields: "
                    + ", ".join(sorted(missing))
                )
            raise ValueError(
                "system inventory has unsupported fields: "
                + ", ".join(sorted(unknown))
            )
        raw_context = value.get("evaluation_context")
        if schema_version == SYSTEM_INVENTORY_SCHEMA_VERSION:
            if raw_context is None:
                raise ValueError("system inventory v2 requires evaluation_context")
            context = EvaluationContext.from_dict(raw_context)
        else:
            context = EvaluationContext.legacy_unverified()
        return cls(
            schema_version=schema_version,
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
            evaluation_context=context,
        )

    @property
    def artifact_sha256(self) -> str:
        return sha256_json(self.to_dict())


def _path_is_under(path: str, roots: tuple[Path, ...]) -> bool:
    candidate = Path(path).resolve(strict=False)
    return any(_inside(candidate, root) for root in roots)


def _machine_path_filter(
    global_roots: tuple[Path, ...],
    surfaces: tuple[ProfileSurface, ...] = (),
) -> PathFilter:
    # Surfaces carry the same relative exclude patterns roots do. Without this
    # a directory listed in global_surfaces is scanned wholesale, sweeping in
    # agent runtime scratch such as ~/.codex/tmp/arg0.
    resolved = tuple(
        (Path(surface.path).resolve(strict=False), surface.excludes)
        for surface in surfaces
        if surface.excludes
    )

    def _accept(path: Path) -> bool:
        if not _path_is_under(str(path), global_roots):
            return False
        candidate = Path(path).resolve(strict=False)
        for base, patterns in resolved:
            try:
                relative = candidate.relative_to(base)
            except ValueError:
                continue
            if _matches_exclude(str(relative), patterns):
                return False
        return True

    return _accept


def _repository_path_filter(
    profile: SystemProfile,
    repository: Path,
    *,
    home: Path,
    global_roots: tuple[Path, ...],
) -> PathFilter:
    repository = repository.resolve(strict=False)
    ancestor_bases = tuple(
        base
        for base in repository_bases(
            ancestor_directories(repository),
            home=home,
        )
        if base != repository
    )
    applicable_roots = tuple(
        root_spec
        for root_spec in profile.roots
        if _inside(repository, Path(root_spec.path).resolve(strict=False))
    )

    def included(path: Path) -> bool:
        candidate = path.resolve(strict=False)
        if _path_is_under(str(candidate), global_roots):
            return True
        if _inside(candidate, repository):
            for root_spec in applicable_roots:
                root = Path(root_spec.path).resolve(strict=False)
                if not _inside(candidate, root):
                    continue
                relative = candidate.relative_to(root).as_posix()
                if relative != "." and _matches_exclude(
                    relative,
                    root_spec.excludes,
                ):
                    return False
            return True
        for base in ancestor_bases:
            if candidate.parent == base:
                return True
            if any(
                _inside(candidate, base / directory)
                for directory in (".agents", ".claude", ".claude-plugin")
            ):
                return True
        return False

    return included


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
    evaluation_context: EvaluationContext | None = None,
    detector_policy: DetectorPolicy | None = None,
) -> SystemInventory:
    """Build one global inventory and one effective child inventory per repo."""

    selected_home = (home or Path.home()).resolve(strict=False)
    timestamp = generated_at or _now()
    discovery = discover_repositories(profile.roots)
    global_roots = tuple(
        Path(surface.path).resolve(strict=False)
        for surface in profile.global_surfaces
    )
    machine = _scope_inventory(
        build_inventory(
            global_roots,
            home=selected_home,
            working_directories=global_roots,
            generated_at=timestamp,
            detector_policy=detector_policy,
            path_filter=_machine_path_filter(
                global_roots, profile.global_surfaces
            ),
        ),
        global_roots=global_roots,
        machine_only=True,
    )

    children: list[RepositoryInventory] = []
    for repository in discovery.repositories:
        repository_path = Path(repository)
        path_filter = _repository_path_filter(
            profile,
            repository_path,
            home=selected_home,
            global_roots=global_roots,
        )
        child = _scope_inventory(
            build_inventory(
                (*global_roots, repository_path),
                home=selected_home,
                working_directories=(repository_path,),
                git_repository=repository_path,
                generated_at=timestamp,
                detector_policy=detector_policy,
                path_filter=path_filter,
            ),
            global_roots=global_roots,
            machine_only=False,
        )
        child = _deduplicate_global_findings(child, machine)
        children.append(
            RepositoryInventory(repository=repository, inventory=child)
        )

    return SystemInventory(
        schema_version=SYSTEM_INVENTORY_SCHEMA_VERSION,
        apu_version=__version__,
        generated_at=timestamp,
        profile_sha256=profile.artifact_sha256,
        machine_inventory=machine,
        repositories=tuple(children),
        discovery_issues=discovery.issues,
        evaluation_context=evaluation_context or EvaluationContext.unconfigured(),
    )


build_system_inventory = audit_system
SystemAuditResult = SystemInventory

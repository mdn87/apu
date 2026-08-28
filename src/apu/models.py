from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, is_dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping


class ValidationError(ValueError):
    """Raised when an APU artifact violates its public contract."""


def _primitive(value: Any) -> Any:
    if is_dataclass(value):
        return _primitive(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_primitive(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Serialize an artifact deterministically for hashing and persistence."""

    return json.dumps(
        _primitive(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def sha256_bytes(content: bytes) -> str:
    return sha256(content).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


@dataclass(frozen=True)
class InstructionSurface:
    id: str
    path: str
    kind: str
    provider: str
    authority: str
    scope: str
    real_path: str
    is_symlink: bool
    content_sha256: str
    mode: str | None
    precedence: int
    sensitive: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> InstructionSurface:
        return cls(**dict(value))


@dataclass(frozen=True)
class SurfaceRelationship:
    type: str
    from_surface_id: str
    to_surface_id: str | None
    status: str
    location: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SurfaceRelationship:
        return cls(**dict(value))


@dataclass(frozen=True)
class Finding:
    id: str
    surface_id: str
    location: Mapping[str, Any]
    category: str
    severity: str
    confidence: str
    analysis_method: str
    evidence: tuple[str, ...]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence"] = list(self.evidence)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Finding:
        fields = dict(value)
        fields["evidence"] = tuple(fields.get("evidence", ()))
        return cls(**fields)


@dataclass(frozen=True)
class Inventory:
    schema_version: int
    apu_version: str
    generated_at: str
    scope: Mapping[str, Any]
    surfaces: tuple[InstructionSurface, ...]
    relationships: tuple[SurfaceRelationship, ...] = ()
    effective_stacks: tuple[Mapping[str, Any], ...] = ()
    findings: tuple[Finding, ...] = ()
    evidence_summary: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "apu_version": self.apu_version,
            "generated_at": self.generated_at,
            "scope": dict(self.scope),
            "surfaces": [surface.to_dict() for surface in self.surfaces],
            "relationships": [
                relationship.to_dict() for relationship in self.relationships
            ],
            "effective_stacks": [dict(stack) for stack in self.effective_stacks],
            "findings": [finding.to_dict() for finding in self.findings],
            "evidence_summary": dict(self.evidence_summary),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Inventory:
        fields = dict(value)
        fields["surfaces"] = tuple(
            InstructionSurface.from_dict(item) for item in fields.get("surfaces", ())
        )
        fields["relationships"] = tuple(
            SurfaceRelationship.from_dict(item)
            for item in fields.get("relationships", ())
        )
        fields["effective_stacks"] = tuple(
            dict(item) for item in fields.get("effective_stacks", ())
        )
        fields["findings"] = tuple(
            Finding.from_dict(item) for item in fields.get("findings", ())
        )
        return cls(**fields)

    @property
    def artifact_sha256(self) -> str:
        return sha256_json(self.to_dict())


APPROVAL_STATUSES = frozenset({"pending", "approved", "rejected", "deferred"})
PLAN_STATUSES = frozenset({"draft", "approved", "applied", "rolled_back"})
MUTATING_ACTIONS = frozenset({"merge", "create", "remove", "symlink", "configure"})


@dataclass(frozen=True)
class Approval:
    status: str = "pending"
    recorded_at: str | None = None
    method: str | None = None

    def validate(self) -> None:
        if self.status not in APPROVAL_STATUSES:
            raise ValidationError(f"unsupported approval status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Approval:
        return cls(**dict(value))


@dataclass(frozen=True)
class PlanOperation:
    id: str
    action: str
    target: str
    source: str | None
    ownership: str
    strategy: str
    precondition_sha256: str | None
    proposed_sha256: str | None
    backup_required: bool
    requires_confirmation: bool
    approval: Approval
    reason: str
    evidence: tuple[str, ...]
    atomic_group_id: str | None = None
    group_content_sha256: str | None = None

    @property
    def is_mutating(self) -> bool:
        return self.action in MUTATING_ACTIONS and self.strategy != "proposal_only"

    def validate(self) -> None:
        if not self.id:
            raise ValidationError("operation id is required")
        if not self.target:
            raise ValidationError(f"operation {self.id} target is required")
        self.approval.validate()
        if self.action == "relocate":
            raise ValidationError(
                "relocate must be represented by a paired remove and create"
            )
        if (self.atomic_group_id is None) != (self.group_content_sha256 is None):
            raise ValidationError(
                f"operation {self.id} must set both atomic-group fields or neither"
            )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["approval"] = self.approval.to_dict()
        value["evidence"] = list(self.evidence)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PlanOperation:
        fields = dict(value)
        fields["approval"] = Approval.from_dict(fields["approval"])
        fields["evidence"] = tuple(fields.get("evidence", ()))
        fields.setdefault("atomic_group_id", None)
        fields.setdefault("group_content_sha256", None)
        return cls(**fields)


def derive_plan_status(operations: Iterable[PlanOperation]) -> str:
    mutating = tuple(operation for operation in operations if operation.is_mutating)
    if not mutating:
        return "draft"
    decisions = {operation.approval.status for operation in mutating}
    if "pending" in decisions or "deferred" in decisions:
        return "draft"
    if "approved" not in decisions:
        return "draft"
    return "approved"


def _validate_relocations(operations: tuple[PlanOperation, ...]) -> None:
    groups: dict[str, list[PlanOperation]] = {}
    for operation in operations:
        if operation.atomic_group_id is not None:
            groups.setdefault(operation.atomic_group_id, []).append(operation)
    for group_id, members in groups.items():
        actions = {member.action for member in members}
        approvals = {member.approval.status for member in members}
        hashes = {member.group_content_sha256 for member in members}
        if (
            len(members) != 2
            or actions != {"remove", "create"}
            or len(approvals) != 1
            or len(hashes) != 1
            or None in hashes
            or next(
                member for member in members if member.action == "create"
            ).proposed_sha256
            not in hashes
        ):
            raise ValidationError(
                f"relocation {group_id} must be an atomic remove/create pair "
                "with one approval decision and shared content hash"
            )


@dataclass(frozen=True)
class Plan:
    schema_version: int
    apu_version: str
    created_at: str
    inventory_sha256: str
    status: str
    operations: tuple[PlanOperation, ...]
    validation: Mapping[str, Any] = field(
        default_factory=lambda: {"commands": [], "fixtures": [], "required": []}
    )

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ValidationError("unsupported plan schema version")
        if self.status not in PLAN_STATUSES:
            raise ValidationError(f"unsupported plan status: {self.status}")
        protected_roots = self.validation.get("protected_roots", ())
        if not isinstance(protected_roots, (list, tuple)) or any(
            not isinstance(value, str)
            or not value
            or not Path(value).expanduser().is_absolute()
            for value in protected_roots
        ):
            raise ValidationError(
                "plan validation.protected_roots must contain absolute paths"
            )
        identifiers: set[str] = set()
        mutation_targets: dict[str, str] = {}
        for operation in self.operations:
            operation.validate()
            if operation.id in identifiers:
                raise ValidationError(f"duplicate operation id: {operation.id}")
            identifiers.add(operation.id)
            if operation.is_mutating:
                target = Path(operation.target).expanduser()
                if not target.is_absolute():
                    raise ValidationError(
                        f"operation {operation.id} target must be absolute"
                    )
                identity = os.path.normcase(str(target.resolve(strict=False)))
                previous = mutation_targets.get(identity)
                if previous is not None:
                    raise ValidationError(
                        f"operations {previous} and {operation.id} target "
                        "the same filesystem object"
                    )
                mutation_targets[identity] = operation.id
        _validate_relocations(self.operations)
        derived = derive_plan_status(self.operations)
        if (
            self.status in {"approved", "applied", "rolled_back"}
            and derived != "approved"
        ):
            raise ValidationError(
                f"plan status {self.status} conflicts with operation decisions"
            )
        if self.status == "draft" and derived == "approved":
            raise ValidationError(
                "plan status draft conflicts with resolved operation decisions"
            )

    def executable_operations(self) -> tuple[PlanOperation, ...]:
        self.validate()
        if self.status != "approved":
            return ()
        return tuple(
            operation
            for operation in self.operations
            if operation.is_mutating and operation.approval.status == "approved"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "apu_version": self.apu_version,
            "created_at": self.created_at,
            "inventory_sha256": self.inventory_sha256,
            "status": self.status,
            "operations": [operation.to_dict() for operation in self.operations],
            "validation": dict(self.validation),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Plan:
        fields = dict(value)
        fields["operations"] = tuple(
            PlanOperation.from_dict(item) for item in fields.get("operations", ())
        )
        fields.setdefault(
            "validation", {"commands": [], "fixtures": [], "required": []}
        )
        return cls(**fields)

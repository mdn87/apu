from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apu.classify import AUTO_REMOVABLE_CATEGORIES
from apu.models import Finding, InstructionSurface, canonical_json, sha256_json
from apu.system_audit import SystemInventory

REMEDIATION_POLICIES = frozenset({"auto", "work-order", "ignore"})
SENSITIVE_CATEGORY = "sensitive-material-exposure"
SYSTEM_PLAN_SCHEMA_VERSION = 1
_HEX_DIGITS = frozenset("0123456789abcdef")


class SystemPlanningError(ValueError):
    """Raised when a system proposal cannot satisfy its public contract."""


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SystemPlanningError(f"{field} must be a non-empty string")
    return value


def _require_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise SystemPlanningError(f"{field} must be a boolean")
    return value


def _require_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SystemPlanningError(f"{field} must be an object")
    return value


def _require_array(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise SystemPlanningError(f"{field} must be an array")
    return value


def _require_fields(
    value: Mapping[str, Any],
    fields: set[str],
    *,
    artifact: str,
) -> None:
    missing = fields - set(value)
    unknown = set(value) - fields
    if missing:
        raise SystemPlanningError(
            f"{artifact} is missing fields: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise SystemPlanningError(
            f"{artifact} has unsupported fields: {', '.join(sorted(unknown))}"
        )


def _validate_sha256(value: str, field: str) -> None:
    if len(value) != 64 or any(character not in _HEX_DIGITS for character in value):
        raise SystemPlanningError(f"{field} must be a lowercase SHA-256 digest")


def _normalized_target(path: str) -> str:
    return os.path.normcase(str(Path(path).resolve(strict=False)))


def _stable_id(prefix: str, value: Mapping[str, Any]) -> str:
    return f"{prefix}-{sha256_json(value)[:20]}"


def _policy_map(value: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_category, raw_policy in value.items():
        category = _require_string(raw_category, "remediation policy category")
        policy = _require_string(
            raw_policy, f"remediation policy for {category}"
        )
        if policy not in REMEDIATION_POLICIES:
            raise SystemPlanningError(
                f"unsupported remediation policy for {category}: {policy}"
            )
        result[category] = policy
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class RoutedFinding:
    """One deduplicated finding and the decision that routed it."""

    id: str
    finding_ids: tuple[str, ...]
    inventory_refs: tuple[str, ...]
    surface_ids: tuple[str, ...]
    target: str
    surface_content_sha256: str
    authority: str
    scope: str
    category: str
    severity: str
    confidence: str
    analysis_method: str
    location: Mapping[str, Any]
    evidence: tuple[str, ...]
    summary: str
    requested_policy: str
    effective_policy: str
    routing_reason: str
    surface_sensitive: bool

    def validate(self) -> None:
        _require_string(self.id, "routed finding id")
        if not self.id.startswith("finding-route-"):
            raise SystemPlanningError(
                f"routed finding id has unsupported form: {self.id}"
            )
        if not self.finding_ids:
            raise SystemPlanningError(f"{self.id} must name source findings")
        for field, values in (
            ("finding_ids", self.finding_ids),
            ("inventory_refs", self.inventory_refs),
            ("surface_ids", self.surface_ids),
            ("evidence", self.evidence),
        ):
            if any(not isinstance(item, str) or not item for item in values):
                raise SystemPlanningError(
                    f"{self.id}.{field} must contain non-empty strings"
                )
            if tuple(sorted(set(values))) != values:
                raise SystemPlanningError(
                    f"{self.id}.{field} must be sorted and unique"
                )
        _require_string(self.target, f"{self.id}.target")
        if not Path(self.target).is_absolute():
            raise SystemPlanningError(f"{self.id}.target must be absolute")
        _validate_sha256(
            self.surface_content_sha256,
            f"{self.id}.surface_content_sha256",
        )
        for field in (
            "authority",
            "scope",
            "category",
            "severity",
            "confidence",
            "analysis_method",
            "summary",
            "routing_reason",
        ):
            _require_string(getattr(self, field), f"{self.id}.{field}")
        if self.requested_policy not in REMEDIATION_POLICIES:
            raise SystemPlanningError(
                f"{self.id}.requested_policy is unsupported"
            )
        if self.effective_policy not in REMEDIATION_POLICIES:
            raise SystemPlanningError(
                f"{self.id}.effective_policy is unsupported"
            )
        if self.category == SENSITIVE_CATEGORY:
            if self.effective_policy != "work-order":
                raise SystemPlanningError(
                    f"{self.id} sensitive material must be a work order"
                )
            if self.routing_reason != "sensitive-material-manual-only":
                raise SystemPlanningError(
                    f"{self.id} sensitive material must be manual-only"
                )
            if self.evidence:
                raise SystemPlanningError(
                    f"{self.id} sensitive material cannot embed evidence"
                )
        if self.surface_sensitive and self.evidence:
            raise SystemPlanningError(
                f"{self.id} sensitive surface cannot embed evidence"
            )
        if (
            self.surface_sensitive or self.category == SENSITIVE_CATEGORY
        ) and set(self.location) - {"line"}:
            raise SystemPlanningError(
                f"{self.id} sensitive location may contain only a line number"
            )
        _require_bool(self.surface_sensitive, f"{self.id}.surface_sensitive")
        expected_id = _stable_id(
            "finding-route",
            {
                "target": _normalized_target(self.target),
                "content_sha256": self.surface_content_sha256,
                "category": self.category,
                "location": dict(self.location),
                "summary": self.summary,
            },
        )
        if self.id != expected_id:
            raise SystemPlanningError(
                f"{self.id} does not match routed finding contents"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "finding_ids": list(self.finding_ids),
            "inventory_refs": list(self.inventory_refs),
            "surface_ids": list(self.surface_ids),
            "target": self.target,
            "surface_content_sha256": self.surface_content_sha256,
            "authority": self.authority,
            "scope": self.scope,
            "category": self.category,
            "severity": self.severity,
            "confidence": self.confidence,
            "analysis_method": self.analysis_method,
            "location": dict(self.location),
            "evidence": list(self.evidence),
            "summary": self.summary,
            "requested_policy": self.requested_policy,
            "effective_policy": self.effective_policy,
            "routing_reason": self.routing_reason,
            "surface_sensitive": self.surface_sensitive,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RoutedFinding:
        fields = {
            "id",
            "finding_ids",
            "inventory_refs",
            "surface_ids",
            "target",
            "surface_content_sha256",
            "authority",
            "scope",
            "category",
            "severity",
            "confidence",
            "analysis_method",
            "location",
            "evidence",
            "summary",
            "requested_policy",
            "effective_policy",
            "routing_reason",
            "surface_sensitive",
        }
        _require_fields(value, fields, artifact="routed finding")
        result = cls(
            id=_require_string(value["id"], "routed finding id"),
            finding_ids=tuple(
                _require_string(item, "finding_ids[]")
                for item in _require_array(value["finding_ids"], "finding_ids")
            ),
            inventory_refs=tuple(
                _require_string(item, "inventory_refs[]")
                for item in _require_array(
                    value["inventory_refs"], "inventory_refs"
                )
            ),
            surface_ids=tuple(
                _require_string(item, "surface_ids[]")
                for item in _require_array(value["surface_ids"], "surface_ids")
            ),
            target=_require_string(value["target"], "target"),
            surface_content_sha256=_require_string(
                value["surface_content_sha256"],
                "surface_content_sha256",
            ),
            authority=_require_string(value["authority"], "authority"),
            scope=_require_string(value["scope"], "scope"),
            category=_require_string(value["category"], "category"),
            severity=_require_string(value["severity"], "severity"),
            confidence=_require_string(value["confidence"], "confidence"),
            analysis_method=_require_string(
                value["analysis_method"], "analysis_method"
            ),
            location=dict(_require_mapping(value["location"], "location")),
            evidence=tuple(
                _require_string(item, "evidence[]")
                for item in _require_array(value["evidence"], "evidence")
            ),
            summary=_require_string(value["summary"], "summary"),
            requested_policy=_require_string(
                value["requested_policy"], "requested_policy"
            ),
            effective_policy=_require_string(
                value["effective_policy"], "effective_policy"
            ),
            routing_reason=_require_string(
                value["routing_reason"], "routing_reason"
            ),
            surface_sensitive=_require_bool(
                value["surface_sensitive"], "surface_sensitive"
            ),
        )
        result.validate()
        return result


@dataclass(frozen=True)
class SystemAutoOperation:
    """A deterministic batch that will become one target-file operation."""

    id: str
    target: str
    precondition_sha256: str
    authority: str
    action: str
    strategy: str
    line_numbers: tuple[int, ...]
    findings: tuple[RoutedFinding, ...]

    def validate(self) -> None:
        _require_string(self.id, "auto operation id")
        if not self.id.startswith("system-op-"):
            raise SystemPlanningError(
                f"auto operation id has unsupported form: {self.id}"
            )
        if not Path(self.target).is_absolute():
            raise SystemPlanningError(f"{self.id}.target must be absolute")
        _validate_sha256(
            self.precondition_sha256, f"{self.id}.precondition_sha256"
        )
        if self.authority == "package":
            raise SystemPlanningError(
                f"{self.id} cannot directly rewrite package-authority content"
            )
        if self.action != "merge" or self.strategy != "remove-lines":
            raise SystemPlanningError(
                f"{self.id} has unsupported deterministic remediation"
            )
        if not self.findings:
            raise SystemPlanningError(f"{self.id} must contain findings")
        if not self.line_numbers or any(
                not isinstance(line, int)
                or isinstance(line, bool)
                or line < 1
                for line in self.line_numbers
        ):
            raise SystemPlanningError(
                f"{self.id}.line_numbers must be sorted unique positive integers"
            )
        if tuple(sorted(set(self.line_numbers))) != self.line_numbers:
            raise SystemPlanningError(
                f"{self.id}.line_numbers must be sorted unique positive integers"
            )
        for finding in self.findings:
            finding.validate()
            if finding.effective_policy != "auto":
                raise SystemPlanningError(
                    f"{self.id} contains a non-auto finding"
                )
            if finding.target != self.target:
                raise SystemPlanningError(
                    f"{self.id} contains findings for another target"
                )
            if finding.surface_content_sha256 != self.precondition_sha256:
                raise SystemPlanningError(
                    f"{self.id} contains conflicting preconditions"
                )
        expected_id = _stable_id(
            "system-op",
            {
                "target": _normalized_target(self.target),
                "precondition_sha256": self.precondition_sha256,
                "finding_route_ids": [
                    finding.id for finding in self.findings
                ],
            },
        )
        if self.id != expected_id:
            raise SystemPlanningError(
                f"{self.id} does not match auto operation contents"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "precondition_sha256": self.precondition_sha256,
            "authority": self.authority,
            "action": self.action,
            "strategy": self.strategy,
            "line_numbers": list(self.line_numbers),
            "findings": [finding.to_dict() for finding in self.findings],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SystemAutoOperation:
        fields = {
            "id",
            "target",
            "precondition_sha256",
            "authority",
            "action",
            "strategy",
            "line_numbers",
            "findings",
        }
        _require_fields(value, fields, artifact="auto operation")
        result = cls(
            id=_require_string(value["id"], "auto operation id"),
            target=_require_string(value["target"], "auto operation target"),
            precondition_sha256=_require_string(
                value["precondition_sha256"], "precondition_sha256"
            ),
            authority=_require_string(value["authority"], "authority"),
            action=_require_string(value["action"], "action"),
            strategy=_require_string(value["strategy"], "strategy"),
            line_numbers=tuple(
                item
                for item in _require_array(
                    value["line_numbers"], "line_numbers"
                )
            ),
            findings=tuple(
                RoutedFinding.from_dict(
                    _require_mapping(item, "auto operation finding")
                )
                for item in _require_array(value["findings"], "findings")
            ),
        )
        result.validate()
        return result


@dataclass(frozen=True)
class SystemWorkOrder:
    """A target-scoped group whose remediation requires judgment."""

    id: str
    target: str
    handling: str
    dispatchable: bool
    manual_only: bool
    requires_sanitized_staging: bool
    content_policy: str
    package_authority: bool
    findings: tuple[RoutedFinding, ...]

    def validate(self) -> None:
        _require_string(self.id, "work order id")
        if not self.id.startswith("work-order-"):
            raise SystemPlanningError(
                f"work order id has unsupported form: {self.id}"
            )
        if not Path(self.target).is_absolute():
            raise SystemPlanningError(f"{self.id}.target must be absolute")
        if self.handling not in {"manual-only", "sanitized", "standard"}:
            raise SystemPlanningError(
                f"{self.id}.handling is unsupported: {self.handling}"
            )
        for field in (
            "dispatchable",
            "manual_only",
            "requires_sanitized_staging",
            "package_authority",
        ):
            _require_bool(getattr(self, field), f"{self.id}.{field}")
        if self.manual_only == self.dispatchable:
            raise SystemPlanningError(
                f"{self.id} must be either manual-only or dispatchable"
            )
        if self.manual_only and self.handling != "manual-only":
            raise SystemPlanningError(
                f"{self.id} manual work orders require manual-only handling"
            )
        if self.requires_sanitized_staging != (
            self.dispatchable and self.handling == "sanitized"
        ):
            raise SystemPlanningError(
                f"{self.id} has inconsistent sanitized-staging flags"
            )
        expected_policy = (
            "location-line-content-hash-only"
            if self.handling in {"manual-only", "sanitized"}
            else "finding-evidence"
        )
        if self.content_policy != expected_policy:
            raise SystemPlanningError(
                f"{self.id} has inconsistent content policy"
            )
        if not self.findings:
            raise SystemPlanningError(f"{self.id} must contain findings")
        for finding in self.findings:
            finding.validate()
            if finding.effective_policy != "work-order":
                raise SystemPlanningError(
                    f"{self.id} contains a non-work-order finding"
                )
            if finding.target != self.target:
                raise SystemPlanningError(
                    f"{self.id} contains findings for another target"
                )
        contains_sensitive_material = any(
            finding.category == SENSITIVE_CATEGORY
            for finding in self.findings
        )
        if contains_sensitive_material != self.manual_only:
            raise SystemPlanningError(
                f"{self.id} has inconsistent sensitive-material routing"
            )
        if self.package_authority != any(
            finding.authority == "package" for finding in self.findings
        ):
            raise SystemPlanningError(
                f"{self.id} has inconsistent package-authority routing"
            )
        expected_id = _stable_id(
            "work-order",
            {
                "target": _normalized_target(self.target),
                "handling": self.handling,
                "finding_route_ids": [
                    finding.id for finding in self.findings
                ],
            },
        )
        if self.id != expected_id:
            raise SystemPlanningError(
                f"{self.id} does not match work order contents"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "handling": self.handling,
            "dispatchable": self.dispatchable,
            "manual_only": self.manual_only,
            "requires_sanitized_staging": self.requires_sanitized_staging,
            "content_policy": self.content_policy,
            "package_authority": self.package_authority,
            "findings": [finding.to_dict() for finding in self.findings],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SystemWorkOrder:
        fields = {
            "id",
            "target",
            "handling",
            "dispatchable",
            "manual_only",
            "requires_sanitized_staging",
            "content_policy",
            "package_authority",
            "findings",
        }
        _require_fields(value, fields, artifact="work order")
        result = cls(
            id=_require_string(value["id"], "work order id"),
            target=_require_string(value["target"], "work order target"),
            handling=_require_string(value["handling"], "handling"),
            dispatchable=_require_bool(value["dispatchable"], "dispatchable"),
            manual_only=_require_bool(value["manual_only"], "manual_only"),
            requires_sanitized_staging=_require_bool(
                value["requires_sanitized_staging"],
                "requires_sanitized_staging",
            ),
            content_policy=_require_string(
                value["content_policy"], "content_policy"
            ),
            package_authority=_require_bool(
                value["package_authority"], "package_authority"
            ),
            findings=tuple(
                RoutedFinding.from_dict(
                    _require_mapping(item, "work order finding")
                )
                for item in _require_array(value["findings"], "findings")
            ),
        )
        result.validate()
        return result


@dataclass(frozen=True)
class SystemPlan:
    """Serializable rollup plan partitioned into three exclusive routes."""

    schema_version: int
    apu_version: str
    created_at: str
    id: str
    inventory_sha256: str
    profile_sha256: str
    remediation_policy: Mapping[str, str]
    policy_sha256: str
    auto_operations: tuple[SystemAutoOperation, ...]
    work_orders: tuple[SystemWorkOrder, ...]
    ignored_findings: tuple[RoutedFinding, ...]

    def validate(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != SYSTEM_PLAN_SCHEMA_VERSION
        ):
            raise SystemPlanningError(
                f"unsupported system plan schema version: {self.schema_version}"
            )
        _require_string(self.apu_version, "apu_version")
        _require_string(self.created_at, "created_at")
        _validate_sha256(self.inventory_sha256, "inventory_sha256")
        _validate_sha256(self.profile_sha256, "profile_sha256")
        policy = _policy_map(self.remediation_policy)
        expected_policy_hash = sha256_json(policy)
        if self.policy_sha256 != expected_policy_hash:
            raise SystemPlanningError(
                "policy_sha256 does not match remediation_policy"
            )

        artifact_ids: set[str] = set()
        source_finding_ids: set[str] = set()
        for artifact in (*self.auto_operations, *self.work_orders):
            artifact.validate()
            if artifact.id in artifact_ids:
                raise SystemPlanningError(
                    f"duplicate system plan artifact id: {artifact.id}"
                )
            artifact_ids.add(artifact.id)
            findings = artifact.findings
            for finding in findings:
                overlap = source_finding_ids.intersection(finding.finding_ids)
                if overlap:
                    raise SystemPlanningError(
                        "source findings appear in multiple routes: "
                        + ", ".join(sorted(overlap))
                    )
                source_finding_ids.update(finding.finding_ids)
        for finding in self.ignored_findings:
            finding.validate()
            if finding.effective_policy != "ignore":
                raise SystemPlanningError(
                    f"{finding.id} is not an ignored finding"
                )
            overlap = source_finding_ids.intersection(finding.finding_ids)
            if overlap:
                raise SystemPlanningError(
                    "source findings appear in multiple routes: "
                    + ", ".join(sorted(overlap))
                )
            source_finding_ids.update(finding.finding_ids)

        expected_id = _derive_plan_id(
            inventory_sha256=self.inventory_sha256,
            profile_sha256=self.profile_sha256,
            policy_sha256=self.policy_sha256,
            auto_operations=self.auto_operations,
            work_orders=self.work_orders,
            ignored_findings=self.ignored_findings,
        )
        if self.id != expected_id:
            raise SystemPlanningError("system plan id does not match its contents")

    @property
    def artifact_sha256(self) -> str:
        return sha256_json(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "apu_version": self.apu_version,
            "created_at": self.created_at,
            "id": self.id,
            "inventory_sha256": self.inventory_sha256,
            "profile_sha256": self.profile_sha256,
            "remediation_policy": dict(self.remediation_policy),
            "policy_sha256": self.policy_sha256,
            "auto_operations": [
                operation.to_dict() for operation in self.auto_operations
            ],
            "work_orders": [
                work_order.to_dict() for work_order in self.work_orders
            ],
            "ignored_findings": [
                finding.to_dict() for finding in self.ignored_findings
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SystemPlan:
        fields = {
            "schema_version",
            "apu_version",
            "created_at",
            "id",
            "inventory_sha256",
            "profile_sha256",
            "remediation_policy",
            "policy_sha256",
            "auto_operations",
            "work_orders",
            "ignored_findings",
        }
        _require_fields(value, fields, artifact="system plan")
        schema_version = value["schema_version"]
        if not isinstance(schema_version, int) or isinstance(
            schema_version, bool
        ):
            raise SystemPlanningError("schema_version must be an integer")
        raw_policy = _require_mapping(
            value["remediation_policy"], "remediation_policy"
        )
        policy = _policy_map(
            {
                _require_string(key, "remediation policy category"): (
                    _require_string(item, f"remediation_policy.{key}")
                )
                for key, item in raw_policy.items()
            }
        )
        result = cls(
            schema_version=schema_version,
            apu_version=_require_string(value["apu_version"], "apu_version"),
            created_at=_require_string(value["created_at"], "created_at"),
            id=_require_string(value["id"], "id"),
            inventory_sha256=_require_string(
                value["inventory_sha256"], "inventory_sha256"
            ),
            profile_sha256=_require_string(
                value["profile_sha256"], "profile_sha256"
            ),
            remediation_policy=policy,
            policy_sha256=_require_string(
                value["policy_sha256"], "policy_sha256"
            ),
            auto_operations=tuple(
                SystemAutoOperation.from_dict(
                    _require_mapping(item, "auto operation")
                )
                for item in _require_array(
                    value["auto_operations"], "auto_operations"
                )
            ),
            work_orders=tuple(
                SystemWorkOrder.from_dict(
                    _require_mapping(item, "work order")
                )
                for item in _require_array(value["work_orders"], "work_orders")
            ),
            ignored_findings=tuple(
                RoutedFinding.from_dict(
                    _require_mapping(item, "ignored finding")
                )
                for item in _require_array(
                    value["ignored_findings"], "ignored_findings"
                )
            ),
        )
        result.validate()
        return result


@dataclass(frozen=True)
class _FindingSource:
    inventory_ref: str
    finding: Finding
    surface: InstructionSurface


def _all_sources(inventory: SystemInventory) -> tuple[_FindingSource, ...]:
    sources: list[_FindingSource] = []
    inventories = [("machine", inventory.machine_inventory)]
    inventories.extend(
        (f"repository:{item.repository}", item.inventory)
        for item in sorted(
            inventory.repositories,
            key=lambda item: (item.repository.casefold(), item.repository),
        )
    )
    for inventory_ref, child in inventories:
        surfaces = {surface.id: surface for surface in child.surfaces}
        for finding in sorted(child.findings, key=lambda item: item.id):
            surface = surfaces.get(finding.surface_id)
            if surface is None:
                raise SystemPlanningError(
                    f"finding references unknown surface: {finding.id}"
                )
            sources.append(
                _FindingSource(
                    inventory_ref=inventory_ref,
                    finding=finding,
                    surface=surface,
                )
            )
    return tuple(sources)


def _source_key(source: _FindingSource) -> tuple[str, str, str, str]:
    protected = (
        source.surface.sensitive
        or source.finding.category == SENSITIVE_CATEGORY
    )
    location = _safe_location(source.finding.location) if protected else dict(
        source.finding.location
    )
    summary = (
        "Sensitive material requires manual remediation."
        if source.finding.category == SENSITIVE_CATEGORY
        else source.finding.summary
    )
    return (
        _normalized_target(source.surface.path),
        source.finding.category,
        canonical_json(location),
        summary,
    )


def _safe_location(location: Mapping[str, Any]) -> dict[str, Any]:
    line = location.get("line")
    if isinstance(line, int) and not isinstance(line, bool) and line > 0:
        return {"line": line}
    return {}


def _requested_and_effective_policy(
    *,
    category: str,
    requested: str,
    authority: str,
    location: Mapping[str, Any],
    surface_sensitive: bool,
) -> tuple[str, str]:
    if category == SENSITIVE_CATEGORY:
        return "work-order", "sensitive-material-manual-only"
    if requested == "ignore":
        return "ignore", "profile-ignore"
    if surface_sensitive:
        return "work-order", "sensitive-surface-sanitized"
    if requested == "work-order":
        return "work-order", "profile-work-order"
    if authority == "package":
        return "work-order", "package-authority-no-direct-rewrite"
    line = location.get("line")
    if (
        category not in AUTO_REMOVABLE_CATEGORIES
        or not isinstance(line, int)
        or isinstance(line, bool)
        or line < 1
    ):
        return "work-order", "non-deterministic-remediation"
    return "auto", "profile-auto-deterministic"


def _route_sources(
    sources: tuple[_FindingSource, ...],
    policy: Mapping[str, str],
) -> tuple[RoutedFinding, ...]:
    grouped: dict[tuple[str, str, str, str], list[_FindingSource]] = {}
    for source in sources:
        grouped.setdefault(_source_key(source), []).append(source)

    routes: list[RoutedFinding] = []
    for key, members in sorted(grouped.items()):
        content_hashes = {
            member.surface.content_sha256 for member in members
        }
        if len(content_hashes) != 1:
            raise SystemPlanningError(
                f"surface has conflicting audited hashes: {members[0].surface.path}"
            )
        categories = {member.finding.category for member in members}
        locations = {
            canonical_json(member.finding.location) for member in members
        }
        if len(categories) != 1 or len(locations) != 1:
            raise SystemPlanningError("deduplicated findings are inconsistent")
        representatives = sorted(
            members,
            key=lambda item: (
                item.surface.authority != "package",
                item.surface.provider,
                item.surface.precedence,
                item.finding.id,
            ),
        )
        representative = representatives[0]
        authority = (
            "package"
            if any(item.surface.authority == "package" for item in members)
            else representative.surface.authority
        )
        category = representative.finding.category
        protected = (
            category == SENSITIVE_CATEGORY
            or any(item.surface.sensitive for item in members)
        )
        location = (
            _safe_location(representative.finding.location)
            if protected
            else dict(representative.finding.location)
        )
        surface_sensitive = any(item.surface.sensitive for item in members)
        if category == SENSITIVE_CATEGORY:
            summary = "Sensitive material requires manual remediation."
        elif surface_sensitive:
            summary = f"{category} requires remediation on a sensitive surface."
        else:
            summary = representative.finding.summary
        requested = policy.get(category, "work-order")
        effective, reason = _requested_and_effective_policy(
            category=category,
            requested=requested,
            authority=authority,
            location=location,
            surface_sensitive=surface_sensitive,
        )
        identity = {
            "target": key[0],
            "content_sha256": representative.surface.content_sha256,
            "category": category,
            "location": location,
            "summary": summary,
        }
        route = RoutedFinding(
            id=_stable_id("finding-route", identity),
            finding_ids=tuple(
                sorted({item.finding.id for item in members})
            ),
            inventory_refs=tuple(
                sorted({item.inventory_ref for item in members})
            ),
            surface_ids=tuple(
                sorted({item.surface.id for item in members})
            ),
            target=representative.surface.path,
            surface_content_sha256=representative.surface.content_sha256,
            authority=authority,
            scope=representative.surface.scope,
            category=category,
            severity=representative.finding.severity,
            confidence=representative.finding.confidence,
            analysis_method=representative.finding.analysis_method,
            location=location,
            evidence=tuple(
                sorted(
                    {
                        evidence
                        for item in members
                        for evidence in item.finding.evidence
                    }
                )
            )
            if not protected
            else (),
            summary=summary,
            requested_policy=requested,
            effective_policy=effective,
            routing_reason=reason,
            surface_sensitive=surface_sensitive,
        )
        route.validate()
        routes.append(route)
    return tuple(
        sorted(
            routes,
            key=lambda item: (
                _normalized_target(item.target),
                item.category,
                canonical_json(item.location),
                item.id,
            ),
        )
    )


def _build_auto_operations(
    routes: tuple[RoutedFinding, ...],
) -> tuple[SystemAutoOperation, ...]:
    grouped: dict[str, list[RoutedFinding]] = {}
    for route in routes:
        if route.effective_policy == "auto":
            grouped.setdefault(_normalized_target(route.target), []).append(route)

    operations: list[SystemAutoOperation] = []
    for _, findings in sorted(grouped.items()):
        hashes = {finding.surface_content_sha256 for finding in findings}
        authorities = {finding.authority for finding in findings}
        if len(hashes) != 1 or len(authorities) != 1:
            raise SystemPlanningError(
                f"auto batch has conflicting surface metadata: {findings[0].target}"
            )
        findings_tuple = tuple(
            sorted(findings, key=lambda finding: finding.id)
        )
        line_numbers = tuple(
            sorted(
                {
                    finding.location["line"]
                    for finding in findings_tuple
                    if isinstance(finding.location.get("line"), int)
                }
            )
        )
        identity = {
            "target": _normalized_target(findings_tuple[0].target),
            "precondition_sha256": findings_tuple[0].surface_content_sha256,
            "finding_route_ids": [
                finding.id for finding in findings_tuple
            ],
        }
        operation = SystemAutoOperation(
            id=_stable_id("system-op", identity),
            target=findings_tuple[0].target,
            precondition_sha256=findings_tuple[0].surface_content_sha256,
            authority=findings_tuple[0].authority,
            action="merge",
            strategy="remove-lines",
            line_numbers=line_numbers,
            findings=findings_tuple,
        )
        operation.validate()
        operations.append(operation)
    return tuple(
        sorted(
            operations,
            key=lambda item: (_normalized_target(item.target), item.id),
        )
    )


def _work_order_handling(finding: RoutedFinding) -> str:
    if finding.category == SENSITIVE_CATEGORY:
        return "manual-only"
    if finding.surface_sensitive:
        return "sanitized"
    return "standard"


def _build_work_orders(
    routes: tuple[RoutedFinding, ...],
) -> tuple[SystemWorkOrder, ...]:
    grouped: dict[tuple[str, str], list[RoutedFinding]] = {}
    for route in routes:
        if route.effective_policy == "work-order":
            key = (_normalized_target(route.target), _work_order_handling(route))
            grouped.setdefault(key, []).append(route)

    result: list[SystemWorkOrder] = []
    for (_, handling), findings in sorted(grouped.items()):
        findings_tuple = tuple(
            sorted(findings, key=lambda finding: finding.id)
        )
        manual_only = handling == "manual-only"
        identity = {
            "target": _normalized_target(findings_tuple[0].target),
            "handling": handling,
            "finding_route_ids": [
                finding.id for finding in findings_tuple
            ],
        }
        work_order = SystemWorkOrder(
            id=_stable_id("work-order", identity),
            target=findings_tuple[0].target,
            handling=handling,
            dispatchable=not manual_only,
            manual_only=manual_only,
            requires_sanitized_staging=handling == "sanitized",
            content_policy=(
                "location-line-content-hash-only"
                if handling in {"manual-only", "sanitized"}
                else "finding-evidence"
            ),
            package_authority=any(
                finding.authority == "package" for finding in findings_tuple
            ),
            findings=findings_tuple,
        )
        work_order.validate()
        result.append(work_order)
    return tuple(
        sorted(
            result,
            key=lambda item: (
                _normalized_target(item.target),
                item.handling,
                item.id,
            ),
        )
    )


def _derive_plan_id(
    *,
    inventory_sha256: str,
    profile_sha256: str,
    policy_sha256: str,
    auto_operations: tuple[SystemAutoOperation, ...],
    work_orders: tuple[SystemWorkOrder, ...],
    ignored_findings: tuple[RoutedFinding, ...],
) -> str:
    return _stable_id(
        "system-plan",
        {
            "inventory_sha256": inventory_sha256,
            "profile_sha256": profile_sha256,
            "policy_sha256": policy_sha256,
            "auto_operations": [
                operation.to_dict() for operation in auto_operations
            ],
            "work_orders": [
                work_order.to_dict() for work_order in work_orders
            ],
            "ignored_findings": [
                finding.to_dict() for finding in ignored_findings
            ],
        },
    )


def propose_system(
    inventory: SystemInventory,
    remediation_policy: Mapping[str, str],
    *,
    created_at: str,
) -> SystemPlan:
    """Partition a system inventory without generating or mutating files."""

    if inventory.schema_version != 1:
        raise SystemPlanningError(
            f"unsupported system inventory schema version: "
            f"{inventory.schema_version}"
        )
    _validate_sha256(inventory.profile_sha256, "profile_sha256")
    _require_string(created_at, "created_at")
    policy = _policy_map(remediation_policy)
    policy_sha256 = sha256_json(policy)
    routes = _route_sources(_all_sources(inventory), policy)
    auto_operations = _build_auto_operations(routes)
    work_orders = _build_work_orders(routes)
    ignored = tuple(
        route for route in routes if route.effective_policy == "ignore"
    )
    plan_id = _derive_plan_id(
        inventory_sha256=inventory.artifact_sha256,
        profile_sha256=inventory.profile_sha256,
        policy_sha256=policy_sha256,
        auto_operations=auto_operations,
        work_orders=work_orders,
        ignored_findings=ignored,
    )
    plan = SystemPlan(
        schema_version=SYSTEM_PLAN_SCHEMA_VERSION,
        apu_version=inventory.apu_version,
        created_at=created_at,
        id=plan_id,
        inventory_sha256=inventory.artifact_sha256,
        profile_sha256=inventory.profile_sha256,
        remediation_policy=policy,
        policy_sha256=policy_sha256,
        auto_operations=auto_operations,
        work_orders=work_orders,
        ignored_findings=ignored,
    )
    plan.validate()
    return plan


def load_system_plan(path: Path | str) -> SystemPlan:
    """Load and validate a persisted system-plan JSON artifact."""

    selected = Path(path)
    try:
        raw = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemPlanningError(
            f"cannot load system plan {selected}: {error}"
        ) from error
    return SystemPlan.from_dict(_require_mapping(raw, "system plan"))


propose_system_inventory = propose_system
RollupPlan = SystemPlan

from __future__ import annotations

import os
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Iterable

from apu.classify import AUTO_REMOVABLE_CATEGORIES
from apu.filesystem import hash_object
from apu.models import (
    Approval,
    Finding,
    InstructionSurface,
    Inventory,
    Plan,
    PlanOperation,
    derive_plan_status,
    sha256_bytes,
)
from apu.render import render_bytes


def _operation_id(finding: Finding, index: int) -> str:
    digest = sha256(f"{finding.id}\0{index}".encode()).hexdigest()[:12]
    return f"op-{digest}"


def propose_inventory(
    inventory: Inventory,
    *,
    created_at: str,
    candidate_dir: Path | None = None,
) -> Plan:
    surfaces = {surface.id: surface for surface in inventory.surfaces}
    operations: list[PlanOperation] = []
    # One file is one operation. The same path can be an effective surface for
    # more than one provider, and two operations writing one file would
    # conflict.
    grouped: dict[str, list[Finding]] = {}
    representatives: dict[str, InstructionSurface] = {}
    for finding in sorted(inventory.findings, key=lambda item: item.id):
        surface = surfaces.get(finding.surface_id)
        if surface is None:
            raise ValueError(f"finding references unknown surface: {finding.id}")
        existing = representatives.get(surface.path)
        if existing is None or (surface.provider, surface.precedence) < (
            existing.provider,
            existing.precedence,
        ):
            representatives[surface.path] = surface
        seen = grouped.setdefault(surface.path, [])
        if any(
            item.category == finding.category and item.location == finding.location
            for item in seen
        ):
            continue
        seen.append(finding)
    if candidate_dir is not None:
        candidate_dir.mkdir(parents=True, exist_ok=True)

    for index, (path, findings) in enumerate(sorted(grouped.items()), 1):
        surface = representatives[path]
        operation_id = _operation_id(findings[0], index)
        action = "preserve"
        strategy = "proposal_only"
        source: str | None = None
        proposed_sha256 = surface.content_sha256
        removable = [
            finding
            for finding in findings
            if finding.category in AUTO_REMOVABLE_CATEGORIES
            # Package files are replaced by their upstream on the next update,
            # so editing one silently loses the change and desyncs the copy.
            and surface.authority != "package"
        ]
        review_only = len(removable) != len(findings)
        if candidate_dir is not None and removable:
            target = Path(surface.path)
            original = target.read_bytes()
            if sha256_bytes(original) != surface.content_sha256:
                raise ValueError(f"surface changed after audit: {target}")
            removed_lines = {
                finding.location["line"]
                for finding in removable
                if isinstance(finding.location.get("line"), int)
            }
            rendered = b"".join(
                line
                for line_number, line in enumerate(
                    original.splitlines(keepends=True), 1
                )
                if line_number not in removed_lines
            )
            candidate = candidate_dir / f"{operation_id}.candidate"
            candidate.write_bytes(rendered)
            action = "merge"
            strategy = "full_file"
            source = str(candidate.resolve())
            proposed_sha256 = sha256_bytes(rendered)
        operations.append(
            PlanOperation(
                id=operation_id,
                action=action,
                target=surface.path,
                source=source,
                ownership=surface.authority,
                strategy=strategy,
                precondition_sha256=surface.content_sha256,
                proposed_sha256=proposed_sha256,
                backup_required=action != "preserve",
                requires_confirmation=review_only
                or any(finding.confidence != "high" for finding in findings),
                approval=Approval(),
                reason="; ".join(
                    f"{finding.category}: {finding.summary}" for finding in findings
                ),
                evidence=tuple(finding.id for finding in findings),
            )
        )
    plan = Plan(
        schema_version=1,
        apu_version=inventory.apu_version,
        created_at=created_at,
        inventory_sha256=inventory.artifact_sha256,
        status="draft",
        operations=tuple(operations),
        validation={
            "commands": [],
            "fixtures": [],
            "required": [],
            "protected_roots": list(inventory.scope.get("roots", ())),
        },
    )
    plan.validate()
    return plan


def approve_all_recommended(
    operations: Iterable[PlanOperation],
    *,
    recorded_at: str | None = None,
) -> tuple[PlanOperation, ...]:
    result: list[PlanOperation] = []
    for operation in operations:
        if (
            operation.is_mutating
            and operation.approval.status == "pending"
            and not operation.requires_confirmation
        ):
            result.append(
                replace(
                    operation,
                    approval=Approval(
                        status="approved",
                        recorded_at=recorded_at,
                        method="approve-recommended",
                    ),
                )
            )
        else:
            result.append(operation)
    return tuple(result)


def update_plan_status(plan: Plan) -> Plan:
    return replace(plan, status=derive_plan_status(plan.operations))


def build_relocation_operations(
    *,
    operation_id: str,
    source: Path,
    destination: Path,
    content_sha256: str,
    approval: Approval,
    evidence: tuple[str, ...] = (),
) -> tuple[PlanOperation, PlanOperation]:
    group = operation_id
    remove = PlanOperation(
        id=f"{operation_id}-remove",
        action="remove",
        target=str(source),
        source=None,
        ownership="repository",
        strategy="full_file",
        precondition_sha256=content_sha256,
        proposed_sha256=None,
        backup_required=True,
        requires_confirmation=True,
        approval=approval,
        reason=f"Relocate content to {destination}",
        evidence=evidence,
        atomic_group_id=group,
        group_content_sha256=content_sha256,
    )
    create = PlanOperation(
        id=f"{operation_id}-create",
        action="create",
        target=str(destination),
        source=str(source),
        ownership="repository",
        strategy="full_file",
        precondition_sha256=None,
        proposed_sha256=content_sha256,
        backup_required=False,
        requires_confirmation=True,
        approval=approval,
        reason=f"Relocate content from {source}",
        evidence=evidence,
        atomic_group_id=group,
        group_content_sha256=content_sha256,
    )
    return remove, create


def _tree_sha256(root: Path) -> str:
    return hash_object(root)


def build_skill_install_operations(
    *,
    package_skill: Path,
    home: Path,
    include_claude: bool,
    include_codex: bool = True,
    symlink_supported: bool = True,
    claude_marketplace_rendered: Path | None = None,
) -> tuple[PlanOperation, ...]:
    if not (package_skill / "SKILL.md").is_file():
        raise ValueError(f"canonical skill is missing SKILL.md: {package_skill}")
    content_hash = _tree_sha256(package_skill)
    targets: list[tuple[str, Path]] = []
    if include_codex:
        targets.append(
            (
                "codex",
                home / ".agents" / "skills" / "optimizing-agent-instructions",
            )
        )
    if include_claude:
        targets.append(
            (
                "claude",
                home / ".claude" / "skills" / "optimizing-agent-instructions",
            )
        )
    operations: list[PlanOperation] = []
    for provider, target in targets:
        if os.path.lexists(target):
            operations.append(
                PlanOperation(
                    id=f"install-skill-{provider}",
                    action="preserve",
                    target=str(target),
                    source=str(package_skill),
                    ownership="user",
                    strategy="proposal_only",
                    precondition_sha256=_path_sha256(target),
                    proposed_sha256=content_hash,
                    backup_required=False,
                    requires_confirmation=True,
                    approval=Approval(),
                    reason="Existing skill target requires explicit ownership review.",
                    evidence=(),
                )
            )
            continue
        action = "symlink" if symlink_supported else "create"
        operations.append(
            PlanOperation(
                id=f"install-skill-{provider}",
                action=action,
                target=str(target),
                source=str(package_skill),
                ownership="apu",
                strategy="sidecar" if symlink_supported else "full_file",
                precondition_sha256=None,
                proposed_sha256=content_hash,
                backup_required=False,
                requires_confirmation=True,
                approval=Approval(),
                reason=(
                    "Install the canonical optimizer skill by symlink."
                    if symlink_supported
                    else "Copy the canonical optimizer skill because symlinks "
                    "are unavailable; the fallback is explicit in this plan."
                ),
                evidence=(),
            )
        )
    if include_claude and claude_marketplace_rendered is not None:
        rendered = claude_marketplace_rendered.expanduser().resolve()
        if not rendered.is_file():
            raise ValueError(
                f"rendered Claude marketplace metadata is missing: {rendered}"
            )
        target = home / ".claude" / "plugins" / "known_marketplaces.json"
        exists = os.path.lexists(target)
        rendered_bytes = render_bytes(
            action="configure",
            strategy="managed_section",
            source=rendered.read_bytes(),
            current=target.read_bytes() if exists else None,
            target=target,
        )
        operations.append(
            PlanOperation(
                id="configure-skill-claude-marketplace",
                action="configure",
                target=str(target),
                source=str(rendered),
                ownership="user" if exists else "apu",
                strategy="managed_section",
                precondition_sha256=_path_sha256(target) if exists else None,
                proposed_sha256=sha256_bytes(rendered_bytes),
                backup_required=exists,
                requires_confirmation=True,
                approval=Approval(),
                reason="Point Claude marketplace metadata at the canonical source.",
                evidence=(),
            )
        )
    return tuple(operations)


def _path_sha256(path: Path) -> str:
    return hash_object(path)

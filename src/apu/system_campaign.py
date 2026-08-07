from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from .apply import apply_plan
from .campaigns import (
    CampaignLock,
    build_campaign_manifest,
    campaign_directory,
    create_campaign,
    load_campaign_index,
    load_campaign_manifest,
    read_campaign_lock_status,
    rebuild_campaign_index_locked,
    reconcile_campaign_locked,
    register_leaf_artifact,
)
from .models import (
    Approval,
    Plan,
    PlanOperation,
    derive_plan_status,
    sha256_bytes,
    sha256_json,
)
from .planning import approve_all_recommended, update_plan_status
from .receipts import load_receipt
from .snapshot_scope import snapshot_surfaces_for_profile
from .snapshots import create_snapshot, load_snapshot
from .state import ensure_private_directory, write_json_atomic
from .system_audit import SystemInventory
from .system_planning import SystemPlan, propose_system
from .system_profile import SystemProfile
from .work_orders import (
    GuidanceCitation,
    WorkOrderArtifact,
    WorkOrderFinding,
    export_work_order,
    find_secret_spans,
    render_work_order,
    sanitize_staged_files,
    verify_plan_candidate,
    write_redaction_map,
    write_work_order,
)

CAMPAIGN_BUNDLE_SCHEMA_VERSION = 1


def propose_campaign(
    state_home: Path,
    inventory: SystemInventory,
    profile: SystemProfile,
    *,
    created_at: str,
    baseline_version: str = "baseline-unconfigured",
    model_generation: str = "model-unverified",
    emit_prompts: Path | None = None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Create one immutable campaign and its private proposal artifacts."""

    if inventory.profile_sha256 != profile.artifact_sha256:
        raise ValueError("inventory profile hash does not match the selected profile")
    campaign_id = f"campaign-{uuid4().hex}"
    system_plan = propose_system(
        inventory,
        profile.remediation_policy,
        created_at=created_at,
    )
    _validate_system_plan_scope(system_plan, profile)
    _verify_current_plan_inputs(system_plan)
    root = campaign_directory(state_home, campaign_id)
    candidates = _render_auto_candidates(system_plan)
    auto_plan = _build_auto_plan(system_plan, root, profile, candidates)
    rendered_orders = tuple(
        _render_system_work_order(campaign_id, item)
        for item in system_plan.work_orders
    )
    plan_binding = {
        "system_plan_id": system_plan.id,
        "system_plan_sha256": system_plan.artifact_sha256,
        "auto_plan_sha256": sha256_json(auto_plan.to_dict()),
    }
    work_order_bindings = [
        {
            "work_order_id": artifact.work_order_id,
            "sha256": sha256_bytes(artifact.rendered.encode("utf-8")),
            "manual_only": artifact.manual_only,
            "dispatchable": artifact.dispatchable,
        }
        for artifact in rendered_orders
    ]
    bundle = {
        "schema_version": CAMPAIGN_BUNDLE_SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "system_plan": system_plan.to_dict(),
        "auto_plan": auto_plan.to_dict(),
        "campaign_directory": str(root),
        "work_orders": [
            {
                "work_order_id": artifact.work_order_id,
                "path": str(
                    root / "work-orders" / f"{artifact.work_order_id}.md"
                ),
                "manual_only": artifact.manual_only,
                "dispatchable": artifact.dispatchable,
                "requires_sanitized_stage": (
                    artifact.requires_sanitized_stage
                ),
            }
            for artifact in rendered_orders
        ],
    }
    _validate_bundle(bundle)
    _reject_public_secret_material(bundle, artifact="campaign bundle")
    manifest = build_campaign_manifest(
        campaign_id=campaign_id,
        inventory_hash=inventory.artifact_sha256,
        profile_hash=profile.artifact_sha256,
        baseline_version=baseline_version,
        model_generation=model_generation,
        plan_binding=plan_binding,
        work_order_bindings=work_order_bindings,
    )
    create_campaign(state_home, manifest)

    warnings: list[str] = []
    order_by_id = {item.id: item for item in system_plan.work_orders}
    with CampaignLock(state_home, campaign_id, purpose="proposal-finalize"):
        index = load_campaign_index(state_home, campaign_id)
        write_json_atomic(root / "system-plan.json", system_plan.to_dict())
        write_json_atomic(root / "auto-plan.json", auto_plan.to_dict())
        _write_auto_candidates(auto_plan, candidates)
        for artifact in rendered_orders:
            path = write_work_order(root, artifact)
            source_order = order_by_id[artifact.work_order_id]
            if source_order.requires_sanitized_staging:
                _write_sanitized_stage(root, source_order)
            register_leaf_artifact(
                state_home,
                campaign_id,
                {
                    "schema_version": 1,
                    "campaign_id": campaign_id,
                    "artifact_type": "work-order",
                    "artifact_id": artifact.work_order_id,
                    "snapshot_id": None,
                    "path": str(path.relative_to(root)),
                    "sha256": sha256_bytes(artifact.rendered.encode("utf-8")),
                    "manual_only": artifact.manual_only,
                    "dispatchable": artifact.dispatchable,
                },
            )

        register_leaf_artifact(
            state_home,
            campaign_id,
            {
                "schema_version": 1,
                "campaign_id": campaign_id,
                "artifact_type": "plan",
                "artifact_id": system_plan.id,
                "snapshot_id": None,
                "system_plan_sha256": system_plan.artifact_sha256,
                "auto_plan_sha256": sha256_json(auto_plan.to_dict()),
            },
        )
        write_json_atomic(root / "bundle.json", bundle)
        reconcile_campaign_locked(
            state_home,
            campaign_id,
            expected_revision=index["revision"],
        )

    if emit_prompts is not None:
        for artifact in rendered_orders:
            exported = export_work_order(artifact, emit_prompts)
            warnings.append(exported.warning)
    return bundle, tuple(warnings)


def load_campaign_bundle(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid campaign bundle at {path}: {error}") from error
    _validate_bundle(value)
    return value


def campaign_has_work_orders(
    state_home: Path,
    bundle: dict[str, Any],
) -> bool:
    """Return queued state from the immutable, state-owned system plan."""

    _manifest, system_plan, _auto_plan = _load_bound_campaign_plans(
        state_home,
        bundle,
    )
    return bool(system_plan.work_orders)


def apply_campaign(
    state_home: Path,
    bundle: dict[str, Any],
    profile: SystemProfile,
    *,
    installation_id: str,
) -> dict[str, Any]:
    """Apply the deterministic section with snapshot-before-mutation binding."""

    _validate_bundle(bundle)
    campaign_id = bundle["campaign_id"]

    with CampaignLock(state_home, campaign_id, purpose="system-apply"):
        manifest, system_plan, auto_plan = _load_bound_campaign_plans(
            state_home,
            bundle,
        )
        if manifest["profile_hash"] != profile.artifact_sha256:
            raise ValueError(
                "campaign profile hash does not match the selected profile"
            )
        _validate_system_plan_scope(system_plan, profile)
        approved = replace(
            auto_plan,
            operations=approve_all_recommended(
                auto_plan.operations,
                recorded_at=system_plan.created_at,
            ),
        )
        approved = update_plan_status(approved)
        executable = approved.executable_operations()
        if not executable:
            return {
                "campaign_id": campaign_id,
                "status": "no-auto-operations",
                "snapshot_id": None,
                "receipt": None,
            }

        index = load_campaign_index(state_home, campaign_id)
        snapshot_id = index["snapshot_id"]
        if snapshot_id is None:
            surfaces, inventory = snapshot_surfaces_for_profile(profile)
            if inventory.profile_sha256 != profile.artifact_sha256:
                raise ValueError("snapshot inventory profile hash changed")
            snapshot = create_snapshot(
                state_home,
                surfaces,
                campaign_id=campaign_id,
                label=f"before {campaign_id}",
            )
            snapshot_id = snapshot["snapshot_id"]
            register_leaf_artifact(
                state_home,
                campaign_id,
                {
                    "schema_version": 1,
                    "campaign_id": campaign_id,
                    "artifact_type": "snapshot",
                    "artifact_id": snapshot_id,
                    "snapshot_id": snapshot_id,
                },
            )
            index = reconcile_campaign_locked(
                state_home,
                campaign_id,
                expected_revision=index["revision"],
            )

        snapshot = load_snapshot(state_home, snapshot_id, verify_blobs=True)
        if snapshot["campaign_id"] != campaign_id:
            raise ValueError(
                "campaign snapshot is not reciprocally bound to the campaign"
            )
        _verify_snapshot_coverage(snapshot, executable)
        receipt_path = apply_plan(
            approved,
            state_home=state_home,
            installation_id=installation_id,
            confirmed=True,
            campaign_id=campaign_id,
            snapshot_id=snapshot_id,
        )
        receipt = load_receipt(receipt_path)
        register_leaf_artifact(
            state_home,
            campaign_id,
            {
                "schema_version": 1,
                "campaign_id": campaign_id,
                "artifact_type": "receipt",
                "artifact_id": installation_id,
                "snapshot_id": snapshot_id,
                "idempotency_key": {
                    "operation_id": "system-apply",
                    "attempt": 1,
                },
                "receipt_path": str(receipt_path),
                "receipt_sha256": sha256_json(receipt),
            },
        )
        reconcile_campaign_locked(
            state_home,
            campaign_id,
            expected_revision=index["revision"],
        )
    return {
        "campaign_id": campaign_id,
        "status": "applied",
        "snapshot_id": snapshot_id,
        "receipt": str(receipt_path),
    }


def list_campaign_status(state_home: Path) -> list[dict[str, Any]]:
    root = Path(state_home) / "campaigns"
    if not root.exists():
        return []
    result: list[dict[str, Any]] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        try:
            manifest = load_campaign_manifest(state_home, directory.name)
            index: dict[str, Any] | None = None
            try:
                with CampaignLock(
                    state_home,
                    directory.name,
                    purpose="status-reconcile",
                ):
                    _attach_orphan_receipts(state_home, directory.name)
                    try:
                        index = load_campaign_index(state_home, directory.name)
                    except FileNotFoundError:
                        index = rebuild_campaign_index_locked(
                            state_home,
                            directory.name,
                        )
                    index = reconcile_campaign_locked(
                        state_home,
                        directory.name,
                        expected_revision=index["revision"],
                    )
                error = None
            except RuntimeError as reconcile_error:
                error = str(reconcile_error)
                index = load_campaign_index(state_home, directory.name)
            if index is None:
                raise ValueError("campaign status could not load its progress index")
            result.append(
                {
                    "campaign_id": directory.name,
                    "inventory_hash": manifest["inventory_hash"],
                    "profile_hash": manifest["profile_hash"],
                    "baseline_version": manifest["baseline_version"],
                    "model_generation": manifest["model_generation"],
                    "revision": index["revision"],
                    "snapshot_id": index["snapshot_id"],
                    "artifacts": index["artifacts"],
                    "reconcile_error": error,
                    "lock": read_campaign_lock_status(
                        state_home,
                        directory.name,
                    ),
                }
            )
        except (OSError, ValueError, TypeError) as error:
            result.append(
                {"campaign_id": directory.name, "error": str(error)}
            )
    return result


def _attach_orphan_receipts(state_home: Path, campaign_id: str) -> None:
    installations = Path(state_home) / "installations"
    if not installations.exists():
        return
    for path in sorted(installations.glob("*/receipt.json")):
        try:
            receipt = load_receipt(path)
        except (OSError, ValueError):
            continue
        if receipt.get("campaign_id") != campaign_id:
            continue
        register_leaf_artifact(
            state_home,
            campaign_id,
            {
                "schema_version": 1,
                "campaign_id": campaign_id,
                "artifact_type": "receipt",
                "artifact_id": receipt["installation_id"],
                "snapshot_id": receipt["snapshot_id"],
                "idempotency_key": {
                    "operation_id": "system-apply",
                    "attempt": 1,
                },
                "receipt_path": str(path),
                "receipt_sha256": sha256_json(receipt),
            },
        )


def _load_bound_campaign_plans(
    state_home: Path,
    bundle: dict[str, Any],
) -> tuple[dict[str, Any], SystemPlan, Plan]:
    """Load only state-owned plans and prove their immutable bindings."""

    _validate_bundle(bundle)
    campaign_id = bundle["campaign_id"]
    root = campaign_directory(state_home, campaign_id)
    manifest = load_campaign_manifest(state_home, campaign_id)
    binding = manifest["plan_binding"]
    if not isinstance(binding, dict) or set(binding) != {
        "system_plan_id",
        "system_plan_sha256",
        "auto_plan_sha256",
    }:
        raise ValueError("campaign plan binding has unsupported fields")

    system_plan_value = _read_json_object(root / "system-plan.json")
    auto_plan_value = _read_json_object(root / "auto-plan.json")
    system_plan = SystemPlan.from_dict(system_plan_value)
    auto_plan = Plan.from_dict(auto_plan_value)
    auto_plan.validate()
    if binding["system_plan_id"] != system_plan.id:
        raise ValueError("campaign system plan id does not match its binding")
    if binding["system_plan_sha256"] != system_plan.artifact_sha256:
        raise ValueError("campaign system plan hash does not match its binding")
    if binding["auto_plan_sha256"] != sha256_json(auto_plan.to_dict()):
        raise ValueError("campaign auto plan hash does not match its binding")
    if manifest["inventory_hash"] != system_plan.inventory_sha256:
        raise ValueError("campaign inventory hash does not match its system plan")
    if manifest["profile_hash"] != system_plan.profile_sha256:
        raise ValueError("campaign profile hash does not match its system plan")
    if auto_plan.inventory_sha256 != system_plan.inventory_sha256:
        raise ValueError("campaign auto plan inventory binding does not match")
    if SystemPlan.from_dict(bundle["system_plan"]).artifact_sha256 != (
        system_plan.artifact_sha256
    ):
        raise ValueError("supplied system plan differs from the campaign binding")
    if sha256_json(Plan.from_dict(bundle["auto_plan"]).to_dict()) != sha256_json(
        auto_plan.to_dict()
    ):
        raise ValueError("supplied auto plan differs from the campaign binding")
    if _path_identity(Path(bundle["campaign_directory"])) != _path_identity(root):
        raise ValueError("campaign directory does not match canonical APU state")

    system_operations = {
        operation.id: operation for operation in system_plan.auto_operations
    }
    if set(system_operations) != {operation.id for operation in auto_plan.operations}:
        raise ValueError("campaign auto operations do not match the system plan")
    for operation in auto_plan.operations:
        source = Path(operation.source or "")
        expected_source = root / "candidates" / f"{operation.id}.candidate"
        if _path_identity(source) != _path_identity(expected_source):
            raise ValueError(
                f"campaign operation {operation.id} has a non-canonical candidate"
            )
        system_operation = system_operations[operation.id]
        if (
            _path_identity(Path(operation.target))
            != _path_identity(Path(system_operation.target))
            or operation.precondition_sha256
            != system_operation.precondition_sha256
        ):
            raise ValueError(
                f"campaign operation {operation.id} differs from its system route"
            )
        try:
            candidate = source.read_bytes()
        except OSError as error:
            raise ValueError(
                f"campaign candidate is unavailable for {operation.id}: {error}"
            ) from error
        if sha256_bytes(candidate) != operation.proposed_sha256:
            raise ValueError(
                f"campaign candidate hash changed for {operation.id}"
            )

    _verify_work_order_bindings(manifest, system_plan, root)
    return manifest, system_plan, auto_plan


def _verify_work_order_bindings(
    manifest: dict[str, Any],
    system_plan: SystemPlan,
    root: Path,
) -> None:
    expected = {item.id: item for item in system_plan.work_orders}
    bindings = manifest["work_order_bindings"]
    if not isinstance(bindings, list):
        raise TypeError("campaign work-order bindings must be a list")
    observed: set[str] = set()
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != {
            "work_order_id",
            "sha256",
            "manual_only",
            "dispatchable",
        }:
            raise ValueError("campaign work-order binding has unsupported fields")
        work_order_id = binding["work_order_id"]
        if work_order_id in observed or work_order_id not in expected:
            raise ValueError("campaign work-order bindings do not match the plan")
        observed.add(work_order_id)
        planned = expected[work_order_id]
        if (
            binding["manual_only"] != planned.manual_only
            or binding["dispatchable"] != planned.dispatchable
        ):
            raise ValueError(
                f"campaign work-order flags changed for {work_order_id}"
            )
        path = root / "work-orders" / f"{work_order_id}.md"
        try:
            rendered = path.read_bytes()
        except OSError as error:
            raise ValueError(
                f"campaign work order is unavailable for {work_order_id}: {error}"
            ) from error
        if sha256_bytes(rendered) != binding["sha256"]:
            raise ValueError(
                f"campaign work-order hash changed for {work_order_id}"
            )
    if observed != set(expected):
        raise ValueError("campaign work-order bindings do not match the plan")


def _validate_system_plan_scope(
    system_plan: SystemPlan,
    profile: SystemProfile,
) -> None:
    targets = [
        *(operation.target for operation in system_plan.auto_operations),
        *(work_order.target for work_order in system_plan.work_orders),
    ]
    for target in targets:
        if not _target_is_in_profile(Path(target), profile):
            raise ValueError(f"planned target is outside the selected profile: {target}")


def _verify_current_plan_inputs(system_plan: SystemPlan) -> None:
    hashes_by_target: dict[str, tuple[Path, set[str]]] = {}
    artifacts = (*system_plan.auto_operations, *system_plan.work_orders)
    for artifact in artifacts:
        identity = _path_identity(Path(artifact.target))
        path, hashes = hashes_by_target.setdefault(
            identity,
            (Path(artifact.target), set()),
        )
        hashes.update(
            finding.surface_content_sha256 for finding in artifact.findings
        )
    for path, hashes in hashes_by_target.values():
        if len(hashes) != 1:
            raise ValueError(f"planned surface has conflicting hashes: {path}")
        try:
            content = path.read_bytes()
        except OSError as error:
            raise ValueError(f"planned surface is unavailable: {path}: {error}") from error
        if sha256_bytes(content) != next(iter(hashes)):
            raise ValueError(f"planned surface changed after audit: {path}")


def _verify_snapshot_coverage(
    snapshot: dict[str, Any],
    operations: tuple[PlanOperation, ...],
) -> None:
    entries: dict[str, dict[str, Any]] = {}
    for entry in snapshot["entries"]:
        identity = _path_identity(Path(entry["target_path"]))
        if identity in entries:
            raise ValueError(
                f"campaign snapshot has ambiguous target coverage: "
                f"{entry['target_path']}"
            )
        entries[identity] = entry
    for operation in operations:
        entry = entries.get(_path_identity(Path(operation.target)))
        if entry is None:
            raise ValueError(
                f"campaign snapshot does not cover operation {operation.id}"
            )
        if entry["object_type"] != "file":
            raise ValueError(
                f"campaign snapshot target is not a file for {operation.id}"
            )
        if entry["hash"] != operation.precondition_sha256:
            raise ValueError(
                f"campaign snapshot hash does not match operation {operation.id}"
            )


def _profile_boundaries(profile: SystemProfile) -> list[str]:
    values = [Path(root.path) for root in profile.roots]
    values.extend(
        path
        for path in (Path(value) for value in profile.global_surfaces)
        if path.is_dir()
    )
    return [
        str(path.resolve(strict=False))
        for path in sorted(values, key=lambda item: _path_identity(item))
    ]


def _target_is_in_profile(target: Path, profile: SystemProfile) -> bool:
    resolved = target.resolve(strict=False)
    for root in profile.roots:
        if _path_is_within(resolved, Path(root.path).resolve(strict=False)):
            return True
    for raw in profile.global_surfaces:
        surface = Path(raw).resolve(strict=False)
        if _path_identity(resolved) == _path_identity(surface):
            return True
        if surface.is_dir() and _path_is_within(resolved, surface):
            return True
    return False


def _path_is_within(candidate: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            (_path_identity(candidate), _path_identity(root))
        ) == _path_identity(root)
    except ValueError:
        return False


def _path_identity(path: Path) -> str:
    return os.path.normcase(
        os.path.normpath(str(path.expanduser().resolve(strict=False)))
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid campaign artifact at {path}: {error}") from error
    if not isinstance(value, dict):
        raise TypeError(f"campaign artifact must be an object: {path}")
    return value


def _reject_public_secret_material(value: Any, *, artifact: str) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _reject_public_secret_material(item, artifact=artifact)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_public_secret_material(item, artifact=artifact)
        return
    if isinstance(value, str) and find_secret_spans(value):
        raise ValueError(f"{artifact} contains credential-shaped material")


def _build_auto_plan(
    system_plan: SystemPlan,
    root: Path,
    profile: SystemProfile,
    candidates: dict[str, bytes],
) -> Plan:
    operations: list[PlanOperation] = []
    for operation in system_plan.auto_operations:
        candidate = root / "candidates" / f"{operation.id}.candidate"
        rendered = candidates[operation.id]
        operations.append(
            PlanOperation(
                id=operation.id,
                action="merge",
                target=operation.target,
                source=str(candidate),
                ownership=operation.authority,
                strategy="full_file",
                precondition_sha256=operation.precondition_sha256,
                proposed_sha256=sha256_bytes(rendered),
                backup_required=True,
                requires_confirmation=False,
                approval=Approval(),
                reason="; ".join(
                    f"{finding.category}: {finding.summary}"
                    for finding in operation.findings
                ),
                evidence=tuple(
                    finding_id
                    for finding in operation.findings
                    for finding_id in finding.finding_ids
                ),
            )
        )
    status = derive_plan_status(tuple(operations))
    plan = Plan(
        schema_version=1,
        apu_version=system_plan.apu_version,
        created_at=system_plan.created_at,
        inventory_sha256=system_plan.inventory_sha256,
        status=status,
        operations=tuple(operations),
        validation={
            "commands": ["apu validate --plan AUTO_PLAN"],
            "fixtures": [],
            "required": ["structural"],
            "protected_roots": _profile_boundaries(profile),
        },
    )
    plan.validate()
    return plan


def _render_auto_candidates(system_plan: SystemPlan) -> dict[str, bytes]:
    return {
        operation.id: _render_line_removal(
            operation.target,
            operation.line_numbers,
            operation.precondition_sha256,
        )
        for operation in system_plan.auto_operations
    }


def _write_auto_candidates(
    auto_plan: Plan,
    candidates: dict[str, bytes],
) -> None:
    for operation in auto_plan.operations:
        source = Path(operation.source or "")
        rendered = candidates[operation.id]
        if sha256_bytes(rendered) != operation.proposed_sha256:
            raise ValueError(f"candidate hash changed for {operation.id}")
        _write_bytes_atomic(source, rendered)


def _render_line_removal(
    target: str,
    lines: tuple[int, ...],
    expected_sha256: str,
) -> bytes:
    path = Path(target)
    content = path.read_bytes()
    if sha256_bytes(content) != expected_sha256:
        raise ValueError(f"planned surface changed after audit: {path}")
    line_set = set(lines)
    return b"".join(
        line
        for number, line in enumerate(content.splitlines(keepends=True), 1)
        if number not in line_set
    )


def _render_system_work_order(
    campaign_id: str,
    work_order: Any,
) -> WorkOrderArtifact:
    findings: list[WorkOrderFinding] = []
    for finding in work_order.findings:
        line = finding.location.get("line")
        line_number = line if isinstance(line, int) and line > 0 else 1
        offending_text = None
        if not finding.surface_sensitive and finding.category != "sensitive-material-exposure":
            offending_text = _read_line(Path(finding.target), line_number)
        findings.append(
            WorkOrderFinding(
                id=finding.id,
                category=finding.category,
                path=finding.target,
                line=line_number,
                content_sha256=finding.surface_content_sha256,
                summary=finding.summary,
                offending_text=offending_text,
                surface_sensitive=finding.surface_sensitive,
            )
        )
    constraints = [
        "Keep the remediation scoped to the listed findings.",
        "Do not weaken defect detection or validation.",
    ]
    if work_order.package_authority:
        constraints.append(
            "Do not edit the package-owned target directly; propose an upstream "
            "upgrade, pin, fork, or package-management remediation."
        )
    return render_work_order(
        campaign_id=campaign_id,
        work_order_id=work_order.id,
        findings=findings,
        guidance=(
            GuidanceCitation(
                guidance=(
                    "Preserve user authority and route every mutation through "
                    "plan, approval, receipt, and rollback."
                ),
                source="roadmap.md",
                locator="Work-order prompt",
            ),
        ),
        constraints=tuple(constraints),
        acceptance_criteria=(
            "The returned candidate resolves every listed finding.",
            "No unrelated live surface is changed.",
        ),
        validation_steps=(
            "Run `apu validate --plan PLAN_CANDIDATE`.",
            "Run the category-specific behavioral fixture when available.",
        ),
    )


def _write_sanitized_stage(root: Path, work_order: Any) -> None:
    target = Path(work_order.target)
    raw = target.read_bytes()
    expected_hashes = {
        finding.surface_content_sha256 for finding in work_order.findings
    }
    if len(expected_hashes) != 1:
        raise ValueError(f"sensitive surface has conflicting hashes: {target}")
    expected = next(iter(expected_hashes))
    if sha256_bytes(raw) != expected:
        raise ValueError(f"sensitive surface changed after audit: {target}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"sensitive surface is not UTF-8 text: {target}") from error
    raw_spans = find_secret_spans(text)
    if not raw_spans:
        raise ValueError(
            f"sensitive surface has no safely recognized credential spans: {target}"
        )
    stage = sanitize_staged_files({str(target): text})
    if len(stage.redactions.entries) != len(raw_spans):
        raise ValueError(f"sensitive surface redaction map is incomplete: {target}")
    if any(find_secret_spans(content) for content in stage.files.values()):
        raise ValueError(f"sensitive surface remains secret-bearing: {target}")
    for entry in stage.redactions.entries:
        if any(entry.original_value in content for content in stage.files.values()):
            raise ValueError(f"sensitive value survived staging: {target}")
    verification = verify_plan_candidate(stage.files, stage.redactions)
    if (
        not verification.accepted
        or verification.materialized_files != {str(target): text}
    ):
        raise ValueError(f"sensitive stage failed structural verification: {target}")
    write_redaction_map(root, work_order.id, stage.redactions)
    directory = ensure_private_directory(root / "staging")
    write_json_atomic(
        directory / f"{work_order.id}.json",
        {"schema_version": 1, "files": dict(stage.files)},
    )


def _read_line(path: Path, number: int) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    return lines[number - 1] if number <= len(lines) else None


def _write_bytes_atomic(path: Path, content: bytes) -> Path:
    ensure_private_directory(path.parent)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _validate_bundle(value: Any) -> None:
    if not isinstance(value, dict):
        raise TypeError("campaign bundle must be an object")
    required = {
        "schema_version",
        "campaign_id",
        "system_plan",
        "auto_plan",
        "campaign_directory",
        "work_orders",
    }
    if set(value) != required:
        raise ValueError("campaign bundle fields do not match its schema")
    if value["schema_version"] != CAMPAIGN_BUNDLE_SCHEMA_VERSION:
        raise ValueError("unsupported campaign bundle schema_version")
    if not isinstance(value["campaign_id"], str) or not value["campaign_id"]:
        raise ValueError("campaign_id is required")
    system_plan = SystemPlan.from_dict(value["system_plan"])
    plan = Plan.from_dict(value["auto_plan"])
    plan.validate()
    if plan.inventory_sha256 != system_plan.inventory_sha256:
        raise ValueError("bundle auto plan inventory binding does not match")
    if not isinstance(value["campaign_directory"], str):
        raise TypeError("campaign_directory must be a path")
    if not isinstance(value["work_orders"], list):
        raise TypeError("work_orders must be a list")
    planned_orders = {item.id: item for item in system_plan.work_orders}
    observed: set[str] = set()
    for item in value["work_orders"]:
        if not isinstance(item, dict) or set(item) != {
            "work_order_id",
            "path",
            "manual_only",
            "dispatchable",
            "requires_sanitized_stage",
        }:
            raise ValueError("bundle work-order fields do not match its schema")
        work_order_id = item["work_order_id"]
        if (
            not isinstance(work_order_id, str)
            or work_order_id in observed
            or work_order_id not in planned_orders
        ):
            raise ValueError("bundle work orders do not match the system plan")
        observed.add(work_order_id)
        planned = planned_orders[work_order_id]
        if not isinstance(item["path"], str) or not item["path"]:
            raise ValueError("bundle work-order path is required")
        if (
            item["manual_only"] != planned.manual_only
            or item["dispatchable"] != planned.dispatchable
            or item["requires_sanitized_stage"]
            != planned.requires_sanitized_staging
        ):
            raise ValueError(
                f"bundle work-order flags changed for {work_order_id}"
            )
    if observed != set(planned_orders):
        raise ValueError("bundle work orders do not match the system plan")

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .campaigns import (
    CampaignLock,
    campaign_directory,
    load_campaign_index,
    load_campaign_manifest,
    reconcile_campaign_locked,
    register_leaf_artifact,
    scan_leaf_artifacts,
)
from .models import (
    Approval,
    Plan,
    PlanOperation,
    canonical_json,
    sha256_bytes,
    sha256_json,
)
from .snapshots import create_snapshot, load_snapshot
from .state import ensure_private_directory, write_json_atomic
from .system_planning import SystemPlan, SystemWorkOrder
from .work_orders import RedactionMap, load_redaction_map, verify_plan_candidate


class DispatchError(RuntimeError):
    """Base class for fail-closed dispatch errors."""


class DispatchRejectedError(DispatchError):
    """Raised when a work order is not eligible for automated dispatch."""


class DispatchUnavailableError(DispatchError):
    """Raised when enforceable isolation is unavailable."""


class DispatchSecurityError(DispatchError):
    """Raised when dispatch observes a containment contract violation."""


@dataclass(frozen=True)
class IsolationProbeRequest:
    stage_root: Path
    probe_target: Path
    live_root: Path


@dataclass(frozen=True)
class IsolationProbeResult:
    context_id: str
    mechanism: str
    attempted: bool
    write_denied: bool


@dataclass(frozen=True)
class RunnerRequest:
    work_order: str
    stage_root: Path
    staged_files: Mapping[str, Path]
    isolation_context_id: str


@dataclass(frozen=True)
class DispatchResult:
    status: str
    campaign_id: str
    work_order_id: str
    snapshot_id: str
    artifact_path: Path
    plan_path: Path | None
    reasons: tuple[str, ...] = ()


IsolationProbe = Callable[[IsolationProbeRequest], IsolationProbeResult]
DispatchRunner = Callable[[RunnerRequest], Mapping[str, Any]]


def dispatch_work_order(
    state_home: Path,
    campaign_id: str,
    work_order_id: str,
    *,
    runner: DispatchRunner,
    isolation_probe: IsolationProbe,
    snapshot_id: str | None = None,
    attempt: int = 1,
    created_at: str | None = None,
) -> DispatchResult:
    """Run one verified work order against a disposable, confined stage.

    The runner's only accepted output is ``{"files": {absolute_target: text}}``.
    Rejected output is represented by hashes and reason codes only.
    """

    if not callable(runner) or not callable(isolation_probe):
        raise TypeError("runner and isolation_probe must be callable")
    _component(campaign_id, "campaign_id")
    _component(work_order_id, "work_order_id")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    timestamp = created_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    operation_id = f"dispatch-{work_order_id}"
    idempotency_key = {"operation_id": operation_id, "attempt": attempt}

    state_root = Path(state_home).expanduser().resolve()
    with CampaignLock(state_root, campaign_id, purpose="dispatch"):
        existing = _existing_result(
            state_root,
            campaign_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return _result_from_leaf(state_root, campaign_id, existing)

        contract = _load_dispatch_contract(
            state_root,
            campaign_id,
            work_order_id,
        )
        order: SystemWorkOrder = contract["order"]
        if order.manual_only or not order.dispatchable:
            raise DispatchRejectedError(
                f"work order {work_order_id} is manual-only and cannot be dispatched"
            )

        live_target = Path(order.target).expanduser().resolve(strict=True)
        live_bytes = _read_stable_file(live_target)
        expected_hashes = {finding.surface_content_sha256 for finding in order.findings}
        if len(expected_hashes) != 1 or sha256_bytes(live_bytes) not in expected_hashes:
            raise DispatchRejectedError(
                f"work order target bytes no longer match {work_order_id}"
            )
        try:
            live_text = live_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DispatchRejectedError(
                f"work order target is not UTF-8 text: {live_target}"
            ) from error

        index = load_campaign_index(state_root, campaign_id)
        bound_snapshot = _bind_snapshot(
            state_root,
            campaign_id,
            order,
            system_plan=contract["system_plan"],
            index=index,
            requested_snapshot_id=snapshot_id,
            created_at=timestamp,
        )
        index = load_campaign_index(state_root, campaign_id)

        staged_text, redactions = _load_stage_input(
            contract["root"],
            order,
            live_text=live_text,
        )
        with _temporary_dispatch_stage(
            prefix=(
                "apu-dispatch-"
                + sha256_json({"work_order_id": work_order_id})[:12]
                + f"-a{attempt}-"
            ),
        ) as stage_root:
            ensure_private_directory(stage_root)
            staged_file = stage_root / "files" / "target.txt"
            _write_bytes_once(staged_file, staged_text.encode("utf-8"))
            stage_manifest = {
                "schema_version": 1,
                "work_order_id": work_order_id,
                "files": [
                    {
                        "logical_path": str(live_target),
                        "stage_path": "files/target.txt",
                        "source_sha256": sha256_bytes(live_bytes),
                        "staged_sha256": sha256_bytes(staged_text.encode("utf-8")),
                        "sanitized": order.requires_sanitized_staging,
                    }
                ],
            }
            write_json_atomic(stage_root / "manifest.json", stage_manifest)
            stage_before = _tree_identity(stage_root)

            live_before_probe = sha256_bytes(_read_stable_file(live_target))
            try:
                probe = isolation_probe(
                    IsolationProbeRequest(
                        stage_root=stage_root,
                        probe_target=live_target,
                        live_root=live_target.parent,
                    )
                )
            except Exception as error:
                raise DispatchUnavailableError(
                    f"isolation probe failed: {type(error).__name__}"
                ) from error
            live_after_probe = sha256_bytes(_read_stable_file(live_target))
            _validate_probe(
                probe,
                before_sha256=live_before_probe,
                after_sha256=live_after_probe,
            )
            if _tree_identity(stage_root) != stage_before:
                raise DispatchSecurityError(
                    "isolation probe changed the dispatch stage"
                )

            try:
                returned = runner(
                    RunnerRequest(
                        work_order=contract["rendered"],
                        stage_root=stage_root,
                        staged_files={str(live_target): staged_file},
                        isolation_context_id=probe.context_id,
                    )
                )
            except DispatchUnavailableError:
                raise
            # The injected runner is an external boundary; all of its failures
            # become content-free quarantine records.
            except Exception as error:  # noqa: BLE001
                return _persist_quarantine(
                    state_root,
                    campaign_id,
                    work_order_id,
                    snapshot_id=bound_snapshot,
                    idempotency_key=idempotency_key,
                    expected_revision=index["revision"],
                    created_at=timestamp,
                    work_order_sha256=contract["work_order_sha256"],
                    stage_manifest=stage_manifest,
                    probe=probe,
                    candidate_sha256=None,
                    reasons=(f"runner-failed:{type(error).__name__}",),
                )

            reasons: list[str] = []
            if sha256_bytes(_read_stable_file(live_target)) != live_before_probe:
                reasons.append("live-root-mutated-during-runner")
            if _tree_identity(stage_root) != stage_before:
                reasons.append("stage-mutated-during-runner")
            candidate_hash = _safe_json_hash(returned)
            candidate_files = _candidate_files(
                returned,
                expected_files={str(live_target)},
                reasons=reasons,
            )
            verification = None
            if not reasons:
                verification = verify_plan_candidate(candidate_files, redactions)
                if not verification.accepted:
                    reasons.extend(verification.reasons)
            if (
                reasons
                or verification is None
                or verification.materialized_files is None
            ):
                return _persist_quarantine(
                    state_root,
                    campaign_id,
                    work_order_id,
                    snapshot_id=bound_snapshot,
                    idempotency_key=idempotency_key,
                    expected_revision=index["revision"],
                    created_at=timestamp,
                    work_order_sha256=contract["work_order_sha256"],
                    stage_manifest=stage_manifest,
                    probe=probe,
                    candidate_sha256=candidate_hash,
                    reasons=tuple(reasons or ("candidate-verification-failed",)),
                )

            return _persist_accepted(
                state_root,
                campaign_id,
                work_order_id,
                snapshot_id=bound_snapshot,
                idempotency_key=idempotency_key,
                expected_revision=index["revision"],
                created_at=timestamp,
                work_order_sha256=contract["work_order_sha256"],
                stage_manifest=stage_manifest,
                probe=probe,
                system_plan=contract["system_plan"],
                protected_roots=contract["protected_roots"],
                order=order,
                materialized_files=verification.materialized_files,
            )


def _load_dispatch_contract(
    state_home: Path,
    campaign_id: str,
    work_order_id: str,
) -> dict[str, Any]:
    root = campaign_directory(state_home, campaign_id).resolve()
    manifest = load_campaign_manifest(state_home, campaign_id)
    bindings = [
        item
        for item in manifest["work_order_bindings"]
        if isinstance(item, Mapping) and item.get("work_order_id") == work_order_id
    ]
    if len(bindings) != 1:
        raise DispatchRejectedError("campaign does not bind the requested work order")
    binding = bindings[0]
    if set(binding) != {
        "work_order_id",
        "sha256",
        "manual_only",
        "dispatchable",
    }:
        raise DispatchRejectedError("work-order binding has unsupported fields")

    work_order_path = root / "work-orders" / f"{work_order_id}.md"
    rendered_bytes = _read_stable_file(work_order_path)
    if sha256_bytes(rendered_bytes) != binding["sha256"]:
        raise DispatchRejectedError("campaign work-order hash does not match")
    try:
        rendered = rendered_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DispatchRejectedError("campaign work order is not UTF-8") from error

    plan_value = _read_json(root / "system-plan.json", "system plan")
    system_plan = SystemPlan.from_dict(plan_value)
    plan_binding = manifest["plan_binding"]
    if (
        not isinstance(plan_binding, Mapping)
        or plan_binding.get("system_plan_id") != system_plan.id
        or plan_binding.get("system_plan_sha256") != system_plan.artifact_sha256
        or system_plan.inventory_sha256 != manifest["inventory_hash"]
        or system_plan.profile_sha256 != manifest["profile_hash"]
    ):
        raise DispatchRejectedError("campaign system-plan binding does not match")
    orders = [item for item in system_plan.work_orders if item.id == work_order_id]
    if len(orders) != 1:
        raise DispatchRejectedError("system plan does not contain the work order")
    order = orders[0]
    if (
        binding["manual_only"] != order.manual_only
        or binding["dispatchable"] != order.dispatchable
    ):
        raise DispatchRejectedError("work-order routing flags do not match")

    auto_plan_value = _read_json(root / "auto-plan.json", "auto plan")
    auto_plan = Plan.from_dict(auto_plan_value)
    auto_plan.validate()
    if (
        not isinstance(plan_binding.get("auto_plan_sha256"), str)
        or sha256_json(auto_plan.to_dict()) != plan_binding["auto_plan_sha256"]
    ):
        raise DispatchRejectedError("campaign auto-plan binding does not match")
    protected_roots = tuple(auto_plan.validation.get("protected_roots", ()))
    return {
        "root": root,
        "rendered": rendered,
        "work_order_sha256": binding["sha256"],
        "system_plan": system_plan,
        "protected_roots": protected_roots,
        "order": order,
    }


def _load_stage_input(
    campaign_root: Path,
    order: SystemWorkOrder,
    *,
    live_text: str,
) -> tuple[str, RedactionMap]:
    if not order.requires_sanitized_staging:
        return live_text, RedactionMap(())
    stage = _read_json(
        campaign_root / "staging" / f"{order.id}.json",
        "sanitized stage",
    )
    if set(stage) != {"schema_version", "files"} or stage["schema_version"] != 1:
        raise DispatchRejectedError("sanitized stage schema is invalid")
    files = stage["files"]
    if not isinstance(files, Mapping) or set(files) != {order.target}:
        raise DispatchRejectedError("sanitized stage does not match the work order")
    staged_text = files[order.target]
    if not isinstance(staged_text, str):
        raise DispatchRejectedError("sanitized stage content must be text")
    redactions = load_redaction_map(campaign_root / "redactions" / f"{order.id}.json")
    verification = verify_plan_candidate({order.target: staged_text}, redactions)
    if not verification.accepted or verification.materialized_files != {
        order.target: live_text
    }:
        raise DispatchRejectedError(
            "sanitized stage no longer reconstructs the exact live input"
        )
    return staged_text, redactions


def _bind_snapshot(
    state_home: Path,
    campaign_id: str,
    order: SystemWorkOrder,
    *,
    system_plan: SystemPlan,
    index: Mapping[str, Any],
    requested_snapshot_id: str | None,
    created_at: str,
) -> str:
    current = index["snapshot_id"]
    if current is not None and requested_snapshot_id not in {None, current}:
        raise DispatchRejectedError("campaign is already bound to another snapshot")
    selected = current or requested_snapshot_id
    campaign_targets = _campaign_targets(system_plan)
    if selected is None:
        snapshot = create_snapshot(
            state_home,
            {
                f"campaign-target-{position:03d}": target
                for position, target in enumerate(campaign_targets, 1)
            },
            label=f"before dispatch {order.id}",
            campaign_id=campaign_id,
            created_at=created_at,
        )
        selected = snapshot["snapshot_id"]
    snapshot = load_snapshot(state_home, selected)
    if snapshot.get("campaign_id") != campaign_id:
        raise DispatchRejectedError("snapshot is not bound to this campaign")
    covered = {
        os.path.normcase(
            os.path.normpath(
                str(Path(entry["target_path"]).expanduser().resolve(strict=False))
            )
        )
        for entry in snapshot["entries"]
    }
    missing = tuple(
        str(target)
        for target in campaign_targets
        if os.path.normcase(os.path.normpath(str(target))) not in covered
    )
    if missing:
        raise DispatchRejectedError(
            "campaign snapshot does not cover every planned target: "
            + ", ".join(missing)
        )
    if current is None:
        register_leaf_artifact(
            state_home,
            campaign_id,
            {
                "schema_version": 1,
                "campaign_id": campaign_id,
                "artifact_type": "snapshot-binding",
                "artifact_id": selected,
                "snapshot_id": selected,
            },
        )
        reconcile_campaign_locked(
            state_home,
            campaign_id,
            expected_revision=index["revision"],
        )
    return selected


def _campaign_targets(system_plan: SystemPlan) -> tuple[Path, ...]:
    targets = (
        *(operation.target for operation in system_plan.auto_operations),
        *(order.target for order in system_plan.work_orders),
    )
    unique: dict[str, Path] = {}
    for target in targets:
        resolved = Path(target).expanduser().resolve(strict=False)
        identity = os.path.normcase(os.path.normpath(str(resolved)))
        unique.setdefault(identity, resolved)
    return tuple(unique[key] for key in sorted(unique))


def _validate_probe(
    probe: IsolationProbeResult,
    *,
    before_sha256: str,
    after_sha256: str,
) -> None:
    if not isinstance(probe, IsolationProbeResult):
        raise DispatchUnavailableError("isolation probe returned an invalid result")
    if before_sha256 != after_sha256:
        raise DispatchSecurityError("isolation probe changed live target bytes")
    if (
        not probe.attempted
        or not probe.write_denied
        or not isinstance(probe.context_id, str)
        or not probe.context_id
        or not isinstance(probe.mechanism, str)
        or not probe.mechanism
    ):
        raise DispatchUnavailableError(
            "automated dispatch is unavailable: live-root write denial was not proven"
        )


def _candidate_files(
    candidate: Any,
    *,
    expected_files: set[str],
    reasons: list[str],
) -> dict[str, str]:
    if not isinstance(candidate, Mapping) or set(candidate) != {"files"}:
        reasons.append("runner-return-is-not-a-plan-candidate")
        return {}
    files = candidate["files"]
    if not isinstance(files, Mapping):
        reasons.append("plan-candidate-files-is-not-an-object")
        return {}
    normalized: dict[str, str] = {}
    for path, content in files.items():
        if not isinstance(path, str) or not isinstance(content, str):
            reasons.append("plan-candidate-files-must-map-paths-to-text")
            continue
        normalized[path] = content
    if set(normalized) != expected_files:
        reasons.append("plan-candidate-file-set-does-not-match-work-order")
    return normalized


def _persist_accepted(
    state_home: Path,
    campaign_id: str,
    work_order_id: str,
    *,
    snapshot_id: str,
    idempotency_key: Mapping[str, Any],
    expected_revision: int,
    created_at: str,
    work_order_sha256: str,
    stage_manifest: Mapping[str, Any],
    probe: IsolationProbeResult,
    system_plan: SystemPlan,
    protected_roots: tuple[str, ...],
    order: SystemWorkOrder,
    materialized_files: Mapping[str, str],
) -> DispatchResult:
    root = campaign_directory(state_home, campaign_id).resolve()
    candidate_root = ensure_private_directory(
        root / "accepted-candidates" / work_order_id
    )
    operations: list[PlanOperation] = []
    source_hashes = {
        item["logical_path"]: item["source_sha256"] for item in stage_manifest["files"]
    }
    for index, (target, text) in enumerate(sorted(materialized_files.items()), 1):
        content = text.encode("utf-8")
        content_hash = sha256_bytes(content)
        source = candidate_root / f"{content_hash}.candidate"
        _write_bytes_once(source, content)
        operations.append(
            PlanOperation(
                id=f"dispatch-{index}-{content_hash[:16]}",
                action="merge",
                target=target,
                source=str(source),
                ownership=(order.findings[0].authority if order.findings else "user"),
                strategy="full_file",
                precondition_sha256=source_hashes[target],
                proposed_sha256=content_hash,
                backup_required=True,
                requires_confirmation=True,
                approval=Approval(),
                reason=f"reviewed plan candidate for {work_order_id}",
                evidence=tuple(
                    finding_id
                    for finding in order.findings
                    for finding_id in finding.finding_ids
                ),
            )
        )
    plan = Plan(
        schema_version=1,
        apu_version=system_plan.apu_version,
        created_at=created_at,
        inventory_sha256=system_plan.inventory_sha256,
        status="draft",
        operations=tuple(operations),
        validation={
            "commands": [],
            "fixtures": [],
            "required": ["human-review"],
            "protected_roots": list(protected_roots),
            "campaign_binding": {
                "campaign_id": campaign_id,
                "snapshot_id": snapshot_id,
                "work_order_id": work_order_id,
                "work_order_sha256": work_order_sha256,
            },
        },
    )
    plan.validate()
    plan_value = plan.to_dict()
    plan_hash = sha256_json(plan_value)
    plan_path = root / "plans" / f"{plan_hash}.json"
    write_json_atomic(plan_path, plan_value)
    leaf = _result_leaf(
        campaign_id,
        work_order_id,
        snapshot_id=snapshot_id,
        idempotency_key=idempotency_key,
        created_at=created_at,
        status="accepted",
        work_order_sha256=work_order_sha256,
        stage_manifest=stage_manifest,
        probe=probe,
        candidate_sha256=plan_hash,
        reasons=(),
        plan={
            "path": str(plan_path.relative_to(root)),
            "sha256": plan_hash,
        },
    )
    artifact_path = register_leaf_artifact(state_home, campaign_id, leaf)
    reconcile_campaign_locked(
        state_home,
        campaign_id,
        expected_revision=expected_revision,
    )
    return DispatchResult(
        status="accepted",
        campaign_id=campaign_id,
        work_order_id=work_order_id,
        snapshot_id=snapshot_id,
        artifact_path=artifact_path,
        plan_path=plan_path,
    )


def _persist_quarantine(
    state_home: Path,
    campaign_id: str,
    work_order_id: str,
    *,
    snapshot_id: str,
    idempotency_key: Mapping[str, Any],
    expected_revision: int,
    created_at: str,
    work_order_sha256: str,
    stage_manifest: Mapping[str, Any],
    probe: IsolationProbeResult,
    candidate_sha256: str | None,
    reasons: tuple[str, ...],
) -> DispatchResult:
    leaf = _result_leaf(
        campaign_id,
        work_order_id,
        snapshot_id=snapshot_id,
        idempotency_key=idempotency_key,
        created_at=created_at,
        status="quarantined",
        work_order_sha256=work_order_sha256,
        stage_manifest=stage_manifest,
        probe=probe,
        candidate_sha256=candidate_sha256,
        reasons=reasons,
        plan=None,
    )
    artifact_path = register_leaf_artifact(state_home, campaign_id, leaf)
    reconcile_campaign_locked(
        state_home,
        campaign_id,
        expected_revision=expected_revision,
    )
    return DispatchResult(
        status="quarantined",
        campaign_id=campaign_id,
        work_order_id=work_order_id,
        snapshot_id=snapshot_id,
        artifact_path=artifact_path,
        plan_path=None,
        reasons=reasons,
    )


def _result_leaf(
    campaign_id: str,
    work_order_id: str,
    *,
    snapshot_id: str,
    idempotency_key: Mapping[str, Any],
    created_at: str,
    status: str,
    work_order_sha256: str,
    stage_manifest: Mapping[str, Any],
    probe: IsolationProbeResult,
    candidate_sha256: str | None,
    reasons: tuple[str, ...],
    plan: Mapping[str, str] | None,
) -> dict[str, Any]:
    body = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "artifact_type": "work-order-result",
        "snapshot_id": snapshot_id,
        "idempotency_key": dict(idempotency_key),
        "work_order_id": work_order_id,
        "created_at": created_at,
        "status": status,
        "work_order_sha256": work_order_sha256,
        "stage": {
            "manifest_sha256": sha256_json(stage_manifest),
            "files": [
                {
                    key: item[key]
                    for key in (
                        "logical_path",
                        "source_sha256",
                        "staged_sha256",
                        "sanitized",
                    )
                }
                for item in stage_manifest["files"]
            ],
        },
        "isolation": {
            "context_id": probe.context_id,
            "mechanism": probe.mechanism,
            "actual_denied_write_probe": True,
        },
        "candidate_sha256": candidate_sha256,
        "reasons": list(reasons),
        "plan": dict(plan) if plan is not None else None,
    }
    return {**body, "artifact_id": sha256_json(body)}


def _existing_result(
    state_home: Path,
    campaign_id: str,
    *,
    idempotency_key: Mapping[str, Any],
) -> dict[str, Any] | None:
    matches = [
        artifact
        for artifact in scan_leaf_artifacts(state_home, campaign_id)
        if artifact["artifact_type"] == "work-order-result"
        and artifact.get("idempotency_key") == dict(idempotency_key)
    ]
    if len(matches) > 1:
        raise DispatchSecurityError("duplicate dispatch idempotency key")
    return matches[0] if matches else None


def _result_from_leaf(
    state_home: Path,
    campaign_id: str,
    leaf: Mapping[str, Any],
) -> DispatchResult:
    root = campaign_directory(state_home, campaign_id).resolve()
    plan = leaf.get("plan")
    plan_path = root / plan["path"] if isinstance(plan, Mapping) else None
    return DispatchResult(
        status=leaf["status"],
        campaign_id=campaign_id,
        work_order_id=leaf["work_order_id"],
        snapshot_id=leaf["snapshot_id"],
        artifact_path=(
            root / "artifacts" / "work-order-result" / f"{leaf['artifact_id']}.json"
        ),
        plan_path=plan_path,
        reasons=tuple(leaf.get("reasons", ())),
    )


def _tree_identity(root: Path) -> str:
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise DispatchSecurityError("dispatch stage contains a symbolic link")
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append({"path": relative, "type": "directory", "sha256": ""})
        elif path.is_file():
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "sha256": sha256_bytes(path.read_bytes()),
                }
            )
        else:
            raise DispatchSecurityError("dispatch stage contains an unsupported object")
    return sha256_json(entries)


def _read_stable_file(path: Path) -> bytes:
    candidate = Path(path)
    if candidate.is_symlink():
        raise DispatchRejectedError(
            f"dispatch input must not be a symlink: {candidate}"
        )
    try:
        before = candidate.stat()
        with candidate.open("rb") as stream:
            content = stream.read()
        after = candidate.stat()
    except OSError as error:
        raise DispatchRejectedError(
            f"dispatch input is unavailable: {candidate}"
        ) from error
    identity = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_size,
        item.st_mtime_ns,
    )
    if identity(before) != identity(after):
        raise DispatchRejectedError(
            f"dispatch input changed while reading: {candidate}"
        )
    return content


def _write_bytes_once(path: Path, content: bytes) -> Path:
    destination = Path(path)
    ensure_private_directory(destination.parent)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if _read_stable_file(destination) != content:
            raise DispatchSecurityError(
                f"immutable dispatch file collision: {destination}"
            )
        return destination
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name == "posix":
        destination.chmod(0o600)
    return destination


def _read_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_stable_file(path).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise DispatchRejectedError(f"invalid {description}: {path}") from error
    if not isinstance(value, dict):
        raise DispatchRejectedError(f"{description} must be an object")
    return value


def _safe_json_hash(value: Any) -> str | None:
    try:
        return sha256_bytes(canonical_json(value).encode("utf-8"))
    except (TypeError, ValueError):
        return None


def _component(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\0" in value
    ):
        raise ValueError(f"{name} must be one safe path component")
    return value


@contextmanager
def _temporary_dispatch_stage(*, prefix: str):
    """Create a disposable stage whose Windows ACL is sandbox-traversable.

    Python's TemporaryDirectory intentionally installs an owner-only Windows
    DACL. Codex's restricted token then cannot enter the stage to prove its
    write boundary. A normal Windows mkdir inherits the user's Temp ACL,
    including the dedicated Codex sandbox group; POSIX remains mode 0700.
    """

    parent = Path(tempfile.gettempdir()).resolve()
    for _ in range(16):
        root = parent / f"{prefix}{uuid4().hex}"
        try:
            root.mkdir(mode=0o700 if os.name == "posix" else 0o777)
            break
        except FileExistsError:
            continue
    else:
        raise OSError("could not allocate a unique dispatch stage")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)

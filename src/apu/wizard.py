from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from apu.models import Approval, Plan, PlanOperation
from apu.filesystem import hash_object
from apu.planning import build_relocation_operations, update_plan_status


@dataclass(frozen=True)
class ReviewDecision:
    status: str
    replacement_source: Path | None = None
    relocate_target: Path | None = None


DecisionReader = Callable[[PlanOperation], str | ReviewDecision]


def review_plan(
    plan: Plan,
    *,
    decide: DecisionReader,
    recorded_at: str | None = None,
) -> Plan:
    """Apply persisted review decisions through an injectable terminal boundary."""

    reviewed: list[PlanOperation] = []
    group_decisions: dict[str, str] = {}
    for operation in plan.operations:
        if not operation.is_mutating:
            reviewed.append(operation)
            continue
        if operation.atomic_group_id is not None:
            stored = group_decisions.get(operation.atomic_group_id)
            if stored is None:
                raw = decide(operation)
                decision = raw if isinstance(raw, str) else raw.status
                group_decisions[operation.atomic_group_id] = decision
            else:
                decision = stored
        else:
            raw = decide(operation)
            detailed = (
                ReviewDecision(raw) if isinstance(raw, str) else raw
            )
            decision = detailed.status
            if detailed.replacement_source is not None:
                operation = _edited_operation(operation, detailed)
            if detailed.relocate_target is not None:
                reviewed.extend(
                    _relocated_operations(
                        operation,
                        detailed,
                        recorded_at=recorded_at,
                    )
                )
                continue
        if decision not in {"approved", "rejected", "deferred"}:
            raise ValueError(f"unsupported review decision: {decision}")
        reviewed.append(
            replace(
                operation,
                approval=Approval(
                    status=decision,
                    recorded_at=recorded_at,
                    method="interactive",
                ),
            )
        )
    return update_plan_status(replace(plan, operations=tuple(reviewed)))


def _edited_operation(
    operation: PlanOperation,
    decision: ReviewDecision,
) -> PlanOperation:
    source = Path(decision.replacement_source).expanduser().resolve()
    if not source.exists() or source.is_symlink():
        raise ValueError(f"replacement source is missing or unsafe: {source}")
    return replace(
        operation,
        action="merge",
        source=str(source),
        strategy="full_file",
        proposed_sha256=hash_object(source),
        backup_required=True,
        requires_confirmation=True,
    )


def _relocated_operations(
    operation: PlanOperation,
    decision: ReviewDecision,
    *,
    recorded_at: str | None,
) -> tuple[PlanOperation, PlanOperation]:
    destination = Path(decision.relocate_target).expanduser().resolve()
    content_hash = operation.precondition_sha256
    if content_hash is None:
        content_hash = hash_object(Path(operation.target))
    approval = Approval(
        status=decision.status,
        recorded_at=recorded_at,
        method="interactive-relocate",
    )
    return build_relocation_operations(
        operation_id=f"{operation.id}-relocate",
        source=Path(operation.target),
        destination=destination,
        content_sha256=content_hash,
        approval=approval,
        evidence=operation.evidence,
    )

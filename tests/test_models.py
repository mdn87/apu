from __future__ import annotations

from dataclasses import replace

import pytest

from apu.models import (
    Approval,
    InstructionSurface,
    Plan,
    PlanOperation,
    ValidationError,
    canonical_json,
    derive_plan_status,
    sha256_json,
)


def operation(
    operation_id: str,
    *,
    action: str = "create",
    strategy: str = "full_file",
    approval: str = "approved",
) -> PlanOperation:
    return PlanOperation(
        id=operation_id,
        action=action,
        target=f"/tmp/{operation_id}",
        source=None,
        ownership="apu",
        strategy=strategy,
        precondition_sha256=None,
        proposed_sha256="f" * 64,
        backup_required=False,
        requires_confirmation=False,
        approval=Approval(status=approval),
        reason="fixture",
        evidence=(),
    )


def test_canonical_json_is_stable_and_compact() -> None:
    left = {"b": [2, 1], "a": {"z": True}}
    right = {"a": {"z": True}, "b": [2, 1]}

    assert canonical_json(left) == '{"a":{"z":true},"b":[2,1]}'
    assert sha256_json(left) == sha256_json(right)


def test_inventory_identity_includes_generated_at() -> None:
    first = {
        "schema_version": 1,
        "generated_at": "2026-08-06T10:00:00Z",
        "surfaces": [],
    }
    second = {
        **first,
        "generated_at": "2026-08-06T10:00:01Z",
    }

    assert sha256_json(first) != sha256_json(second)


@pytest.mark.parametrize(
    ("decisions", "expected"),
    [
        (["approved"], "approved"),
        (["approved", "rejected"], "approved"),
        (["approved", "pending"], "draft"),
        (["approved", "deferred"], "draft"),
        (["rejected"], "draft"),
        ([], "draft"),
    ],
)
def test_plan_status_derives_from_mutating_decisions(
    decisions: list[str], expected: str
) -> None:
    operations = tuple(
        operation(f"op-{index}", approval=decision)
        for index, decision in enumerate(decisions)
    )

    assert derive_plan_status(operations) == expected


def test_non_mutating_operations_do_not_block_approval() -> None:
    operations = (
        operation("apply", approval="approved"),
        operation(
            "preserve",
            action="preserve",
            strategy="proposal_only",
            approval="pending",
        ),
        operation(
            "proposal",
            action="create",
            strategy="proposal_only",
            approval="deferred",
        ),
    )

    assert derive_plan_status(operations) == "approved"


def test_plan_executable_operations_include_only_approved_mutations() -> None:
    approved = operation("approved", approval="approved")
    rejected = operation("rejected", approval="rejected")
    preserve = operation(
        "preserve",
        action="preserve",
        strategy="proposal_only",
        approval="pending",
    )
    plan = Plan(
        schema_version=1,
        apu_version="0.1.0",
        created_at="2026-08-06T10:00:00Z",
        inventory_sha256="a" * 64,
        status="approved",
        operations=(approved, rejected, preserve),
    )

    assert plan.executable_operations() == (approved,)


def test_plan_rejects_claimed_approved_status_when_decisions_are_unresolved() -> None:
    plan = Plan(
        schema_version=1,
        apu_version="0.1.0",
        created_at="2026-08-06T10:00:00Z",
        inventory_sha256="a" * 64,
        status="approved",
        operations=(operation("pending", approval="pending"),),
    )

    with pytest.raises(ValidationError, match="status"):
        plan.validate()


def test_relocation_requires_atomic_pair_with_shared_group_and_hash() -> None:
    remove = operation("move-remove", action="remove")
    remove = replace(
        remove,
        atomic_group_id="move-1",
        group_content_sha256="b" * 64,
        proposed_sha256=None,
    )
    create = operation("move-create", action="create")
    create = replace(
        create,
        atomic_group_id="move-1",
        group_content_sha256="b" * 64,
        proposed_sha256="b" * 64,
    )
    plan = Plan(
        schema_version=1,
        apu_version="0.1.0",
        created_at="2026-08-06T10:00:00Z",
        inventory_sha256="a" * 64,
        status="approved",
        operations=(remove, create),
    )

    plan.validate()

    broken = replace(plan, operations=(remove,))
    with pytest.raises(ValidationError, match="relocation"):
        broken.validate()


def test_instruction_surface_round_trips_nullable_mode() -> None:
    surface = InstructionSurface(
        id="sha256:" + "a" * 64,
        path="C:\\repo\\CLAUDE.md",
        kind="claude",
        provider="claude",
        authority="repository",
        scope="repository",
        real_path="C:\\repo\\CLAUDE.md",
        is_symlink=False,
        content_sha256="b" * 64,
        mode=None,
        precedence=30,
        sensitive=False,
    )

    assert InstructionSurface.from_dict(surface.to_dict()) == surface


def test_plan_rejects_duplicate_mutation_targets() -> None:
    first = operation("first")
    second = replace(
        operation("second"),
        target=first.target,
        source="/tmp/other-source",
    )
    duplicate = Plan(
        schema_version=1,
        apu_version="0.1.0",
        created_at="2026-08-06T10:00:00Z",
        inventory_sha256="a" * 64,
        status="approved",
        operations=(first, second),
    )

    with pytest.raises(ValidationError, match="same filesystem object"):
        duplicate.validate()

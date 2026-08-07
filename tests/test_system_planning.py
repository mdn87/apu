from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from apu.models import Finding, InstructionSurface, Inventory
from apu.system_audit import RepositoryInventory, SystemInventory
from apu.system_planning import (
    SystemPlan,
    SystemPlanningError,
    load_system_plan,
    propose_system,
)


def surface(
    path: Path,
    *,
    identifier: str,
    authority: str = "repository",
    sensitive: bool = False,
) -> InstructionSurface:
    return InstructionSurface(
        id=f"surface-{identifier}",
        path=str(path.resolve()),
        kind="agents",
        provider="codex",
        authority=authority,
        scope="repository" if authority != "user" else "global",
        real_path=str(path.resolve()),
        is_symlink=False,
        content_sha256=identifier[0] * 64,
        mode="0644",
        precedence=30,
        sensitive=sensitive,
    )


def finding(
    item: InstructionSurface,
    *,
    identifier: str,
    category: str,
    line: int = 1,
    location: dict[str, object] | None = None,
) -> Finding:
    return Finding(
        id=f"finding-{identifier}",
        surface_id=item.id,
        location=location or {"line": line},
        category=category,
        severity="high",
        confidence="high",
        analysis_method=(
            "structural"
            if category in {
                "duplicate-instruction",
                "sensitive-material-exposure",
            }
            else "heuristic"
        ),
        evidence=(f"evidence-{identifier}",),
        summary=f"{category} needs remediation.",
    )


def child_inventory(
    *surfaces: InstructionSurface,
    findings: tuple[Finding, ...],
    generated_at: str = "2026-08-06T20:00:00Z",
) -> Inventory:
    return Inventory(
        schema_version=1,
        apu_version="0.2.0.dev0",
        generated_at=generated_at,
        scope={"roots": sorted({str(Path(item.path).parent) for item in surfaces})},
        surfaces=surfaces,
        findings=findings,
    )


def system_inventory(
    machine: Inventory,
    *,
    repositories: tuple[RepositoryInventory, ...] = (),
) -> SystemInventory:
    return SystemInventory(
        schema_version=1,
        apu_version="0.2.0.dev0",
        generated_at="2026-08-06T20:00:00Z",
        profile_sha256="f" * 64,
        machine_inventory=machine,
        repositories=repositories,
    )


def propose(
    inventory: SystemInventory,
    policy: dict[str, str],
) -> SystemPlan:
    return propose_system(
        inventory,
        policy,
        created_at="2026-08-06T20:05:00Z",
    )


def test_partitions_and_batches_deterministic_findings_per_target(
    tmp_path: Path,
) -> None:
    first = surface(tmp_path / "AGENTS.md", identifier="a")
    second = surface(tmp_path / "CLAUDE.md", identifier="b")
    audit = system_inventory(
        child_inventory(
            first,
            second,
            findings=(
                finding(
                    first,
                    identifier="duplicate-3",
                    category="duplicate-instruction",
                    line=3,
                ),
                finding(
                    second,
                    identifier="judgment",
                    category="guidance-conflict",
                ),
                finding(
                    first,
                    identifier="duplicate-2",
                    category="duplicate-instruction",
                    line=2,
                ),
            ),
        )
    )

    plan = propose(
        audit,
        {
            "duplicate-instruction": "auto",
            "guidance-conflict": "work-order",
        },
    )

    assert len(plan.auto_operations) == 1
    operation = plan.auto_operations[0]
    assert operation.target == first.path
    assert operation.action == "merge"
    assert operation.strategy == "remove-lines"
    assert operation.line_numbers == (2, 3)
    assert [item.category for item in operation.findings] == [
        "duplicate-instruction",
        "duplicate-instruction",
    ]
    assert len(plan.work_orders) == 1
    assert plan.work_orders[0].target == second.path
    assert plan.work_orders[0].handling == "standard"
    assert plan.ignored_findings == ()


def test_ignore_policy_is_an_explicit_exclusive_partition(
    tmp_path: Path,
) -> None:
    item = surface(tmp_path / "AGENTS.md", identifier="a")
    audit = system_inventory(
        child_inventory(
            item,
            findings=(
                finding(
                    item,
                    identifier="ignored",
                    category="guidance-conflict",
                ),
            ),
        )
    )

    plan = propose(audit, {"guidance-conflict": "ignore"})

    assert plan.auto_operations == ()
    assert plan.work_orders == ()
    assert len(plan.ignored_findings) == 1
    assert plan.ignored_findings[0].routing_reason == "profile-ignore"


def test_unconfigured_category_defaults_to_work_order(tmp_path: Path) -> None:
    item = surface(tmp_path / "AGENTS.md", identifier="a")
    audit = system_inventory(
        child_inventory(
            item,
            findings=(
                finding(
                    item,
                    identifier="default",
                    category="guidance-conflict",
                ),
            ),
        )
    )

    plan = propose(audit, {})

    routed = plan.work_orders[0].findings[0]
    assert routed.requested_policy == "work-order"
    assert routed.effective_policy == "work-order"
    assert routed.routing_reason == "profile-work-order"


def test_non_deterministic_auto_request_fails_closed_to_work_order(
    tmp_path: Path,
) -> None:
    item = surface(tmp_path / "AGENTS.md", identifier="a")
    audit = system_inventory(
        child_inventory(
            item,
            findings=(
                finding(
                    item,
                    identifier="judgment",
                    category="universal-skill-trigger",
                ),
            ),
        )
    )

    plan = propose(audit, {"universal-skill-trigger": "auto"})

    assert plan.auto_operations == ()
    routed = plan.work_orders[0].findings[0]
    assert routed.requested_policy == "auto"
    assert routed.effective_policy == "work-order"
    assert routed.routing_reason == "non-deterministic-remediation"


def test_package_authority_never_produces_direct_rewrite(
    tmp_path: Path,
) -> None:
    item = surface(
        tmp_path / "package" / "SKILL.md",
        identifier="a",
        authority="package",
    )
    audit = system_inventory(
        child_inventory(
            item,
            findings=(
                finding(
                    item,
                    identifier="duplicate",
                    category="duplicate-instruction",
                    line=2,
                ),
            ),
        )
    )

    plan = propose(audit, {"duplicate-instruction": "auto"})

    assert plan.auto_operations == ()
    assert plan.work_orders[0].package_authority is True
    routed = plan.work_orders[0].findings[0]
    assert routed.requested_policy == "auto"
    assert routed.routing_reason == "package-authority-no-direct-rewrite"


@pytest.mark.parametrize("configured_policy", ["auto", "ignore", "work-order"])
def test_sensitive_material_is_always_private_manual_only(
    tmp_path: Path,
    configured_policy: str,
) -> None:
    secret = "sk-live-this-must-not-be-emitted"
    item = surface(
        tmp_path / "AGENTS.md",
        identifier="a",
        sensitive=True,
    )
    audit = system_inventory(
        child_inventory(
            item,
            findings=(
                finding(
                    item,
                    identifier="secret",
                    category="sensitive-material-exposure",
                    location={"line": 7, "content": secret},
                ),
            ),
        )
    )

    plan = propose(
        audit,
        {"sensitive-material-exposure": configured_policy},
    )
    encoded = json.dumps(plan.to_dict(), sort_keys=True)

    assert secret not in encoded
    assert plan.auto_operations == ()
    assert plan.ignored_findings == ()
    work_order = plan.work_orders[0]
    assert work_order.manual_only is True
    assert work_order.dispatchable is False
    assert work_order.content_policy == "location-line-content-hash-only"
    routed = work_order.findings[0]
    assert routed.location == {"line": 7}
    assert routed.evidence == ()
    assert routed.requested_policy == configured_policy
    assert routed.routing_reason == "sensitive-material-manual-only"


@pytest.mark.parametrize("configured_policy", ["auto", "work-order"])
def test_other_finding_on_sensitive_surface_requires_sanitized_staging(
    tmp_path: Path,
    configured_policy: str,
) -> None:
    item = surface(
        tmp_path / "AGENTS.md",
        identifier="a",
        sensitive=True,
    )
    audit = system_inventory(
        child_inventory(
            item,
            findings=(
                finding(
                    item,
                    identifier="judgment",
                    category="guidance-conflict",
                    location={"line": 4, "content": "private"},
                ),
            ),
        )
    )

    plan = propose(audit, {"guidance-conflict": configured_policy})

    assert plan.auto_operations == ()
    work_order = plan.work_orders[0]
    assert work_order.handling == "sanitized"
    assert work_order.dispatchable is True
    assert work_order.requires_sanitized_staging is True
    assert work_order.findings[0].location == {"line": 4}
    assert work_order.findings[0].evidence == ()
    assert '"content": "private"' not in json.dumps(plan.to_dict())
    if configured_policy == "auto":
        assert (
            work_order.findings[0].routing_reason
            == "sensitive-surface-sanitized"
        )


def test_deduplicates_provider_views_and_has_stable_order_and_ids(
    tmp_path: Path,
) -> None:
    target = tmp_path / "AGENTS.md"
    codex = surface(target, identifier="a")
    claude = replace(
        codex,
        id="surface-claude",
        provider="claude",
        precedence=40,
    )
    first_finding = finding(
        codex,
        identifier="codex",
        category="duplicate-instruction",
        line=2,
    )
    second_finding = replace(
        first_finding,
        id="finding-claude",
        surface_id=claude.id,
    )
    first_inventory = system_inventory(
        child_inventory(
            codex,
            claude,
            findings=(first_finding, second_finding),
        )
    )
    second_inventory = system_inventory(
        child_inventory(
            claude,
            codex,
            findings=(second_finding, first_finding),
        )
    )

    first = propose(first_inventory, {"duplicate-instruction": "auto"})
    second = propose_system(
        second_inventory,
        {"duplicate-instruction": "auto"},
        created_at="2026-08-06T21:00:00Z",
    )
    repeated = propose_system(
        first_inventory,
        {"duplicate-instruction": "auto"},
        created_at="2026-08-06T21:00:00Z",
    )

    # The plan binds the exact inventory artifact, while child artifact IDs
    # remain stable for semantically equivalent, differently ordered inputs.
    assert first.id == repeated.id
    assert first.id != second.id
    assert first.auto_operations[0].id == second.auto_operations[0].id
    routed = first.auto_operations[0].findings[0]
    assert routed.finding_ids == ("finding-claude", "finding-codex")
    assert routed.surface_ids == ("surface-a", "surface-claude")


def test_serialization_round_trip_and_file_loader(tmp_path: Path) -> None:
    item = surface(tmp_path / "AGENTS.md", identifier="a")
    audit = system_inventory(
        child_inventory(
            item,
            findings=(
                finding(
                    item,
                    identifier="duplicate",
                    category="duplicate-instruction",
                ),
            ),
        )
    )
    plan = propose(audit, {"duplicate-instruction": "auto"})
    path = tmp_path / "system-plan.json"
    path.write_text(
        json.dumps(plan.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    loaded = load_system_plan(path)

    assert loaded == plan
    assert SystemPlan.from_dict(plan.to_dict()) == plan
    assert loaded.artifact_sha256 == plan.artifact_sha256


def test_rejects_unsupported_policy_and_unknown_surface(
    tmp_path: Path,
) -> None:
    item = surface(tmp_path / "AGENTS.md", identifier="a")
    valid = child_inventory(
        item,
        findings=(
            finding(
                item,
                identifier="finding",
                category="duplicate-instruction",
            ),
        ),
    )

    with pytest.raises(SystemPlanningError, match="unsupported remediation"):
        propose(system_inventory(valid), {"duplicate-instruction": "sometimes"})

    broken = replace(
        valid,
        findings=(
            replace(valid.findings[0], surface_id="missing-surface"),
        ),
    )
    with pytest.raises(SystemPlanningError, match="unknown surface"):
        propose(system_inventory(broken), {})


def test_rejects_tampered_or_malformed_serialized_plan(
    tmp_path: Path,
) -> None:
    item = surface(tmp_path / "AGENTS.md", identifier="a")
    audit = system_inventory(
        child_inventory(
            item,
            findings=(
                finding(
                    item,
                    identifier="duplicate",
                    category="duplicate-instruction",
                ),
            ),
        )
    )
    plan = propose(audit, {"duplicate-instruction": "auto"})
    tampered = plan.to_dict()
    tampered["id"] = "system-plan-" + "0" * 20

    with pytest.raises(SystemPlanningError, match="id does not match"):
        SystemPlan.from_dict(tampered)

    malformed = plan.to_dict()
    malformed["unexpected"] = True
    with pytest.raises(SystemPlanningError, match="unsupported fields"):
        SystemPlan.from_dict(malformed)

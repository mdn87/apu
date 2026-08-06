from __future__ import annotations

from pathlib import Path
from dataclasses import replace

from apu.models import (
    Approval,
    Finding,
    InstructionSurface,
    Inventory,
    Plan,
    PlanOperation,
    sha256_bytes,
)
from apu.adapters.claude import ClaudeAdapter
from apu.adapters.codex import CodexAdapter
from apu.planning import (
    approve_all_recommended,
    build_relocation_operations,
    build_skill_install_operations,
    propose_inventory,
)
from apu.wizard import ReviewDecision, review_plan


def inventory(path: Path) -> Inventory:
    surface = InstructionSurface(
        id="sha256:" + "a" * 64,
        path=str(path),
        kind="agents",
        provider="codex",
        authority="repository",
        scope="repository",
        real_path=str(path),
        is_symlink=False,
        content_sha256="b" * 64,
        mode="0644",
        precedence=30,
        sensitive=False,
    )
    finding = Finding(
        id="finding-1",
        surface_id=surface.id,
        location={"line": 1},
        category="universal-skill-trigger",
        severity="high",
        confidence="high",
        analysis_method="heuristic",
        evidence=("matched-rule",),
        summary="Universal trigger.",
    )
    return Inventory(
        schema_version=1,
        apu_version="0.1.0",
        generated_at="2026-08-06T10:00:00Z",
        scope={"roots": [str(path.parent)], "working_directories": []},
        surfaces=(surface,),
        findings=(finding,),
    )


def test_proposal_is_deterministic_for_same_inventory_and_timestamp(
    tmp_path: Path,
) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("Always invoke the skill.\n")
    audit = inventory(target)

    first = propose_inventory(audit, created_at="2026-08-06T11:00:00Z")
    second = propose_inventory(audit, created_at="2026-08-06T11:00:00Z")

    assert first.to_dict() == second.to_dict()
    assert first.inventory_sha256 == audit.artifact_sha256
    assert first.operations[0].precondition_sha256 == "b" * 64
    assert first.operations[0].strategy == "proposal_only"


def test_batch_approval_leaves_confirmation_and_low_confidence_pending() -> None:
    recommended = PlanOperation(
        id="recommended",
        action="create",
        target="/tmp/recommended",
        source="/tmp/source",
        ownership="apu",
        strategy="full_file",
        precondition_sha256=None,
        proposed_sha256="a" * 64,
        backup_required=False,
        requires_confirmation=False,
        approval=Approval(),
        reason="high-confidence recommendation",
        evidence=("finding-1",),
    )
    manual = PlanOperation(
        **{
            **recommended.__dict__,
            "id": "manual",
            "target": "/tmp/manual",
            "requires_confirmation": True,
        }
    )

    approved = approve_all_recommended((recommended, manual))

    assert approved[0].approval.status == "approved"
    assert approved[1].approval.status == "pending"


def test_relocation_expands_to_atomic_remove_create_pair(tmp_path: Path) -> None:
    source = tmp_path / "old.md"
    destination = tmp_path / "new.md"
    content_hash = "c" * 64

    operations = build_relocation_operations(
        operation_id="move-policy",
        source=source,
        destination=destination,
        content_sha256=content_hash,
        approval=Approval(status="approved"),
        evidence=("finding-1",),
    )

    assert [operation.action for operation in operations] == ["remove", "create"]
    assert {operation.atomic_group_id for operation in operations} == {
        "move-policy"
    }
    assert {operation.group_content_sha256 for operation in operations} == {
        content_hash
    }
    assert len({operation.approval for operation in operations}) == 1


def test_skill_install_uses_canonical_shared_source_and_provider_targets(
    tmp_path: Path,
) -> None:
    package_skill = tmp_path / "package" / "optimizing-agent-instructions"
    package_skill.mkdir(parents=True)
    (package_skill / "SKILL.md").write_text("---\nname: optimizer\n---\n")
    home = tmp_path / "home"

    operations = build_skill_install_operations(
        package_skill=package_skill,
        home=home,
        include_claude=True,
    )

    targets = {operation.target for operation in operations}
    shared = home / ".agents" / "skills" / "optimizing-agent-instructions"
    claude = home / ".claude" / "skills" / "optimizing-agent-instructions"
    assert targets == {str(shared), str(claude)}
    assert all(operation.action == "symlink" for operation in operations)
    assert all(operation.source == str(package_skill) for operation in operations)


def test_skill_install_makes_windows_copy_fallback_visible(tmp_path: Path) -> None:
    package_skill = tmp_path / "package" / "optimizing-agent-instructions"
    package_skill.mkdir(parents=True)
    (package_skill / "SKILL.md").write_text("---\nname: optimizer\n---\n")

    operations = CodexAdapter().plan_skill_install(
        package_skill,
        home=tmp_path / "home",
        symlink_supported=False,
    )

    assert len(operations) == 1
    assert operations[0].action == "create"
    assert operations[0].strategy == "full_file"
    assert "symlinks are unavailable" in operations[0].reason


def test_existing_skill_target_is_preserved_for_review(tmp_path: Path) -> None:
    package_skill = tmp_path / "package" / "optimizing-agent-instructions"
    package_skill.mkdir(parents=True)
    (package_skill / "SKILL.md").write_text("---\nname: optimizer\n---\n")
    target = (
        tmp_path
        / "home"
        / ".agents"
        / "skills"
        / "optimizing-agent-instructions"
    )
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("user-owned")

    operations = CodexAdapter().plan_skill_install(
        package_skill,
        home=tmp_path / "home",
    )

    assert operations[0].action == "preserve"
    assert operations[0].strategy == "proposal_only"


def test_claude_adapter_can_plan_marketplace_configuration(tmp_path: Path) -> None:
    package_skill = tmp_path / "package" / "optimizing-agent-instructions"
    package_skill.mkdir(parents=True)
    (package_skill / "SKILL.md").write_text("---\nname: optimizer\n---\n")
    rendered = tmp_path / "rendered-marketplaces.json"
    rendered.write_text('{"apu":{"source":"directory"}}')

    operations = ClaudeAdapter().plan_skill_install(
        package_skill,
        home=tmp_path / "home",
        marketplace_rendered=rendered,
    )

    assert [operation.action for operation in operations] == [
        "symlink",
        "configure",
    ]
    assert operations[1].precondition_sha256 is None
    assert operations[1].source == str(rendered.resolve())


def test_review_records_one_decision_for_atomic_relocation(
    tmp_path: Path,
) -> None:
    operations = build_relocation_operations(
        operation_id="move-policy",
        source=tmp_path / "old.md",
        destination=tmp_path / "new.md",
        content_sha256="d" * 64,
        approval=Approval(),
    )
    plan = Plan(
        schema_version=1,
        apu_version="0.1.0",
        created_at="2026-08-06T11:00:00Z",
        inventory_sha256="a" * 64,
        status="draft",
        operations=operations,
    )
    calls = 0

    def decide(_operation: PlanOperation) -> str:
        nonlocal calls
        calls += 1
        return "approved"

    reviewed = review_plan(plan, decide=decide)

    assert calls == 1
    assert reviewed.status == "approved"
    assert {item.approval.status for item in reviewed.operations} == {"approved"}


def test_proposal_writes_reviewable_mutation_candidate_when_requested(
    tmp_path: Path,
) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text(
        "Always invoke the skill at the start of every conversation.\n"
        "Run focused tests.\n",
        encoding="utf-8",
    )
    audit = inventory(target)
    actual_surface = replace(
        audit.surfaces[0],
        content_sha256=sha256_bytes(target.read_bytes()),
    )
    audit = replace(audit, surfaces=(actual_surface,))

    proposed = propose_inventory(
        audit,
        created_at="2026-08-06T11:00:00Z",
        candidate_dir=tmp_path / "candidates",
    )

    operation = proposed.operations[0]
    assert operation.action == "merge"
    assert operation.strategy == "full_file"
    assert Path(operation.source).read_text(encoding="utf-8") == "Run focused tests.\n"
    assert proposed.validation["protected_roots"] == [str(tmp_path)]


def test_review_can_edit_candidate_or_expand_relocation(tmp_path: Path) -> None:
    source = tmp_path / "AGENTS.md"
    source.write_text("old", encoding="utf-8")
    replacement = tmp_path / "replacement.md"
    replacement.write_text("edited", encoding="utf-8")
    base = PlanOperation(
        id="candidate",
        action="merge",
        target=str(source),
        source=str(source),
        ownership="repository",
        strategy="full_file",
        precondition_sha256=sha256_bytes(b"old"),
        proposed_sha256=sha256_bytes(b"old"),
        backup_required=True,
        requires_confirmation=True,
        approval=Approval(),
        reason="fixture",
        evidence=(),
    )
    draft = Plan(
        schema_version=1,
        apu_version="0.1.0",
        created_at="2026-08-06T11:00:00Z",
        inventory_sha256="a" * 64,
        status="draft",
        operations=(base,),
    )

    edited = review_plan(
        draft,
        decide=lambda _: ReviewDecision(
            "approved", replacement_source=replacement
        ),
    )
    assert edited.status == "approved"
    assert edited.operations[0].source == str(replacement.resolve())
    assert edited.operations[0].proposed_sha256 == sha256_bytes(b"edited")

    destination = tmp_path / "moved.md"
    relocated = review_plan(
        draft,
        decide=lambda _: ReviewDecision(
            "approved", relocate_target=destination
        ),
    )
    assert relocated.status == "approved"
    assert [item.action for item in relocated.operations] == ["remove", "create"]
    assert {item.atomic_group_id for item in relocated.operations} == {
        "candidate-relocate"
    }

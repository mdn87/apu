from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import apu.rollback as rollback_module
from apu.campaigns import (
    load_campaign_index,
    reconcile_campaign,
    register_leaf_artifact,
)
from apu.models import Finding, InstructionSurface, Inventory, sha256_bytes
from apu.receipts import load_receipt
from apu.rollback import rollback_receipt
from apu.snapshots import create_snapshot
from apu.state import load_registry
from apu.system_audit import SystemInventory
from apu.system_campaign import (
    apply_campaign,
    campaign_has_work_orders,
    list_campaign_status,
    load_campaign_bundle,
    propose_campaign,
)
from apu.system_profile import ProfileRoot, SystemProfile


def _surface(
    path: Path,
    *,
    identifier: str,
    sensitive: bool = False,
    authority: str = "user",
) -> InstructionSurface:
    return InstructionSurface(
        id=identifier,
        path=str(path.resolve()),
        kind="agents",
        provider="codex",
        authority=authority,
        scope="global",
        real_path=str(path.resolve()),
        is_symlink=False,
        content_sha256=sha256_bytes(path.read_bytes()),
        mode="0644",
        precedence=10,
        sensitive=sensitive,
    )


def _finding(
    surface: InstructionSurface,
    *,
    identifier: str,
    category: str,
    line: int,
    evidence: tuple[str, ...] = ("fixture",),
) -> Finding:
    return Finding(
        id=identifier,
        surface_id=surface.id,
        location={"line": line},
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
        evidence=evidence,
        summary=f"{category} requires remediation.",
    )


def _fixture(
    tmp_path: Path,
    *,
    secret: str = "sk-proj-ABCDEFGHIJKLMNOP",
) -> tuple[SystemProfile, SystemInventory, Path, str]:
    projects = tmp_path / "projects"
    projects.mkdir()
    global_root = tmp_path / "home" / ".codex"
    global_root.mkdir(parents=True)
    auto_target = global_root / "AGENTS.md"
    auto_target.write_text("keep\nremove duplicate\n", encoding="utf-8")
    sensitive_target = global_root / "settings.json"
    sensitive_target.write_text(
        f'{{"api_key":"{secret}","policy":"review"}}\n',
        encoding="utf-8",
    )
    profile = SystemProfile(
        schema_version=1,
        roots=(ProfileRoot(path=str(projects.resolve()), excludes=()),),
        global_surfaces=(str(global_root.resolve()),),
        packages=(),
        guidance_sources=(),
        remediation_policy={
            "duplicate-instruction": "auto",
            "guidance-conflict": "work-order",
            "sensitive-material-exposure": "ignore",
        },
    )
    auto_surface = _surface(auto_target, identifier="auto")
    sensitive_surface = _surface(
        sensitive_target,
        identifier="sensitive",
        sensitive=True,
    )
    machine = Inventory(
        schema_version=1,
        apu_version="0.3.0.dev0",
        generated_at="2026-08-06T22:00:00Z",
        scope={"roots": [str(global_root)]},
        surfaces=(auto_surface, sensitive_surface),
        findings=(
            _finding(
                auto_surface,
                identifier="duplicate",
                category="duplicate-instruction",
                line=2,
            ),
            _finding(
                sensitive_surface,
                identifier="judgment",
                category="guidance-conflict",
                line=1,
            ),
            _finding(
                sensitive_surface,
                identifier="credential",
                category="sensitive-material-exposure",
                line=1,
            ),
        ),
    )
    inventory = SystemInventory(
        schema_version=1,
        apu_version="0.3.0.dev0",
        generated_at="2026-08-06T22:00:00Z",
        profile_sha256=profile.artifact_sha256,
        machine_inventory=machine,
        repositories=(),
    )
    return profile, inventory, auto_target, secret


def test_proposal_persists_private_redacted_campaign_artifacts(
    tmp_path: Path,
) -> None:
    profile, inventory, _target, secret = _fixture(tmp_path)
    state = tmp_path / "state"
    exported = tmp_path / "exports"

    bundle, warnings = propose_campaign(
        state,
        inventory,
        profile,
        created_at="2026-08-06T22:01:00Z",
        emit_prompts=exported,
    )

    root = Path(bundle["campaign_directory"])
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["campaign_id"] == bundle["campaign_id"]
    assert len(bundle["work_orders"]) == 2
    assert any(item["manual_only"] for item in bundle["work_orders"])
    assert any(
        item["requires_sanitized_stage"] for item in bundle["work_orders"]
    )
    rendered = "\n".join(
        Path(item["path"]).read_text(encoding="utf-8")
        for item in bundle["work_orders"]
    )
    assert secret not in rendered
    assert secret not in "\n".join(
        path.read_text(encoding="utf-8")
        for path in exported.glob("*.md")
    )
    assert (root / "redactions").is_dir()
    assert (root / "staging").is_dir()
    assert warnings and all("outside APU state protection" in item for item in warnings)


def test_campaign_apply_snapshots_then_stamps_receipt(
    tmp_path: Path,
) -> None:
    profile, inventory, target, _secret = _fixture(tmp_path)
    state = tmp_path / "state"
    bundle, _warnings = propose_campaign(
        state,
        inventory,
        profile,
        created_at="2026-08-06T22:01:00Z",
    )

    result = apply_campaign(
        state,
        bundle,
        profile,
        installation_id="campaign-apply-1",
    )

    assert result["status"] == "applied"
    assert result["snapshot_id"]
    assert target.read_text(encoding="utf-8") == "keep\n"
    receipt = load_receipt(Path(result["receipt"]))
    assert receipt["campaign_id"] == bundle["campaign_id"]
    assert receipt["snapshot_id"] == result["snapshot_id"]
    index = json.loads(
        (
            Path(bundle["campaign_directory"]) / "index.json"
        ).read_text(encoding="utf-8")
    )
    assert index["snapshot_id"] == result["snapshot_id"]
    assert {item["artifact_type"] for item in index["artifacts"]} >= {
        "plan",
        "work-order",
        "snapshot",
        "receipt",
    }
    receipt_leaf = (
        Path(bundle["campaign_directory"])
        / "artifacts"
        / "receipt"
        / "campaign-apply-1.json"
    )
    receipt_leaf.unlink()
    assert not receipt_leaf.exists()
    status = list_campaign_status(state)
    assert status[0]["reconcile_error"] is None
    assert receipt_leaf.exists()


def test_tampered_exported_auto_plan_is_rejected_before_mutation(
    tmp_path: Path,
) -> None:
    profile, inventory, target, _secret = _fixture(tmp_path)
    state = tmp_path / "state"
    bundle, _warnings = propose_campaign(
        state,
        inventory,
        profile,
        created_at="2026-08-06T22:01:00Z",
    )
    tampered = json.loads(json.dumps(bundle))
    replacement_target = tmp_path / "outside.txt"
    replacement_target.write_text("outside\n", encoding="utf-8")
    tampered["auto_plan"]["operations"][0]["target"] = str(
        replacement_target.resolve()
    )
    exported = tmp_path / "tampered-campaign.json"
    exported.write_text(json.dumps(tampered), encoding="utf-8")
    loaded = load_campaign_bundle(exported)

    with pytest.raises(ValueError, match="supplied auto plan differs"):
        apply_campaign(
            state,
            loaded,
            profile,
            installation_id="tampered-auto-plan",
        )

    assert target.read_text(encoding="utf-8") == "keep\nremove duplicate\n"
    assert replacement_target.read_text(encoding="utf-8") == "outside\n"


def test_cleared_exported_work_orders_cannot_bypass_canonical_gate(
    tmp_path: Path,
) -> None:
    profile, inventory, target, _secret = _fixture(tmp_path)
    state = tmp_path / "state"
    bundle, _warnings = propose_campaign(
        state,
        inventory,
        profile,
        created_at="2026-08-06T22:01:00Z",
    )
    tampered = json.loads(json.dumps(bundle))
    tampered["work_orders"] = []

    with pytest.raises(ValueError, match="work orders do not match"):
        campaign_has_work_orders(state, tampered)

    assert target.read_text(encoding="utf-8") == "keep\nremove duplicate\n"


def test_forged_out_of_profile_inventory_is_rejected_before_persistence(
    tmp_path: Path,
) -> None:
    profile, inventory, _target, _secret = _fixture(tmp_path)
    outside = tmp_path / "outside" / "AGENTS.md"
    outside.parent.mkdir()
    outside.write_text("keep\nremove duplicate\n", encoding="utf-8")
    outside_surface = _surface(outside, identifier="outside")
    forged_machine = Inventory(
        schema_version=1,
        apu_version=inventory.apu_version,
        generated_at=inventory.generated_at,
        scope={"roots": [str(outside.parent)]},
        surfaces=(outside_surface,),
        findings=(
            _finding(
                outside_surface,
                identifier="outside-duplicate",
                category="duplicate-instruction",
                line=2,
            ),
        ),
    )
    forged = SystemInventory(
        schema_version=1,
        apu_version=inventory.apu_version,
        generated_at=inventory.generated_at,
        profile_sha256=profile.artifact_sha256,
        machine_inventory=forged_machine,
        repositories=(),
    )
    state = tmp_path / "state"

    with pytest.raises(ValueError, match="outside the selected profile"):
        propose_campaign(
            state,
            forged,
            profile,
            created_at="2026-08-06T22:01:00Z",
        )

    assert outside.read_text(encoding="utf-8") == "keep\nremove duplicate\n"
    assert not (state / "campaigns").exists()


def test_existing_snapshot_without_operation_target_blocks_apply(
    tmp_path: Path,
) -> None:
    profile, inventory, target, _secret = _fixture(tmp_path)
    state = tmp_path / "state"
    bundle, _warnings = propose_campaign(
        state,
        inventory,
        profile,
        created_at="2026-08-06T22:01:00Z",
    )
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("unrelated\n", encoding="utf-8")
    snapshot = create_snapshot(
        state,
        {"unrelated": unrelated},
        campaign_id=bundle["campaign_id"],
        created_at="2026-08-06T22:02:00Z",
    )
    register_leaf_artifact(
        state,
        bundle["campaign_id"],
        {
            "schema_version": 1,
            "campaign_id": bundle["campaign_id"],
            "artifact_type": "snapshot",
            "artifact_id": snapshot["snapshot_id"],
            "snapshot_id": snapshot["snapshot_id"],
        },
    )
    index = load_campaign_index(state, bundle["campaign_id"])
    reconcile_campaign(
        state,
        bundle["campaign_id"],
        expected_revision=index["revision"],
        purpose="test-snapshot-binding",
    )

    with pytest.raises(ValueError, match="does not cover operation"):
        apply_campaign(
            state,
            bundle,
            profile,
            installation_id="missing-snapshot-coverage",
        )

    assert target.read_text(encoding="utf-8") == "keep\nremove duplicate\n"


def test_package_authority_work_order_forbids_direct_target_edit(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    global_root = tmp_path / "home" / ".codex"
    package_root = global_root / "plugins" / "package"
    package_root.mkdir(parents=True)
    target = package_root / "SKILL.md"
    target.write_text("keep\nremove duplicate\n", encoding="utf-8")
    profile = SystemProfile(
        schema_version=1,
        roots=(ProfileRoot(path=str(projects.resolve()), excludes=()),),
        global_surfaces=(str(global_root.resolve()),),
        packages=("package@catalog",),
        guidance_sources=(),
        remediation_policy={"duplicate-instruction": "auto"},
    )
    package_surface = _surface(
        target,
        identifier="package",
        authority="package",
    )
    machine = Inventory(
        schema_version=1,
        apu_version="0.3.0.dev0",
        generated_at="2026-08-06T22:00:00Z",
        scope={"roots": [str(global_root)]},
        surfaces=(package_surface,),
        findings=(
            _finding(
                package_surface,
                identifier="package-duplicate",
                category="duplicate-instruction",
                line=2,
            ),
        ),
    )
    inventory = SystemInventory(
        schema_version=1,
        apu_version="0.3.0.dev0",
        generated_at="2026-08-06T22:00:00Z",
        profile_sha256=profile.artifact_sha256,
        machine_inventory=machine,
        repositories=(),
    )

    bundle, _warnings = propose_campaign(
        tmp_path / "state",
        inventory,
        profile,
        created_at="2026-08-06T22:01:00Z",
    )

    assert bundle["auto_plan"]["operations"] == []
    rendered = Path(bundle["work_orders"][0]["path"]).read_text(
        encoding="utf-8"
    )
    assert "Do not edit the package-owned target directly" in rendered
    assert "upgrade, pin, fork, or package-management remediation" in rendered


def test_short_secret_is_replaced_in_sanitized_stage(
    tmp_path: Path,
) -> None:
    secret = "sk-proj-ABCDEFGHIJKL"
    profile, inventory, _target, returned_secret = _fixture(
        tmp_path,
        secret=secret,
    )

    bundle, _warnings = propose_campaign(
        tmp_path / "state",
        inventory,
        profile,
        created_at="2026-08-06T22:01:00Z",
    )

    root = Path(bundle["campaign_directory"])
    staged_files = list((root / "staging").glob("*.json"))
    assert len(staged_files) == 1
    staged = staged_files[0].read_text(encoding="utf-8")
    assert returned_secret not in staged
    assert "«APU-REDACTED-1»" in staged


def test_sensitive_finding_evidence_never_reaches_public_campaign_artifacts(
    tmp_path: Path,
) -> None:
    profile, inventory, _target, _secret = _fixture(tmp_path)
    exposed = "sk-proj-" + "Z" * 24
    machine = inventory.machine_inventory
    sensitive_surface = next(item for item in machine.surfaces if item.sensitive)
    findings = tuple(
        _finding(
            sensitive_surface,
            identifier=item.id,
            category=item.category,
            line=int(item.location["line"]),
            evidence=(exposed,),
        )
        if item.category == "guidance-conflict"
        else item
        for item in machine.findings
    )
    protected_inventory = replace(
        inventory,
        machine_inventory=replace(machine, findings=findings),
    )

    bundle, _warnings = propose_campaign(
        tmp_path / "state",
        protected_inventory,
        profile,
        created_at="2026-08-06T22:01:00Z",
    )

    assert exposed not in json.dumps(bundle)
    root = Path(bundle["campaign_directory"])
    for path in root.rglob("*"):
        if path.is_file() and "redactions" not in path.parts:
            assert exposed not in path.read_text(encoding="utf-8")


def test_campaign_rollback_preserves_receipt_and_appends_evidence(
    tmp_path: Path,
) -> None:
    profile, inventory, target, _secret = _fixture(tmp_path)
    state = tmp_path / "state"
    bundle, _warnings = propose_campaign(
        state,
        inventory,
        profile,
        created_at="2026-08-06T22:01:00Z",
    )
    applied = apply_campaign(
        state,
        bundle,
        profile,
        installation_id="campaign-rollback-1",
    )
    receipt_path = Path(applied["receipt"])
    immutable_receipt = receipt_path.read_bytes()

    result = rollback_receipt(receipt_path)

    assert result == {"status": "rolled_back", "drifted_operation_ids": []}
    assert target.read_text(encoding="utf-8") == "keep\nremove duplicate\n"
    assert receipt_path.read_bytes() == immutable_receipt
    status = list_campaign_status(state)[0]
    assert status["reconcile_error"] is None
    assert any(
        item["artifact_type"] == "rollback"
        for item in status["artifacts"]
    )
    assert rollback_receipt(receipt_path) == result


def test_campaign_rollback_repairs_registry_from_committed_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, inventory, target, _secret = _fixture(tmp_path)
    state = tmp_path / "state"
    bundle, _warnings = propose_campaign(
        state,
        inventory,
        profile,
        created_at="2026-08-06T22:01:00Z",
    )
    applied = apply_campaign(
        state,
        bundle,
        profile,
        installation_id="campaign-rollback-retry",
    )
    receipt_path = Path(applied["receipt"])
    update_registry = rollback_module.update_registry

    def fail_registry_update(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected registry write failure")

    monkeypatch.setattr(
        rollback_module,
        "update_registry",
        fail_registry_update,
    )
    with pytest.raises(OSError, match="injected registry"):
        rollback_receipt(receipt_path)

    assert target.read_text(encoding="utf-8") == "keep\nremove duplicate\n"
    assert (
        load_registry(state)["installations"]["campaign-rollback-retry"][
            "status"
        ]
        == "active"
    )

    monkeypatch.setattr(rollback_module, "update_registry", update_registry)
    result = rollback_receipt(receipt_path)

    assert result == {"status": "rolled_back", "drifted_operation_ids": []}
    assert (
        load_registry(state)["installations"]["campaign-rollback-retry"][
            "status"
        ]
        == "rolled_back"
    )
    assert list_campaign_status(state)[0]["reconcile_error"] is None


def test_campaign_rollback_resumes_when_leaf_write_was_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, inventory, target, _secret = _fixture(tmp_path)
    state = tmp_path / "state"
    bundle, _warnings = propose_campaign(
        state,
        inventory,
        profile,
        created_at="2026-08-06T22:01:00Z",
    )
    applied = apply_campaign(
        state,
        bundle,
        profile,
        installation_id="campaign-rollback-resume",
    )
    receipt_path = Path(applied["receipt"])
    register_leaf = rollback_module.register_leaf_artifact

    def fail_leaf_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected rollback leaf failure")

    monkeypatch.setattr(
        rollback_module,
        "register_leaf_artifact",
        fail_leaf_write,
    )
    with pytest.raises(OSError, match="injected rollback leaf"):
        rollback_receipt(receipt_path)

    assert target.read_text(encoding="utf-8") == "keep\nremove duplicate\n"
    monkeypatch.setattr(
        rollback_module,
        "register_leaf_artifact",
        register_leaf,
    )

    assert rollback_receipt(receipt_path) == {
        "status": "rolled_back",
        "drifted_operation_ids": [],
    }
    assert list_campaign_status(state)[0]["reconcile_error"] is None


def test_status_rebuilds_lost_campaign_index_and_surfaces_lock_metadata(
    tmp_path: Path,
) -> None:
    profile, inventory, _target, _secret = _fixture(tmp_path)
    state = tmp_path / "state"
    bundle, _warnings = propose_campaign(
        state,
        inventory,
        profile,
        created_at="2026-08-06T22:01:00Z",
    )
    root = Path(bundle["campaign_directory"])
    (root / "index.json").unlink()
    for path in (root / "revisions").glob("*.json"):
        path.unlink()

    status = list_campaign_status(state)[0]

    assert status["reconcile_error"] is None
    assert status["artifacts"]
    assert status["lock"]["purpose"] == "status-reconcile"
    assert status["lock"]["released_at"] is not None
    assert (root / "index.json").is_file()

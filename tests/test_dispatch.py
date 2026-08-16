from __future__ import annotations

import json
from pathlib import Path

import pytest

from apu.dispatch import (
    DispatchRejectedError,
    DispatchUnavailableError,
    IsolationProbeResult,
    dispatch_work_order,
)
from apu.models import Finding, InstructionSurface, Inventory, Plan, sha256_bytes
from apu.snapshots import load_snapshot
from apu.system_audit import (
    SYSTEM_INVENTORY_SCHEMA_VERSION,
    EvaluationContext,
    SystemInventory,
)
from apu.system_campaign import propose_campaign
from apu.system_profile import ProfileRoot, SystemProfile

SECRET = "sk-proj-" + "x" * 30


def _campaign(
    tmp_path: Path,
    *,
    sensitive: bool = False,
    manual_only: bool = False,
    additional_work_order: bool = False,
) -> tuple[Path, dict, Path, str]:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    target = project / ("settings.env" if sensitive else "AGENTS.md")
    content = (
        f"OPENAI_API_KEY={SECRET}\nMODE=strict\n"
        if sensitive
        else "Always invoke a skill before every response.\n"
    )
    target.write_bytes(content.encode("utf-8"))
    category = "sensitive-material-exposure" if manual_only else "guidance-conflict"
    surface = InstructionSurface(
        id="surface-1",
        path=str(target.resolve()),
        kind="agents",
        provider="codex",
        authority="user",
        scope="project",
        real_path=str(target.resolve()),
        is_symlink=False,
        content_sha256=sha256_bytes(target.read_bytes()),
        mode="0644",
        precedence=10,
        sensitive=sensitive,
    )
    finding = Finding(
        id="finding-1",
        surface_id=surface.id,
        location={"line": 1},
        category=category,
        severity="high",
        confidence="high",
        analysis_method=("structural" if manual_only else "heuristic"),
        evidence=("fixture",),
        summary=f"{category} requires remediation.",
    )
    surfaces = [surface]
    findings = [finding]
    if additional_work_order:
        additional_target = project / "CLAUDE.md"
        additional_target.write_text("Always use every tool.\n", encoding="utf-8")
        additional_surface = InstructionSurface(
            id="surface-2",
            path=str(additional_target.resolve()),
            kind="claude-instructions",
            provider="claude",
            authority="user",
            scope="project",
            real_path=str(additional_target.resolve()),
            is_symlink=False,
            content_sha256=sha256_bytes(additional_target.read_bytes()),
            mode="0644",
            precedence=10,
            sensitive=False,
        )
        surfaces.append(additional_surface)
        findings.append(
            Finding(
                id="finding-2",
                surface_id=additional_surface.id,
                location={"line": 1},
                category="guidance-conflict",
                severity="high",
                confidence="high",
                analysis_method="heuristic",
                evidence=("fixture",),
                summary="guidance-conflict requires remediation.",
            )
        )
    profile = SystemProfile(
        schema_version=1,
        roots=(ProfileRoot(path=str(project.resolve()), excludes=()),),
        global_surfaces=(),
        packages=(),
        guidance_sources=(),
        remediation_policy={
            "guidance-conflict": "work-order",
            "sensitive-material-exposure": "ignore",
        },
    )
    machine = Inventory(
        schema_version=1,
        apu_version="0.3.0.dev0",
        generated_at="2026-08-07T01:00:00Z",
        scope={"roots": [str(project.resolve())]},
        surfaces=tuple(surfaces),
        findings=tuple(findings),
    )
    inventory = SystemInventory(
        schema_version=SYSTEM_INVENTORY_SCHEMA_VERSION,
        apu_version="0.3.0.dev0",
        generated_at="2026-08-07T01:00:00Z",
        profile_sha256=profile.artifact_sha256,
        machine_inventory=machine,
        repositories=(),
        evaluation_context=EvaluationContext.unconfigured(),
    )
    state = tmp_path / "state"
    bundle, _ = propose_campaign(
        state,
        inventory,
        profile,
        created_at="2026-08-07T01:01:00Z",
    )
    return state, bundle, target, content


def _passing_probe(request):
    assert request.probe_target.is_file()
    assert request.live_root == request.probe_target.parent
    return IsolationProbeResult(
        context_id="sandbox-1",
        mechanism="test-denied-write-open",
        attempted=True,
        write_denied=True,
    )


def test_dispatch_builds_private_draft_plan_and_campaign_receipt(
    tmp_path: Path,
) -> None:
    state, bundle, target, original = _campaign(tmp_path)
    order = bundle["work_orders"][0]
    seen = {}

    def runner(request):
        seen["request"] = request
        assert request.isolation_context_id == "sandbox-1"
        assert (
            request.staged_files[str(target.resolve())].read_text(encoding="utf-8")
            == original
        )
        return {
            "files": {str(target.resolve()): "Invoke a skill only when relevant.\n"}
        }

    result = dispatch_work_order(
        state,
        bundle["campaign_id"],
        order["work_order_id"],
        runner=runner,
        isolation_probe=_passing_probe,
        attempt=1,
        created_at="2026-08-07T01:02:00Z",
    )

    assert result.status == "accepted"
    assert result.plan_path is not None
    plan = Plan.from_dict(json.loads(result.plan_path.read_text(encoding="utf-8")))
    plan.validate()
    assert plan.status == "draft"
    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.target == str(target.resolve())
    assert operation.approval.status == "pending"
    assert operation.precondition_sha256 == sha256_bytes(original.encode())
    assert Path(operation.source or "").read_text(encoding="utf-8") == (
        "Invoke a skill only when relevant.\n"
    )
    assert target.read_text(encoding="utf-8") == original
    leaf = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    assert leaf["artifact_type"] == "work-order-result"
    assert leaf["snapshot_id"] == result.snapshot_id
    assert leaf["idempotency_key"] == {
        "operation_id": f"dispatch-{order['work_order_id']}",
        "attempt": 1,
    }
    assert "transcript" not in leaf
    assert not list(
        (
            Path(bundle["campaign_directory"])
            / "dispatch-stages"
            / order["work_order_id"]
        ).glob("attempt-*")
    )


def test_first_dispatch_snapshot_covers_every_campaign_target(
    tmp_path: Path,
) -> None:
    state, bundle, target, _original = _campaign(
        tmp_path,
        additional_work_order=True,
    )
    order = next(
        item
        for item in bundle["work_orders"]
        if str(target) in Path(item["path"]).read_text(encoding="utf-8")
    )

    result = dispatch_work_order(
        state,
        bundle["campaign_id"],
        order["work_order_id"],
        runner=lambda _request: {
            "files": {str(target.resolve()): "Use relevant tools.\n"}
        },
        isolation_probe=_passing_probe,
        created_at="2026-08-07T01:02:00Z",
    )

    snapshot = load_snapshot(state, result.snapshot_id)
    assert {entry["target_path"] for entry in snapshot["entries"]} == {
        str(target.resolve()),
        str((target.parent / "CLAUDE.md").resolve()),
    }


def test_sanitized_stage_never_reaches_runner_with_secret(
    tmp_path: Path,
) -> None:
    state, bundle, target, original = _campaign(tmp_path, sensitive=True)
    order = bundle["work_orders"][0]
    assert order["requires_sanitized_stage"] is True

    def runner(request):
        staged = request.staged_files[str(target.resolve())].read_text(encoding="utf-8")
        assert SECRET not in staged
        assert "«APU-REDACTED-1»" in staged
        return {
            "files": {
                str(target.resolve()): staged.replace("MODE=strict", "MODE=reviewed")
            }
        }

    result = dispatch_work_order(
        state,
        bundle["campaign_id"],
        order["work_order_id"],
        runner=runner,
        isolation_probe=_passing_probe,
        created_at="2026-08-07T01:02:00Z",
    )

    assert result.status == "accepted"
    plan = Plan.from_dict(json.loads(result.plan_path.read_text(encoding="utf-8")))
    source = Path(plan.operations[0].source or "")
    assert source.read_text(encoding="utf-8") == (
        f"OPENAI_API_KEY={SECRET}\nMODE=reviewed\n"
    )
    assert target.read_text(encoding="utf-8") == original


def test_unsafe_runner_return_is_quarantined_without_body_or_transcript(
    tmp_path: Path,
) -> None:
    state, bundle, target, _ = _campaign(tmp_path)
    order = bundle["work_orders"][0]
    returned_secret = "ghp_" + "z" * 30

    result = dispatch_work_order(
        state,
        bundle["campaign_id"],
        order["work_order_id"],
        runner=lambda _request: {
            "files": {str(target.resolve()): f"TOKEN={returned_secret}\n"}
        },
        isolation_probe=_passing_probe,
        created_at="2026-08-07T01:02:00Z",
    )

    assert result.status == "quarantined"
    assert result.plan_path is None
    persisted = result.artifact_path.read_text(encoding="utf-8")
    assert returned_secret not in persisted
    assert "TOKEN=" not in persisted
    assert "transcript" not in persisted
    leaf = json.loads(persisted)
    assert leaf["candidate_sha256"]
    assert leaf["plan"] is None
    assert any("before persistence" in reason for reason in leaf["reasons"])


def test_manual_only_and_unproven_isolation_never_call_runner(
    tmp_path: Path,
) -> None:
    state, bundle, _target, _ = _campaign(
        tmp_path / "manual",
        sensitive=True,
        manual_only=True,
    )
    order = bundle["work_orders"][0]
    calls = []
    with pytest.raises(DispatchRejectedError, match="manual-only"):
        dispatch_work_order(
            state,
            bundle["campaign_id"],
            order["work_order_id"],
            runner=lambda request: calls.append(request) or {"files": {}},
            isolation_probe=_passing_probe,
        )
    assert calls == []

    state, bundle, _target, _ = _campaign(tmp_path / "unavailable")
    order = bundle["work_orders"][0]
    with pytest.raises(DispatchUnavailableError, match="not proven"):
        dispatch_work_order(
            state,
            bundle["campaign_id"],
            order["work_order_id"],
            runner=lambda request: calls.append(request) or {"files": {}},
            isolation_probe=lambda _request: IsolationProbeResult(
                context_id="sandbox-2",
                mechanism="unsupported",
                attempted=True,
                write_denied=False,
            ),
        )
    assert calls == []


def test_changed_input_fails_before_probe_and_exact_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    state, bundle, target, _ = _campaign(tmp_path / "changed")
    order = bundle["work_orders"][0]
    calls = []
    target.write_text("changed after proposal\n", encoding="utf-8")
    with pytest.raises(DispatchRejectedError, match="no longer match"):
        dispatch_work_order(
            state,
            bundle["campaign_id"],
            order["work_order_id"],
            runner=lambda request: calls.append(request) or {"files": {}},
            isolation_probe=lambda request: (
                calls.append(request) or _passing_probe(request)
            ),
        )
    assert calls == []

    state, bundle, target, _ = _campaign(tmp_path / "retry")
    order = bundle["work_orders"][0]
    counts = {"probe": 0, "runner": 0}

    def probe(request):
        counts["probe"] += 1
        return _passing_probe(request)

    def runner(_request):
        counts["runner"] += 1
        return {"files": {str(target.resolve()): "reviewed\n"}}

    first = dispatch_work_order(
        state,
        bundle["campaign_id"],
        order["work_order_id"],
        runner=runner,
        isolation_probe=probe,
        created_at="2026-08-07T01:02:00Z",
    )
    second = dispatch_work_order(
        state,
        bundle["campaign_id"],
        order["work_order_id"],
        runner=runner,
        isolation_probe=probe,
        created_at="2026-08-07T01:03:00Z",
    )
    assert second == first
    assert counts == {"probe": 1, "runner": 1}

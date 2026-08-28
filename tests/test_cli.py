from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from apu.cli import main
from apu.models import sha256_bytes
from apu.outcomes import append_outcome
from apu.receipts import write_receipt
from apu.state import update_registry


def test_audit_and_propose_round_trip_without_creating_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text(
        "Run focused tests for changed behavior.\n"
        "Run focused tests for changed behavior.\n"
    )
    inventory = tmp_path / "inventory.json"
    plan = tmp_path / "plan.json"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APU_HOME", str(state))

    assert main(["audit", str(repo), "--json", str(inventory)]) == 0
    assert inventory.is_file()
    assert not state.exists()
    assert main(["propose", "--inventory", str(inventory), "--output", str(plan)]) == 0
    artifact = json.loads(plan.read_text())
    assert artifact["inventory_sha256"]
    assert artifact["operations"]
    assert artifact["operations"][0]["action"] == "merge"
    assert Path(artifact["operations"][0]["source"]).is_file()
    assert not state.exists()
    capsys.readouterr()


def test_root_session_id_without_sessions_is_usage_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as error:
        main(["audit", str(tmp_path), "--root-session-id", "root"])

    assert error.value.code == 2


def test_init_defaults_to_preview_and_saved_draft_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Repository guidance.\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APU_HOME", str(state))

    assert main(["init", str(repo)]) == 0

    plans = list((state / "plans").glob("*.json"))
    assert len(plans) == 1
    artifact = json.loads(plans[0].read_text())
    assert artifact["status"] == "draft"
    assert any(item["action"] == "symlink" for item in artifact["operations"])
    assert not (home / ".agents" / "skills").exists()
    assert "Draft plan:" in capsys.readouterr().out


def test_status_reports_monitoring_progress_for_registered_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    state = tmp_path / "state"
    target = tmp_path / "installed.md"
    target.write_text("installed", encoding="utf-8")
    receipt = write_receipt(
        state,
        {
            "schema_version": 1,
            "installation_id": "install-1",
            "created_at": "2026-08-01T00:00:00Z",
            "operations": [
                {
                    "id": "op-1",
                    "target": str(target),
                    "original_sha256": None,
                    "installed_sha256": sha256_bytes(b"installed"),
                    "backup_path": None,
                }
            ],
            "rollback_status": "available",
        },
    )
    update_registry(
        state,
        "install-1",
        {
            "status": "active",
            "receipt": str(receipt.relative_to(state)),
            "monitoring_started_at": "2026-08-01T00:00:00Z",
        },
    )
    append_outcome(
        state,
        {
            "schema_version": 1,
            "installation_id": "install-1",
            "recorded_at": "2026-08-02T00:00:00Z",
            "task_id": "task-1",
            "material": True,
            "source": "user",
            "elapsed_seconds": None,
            "agent_count": None,
            "review_count": None,
            "remediation_count": None,
            "validation": "passed",
            "rework": False,
            "escaped_defect": {
                "present": False,
                "severity": "none",
                "category": None,
            },
            "notes": None,
        },
    )
    monkeypatch.setenv("APU_HOME", str(state))

    assert main(["status"]) == 0
    output = json.loads(capsys.readouterr().out)
    monitoring = output["installations"]["install-1"]["monitoring"]
    assert monitoring["material_task_count"] == 1
    assert monitoring["required_days"] == 30
    assert monitoring["required_material_tasks"] == 10


def test_root_cli_exposes_reviewable_hook_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert (
        main(
            [
                "hooks",
                "render",
                "--provider",
                "codex",
                "--scope",
                "user",
            ]
        )
        == 0
    )

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["target"] == str(home / ".codex" / "hooks.json")
    assert rendered["policy_changes"] is False
    assert rendered["trust_changes"] is False
    assert not (home / ".codex" / "hooks.json").exists()


def test_init_explicit_apply_uses_visible_copy_fallback_and_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Repository guidance.\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APU_HOME", str(state))
    monkeypatch.setattr("apu.cli._supports_directory_symlink", lambda _: False)
    monkeypatch.setattr("builtins.input", lambda _: "a")

    class InteractiveInput:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", InteractiveInput())

    assert main(["init", str(repo), "--apply", "--yes"]) == 0

    assert (
        home / ".agents" / "skills" / "optimizing-agent-instructions" / "SKILL.md"
    ).is_file()
    assert (
        home / ".claude" / "skills" / "optimizing-agent-instructions" / "SKILL.md"
    ).is_file()
    registry = json.loads((state / "registry.json").read_text(encoding="utf-8"))
    entry = next(iter(registry["installations"].values()))
    assert entry["status"] == "active"
    assert entry["monitoring_started_at"]
    assert '"structural_validation"' in capsys.readouterr().out


def test_cli_review_accepts_an_edited_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("old", encoding="utf-8")
    initial = tmp_path / "initial.md"
    initial.write_text("initial", encoding="utf-8")
    replacement = tmp_path / "replacement.md"
    replacement.write_text("edited", encoding="utf-8")
    from apu.models import Approval, Plan, PlanOperation, canonical_json

    draft = Plan(
        schema_version=1,
        apu_version="0.1.0",
        created_at="2026-08-06T00:00:00Z",
        inventory_sha256="a" * 64,
        status="draft",
        operations=(
            PlanOperation(
                id="edit-me",
                action="merge",
                target=str(target),
                source=str(initial),
                ownership="repository",
                strategy="full_file",
                precondition_sha256=sha256_bytes(b"old"),
                proposed_sha256=sha256_bytes(b"initial"),
                backup_required=True,
                requires_confirmation=True,
                approval=Approval(),
                reason="fixture",
                evidence=(),
            ),
        ),
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(canonical_json(draft.to_dict()), encoding="utf-8")
    answers = iter(("e", str(replacement)))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    assert main(["review", str(plan_path)]) == 0
    reviewed = json.loads(plan_path.read_text(encoding="utf-8"))
    assert reviewed["status"] == "approved"
    assert reviewed["operations"][0]["source"] == str(replacement.resolve())
    assert reviewed["operations"][0]["proposed_sha256"] == sha256_bytes(b"edited")
    capsys.readouterr()

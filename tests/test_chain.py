from __future__ import annotations

import json
from pathlib import Path

import pytest

from apu.apply import ApplyError, apply_plan
from apu.cli import build_parser, main
from apu.models import Approval, Plan, PlanOperation, sha256_bytes
from apu.receipts import load_receipt, write_receipt
from apu.rollback import rollback_receipt
from apu.state import load_registry, update_registry


def _plan(operation: PlanOperation) -> Plan:
    return Plan(
        schema_version=1,
        apu_version="0.1.0",
        created_at="2026-08-06T10:00:00Z",
        inventory_sha256="a" * 64,
        status="approved",
        operations=(operation,),
    )


def _install(
    tmp_path: Path,
    state: Path,
    installation_id: str,
    target: Path,
    after: bytes,
    **kwargs,
) -> Path:
    """Apply a merge when the target exists, a create when it does not."""

    source = tmp_path / f"{installation_id}.src"
    source.write_bytes(after)
    exists = target.exists()
    operation = PlanOperation(
        id="update",
        action="merge" if exists else "create",
        target=str(target),
        source=str(source),
        ownership="apu",
        strategy="full_file",
        precondition_sha256=sha256_bytes(target.read_bytes()) if exists else None,
        proposed_sha256=sha256_bytes(after),
        backup_required=exists,
        requires_confirmation=False,
        approval=Approval(status="approved"),
        reason="fixture",
        evidence=(),
    )
    return apply_plan(
        _plan(operation), state_home=state, installation_id=installation_id, **kwargs
    )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "state"
    monkeypatch.setenv("APU_HOME", str(state))
    target = tmp_path / "hook.mjs"
    target.write_bytes(b"original")
    return {"tmp": tmp_path, "state": state, "target": target}


def _status(capsys) -> tuple[int, dict]:
    code = main(["status"])
    return code, json.loads(capsys.readouterr().out)


def test_a_contiguous_chain_installs_without_acknowledgment(world) -> None:
    _install(world["tmp"], world["state"], "install-1", world["target"], b"one")
    receipt = _install(world["tmp"], world["state"], "install-2", world["target"], b"two")

    assert "drift_acknowledgments" not in load_receipt(receipt)
    assert world["target"].read_bytes() == b"two"


def test_a_continuous_chain_leaves_status_green_and_the_older_receipt_superseded(
    world, capsys
) -> None:
    _install(world["tmp"], world["state"], "install-1", world["target"], b"one")
    _install(world["tmp"], world["state"], "install-2", world["target"], b"two")

    code, output = _status(capsys)

    assert code == 0
    checks = {c["name"]: c for c in output["validation"]["checks"]}
    assert checks["installation:install-2"]["status"] == "passed"
    assert checks["installation:install-1"]["status"] == "passed"
    assert "superseded by install-2" in checks["installation:install-1"]["reason"]
    assert "continuous" in checks["installation:install-1"]["reason"]


def test_a_continuous_chain_is_undone_by_rolling_back_the_newer_install(
    world, capsys
) -> None:
    _install(world["tmp"], world["state"], "install-1", world["target"], b"one")
    new = _install(world["tmp"], world["state"], "install-2", world["target"], b"two")

    assert rollback_receipt(new)["status"] == "rolled_back"

    code, output = _status(capsys)
    assert code == 0
    checks = {c["name"]: c for c in output["validation"]["checks"]}
    assert checks["installation:install-1"]["reason"] == "receipt and installed targets are valid"


def test_a_continuous_chain_does_not_excuse_a_later_unacknowledged_edit(
    world, capsys
) -> None:
    _install(world["tmp"], world["state"], "install-1", world["target"], b"one")
    _install(world["tmp"], world["state"], "install-2", world["target"], b"two")
    world["target"].write_bytes(b"edited with no acknowledgment")

    code, output = _status(capsys)

    assert code == 1
    checks = {c["name"]: c["status"] for c in output["validation"]["checks"]}
    assert checks["installation:install-2"] == "failed"


def test_a_chain_gap_is_refused_before_anything_changes(world) -> None:
    _install(world["tmp"], world["state"], "install-1", world["target"], b"one")
    world["target"].write_bytes(b"edited elsewhere")
    registry_before = load_registry(world["state"])

    with pytest.raises(ApplyError, match="--acknowledge-drift"):
        _install(world["tmp"], world["state"], "install-2", world["target"], b"two")

    assert world["target"].read_bytes() == b"edited elsewhere"
    assert load_registry(world["state"]) == registry_before
    assert not (world["state"] / "installations" / "install-2").exists()


def test_the_refusal_names_the_target_the_prior_install_and_both_hashes(world) -> None:
    _install(world["tmp"], world["state"], "install-1", world["target"], b"one")
    world["target"].write_bytes(b"edited elsewhere")

    with pytest.raises(ApplyError) as raised:
        _install(world["tmp"], world["state"], "install-2", world["target"], b"two")

    message = str(raised.value)
    assert str(world["target"]) in message
    assert "install-1" in message
    assert sha256_bytes(b"one")[:12] in message
    assert sha256_bytes(b"edited elsewhere")[:12] in message


def test_an_acknowledged_gap_installs_records_the_acknowledgment_and_supersedes(
    world, capsys
) -> None:
    old = _install(world["tmp"], world["state"], "install-1", world["target"], b"one")
    world["target"].write_bytes(b"edited elsewhere")
    old_bytes = old.read_bytes()
    registry_before = load_registry(world["state"])["installations"]["install-1"]

    new = _install(
        world["tmp"],
        world["state"],
        "install-2",
        world["target"],
        b"two",
        acknowledge_drift="hook is edited in place by other work",
    )

    assert world["target"].read_bytes() == b"two"
    assert load_receipt(new)["drift_acknowledgments"] == [
        {
            "target": str(world["target"]),
            "prior_installation_id": "install-1",
            "prior_installed_sha256": sha256_bytes(b"one"),
            "observed_original_sha256": sha256_bytes(b"edited elsewhere"),
            "reason": "hook is edited in place by other work",
        }
    ]
    assert old.read_bytes() == old_bytes
    assert load_registry(world["state"])["installations"]["install-1"] == registry_before

    code, output = _status(capsys)
    assert code == 0
    checks = {c["name"]: c for c in output["validation"]["checks"]}
    assert checks["installation:install-2"]["status"] == "passed"
    assert checks["installation:install-1"]["status"] == "passed"
    assert "superseded by install-2" in checks["installation:install-1"]["reason"]
    assert "hook is edited in place by other work" in checks["installation:install-1"]["reason"]


def test_the_acknowledgment_does_not_weaken_the_guard_on_later_edits(world, capsys) -> None:
    _install(world["tmp"], world["state"], "install-1", world["target"], b"one")
    world["target"].write_bytes(b"edited elsewhere")
    _install(
        world["tmp"], world["state"], "install-2", world["target"], b"two",
        acknowledge_drift="intended",
    )

    world["target"].write_bytes(b"edited again with no acknowledgment")

    code, output = _status(capsys)
    assert code == 1
    checks = {c["name"]: c["status"] for c in output["validation"]["checks"]}
    assert checks["installation:install-2"] == "failed"


def test_the_prior_entry_fails_again_when_its_superseding_install_is_rolled_back(
    world, capsys
) -> None:
    _install(world["tmp"], world["state"], "install-1", world["target"], b"one")
    world["target"].write_bytes(b"edited elsewhere")
    new = _install(
        world["tmp"], world["state"], "install-2", world["target"], b"two",
        acknowledge_drift="intended",
    )

    assert rollback_receipt(new)["status"] == "rolled_back"

    code, output = _status(capsys)
    assert code == 1
    checks = {c["name"]: c["status"] for c in output["validation"]["checks"]}
    assert checks["installation:install-1"] == "failed"


def test_an_acknowledgment_for_a_different_prior_hash_does_not_excuse_the_prior(
    world, capsys
) -> None:
    old = _install(world["tmp"], world["state"], "install-1", world["target"], b"one")
    world["target"].write_bytes(b"edited elsewhere")
    new = _install(
        world["tmp"], world["state"], "install-2", world["target"], b"two",
        acknowledge_drift="intended",
    )
    receipt = load_receipt(new)
    receipt["drift_acknowledgments"][0]["prior_installed_sha256"] = sha256_bytes(b"other")
    new.write_text(json.dumps(receipt), encoding="utf-8")

    code, output = _status(capsys)
    assert code == 1
    assert {c["name"]: c["status"] for c in output["validation"]["checks"]}[
        "installation:install-1"
    ] == "failed"
    assert old.exists()


def test_a_retired_prior_install_does_not_require_acknowledgment(world) -> None:
    old = _install(world["tmp"], world["state"], "install-1", world["target"], b"one")
    world["target"].write_bytes(b"edited elsewhere")
    assert main(["retire", "--receipt", str(old), "--reason", "r", "--allow-uncovered"]) == 0

    _install(world["tmp"], world["state"], "install-2", world["target"], b"two")

    assert world["target"].read_bytes() == b"two"


def test_a_file_apu_only_adopted_does_not_require_acknowledgment(world) -> None:
    adopted = sha256_bytes(world["target"].read_bytes())
    receipt = write_receipt(
        world["state"],
        {
            "schema_version": 1,
            "installation_id": "adopt-1",
            "created_at": "2026-08-01T00:00:00Z",
            "operations": [
                {
                    "id": "adopt",
                    "action": "merge",
                    "target": str(world["target"]),
                    "original_sha256": adopted,
                    "installed_sha256": adopted,
                    "backup_path": None,
                }
            ],
            "rollback_status": "available",
        },
    )
    update_registry(
        world["state"],
        "adopt-1",
        {"status": "active", "receipt": str(receipt.relative_to(world["state"]))},
    )
    world["target"].write_bytes(b"someone changed it later")

    _install(world["tmp"], world["state"], "install-2", world["target"], b"two")

    assert world["target"].read_bytes() == b"two"


def test_a_create_over_a_file_removed_out_of_band_is_a_gap(world) -> None:
    fresh = world["tmp"] / "fresh.md"
    _install(world["tmp"], world["state"], "install-1", fresh, b"one")
    fresh.unlink()

    with pytest.raises(ApplyError, match="--acknowledge-drift"):
        _install(world["tmp"], world["state"], "install-2", fresh, b"two")
    _install(
        world["tmp"], world["state"], "install-2", fresh, b"two",
        acknowledge_drift="removed on purpose",
    )
    receipt = load_receipt(world["state"] / "installations" / "install-2" / "receipt.json")
    assert receipt["drift_acknowledgments"][0]["observed_original_sha256"] is None


def test_an_acknowledgment_without_a_gap_is_not_recorded(world) -> None:
    _install(world["tmp"], world["state"], "install-1", world["target"], b"one")

    receipt = _install(
        world["tmp"], world["state"], "install-2", world["target"], b"two",
        acknowledge_drift="not needed",
    )

    assert "drift_acknowledgments" not in load_receipt(receipt)


def test_a_blank_acknowledgment_reason_is_rejected(world) -> None:
    with pytest.raises(ApplyError, match="reason"):
        _install(
            world["tmp"], world["state"], "install-1", world["target"], b"one",
            acknowledge_drift="   ",
        )


def test_the_apply_command_accepts_the_flag() -> None:
    args = build_parser().parse_args(
        ["apply", "plan.json", "--acknowledge-drift", "intended"]
    )
    assert args.acknowledge_drift == "intended"

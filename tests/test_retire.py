from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apu.apply import apply_plan
from apu.cli import main
from apu.models import Approval, Plan, PlanOperation, sha256_bytes
from apu.outcomes import append_outcome
from apu.rollback import RollbackError, rollback_receipt
from apu.state import load_registry, update_registry


def _operation(target: Path, source: Path, before: bytes, after: bytes) -> PlanOperation:
    return PlanOperation(
        id="update",
        action="merge",
        target=str(target),
        source=str(source),
        ownership="apu",
        strategy="full_file",
        precondition_sha256=sha256_bytes(before),
        proposed_sha256=sha256_bytes(after),
        backup_required=True,
        requires_confirmation=False,
        approval=Approval(status="approved"),
        reason="fixture",
        evidence=(),
    )


def _install(
    tmp_path: Path, state: Path, installation_id: str, target: Path, after: bytes
) -> Path:
    before = target.read_bytes()
    source = tmp_path / f"{installation_id}.src"
    source.write_bytes(after)
    plan = Plan(
        schema_version=1,
        apu_version="0.1.0",
        created_at="2026-08-06T10:00:00Z",
        inventory_sha256="a" * 64,
        status="approved",
        operations=(_operation(target, source, before, after),),
    )
    return apply_plan(plan, state_home=state, installation_id=installation_id)


@pytest.fixture
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """September-style install, an out-of-band edit, then a later install."""

    state = tmp_path / "state"
    monkeypatch.setenv("APU_HOME", str(state))
    target = tmp_path / "hook.mjs"
    target.write_bytes(b"original")
    old = _install(tmp_path, state, "install-old", target, b"old-installed")
    target.write_bytes(b"out-of-band-edit")
    new = _install(tmp_path, state, "install-new", target, b"new-installed")
    return {"state": state, "target": target, "old": old, "new": new}


def _status(capsys) -> tuple[int, dict]:
    code = main(["status"])
    return code, json.loads(capsys.readouterr().out)


def _retire(receipt: Path, *flags: str, reason: str = "hook is co-edited") -> int:
    return main(["retire", "--receipt", str(receipt), "--reason", reason, *flags])


def test_retiring_the_stale_receipt_turns_status_green_and_keeps_the_guard(
    chain, capsys
) -> None:
    code, output = _status(capsys)
    assert code == 1
    checks = {c["name"]: c["status"] for c in output["validation"]["checks"]}
    assert checks == {"installation:install-old": "failed", "installation:install-new": "passed"}

    assert _retire(chain["old"]) == 0
    capsys.readouterr()

    code, output = _status(capsys)
    assert code == 0
    checks = {c["name"]: c for c in output["validation"]["checks"]}
    assert checks["installation:install-new"]["status"] == "passed"
    assert checks["installation:install-old"]["status"] == "passed"
    assert "retired" in checks["installation:install-old"]["reason"]
    assert "hook is co-edited" in checks["installation:install-old"]["reason"]

    chain["target"].write_bytes(b"edited again with no acknowledgment")
    code, _ = _status(capsys)
    assert code == 1


def test_retire_records_the_acknowledgment_and_keeps_existing_entry_fields(chain) -> None:
    before = load_registry(chain["state"])["installations"]["install-old"]
    assert _retire(chain["old"]) == 0
    entry = load_registry(chain["state"])["installations"]["install-old"]

    assert entry["status"] == "retired"
    assert entry["retire_reason"] == "hook is co-edited"
    assert entry["superseded_by"] == ["install-new"]
    assert entry["live_sha256_at_retirement"] == {
        str(chain["target"]): sha256_bytes(b"new-installed")
    }
    datetime.fromisoformat(entry["retired_at"])
    for key in ("applied_at", "monitoring_started_at", "receipt", "installation_id"):
        assert entry[key] == before[key]


def test_retire_does_not_touch_receipt_backup_or_outcomes(chain) -> None:
    receipt = json.loads(chain["old"].read_text(encoding="utf-8"))
    backup = Path(receipt["operations"][0]["backup_path"])
    outcomes = chain["state"] / "outcomes" / "install-old.jsonl"
    append_outcome(
        chain["state"],
        {
            "schema_version": 1,
            "installation_id": "install-old",
            "recorded_at": "2026-08-07T00:00:00Z",
            "task_id": "task-1",
            "material": True,
            "source": "user",
            "elapsed_seconds": None,
            "agent_count": None,
            "review_count": None,
            "remediation_count": None,
            "validation": "passed",
            "rework": False,
            "escaped_defect": {"present": False, "severity": "none", "category": None},
            "notes": None,
        },
    )
    snapshot = {p: p.read_bytes() for p in (chain["old"], backup, outcomes)}

    assert _retire(chain["old"]) == 0

    assert {p: p.read_bytes() for p in snapshot} == snapshot


def test_retire_is_refused_when_no_other_active_install_covers_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("APU_HOME", str(state))
    target = tmp_path / "solo.md"
    target.write_bytes(b"original")
    receipt = _install(tmp_path, state, "install-solo", target, b"installed")
    target.write_bytes(b"drifted")
    registry_before = load_registry(state)

    assert _retire(receipt) == 1
    assert "no other active installation" in capsys.readouterr().err
    assert load_registry(state) == registry_before

    assert _retire(receipt, "--allow-uncovered") == 0
    entry = load_registry(state)["installations"]["install-solo"]
    assert entry["status"] == "retired"
    assert "superseded_by" not in entry


def test_retire_is_refused_for_a_receipt_that_currently_validates(
    chain, capsys
) -> None:
    registry_before = load_registry(chain["state"])

    assert _retire(chain["new"]) == 1
    assert "currently validates" in capsys.readouterr().err
    assert load_registry(chain["state"]) == registry_before

    # A valid entry can still be retired deliberately when another active
    # installation covers its target.
    assert _retire(chain["new"], "--force") == 0
    entry = load_registry(chain["state"])["installations"]["install-new"]
    assert entry["status"] == "retired"
    assert entry["superseded_by"] == ["install-old"]


def test_retiring_twice_is_a_noop(chain, capsys) -> None:
    assert _retire(chain["old"]) == 0
    capsys.readouterr()
    after_first = load_registry(chain["state"])

    assert _retire(chain["old"], reason="a different reason") == 0
    assert "already retired" in capsys.readouterr().out
    assert load_registry(chain["state"]) == after_first


def test_retire_requires_a_reason(chain, capsys) -> None:
    assert _retire(chain["old"], reason="   ") == 1
    assert "reason" in capsys.readouterr().err
    assert load_registry(chain["state"])["installations"]["install-old"]["status"] == "active"


def test_rollback_refuses_a_retired_entry_unless_allowed(chain) -> None:
    assert _retire(chain["old"]) == 0
    registry_before = load_registry(chain["state"])

    with pytest.raises(RollbackError, match="retired"):
        rollback_receipt(chain["old"])
    assert load_registry(chain["state"]) == registry_before


def test_allowed_rollback_of_a_retired_entry_writes_nothing_to_the_hook_and_keeps_the_acknowledgment(
    chain,
) -> None:
    assert _retire(chain["old"]) == 0
    retired = load_registry(chain["state"])["installations"]["install-old"]

    result = rollback_receipt(chain["old"], allow_retired=True)

    assert result["status"] == "drifted"
    assert chain["target"].read_bytes() == b"new-installed"
    entry = load_registry(chain["state"])["installations"]["install-old"]
    assert entry["status"] == "drifted"
    for key in (
        "applied_at",
        "monitoring_started_at",
        "retired_at",
        "retire_reason",
        "superseded_by",
        "live_sha256_at_retirement",
    ):
        assert entry[key] == retired[key]


def test_rollback_no_longer_erases_applied_at_and_monitoring_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("APU_HOME", str(state))
    target = tmp_path / "policy.md"
    target.write_bytes(b"original")
    receipt = _install(tmp_path, state, "install-1", target, b"installed")
    before = load_registry(state)["installations"]["install-1"]

    assert rollback_receipt(receipt)["status"] == "rolled_back"

    after = load_registry(state)["installations"]["install-1"]
    assert after["status"] == "rolled_back"
    assert after["applied_at"] == before["applied_at"]
    assert after["monitoring_started_at"] == before["monitoring_started_at"]


def test_retirement_freezes_the_monitoring_display(
    chain, capsys
) -> None:
    now = datetime.now(UTC)
    started = (now - timedelta(days=40)).isoformat().replace("+00:00", "Z")
    retired = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    entry = dict(load_registry(chain["state"])["installations"]["install-old"])
    entry.update(
        status="retired",
        monitoring_started_at=started,
        retired_at=retired,
        retire_reason="fixture",
    )
    update_registry(chain["state"], "install-old", entry)

    _, output = _status(capsys)

    assert output["installations"]["install-old"]["monitoring"]["elapsed_days"] == 30
    assert output["installations"]["install-new"]["monitoring"]["elapsed_days"] == 0

from dataclasses import replace
from pathlib import Path

from apu.apply import apply_plan
from apu.audit import build_inventory
from apu.planning import (
    approve_all_recommended,
    propose_inventory,
    update_plan_status,
)
from apu.rollback import rollback_receipt
from apu.validate import validate_receipt_path


def test_audit_to_rollback_restores_original_tree(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repository = tmp_path / "repo"
    repository.mkdir()
    policy = repository / "AGENTS.md"
    original = (
        "Run focused tests for changed behavior.\n"
        "Run focused tests for changed behavior.\n"
    ).encode()
    policy.write_bytes(original)

    inventory = build_inventory(
        [repository],
        home=home,
        working_directories=[repository],
        generated_at="2026-08-06T00:00:00Z",
    )
    draft = propose_inventory(
        inventory,
        created_at="2026-08-06T00:01:00Z",
        candidate_dir=tmp_path / "candidates",
    )
    approved = update_plan_status(
        replace(
            draft,
            operations=approve_all_recommended(
                draft.operations,
                recorded_at="2026-08-06T00:02:00Z",
            ),
        )
    )

    receipt = apply_plan(
        approved,
        state_home=tmp_path / "state",
        installation_id="e2e",
    )

    assert policy.read_text(encoding="utf-8") == (
        "Run focused tests for changed behavior.\n"
    )
    assert validate_receipt_path(receipt).status == "passed"
    assert rollback_receipt(receipt)["status"] == "rolled_back"
    assert policy.read_bytes() == original

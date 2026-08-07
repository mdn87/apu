from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_dispatch import _campaign

from apu.dispatch import IsolationProbeResult, dispatch_work_order
from apu.dispatch_apply import apply_dispatched_plan
from apu.models import Approval, Plan, sha256_bytes
from apu.planning import update_plan_status
from apu.receipts import load_receipt


def test_reviewed_dispatch_plan_applies_with_campaign_receipt(
    tmp_path: Path,
) -> None:
    state, bundle, target, _ = _campaign(tmp_path)
    order = bundle["work_orders"][0]
    result = dispatch_work_order(
        state,
        bundle["campaign_id"],
        order["work_order_id"],
        runner=lambda _request: {"files": {str(target.resolve()): "reviewed\n"}},
        isolation_probe=lambda _request: IsolationProbeResult(
            context_id="sandbox",
            mechanism="test-denial",
            attempted=True,
            write_denied=True,
        ),
        created_at="2026-08-07T01:02:00Z",
    )
    draft = Plan.from_dict(
        __import__("json").loads(result.plan_path.read_text(encoding="utf-8"))
    )
    reviewed = replace(
        draft,
        operations=tuple(
            replace(
                operation,
                approval=Approval(
                    status="approved",
                    recorded_at="2026-08-07T01:03:00Z",
                    method="interactive-review",
                ),
            )
            for operation in draft.operations
        ),
    )
    reviewed = update_plan_status(reviewed)

    receipt_path = apply_dispatched_plan(
        state,
        reviewed,
        installation_id="dispatch-install",
    )

    assert target.read_text(encoding="utf-8") == "reviewed\n"
    receipt = load_receipt(receipt_path)
    assert receipt["campaign_id"] == bundle["campaign_id"]
    assert receipt["snapshot_id"] == result.snapshot_id


def test_dispatch_apply_rejects_candidate_edits_after_secret_scan(
    tmp_path: Path,
) -> None:
    state, bundle, target, _ = _campaign(tmp_path)
    order = bundle["work_orders"][0]
    result = dispatch_work_order(
        state,
        bundle["campaign_id"],
        order["work_order_id"],
        runner=lambda _request: {"files": {str(target.resolve()): "reviewed\n"}},
        isolation_probe=lambda _request: IsolationProbeResult(
            context_id="sandbox",
            mechanism="test-denial",
            attempted=True,
            write_denied=True,
        ),
    )
    draft = Plan.from_dict(
        __import__("json").loads(result.plan_path.read_text(encoding="utf-8"))
    )
    changed = replace(
        draft,
        operations=(
            replace(
                draft.operations[0],
                proposed_sha256=sha256_bytes(b"different"),
            ),
        ),
    )
    with pytest.raises(ValueError, match="outside approval"):
        apply_dispatched_plan(
            state,
            changed,
            installation_id="dispatch-install",
        )

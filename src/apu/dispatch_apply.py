from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .apply import apply_plan
from .campaigns import (
    CampaignLock,
    campaign_directory,
    load_campaign_index,
    load_campaign_manifest,
    reconcile_campaign_locked,
    register_leaf_artifact,
    scan_leaf_artifacts,
)
from .models import Approval, Plan, sha256_json
from .receipts import load_receipt
from .snapshots import load_snapshot


def dispatch_plan_binding(
    state_home: Path,
    reviewed_plan: Plan,
) -> dict[str, str] | None:
    """Verify that a reviewed plan differs from an accepted draft only by decisions."""

    binding = reviewed_plan.validation.get("campaign_binding")
    if binding is None:
        return None
    required = {
        "campaign_id",
        "snapshot_id",
        "work_order_id",
        "work_order_sha256",
    }
    if not isinstance(binding, Mapping) or set(binding) != required:
        raise ValueError("dispatch plan campaign binding is invalid")
    normalized = {
        key: value for key, value in binding.items() if isinstance(value, str) and value
    }
    if set(normalized) != required:
        raise ValueError("dispatch plan campaign binding fields must be strings")

    root = campaign_directory(state_home, normalized["campaign_id"]).resolve()
    load_campaign_manifest(state_home, normalized["campaign_id"])
    index = load_campaign_index(state_home, normalized["campaign_id"])
    if index["snapshot_id"] != normalized["snapshot_id"]:
        raise ValueError("dispatch plan snapshot binding is stale")
    snapshot = load_snapshot(state_home, normalized["snapshot_id"])
    if snapshot.get("campaign_id") != normalized["campaign_id"]:
        raise ValueError("dispatch plan snapshot identifies another campaign")

    results = [
        leaf
        for leaf in scan_leaf_artifacts(state_home, normalized["campaign_id"])
        if leaf["artifact_type"] == "work-order-result"
        and leaf.get("status") == "accepted"
        and leaf.get("snapshot_id") == normalized["snapshot_id"]
        and leaf.get("work_order_id") == normalized["work_order_id"]
        and leaf.get("work_order_sha256") == normalized["work_order_sha256"]
    ]
    if len(results) != 1 or not isinstance(results[0].get("plan"), Mapping):
        raise ValueError("dispatch plan has no unique accepted work-order result")
    plan_reference = results[0]["plan"]
    referenced_path = (root / plan_reference["path"]).resolve()
    if not referenced_path.is_relative_to(root / "plans"):
        raise ValueError("accepted dispatch plan path escapes campaign state")
    if sha256_json(_decision_neutral(reviewed_plan)) != plan_reference["sha256"]:
        raise ValueError("reviewed dispatch plan changed outside approval decisions")
    return normalized


def apply_dispatched_plan(
    state_home: Path,
    plan: Plan,
    *,
    installation_id: str,
) -> Path:
    binding = dispatch_plan_binding(state_home, plan)
    if binding is None:
        raise ValueError("plan is not bound to an accepted dispatch result")
    campaign_id = binding["campaign_id"]
    with CampaignLock(state_home, campaign_id, purpose="dispatch-apply"):
        binding = dispatch_plan_binding(state_home, plan)
        assert binding is not None
        index = load_campaign_index(state_home, campaign_id)
        receipt_path = apply_plan(
            plan,
            state_home=state_home,
            installation_id=installation_id,
            confirmed=True,
            campaign_id=campaign_id,
            snapshot_id=binding["snapshot_id"],
        )
        receipt = load_receipt(receipt_path)
        register_leaf_artifact(
            state_home,
            campaign_id,
            {
                "schema_version": 1,
                "campaign_id": campaign_id,
                "artifact_type": "receipt",
                "artifact_id": installation_id,
                "snapshot_id": binding["snapshot_id"],
                "idempotency_key": {
                    "operation_id": ("dispatch-apply-" + binding["work_order_id"]),
                    "attempt": 1,
                },
                "receipt_path": str(receipt_path),
                "receipt_sha256": sha256_json(receipt),
                "work_order_id": binding["work_order_id"],
            },
        )
        reconcile_campaign_locked(
            state_home,
            campaign_id,
            expected_revision=index["revision"],
        )
    return receipt_path


def _decision_neutral(plan: Plan) -> dict[str, Any]:
    neutral_operations = tuple(
        replace(operation, approval=Approval()) for operation in plan.operations
    )
    neutral = replace(plan, status="draft", operations=neutral_operations)
    return neutral.to_dict()

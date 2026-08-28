from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apu.campaigns import (
    CampaignExistsError,
    CampaignLock,
    CampaignLockedError,
    StaleCampaignRevisionError,
    build_campaign_manifest,
    campaign_directory,
    create_campaign,
    leaf_artifact_path,
    load_campaign_index,
    load_campaign_manifest,
    read_campaign_lock_status,
    rebuild_campaign_index,
    reconcile_campaign,
    register_leaf_artifact,
    scan_leaf_artifacts,
)
from apu.models import canonical_json


def _manifest(campaign_id: str = "campaign-1") -> dict[str, object]:
    return build_campaign_manifest(
        campaign_id=campaign_id,
        inventory_hash="a" * 64,
        profile_hash="b" * 64,
        baseline_version="2026-08-06",
        model_generation="openai-gpt-5-generation",
        plan_binding={"plan_id": "plan-1", "sha256": "c" * 64},
        work_order_bindings=[
            {"work_order_id": "wo-1", "sha256": "d" * 64},
        ],
    )


def _receipt(
    *,
    campaign_id: str = "campaign-1",
    artifact_id: str = "receipt-1",
    snapshot_id: str = "snapshot-1",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "artifact_type": "receipt",
        "artifact_id": artifact_id,
        "snapshot_id": snapshot_id,
        "idempotency_key": {
            "operation_id": "apply-policy",
            "attempt": 1,
        },
        "result": {"status": "applied"},
    }


def test_create_campaign_freezes_manifest_and_initializes_index(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    manifest = _manifest()

    index = create_campaign(state_home, manifest)

    assert load_campaign_manifest(state_home, "campaign-1") == manifest
    assert index == {
        "schema_version": 1,
        "campaign_id": "campaign-1",
        "revision": 0,
        "snapshot_id": None,
        "artifacts": [],
    }
    assert load_campaign_index(state_home, "campaign-1") == index
    root = campaign_directory(state_home, "campaign-1")
    assert (root / "revisions" / "00000000000000000000.json").exists()
    assert not list(root.glob(".manifest.json.*"))

    assert create_campaign(state_home, manifest) == index

    if os.name == "posix":
        assert root.stat().st_mode & 0o777 == 0o700
        assert (root / "manifest.json").stat().st_mode & 0o777 == 0o600
        assert (root / "index.json").stat().st_mode & 0o777 == 0o600


def test_campaign_manifest_collision_never_rewrites_original(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    create_campaign(tmp_path, manifest)
    changed = dict(manifest)
    changed["baseline_version"] = "different"

    with pytest.raises(CampaignExistsError, match="different immutable manifest"):
        create_campaign(tmp_path, changed)

    assert load_campaign_manifest(tmp_path, "campaign-1") == manifest


def test_campaign_manifest_requires_frozen_bindings() -> None:
    manifest = _manifest()
    del manifest["profile_hash"]

    with pytest.raises(ValueError, match="manifest fields"):
        create_campaign(Path("unused"), manifest)


def test_register_leaf_is_atomic_idempotent_and_self_describing(
    tmp_path: Path,
) -> None:
    create_campaign(tmp_path, _manifest())
    receipt = _receipt()

    path = register_leaf_artifact(tmp_path, "campaign-1", receipt)

    assert path == leaf_artifact_path(
        tmp_path,
        "campaign-1",
        "receipt",
        "receipt-1",
    )
    assert json.loads(path.read_text(encoding="utf-8")) == receipt
    assert path.read_text(encoding="utf-8") == canonical_json(receipt)
    assert register_leaf_artifact(tmp_path, "campaign-1", receipt) == path
    assert not list(path.parent.glob(f".{path.name}.*"))
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_leaf_identity_collision_never_rewrites_original(tmp_path: Path) -> None:
    create_campaign(tmp_path, _manifest())
    receipt = _receipt()
    register_leaf_artifact(tmp_path, "campaign-1", receipt)
    changed = dict(receipt)
    changed["result"] = {"status": "failed"}

    with pytest.raises(CampaignExistsError, match="different content"):
        register_leaf_artifact(tmp_path, "campaign-1", changed)

    assert scan_leaf_artifacts(tmp_path, "campaign-1") == [receipt]


def test_execution_leaves_require_snapshot_and_idempotency_identity(
    tmp_path: Path,
) -> None:
    create_campaign(tmp_path, _manifest())
    receipt = _receipt()
    del receipt["snapshot_id"]
    with pytest.raises(ValueError, match="requires snapshot_id"):
        register_leaf_artifact(tmp_path, "campaign-1", receipt)

    receipt = _receipt()
    del receipt["idempotency_key"]
    with pytest.raises(ValueError, match="requires idempotency_key"):
        register_leaf_artifact(tmp_path, "campaign-1", receipt)


def test_reconcile_attaches_orphan_leaf_and_increments_revision(
    tmp_path: Path,
) -> None:
    create_campaign(tmp_path, _manifest())
    receipt = _receipt()
    register_leaf_artifact(tmp_path, "campaign-1", receipt)

    before = load_campaign_index(tmp_path, "campaign-1")
    assert before["artifacts"] == []

    after = reconcile_campaign(
        tmp_path,
        "campaign-1",
        expected_revision=0,
    )

    assert after == {
        "schema_version": 1,
        "campaign_id": "campaign-1",
        "revision": 1,
        "snapshot_id": "snapshot-1",
        "artifacts": [
            {
                "artifact_type": "receipt",
                "artifact_id": "receipt-1",
                "snapshot_id": "snapshot-1",
                "idempotency_key": {
                    "operation_id": "apply-policy",
                    "attempt": 1,
                },
            }
        ],
    }
    assert load_campaign_index(tmp_path, "campaign-1") == after
    assert (
        reconcile_campaign(
            tmp_path,
            "campaign-1",
            expected_revision=1,
        )
        == after
    )


def test_stale_revision_aborts_without_overwriting_index(tmp_path: Path) -> None:
    create_campaign(tmp_path, _manifest())
    register_leaf_artifact(tmp_path, "campaign-1", _receipt())
    current = reconcile_campaign(
        tmp_path,
        "campaign-1",
        expected_revision=0,
    )

    with pytest.raises(StaleCampaignRevisionError, match="writer expected 0"):
        reconcile_campaign(
            tmp_path,
            "campaign-1",
            expected_revision=0,
        )

    assert load_campaign_index(tmp_path, "campaign-1") == current


def test_manifest_and_leaves_rebuild_deleted_index_state(tmp_path: Path) -> None:
    create_campaign(tmp_path, _manifest())
    register_leaf_artifact(tmp_path, "campaign-1", _receipt())
    root = campaign_directory(tmp_path, "campaign-1")
    (root / "index.json").unlink()
    for revision in (root / "revisions").glob("*.json"):
        revision.unlink()

    rebuilt = rebuild_campaign_index(tmp_path, "campaign-1")

    assert rebuilt["revision"] == 0
    assert rebuilt["snapshot_id"] == "snapshot-1"
    assert [item["artifact_id"] for item in rebuilt["artifacts"]] == ["receipt-1"]
    assert load_campaign_index(tmp_path, "campaign-1") == rebuilt


def test_scan_fails_closed_for_misfiled_or_foreign_leaf(tmp_path: Path) -> None:
    create_campaign(tmp_path, _manifest())
    path = leaf_artifact_path(
        tmp_path,
        "campaign-1",
        "receipt",
        "receipt-1",
    )
    path.parent.mkdir(parents=True)
    foreign = _receipt(campaign_id="campaign-2")
    path.write_text(canonical_json(foreign), encoding="utf-8")

    with pytest.raises(ValueError, match="mismatched campaign_id"):
        scan_leaf_artifacts(tmp_path, "campaign-1")


def test_campaign_lock_fails_fast_with_holder_identity(tmp_path: Path) -> None:
    create_campaign(tmp_path, _manifest())

    with (
        CampaignLock(tmp_path, "campaign-1", purpose="apply") as held,
        pytest.raises(CampaignLockedError) as captured,
    ):
        assert held.metadata is not None
        with CampaignLock(tmp_path, "campaign-1", purpose="dispatch"):
            pass

    assert captured.value.holder is not None
    assert captured.value.holder["pid"] == os.getpid()
    assert captured.value.holder["purpose"] == "apply"
    status = read_campaign_lock_status(tmp_path, "campaign-1")
    assert status is not None
    assert status["released_at"] is not None


def test_unheld_stale_metadata_is_overwritten_and_reported(
    tmp_path: Path,
) -> None:
    create_campaign(tmp_path, _manifest())
    lock_path = campaign_directory(tmp_path, "campaign-1") / "lock.json"
    stale = {
        "schema_version": 1,
        "campaign_id": "campaign-1",
        "pid": 999999,
        "purpose": "crashed-dispatch",
        "acquired_at": "2026-01-01T00:00:00+00:00",
        "released_at": None,
    }
    lock_path.write_text(canonical_json(stale), encoding="utf-8")

    with CampaignLock(tmp_path, "campaign-1", purpose="status-reconcile") as lock:
        assert lock.recovered_stale_holder == stale

    status = read_campaign_lock_status(tmp_path, "campaign-1")
    assert status is not None
    assert status["purpose"] == "status-reconcile"
    assert status["recovered_stale_holder"] == stale


def test_artifacts_from_multiple_snapshots_fail_reconciliation(
    tmp_path: Path,
) -> None:
    create_campaign(tmp_path, _manifest())
    register_leaf_artifact(tmp_path, "campaign-1", _receipt())
    register_leaf_artifact(
        tmp_path,
        "campaign-1",
        _receipt(artifact_id="receipt-2", snapshot_id="snapshot-2"),
    )

    with pytest.raises(ValueError, match="multiple snapshot_ids"):
        reconcile_campaign(
            tmp_path,
            "campaign-1",
            expected_revision=0,
        )

    assert load_campaign_index(tmp_path, "campaign-1")["revision"] == 0

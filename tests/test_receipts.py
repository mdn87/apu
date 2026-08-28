from __future__ import annotations

import os
from pathlib import Path

import pytest

from apu.receipts import backup_dir, load_receipt, receipt_path, write_receipt


def _receipt(state_home: Path) -> dict:
    return {
        "schema_version": 1,
        "installation_id": "install-123",
        "created_at": "2026-08-06T12:00:00Z",
        "operations": [
            {
                "id": "op-1",
                "original_sha256": "a" * 64,
                "installed_sha256": "b" * 64,
                "backup_path": str(
                    state_home / "installations" / "install-123" / "backups" / "op-1"
                ),
            }
        ],
        "rollback_status": "available",
    }


def _campaign_receipt(state_home: Path) -> dict:
    receipt = _receipt(state_home)
    receipt.update(
        {
            "campaign_id": "campaign-1",
            "snapshot_id": "snapshot-1",
            "idempotency_keys": {
                "op-1": {
                    "operation_id": "op-1",
                    "attempt": 1,
                }
            },
        }
    )
    return receipt


def test_receipt_paths_are_side_effect_free_until_creation(tmp_path: Path) -> None:
    state_home = tmp_path / "state"

    assert receipt_path(state_home, "install-123") == (
        state_home / "installations" / "install-123" / "receipt.json"
    )
    assert backup_dir(state_home, "install-123") == (
        state_home / "installations" / "install-123" / "backups"
    )
    assert not state_home.exists()

    created = backup_dir(state_home, "install-123", create=True)
    assert created.is_dir()
    if os.name == "posix":
        assert created.stat().st_mode & 0o777 == 0o700
        assert created.parent.stat().st_mode & 0o777 == 0o700
        assert created.parent.parent.stat().st_mode & 0o777 == 0o700


def test_write_and_load_receipt_with_hashes_and_backup_references(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    receipt = _receipt(state_home)

    written = write_receipt(state_home, receipt)

    assert written == receipt_path(state_home, "install-123")
    assert load_receipt(written) == receipt
    assert written.read_text(encoding="utf-8").startswith(
        '{"created_at":"2026-08-06T12:00:00Z","installation_id":"install-123"'
    )
    if os.name == "posix":
        assert written.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"installation_id": "../escape"}, "installation_id"),
        ({"operations": "op-1"}, "operations"),
        ({"rollback_status": ""}, "rollback_status"),
        (
            {
                "operations": [
                    {
                        "id": "op-1",
                        "original_sha256": "not-a-hash",
                        "installed_sha256": "b" * 64,
                        "backup_path": None,
                    }
                ]
            },
            "original_sha256",
        ),
    ],
)
def test_write_receipt_rejects_invalid_contract(
    tmp_path: Path, change: dict, message: str
) -> None:
    receipt = _receipt(tmp_path)
    receipt.update(change)

    with pytest.raises(ValueError, match=message):
        write_receipt(tmp_path, receipt)

    assert not (tmp_path / "installations").exists()


def test_write_receipt_rejects_embedded_source_content_and_external_backups(
    tmp_path: Path,
) -> None:
    source_receipt = _receipt(tmp_path)
    source_receipt["operations"][0]["original_content"] = "sensitive"
    with pytest.raises(ValueError, match="content"):
        write_receipt(tmp_path, source_receipt)

    external_receipt = _receipt(tmp_path)
    external_receipt["operations"][0]["backup_path"] = str(
        tmp_path / "outside" / "op-1"
    )
    with pytest.raises(ValueError, match="backup_path"):
        write_receipt(tmp_path, external_receipt)

    assert not (tmp_path / "installations").exists()


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("op-1:1", "must be an object"),
        ({"operation_id": "op-1"}, "requires exactly"),
        (
            {"operation_id": "op-1", "attempt": 1, "extra": True},
            "requires exactly",
        ),
        (
            {"operation_id": "different", "attempt": 1},
            "operation_id does not match",
        ),
        ({"operation_id": "op-1", "attempt": 0}, "positive integer"),
        ({"operation_id": "op-1", "attempt": True}, "positive integer"),
        ({"operation_id": "op-1", "attempt": "1"}, "positive integer"),
    ],
)
def test_campaign_receipt_requires_structured_idempotency_keys(
    tmp_path: Path,
    key: object,
    message: str,
) -> None:
    receipt = _campaign_receipt(tmp_path)
    receipt["idempotency_keys"]["op-1"] = key

    with pytest.raises(ValueError, match=message):
        write_receipt(tmp_path, receipt)


def test_campaign_receipt_rejects_extra_idempotency_keys(tmp_path: Path) -> None:
    receipt = _campaign_receipt(tmp_path)
    receipt["idempotency_keys"]["not-an-operation"] = {
        "operation_id": "not-an-operation",
        "attempt": 1,
    }

    with pytest.raises(ValueError, match="must match receipt operations"):
        write_receipt(tmp_path, receipt)

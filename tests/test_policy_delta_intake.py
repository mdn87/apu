from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from apu.cli import main
from apu.models import canonical_json, sha256_json
from apu.policy_delta import (
    PolicyDeltaIntakeError,
    ingest_policy_delta,
    validate_policy_delta_proposal,
)
from jsonschema import Draft202012Validator

NOW = "2026-09-02T15:00:00Z"


def _proposal() -> dict:
    body = {
        "schema_version": 1,
        "record_type": "aeta_apu_policy_delta_proposal",
        "thread_ref": {
            "thread_id": "plir:thread:01M1H5A6BETTS02HFV1SXQ5Y67",
            "checkpoint_ref": "thread-checkpoint:01M1H7PW2J75HSCNK1ZDPJS269",
        },
        "source": {
            "repository_ref": "https://github.com/mdn87/lugos.git",
            "revision": "d0fb9a81b351a1431fc9a36ae1912154bd72f976",
            "path": "docs/lir/lir-protocol-v1.md",
            "content_sha256": "sha256:" + "a" * 64,
            "snapshot_ref": "aeta:snapshot:" + "b" * 64,
            "freshness": {"kind": "immutable_git", "status": "fresh"},
        },
        "aeta_run_ref": "aeta:run:" + "c" * 64,
        "target": {
            "owner": "apu",
            "surface": "cross-agent-thread-resumption",
            "expected_base_version": None,
        },
        "evidence_refs": [
            "aeta:snapshot:" + "b" * 64,
            "github:mdn87/lugos@d0fb9a81b351a1431fc9a36ae1912154bd72f976",
        ],
        "rule_operations": [
            {
                "rule_id": "lir.exact-thread-alias",
                "action": "add",
                "summary": (
                    "Interpret pick up the thread as exact OGMI thread or alias "
                    "resolution without semantic fallback."
                ),
                "evidence_refs": ["section:3"],
            }
        ],
        "requires_review": True,
        "promotion_authorized": False,
    }
    return {**body, "proposal_id": "sha256:" + sha256_json(body)}


def test_intake_is_immutable_idempotent_and_does_not_promote(tmp_path: Path) -> None:
    baseline = tmp_path / "guidance" / "baselines" / "current.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text('{"sentinel":"unchanged"}\n', encoding="utf-8")
    before = baseline.read_bytes()

    first = ingest_policy_delta(tmp_path, _proposal(), received_at=NOW)
    second = ingest_policy_delta(
        tmp_path,
        _proposal(),
        received_at="2026-09-03T15:00:00Z",
    )

    assert first == second
    assert first["status"] == "pending_review"
    assert first["requires_review"] is True
    assert first["promotion_authorized"] is False
    assert baseline.read_bytes() == before
    assert len(list((tmp_path / "policy-delta-intake" / "candidates").glob("*.json"))) == 1
    assert len(list((tmp_path / "policy-delta-intake" / "receipts").glob("*.json"))) == 1


def test_intake_rejects_hash_drift_authorization_and_secrets() -> None:
    drifted = _proposal()
    drifted["target"]["surface"] = "different"
    with pytest.raises(PolicyDeltaIntakeError, match="proposal_id"):
        validate_policy_delta_proposal(drifted)

    authorized = deepcopy(_proposal())
    authorized["promotion_authorized"] = True
    body = {key: value for key, value in authorized.items() if key != "proposal_id"}
    authorized["proposal_id"] = "sha256:" + sha256_json(body)
    with pytest.raises(PolicyDeltaIntakeError, match="promotion_authorized"):
        validate_policy_delta_proposal(authorized)

    secret = deepcopy(_proposal())
    secret["rule_operations"][0]["summary"] = "token sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    body = {key: value for key, value in secret.items() if key != "proposal_id"}
    secret["proposal_id"] = "sha256:" + sha256_json(body)
    with pytest.raises(PolicyDeltaIntakeError, match="credential-shaped"):
        validate_policy_delta_proposal(secret)


def test_intake_rejects_a_tampered_replay_receipt(tmp_path: Path) -> None:
    proposal = _proposal()
    ingest_policy_delta(tmp_path, proposal, received_at=NOW)
    key = proposal["proposal_id"].removeprefix("sha256:")
    receipt_path = tmp_path / "policy-delta-intake" / "receipts" / f"{key}.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["promotion_authorized"] = True
    receipt_path.write_text(canonical_json(receipt), encoding="utf-8")

    with pytest.raises(PolicyDeltaIntakeError, match="intake receipt"):
        ingest_policy_delta(tmp_path, proposal, received_at=NOW)


def test_intake_cli_and_committed_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(canonical_json(_proposal()) + "\n", encoding="utf-8")
    state_home = tmp_path / "state"
    monkeypatch.setenv("APU_HOME", str(state_home.resolve()))
    monkeypatch.setattr("apu.cli._timestamp", lambda: NOW)

    assert main(["intake", "policy-delta", str(proposal_path)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "pending_review"

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "apu-policy-delta-intake-receipt.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["record_type"]["const"] == (
        "apu_policy_delta_intake_receipt"
    )
    assert set(schema["required"]) == set(receipt)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(
        receipt
    )

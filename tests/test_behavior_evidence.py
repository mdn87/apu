from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import jsonschema
import pytest

from apu.behavior_evidence import (
    BehaviorEvidenceError,
    build_behavior_registry_candidate_patch,
    import_behavior_evaluation_evidence,
    validate_behavior_evaluation_evidence,
    validate_behavior_registry_candidate_patch,
)
from apu.models import sha256_json

ROOT = Path(__file__).parents[1]
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
COMMIT = "d" * 40


def _receipt(*, tier: int = 2) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "kind": "lugos.autowork.behavior-delegation-receipt",
        "run_id": "run-1",
        "assignment_id": "assignment-1",
        "loadout_advice_sha256": HASH_A,
        "seat_policy_advice_sha256": HASH_B,
        "behavior_advice_sha256": HASH_C,
        "seat": "implementer",
        "route": "claude-cli",
        "provider": "anthropic",
        "model_requested": "opus-5",
        "model_resolved": "opus-5-2026-08-01",
        "thinking_level_requested": "high",
        "adaptation_tier": tier,
        "registry_schema_version": 1,
        "registry_content_revision": {
            "orca_commit": COMMIT,
            "behavior_tree_sha256": HASH_A,
        },
        "compiled_envelope_sha256": HASH_B,
        "applied_artifacts": [
            {
                "id": "instruction.plain-language",
                "kind": "instruction",
                "content_sha256": HASH_C,
            },
            {"id": "skill.bounded-planner", "kind": "skill", "content_sha256": HASH_B},
        ],
        "narrowings": [
            {
                "field": "tools",
                "advised_sha256": HASH_A,
                "applied_sha256": HASH_B,
                "reason": "seat-policy",
            }
        ],
        "rejected_techniques": [
            {"technique_id": "hook.unsupported", "reason": "route-capability"}
        ],
        "telemetry": {
            "duration_ms": 1200,
            "input_tokens": 100,
            "output_tokens": 50,
            "exit_status": "success",
        },
    }
    value["receipt_sha256"] = sha256_json(value)
    return value


def _evaluation() -> dict[str, object]:
    return {
        "suite_id": "complexity-baseline-v1",
        "task_id": "task-7",
        "comparison_group_id": "group-2",
        "status": "passed",
        "checks": [
            {"id": "plain-language", "status": "passed"},
            {"id": "bounded-scope", "status": "passed"},
        ],
        "metrics": [
            {"id": "new-components", "value": 1, "unit": "count"},
            {"id": "elapsed", "value": 1.2, "unit": "seconds"},
        ],
    }


def _evidence() -> dict[str, object]:
    return import_behavior_evaluation_evidence(
        _receipt(), _evaluation(), created_at="2026-08-23T07:00:00Z"
    ).to_dict()


def _operation() -> dict[str, object]:
    value = {"introduced_at_tier": 2, "techniques": ["instruction.plain-language"]}
    return {
        "op": "replace",
        "path": "/behavior/profiles/models/anthropic-opus-5/rules/0",
        "prior_value_sha256": HASH_A,
        "value": value,
        "value_sha256": sha256_json(value),
    }


def test_checked_in_schemas_are_valid_and_accept_built_artifacts() -> None:
    evidence_schema = json.loads(
        (ROOT / "schemas/behavior-evaluation-evidence.schema.json").read_text(
            encoding="utf-8"
        )
    )
    patch_schema = json.loads(
        (ROOT / "schemas/behavior-registry-candidate-patch.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validators.validator_for(evidence_schema).check_schema(evidence_schema)
    jsonschema.validators.validator_for(patch_schema).check_schema(patch_schema)

    evidence = _evidence()
    patch = build_behavior_registry_candidate_patch(
        base_registry_revision=evidence["registry_revision"],
        supporting_evidence=[evidence],
        operations=[_operation()],
        created_at="2026-08-23T08:00:00Z",
    ).to_dict()
    jsonschema.Draft202012Validator(evidence_schema).validate(evidence)
    jsonschema.Draft202012Validator(patch_schema).validate(patch)


def test_import_is_canonical_and_content_minimized() -> None:
    first = _evidence()
    receipt = _receipt()
    evaluation = _evaluation()
    second = import_behavior_evaluation_evidence(
        dict(reversed(list(receipt.items()))),
        dict(reversed(list(evaluation.items()))),
        created_at="2026-08-23T07:00:00Z",
    ).to_dict()

    assert first == second
    assert first["evidence_id"] == sha256_json(
        {key: value for key, value in first.items() if key != "evidence_id"}
    )
    encoded = json.dumps(first)
    assert "seat-policy" not in encoded
    assert "route-capability" not in encoded
    assert "do the work" not in encoded
    assert first["privacy"] == {
        "redacted": True,
        "contains_raw_prompt": False,
        "contains_raw_response": False,
        "contains_reasoning": False,
        "contains_environment": False,
        "contains_credentials": False,
    }


@pytest.mark.parametrize("tier", [3, 4])
def test_import_rejects_reserved_tiers(tier: int) -> None:
    with pytest.raises(BehaviorEvidenceError, match="tiers 0 through 2"):
        import_behavior_evaluation_evidence(_receipt(tier=tier), _evaluation())


def test_import_rejects_invalid_receipt_hash_and_unknown_fields() -> None:
    receipt = _receipt()
    receipt["receipt_sha256"] = HASH_A
    with pytest.raises(BehaviorEvidenceError, match="receipt_sha256"):
        import_behavior_evaluation_evidence(receipt, _evaluation())

    receipt = _receipt()
    receipt["unexpected"] = "value"
    with pytest.raises(BehaviorEvidenceError, match="fields must be exact"):
        import_behavior_evaluation_evidence(receipt, _evaluation())


def test_import_preserves_nullable_safe_telemetry() -> None:
    receipt = _receipt()
    receipt["telemetry"] = {
        "duration_ms": None,
        "input_tokens": None,
        "output_tokens": None,
        "exit_status": None,
    }
    receipt["receipt_sha256"] = sha256_json(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    evidence = import_behavior_evaluation_evidence(receipt, _evaluation()).to_dict()
    assert evidence["telemetry"] == receipt["telemetry"]


def test_import_rejects_private_fields_and_secret_shapes() -> None:
    evaluation = _evaluation()
    evaluation["raw_prompt"] = "do the work"
    with pytest.raises(BehaviorEvidenceError, match="fields must be exact"):
        import_behavior_evaluation_evidence(_receipt(), evaluation)

    receipt = _receipt()
    receipt["provider"] = "sk-proj-abcdefghijklmnop"
    receipt["receipt_sha256"] = sha256_json(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    with pytest.raises(BehaviorEvidenceError, match="credential-shaped"):
        import_behavior_evaluation_evidence(receipt, _evaluation())


def test_evidence_validator_rejects_unknown_fields_and_hash_drift() -> None:
    evidence = _evidence()
    evidence["unknown"] = True
    with pytest.raises(BehaviorEvidenceError, match="fields must be exact"):
        validate_behavior_evaluation_evidence(evidence)

    evidence = _evidence()
    evidence["narrowing_count"] = 99
    with pytest.raises(BehaviorEvidenceError, match="evidence_id"):
        validate_behavior_evaluation_evidence(evidence)


def test_candidate_patch_is_review_only_and_canonical() -> None:
    evidence = _evidence()
    candidate = build_behavior_registry_candidate_patch(
        base_registry_revision=evidence["registry_revision"],
        supporting_evidence=[evidence],
        operations=[_operation()],
        created_at="2026-08-23T08:00:00Z",
    ).to_dict()

    assert candidate["requires_review"] is True
    assert candidate["apply_authorized"] is False
    assert candidate["proposal_id"] == sha256_json(
        {key: value for key, value in candidate.items() if key != "proposal_id"}
    )
    evidence_ids = candidate["evidence"]["supporting_evidence_ids"]
    expected_bundle = {
        "schema_version": 1,
        "kind": "lugos.apu.behavior-evaluation-evidence-bundle",
        "evidence_ids": evidence_ids,
    }
    assert candidate["evidence"]["evidence_bundle_sha256"] == sha256_json(
        expected_bundle
    )


def test_candidate_requires_exact_base_revision() -> None:
    evidence = _evidence()
    short_revision = deepcopy(evidence["registry_revision"])
    short_revision["orca_commit"] = "d" * 12
    with pytest.raises(BehaviorEvidenceError, match="full lowercase Git"):
        build_behavior_registry_candidate_patch(
            base_registry_revision=short_revision,
            supporting_evidence=[evidence],
            operations=[_operation()],
        )

    other_revision = deepcopy(evidence["registry_revision"])
    other_revision["behavior_tree_sha256"] = HASH_C
    with pytest.raises(BehaviorEvidenceError, match="exactly match"):
        build_behavior_registry_candidate_patch(
            base_registry_revision=other_revision,
            supporting_evidence=[evidence],
            operations=[_operation()],
        )


def test_candidate_rejects_hash_drift_paths_overlap_and_secrets() -> None:
    evidence = _evidence()
    operation = _operation()
    operation["value_sha256"] = HASH_B
    with pytest.raises(BehaviorEvidenceError, match="does not match value"):
        build_behavior_registry_candidate_patch(
            base_registry_revision=evidence["registry_revision"],
            supporting_evidence=[evidence],
            operations=[operation],
        )

    child = {
        "op": "remove",
        "path": operation["path"] + "/selector",
        "prior_value_sha256": HASH_A,
    }
    with pytest.raises(BehaviorEvidenceError, match="overlap"):
        build_behavior_registry_candidate_patch(
            base_registry_revision=evidence["registry_revision"],
            supporting_evidence=[evidence],
            operations=[_operation(), child],
        )

    secret_value = {"api_key": "sk-proj-abcdefghijklmnop"}
    secret_operation = {
        "op": "add",
        "path": "/behavior/profiles/providers/openai/rules/0",
        "value": secret_value,
        "value_sha256": sha256_json(secret_value),
    }
    with pytest.raises(BehaviorEvidenceError, match="forbidden private field"):
        build_behavior_registry_candidate_patch(
            base_registry_revision=evidence["registry_revision"],
            supporting_evidence=[evidence],
            operations=[secret_operation],
        )


def test_candidate_validator_rejects_authorization_and_noncanonical_order() -> None:
    evidence = _evidence()
    second = {"op": "remove", "path": "/behavior/tiers/2", "prior_value_sha256": HASH_B}
    candidate = build_behavior_registry_candidate_patch(
        base_registry_revision=evidence["registry_revision"],
        supporting_evidence=[evidence],
        operations=[second, _operation()],
        created_at="2026-08-23T08:00:00Z",
    ).to_dict()
    assert [item["path"] for item in candidate["operations"]] == sorted(
        item["path"] for item in candidate["operations"]
    )

    authorized = deepcopy(candidate)
    authorized["apply_authorized"] = True
    with pytest.raises(BehaviorEvidenceError, match="review-only"):
        validate_behavior_registry_candidate_patch(authorized)

    reversed_candidate = deepcopy(candidate)
    reversed_candidate["operations"].reverse()
    reversed_candidate["proposal_id"] = sha256_json(
        {
            key: value
            for key, value in reversed_candidate.items()
            if key != "proposal_id"
        }
    )
    with pytest.raises(BehaviorEvidenceError, match="unique sorted paths"):
        validate_behavior_registry_candidate_patch(reversed_candidate)


def test_boundary_exposes_no_launch_or_apply_function() -> None:
    import apu.behavior_evidence as boundary

    names = set(dir(boundary))
    assert "launch_provider" not in names
    assert "apply_behavior_registry_candidate_patch" not in names

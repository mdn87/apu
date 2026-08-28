from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .models import canonical_json, sha256_json
from .work_orders import find_secret_spans

EVIDENCE_KIND = "lugos.apu.behavior-evaluation-evidence"
CANDIDATE_PATCH_KIND = "lugos.apu.lugos-orca.behavior-registry-candidate-patch"
SOURCE_RECEIPT_KIND = "lugos.autowork.behavior-delegation-receipt"

_HASH = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_FULL_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_SAFE_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/%+-]{0,63}$")
_ARTIFACT_KINDS = frozenset(
    {"instruction", "skill", "hook", "tool-policy", "output-contract"}
)
_EVALUATION_STATUSES = frozenset({"passed", "failed", "unavailable", "skipped"})
_FORBIDDEN_PRIVACY_KEYS = frozenset(
    {
        "prompt",
        "raw_prompt",
        "response",
        "raw_response",
        "reasoning",
        "environment",
        "credentials",
        "credential",
        "api_key",
        "access_token",
        "password",
        "secret",
        "command",
        "tool_input",
        "tool_output",
        "raw_output",
    }
)


class BehaviorEvidenceError(ValueError):
    """Raised when an Orca behavior artifact violates the APU boundary."""


def _copy_json(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BehaviorEvidenceError(f"{label} must be an object")
    return value


def _exact(value: Mapping[str, Any], required: set[str], label: str) -> None:
    actual = set(value)
    if actual != required:
        missing = sorted(required - actual)
        extra = sorted(actual - required)
        raise BehaviorEvidenceError(
            f"{label} fields must be exact; missing={missing}, extra={extra}"
        )


def _string(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise BehaviorEvidenceError(f"{label} must be a non-empty bounded string")
    return value


def _source_string(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise BehaviorEvidenceError(f"{label} must be a non-empty string")
    return value


def _identifier(value: Any, label: str) -> str:
    text = _string(value, label)
    assert text is not None
    if _SAFE_ID.fullmatch(text) is None:
        raise BehaviorEvidenceError(f"{label} contains unsupported characters")
    return text


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise BehaviorEvidenceError(f"{label} must be a lowercase SHA-256")
    return value


def _timestamp(value: Any, label: str) -> str:
    text = _string(value, label)
    assert text is not None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BehaviorEvidenceError(f"{label} must be RFC 3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BehaviorEvidenceError(f"{label} must include a timezone")
    return text


def _created_at(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return _timestamp(value, "created_at")


def _reject_private_content(value: Any, label: str = "artifact") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _FORBIDDEN_PRIVACY_KEYS:
                raise BehaviorEvidenceError(
                    f"{label} contains forbidden private field {key!r}"
                )
            _reject_private_content(item, f"{label}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_private_content(item, f"{label}[{index}]")
    elif isinstance(value, str) and find_secret_spans(value):
        raise BehaviorEvidenceError(f"{label} contains credential-shaped content")


def _validate_revision(
    value: Any, label: str, *, full_commit: bool = False
) -> dict[str, str]:
    revision = _object(value, label)
    _exact(revision, {"orca_commit", "behavior_tree_sha256"}, label)
    commit = revision["orca_commit"]
    pattern = _FULL_COMMIT if full_commit else _COMMIT
    if not isinstance(commit, str) or pattern.fullmatch(commit) is None:
        qualifier = "full " if full_commit else ""
        raise BehaviorEvidenceError(
            f"{label}.orca_commit must be a {qualifier}lowercase Git object ID"
        )
    return {
        "orca_commit": commit,
        "behavior_tree_sha256": _hash(
            revision["behavior_tree_sha256"], f"{label}.behavior_tree_sha256"
        ),
    }


def _validate_artifacts(value: Any, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise BehaviorEvidenceError(f"{label} must be an array")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(value):
        item_label = f"{label}[{index}]"
        item = _object(raw, item_label)
        _exact(item, {"id", "kind", "content_sha256"}, item_label)
        kind = item["kind"]
        if kind not in _ARTIFACT_KINDS:
            raise BehaviorEvidenceError(f"{item_label}.kind is unsupported")
        record = {
            "id": _source_string(item["id"], f"{item_label}.id"),
            "kind": kind,
            "content_sha256": _hash(
                item["content_sha256"], f"{item_label}.content_sha256"
            ),
        }
        identity = (record["kind"], record["id"], record["content_sha256"])
        if identity in seen:
            raise BehaviorEvidenceError(f"{label} contains a duplicate")
        seen.add(identity)
        result.append(record)
    return result


def _validate_receipt(value: Any) -> dict[str, Any]:
    receipt = _object(value, "receipt")
    fields = {
        "schema_version",
        "kind",
        "run_id",
        "assignment_id",
        "loadout_advice_sha256",
        "seat_policy_advice_sha256",
        "behavior_advice_sha256",
        "seat",
        "route",
        "provider",
        "model_requested",
        "model_resolved",
        "thinking_level_requested",
        "adaptation_tier",
        "registry_schema_version",
        "registry_content_revision",
        "compiled_envelope_sha256",
        "applied_artifacts",
        "narrowings",
        "rejected_techniques",
        "telemetry",
        "receipt_sha256",
    }
    _exact(receipt, fields, "receipt")
    _reject_private_content(receipt, "receipt")
    if receipt["schema_version"] != 1 or receipt["kind"] != SOURCE_RECEIPT_KIND:
        raise BehaviorEvidenceError("receipt contract is unsupported")
    if receipt["registry_schema_version"] != 1:
        raise BehaviorEvidenceError("receipt registry_schema_version is unsupported")
    tier = receipt["adaptation_tier"]
    if type(tier) is not int or tier not in {0, 1, 2}:
        raise BehaviorEvidenceError("APU v1 imports only adaptation tiers 0 through 2")
    result = dict(receipt)
    for name in (
        "run_id",
        "assignment_id",
        "seat",
        "route",
        "provider",
    ):
        result[name] = _source_string(receipt[name], f"receipt.{name}")
    for name in (
        "model_requested",
        "model_resolved",
        "thinking_level_requested",
    ):
        result[name] = _source_string(receipt[name], f"receipt.{name}", nullable=True)
    for name in (
        "loadout_advice_sha256",
        "seat_policy_advice_sha256",
        "behavior_advice_sha256",
        "compiled_envelope_sha256",
        "receipt_sha256",
    ):
        result[name] = _hash(receipt[name], f"receipt.{name}")
    result["registry_content_revision"] = _validate_revision(
        receipt["registry_content_revision"], "receipt.registry_content_revision"
    )
    result["applied_artifacts"] = _validate_artifacts(
        receipt["applied_artifacts"], "receipt.applied_artifacts"
    )
    narrowings = receipt["narrowings"]
    if not isinstance(narrowings, list):
        raise BehaviorEvidenceError("receipt.narrowings must be an array")
    for index, raw in enumerate(narrowings):
        label = f"receipt.narrowings[{index}]"
        item = _object(raw, label)
        _exact(item, {"field", "advised_sha256", "applied_sha256", "reason"}, label)
        _source_string(item["field"], f"{label}.field")
        _hash(item["advised_sha256"], f"{label}.advised_sha256")
        _hash(item["applied_sha256"], f"{label}.applied_sha256")
        _source_string(item["reason"], f"{label}.reason")
    rejections = receipt["rejected_techniques"]
    if not isinstance(rejections, list):
        raise BehaviorEvidenceError("receipt.rejected_techniques must be an array")
    for index, raw in enumerate(rejections):
        label = f"receipt.rejected_techniques[{index}]"
        item = _object(raw, label)
        _exact(item, {"technique_id", "reason"}, label)
        _source_string(item["technique_id"], f"{label}.technique_id")
        _source_string(item["reason"], f"{label}.reason")
    telemetry = _object(receipt["telemetry"], "receipt.telemetry")
    _exact(
        telemetry,
        {"duration_ms", "input_tokens", "output_tokens", "exit_status"},
        "receipt.telemetry",
    )
    for name in ("duration_ms", "input_tokens", "output_tokens"):
        number = telemetry[name]
        if number is not None and (type(number) is not int or number < 0):
            raise BehaviorEvidenceError(f"receipt.telemetry.{name} must be nonnegative")
    _source_string(
        telemetry["exit_status"], "receipt.telemetry.exit_status", nullable=True
    )
    body = {key: item for key, item in receipt.items() if key != "receipt_sha256"}
    if receipt["receipt_sha256"] != sha256_json(body):
        raise BehaviorEvidenceError("receipt_sha256 does not match canonical receipt")
    return result


def _validate_evaluation(value: Any) -> dict[str, Any]:
    evaluation = _object(value, "evaluation input")
    _exact(
        evaluation,
        {"suite_id", "task_id", "comparison_group_id", "status", "checks", "metrics"},
        "evaluation input",
    )
    _reject_private_content(evaluation, "evaluation input")
    result: dict[str, Any] = {
        "suite_id": _identifier(evaluation["suite_id"], "evaluation input.suite_id"),
        "task_id": _identifier(evaluation["task_id"], "evaluation input.task_id"),
        "comparison_group_id": _identifier(
            evaluation["comparison_group_id"], "evaluation input.comparison_group_id"
        ),
    }
    status = evaluation["status"]
    if status not in _EVALUATION_STATUSES:
        raise BehaviorEvidenceError("evaluation input.status is unsupported")
    result["status"] = status
    checks = evaluation["checks"]
    if not isinstance(checks, list):
        raise BehaviorEvidenceError("evaluation input.checks must be an array")
    normalized_checks: list[dict[str, str]] = []
    seen_checks: set[str] = set()
    for index, raw in enumerate(checks):
        label = f"evaluation input.checks[{index}]"
        item = _object(raw, label)
        _exact(item, {"id", "status"}, label)
        check_id = _identifier(item["id"], f"{label}.id")
        if check_id in seen_checks or item["status"] not in _EVALUATION_STATUSES:
            raise BehaviorEvidenceError(
                f"{label} is duplicate or has unsupported status"
            )
        seen_checks.add(check_id)
        normalized_checks.append({"id": check_id, "status": item["status"]})
    metrics = evaluation["metrics"]
    if not isinstance(metrics, list):
        raise BehaviorEvidenceError("evaluation input.metrics must be an array")
    normalized_metrics: list[dict[str, Any]] = []
    seen_metrics: set[str] = set()
    for index, raw in enumerate(metrics):
        label = f"evaluation input.metrics[{index}]"
        item = _object(raw, label)
        _exact(item, {"id", "value", "unit"}, label)
        metric_id = _identifier(item["id"], f"{label}.id")
        number = item["value"]
        unit = item["unit"]
        if (
            metric_id in seen_metrics
            or isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or not isinstance(unit, str)
            or _SAFE_UNIT.fullmatch(unit) is None
        ):
            raise BehaviorEvidenceError(f"{label} is invalid")
        seen_metrics.add(metric_id)
        normalized_metrics.append({"id": metric_id, "value": number, "unit": unit})
    result["checks"] = sorted(normalized_checks, key=lambda item: item["id"])
    result["metrics"] = sorted(normalized_metrics, key=lambda item: item["id"])
    return result


def import_behavior_evaluation_evidence(
    receipt: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    *,
    created_at: str | None = None,
) -> BehaviorEvaluationEvidence:
    source = _validate_receipt(receipt)
    result = _validate_evaluation(evaluation)
    artifacts = sorted(
        source["applied_artifacts"],
        key=lambda item: (item["kind"], item["id"], item["content_sha256"]),
    )
    rejected_ids = sorted(
        {item["technique_id"] for item in source["rejected_techniques"]}
    )
    body: dict[str, Any] = {
        "schema_version": 1,
        "kind": EVIDENCE_KIND,
        "created_at": _created_at(created_at),
        "source": {
            "contract": SOURCE_RECEIPT_KIND,
            "receipt_sha256": source["receipt_sha256"],
            "behavior_advice_sha256": source["behavior_advice_sha256"],
            "compiled_envelope_sha256": source["compiled_envelope_sha256"],
        },
        "run": {
            "run_id": source["run_id"],
            "assignment_id": source["assignment_id"],
            "suite_id": result["suite_id"],
            "task_id": result["task_id"],
            "comparison_group_id": result["comparison_group_id"],
        },
        "identity": {
            "provider": source["provider"],
            "model_requested": source["model_requested"],
            "model_resolved": source["model_resolved"],
            "thinking_level_requested": source["thinking_level_requested"],
            "route": source["route"],
            "seat": source["seat"],
            "adaptation_tier": source["adaptation_tier"],
        },
        "registry_revision": source["registry_content_revision"],
        "applied_artifacts": artifacts,
        "narrowing_count": len(source["narrowings"]),
        "rejected_technique_ids": rejected_ids,
        "telemetry": dict(source["telemetry"]),
        "evaluation": {
            "status": result["status"],
            "checks": result["checks"],
            "metrics": result["metrics"],
        },
        "privacy": {
            "redacted": True,
            "contains_raw_prompt": False,
            "contains_raw_response": False,
            "contains_reasoning": False,
            "contains_environment": False,
            "contains_credentials": False,
        },
    }
    value = {**body, "evidence_id": sha256_json(body)}
    return BehaviorEvaluationEvidence(value)


def _validate_evidence_mapping(value: Any) -> dict[str, Any]:
    evidence = _object(value, "behavior evidence")
    top_fields = {
        "schema_version",
        "kind",
        "evidence_id",
        "created_at",
        "source",
        "run",
        "identity",
        "registry_revision",
        "applied_artifacts",
        "narrowing_count",
        "rejected_technique_ids",
        "telemetry",
        "evaluation",
        "privacy",
    }
    _exact(evidence, top_fields, "behavior evidence")
    _reject_private_content(evidence, "behavior evidence")
    if evidence["schema_version"] != 1 or evidence["kind"] != EVIDENCE_KIND:
        raise BehaviorEvidenceError("behavior evidence contract is unsupported")
    _hash(evidence["evidence_id"], "behavior evidence.evidence_id")
    _timestamp(evidence["created_at"], "behavior evidence.created_at")
    source = _object(evidence["source"], "behavior evidence.source")
    _exact(
        source,
        {
            "contract",
            "receipt_sha256",
            "behavior_advice_sha256",
            "compiled_envelope_sha256",
        },
        "behavior evidence.source",
    )
    if source["contract"] != SOURCE_RECEIPT_KIND:
        raise BehaviorEvidenceError("behavior evidence source contract is unsupported")
    for name in (
        "receipt_sha256",
        "behavior_advice_sha256",
        "compiled_envelope_sha256",
    ):
        _hash(source[name], f"behavior evidence.source.{name}")
    run = _object(evidence["run"], "behavior evidence.run")
    _exact(
        run,
        {"run_id", "assignment_id", "suite_id", "task_id", "comparison_group_id"},
        "behavior evidence.run",
    )
    for name in ("run_id", "assignment_id"):
        _source_string(run[name], f"behavior evidence.run.{name}")
    for name in ("suite_id", "task_id", "comparison_group_id"):
        _identifier(run[name], f"behavior evidence.run.{name}")
    identity = _object(evidence["identity"], "behavior evidence.identity")
    _exact(
        identity,
        {
            "provider",
            "model_requested",
            "model_resolved",
            "thinking_level_requested",
            "route",
            "seat",
            "adaptation_tier",
        },
        "behavior evidence.identity",
    )
    for name in ("provider", "route", "seat"):
        _source_string(identity[name], f"behavior evidence.identity.{name}")
    for name in ("model_requested", "model_resolved", "thinking_level_requested"):
        _source_string(
            identity[name], f"behavior evidence.identity.{name}", nullable=True
        )
    if type(identity["adaptation_tier"]) is not int or identity[
        "adaptation_tier"
    ] not in {0, 1, 2}:
        raise BehaviorEvidenceError("behavior evidence adaptation_tier is unsupported")
    _validate_revision(
        evidence["registry_revision"], "behavior evidence.registry_revision"
    )
    artifacts = _validate_artifacts(
        evidence["applied_artifacts"], "behavior evidence.applied_artifacts"
    )
    if artifacts != sorted(
        artifacts, key=lambda item: (item["kind"], item["id"], item["content_sha256"])
    ):
        raise BehaviorEvidenceError(
            "behavior evidence applied_artifacts are not canonical"
        )
    if type(evidence["narrowing_count"]) is not int or evidence["narrowing_count"] < 0:
        raise BehaviorEvidenceError(
            "behavior evidence narrowing_count must be nonnegative"
        )
    rejected = evidence["rejected_technique_ids"]
    if not isinstance(rejected, list) or rejected != sorted(set(rejected)):
        raise BehaviorEvidenceError(
            "behavior evidence rejected_technique_ids are not canonical"
        )
    for index, item in enumerate(rejected):
        _source_string(item, f"behavior evidence.rejected_technique_ids[{index}]")
    telemetry = _object(evidence["telemetry"], "behavior evidence.telemetry")
    _exact(
        telemetry,
        {"duration_ms", "input_tokens", "output_tokens", "exit_status"},
        "behavior evidence.telemetry",
    )
    for name in ("duration_ms", "input_tokens", "output_tokens"):
        number = telemetry[name]
        if number is not None and (type(number) is not int or number < 0):
            raise BehaviorEvidenceError(
                f"behavior evidence.telemetry.{name} must be nonnegative"
            )
    _source_string(
        telemetry["exit_status"],
        "behavior evidence.telemetry.exit_status",
        nullable=True,
    )
    evaluation = _object(evidence["evaluation"], "behavior evidence.evaluation")
    _validate_evaluation(
        {
            "suite_id": run["suite_id"],
            "task_id": run["task_id"],
            "comparison_group_id": run["comparison_group_id"],
            **evaluation,
        }
    )
    privacy = _object(evidence["privacy"], "behavior evidence.privacy")
    _exact(
        privacy,
        {
            "redacted",
            "contains_raw_prompt",
            "contains_raw_response",
            "contains_reasoning",
            "contains_environment",
            "contains_credentials",
        },
        "behavior evidence.privacy",
    )
    if privacy != {
        "redacted": True,
        "contains_raw_prompt": False,
        "contains_raw_response": False,
        "contains_reasoning": False,
        "contains_environment": False,
        "contains_credentials": False,
    }:
        raise BehaviorEvidenceError("behavior evidence privacy declaration is unsafe")
    body = {key: item for key, item in evidence.items() if key != "evidence_id"}
    if evidence["evidence_id"] != sha256_json(body):
        raise BehaviorEvidenceError("evidence_id does not match canonical evidence")
    return _copy_json(evidence)


def validate_behavior_evaluation_evidence(value: Any) -> dict[str, Any]:
    if isinstance(value, BehaviorEvaluationEvidence):
        value = value.to_dict()
    return _validate_evidence_mapping(value)


@dataclass(frozen=True)
class BehaviorEvaluationEvidence:
    _value: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_value", _validate_evidence_mapping(self._value))

    @property
    def evidence_id(self) -> str:
        return str(self._value["evidence_id"])

    @property
    def registry_revision(self) -> Mapping[str, str]:
        return dict(self._value["registry_revision"])

    def to_dict(self) -> dict[str, Any]:
        return _copy_json(self._value)


def _operation_path(value: Any, label: str) -> str:
    path = _string(value, label)
    assert path is not None
    if (
        not path.startswith("/behavior/")
        or "\\" in path
        or "//" in path
        or any(segment in {"", ".", ".."} for segment in path.split("/")[1:])
    ):
        raise BehaviorEvidenceError(f"{label} must stay under /behavior")
    return path


def _validate_operations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise BehaviorEvidenceError(
            "candidate patch operations must be a non-empty array"
        )
    result: list[dict[str, Any]] = []
    paths: list[str] = []
    for index, raw in enumerate(value):
        label = f"candidate patch.operations[{index}]"
        operation = _object(raw, label)
        op = operation.get("op")
        expected = {
            "add": {"op", "path", "value", "value_sha256"},
            "replace": {"op", "path", "prior_value_sha256", "value", "value_sha256"},
            "remove": {"op", "path", "prior_value_sha256"},
        }.get(op)
        if expected is None:
            raise BehaviorEvidenceError(f"{label}.op is unsupported")
        _exact(operation, expected, label)
        path = _operation_path(operation["path"], f"{label}.path")
        normalized = dict(operation)
        if "prior_value_sha256" in operation:
            normalized["prior_value_sha256"] = _hash(
                operation["prior_value_sha256"], f"{label}.prior_value_sha256"
            )
        if "value" in operation:
            _reject_private_content(operation["value"], f"{label}.value")
            expected_hash = _hash(operation["value_sha256"], f"{label}.value_sha256")
            if expected_hash != sha256_json(operation["value"]):
                raise BehaviorEvidenceError(
                    f"{label}.value_sha256 does not match value"
                )
        normalized["path"] = path
        paths.append(path)
        result.append(normalized)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise BehaviorEvidenceError(
            "candidate patch operations must have unique sorted paths"
        )
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if other.startswith(path.rstrip("/") + "/"):
                raise BehaviorEvidenceError(
                    "candidate patch operations may not overlap"
                )
    return result


def _validate_candidate_mapping(value: Any) -> dict[str, Any]:
    candidate = _object(value, "candidate patch")
    fields = {
        "schema_version",
        "kind",
        "proposal_id",
        "created_at",
        "target_repository",
        "base_registry_revision",
        "evidence",
        "operations",
        "requires_review",
        "apply_authorized",
    }
    _exact(candidate, fields, "candidate patch")
    _reject_private_content(candidate, "candidate patch")
    if candidate["schema_version"] != 1 or candidate["kind"] != CANDIDATE_PATCH_KIND:
        raise BehaviorEvidenceError("candidate patch contract is unsupported")
    if candidate["target_repository"] != "lugos-orca":
        raise BehaviorEvidenceError("candidate patch target_repository is unsupported")
    if (
        candidate["requires_review"] is not True
        or candidate["apply_authorized"] is not False
    ):
        raise BehaviorEvidenceError("candidate patch must remain review-only")
    _hash(candidate["proposal_id"], "candidate patch.proposal_id")
    _timestamp(candidate["created_at"], "candidate patch.created_at")
    _validate_revision(
        candidate["base_registry_revision"],
        "candidate patch.base_registry_revision",
        full_commit=True,
    )
    evidence = _object(candidate["evidence"], "candidate patch.evidence")
    _exact(
        evidence,
        {
            "supporting_evidence_ids",
            "counterexample_evidence_ids",
            "evidence_bundle_sha256",
        },
        "candidate patch.evidence",
    )
    supporting = evidence["supporting_evidence_ids"]
    counter = evidence["counterexample_evidence_ids"]
    for name, ids, allow_empty in (
        ("supporting_evidence_ids", supporting, False),
        ("counterexample_evidence_ids", counter, True),
    ):
        if (
            not isinstance(ids, list)
            or (not allow_empty and not ids)
            or ids != sorted(set(ids))
        ):
            raise BehaviorEvidenceError(
                f"candidate patch.evidence.{name} must be canonical"
            )
        for index, item in enumerate(ids):
            _hash(item, f"candidate patch.evidence.{name}[{index}]")
    if set(supporting) & set(counter):
        raise BehaviorEvidenceError(
            "supporting and counterexample evidence must be disjoint"
        )
    bundle = {
        "schema_version": 1,
        "kind": "lugos.apu.behavior-evaluation-evidence-bundle",
        "evidence_ids": sorted([*supporting, *counter]),
    }
    if _hash(
        evidence["evidence_bundle_sha256"],
        "candidate patch.evidence.evidence_bundle_sha256",
    ) != sha256_json(bundle):
        raise BehaviorEvidenceError(
            "evidence_bundle_sha256 does not match evidence IDs"
        )
    _validate_operations(candidate["operations"])
    body = {key: item for key, item in candidate.items() if key != "proposal_id"}
    if candidate["proposal_id"] != sha256_json(body):
        raise BehaviorEvidenceError(
            "proposal_id does not match canonical candidate patch"
        )
    return _copy_json(candidate)


def validate_behavior_registry_candidate_patch(value: Any) -> dict[str, Any]:
    if isinstance(value, BehaviorRegistryCandidatePatch):
        value = value.to_dict()
    return _validate_candidate_mapping(value)


def build_behavior_registry_candidate_patch(
    *,
    base_registry_revision: Mapping[str, Any],
    supporting_evidence: Sequence[BehaviorEvaluationEvidence | Mapping[str, Any]],
    operations: Sequence[Mapping[str, Any]],
    counterexample_evidence: Sequence[
        BehaviorEvaluationEvidence | Mapping[str, Any]
    ] = (),
    created_at: str | None = None,
) -> BehaviorRegistryCandidatePatch:
    revision = _validate_revision(
        base_registry_revision, "base_registry_revision", full_commit=True
    )
    supporting = [
        validate_behavior_evaluation_evidence(item) for item in supporting_evidence
    ]
    counter = [
        validate_behavior_evaluation_evidence(item) for item in counterexample_evidence
    ]
    if not supporting:
        raise BehaviorEvidenceError(
            "at least one supporting evidence record is required"
        )
    for item in [*supporting, *counter]:
        if item["registry_revision"] != revision:
            raise BehaviorEvidenceError(
                "evidence registry revision does not exactly match candidate base"
            )
    supporting_ids = sorted({item["evidence_id"] for item in supporting})
    counter_ids = sorted({item["evidence_id"] for item in counter})
    if set(supporting_ids) & set(counter_ids):
        raise BehaviorEvidenceError(
            "supporting and counterexample evidence must be disjoint"
        )
    normalized_operations = sorted(
        (_copy_json(item) for item in operations),
        key=lambda item: str(item.get("path", "")),
    )
    _validate_operations(normalized_operations)
    bundle = {
        "schema_version": 1,
        "kind": "lugos.apu.behavior-evaluation-evidence-bundle",
        "evidence_ids": sorted([*supporting_ids, *counter_ids]),
    }
    body: dict[str, Any] = {
        "schema_version": 1,
        "kind": CANDIDATE_PATCH_KIND,
        "created_at": _created_at(created_at),
        "target_repository": "lugos-orca",
        "base_registry_revision": revision,
        "evidence": {
            "supporting_evidence_ids": supporting_ids,
            "counterexample_evidence_ids": counter_ids,
            "evidence_bundle_sha256": sha256_json(bundle),
        },
        "operations": normalized_operations,
        "requires_review": True,
        "apply_authorized": False,
    }
    return BehaviorRegistryCandidatePatch({**body, "proposal_id": sha256_json(body)})


@dataclass(frozen=True)
class BehaviorRegistryCandidatePatch:
    _value: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_value", _validate_candidate_mapping(self._value))

    @property
    def proposal_id(self) -> str:
        return str(self._value["proposal_id"])

    def to_dict(self) -> dict[str, Any]:
        return _copy_json(self._value)

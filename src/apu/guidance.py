from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypedDict
from urllib.parse import urlsplit

from .classify import DetectorPolicy
from .models import canonical_json, sha256_bytes, sha256_json
from .state import ensure_private_directory, write_json_atomic
from .work_orders import find_secret_spans

GUIDANCE_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_POLICY_ALLOWLIST = {
    ("duplicate-instruction", "minimum_words"): "integer:2..100",
    (
        "universal-skill-trigger",
        "speculative_threshold_enabled",
    ): "boolean",
}
_CANDIDATE_SCHEMA = {
    "exact_top_level_fields": [
        "schema_version",
        "artifact_type",
        "work_order_id",
        "principles",
    ],
    "fixed_values": {
        "schema_version": GUIDANCE_SCHEMA_VERSION,
        "artifact_type": "guidance-baseline-candidate",
    },
    "work_order_id": "this work order's lowercase SHA-256 identifier",
    "principles": "non-empty array of principle objects",
    "principle": {
        "exact_fields": [
            "principle_id",
            "statement",
            "sources",
            "detector_policies",
        ],
        "principle_id": "safe non-empty identifier",
        "statement": "non-empty distilled guidance; no copied credentials",
        "sources": "one or more source objects",
        "detector_policies": "one or more allowlisted policy objects",
    },
    "source": {
        "exact_fields": [
            "source_url",
            "retrieved_at",
            "content_sha256",
        ],
        "source_url": "URL from this work order",
        "retrieved_at": "RFC 3339 timestamp from its snapshot reference",
        "content_sha256": "lowercase SHA-256 from its snapshot reference",
    },
    "detector_policy": {
        "exact_fields": [
            "detector_id",
            "setting",
            "value",
            "justification",
            "source_sha256s",
        ],
        "allowlist": [
            {
                "detector_id": detector_id,
                "setting": setting,
                "value_type": value_type,
            }
            for (detector_id, setting), value_type in sorted(_POLICY_ALLOWLIST.items())
        ],
        "justification": "non-empty explanation grounded in cited snapshots",
        "source_sha256s": "non-empty subset of the principle source hashes",
    },
}
_DISTILLATION_INSTRUCTIONS = [
    "Read only snapshot refs listed by private_snapshot_access.allowed_refs.",
    (
        "Use read_guidance_work_order_snapshot with APU_HOME, this work order, "
        "and the selected ref; never resolve arbitrary paths."
    ),
    (
        "Distill durable principles, cite the exact URL/retrieval/hash metadata, "
        "and map only policies present in candidate_schema.detector_policy.allowlist."
    ),
    (
        "Return one JSON guidance-baseline-candidate matching candidate_schema; "
        "do not modify source snapshots or user-owned surfaces."
    ),
]
_DISTILLATION_ACCEPTANCE_CRITERIA = [
    "The candidate contains only the exact declared fields and fixed values.",
    "Every citation resolves to an allowed, hash-verified private snapshot.",
    "Every detector policy is allowlisted, typed, justified, and source-backed.",
    "No credential-shaped source text is copied into the candidate.",
    "A human review records approved status before adoption.",
]


class GuidanceError(ValueError):
    """Raised when guidance state or an artifact violates its contract."""


@dataclass(frozen=True)
class FetchResponse:
    """The raw result returned by an explicitly supplied network fetcher."""

    content: bytes
    media_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("guidance fetch content must be bytes")
        if self.media_type is not None and (
            not isinstance(self.media_type, str) or not self.media_type.strip()
        ):
            raise TypeError("guidance fetch media_type must be non-empty text")


GuidanceFetcher = Callable[[str], FetchResponse | bytes]


class GuidanceEvaluationStamp(TypedDict):
    """Audit-safe summary of the currently adopted guidance baseline."""

    version: str | None
    status: Literal["unconfigured", "adopted", "stale"]
    retrieved_at: str | None
    artifact_sha256: str | None


def guidance_evaluation_stamp(state_home: Path) -> GuidanceEvaluationStamp:
    """Read and validate the current baseline without creating state.

    Raw guidance objects are intentionally neither opened nor returned. A
    baseline becomes stale when a cited source is degraded, has disappeared
    from source status, or has a newer successful content hash.
    """

    root = Path(state_home) / "guidance"
    current_path = root / "baselines" / "current.json"
    if not current_path.exists():
        return {
            "version": None,
            "status": "unconfigured",
            "retrieved_at": None,
            "artifact_sha256": None,
        }

    baseline = _load_json(current_path, "current guidance baseline")
    _validate_baseline(baseline)
    immutable = _load_json(
        root / "baselines" / f"{baseline['baseline_version']}.json",
        "immutable guidance baseline",
    )
    _validate_baseline(immutable)
    if immutable != baseline:
        raise GuidanceError(
            "current guidance baseline does not match its immutable artifact"
        )

    citations = [
        source
        for principle in baseline["principles"]
        for source in principle["sources"]
    ]
    retrieved_at = max(
        (source["retrieved_at"] for source in citations),
        key=lambda value: datetime.fromisoformat(value),
    )
    status: Literal["adopted", "stale"] = "adopted"
    for citation in citations:
        source_status_path = _source_root(root, citation["source_url"]) / "status.json"
        if not source_status_path.exists():
            status = "stale"
            break
        source_status = _load_json(
            source_status_path,
            "guidance source status",
        )
        _validate_observation(source_status)
        if source_status["source_url"] != citation["source_url"]:
            raise GuidanceError("guidance source status URL does not match its path")
        if (
            source_status["status"] != "fresh"
            or source_status["content_sha256"] != citation["content_sha256"]
        ):
            status = "stale"
            break

    return {
        "version": baseline["baseline_version"],
        "status": status,
        "retrieved_at": retrieved_at,
        "artifact_sha256": sha256_json(baseline),
    }


def refresh_guidance(
    state_home: Path,
    source_urls: Sequence[str],
    *,
    fetcher: GuidanceFetcher,
    retrieved_at: str,
) -> dict[str, Any]:
    """Explicitly fetch configured sources and persist immutable observations.

    Network behavior is deliberately absent from core: callers must inject a
    fetcher for each explicit refresh invocation.
    """

    timestamp = _timestamp(retrieved_at, "retrieved_at")
    urls = _source_urls(source_urls)
    if not callable(fetcher):
        raise TypeError("fetcher must be callable")

    root = _guidance_root(state_home)
    results: list[dict[str, Any]] = []
    for source_url in urls:
        previous = _last_success(root, source_url)
        try:
            fetched = fetcher(source_url)
            response = FetchResponse(fetched) if isinstance(fetched, bytes) else fetched
            if not isinstance(response, FetchResponse):
                raise TypeError("fetcher must return bytes or FetchResponse")
        # Fetcher failures are data for stale-state reporting, regardless of
        # the provider-specific exception hierarchy.
        except Exception as error:  # noqa: BLE001
            error_code = type(error).__name__
            if _SAFE_ERROR_CODE.fullmatch(error_code) is None:
                error_code = "FetchError"
            result = {
                "source_url": source_url,
                "status": "stale" if previous is not None else "unavailable",
                "retrieved_at": timestamp,
                "error_code": error_code,
                "last_success": previous,
            }
        else:
            content_sha256 = sha256_bytes(response.content)
            _write_bytes_once(
                root / "objects" / f"{content_sha256}.bin",
                response.content,
            )
            result = {
                "source_url": source_url,
                "status": "fresh",
                "retrieved_at": timestamp,
                "content_sha256": content_sha256,
                "media_type": (
                    response.media_type.strip()
                    if response.media_type is not None
                    else None
                ),
            }

        _validate_source_result(result)
        observation_id = sha256_json(result)
        observation = {
            "schema_version": GUIDANCE_SCHEMA_VERSION,
            "artifact_type": "guidance-source-observation",
            "observation_id": observation_id,
            **result,
        }
        _write_json_once(
            _source_root(root, source_url) / "observations" / f"{observation_id}.json",
            observation,
        )
        write_json_atomic(
            _source_root(root, source_url) / "status.json",
            observation,
        )
        results.append(result)

    body = {
        "schema_version": GUIDANCE_SCHEMA_VERSION,
        "artifact_type": "guidance-refresh",
        "retrieved_at": timestamp,
        "sources": results,
    }
    refresh_id = sha256_json(body)
    artifact = {**body, "refresh_id": refresh_id}
    _validate_refresh(artifact)
    _write_json_once(root / "refreshes" / f"{refresh_id}.json", artifact)
    return artifact


def write_guidance_distillation_work_order(
    state_home: Path,
    refresh: Mapping[str, Any],
) -> dict[str, Any]:
    """Write a privacy-safe judgment boundary without fetched source prose."""

    normalized = _json_object(refresh, "guidance refresh")
    _validate_refresh(normalized)
    source_references = []
    allowed_refs: set[str] = set()
    for source in normalized["sources"]:
        reference = {
            "source_url": source["source_url"],
            "status": source["status"],
            "retrieved_at": source["retrieved_at"],
        }
        if source["status"] == "fresh":
            snapshot = _work_order_snapshot_reference(source)
            reference["snapshot"] = snapshot
            allowed_refs.add(snapshot["ref"])
        elif source["status"] == "stale":
            snapshot = _work_order_snapshot_reference(source["last_success"])
            reference["snapshot"] = snapshot
            allowed_refs.add(snapshot["ref"])
        else:
            reference["snapshot"] = None
        source_references.append(reference)

    body = {
        "schema_version": GUIDANCE_SCHEMA_VERSION,
        "artifact_type": "guidance-distillation-work-order",
        "refresh_id": normalized["refresh_id"],
        "privacy_contract": (
            "Source content remains in private APU state; the candidate may "
            "contain distilled principles and citations, never copied secrets."
        ),
        "sources": source_references,
        "private_snapshot_access": {
            "root": "guidance/objects",
            "mode": "read-only",
            "resolver": "apu.guidance.read_guidance_work_order_snapshot",
            "allowed_refs": sorted(allowed_refs),
            "contract": (
                "Resolve only an allowed relative ref beneath APU_HOME, reject "
                "links and traversal, and verify bytes against the ref SHA-256."
            ),
        },
        "instructions": list(_DISTILLATION_INSTRUCTIONS),
        "candidate_schema": json.loads(canonical_json(_CANDIDATE_SCHEMA)),
        "acceptance_criteria": list(_DISTILLATION_ACCEPTANCE_CRITERIA),
    }
    work_order_id = sha256_json(body)
    artifact = {**body, "work_order_id": work_order_id}
    _validate_work_order(artifact)
    _write_json_once(
        _guidance_root(state_home) / "work-orders" / f"{work_order_id}.json",
        artifact,
    )
    return artifact


def read_guidance_work_order_snapshot(
    state_home: Path,
    work_order: Mapping[str, Any],
    snapshot_ref: str,
) -> bytes:
    """Read one explicitly allowed private snapshot through a bounded resolver."""

    normalized = _json_object(work_order, "guidance distillation work order")
    _validate_work_order(normalized)
    if not isinstance(snapshot_ref, str):
        raise TypeError("snapshot_ref must be text")
    allowed = normalized["private_snapshot_access"]["allowed_refs"]
    if snapshot_ref not in allowed:
        raise GuidanceError("snapshot_ref is not allowed by this work order")
    logical = PurePosixPath(snapshot_ref)
    if logical.is_absolute() or ".." in logical.parts or len(logical.parts) != 3:
        raise GuidanceError("snapshot_ref must be a bounded relative object ref")
    if logical.parts[:2] != ("guidance", "objects"):
        raise GuidanceError("snapshot_ref is outside the guidance object root")
    filename = logical.parts[2]
    if not filename.endswith(".bin"):
        raise GuidanceError("snapshot_ref must name a guidance byte object")
    content_sha256 = _hash(filename.removesuffix(".bin"), "snapshot_ref hash")
    try:
        state_root = Path(state_home).resolve(strict=True)
    except OSError as error:
        raise GuidanceError("APU state root is unavailable") from error
    path = state_root.joinpath(*logical.parts)
    if path.is_symlink():
        raise GuidanceError("snapshot_ref must not resolve through a link")
    try:
        resolved = path.resolve(strict=True)
        expected_object_root = state_root / "guidance" / "objects"
        object_root = expected_object_root.resolve(strict=True)
    except OSError as error:
        raise GuidanceError("snapshot_ref is unavailable") from error
    if (
        object_root != expected_object_root
        or resolved.parent != object_root
        or resolved.is_symlink()
    ):
        raise GuidanceError("snapshot_ref escaped the guidance object root")
    content = resolved.read_bytes()
    if sha256_bytes(content) != content_sha256:
        raise GuidanceError("snapshot_ref content hash mismatch")
    return content


def adopt_guidance_baseline(
    state_home: Path,
    candidate: Mapping[str, Any],
    *,
    approval: Mapping[str, Any],
    adopted_at: str,
) -> dict[str, Any]:
    """Validate a reviewed candidate and atomically adopt its baseline."""

    root = _guidance_root(state_home)
    normalized_candidate = _json_object(candidate, "guidance baseline candidate")
    _validate_candidate(normalized_candidate)
    if find_secret_spans(canonical_json(normalized_candidate)):
        raise GuidanceError(
            "guidance baseline candidate contains credential-shaped material"
        )
    normalized_approval = _json_object(approval, "guidance baseline approval")
    _validate_approval(normalized_approval)
    adopted_timestamp = _timestamp(adopted_at, "adopted_at")

    work_order_path = (
        root / "work-orders" / f"{normalized_candidate['work_order_id']}.json"
    )
    work_order = _load_json(work_order_path, "guidance distillation work order")
    _validate_work_order(work_order)
    if work_order["work_order_id"] != normalized_candidate["work_order_id"]:
        raise GuidanceError("candidate work_order_id does not match its work order")

    principles = _normalize_principles(normalized_candidate["principles"])
    permitted_citations = _work_order_citations(work_order)
    for principle in principles:
        for source in principle["sources"]:
            identity = (
                source["source_url"],
                source["retrieved_at"],
                source["content_sha256"],
            )
            if identity not in permitted_citations:
                raise GuidanceError(
                    "candidate cites a source snapshot outside its work order: "
                    f"{source['source_url']} {source['content_sha256']}"
                )
            if not _has_successful_observation(root, source):
                raise GuidanceError(
                    "candidate cites a source snapshot not present in APU state: "
                    f"{source['source_url']} {source['content_sha256']}"
                )

    baseline_version = _semantic_baseline_version(principles)
    artifact = {
        "schema_version": GUIDANCE_SCHEMA_VERSION,
        "artifact_type": "guidance-baseline",
        "baseline_version": baseline_version,
        "adopted_at": adopted_timestamp,
        "work_order_id": normalized_candidate["work_order_id"],
        "review": normalized_approval,
        "principles": principles,
    }
    _validate_baseline(artifact)
    existing = _write_json_once(
        root / "baselines" / f"{baseline_version}.json",
        artifact,
    )
    if existing != artifact:
        _validate_baseline(existing)
        if existing["baseline_version"] != baseline_version:
            raise GuidanceError(
                "baseline version is already bound to different semantics"
            )
        artifact = existing
    write_json_atomic(root / "baselines" / "current.json", artifact)
    return artifact


def load_current_guidance_baseline(state_home: Path) -> dict[str, Any]:
    artifact = _load_json(
        _guidance_root(state_home) / "baselines" / "current.json",
        "current guidance baseline",
    )
    _validate_baseline(artifact)
    return artifact


def detector_policy_from_baseline(
    baseline: Mapping[str, Any],
) -> DetectorPolicy:
    """Derive the only typed detector settings accepted from guidance."""

    normalized = _json_object(baseline, "guidance baseline")
    _validate_baseline(normalized)
    return _detector_policy_from_principles(normalized["principles"])


def load_guidance_detector_policy(state_home: Path) -> DetectorPolicy:
    """Read adopted detector policy without networking or state creation."""

    current = Path(state_home) / "guidance" / "baselines" / "current.json"
    if not current.exists():
        return DetectorPolicy()
    baseline = _load_json(current, "current guidance baseline")
    _validate_baseline(baseline)
    immutable = _load_json(
        current.parent / f"{baseline['baseline_version']}.json",
        "immutable guidance baseline",
    )
    _validate_baseline(immutable)
    if immutable != baseline:
        raise GuidanceError(
            "current guidance baseline does not match its immutable artifact"
        )
    return _detector_policy_from_principles(baseline["principles"])


def diff_guidance_baselines(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a deterministic semantic diff between adopted baselines."""

    old = _json_object(before, "before guidance baseline")
    new = _json_object(after, "after guidance baseline")
    _validate_baseline(old)
    _validate_baseline(new)
    old_by_id = {
        item["principle_id"]: item for item in _semantic_principles(old["principles"])
    }
    new_by_id = {
        item["principle_id"]: item for item in _semantic_principles(new["principles"])
    }
    old_ids = set(old_by_id)
    new_ids = set(new_by_id)
    return {
        "schema_version": GUIDANCE_SCHEMA_VERSION,
        "artifact_type": "guidance-baseline-diff",
        "before_version": old["baseline_version"],
        "after_version": new["baseline_version"],
        "added": [new_by_id[item] for item in sorted(new_ids - old_ids)],
        "removed": [old_by_id[item] for item in sorted(old_ids - new_ids)],
        "changed": [
            {
                "principle_id": item,
                "before": old_by_id[item],
                "after": new_by_id[item],
            }
            for item in sorted(old_ids & new_ids)
            if old_by_id[item] != new_by_id[item]
        ],
    }


def _guidance_root(state_home: Path) -> Path:
    state = ensure_private_directory(Path(state_home))
    root = ensure_private_directory(state / "guidance")
    for name in ("objects", "sources", "refreshes", "work-orders", "baselines"):
        ensure_private_directory(root / name)
    return root


def _source_root(root: Path, source_url: str) -> Path:
    identity = sha256(source_url.encode("utf-8")).hexdigest()
    return root / "sources" / identity


def _source_urls(source_urls: Sequence[str]) -> tuple[str, ...]:
    if isinstance(source_urls, (str, bytes)) or not isinstance(source_urls, Sequence):
        raise TypeError("source_urls must be a sequence")
    urls = tuple(_source_url(item) for item in source_urls)
    if not urls:
        raise GuidanceError("at least one guidance source is required")
    if len(set(urls)) != len(urls):
        raise GuidanceError("guidance source URLs must be unique")
    return tuple(sorted(urls))


def _source_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GuidanceError("source_url must be non-empty text")
    normalized = value.strip()
    if find_secret_spans(normalized):
        raise GuidanceError("source_url contains credential-shaped material")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise GuidanceError(
            "source_url must be an HTTP(S) URL without embedded credentials"
        )
    return normalized


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise GuidanceError(f"{field} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise GuidanceError(f"{field} must be an RFC 3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GuidanceError(f"{field} must include a UTC offset")
    return value


def _last_success(root: Path, source_url: str) -> dict[str, Any] | None:
    path = _source_root(root, source_url) / "status.json"
    if not path.exists():
        return None
    status = _load_json(path, "guidance source status")
    _validate_observation(status)
    if status["source_url"] != source_url:
        raise GuidanceError("guidance source status URL does not match its path")
    if status["status"] == "fresh":
        return _success_reference(status)
    return status["last_success"]


def _success_reference(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_url": source["source_url"],
        "retrieved_at": source["retrieved_at"],
        "content_sha256": source["content_sha256"],
        "media_type": source["media_type"],
    }


def _validate_source_result(value: Any) -> None:
    if not isinstance(value, dict):
        raise GuidanceError("guidance source result must be an object")
    common = {"source_url", "status", "retrieved_at"}
    _source_url(value.get("source_url"))
    _timestamp(value.get("retrieved_at"), "retrieved_at")
    if value.get("status") == "fresh":
        _exact_fields(
            value,
            common | {"content_sha256", "media_type"},
            "fresh guidance source result",
        )
        _hash(value["content_sha256"], "content_sha256")
        media_type = value["media_type"]
        if media_type is not None and (
            not isinstance(media_type, str) or not media_type
        ):
            raise GuidanceError("media_type must be null or non-empty text")
        return
    if value.get("status") not in {"stale", "unavailable"}:
        raise GuidanceError("guidance source status is unsupported")
    _exact_fields(
        value,
        common | {"error_code", "last_success"},
        "failed guidance source result",
    )
    if (
        not isinstance(value["error_code"], str)
        or _SAFE_ERROR_CODE.fullmatch(value["error_code"]) is None
    ):
        raise GuidanceError("error_code is invalid")
    last_success = value["last_success"]
    if value["status"] == "stale" and last_success is None:
        raise GuidanceError("stale source requires last_success provenance")
    if value["status"] == "unavailable" and last_success is not None:
        raise GuidanceError("unavailable source cannot claim last_success")
    if last_success is not None:
        _validate_success_reference(last_success)


def _validate_success_reference(value: Any) -> None:
    _exact_fields(
        value,
        {"source_url", "retrieved_at", "content_sha256", "media_type"},
        "guidance source success reference",
    )
    _source_url(value["source_url"])
    _timestamp(value["retrieved_at"], "retrieved_at")
    _hash(value["content_sha256"], "content_sha256")
    if value["media_type"] is not None and (
        not isinstance(value["media_type"], str) or not value["media_type"]
    ):
        raise GuidanceError("media_type must be null or non-empty text")


def _validate_refresh(value: Any) -> None:
    _exact_fields(
        value,
        {
            "schema_version",
            "artifact_type",
            "refresh_id",
            "retrieved_at",
            "sources",
        },
        "guidance refresh",
    )
    _schema_and_type(value, "guidance-refresh")
    _hash(value["refresh_id"], "refresh_id")
    _timestamp(value["retrieved_at"], "retrieved_at")
    if not isinstance(value["sources"], list) or not value["sources"]:
        raise GuidanceError("guidance refresh sources must be a non-empty list")
    for source in value["sources"]:
        _validate_source_result(source)
    if [item["source_url"] for item in value["sources"]] != sorted(
        item["source_url"] for item in value["sources"]
    ):
        raise GuidanceError("guidance refresh sources must be sorted")
    body = dict(value)
    identity = body.pop("refresh_id")
    if sha256_json(body) != identity:
        raise GuidanceError("guidance refresh_id does not match its content")


def _validate_observation(value: Any) -> None:
    if not isinstance(value, dict):
        raise GuidanceError("guidance source observation must be an object")
    if value.get("schema_version") != GUIDANCE_SCHEMA_VERSION:
        raise GuidanceError("unsupported guidance schema_version")
    if value.get("artifact_type") != "guidance-source-observation":
        raise GuidanceError("unsupported guidance observation type")
    _hash(value.get("observation_id"), "observation_id")
    result = dict(value)
    for field in ("schema_version", "artifact_type", "observation_id"):
        result.pop(field)
    _validate_source_result(result)
    if sha256_json(result) != value["observation_id"]:
        raise GuidanceError("observation_id does not match its content")


def _validate_work_order(value: Any) -> None:
    _exact_fields(
        value,
        {
            "schema_version",
            "artifact_type",
            "work_order_id",
            "refresh_id",
            "privacy_contract",
            "sources",
            "private_snapshot_access",
            "instructions",
            "candidate_schema",
            "acceptance_criteria",
        },
        "guidance distillation work order",
    )
    _schema_and_type(value, "guidance-distillation-work-order")
    _hash(value["work_order_id"], "work_order_id")
    _hash(value["refresh_id"], "refresh_id")
    if not isinstance(value["privacy_contract"], str) or not value["privacy_contract"]:
        raise GuidanceError("privacy_contract must be non-empty text")
    if not isinstance(value["sources"], list) or not value["sources"]:
        raise GuidanceError("work order sources must be a non-empty list")
    for source in value["sources"]:
        _validate_work_order_source(source)
    access = value["private_snapshot_access"]
    _exact_fields(
        access,
        {"root", "mode", "resolver", "allowed_refs", "contract"},
        "private snapshot access",
    )
    if access["root"] != "guidance/objects" or access["mode"] != "read-only":
        raise GuidanceError("private snapshot access root/mode is unsupported")
    if access["resolver"] != "apu.guidance.read_guidance_work_order_snapshot":
        raise GuidanceError("private snapshot resolver is unsupported")
    if not isinstance(access["contract"], str) or not access["contract"]:
        raise GuidanceError("private snapshot access contract is required")
    expected_refs = sorted(
        {
            source["snapshot"]["ref"]
            for source in value["sources"]
            if source["snapshot"] is not None
        }
    )
    if access["allowed_refs"] != expected_refs:
        raise GuidanceError(
            "private snapshot allowed_refs must exactly match source snapshots"
        )
    if value["instructions"] != _DISTILLATION_INSTRUCTIONS:
        raise GuidanceError("distillation instructions do not match the contract")
    if value["candidate_schema"] != _CANDIDATE_SCHEMA:
        raise GuidanceError("candidate_schema does not match the contract")
    if value["acceptance_criteria"] != _DISTILLATION_ACCEPTANCE_CRITERIA:
        raise GuidanceError(
            "distillation acceptance criteria do not match the contract"
        )
    body = dict(value)
    identity = body.pop("work_order_id")
    if sha256_json(body) != identity:
        raise GuidanceError("work_order_id does not match its content")


def _validate_candidate(value: Any) -> None:
    _exact_fields(
        value,
        {
            "schema_version",
            "artifact_type",
            "work_order_id",
            "principles",
        },
        "guidance baseline candidate",
    )
    _schema_and_type(value, "guidance-baseline-candidate")
    _hash(value["work_order_id"], "work_order_id")
    _normalize_principles(value["principles"])


def _normalize_principles(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise GuidanceError("baseline principles must be a non-empty list")
    principles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        principle = _json_object(raw, "guidance principle")
        _exact_fields(
            principle,
            {"principle_id", "statement", "sources", "detector_policies"},
            "guidance principle",
        )
        principle_id = _safe_identifier(principle["principle_id"], "principle_id")
        if principle_id in seen:
            raise GuidanceError("principle_id values must be unique")
        seen.add(principle_id)
        if (
            not isinstance(principle["statement"], str)
            or not principle["statement"].strip()
        ):
            raise GuidanceError("principle statement must be non-empty text")
        sources = _normalize_citations(principle["sources"])
        policies = _normalize_policies(
            principle["detector_policies"],
            {source["content_sha256"] for source in sources},
        )
        principles.append(
            {
                "principle_id": principle_id,
                "statement": principle["statement"].strip(),
                "sources": sources,
                "detector_policies": policies,
            }
        )
    return sorted(principles, key=lambda item: item["principle_id"])


def _normalize_citations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise GuidanceError("principle sources must be a non-empty list")
    citations = []
    identities: set[tuple[str, str, str]] = set()
    for raw in value:
        citation = _json_object(raw, "principle source")
        _exact_fields(
            citation,
            {"source_url", "retrieved_at", "content_sha256"},
            "principle source",
        )
        normalized = {
            "source_url": _source_url(citation["source_url"]),
            "retrieved_at": _timestamp(citation["retrieved_at"], "retrieved_at"),
            "content_sha256": _hash(citation["content_sha256"], "content_sha256"),
        }
        identity = (
            normalized["source_url"],
            normalized["retrieved_at"],
            normalized["content_sha256"],
        )
        if identity in identities:
            raise GuidanceError("principle sources must be unique")
        identities.add(identity)
        citations.append(normalized)
    return sorted(
        citations,
        key=lambda item: (
            item["source_url"],
            item["retrieved_at"],
            item["content_sha256"],
        ),
    )


def _normalize_policies(
    value: Any,
    cited_hashes: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise GuidanceError("detector_policies must be a non-empty list")
    policies = []
    seen: set[tuple[str, str]] = set()
    for raw in value:
        policy = _json_object(raw, "detector policy")
        _exact_fields(
            policy,
            {
                "detector_id",
                "setting",
                "value",
                "justification",
                "source_sha256s",
            },
            "detector policy",
        )
        detector_id = _safe_identifier(policy["detector_id"], "detector_id")
        setting = _safe_identifier(policy["setting"], "setting")
        key = (detector_id, setting)
        if key in seen:
            raise GuidanceError("detector policy keys must be unique per principle")
        seen.add(key)
        typed_value = _normalize_policy_value(
            detector_id,
            setting,
            policy["value"],
        )
        if (
            not isinstance(policy["justification"], str)
            or not policy["justification"].strip()
        ):
            raise GuidanceError("detector justification must be non-empty text")
        raw_hashes = policy["source_sha256s"]
        if not isinstance(raw_hashes, list) or not raw_hashes:
            raise GuidanceError("source_sha256s must be a non-empty list")
        hashes = [_hash(item, "source_sha256s[]") for item in raw_hashes]
        if len(set(hashes)) != len(hashes):
            raise GuidanceError("source_sha256s must be unique")
        if not set(hashes) <= cited_hashes:
            raise GuidanceError(
                "detector policy justification must use cited source hashes"
            )
        policies.append(
            {
                "detector_id": detector_id,
                "setting": setting,
                "value": typed_value,
                "justification": policy["justification"].strip(),
                "source_sha256s": sorted(hashes),
            }
        )
    return sorted(
        policies,
        key=lambda item: (item["detector_id"], item["setting"]),
    )


def _normalize_policy_value(
    detector_id: str,
    setting: str,
    value: Any,
) -> bool | int:
    policy_type = _POLICY_ALLOWLIST.get((detector_id, setting))
    if policy_type is None:
        raise GuidanceError(
            f"detector policy is not allowlisted: {detector_id}/{setting}"
        )
    if policy_type == "integer:2..100":
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 2 <= value <= 100
        ):
            raise GuidanceError(
                f"{detector_id}/{setting} must be an integer between 2 and 100"
            )
        return value
    if policy_type == "boolean":
        if not isinstance(value, bool):
            raise GuidanceError(f"{detector_id}/{setting} must be boolean")
        return value
    raise AssertionError(f"unsupported internal detector policy type: {policy_type}")


def _detector_policy_from_principles(
    principles: Sequence[Mapping[str, Any]],
) -> DetectorPolicy:
    values: dict[tuple[str, str], bool | int] = {}
    for principle in principles:
        for policy in principle["detector_policies"]:
            key = (policy["detector_id"], policy["setting"])
            existing = values.get(key)
            if existing is not None and existing != policy["value"]:
                raise GuidanceError(
                    "adopted baseline contains conflicting detector policy "
                    f"values for {key[0]}/{key[1]}"
                )
            values[key] = policy["value"]
    return DetectorPolicy(
        duplicate_instruction_minimum_words=int(
            values.get(
                ("duplicate-instruction", "minimum_words"),
                6,
            )
        ),
        speculative_skill_threshold_enabled=bool(
            values.get(
                (
                    "universal-skill-trigger",
                    "speculative_threshold_enabled",
                ),
                True,
            )
        ),
    )


def _semantic_principles(
    principles: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    semantic: list[dict[str, Any]] = []
    for principle in principles:
        semantic.append(
            {
                "principle_id": principle["principle_id"],
                "statement": principle["statement"],
                "sources": sorted(
                    (
                        {
                            "source_url": source["source_url"],
                            "content_sha256": source["content_sha256"],
                        }
                        for source in principle["sources"]
                    ),
                    key=lambda source: (
                        source["source_url"],
                        source["content_sha256"],
                    ),
                ),
                "detector_policies": [
                    dict(policy) for policy in principle["detector_policies"]
                ],
            }
        )
    return sorted(semantic, key=lambda item: item["principle_id"])


def _semantic_baseline_version(
    principles: Sequence[Mapping[str, Any]],
) -> str:
    return sha256_json(
        {
            "schema_version": GUIDANCE_SCHEMA_VERSION,
            "principles": _semantic_principles(principles),
        }
    )


def _validate_approval(value: Any) -> None:
    _exact_fields(
        value,
        {"status", "reviewer", "reviewed_at"},
        "guidance baseline approval",
    )
    if value["status"] != "approved":
        raise GuidanceError("guidance baseline requires approved review")
    _safe_identifier(value["reviewer"], "reviewer")
    _timestamp(value["reviewed_at"], "reviewed_at")


def _validate_baseline(value: Any) -> None:
    _exact_fields(
        value,
        {
            "schema_version",
            "artifact_type",
            "baseline_version",
            "adopted_at",
            "work_order_id",
            "review",
            "principles",
        },
        "guidance baseline",
    )
    _schema_and_type(value, "guidance-baseline")
    _hash(value["baseline_version"], "baseline_version")
    _hash(value["work_order_id"], "work_order_id")
    _timestamp(value["adopted_at"], "adopted_at")
    _validate_approval(value["review"])
    normalized = _normalize_principles(value["principles"])
    if normalized != value["principles"]:
        raise GuidanceError("guidance baseline principles are not canonical")
    _detector_policy_from_principles(normalized)
    expected = _semantic_baseline_version(normalized)
    if expected != value["baseline_version"]:
        raise GuidanceError("baseline_version does not match its principles")


def _has_successful_observation(
    root: Path,
    citation: Mapping[str, Any],
) -> bool:
    observations = _source_root(root, citation["source_url"]) / "observations"
    if not observations.exists():
        return False
    for path in observations.glob("*.json"):
        observation = _load_json(path, "guidance source observation")
        _validate_observation(observation)
        if (
            observation["status"] == "fresh"
            and observation["source_url"] == citation["source_url"]
            and observation["retrieved_at"] == citation["retrieved_at"]
            and observation["content_sha256"] == citation["content_sha256"]
            and _object_matches_hash(root, citation["content_sha256"])
        ):
            return True
    return False


def _validate_work_order_source(value: Any) -> None:
    if not isinstance(value, dict):
        raise GuidanceError("work order source must be an object")
    common = {"source_url", "status", "retrieved_at", "snapshot"}
    _exact_fields(value, common, "work order source")
    _source_url(value.get("source_url"))
    _timestamp(value.get("retrieved_at"), "retrieved_at")
    if value.get("status") == "fresh":
        _validate_work_order_snapshot(value["snapshot"])
        if (
            value["snapshot"]["source_url"] != value["source_url"]
            or value["snapshot"]["retrieved_at"] != value["retrieved_at"]
        ):
            raise GuidanceError(
                "fresh work order snapshot provenance does not match its source"
            )
        return
    if value.get("status") not in {"stale", "unavailable"}:
        raise GuidanceError("work order source status is unsupported")
    if value["status"] == "stale":
        _validate_work_order_snapshot(value["snapshot"])
        if value["snapshot"]["source_url"] != value["source_url"]:
            raise GuidanceError("stale work order snapshot URL mismatch")
    elif value["snapshot"] is not None:
        raise GuidanceError("unavailable work order source cannot have a snapshot")


def _work_order_citations(
    work_order: Mapping[str, Any],
) -> set[tuple[str, str, str]]:
    citations: set[tuple[str, str, str]] = set()
    for source in work_order["sources"]:
        if source["snapshot"] is not None:
            snapshot = source["snapshot"]
            citations.add(
                (
                    snapshot["source_url"],
                    snapshot["retrieved_at"],
                    snapshot["content_sha256"],
                )
            )
    return citations


def _work_order_snapshot_reference(
    source: Mapping[str, Any],
) -> dict[str, str]:
    content_sha256 = source["content_sha256"]
    return {
        "ref": f"guidance/objects/{content_sha256}.bin",
        "source_url": source["source_url"],
        "retrieved_at": source["retrieved_at"],
        "content_sha256": content_sha256,
    }


def _validate_work_order_snapshot(value: Any) -> None:
    _exact_fields(
        value,
        {"ref", "source_url", "retrieved_at", "content_sha256"},
        "work order snapshot",
    )
    _source_url(value["source_url"])
    _timestamp(value["retrieved_at"], "retrieved_at")
    content_sha256 = _hash(value["content_sha256"], "content_sha256")
    if value["ref"] != f"guidance/objects/{content_sha256}.bin":
        raise GuidanceError("work order snapshot ref does not match its hash")


def _object_matches_hash(root: Path, content_sha256: str) -> bool:
    path = root / "objects" / f"{content_sha256}.bin"
    if not path.exists():
        return False
    return sha256_bytes(path.read_bytes()) == content_sha256


def _schema_and_type(value: Mapping[str, Any], artifact_type: str) -> None:
    if value.get("schema_version") != GUIDANCE_SCHEMA_VERSION:
        raise GuidanceError("unsupported guidance schema_version")
    if value.get("artifact_type") != artifact_type:
        raise GuidanceError(f"expected artifact_type {artifact_type}")


def _exact_fields(value: Any, fields: set[str], name: str) -> None:
    if not isinstance(value, Mapping):
        raise GuidanceError(f"{name} must be an object")
    if set(value) != fields:
        raise GuidanceError(
            f"{name} fields must be exactly: {', '.join(sorted(fields))}"
        )


def _safe_identifier(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise GuidanceError(f"{field} must be one safe non-empty identifier")
    return value


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise GuidanceError(f"{field} must be a lowercase SHA-256")
    return value


def _json_object(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    try:
        copied = json.loads(canonical_json(value))
    except (TypeError, ValueError) as error:
        raise GuidanceError(f"{name} must contain only JSON values") from error
    if not isinstance(copied, dict):
        raise GuidanceError(f"{name} must be an object")
    return copied


def _load_json(path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GuidanceError(f"invalid {name} at {path}: {error}") from error
    if not isinstance(value, dict):
        raise GuidanceError(f"{name} must be an object")
    return value


def _write_json_once(path: Path, value: Mapping[str, Any]) -> dict[str, Any]:
    encoded = canonical_json(value).encode("utf-8")
    ensure_private_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _load_json(path, "immutable guidance artifact")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return json.loads(encoded)


def _write_bytes_once(path: Path, content: bytes) -> None:
    ensure_private_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != content:
            raise GuidanceError(
                f"content-addressed guidance object collision at {path}"
            )
        return
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise

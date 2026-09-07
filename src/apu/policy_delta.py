"""Immutable, review-only intake for AETA policy-delta proposals."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Self
from urllib.parse import urlsplit

from apu.models import canonical_json, sha256_json
from apu.state import ensure_private_directory, ensure_state_home, write_json_atomic
from apu.work_orders import find_secret_spans

MAX_PROPOSAL_BYTES = 1024 * 1024
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_THREAD_ID = re.compile(r"plir:thread:[0-9A-HJKMNP-TV-Z]{26}\Z")
_CHECKPOINT_REF = re.compile(r"thread-checkpoint:[0-9A-HJKMNP-TV-Z]{26}\Z")
_SAFE_NAME = re.compile(r"[a-z][a-z0-9._-]{0,127}\Z")
_AETA_REF = re.compile(r"aeta:(?:snapshot|run):[0-9a-f]{64}\Z")


class PolicyDeltaIntakeError(ValueError):
    """A policy-delta proposal or its immutable intake state is invalid."""


class _PolicyDeltaLock(AbstractContextManager["_PolicyDeltaLock"]):
    def __init__(self, state_home: Path, proposal_id: str) -> None:
        root = ensure_state_home(state_home)
        lock_root = ensure_private_directory(root / "policy-delta-intake" / "locks")
        self.path = lock_root / f"{proposal_id.removeprefix('sha256:')}.lock"
        self._stream = None

    def __enter__(self) -> Self:
        self._stream = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                self._stream.write(b"\0")
                self._stream.flush()
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self._stream.close()
            self._stream = None
            raise PolicyDeltaIntakeError("policy-delta proposal is already being ingested") from exc
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._stream is None:
            return
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None


def _json_object(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    try:
        copied = json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise PolicyDeltaIntakeError(f"{label} must contain only JSON values") from exc
    if not isinstance(copied, dict):
        raise PolicyDeltaIntakeError(f"{label} must be an object")
    if len(canonical_json(copied).encode("utf-8")) > MAX_PROPOSAL_BYTES:
        raise PolicyDeltaIntakeError(f"{label} exceeds {MAX_PROPOSAL_BYTES} bytes")
    return copied


def _exact(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise PolicyDeltaIntakeError(f"{label} fields are invalid")


def _text(value: object, label: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise PolicyDeltaIntakeError(f"{label} must be non-empty text")
    result = value.strip()
    if len(result.encode("utf-8")) > maximum:
        raise PolicyDeltaIntakeError(f"{label} exceeds {maximum} bytes")
    return result


def _hash(value: object, label: str) -> str:
    result = _text(value, label, maximum=71)
    if _HASH.fullmatch(result) is None:
        raise PolicyDeltaIntakeError(f"{label} must be a SHA-256 reference")
    return result


def _sorted_refs(value: object, label: str, *, maximum: int) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > maximum
        or not all(isinstance(item, str) for item in value)
    ):
        raise PolicyDeltaIntakeError(f"{label} must be a bounded non-empty list")
    refs = [_text(item, label) for item in value]
    if refs != sorted(set(refs)):
        raise PolicyDeltaIntakeError(f"{label} must be unique and canonically sorted")
    return refs


def _validate_thread_ref(value: object) -> None:
    if not isinstance(value, dict):
        raise PolicyDeltaIntakeError("thread_ref must be an object")
    _exact(value, {"thread_id", "checkpoint_ref"}, "thread_ref")
    if _THREAD_ID.fullmatch(_text(value["thread_id"], "thread_id", maximum=64)) is None:
        raise PolicyDeltaIntakeError("thread_id is invalid")
    if _CHECKPOINT_REF.fullmatch(
        _text(value["checkpoint_ref"], "checkpoint_ref", maximum=80)
    ) is None:
        raise PolicyDeltaIntakeError("checkpoint_ref is invalid")


def _validate_source(value: object) -> None:
    if not isinstance(value, dict):
        raise PolicyDeltaIntakeError("source must be an object")
    _exact(
        value,
        {
            "repository_ref",
            "revision",
            "path",
            "content_sha256",
            "snapshot_ref",
            "freshness",
        },
        "source",
    )
    repository_ref = _text(value["repository_ref"], "repository_ref")
    parsed = urlsplit(repository_ref)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise PolicyDeltaIntakeError("repository_ref must be a credential-free HTTPS URL")
    revision = _text(value["revision"], "source revision", maximum=64)
    if _GIT_OID.fullmatch(revision) is None:
        raise PolicyDeltaIntakeError("source revision must be an exact Git object ID")
    source_path = _text(value["path"], "source path")
    pure_path = PurePosixPath(source_path)
    if (
        "\\" in source_path
        or pure_path.is_absolute()
        or any(part in {"", ".", ".."} for part in pure_path.parts)
    ):
        raise PolicyDeltaIntakeError("source path must be repository-relative POSIX syntax")
    _hash(value["content_sha256"], "source content_sha256")
    snapshot_ref = _text(value["snapshot_ref"], "snapshot_ref", maximum=78)
    if _AETA_REF.fullmatch(snapshot_ref) is None or not snapshot_ref.startswith(
        "aeta:snapshot:"
    ):
        raise PolicyDeltaIntakeError("snapshot_ref is invalid")
    freshness = value["freshness"]
    if not isinstance(freshness, dict):
        raise PolicyDeltaIntakeError("source freshness must be an object")
    _exact(freshness, {"kind", "status"}, "source freshness")
    if freshness != {"kind": "immutable_git", "status": "fresh"}:
        raise PolicyDeltaIntakeError("source freshness is unsupported")


def _validate_target(value: object) -> None:
    if not isinstance(value, dict):
        raise PolicyDeltaIntakeError("target must be an object")
    _exact(value, {"owner", "surface", "expected_base_version"}, "target")
    if value["owner"] != "apu":
        raise PolicyDeltaIntakeError("target owner must be apu")
    surface = _text(value["surface"], "target surface", maximum=128)
    if _SAFE_NAME.fullmatch(surface) is None:
        raise PolicyDeltaIntakeError("target surface is invalid")
    base = value["expected_base_version"]
    if base is not None:
        _text(base, "expected_base_version", maximum=128)


def _validate_rule_operations(value: object) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise PolicyDeltaIntakeError("rule_operations must contain 1 to 32 entries")
    rule_ids: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            raise PolicyDeltaIntakeError("rule operation must be an object")
        _exact(item, {"rule_id", "action", "summary", "evidence_refs"}, "rule operation")
        rule_id = _text(item["rule_id"], "rule_id", maximum=128)
        if _SAFE_NAME.fullmatch(rule_id) is None:
            raise PolicyDeltaIntakeError("rule_id is invalid")
        rule_ids.append(rule_id)
        if item["action"] not in {"add", "replace", "remove"}:
            raise PolicyDeltaIntakeError("rule action is invalid")
        _text(item["summary"], "rule summary", maximum=1000)
        _sorted_refs(item["evidence_refs"], "rule evidence_refs", maximum=16)
    if rule_ids != sorted(set(rule_ids)):
        raise PolicyDeltaIntakeError("rule operations must have unique sorted rule_id values")


def validate_policy_delta_proposal(value: Mapping[str, Any]) -> dict[str, Any]:
    proposal = _json_object(value, "policy-delta proposal")
    _exact(
        proposal,
        {
            "schema_version",
            "record_type",
            "thread_ref",
            "source",
            "aeta_run_ref",
            "target",
            "evidence_refs",
            "rule_operations",
            "requires_review",
            "promotion_authorized",
            "proposal_id",
        },
        "policy-delta proposal",
    )
    if proposal["schema_version"] != 1:
        raise PolicyDeltaIntakeError("policy-delta schema_version must be 1")
    if proposal["record_type"] != "aeta_apu_policy_delta_proposal":
        raise PolicyDeltaIntakeError("policy-delta record_type is unsupported")
    _validate_thread_ref(proposal["thread_ref"])
    _validate_source(proposal["source"])
    aeta_run_ref = _text(proposal["aeta_run_ref"], "aeta_run_ref", maximum=73)
    if _AETA_REF.fullmatch(aeta_run_ref) is None or not aeta_run_ref.startswith("aeta:run:"):
        raise PolicyDeltaIntakeError("aeta_run_ref is invalid")
    _validate_target(proposal["target"])
    _sorted_refs(proposal["evidence_refs"], "evidence_refs", maximum=32)
    _validate_rule_operations(proposal["rule_operations"])
    if proposal["requires_review"] is not True:
        raise PolicyDeltaIntakeError("requires_review must be true")
    if proposal["promotion_authorized"] is not False:
        raise PolicyDeltaIntakeError("promotion_authorized must be false")
    proposal_id = _hash(proposal["proposal_id"], "proposal_id")
    body = {key: item for key, item in proposal.items() if key != "proposal_id"}
    if proposal_id != f"sha256:{sha256_json(body)}":
        raise PolicyDeltaIntakeError("proposal_id does not match proposal content")
    if find_secret_spans(canonical_json(proposal)):
        raise PolicyDeltaIntakeError("policy-delta proposal contains credential-shaped material")
    return proposal


def _timestamp(value: str) -> str:
    timestamp = _text(value, "received_at", maximum=40)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyDeltaIntakeError("received_at must be an ISO-8601 timestamp") from exc
    if not timestamp.endswith("Z") or parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PolicyDeltaIntakeError("received_at must be UTC with a Z suffix")
    return timestamp


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyDeltaIntakeError(f"invalid {label} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PolicyDeltaIntakeError(f"{label} must be an object")
    return value


def _validate_intake_receipt(
    value: Mapping[str, Any],
    *,
    proposal: Mapping[str, Any],
    proposal_hash: str,
) -> dict[str, Any]:
    receipt = _json_object(value, "policy-delta intake receipt")
    _exact(
        receipt,
        {
            "schema_version",
            "record_type",
            "proposal_ref",
            "candidate_ref",
            "target_surface",
            "status",
            "requires_review",
            "promotion_authorized",
            "received_at",
            "receipt_id",
        },
        "policy-delta intake receipt",
    )
    proposal_id = str(proposal["proposal_id"])
    key = proposal_id.removeprefix("sha256:")
    fixed = {
        "schema_version": 1,
        "record_type": "apu_policy_delta_intake_receipt",
        "proposal_ref": {"id": proposal_id, "hash": proposal_hash},
        "candidate_ref": f"apu:policy-delta-candidate:{key}",
        "target_surface": proposal["target"]["surface"],
        "status": "pending_review",
        "requires_review": True,
        "promotion_authorized": False,
    }
    for name, expected in fixed.items():
        if receipt[name] != expected:
            raise PolicyDeltaIntakeError(
                f"policy-delta intake receipt {name} does not match the proposal"
            )
    _timestamp(receipt["received_at"])
    receipt_id = _hash(receipt["receipt_id"], "policy-delta intake receipt receipt_id")
    body = {name: item for name, item in receipt.items() if name != "receipt_id"}
    if receipt_id != f"sha256:{sha256_json(body)}":
        raise PolicyDeltaIntakeError("policy-delta intake receipt identity is invalid")
    return receipt


def ingest_policy_delta(
    state_home: str | Path,
    proposal: Mapping[str, Any],
    *,
    received_at: str,
) -> dict[str, Any]:
    """Store one immutable proposal and a pending-review receipt; never promote it."""
    state_home = Path(state_home).expanduser().resolve()
    normalized = validate_policy_delta_proposal(proposal)
    proposal_id = str(normalized["proposal_id"])
    proposal_hash = f"sha256:{sha256_json(normalized)}"
    key = proposal_id.removeprefix("sha256:")
    root = state_home / "policy-delta-intake"
    candidate_path = root / "candidates" / f"{key}.json"
    receipt_path = root / "receipts" / f"{key}.json"

    with _PolicyDeltaLock(state_home, proposal_id):
        if candidate_path.is_file():
            existing = _load_json(candidate_path, "policy-delta candidate")
            if existing != normalized:
                raise PolicyDeltaIntakeError("proposal_id is already bound to different content")
            if receipt_path.is_file():
                return _validate_intake_receipt(
                    _load_json(receipt_path, "policy-delta intake receipt"),
                    proposal=normalized,
                    proposal_hash=proposal_hash,
                )
        else:
            write_json_atomic(candidate_path, normalized)

        body = {
            "schema_version": 1,
            "record_type": "apu_policy_delta_intake_receipt",
            "proposal_ref": {"id": proposal_id, "hash": proposal_hash},
            "candidate_ref": f"apu:policy-delta-candidate:{key}",
            "target_surface": normalized["target"]["surface"],
            "status": "pending_review",
            "requires_review": True,
            "promotion_authorized": False,
            "received_at": _timestamp(received_at),
        }
        receipt = {**body, "receipt_id": f"sha256:{sha256_json(body)}"}
        write_json_atomic(receipt_path, receipt)
        return receipt

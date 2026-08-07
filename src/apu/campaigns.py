from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from .models import canonical_json
from .state import ensure_private_directory, write_json_atomic

CAMPAIGN_SCHEMA_VERSION = 1
CAMPAIGN_MANIFEST_SCHEMA_VERSION = 2
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXECUTION_ARTIFACT_TYPES = frozenset(
    {"receipt", "rollback", "work-order-result"}
)


class CampaignError(RuntimeError):
    """Base class for campaign persistence failures."""


class CampaignExistsError(CampaignError):
    """Raised when an immutable campaign object would be replaced."""


class StaleCampaignRevisionError(CampaignError):
    """Raised when a writer's expected campaign revision is no longer current."""


class CampaignLockedError(CampaignError):
    """Raised when another process holds a campaign's mutation lock."""

    def __init__(self, campaign_id: str, holder: Mapping[str, Any] | None) -> None:
        self.campaign_id = campaign_id
        self.holder = dict(holder) if holder is not None else None
        if self.holder:
            identity = (
                f"pid={self.holder.get('pid', 'unknown')} "
                f"purpose={self.holder.get('purpose', 'unknown')}"
            )
        else:
            identity = "holder identity unavailable"
        super().__init__(f"campaign {campaign_id} is locked ({identity})")


def campaign_directory(state_home: Path, campaign_id: str) -> Path:
    """Return the private directory assigned to one campaign."""

    _validate_component(campaign_id, "campaign_id")
    return Path(state_home) / "campaigns" / campaign_id


def build_campaign_manifest(
    *,
    campaign_id: str,
    inventory_hash: str,
    profile_hash: str,
    baseline_version: str,
    model_generation: str,
    plan_binding: Mapping[str, Any] | str,
    work_order_bindings: list[Mapping[str, Any] | str] | tuple[
        Mapping[str, Any] | str, ...
    ],
    evaluation_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a validated immutable campaign manifest."""

    manifest: dict[str, Any] = {
        "schema_version": (
            CAMPAIGN_MANIFEST_SCHEMA_VERSION
            if evaluation_context is not None
            else CAMPAIGN_SCHEMA_VERSION
        ),
        "campaign_id": campaign_id,
        "inventory_hash": inventory_hash,
        "profile_hash": profile_hash,
        "baseline_version": baseline_version,
        "model_generation": model_generation,
        "plan_binding": _json_copy(plan_binding),
        "work_order_bindings": _json_copy(list(work_order_bindings)),
    }
    if evaluation_context is not None:
        manifest["evaluation_context"] = _json_copy(evaluation_context)
    _validate_manifest(manifest)
    return manifest


def create_campaign(
    state_home: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist a campaign's immutable manifest and initial rebuildable index.

    An exact retry is idempotent. A different manifest for an existing
    campaign id is rejected without modifying the original.
    """

    stored_manifest = _json_copy(manifest)
    _validate_manifest(stored_manifest)
    campaign_id = stored_manifest["campaign_id"]
    root = _ensure_campaign_layout(state_home, campaign_id)
    manifest_path = root / "manifest.json"
    existing = _write_json_once(manifest_path, stored_manifest)
    if existing != stored_manifest:
        raise CampaignExistsError(
            f"campaign {campaign_id} already has a different immutable manifest"
        )

    try:
        return load_campaign_index(state_home, campaign_id)
    except FileNotFoundError:
        index = _index_from_artifacts(campaign_id, [], revision=0)
        _write_revision_once(root, index)
        write_json_atomic(root / "index.json", index)
        return index


def load_campaign_manifest(
    state_home: Path,
    campaign_id: str,
) -> dict[str, Any]:
    """Load and validate one campaign's immutable manifest."""

    path = campaign_directory(state_home, campaign_id) / "manifest.json"
    value = _load_json(path, "campaign manifest")
    _validate_manifest(value)
    if value["campaign_id"] != campaign_id:
        raise ValueError("campaign manifest id does not match its directory")
    return value


def load_campaign_index(
    state_home: Path,
    campaign_id: str,
) -> dict[str, Any]:
    """Load the highest durable revision of a campaign's progress index."""

    root = campaign_directory(state_home, campaign_id)
    revision_root = root / "revisions"
    candidates: list[tuple[int, Path]] = []
    if revision_root.exists():
        for path in revision_root.glob("*.json"):
            try:
                revision = int(path.stem)
            except ValueError:
                continue
            if revision >= 0:
                candidates.append((revision, path))

    if candidates:
        _, path = max(candidates, key=lambda item: item[0])
    else:
        path = root / "index.json"
        if not path.exists():
            raise FileNotFoundError(f"campaign index does not exist: {path}")

    value = _load_json(path, "campaign index")
    _validate_index(value, campaign_id)
    return value


def leaf_artifact_path(
    state_home: Path,
    campaign_id: str,
    artifact_type: str,
    artifact_id: str,
) -> Path:
    """Return the canonical path of a self-describing campaign leaf."""

    _validate_component(campaign_id, "campaign_id")
    _validate_component(artifact_type, "artifact_type")
    _validate_component(artifact_id, "artifact_id")
    return (
        campaign_directory(state_home, campaign_id)
        / "artifacts"
        / artifact_type
        / f"{artifact_id}.json"
    )


def register_leaf_artifact(
    state_home: Path,
    campaign_id: str,
    artifact: Mapping[str, Any],
) -> Path:
    """Write one canonical leaf before it is referenced by the mutable index.

    Exact retries are idempotent. Reusing the same artifact identity with
    different content is an immutable collision.
    """

    stored = _json_copy(artifact)
    _validate_artifact(stored)
    if stored["campaign_id"] != campaign_id:
        raise ValueError("artifact campaign_id does not match the campaign")
    load_campaign_manifest(state_home, campaign_id)
    path = leaf_artifact_path(
        state_home,
        campaign_id,
        stored["artifact_type"],
        stored["artifact_id"],
    )
    ensure_private_directory(path.parent)
    existing = _write_json_once(path, stored)
    if existing != stored:
        raise CampaignExistsError(
            "campaign leaf identity is already bound to different content"
        )
    return path


def scan_leaf_artifacts(
    state_home: Path,
    campaign_id: str,
) -> list[dict[str, Any]]:
    """Load all valid self-describing leaves from the campaign directory."""

    root = campaign_directory(state_home, campaign_id) / "artifacts"
    if not root.exists():
        return []
    artifacts: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.json")):
        value = _load_json(path, "campaign leaf artifact")
        _validate_artifact(value)
        if value["campaign_id"] != campaign_id:
            raise ValueError(f"campaign leaf has mismatched campaign_id: {path}")
        expected = leaf_artifact_path(
            state_home,
            campaign_id,
            value["artifact_type"],
            value["artifact_id"],
        )
        if path.resolve() != expected.resolve():
            raise ValueError(f"campaign leaf is not at its canonical path: {path}")
        identity = (value["artifact_type"], value["artifact_id"])
        if identity in identities:
            raise ValueError(f"duplicate campaign leaf identity: {identity}")
        identities.add(identity)
        artifacts.append(value)
    return artifacts


def reconstruct_campaign_index(
    state_home: Path,
    campaign_id: str,
    *,
    revision: int = 0,
) -> dict[str, Any]:
    """Reconstruct an index solely from the manifest and canonical leaves."""

    load_campaign_manifest(state_home, campaign_id)
    artifacts = scan_leaf_artifacts(state_home, campaign_id)
    return _index_from_artifacts(campaign_id, artifacts, revision=revision)


def reconcile_campaign(
    state_home: Path,
    campaign_id: str,
    *,
    expected_revision: int,
    purpose: str = "reconcile",
) -> dict[str, Any]:
    """Attach orphan leaves with a revision-checked, atomic index update."""

    with CampaignLock(state_home, campaign_id, purpose=purpose):
        return reconcile_campaign_locked(
            state_home,
            campaign_id,
            expected_revision=expected_revision,
        )


def reconcile_campaign_locked(
    state_home: Path,
    campaign_id: str,
    *,
    expected_revision: int,
) -> dict[str, Any]:
    """Attach orphan leaves while the caller holds ``CampaignLock``."""

    current = load_campaign_index(state_home, campaign_id)
    if current["revision"] != expected_revision:
        raise StaleCampaignRevisionError(
            f"campaign {campaign_id} revision is {current['revision']}; "
            f"writer expected {expected_revision}"
        )
    desired = reconstruct_campaign_index(
        state_home,
        campaign_id,
        revision=current["revision"],
    )
    if _without_revision(desired) == _without_revision(current):
        return current

    desired["revision"] = current["revision"] + 1
    root = campaign_directory(state_home, campaign_id)
    _write_revision_once(root, desired)
    write_json_atomic(root / "index.json", desired)
    return desired


def rebuild_campaign_index(
    state_home: Path,
    campaign_id: str,
    *,
    purpose: str = "rebuild-index",
) -> dict[str, Any]:
    """Recreate a missing progress index from immutable campaign evidence."""

    with CampaignLock(state_home, campaign_id, purpose=purpose):
        return rebuild_campaign_index_locked(state_home, campaign_id)


def rebuild_campaign_index_locked(
    state_home: Path,
    campaign_id: str,
) -> dict[str, Any]:
    """Rebuild the mutable index while the caller holds ``CampaignLock``."""

    try:
        current = load_campaign_index(state_home, campaign_id)
    except FileNotFoundError:
        current = None
    revision = 0 if current is None else current["revision"] + 1
    rebuilt = reconstruct_campaign_index(
        state_home,
        campaign_id,
        revision=revision,
    )
    root = campaign_directory(state_home, campaign_id)
    _write_revision_once(root, rebuilt)
    write_json_atomic(root / "index.json", rebuilt)
    return rebuilt


def read_campaign_lock_status(
    state_home: Path,
    campaign_id: str,
) -> dict[str, Any] | None:
    """Read diagnostic lock metadata; it is not a liveness determination."""

    path = campaign_directory(state_home, campaign_id) / "lock.json"
    if not path.exists():
        return None
    value = _load_json(path, "campaign lock metadata")
    if not isinstance(value, dict):
        raise TypeError("campaign lock metadata must be an object")
    return value


class CampaignLock(AbstractContextManager["CampaignLock"]):
    """Portable, fail-fast campaign lock backed by an OS-released handle."""

    def __init__(
        self,
        state_home: Path,
        campaign_id: str,
        *,
        purpose: str,
    ) -> None:
        _validate_component(campaign_id, "campaign_id")
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError("lock purpose must be a non-empty string")
        self.state_home = Path(state_home)
        self.campaign_id = campaign_id
        self.purpose = purpose.strip()
        self._stream: Any = None
        self.metadata: dict[str, Any] | None = None
        self.recovered_stale_holder: dict[str, Any] | None = None

    def __enter__(self) -> Self:
        root = campaign_directory(self.state_home, self.campaign_id)
        ensure_private_directory(root)
        handle_path = root / "lock.handle"
        metadata_path = root / "lock.json"
        stream = handle_path.open("a+b")
        if os.name == "posix":
            handle_path.chmod(0o600)
        if handle_path.stat().st_size == 0:
            stream.write(b" ")
            stream.flush()
        try:
            _lock_stream(stream)
        except OSError as error:
            stream.close()
            holder = _read_lock_metadata_tolerant(metadata_path)
            raise CampaignLockedError(self.campaign_id, holder) from error

        previous = _read_lock_metadata_tolerant(metadata_path)
        stale = (
            previous
            if previous is not None and previous.get("released_at") is None
            else None
        )
        metadata: dict[str, Any] = {
            "schema_version": CAMPAIGN_SCHEMA_VERSION,
            "campaign_id": self.campaign_id,
            "pid": os.getpid(),
            "purpose": self.purpose,
            "acquired_at": _utc_now(),
            "released_at": None,
        }
        if stale is not None:
            metadata["recovered_stale_holder"] = stale
        write_json_atomic(metadata_path, metadata)
        self._stream = stream
        self.metadata = metadata
        self.recovered_stale_holder = stale
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            metadata = dict(self.metadata or {})
            metadata["released_at"] = _utc_now()
            write_json_atomic(
                campaign_directory(self.state_home, self.campaign_id) / "lock.json",
                metadata,
            )
            self.metadata = metadata
        finally:
            try:
                _unlock_stream(stream)
            finally:
                stream.close()
                self._stream = None


def _ensure_campaign_layout(state_home: Path, campaign_id: str) -> Path:
    root = campaign_directory(state_home, campaign_id)
    ensure_private_directory(Path(state_home))
    ensure_private_directory(Path(state_home) / "campaigns")
    ensure_private_directory(root)
    ensure_private_directory(root / "artifacts")
    ensure_private_directory(root / "revisions")
    return root


def _write_revision_once(root: Path, index: Mapping[str, Any]) -> None:
    revision = index["revision"]
    path = root / "revisions" / f"{revision:020d}.json"
    existing = _write_json_once(path, index)
    if existing != index:
        raise StaleCampaignRevisionError(
            f"campaign revision {revision} was already claimed by another writer"
        )


def _write_json_once(path: Path, value: Any) -> Any:
    ensure_private_directory(path.parent)
    encoded = canonical_json(value).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _load_json(path, "immutable campaign object")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return _json_copy(value)


def _index_from_artifacts(
    campaign_id: str,
    artifacts: list[Mapping[str, Any]],
    *,
    revision: int,
) -> dict[str, Any]:
    references = [_artifact_reference(artifact) for artifact in artifacts]
    references.sort(key=lambda value: (value["artifact_type"], value["artifact_id"]))
    snapshots = {
        artifact["snapshot_id"]
        for artifact in artifacts
        if artifact.get("snapshot_id") is not None
    }
    if len(snapshots) > 1:
        raise ValueError(
            f"campaign {campaign_id} artifacts refer to multiple snapshot_ids"
        )
    snapshot_id = next(iter(snapshots), None)
    return {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "revision": revision,
        "snapshot_id": snapshot_id,
        "artifacts": references,
    }


def _artifact_reference(artifact: Mapping[str, Any]) -> dict[str, Any]:
    reference = {
        "artifact_type": artifact["artifact_type"],
        "artifact_id": artifact["artifact_id"],
        "snapshot_id": artifact.get("snapshot_id"),
    }
    if "idempotency_key" in artifact:
        reference["idempotency_key"] = _json_copy(artifact["idempotency_key"])
    return reference


def _without_revision(index: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(index)
    value.pop("revision", None)
    return value


def _validate_manifest(value: Any) -> None:
    if not isinstance(value, dict):
        raise TypeError("campaign manifest must be a JSON object")
    required = {
        "schema_version",
        "campaign_id",
        "inventory_hash",
        "profile_hash",
        "baseline_version",
        "model_generation",
        "plan_binding",
        "work_order_bindings",
    }
    schema_version = value.get("schema_version")
    if schema_version == CAMPAIGN_MANIFEST_SCHEMA_VERSION:
        required.add("evaluation_context")
    if set(value) != required:
        raise ValueError(
            "campaign manifest fields must be exactly: "
            + ", ".join(sorted(required))
        )
    if schema_version not in {
        CAMPAIGN_SCHEMA_VERSION,
        CAMPAIGN_MANIFEST_SCHEMA_VERSION,
    }:
        raise ValueError("unsupported campaign manifest schema_version")
    _validate_component(value["campaign_id"], "campaign_id")
    for name in ("inventory_hash", "profile_hash"):
        if not isinstance(value[name], str) or _SHA256.fullmatch(value[name]) is None:
            raise ValueError(f"{name} must be a lowercase SHA-256")
    for name in ("baseline_version", "model_generation"):
        if not isinstance(value[name], str) or not value[name]:
            raise ValueError(f"{name} must be a non-empty string")
    if not isinstance(value["plan_binding"], (dict, str)):
        raise TypeError("plan_binding must be an object or string")
    if isinstance(value["plan_binding"], str) and not value["plan_binding"]:
        raise ValueError("plan_binding must not be empty")
    if not isinstance(value["work_order_bindings"], list):
        raise TypeError("work_order_bindings must be a list")
    for binding in value["work_order_bindings"]:
        if not isinstance(binding, (dict, str)):
            raise TypeError("work-order bindings must be objects or strings")
        if isinstance(binding, str) and not binding:
            raise ValueError("work-order bindings must not be empty")
    if schema_version == CAMPAIGN_MANIFEST_SCHEMA_VERSION:
        from .system_audit import EvaluationContext

        context = EvaluationContext.from_dict(value["evaluation_context"])
        expected_baseline = (
            context.baseline["version"] or "baseline-unconfigured"
        )
        expected_generation = (
            context.models["generation"] or "model-unverified"
        )
        if value["baseline_version"] != expected_baseline:
            raise ValueError(
                "campaign baseline_version does not match evaluation_context"
            )
        if value["model_generation"] != expected_generation:
            raise ValueError(
                "campaign model_generation does not match evaluation_context"
            )
    _json_copy(value)


def _validate_artifact(value: Any) -> None:
    if not isinstance(value, dict):
        raise TypeError("campaign leaf artifact must be a JSON object")
    if value.get("schema_version") != CAMPAIGN_SCHEMA_VERSION:
        raise ValueError("unsupported campaign leaf schema_version")
    for name in ("campaign_id", "artifact_type", "artifact_id"):
        _validate_component(value.get(name), name)
    snapshot_id = value.get("snapshot_id")
    if snapshot_id is not None:
        _validate_component(snapshot_id, "snapshot_id")
    if value["artifact_type"] in _EXECUTION_ARTIFACT_TYPES:
        if snapshot_id is None:
            raise ValueError(
                f"{value['artifact_type']} artifact requires snapshot_id"
            )
        key = value.get("idempotency_key")
        if not isinstance(key, dict):
            raise ValueError(
                f"{value['artifact_type']} artifact requires idempotency_key"
            )
        if set(key) != {"operation_id", "attempt"}:
            raise ValueError(
                "idempotency_key requires exactly operation_id and attempt"
            )
        _validate_component(key["operation_id"], "idempotency operation_id")
        if (
            not isinstance(key["attempt"], int)
            or isinstance(key["attempt"], bool)
            or key["attempt"] < 1
        ):
            raise ValueError("idempotency attempt must be a positive integer")
    _json_copy(value)


def _validate_index(value: Any, campaign_id: str) -> None:
    if not isinstance(value, dict):
        raise TypeError("campaign index must be a JSON object")
    required = {
        "schema_version",
        "campaign_id",
        "revision",
        "snapshot_id",
        "artifacts",
    }
    if set(value) != required:
        raise ValueError("campaign index has unsupported fields")
    if value["schema_version"] != CAMPAIGN_SCHEMA_VERSION:
        raise ValueError("unsupported campaign index schema_version")
    if value["campaign_id"] != campaign_id:
        raise ValueError("campaign index id does not match its directory")
    if (
        not isinstance(value["revision"], int)
        or isinstance(value["revision"], bool)
        or value["revision"] < 0
    ):
        raise ValueError("campaign index revision must be non-negative")
    if value["snapshot_id"] is not None:
        _validate_component(value["snapshot_id"], "snapshot_id")
    if not isinstance(value["artifacts"], list):
        raise TypeError("campaign index artifacts must be a list")
    for artifact in value["artifacts"]:
        if not isinstance(artifact, dict):
            raise TypeError("campaign index artifact references must be objects")


def _validate_component(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError(f"{name} must be one safe path component")
    return value


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"value must contain only JSON data: {error}") from error


def _load_json(path: Path, description: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {description} at {path}: {error}") from error


def _read_lock_metadata_tolerant(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8").strip())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _lock_stream(stream: Any) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_stream(stream: Any) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()

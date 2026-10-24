"""Preview and explicitly prune old, unreferenced normalized session files.

Provider logs are never traversed. All JSON records in APU state are checked
conservatively for references, including closed monitoring windows. There is
no early expiry based on window closure without a reliable session linkage.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .behavior_lock import behavior_lock
from .evidence import evidence_path, validate_evidence_event
from .models import sha256_bytes, sha256_json
from .state import write_json_atomic

DEFAULT_MAX_FILES = 20_000
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
_RECEIPTS = ".behavior-retention"


def _timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("retention requires timezone-aware timestamps")
    return result.astimezone(UTC)


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _strings(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
            yield from re.findall(r"evidence-[0-9a-f]{64}", item)
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _scan(root, max_files, max_bytes):
    """Bound traversal and reads; never follow links or inspect provider paths."""
    evidence, references, inventory = {}, set(), []
    if not root.exists():
        return evidence, references, inventory
    pending = [root]
    count = used = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > max_files:
                    raise ValueError("retention file limit exceeded")
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                info = path.lstat()
                if _linked(info):
                    raise ValueError("retention cannot scan linked state entries")
                if relative == _RECEIPTS:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                    continue
                is_evidence = relative.startswith("behavior/evidence/")
                if not is_evidence and path.suffix not in {".json", ".jsonl"}:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("retention cannot scan special state entries")
                if info.st_size > max_bytes - used:
                    raise ValueError("retention byte limit exceeded")
                with path.open("rb") as stream:
                    data = stream.read(max_bytes - used + 1)
                used += len(data)
                if used > max_bytes:
                    raise ValueError("retention byte limit exceeded")
                after = path.lstat()
                if (info.st_size, info.st_mtime_ns, info.st_ino) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ino,
                ) or _linked(after):
                    raise ValueError("state changed during retention scan")
                fingerprint = {
                    "path": relative,
                    "sha256": sha256_bytes(data),
                    "bytes": len(data),
                    "mtime_ns": info.st_mtime_ns,
                }
                if is_evidence:
                    evidence[relative] = (fingerprint, data)
                    continue
                try:
                    records = (
                        [json.loads(line) for line in data.splitlines() if line.strip()]
                        if path.suffix == ".jsonl"
                        else [json.loads(data)]
                    )
                    for record in records:
                        references.update(_strings(record))
                except (ValueError, RecursionError) as error:
                    raise ValueError("invalid retention reference record") from error
                inventory.append(fingerprint)
    return evidence, references, sorted(inventory, key=lambda item: item["path"])


def preview_retention(
    state_home: Path,
    *,
    now: datetime | None = None,
    days: int = 30,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Return a content-bound plan without creating or changing state."""
    for value in (days, max_files, max_bytes):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("retention limits must be positive integers")
    current = _timestamp((now or datetime.now(UTC)).isoformat())
    try:
        cutoff = current - timedelta(days=days)
    except OverflowError as error:
        raise ValueError("retention age exceeds the supported range") from error
    root = Path(state_home).expanduser().resolve()
    files, references, inventory = _scan(root, max_files, max_bytes)
    groups = defaultdict(list)
    retained, candidates = [], []
    for relative, (fingerprint, data) in sorted(files.items()):
        path = root / relative
        reason = None
        tokens = {relative, str(path), path.as_posix(), path.name, path.stem}
        try:
            records = [json.loads(line) for line in data.splitlines() if line.strip()]
            if not records or path.suffix != ".jsonl":
                raise ValueError("empty or unknown evidence")
            identity = None
            for event in records:
                validate_evidence_event(event)
                expected = evidence_path(
                    root,
                    event["provider"],
                    event["session_id"],
                    schema_version=event["schema_version"],
                )
                if expected != path:
                    raise ValueError("evidence route mismatch")
                selected = (event["provider"], event["session_id"])
                if identity is not None and selected != identity:
                    raise ValueError("mixed session identities")
                identity = selected
                tokens.update((event["event_id"], event["session_id"]))
                if (
                    event["observed_at"] is None
                    or _timestamp(event["observed_at"]) >= cutoff
                ):
                    reason = "recent-or-undated"
            if fingerprint["mtime_ns"] / 1e9 >= cutoff.timestamp():
                reason = "recent-or-undated"
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            reason = "invalid-or-unknown-evidence"
        if tokens & references:
            reason = "referenced"
        # Pin all stored versions when any file for the session must remain.
        groups[(path.parent.name, path.stem)].append((fingerprint, reason))
    for members in groups.values():
        reasons = sorted({reason for _, reason in members if reason})
        for fingerprint, _ in members:
            if reasons:
                retained.append({"path": fingerprint["path"], "reason": reasons[0]})
            else:
                candidates.append(fingerprint)
    plan = {
        "schema_version": 1,
        "artifact_type": "behavior-retention-plan",
        "state_identity": sha256_json(os.path.normcase(str(root))),
        "as_of": current.isoformat(),
        "days": days,
        "max_files": max_files,
        "max_bytes": max_bytes,
        "reference_digest": sha256_json(inventory),
        "evidence_digest": sha256_json([files[name][0] for name in sorted(files)]),
        "candidates": sorted(candidates, key=lambda item: item["path"]),
        "retained": sorted(retained, key=lambda item: item["path"]),
    }
    return {**plan, "plan_id": sha256_json(plan)}


def _validate_plan_fields(root, plan, now):
    selected = dict(plan)
    identifier = selected.pop("plan_id", None)
    if identifier != sha256_json(selected):
        raise ValueError("retention plan digest is invalid")
    if (
        selected.get("schema_version") != 1
        or selected.get("artifact_type") != "behavior-retention-plan"
    ):
        raise ValueError("unsupported retention plan")
    if selected["state_identity"] != sha256_json(os.path.normcase(str(root))):
        raise ValueError("retention plan identifies another state directory")
    if _timestamp(selected["as_of"]) > now:
        raise ValueError("retention plan is from the future")
    for item in selected["candidates"]:
        path = root / item["path"]
        if path.is_absolute() and (
            not path.resolve().is_relative_to(root / "behavior" / "evidence")
            or path.relative_to(root).as_posix() != item["path"]
        ):
            raise ValueError("unsafe retention candidate path")
    return identifier


def _validate_plan(root, plan, now):
    try:
        if not isinstance(plan, dict):
            raise TypeError("retention plan must be an object")
        for name in ("days", "max_files", "max_bytes"):
            if (
                isinstance(plan[name], bool)
                or not isinstance(plan[name], int)
                or plan[name] < 1
            ):
                raise ValueError("retention limits must be positive integers")
        if (
            not isinstance(plan["candidates"], list)
            or len(plan["candidates"]) > plan["max_files"]
        ):
            raise ValueError("invalid retention candidates")
        return _validate_plan_fields(root, plan, now)
    except (KeyError, TypeError, AttributeError, OverflowError) as error:
        raise ValueError("invalid retention plan") from error


def apply_retention(
    state_home: Path, plan: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Apply an unchanged preview with durable intent and resumable deletion.

    Cooperative APU writers are excluded. External tools and older APU versions
    must not modify the state directory while this operation runs.
    """
    root = Path(state_home).expanduser().resolve()
    current = _timestamp((now or datetime.now(UTC)).isoformat())
    identifier = _validate_plan(root, plan, current)
    with behavior_lock(root):
        receipt_root = root / _RECEIPTS
        if receipt_root.exists() and _linked(receipt_root.lstat()):
            raise ValueError("retention receipt directory cannot be a link")
        journal_path = receipt_root / f"{identifier}.json"
        journal = None
        if journal_path.exists():
            if (
                _linked(journal_path.lstat())
                or journal_path.stat().st_size > 16 * 1024 * 1024
            ):
                raise ValueError("unsafe retention journal")
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if (
                journal.get("schema_version") != 1
                or journal.get("artifact_type") != "behavior-retention-journal"
                or journal.get("plan") != plan
            ):
                raise ValueError("retention journal does not match the plan")
            if journal.get("status") == "complete":
                return journal["receipt"]
            if journal.get("status") != "prepared":
                raise ValueError("unknown retention journal status")
        fresh = preview_retention(
            root,
            now=_timestamp(plan["as_of"]),
            days=plan["days"],
            max_files=plan["max_files"],
            max_bytes=plan["max_bytes"],
        )
        if journal is None and fresh != plan:
            raise ValueError("state changed since retention preview; preview again")
        if fresh["reference_digest"] != plan["reference_digest"]:
            raise ValueError(
                "references changed since retention preview; preview again"
            )
        eligible = {item["path"]: item for item in fresh["candidates"]}
        for item in plan["candidates"]:
            if (root / item["path"]).exists() and eligible.get(item["path"]) != item:
                raise ValueError(
                    "evidence changed since retention preview; preview again"
                )
        if journal is None:
            journal = {
                "schema_version": 1,
                "artifact_type": "behavior-retention-journal",
                "status": "prepared",
                "plan": plan,
            }
            write_json_atomic(journal_path, journal)
        # Intent is durable before unlink. A crash after any unlink can resume
        # from the same plan: missing files are already retired, never recreated.
        for item in plan["candidates"]:
            path = root / item["path"]
            if not path.exists():
                continue
            info = path.lstat()
            if (
                _linked(info)
                or info.st_mtime_ns != item["mtime_ns"]
                or info.st_size != item["bytes"]
            ):
                raise ValueError("evidence changed during retention apply")
            with path.open("rb") as stream:
                data = stream.read(item["bytes"] + 1)
            if sha256_bytes(data) != item["sha256"]:
                raise ValueError("evidence changed during retention apply")
            path.unlink()
        receipt = {
            "schema_version": 1,
            "artifact_type": "behavior-retention-receipt",
            "plan_id": identifier,
            "status": "complete",
            "removed_files": len(plan["candidates"]),
            "removed_bytes": sum(item["bytes"] for item in plan["candidates"]),
        }
        journal["status"] = "complete"
        journal["receipt"] = receipt
        write_json_atomic(journal_path, journal)
        return receipt

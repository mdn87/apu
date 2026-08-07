from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from apu.classify import DetectorPolicy
from apu.filesystem import hash_object
from apu.models import sha256_bytes, sha256_json
from apu.outcomes import read_outcomes
from apu.package_adapters import ClaudePackageAdapter, PackageObservation
from apu.package_adapters.base import hash_tree_bounded, read_stable_file
from apu.package_analysis import compare_package_versions
from apu.package_coordinates import (
    PackageCoordinate,
    compare_semantic_versions,
)
from apu.package_fetch import fetch_git_candidate, github_archive_url
from apu.package_state import (
    PackageLock,
    validate_candidate_tree,
    write_package_leaf,
)
from apu.receipts import (
    load_receipt,
    validate_receipt_for_state,
)
from apu.state import load_registry

CandidateResolver = Callable[..., tuple[dict[str, Any], Path, Path]]


class PackageResearchError(RuntimeError):
    """Raised when a package cannot be researched without guessing."""


def research_package(
    coordinate: PackageCoordinate,
    *,
    home: Path,
    state_home: Path,
    profile_sha256: str,
    detector_policy: DetectorPolicy,
    baseline_stamp: Mapping[str, Any],
    requested_version: str | None = None,
    researched_at: str | None = None,
    candidate_resolver: CandidateResolver = fetch_git_candidate,
) -> tuple[dict[str, Any], Path]:
    """Research one package without mutating its live installation."""

    if coordinate.provider != "claude":
        raise PackageResearchError(
            f"package provider is unsupported: {coordinate.provider}"
        )
    adapter = ClaudePackageAdapter(home=home)
    observation = adapter.observe(coordinate.profile_selector)
    if observation.status != "verified":
        raise PackageResearchError(
            "installed package observation is not authoritative enough to compare: "
            + ",".join(observation.issues or (observation.status,))
        )
    if (
        observation.version is None
        or observation.install_path is None
        or observation.tree_sha256 is None
    ):
        raise PackageResearchError("installed package observation is incomplete")

    source = _resolve_claude_source(coordinate, home=home)
    with PackageLock(state_home, coordinate.package_id):
        return _research_locked(
            coordinate,
            adapter=adapter,
            observation=observation,
            source=source,
            state_home=state_home,
            profile_sha256=profile_sha256,
            detector_policy=detector_policy,
            baseline_stamp=baseline_stamp,
            requested_version=requested_version,
            timestamp=researched_at or _timestamp(),
            candidate_resolver=candidate_resolver,
        )


def _research_locked(
    coordinate: PackageCoordinate,
    *,
    adapter: ClaudePackageAdapter,
    observation: PackageObservation,
    source: Mapping[str, Any],
    state_home: Path,
    profile_sha256: str,
    detector_policy: DetectorPolicy,
    baseline_stamp: Mapping[str, Any],
    requested_version: str | None,
    timestamp: str,
    candidate_resolver: CandidateResolver,
) -> tuple[dict[str, Any], Path]:
    _require_same_observation(adapter, coordinate, observation)
    candidate, candidate_path, candidate_tree = candidate_resolver(
        package_id=coordinate.package_id,
        source_url=source["source_url"],
        state_home=state_home,
        requested_version=requested_version,
        retrieved_at=timestamp,
    )
    _verify_candidate_artifacts(
        coordinate,
        state_home=state_home,
        candidate=candidate,
        candidate_path=candidate_path,
        candidate_tree=candidate_tree,
        expected_source_url=source["source_url"],
    )
    _require_same_observation(adapter, coordinate, observation)
    candidate_version = candidate.get("version")
    if (
        candidate.get("status") != "available"
        or not isinstance(candidate_version, str)
        or not candidate_version
    ):
        raise PackageResearchError("upstream candidate is unavailable")
    if (
        hash_tree_bounded(
            Path(observation.install_path),
            limits=adapter.limits,
        )
        != observation.tree_sha256
    ):
        raise PackageResearchError(
            "installed package changed while research was running"
        )

    comparison = compare_package_versions(
        Path(observation.install_path),
        candidate_tree,
        package_id=coordinate.package_id,
        installed_version=observation.version,
        candidate_version=candidate_version,
        detector_policy=detector_policy,
        baseline_stamp=baseline_stamp,
        candidate_virtual_links=candidate["normalization"]["links"],
    )
    expected_candidate_tree = candidate["immutable_ref"]["tree_sha256"]
    expected_content_tree = candidate["immutable_ref"]["content_tree_sha256"]
    if (
        hash_tree_bounded(
            Path(observation.install_path),
            limits=adapter.limits,
        )
        != observation.tree_sha256
        or hash_object(candidate_tree) != expected_content_tree
    ):
        raise PackageResearchError(
            "package tree changed while classifier analysis was running"
        )
    comparison = {
        **comparison,
        "installed_tree_sha256": observation.tree_sha256,
        "candidate_tree_sha256": expected_candidate_tree,
        "candidate_content_tree_sha256": expected_content_tree,
    }
    _require_same_observation(adapter, coordinate, observation)
    _, observation_path = write_package_leaf(
        state_home,
        kind="observations",
        package_id=coordinate.package_id,
        value=_observation_artifact(
            coordinate,
            observation,
            observed_at=timestamp,
            profile_sha256=profile_sha256,
        ),
    )
    _, comparison_path = write_package_leaf(
        state_home,
        kind="analyses",
        package_id=coordinate.package_id,
        value=comparison,
    )
    efficacy = _package_efficacy_context(state_home, coordinate.package_id)
    recommendation = _recommendation(
        installed_version=observation.version,
        candidate_version=candidate_version,
        comparison=comparison,
        observation=observation,
    )
    report_core = {
        "schema_version": 1,
        "artifact_type": "package-research-report",
        "researched_at": timestamp,
        "profile_sha256": profile_sha256,
        "package": {
            "provider": coordinate.provider,
            "name": coordinate.package,
            "source": coordinate.source,
            "profile_selector": coordinate.profile_selector,
            "package_id": coordinate.package_id,
        },
        "observation": observation.to_dict(),
        "observation_artifact": {
            "path": str(observation_path),
            "sha256": sha256_bytes(observation_path.read_bytes()),
        },
        "upstream": {
            "candidate": candidate,
            "candidate_artifact": {
                "path": str(candidate_path),
                "sha256": sha256_bytes(candidate_path.read_bytes()),
            },
            "marketplace_metadata": source,
        },
        "classifier_comparison": comparison,
        "comparison_artifact": {
            "path": str(comparison_path),
            "sha256": sha256_bytes(comparison_path.read_bytes()),
        },
        "efficacy": efficacy,
        "recommendation": recommendation,
    }
    report = {
        **report_core,
        "research_id": sha256_json(report_core),
    }
    _, report_path = write_package_leaf(
        state_home,
        kind="reports",
        package_id=coordinate.package_id,
        value=report,
    )
    return report, report_path


def _require_same_observation(
    adapter: ClaudePackageAdapter,
    coordinate: PackageCoordinate,
    expected: PackageObservation,
) -> None:
    current = adapter.observe(coordinate.profile_selector)
    if (
        current.status != "verified"
        or current.to_dict() != expected.to_dict()
    ):
        raise PackageResearchError(
            "installed package authority changed while research was running"
        )


def _resolve_claude_source(
    coordinate: PackageCoordinate,
    *,
    home: Path,
) -> dict[str, Any]:
    plugins_root = home.expanduser().resolve() / ".claude" / "plugins"
    known_path = plugins_root / "known_marketplaces.json"
    try:
        known_content = read_stable_file(known_path, max_bytes=2 * 1024 * 1024)
        known = json.loads(known_content)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise PackageResearchError(
            "Claude marketplace metadata is unavailable"
        ) from error
    if not isinstance(known, dict):
        raise PackageResearchError("Claude marketplace metadata is invalid")
    entry = known.get(coordinate.source)
    if not isinstance(entry, dict):
        raise PackageResearchError("tracked marketplace is not registered")
    raw_location = entry.get("installLocation")
    if not isinstance(raw_location, str) or not raw_location:
        raise PackageResearchError("marketplace install location is invalid")
    location = Path(raw_location).expanduser()
    if not location.is_absolute():
        raise PackageResearchError("marketplace install location is not absolute")
    location = location.resolve()
    marketplace_root = (plugins_root / "marketplaces").resolve()
    if not location.is_relative_to(marketplace_root):
        raise PackageResearchError("marketplace install location escapes its root")
    manifest_path = location / ".claude-plugin" / "marketplace.json"
    try:
        manifest_content = read_stable_file(
            manifest_path,
            max_bytes=8 * 1024 * 1024,
        )
        manifest = json.loads(manifest_content)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise PackageResearchError("marketplace manifest is unavailable") from error
    plugins = manifest.get("plugins") if isinstance(manifest, dict) else None
    if not isinstance(plugins, list):
        raise PackageResearchError("marketplace plugin list is invalid")
    matches = [
        item
        for item in plugins
        if isinstance(item, dict) and item.get("name") == coordinate.package
    ]
    if len(matches) != 1:
        raise PackageResearchError(
            "marketplace plugin identity is missing or ambiguous"
        )
    source = matches[0].get("source")
    if (
        not isinstance(source, dict)
        or source.get("source") != "url"
        or not isinstance(source.get("url"), str)
    ):
        raise PackageResearchError(
            "marketplace package source is not a supported URL source"
        )
    declared_revision = source.get("sha")
    if declared_revision is not None and (
        not isinstance(declared_revision, str)
        or len(declared_revision) not in {40, 64}
        or any(
            character not in "0123456789abcdefABCDEF"
            for character in declared_revision
        )
    ):
        raise PackageResearchError("marketplace package revision is invalid")
    return {
        "source_kind": "claude-marketplace-url",
        "source_url": source["url"],
        "declared_revision": (
            declared_revision.lower()
            if isinstance(declared_revision, str)
            else None
        ),
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_bytes(manifest_content),
        "known_marketplaces_sha256": sha256_bytes(known_content),
    }


def _observation_artifact(
    coordinate: PackageCoordinate,
    observation: PackageObservation,
    *,
    observed_at: str,
    profile_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_type": "package-observation",
        "observed_at": observed_at,
        "profile_sha256": profile_sha256,
        "package_id": coordinate.package_id,
        "observation": observation.to_dict(),
    }


def _recommendation(
    *,
    installed_version: str,
    candidate_version: str,
    comparison: Mapping[str, Any],
    observation: PackageObservation,
) -> dict[str, Any]:
    order = compare_semantic_versions(candidate_version, installed_version)
    verdict = comparison["delta"]["verdict"]
    reason_codes: list[str] = []
    if order <= 0:
        decision = "no-update"
        reason_codes.append("candidate-not-newer")
    elif verdict == "improved":
        decision = "work-order"
        reason_codes.extend(
            (
                "static-classifier-improved",
                "provider-pin-unsupported",
                "candidate-fixtures-unverified",
            )
        )
    elif verdict == "mixed":
        decision = "work-order"
        reason_codes.append("static-classifier-mixed")
    elif verdict == "worse":
        decision = "hold"
        reason_codes.append("static-classifier-worse")
    else:
        decision = "hold"
        reason_codes.append("no-static-improvement")
    if observation.status != "verified":
        reason_codes.append("installed-version-cache-only")
    if comparison["candidate"]["unclassified"]:
        reason_codes.append("candidate-dynamic-surfaces-unclassified")
    if comparison["delta"]["noncomparable"]["candidate"]:
        reason_codes.append("candidate-link-surfaces-noncomparable")
    return {
        "decision": decision,
        "reason_codes": sorted(set(reason_codes)),
        "eligible_for_pin_plan": False,
        "mutation_status": "unavailable-provider-pin-unsupported",
    }


def _package_efficacy_context(
    state_home: Path,
    package_id: str,
) -> dict[str, Any]:
    state_root = Path(state_home).expanduser().resolve()
    registry = load_registry(state_root)
    attributed: list[dict[str, Any]] = []
    unattributed_count = 0
    installation_ids: list[str] = []
    for installation_id, entry in registry["installations"].items():
        records = read_outcomes(state_home, installation_id)
        if not records:
            continue
        receipt_value = entry.get("receipt")
        if not isinstance(receipt_value, str):
            unattributed_count += len(records)
            continue
        receipt_path = (state_root / receipt_value).resolve()
        if not receipt_path.is_relative_to(state_root):
            unattributed_count += len(records)
            continue
        try:
            receipt = load_receipt(receipt_path)
            validate_receipt_for_state(state_root, receipt_path, receipt)
        except (OSError, ValueError):
            unattributed_count += len(records)
            continue
        package_bound = any(
            isinstance(operation.get("package_change"), dict)
            and operation["package_change"].get("package_id") == package_id
            for operation in receipt["operations"]
        )
        if not package_bound:
            unattributed_count += len(records)
            continue
        installation_ids.append(installation_id)
        for record in records:
            defect = record["escaped_defect"]
            attributed.append(
                {
                    "outcome_sha256": sha256_json(record),
                    "installation_id": installation_id,
                    "campaign_id": receipt.get("campaign_id"),
                    "task_id": record["task_id"],
                    "recorded_at": record["recorded_at"],
                    "material": record["material"],
                    "validation": record["validation"],
                    "rework": record["rework"],
                    "escaped_defect": {
                        "present": defect["present"],
                        "severity": defect["severity"],
                        "category": defect["category"],
                    },
                }
            )
    if attributed:
        status = "installation-context-only"
    elif unattributed_count:
        status = "unavailable-unattributed"
    else:
        status = "none"
    return {
        "attribution_status": status,
        "package_id": package_id,
        "installation_ids": sorted(set(installation_ids)),
        "outcome_refs": sorted(
            attributed,
            key=lambda item: (
                item["installation_id"],
                item["recorded_at"],
                item["task_id"],
            ),
        ),
        "unattributed_record_count": unattributed_count,
        "limitations": [
            "v1 outcomes do not prove package causality or activation",
            "outcome notes and runner output are never attached",
        ],
    }


def _verify_candidate_artifacts(
    coordinate: PackageCoordinate,
    *,
    state_home: Path,
    candidate: Mapping[str, Any],
    candidate_path: Path,
    candidate_tree: Path,
    expected_source_url: str,
) -> None:
    state_root = Path(state_home).expanduser().resolve()
    artifact_path = Path(candidate_path).expanduser().resolve()
    tree_path = Path(candidate_tree).expanduser().resolve()
    expected_artifact_path = (
        state_root
        / "packages"
        / "candidates"
        / sha256_json(coordinate.package_id)
        / f"{sha256_json(candidate)}.json"
    )
    if artifact_path != expected_artifact_path:
        raise PackageResearchError("candidate artifact is outside private state")
    if not tree_path.is_relative_to(state_root / "packages" / "trees"):
        raise PackageResearchError("candidate tree is outside private state")
    try:
        stored_candidate = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PackageResearchError("candidate artifact is unreadable") from error
    if stored_candidate != dict(candidate):
        raise PackageResearchError("candidate artifact does not match the resolver")
    if (
        set(candidate)
        != {
            "schema_version",
            "artifact_type",
            "package_id",
            "status",
            "version",
            "immutable_ref",
            "retrieval",
            "normalization",
            "changelog",
        }
        or candidate.get("schema_version") != 1
        or candidate.get("artifact_type") != "package-candidate"
        or candidate.get("status") != "available"
        or candidate.get("package_id") != coordinate.package_id
    ):
        raise PackageResearchError("candidate artifact identifies another package")
    candidate_version = candidate.get("version")
    if not isinstance(candidate_version, str) or not candidate_version:
        raise PackageResearchError("candidate artifact version is invalid")
    immutable = candidate.get("immutable_ref")
    if (
        not isinstance(immutable, Mapping)
        or set(immutable)
        != {
            "tag",
            "commit_oid",
            "archive_sha256",
            "content_tree_sha256",
            "tree_sha256",
        }
    ):
        raise PackageResearchError("candidate immutable reference is missing")
    commit_oid = immutable.get("commit_oid")
    if (
        not isinstance(immutable.get("tag"), str)
        or not immutable["tag"]
        or not isinstance(commit_oid, str)
        or len(commit_oid) not in (40, 64)
        or any(character not in "0123456789abcdef" for character in commit_oid)
        or any(
        not isinstance(immutable.get(field), str)
        or len(immutable[field]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in immutable[field]
        )
        for field in ("archive_sha256", "content_tree_sha256", "tree_sha256")
        )
    ):
        raise PackageResearchError("candidate immutable reference is invalid")
    retrieval = candidate.get("retrieval")
    if (
        not isinstance(retrieval, Mapping)
        or set(retrieval)
        != {
            "retrieved_at",
            "source_kind",
            "source_url",
            "archive_url",
        }
        or retrieval.get("source_kind") != "github-commit-archive"
        or retrieval.get("source_url") != expected_source_url
        or not isinstance(retrieval.get("retrieved_at"), str)
        or not retrieval["retrieved_at"]
        or retrieval.get("archive_url")
        != github_archive_url(expected_source_url, commit_oid)
    ):
        raise PackageResearchError("candidate retrieval provenance is invalid")
    normalization = candidate.get("normalization")
    if (
        not isinstance(normalization, Mapping)
        or set(normalization) != {"policy", "links"}
        or normalization.get("policy") != "virtual-internal-file-links-v1"
        or not isinstance(normalization.get("links"), list)
        or any(
            not isinstance(link, Mapping)
            or set(link)
            != {
                "relative_path",
                "target",
                "resolved_target",
                "target_content_sha256",
            }
            for link in normalization["links"]
        )
    ):
        raise PackageResearchError("candidate normalization provenance is invalid")
    _verify_candidate_links(tree_path, normalization["links"])
    logical_tree_sha256 = sha256_json(
        {
            "content_tree_sha256": immutable["content_tree_sha256"],
            "normalization": dict(normalization),
        }
    )
    if logical_tree_sha256 != immutable["tree_sha256"]:
        raise PackageResearchError("candidate logical tree identity does not match")
    expected_tree = immutable.get("tree_sha256")
    expected_content_tree = immutable.get("content_tree_sha256")
    validate_candidate_tree(tree_path)
    if (
        not isinstance(expected_tree, str)
        or not isinstance(expected_content_tree, str)
        or tree_path.name != expected_content_tree
        or hash_object(tree_path) != expected_content_tree
    ):
        raise PackageResearchError("candidate tree identity does not match")
    manifest_path = tree_path / ".claude-plugin" / "plugin.json"
    try:
        manifest = json.loads(
            read_stable_file(
                manifest_path,
                max_bytes=2 * 1024 * 1024,
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise PackageResearchError("candidate plugin manifest is invalid") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("name") != coordinate.package
        or manifest.get("version") != candidate_version
    ):
        raise PackageResearchError(
            "candidate plugin manifest identity or version does not match"
        )


def _verify_candidate_links(
    tree_path: Path,
    links: list[Mapping[str, Any]],
) -> None:
    identities: set[str] = set()
    previous_path: str | None = None
    for link in links:
        relative = link.get("relative_path")
        target = link.get("target")
        resolved = link.get("resolved_target")
        target_hash = link.get("target_content_sha256")
        if not all(isinstance(value, str) and value for value in link.values()):
            raise PackageResearchError(
                "candidate normalization link values are invalid"
            )
        assert isinstance(relative, str)
        assert isinstance(target, str)
        assert isinstance(resolved, str)
        assert isinstance(target_hash, str)
        relative_path = PurePosixPath(relative)
        target_path = PurePosixPath(target)
        resolved_path = PurePosixPath(resolved)
        if (
            "\\" in relative
            or "\\" in target
            or "\\" in resolved
            or relative_path.is_absolute()
            or target_path.is_absolute()
            or resolved_path.is_absolute()
            or any(
                part in {"", ".", ".."}
                for path in (relative_path, target_path, resolved_path)
                for part in path.parts
            )
            or ":" in target
            or relative_path.parent / target_path != resolved_path
            or len(target_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in target_hash
            )
        ):
            raise PackageResearchError(
                "candidate normalization link relation is invalid"
            )
        identity = relative.casefold()
        if (
            identity in identities
            or (previous_path is not None and relative <= previous_path)
        ):
            raise PackageResearchError(
                "candidate normalization links are not unique and sorted"
            )
        identities.add(identity)
        previous_path = relative
        logical_path = tree_path / Path(*relative_path.parts)
        stored_target = tree_path / Path(*resolved_path.parts)
        try:
            target_content = read_stable_file(
                stored_target,
                max_bytes=256 * 1024 * 1024,
            )
        except (OSError, ValueError) as error:
            raise PackageResearchError(
                "candidate normalization target is unreadable"
            ) from error
        if (
            logical_path.exists()
            or logical_path.is_symlink()
            or stored_target.is_symlink()
            or not stored_target.is_file()
            or sha256_bytes(target_content) != target_hash
        ):
            raise PackageResearchError(
                "candidate normalization target identity does not match"
            )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")

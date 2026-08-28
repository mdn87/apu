from __future__ import annotations

import os
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypedDict

from .classify import DETECTOR_VERSION, DetectorPolicy, classify_surface
from .models import InstructionSurface, sha256_bytes, sha256_json

_MAX_INSTRUCTION_BYTES = 2 * 1024 * 1024
_MAX_PACKAGE_ENTRIES = 20_000
_SHA256_LENGTH = 64
_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)
_SCRIPT_SUFFIXES = frozenset(
    {".bash", ".bat", ".cmd", ".js", ".mjs", ".ps1", ".py", ".sh", ".ts"}
)


class PackageAnalysisError(ValueError):
    """Raised when a package tree or comparison input is unsafe or invalid."""


class BaselineStamp(TypedDict):
    version: str | None
    status: str
    retrieved_at: str | None
    artifact_sha256: str | None


def analyze_package_version(
    root: Path,
    *,
    package_id: str,
    version: str,
    detector_policy: DetectorPolicy,
    baseline_stamp: Mapping[str, Any],
    virtual_links: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """Classify bounded instruction surfaces without returning their content."""

    package_id = _nonempty(package_id, "package_id")
    version = _nonempty(version, "version")
    policy = _detector_policy(detector_policy)
    baseline = _baseline_stamp(baseline_stamp)
    package_root = _package_root(root)
    surfaces, unclassified = _collect_surfaces(
        package_root,
        virtual_links=virtual_links,
    )

    findings: list[dict[str, Any]] = []
    surface_manifest: list[dict[str, Any]] = []
    occurrence_counts: Counter[str] = Counter()
    for surface in surfaces:
        relative_path = surface["relative_path"]
        content = surface["content"]
        content_sha256 = sha256_bytes(content)
        surface_kind = surface["surface_kind"]
        source_object_type = surface["source_object_type"]
        link_target = surface["link_target"]
        surface_manifest.append(
            {
                "relative_path": relative_path,
                "surface_kind": surface_kind,
                "content_sha256": content_sha256,
                "source_object_type": source_object_type,
                "link_target": link_target,
            }
        )
        text = content.decode("utf-8", errors="replace")
        instruction_surface = InstructionSurface(
            id="sha256:"
            + sha256_json(
                {
                    "package_id": package_id,
                    "relative_path": relative_path,
                    "surface_kind": surface_kind,
                    "content_sha256": content_sha256,
                    "source_object_type": source_object_type,
                    "link_target": link_target,
                }
            ),
            path=relative_path,
            kind=surface_kind,
            provider="package",
            authority="package",
            scope="global",
            real_path=relative_path,
            is_symlink=source_object_type == "symlink",
            content_sha256=content_sha256,
            mode=None,
            precedence=80,
            sensitive=False,
        )
        lines = text.splitlines()
        for finding in classify_surface(
            instruction_surface,
            text,
            detector_policy=policy,
        ):
            line = finding.location.get("line")
            normalized_line = (
                _normalize_line(lines[line - 1])
                if isinstance(line, int) and 1 <= line <= len(lines)
                else ""
            )
            normalized_line_sha256 = sha256_bytes(normalized_line.encode("utf-8"))
            base_key = sha256_json(
                {
                    "relative_path": relative_path,
                    "surface_kind": surface_kind,
                    "category": finding.category,
                    "evidence": list(finding.evidence),
                    "normalized_line_sha256": normalized_line_sha256,
                    "source_object_type": source_object_type,
                    "link_target": link_target,
                }
            )
            occurrence_counts[base_key] += 1
            semantic_key = sha256_json(
                {
                    "base_key": base_key,
                    "occurrence": occurrence_counts[base_key],
                }
            )
            findings.append(
                {
                    "semantic_key": semantic_key,
                    "relative_path": relative_path,
                    "surface_kind": surface_kind,
                    "source_object_type": source_object_type,
                    "link_target": link_target,
                    "category": finding.category,
                    "severity": finding.severity,
                    "confidence": finding.confidence,
                    "analysis_method": finding.analysis_method,
                    "line": line,
                    "normalized_line_sha256": normalized_line_sha256,
                    "evidence": list(finding.evidence),
                    "summary": finding.summary,
                }
            )

    findings.sort(
        key=lambda item: (
            item["relative_path"],
            item["line"] if isinstance(item["line"], int) else 0,
            item["category"],
            item["semantic_key"],
        )
    )
    surface_manifest.sort(
        key=lambda item: (item["relative_path"], item["surface_kind"])
    )
    unclassified.sort(key=lambda item: (item["relative_path"], item["reason"]))
    counts = dict(sorted(Counter(item["category"] for item in findings).items()))
    return {
        "schema_version": 1,
        "artifact_type": "package-version-analysis",
        "package_id": package_id,
        "version": version,
        "classifier_context": {
            "detector_version": DETECTOR_VERSION,
            "baseline": baseline,
            "detector_policy": {
                "duplicate_instruction_minimum_words": (
                    policy.duplicate_instruction_minimum_words
                ),
                "speculative_skill_threshold_enabled": (
                    policy.speculative_skill_threshold_enabled
                ),
            },
            "detector_policy_sha256": sha256_json(
                {
                    "duplicate_instruction_minimum_words": (
                        policy.duplicate_instruction_minimum_words
                    ),
                    "speculative_skill_threshold_enabled": (
                        policy.speculative_skill_threshold_enabled
                    ),
                }
            ),
        },
        "surface_manifest_sha256": sha256_json(
            {
                "surfaces": surface_manifest,
                "unclassified": unclassified,
            }
        ),
        "surface_count": len(surface_manifest),
        "finding_counts": counts,
        "findings": findings,
        "unclassified": unclassified,
    }


def compare_package_versions(
    installed_root: Path,
    candidate_root: Path,
    *,
    package_id: str,
    installed_version: str,
    candidate_version: str,
    detector_policy: DetectorPolicy,
    baseline_stamp: Mapping[str, Any],
    candidate_virtual_links: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """Analyze two package trees under one frozen classifier context."""

    installed = analyze_package_version(
        installed_root,
        package_id=package_id,
        version=installed_version,
        detector_policy=detector_policy,
        baseline_stamp=baseline_stamp,
    )
    candidate = analyze_package_version(
        candidate_root,
        package_id=package_id,
        version=candidate_version,
        detector_policy=detector_policy,
        baseline_stamp=baseline_stamp,
        virtual_links=candidate_virtual_links,
    )
    delta = diff_package_analyses(installed, candidate)
    return {
        "schema_version": 1,
        "artifact_type": "package-classifier-comparison",
        "package_id": _nonempty(package_id, "package_id"),
        "classifier_context": installed["classifier_context"],
        "installed": installed,
        "candidate": candidate,
        "delta": delta,
    }


def diff_package_analyses(
    installed: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Diff validated package analyses by stable semantic finding identity."""

    old = _analysis(installed, "installed")
    new = _analysis(candidate, "candidate")
    if old["package_id"] != new["package_id"]:
        raise PackageAnalysisError("package analyses identify different packages")
    if old["classifier_context"] != new["classifier_context"]:
        raise PackageAnalysisError(
            "package analyses must use one frozen classifier context"
        )
    old_noncomparable = [
        item for item in old["findings"] if item["source_object_type"] == "symlink"
    ]
    new_noncomparable = [
        item for item in new["findings"] if item["source_object_type"] == "symlink"
    ]
    old_findings = {
        item["semantic_key"]: item
        for item in old["findings"]
        if item["source_object_type"] == "file"
    }
    new_findings = {
        item["semantic_key"]: item
        for item in new["findings"]
        if item["source_object_type"] == "file"
    }
    if len(old_findings) != len(old["findings"]) - len(old_noncomparable) or len(
        new_findings
    ) != len(new["findings"]) - len(new_noncomparable):
        raise PackageAnalysisError("package analysis semantic keys must be unique")

    old_keys = set(old_findings)
    new_keys = set(new_findings)
    resolved = [old_findings[key] for key in sorted(old_keys - new_keys)]
    introduced = [new_findings[key] for key in sorted(new_keys - old_keys)]
    severity_changed = [
        {
            "semantic_key": key,
            "relative_path": old_findings[key]["relative_path"],
            "category": old_findings[key]["category"],
            "installed": old_findings[key]["severity"],
            "candidate": new_findings[key]["severity"],
        }
        for key in sorted(old_keys & new_keys)
        if old_findings[key]["severity"] != new_findings[key]["severity"]
    ]

    old_counts = Counter(item["category"] for item in old_findings.values())
    new_counts = Counter(item["category"] for item in new_findings.values())
    categories = sorted(set(old_counts) | set(new_counts))
    counts = {
        category: {
            "installed": old_counts.get(category, 0),
            "candidate": new_counts.get(category, 0),
            "delta": new_counts.get(category, 0) - old_counts.get(category, 0),
        }
        for category in categories
    }
    improved_severity = any(
        _severity_rank(item["candidate"]) < _severity_rank(item["installed"])
        for item in severity_changed
    )
    worsened_severity = any(
        _severity_rank(item["candidate"]) > _severity_rank(item["installed"])
        for item in severity_changed
    )
    has_improvement = bool(resolved) or improved_severity
    has_worsening = bool(introduced) or worsened_severity
    if has_improvement and has_worsening:
        verdict: Literal["improved", "worse", "mixed", "unchanged"] = "mixed"
    elif has_improvement:
        verdict = "improved"
    elif has_worsening:
        verdict = "worse"
    else:
        verdict = "unchanged"
    return {
        "finding_counts": counts,
        "resolved": resolved,
        "introduced": introduced,
        "severity_changed": severity_changed,
        "noncomparable": {
            "installed": old_noncomparable,
            "candidate": new_noncomparable,
            "reason": "virtual-link-source-provenance-is-not-installed-comparable",
        },
        "verdict": verdict,
    }


def _collect_surfaces(
    root: Path,
    *,
    virtual_links: Sequence[Mapping[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    surfaces: list[dict[str, Any]] = []
    unclassified: list[dict[str, str]] = []
    entry_count = 0
    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=_raise_walk_error,
    ):
        current_path = Path(current)
        retained_directories: list[str] = []
        for name in sorted(directory_names):
            entry_count += 1
            if entry_count > _MAX_PACKAGE_ENTRIES:
                raise PackageAnalysisError(
                    "package tree exceeds the analysis entry limit"
                )
            path = current_path / name
            relative = _relative(root, path)
            if name in _IGNORED_DIRECTORIES:
                continue
            if path.is_symlink():
                unclassified.append(
                    {
                        "relative_path": relative,
                        "reason": "symlink-not-followed",
                    }
                )
            else:
                retained_directories.append(name)
        directory_names[:] = retained_directories

        for name in sorted(file_names):
            entry_count += 1
            if entry_count > _MAX_PACKAGE_ENTRIES:
                raise PackageAnalysisError(
                    "package tree exceeds the analysis entry limit"
                )
            path = current_path / name
            relative = _relative(root, path)
            if path.is_symlink():
                unclassified.append(
                    {
                        "relative_path": relative,
                        "reason": "symlink-not-followed",
                    }
                )
                continue
            surface_kind = _surface_kind(PurePosixPath(relative))
            if surface_kind is not None:
                try:
                    size = path.stat().st_size
                except OSError:
                    unclassified.append(
                        {
                            "relative_path": relative,
                            "reason": "instruction-unreadable",
                        }
                    )
                    continue
                if size > _MAX_INSTRUCTION_BYTES:
                    unclassified.append(
                        {
                            "relative_path": relative,
                            "reason": "instruction-size-limit",
                        }
                    )
                    continue
                try:
                    before = path.stat()
                    with path.open("rb") as stream:
                        content = stream.read(_MAX_INSTRUCTION_BYTES + 1)
                    after = path.stat()
                except OSError:
                    unclassified.append(
                        {
                            "relative_path": relative,
                            "reason": "instruction-unreadable",
                        }
                    )
                    continue
                if len(content) > _MAX_INSTRUCTION_BYTES:
                    unclassified.append(
                        {
                            "relative_path": relative,
                            "reason": "instruction-size-limit",
                        }
                    )
                    continue
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise PackageAnalysisError(
                        f"package instruction changed while reading: {relative}"
                    )
                surfaces.append(
                    {
                        "relative_path": relative,
                        "surface_kind": surface_kind,
                        "content": content,
                        "source_object_type": "file",
                        "link_target": None,
                    }
                )
            elif _is_dynamic(PurePosixPath(relative)):
                unclassified.append(
                    {
                        "relative_path": relative,
                        "reason": "dynamic-hook-or-script",
                    }
                )
    for link in _virtual_links(root, virtual_links):
        relative = link["relative_path"]
        surface_kind = _surface_kind(PurePosixPath(relative))
        if surface_kind is None:
            if _is_dynamic(PurePosixPath(relative)):
                unclassified.append(
                    {
                        "relative_path": relative,
                        "reason": "virtual-symlink-dynamic",
                    }
                )
            continue
        surfaces.append(
            {
                "relative_path": relative,
                "surface_kind": surface_kind,
                "content": link["content"],
                "source_object_type": "symlink",
                "link_target": link["target"],
            }
        )
    return surfaces, unclassified


def _virtual_links(
    root: Path,
    links: Sequence[Mapping[str, str]],
) -> tuple[dict[str, Any], ...]:
    validated: list[dict[str, Any]] = []
    paths: set[str] = set()
    for link in links:
        if not isinstance(link, Mapping) or set(link) != {
            "relative_path",
            "target",
            "resolved_target",
            "target_content_sha256",
        }:
            raise PackageAnalysisError("virtual link manifest is invalid")
        relative = _virtual_relative(link["relative_path"], "link path")
        resolved = _virtual_relative(
            link["resolved_target"],
            "resolved link target",
        )
        target = link["target"]
        target_hash = link["target_content_sha256"]
        if (
            not isinstance(target, str)
            or not target
            or not isinstance(target_hash, str)
            or len(target_hash) != _SHA256_LENGTH
            or any(character not in "0123456789abcdef" for character in target_hash)
        ):
            raise PackageAnalysisError("virtual link provenance is invalid")
        normalized = relative.casefold()
        if (
            normalized in paths
            or (root / Path(*PurePosixPath(relative).parts)).exists()
        ):
            raise PackageAnalysisError("virtual link path collides with package tree")
        paths.add(normalized)
        target_path = root / Path(*PurePosixPath(resolved).parts)
        if target_path.is_symlink() or not target_path.is_file():
            raise PackageAnalysisError(
                "virtual link target is not a stored regular file"
            )
        content = _read_instruction_stable(target_path, relative)
        if sha256_bytes(content) != target_hash:
            raise PackageAnalysisError("virtual link target identity does not match")
        validated.append(
            {
                "relative_path": relative,
                "target": target,
                "resolved_target": resolved,
                "target_content_sha256": target_hash,
                "content": content,
            }
        )
    return tuple(sorted(validated, key=lambda item: item["relative_path"]))


def _virtual_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PackageAnalysisError(f"virtual {label} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise PackageAnalysisError(f"virtual {label} is unsafe")
    return path.as_posix()


def _read_instruction_stable(path: Path, logical_path: str) -> bytes:
    try:
        before = path.stat()
        if before.st_size > _MAX_INSTRUCTION_BYTES:
            raise PackageAnalysisError(
                f"virtual link target exceeds the instruction limit: {logical_path}"
            )
        with path.open("rb") as stream:
            content = stream.read(_MAX_INSTRUCTION_BYTES + 1)
        after = path.stat()
    except OSError as error:
        raise PackageAnalysisError(
            f"virtual link target is unreadable: {logical_path}"
        ) from error
    if len(content) > _MAX_INSTRUCTION_BYTES:
        raise PackageAnalysisError(
            f"virtual link target exceeds the instruction limit: {logical_path}"
        )
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise PackageAnalysisError(
            f"virtual link target changed while reading: {logical_path}"
        )
    return content


def _raise_walk_error(error: OSError) -> None:
    raise PackageAnalysisError(
        "package tree contains an unreadable directory"
    ) from error


def _surface_kind(path: PurePosixPath) -> str | None:
    if path.name == "SKILL.md":
        return "skill"
    if path.name == "CLAUDE.md":
        return "claude-instructions"
    if path.name == "AGENTS.md":
        return "codex-instructions"
    parts = tuple(part.casefold() for part in path.parts)
    for index in range(len(parts) - 2):
        if (
            parts[index : index + 2] == (".claude", "rules")
            and path.suffix.casefold() == ".md"
        ):
            return "claude-rule"
    return None


def _is_dynamic(path: PurePosixPath) -> bool:
    lowered_parts = tuple(part.casefold() for part in path.parts)
    return (
        "hooks" in lowered_parts
        or path.name.casefold() == "hooks.json"
        or path.suffix.casefold() in _SCRIPT_SUFFIXES
    )


def _relative(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as error:  # pragma: no cover - os.walk contract guard
        raise PackageAnalysisError("package traversal escaped its root") from error
    if relative.is_absolute() or ".." in relative.parts:
        raise PackageAnalysisError("package traversal produced an unsafe path")
    return relative.as_posix()


def _package_root(root: Path) -> Path:
    path = Path(root)
    if path.is_symlink():
        raise PackageAnalysisError("package root must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise PackageAnalysisError(f"package root is unavailable: {path}") from error
    if not resolved.is_dir():
        raise PackageAnalysisError("package root must be a directory")
    return resolved


def _normalize_line(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _baseline_stamp(value: Mapping[str, Any]) -> BaselineStamp:
    if not isinstance(value, Mapping):
        raise PackageAnalysisError("baseline_stamp must be an object")
    fields = {"version", "status", "retrieved_at", "artifact_sha256"}
    if set(value) != fields:
        raise PackageAnalysisError("baseline_stamp has unsupported fields")
    status = value["status"]
    if status not in {"unconfigured", "adopted", "stale", "legacy-unverified"}:
        raise PackageAnalysisError("baseline_stamp status is unsupported")
    version = _optional_hash(value["version"], "baseline version")
    artifact_sha256 = _optional_hash(
        value["artifact_sha256"],
        "baseline artifact_sha256",
    )
    if (version is None) != (artifact_sha256 is None):
        raise PackageAnalysisError(
            "baseline version and artifact hash must both be set or null"
        )
    retrieved_at = value["retrieved_at"]
    if retrieved_at is not None and (
        not isinstance(retrieved_at, str) or not retrieved_at
    ):
        raise PackageAnalysisError(
            "baseline retrieved_at must be null or non-empty text"
        )
    if status in {"adopted", "stale"} and (version is None or retrieved_at is None):
        raise PackageAnalysisError(f"baseline status {status} requires provenance")
    return {
        "version": version,
        "status": status,
        "retrieved_at": retrieved_at,
        "artifact_sha256": artifact_sha256,
    }


def _analysis(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PackageAnalysisError(f"{name} analysis must be an object")
    required = {
        "schema_version",
        "artifact_type",
        "package_id",
        "version",
        "classifier_context",
        "surface_manifest_sha256",
        "surface_count",
        "finding_counts",
        "findings",
        "unclassified",
    }
    if set(value) != required:
        raise PackageAnalysisError(f"{name} analysis has unsupported fields")
    if (
        value["schema_version"] != 1
        or value["artifact_type"] != "package-version-analysis"
    ):
        raise PackageAnalysisError(f"{name} analysis type is unsupported")
    if not isinstance(value["findings"], list):
        raise PackageAnalysisError(f"{name} findings must be a list")
    if not isinstance(value["finding_counts"], Mapping):
        raise PackageAnalysisError(f"{name} finding_counts must be an object")
    _nonempty(value["package_id"], f"{name} package_id")
    _nonempty(value["version"], f"{name} version")
    _required_hash(
        value["surface_manifest_sha256"],
        f"{name} surface_manifest_sha256",
    )
    if (
        not isinstance(value["surface_count"], int)
        or isinstance(value["surface_count"], bool)
        or value["surface_count"] < 0
    ):
        raise PackageAnalysisError(f"{name} surface_count must be non-negative")
    context = value["classifier_context"]
    if not isinstance(context, Mapping) or set(context) != {
        "detector_version",
        "baseline",
        "detector_policy",
        "detector_policy_sha256",
    }:
        raise PackageAnalysisError(f"{name} classifier_context has unsupported fields")
    _baseline_stamp(context["baseline"])
    _nonempty(context["detector_version"], f"{name} detector_version")
    policy_value = context["detector_policy"]
    if not isinstance(policy_value, Mapping) or set(policy_value) != {
        "duplicate_instruction_minimum_words",
        "speculative_skill_threshold_enabled",
    }:
        raise PackageAnalysisError(f"{name} detector_policy has unsupported fields")
    try:
        DetectorPolicy(
            duplicate_instruction_minimum_words=policy_value[
                "duplicate_instruction_minimum_words"
            ],
            speculative_skill_threshold_enabled=policy_value[
                "speculative_skill_threshold_enabled"
            ],
        )
    except (TypeError, ValueError) as error:
        raise PackageAnalysisError(f"{name} detector_policy is invalid") from error
    policy_hash = _required_hash(
        context["detector_policy_sha256"],
        f"{name} detector_policy_sha256",
    )
    if sha256_json(dict(policy_value)) != policy_hash:
        raise PackageAnalysisError(
            f"{name} detector_policy_sha256 does not match its policy"
        )
    for finding in value["findings"]:
        _validate_finding(finding, name)
    expected_counts = dict(
        sorted(Counter(item["category"] for item in value["findings"]).items())
    )
    if dict(value["finding_counts"]) != expected_counts:
        raise PackageAnalysisError(f"{name} finding_counts do not match its findings")
    if not isinstance(value["unclassified"], list):
        raise PackageAnalysisError(f"{name} unclassified must be a list")
    for item in value["unclassified"]:
        if not isinstance(item, Mapping) or set(item) != {"relative_path", "reason"}:
            raise PackageAnalysisError(
                f"{name} unclassified entries have unsupported fields"
            )
        _safe_relative(item["relative_path"], f"{name} unclassified path")
        _nonempty(item["reason"], f"{name} unclassified reason")
    return dict(value)


def _validate_finding(value: Any, name: str) -> None:
    fields = {
        "semantic_key",
        "relative_path",
        "surface_kind",
        "source_object_type",
        "link_target",
        "category",
        "severity",
        "confidence",
        "analysis_method",
        "line",
        "normalized_line_sha256",
        "evidence",
        "summary",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise PackageAnalysisError(f"{name} finding has unsupported fields")
    _required_hash(value["semantic_key"], f"{name} semantic_key")
    _required_hash(
        value["normalized_line_sha256"],
        f"{name} normalized_line_sha256",
    )
    _safe_relative(value["relative_path"], f"{name} finding path")
    for field in (
        "surface_kind",
        "category",
        "severity",
        "confidence",
        "analysis_method",
        "summary",
    ):
        _nonempty(value[field], f"{name} finding {field}")
    source_object_type = value["source_object_type"]
    link_target = value["link_target"]
    if source_object_type == "file":
        if link_target is not None:
            raise PackageAnalysisError(
                f"{name} regular-file finding cannot have a link target"
            )
    elif source_object_type == "symlink":
        _nonempty(link_target, f"{name} finding link_target")
    else:
        raise PackageAnalysisError(f"{name} finding source_object_type is unsupported")
    _severity_rank(value["severity"])
    line = value["line"]
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        raise PackageAnalysisError(f"{name} finding line must be positive")
    if not isinstance(value["evidence"], list) or any(
        not isinstance(item, str) or not item for item in value["evidence"]
    ):
        raise PackageAnalysisError(f"{name} finding evidence must be a string list")


def _safe_relative(value: Any, field: str) -> str:
    text = _nonempty(value, field)
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        raise PackageAnalysisError(f"{field} must be a safe relative path")
    return text


def _detector_policy(value: DetectorPolicy) -> DetectorPolicy:
    if not isinstance(value, DetectorPolicy):
        raise TypeError("detector_policy must be DetectorPolicy")
    return value


def _optional_hash(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PackageAnalysisError(f"{field} must be a lowercase SHA-256")
    return value


def _required_hash(value: Any, field: str) -> str:
    result = _optional_hash(value, field)
    if result is None:
        raise PackageAnalysisError(f"{field} must be a lowercase SHA-256")
    return result


def _severity_rank(value: str) -> int:
    try:
        return _SEVERITY_RANK[value]
    except KeyError as error:
        raise PackageAnalysisError(f"unsupported finding severity: {value}") from error


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PackageAnalysisError(f"{field} must be non-empty text")
    return value.strip()

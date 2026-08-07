from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from .base import (
    EvidenceReference,
    ObservationLimits,
    PackageObservation,
    PackageObservationError,
    absolute_logical_path,
    hash_tree_bounded,
    read_stable_file,
    safe_component,
    same_path,
    sorted_provenance,
    split_package_id,
)


@dataclass(frozen=True)
class ClaudePackageAdapter:
    """Observe Claude marketplace plugins from local read-only evidence."""

    home: Path
    limits: ObservationLimits = field(default_factory=ObservationLimits)
    provider: str = field(default="claude", init=False)

    @property
    def plugins_root(self) -> Path:
        return absolute_logical_path(self.home) / ".claude" / "plugins"

    def observe(self, package_id: str) -> PackageObservation:
        name, marketplace = split_package_id(package_id)
        evidence: list[EvidenceReference] = []
        installed_path = self.plugins_root / "installed_plugins.json"

        if installed_path.is_file():
            try:
                installed = self._read_json(
                    installed_path,
                    kind="installed-plugins",
                    evidence=evidence,
                )
                plugins = installed.get("plugins")
                if (
                    not isinstance(installed.get("version"), int)
                    or isinstance(installed.get("version"), bool)
                    or not isinstance(plugins, dict)
                ):
                    return self._failed(
                        package_id,
                        name,
                        marketplace,
                        "invalid",
                        evidence,
                        "installed-metadata-schema-invalid",
                    )
                records = plugins.get(package_id)
                if records is not None:
                    if not isinstance(records, list):
                        return self._failed(
                            package_id,
                            name,
                            marketplace,
                            "invalid",
                            evidence,
                            "installed-records-schema-invalid",
                        )
                    if not records:
                        return self._failed(
                            package_id,
                            name,
                            marketplace,
                            "invalid",
                            evidence,
                            "installed-records-empty",
                        )
                    if len(records) != 1:
                        issues = ["multiple-installed-records"]
                        scopes = {
                            item.get("scope")
                            for item in records
                            if isinstance(item, dict)
                            and isinstance(item.get("scope"), str)
                        }
                        if len(scopes) > 1:
                            issues.append("multiple-installation-scopes")
                        return self._failed(
                            package_id,
                            name,
                            marketplace,
                            "ambiguous",
                            evidence,
                            *issues,
                        )
                    if not isinstance(records[0], dict):
                        return self._failed(
                            package_id,
                            name,
                            marketplace,
                            "invalid",
                            evidence,
                            "installed-record-schema-invalid",
                        )
                    return self._observe_authoritative(
                        package_id,
                        name,
                        marketplace,
                        records[0],
                        evidence,
                    )
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                PackageObservationError,
                RecursionError,
            ):
                return self._failed(
                    package_id,
                    name,
                    marketplace,
                    "invalid",
                    evidence,
                    "installed-metadata-unreadable",
                )

        return self._observe_cache_fallback(
            package_id,
            name,
            marketplace,
            evidence,
        )

    def _observe_authoritative(
        self,
        package_id: str,
        name: str,
        marketplace: str,
        record: dict[str, Any],
        evidence: list[EvidenceReference],
    ) -> PackageObservation:
        try:
            version = safe_component(record.get("version"), "installed version")
            scope = safe_component(record.get("scope"), "installation scope")
            raw_install_path = record.get("installPath")
            if not isinstance(raw_install_path, str) or not raw_install_path:
                raise PackageObservationError("installed path is unavailable")
            install_path = Path(raw_install_path).expanduser()
            if not install_path.is_absolute():
                raise PackageObservationError("installed path is not absolute")
            install_path = absolute_logical_path(install_path)
            expected = self.plugins_root / "cache" / marketplace / name / version
            if not same_path(install_path, expected):
                raise PackageObservationError(
                    "installed path does not match package version"
                )
            if install_path.is_symlink() or not install_path.is_dir():
                raise PackageObservationError("installed package tree is unavailable")

            self._validate_marketplace(
                name,
                marketplace,
                evidence=evidence,
                required=True,
            )
            self._validate_plugin_manifest(
                install_path,
                name,
                version,
                evidence=evidence,
            )
            tree_sha256 = hash_tree_bounded(install_path, limits=self.limits)
            evidence.append(
                EvidenceReference(
                    kind="package-tree",
                    path=str(install_path),
                    sha256=tree_sha256,
                )
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            PackageObservationError,
            RecursionError,
            ValueError,
        ):
            return self._failed(
                package_id,
                name,
                marketplace,
                "invalid",
                evidence,
                "authoritative-evidence-mismatch",
            )

        return PackageObservation(
            provider=self.provider,
            package_id=package_id,
            package_name=name,
            marketplace=marketplace,
            status="verified",
            confidence="authoritative",
            version=version,
            scope=scope,
            install_path=str(install_path),
            tree_sha256=tree_sha256,
            provenance=sorted_provenance(evidence),
        )

    def _observe_cache_fallback(
        self,
        package_id: str,
        name: str,
        marketplace: str,
        evidence: list[EvidenceReference],
    ) -> PackageObservation:
        cache_root = self.plugins_root / "cache" / marketplace / name
        if cache_root.is_symlink() or (cache_root.exists() and not cache_root.is_dir()):
            return self._failed(
                package_id,
                name,
                marketplace,
                "invalid",
                evidence,
                "cache-root-type-invalid",
            )
        try:
            children: list[Path] = []
            if cache_root.is_dir():
                for child in cache_root.iterdir():
                    if len(children) >= self.limits.max_tree_entries:
                        return self._failed(
                            package_id,
                            name,
                            marketplace,
                            "invalid",
                            evidence,
                            "cache-candidate-limit-exceeded",
                        )
                    children.append(child)
            if any(child.is_symlink() or not child.is_dir() for child in children):
                return self._failed(
                    package_id,
                    name,
                    marketplace,
                    "invalid",
                    evidence,
                    "cache-entry-type-invalid",
                )
            candidates = sorted(children, key=lambda item: item.name)
        except OSError:
            return self._failed(
                package_id,
                name,
                marketplace,
                "invalid",
                evidence,
                "cache-unreadable",
            )

        if not candidates:
            return self._failed(
                package_id,
                name,
                marketplace,
                "absent",
                evidence,
                "package-not-observed",
            )
        if len(candidates) != 1:
            return self._failed(
                package_id,
                name,
                marketplace,
                "ambiguous",
                evidence,
                "multiple-cache-candidates",
            )

        install_path = absolute_logical_path(candidates[0])
        try:
            version = safe_component(install_path.name, "cache version")
            self._validate_marketplace(
                name,
                marketplace,
                evidence=evidence,
                required=False,
            )
            self._validate_plugin_manifest(
                install_path,
                name,
                version,
                evidence=evidence,
            )
            tree_sha256 = hash_tree_bounded(install_path, limits=self.limits)
            evidence.append(
                EvidenceReference(
                    kind="package-tree",
                    path=str(install_path),
                    sha256=tree_sha256,
                )
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            PackageObservationError,
            RecursionError,
            ValueError,
        ):
            return self._failed(
                package_id,
                name,
                marketplace,
                "invalid",
                evidence,
                "cache-evidence-mismatch",
            )

        return PackageObservation(
            provider=self.provider,
            package_id=package_id,
            package_name=name,
            marketplace=marketplace,
            status="fallback",
            confidence="cache-only",
            version=version,
            scope=None,
            install_path=str(install_path),
            tree_sha256=tree_sha256,
            provenance=sorted_provenance(evidence),
            issues=("authoritative-install-metadata-missing",),
        )

    def _validate_marketplace(
        self,
        name: str,
        marketplace: str,
        *,
        evidence: list[EvidenceReference],
        required: bool,
    ) -> None:
        known_path = self.plugins_root / "known_marketplaces.json"
        if not known_path.is_file():
            if required:
                raise PackageObservationError("known marketplace metadata is missing")
            return
        known = self._read_json(
            known_path,
            kind="known-marketplaces",
            evidence=evidence,
        )
        entry = known.get(marketplace)
        if entry is None and not required:
            return
        if not isinstance(entry, dict):
            raise PackageObservationError("known marketplace entry is invalid")
        raw_location = entry.get("installLocation")
        if not isinstance(raw_location, str) or not raw_location:
            raise PackageObservationError("marketplace install location is invalid")
        location = Path(raw_location).expanduser()
        if not location.is_absolute():
            raise PackageObservationError(
                "marketplace install location is not absolute"
            )
        location = absolute_logical_path(location)
        if location.is_symlink() or not location.is_dir():
            raise PackageObservationError("marketplace checkout is unavailable")

        manifest_path = location / ".claude-plugin" / "marketplace.json"
        manifest = self._read_json(
            manifest_path,
            kind="marketplace-manifest",
            evidence=evidence,
        )
        plugins = manifest.get("plugins")
        if not isinstance(plugins, list):
            raise PackageObservationError("marketplace plugin list is invalid")
        matches = [
            item
            for item in plugins
            if isinstance(item, dict) and item.get("name") == name
        ]
        if len(matches) != 1:
            raise PackageObservationError(
                "marketplace plugin identity is missing or ambiguous"
            )

    def _validate_plugin_manifest(
        self,
        install_path: Path,
        expected_name: str,
        version: str,
        *,
        evidence: list[EvidenceReference],
    ) -> None:
        manifest_path = install_path / ".claude-plugin" / "plugin.json"
        manifest = self._read_json(
            manifest_path,
            kind="plugin-manifest",
            evidence=evidence,
        )
        if manifest.get("name") != expected_name:
            raise PackageObservationError("plugin manifest name mismatch")
        declared_version = manifest.get("version")
        if declared_version is not None and declared_version != version:
            raise PackageObservationError("plugin manifest version mismatch")

    def _read_json(
        self,
        path: Path,
        *,
        kind: str,
        evidence: list[EvidenceReference],
    ) -> dict[str, Any]:
        content = read_stable_file(
            path,
            max_bytes=self.limits.max_metadata_bytes,
        )
        reference = EvidenceReference(
            kind=kind,
            path=str(absolute_logical_path(path)),
            sha256=sha256(content).hexdigest(),
        )
        evidence.append(reference)
        value = json.loads(content.decode("utf-8"))
        if not isinstance(value, dict):
            raise PackageObservationError(f"{kind} must be a JSON object")
        return value

    def _failed(
        self,
        package_id: str,
        name: str,
        marketplace: str,
        status: str,
        evidence: list[EvidenceReference],
        *issues: str,
    ) -> PackageObservation:
        return PackageObservation(
            provider=self.provider,
            package_id=package_id,
            package_name=name,
            marketplace=marketplace,
            status=status,
            confidence="none",
            version=None,
            scope=None,
            install_path=None,
            tree_sha256=None,
            provenance=sorted_provenance(evidence),
            issues=tuple(sorted(set(issues))),
        )

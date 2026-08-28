from __future__ import annotations

import json
from pathlib import Path

import pytest

from apu.package_adapters import (
    ClaudePackageAdapter,
    ObservationLimits,
)
from apu.package_adapters.base import split_package_id

PACKAGE_ID = "superpowers@official"


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return path


def _plugins_root(home: Path) -> Path:
    return home / ".claude" / "plugins"


def _cache(
    home: Path,
    *,
    version: str,
    manifest_version: str | None = None,
    manifest_name: str = "superpowers",
    content: str = "package evidence",
) -> Path:
    root = _plugins_root(home) / "cache" / "official" / "superpowers" / version
    manifest: dict[str, object] = {"name": manifest_name}
    if manifest_version is not None:
        manifest["version"] = manifest_version
    _write_json(root / ".claude-plugin" / "plugin.json", manifest)
    (root / "skills").mkdir()
    (root / "skills" / "SKILL.md").write_text(content, encoding="utf-8")
    return root


def _marketplace(
    home: Path,
    *,
    plugins: list[dict[str, object]] | None = None,
) -> Path:
    checkout = _plugins_root(home) / "marketplaces" / "official"
    _write_json(
        checkout / ".claude-plugin" / "marketplace.json",
        {
            "name": "official",
            "plugins": (
                [{"name": "superpowers", "version": "10.0"}]
                if plugins is None
                else plugins
            ),
        },
    )
    _write_json(
        _plugins_root(home) / "known_marketplaces.json",
        {
            "official": {
                "installLocation": str(checkout.resolve()),
                "source": {"source": "github", "repo": "example/plugins"},
            }
        },
    )
    return checkout


def _installed(
    home: Path,
    records: list[dict[str, object]] | None,
) -> Path:
    plugins: dict[str, object] = {}
    if records is not None:
        plugins[PACKAGE_ID] = records
    return _write_json(
        _plugins_root(home) / "installed_plugins.json",
        {"version": 2, "plugins": plugins},
    )


def _record(path: Path, *, version: str, scope: str = "user") -> dict[str, object]:
    return {
        "installPath": str(path.resolve()),
        "installedAt": "2026-08-01T00:00:00Z",
        "scope": scope,
        "version": version,
    }


def test_authoritative_metadata_beats_lexical_cache_order_and_records_hashes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    selected = _cache(
        home,
        version="9.0",
        manifest_version="9.0",
        content="DO_NOT_EMIT_THIS_CONTENT",
    )
    _cache(home, version="10.0", manifest_version="10.0")
    _marketplace(home)
    _installed(home, [_record(selected, version="9.0")])

    observation = ClaudePackageAdapter(home).observe(PACKAGE_ID)

    assert observation.status == "verified"
    assert observation.confidence == "authoritative"
    assert observation.version == "9.0"
    assert observation.scope == "user"
    assert observation.install_path == str(selected.resolve())
    assert len(observation.tree_sha256 or "") == 64
    assert {item.kind for item in observation.provenance} == {
        "installed-plugins",
        "known-marketplaces",
        "marketplace-manifest",
        "package-tree",
        "plugin-manifest",
    }
    serialized = json.dumps(observation.to_dict())
    assert "DO_NOT_EMIT_THIS_CONTENT" not in serialized
    assert set(observation.to_dict()) == {
        "provider",
        "package_id",
        "package_name",
        "marketplace",
        "status",
        "confidence",
        "version",
        "scope",
        "install_path",
        "tree_sha256",
        "provenance",
        "issues",
        "schema_version",
    }


def test_multiple_installed_scopes_fail_closed_without_selecting_a_tree(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    user = _cache(home, version="1.0", manifest_version="1.0")
    project = _cache(home, version="2.0", manifest_version="2.0")
    _marketplace(home)
    _installed(
        home,
        [
            _record(user, version="1.0", scope="user"),
            _record(project, version="2.0", scope="project"),
        ],
    )

    observation = ClaudePackageAdapter(home).observe(PACKAGE_ID)

    assert observation.status == "ambiguous"
    assert observation.confidence == "none"
    assert observation.version is None
    assert observation.install_path is None
    assert observation.tree_sha256 is None
    assert observation.issues == (
        "multiple-installation-scopes",
        "multiple-installed-records",
    )


@pytest.mark.parametrize(
    "mismatch",
    (
        "metadata-path",
        "plugin-name",
        "plugin-version",
        "marketplace-identity",
    ),
)
def test_authoritative_version_path_and_manifest_mismatches_fail_closed(
    tmp_path: Path,
    mismatch: str,
) -> None:
    home = tmp_path / mismatch
    selected = _cache(
        home,
        version="1.0",
        manifest_name=(
            "another-plugin" if mismatch == "plugin-name" else "superpowers"
        ),
        manifest_version=("2.0" if mismatch == "plugin-version" else "1.0"),
    )
    other = _cache(home, version="2.0", manifest_version="2.0")
    _marketplace(
        home,
        plugins=(
            [{"name": "another-plugin"}]
            if mismatch == "marketplace-identity"
            else [{"name": "superpowers"}]
        ),
    )
    record_path = other if mismatch == "metadata-path" else selected
    _installed(home, [_record(record_path, version="1.0")])

    observation = ClaudePackageAdapter(home).observe(PACKAGE_ID)

    assert observation.status == "invalid"
    assert observation.confidence == "none"
    assert observation.tree_sha256 is None
    assert observation.issues == ("authoritative-evidence-mismatch",)


def test_exactly_one_cache_candidate_is_a_lower_confidence_fallback(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    selected = _cache(home, version="3.2.1", manifest_version="3.2.1")

    observation = ClaudePackageAdapter(home).observe(PACKAGE_ID)

    assert observation.status == "fallback"
    assert observation.confidence == "cache-only"
    assert observation.version == "3.2.1"
    assert observation.scope is None
    assert observation.install_path == str(selected.resolve())
    assert observation.issues == ("authoritative-install-metadata-missing",)
    assert {item.kind for item in observation.provenance} == {
        "package-tree",
        "plugin-manifest",
    }


def test_multiple_cache_candidates_are_ambiguous_not_lexically_selected(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _cache(home, version="9.0", manifest_version="9.0")
    _cache(home, version="10.0", manifest_version="10.0")
    _installed(home, None)

    observation = ClaudePackageAdapter(home).observe(PACKAGE_ID)

    assert observation.status == "ambiguous"
    assert observation.version is None
    assert observation.install_path is None
    assert observation.issues == ("multiple-cache-candidates",)


def test_malformed_authoritative_metadata_never_falls_back_to_cache(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _cache(home, version="1.0", manifest_version="1.0")
    installed = _plugins_root(home) / "installed_plugins.json"
    installed.write_text("not-json", encoding="utf-8")

    observation = ClaudePackageAdapter(home).observe(PACKAGE_ID)

    assert observation.status == "invalid"
    assert observation.confidence == "none"
    assert observation.issues == ("installed-metadata-unreadable",)
    assert {item.kind for item in observation.provenance} == {"installed-plugins"}


def test_bounded_tree_failure_is_reported_without_partial_selection(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    selected = _cache(home, version="1.0", manifest_version="1.0")
    _marketplace(home)
    _installed(home, [_record(selected, version="1.0")])

    observation = ClaudePackageAdapter(
        home,
        limits=ObservationLimits(max_tree_bytes=4),
    ).observe(PACKAGE_ID)

    assert observation.status == "invalid"
    assert observation.tree_sha256 is None
    assert observation.issues == ("authoritative-evidence-mismatch",)


def test_cache_candidate_scan_is_bounded_before_selection(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _cache(home, version="1.0", manifest_version="1.0")
    _cache(home, version="2.0", manifest_version="2.0")

    observation = ClaudePackageAdapter(
        home,
        limits=ObservationLimits(max_tree_entries=1),
    ).observe(PACKAGE_ID)

    assert observation.status == "invalid"
    assert observation.version is None
    assert observation.issues == ("cache-candidate-limit-exceeded",)


def test_package_tree_link_escape_fails_closed_when_symlinks_are_available(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    selected = _cache(home, version="1.0", manifest_version="1.0")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = selected / "escape"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    _marketplace(home)
    _installed(home, [_record(selected, version="1.0")])

    observation = ClaudePackageAdapter(home).observe(PACKAGE_ID)

    assert observation.status == "invalid"
    assert observation.tree_sha256 is None


def test_observation_is_read_only_and_invalid_identifiers_are_rejected(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    selected = _cache(home, version="1.0", manifest_version="1.0")
    _marketplace(home)
    _installed(home, [_record(selected, version="1.0")])
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in home.rglob("*")
        if path.is_file()
    }

    ClaudePackageAdapter(home).observe(PACKAGE_ID)

    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in home.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not [path for path in home.rglob("*") if path.name.startswith(".apu")]
    with pytest.raises(ValueError, match="PACKAGE@SOURCE"):
        ClaudePackageAdapter(home).observe("not-qualified")
    with pytest.raises(ValueError, match="invalid claude package name"):
        ClaudePackageAdapter(home).observe("../escape@official")


def test_absent_package_has_no_selected_evidence(tmp_path: Path) -> None:
    observation = ClaudePackageAdapter(tmp_path / "home").observe(PACKAGE_ID)

    assert observation.status == "absent"
    assert observation.confidence == "none"
    assert observation.version is None
    assert observation.provenance == ()
    assert observation.issues == ("package-not-observed",)


def test_scoped_package_identity_splits_on_the_final_at_sign() -> None:
    assert split_package_id("@openai/tools@official") == (
        "@openai/tools",
        "official",
    )

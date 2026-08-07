from __future__ import annotations

import json
from pathlib import Path

import pytest

import apu.package_research as package_research_module
from apu.classify import DetectorPolicy
from apu.models import sha256_bytes, sha256_json
from apu.package_coordinates import parse_profile_package
from apu.package_research import (
    PackageResearchError,
    _verify_candidate_artifacts,
    research_package,
)
from apu.package_state import store_candidate_tree, write_package_leaf


def _claude_package_fixture(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    plugins = home / ".claude" / "plugins"
    installed_tree = (
        plugins
        / "cache"
        / "claude-plugins-official"
        / "superpowers"
        / "5.0.7"
    )
    skill = installed_tree / "skills" / "using-superpowers"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "You must use this skill before every task.",
        encoding="utf-8",
    )
    (installed_tree / ".claude-plugin").mkdir()
    (installed_tree / ".claude-plugin" / "plugin.json").write_text(
        '{"name":"superpowers","version":"5.0.7"}',
        encoding="utf-8",
    )
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "superpowers@claude-plugins-official": [
                        {
                            "scope": "user",
                            "installPath": str(installed_tree),
                            "version": "5.0.7",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    marketplace = (
        plugins / "marketplaces" / "claude-plugins-official"
    )
    manifest = marketplace / ".claude-plugin" / "marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "plugins": [
                    {
                        "name": "superpowers",
                        "source": {
                            "source": "url",
                            "url": "https://github.com/example/superpowers.git",
                            "sha": "a" * 40,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (plugins / "known_marketplaces.json").write_text(
        json.dumps(
            {
                "claude-plugins-official": {
                    "installLocation": str(marketplace)
                }
            }
        ),
        encoding="utf-8",
    )
    return home, installed_tree


def _candidate_resolver(tmp_path: Path):
    def resolve(**kwargs):
        source = tmp_path / "candidate"
        skill = source / "skills" / "using-superpowers"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "Use this skill when the task needs its workflow.",
            encoding="utf-8",
        )
        manifest = source / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir()
        manifest.write_text(
            '{"name":"superpowers","version":"6.2.0"}',
            encoding="utf-8",
        )
        tree_hash, tree = store_candidate_tree(kwargs["state_home"], source)
        normalization = {
            "policy": "virtual-internal-file-links-v1",
            "links": [],
        }
        artifact = {
            "schema_version": 1,
            "artifact_type": "package-candidate",
            "package_id": kwargs["package_id"],
            "status": "available",
            "version": "6.2.0",
            "immutable_ref": {
                "tag": "v6.2.0",
                "commit_oid": "b" * 40,
                "archive_sha256": "d" * 64,
                "content_tree_sha256": tree_hash,
                "tree_sha256": sha256_json(
                    {
                        "content_tree_sha256": tree_hash,
                        "normalization": normalization,
                    }
                ),
            },
            "retrieval": {
                "retrieved_at": kwargs["retrieved_at"],
                "source_kind": "github-commit-archive",
                "source_url": kwargs["source_url"],
                "archive_url": (
                    "https://codeload.github.com/example/superpowers/zip/"
                    + "b" * 40
                ),
            },
            "normalization": normalization,
            "changelog": {
                "relative_path": None,
                "content_sha256": None,
            },
        }
        _, path = write_package_leaf(
            kwargs["state_home"],
            kind="candidates",
            package_id=kwargs["package_id"],
            value=artifact,
        )
        return artifact, path, tree

    return resolve


def test_research_persists_honest_improved_report_without_live_mutation(
    tmp_path: Path,
) -> None:
    home, installed_tree = _claude_package_fixture(tmp_path)
    before = {
        path.relative_to(installed_tree).as_posix(): path.read_bytes()
        for path in installed_tree.rglob("*")
        if path.is_file()
    }
    state = tmp_path / "state"

    report, report_path = research_package(
        parse_profile_package("superpowers@claude-plugins-official"),
        home=home,
        state_home=state,
        profile_sha256="c" * 64,
        detector_policy=DetectorPolicy(),
        baseline_stamp={
            "version": None,
            "status": "unconfigured",
            "retrieved_at": None,
            "artifact_sha256": None,
        },
        researched_at="2026-08-07T02:00:00Z",
        candidate_resolver=_candidate_resolver(tmp_path),
    )

    assert report["classifier_comparison"]["delta"]["verdict"] == "improved"
    assert report["recommendation"] == {
        "decision": "work-order",
        "reason_codes": [
            "candidate-fixtures-unverified",
            "provider-pin-unsupported",
            "static-classifier-improved",
        ],
        "eligible_for_pin_plan": False,
        "mutation_status": "unavailable-provider-pin-unsupported",
    }
    assert report["efficacy"]["attribution_status"] == "none"
    assert report_path.is_file()
    assert {
        path.relative_to(installed_tree).as_posix(): path.read_bytes()
        for path in installed_tree.rglob("*")
        if path.is_file()
    } == before
    rendered_report = report_path.read_text(encoding="utf-8").lower()
    assert "always use this skill" not in rendered_report
    assert "use this skill when" not in rendered_report


def test_research_fails_before_state_when_installation_is_ambiguous(
    tmp_path: Path,
) -> None:
    home, installed_tree = _claude_package_fixture(tmp_path)
    metadata_path = home / ".claude" / "plugins" / "installed_plugins.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["plugins"]["superpowers@claude-plugins-official"].append(
        {
            "scope": "project",
            "installPath": str(installed_tree),
            "version": "5.0.7",
        }
    )
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    state = tmp_path / "state"

    with pytest.raises(PackageResearchError, match="not authoritative"):
        research_package(
            parse_profile_package("superpowers@claude-plugins-official"),
            home=home,
            state_home=state,
            profile_sha256="c" * 64,
            detector_policy=DetectorPolicy(),
            baseline_stamp={
                "version": None,
                "status": "unconfigured",
                "retrieved_at": None,
                "artifact_sha256": None,
            },
        )

    assert not state.exists()


def test_research_rejects_a_tampered_content_addressed_candidate(
    tmp_path: Path,
) -> None:
    home, _ = _claude_package_fixture(tmp_path)
    state = tmp_path / "state"

    def tampered_candidate(**kwargs):
        source = tmp_path / "candidate"
        source.mkdir()
        (source / "SKILL.md").write_text("candidate", encoding="utf-8")
        manifest = source / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir()
        manifest.write_text(
            '{"name":"superpowers","version":"6.2.0"}',
            encoding="utf-8",
        )
        tree_hash, tree = store_candidate_tree(kwargs["state_home"], source)
        normalization = {
            "policy": "virtual-internal-file-links-v1",
            "links": [],
        }
        artifact = {
            "schema_version": 1,
            "artifact_type": "package-candidate",
            "package_id": kwargs["package_id"],
            "status": "available",
            "version": "6.2.0",
            "immutable_ref": {
                "tag": "v6.2.0",
                "commit_oid": "b" * 40,
                "archive_sha256": "d" * 64,
                "content_tree_sha256": tree_hash,
                "tree_sha256": sha256_json(
                    {
                        "content_tree_sha256": tree_hash,
                        "normalization": normalization,
                    }
                ),
            },
            "retrieval": {
                "retrieved_at": kwargs["retrieved_at"],
                "source_kind": "github-commit-archive",
                "source_url": kwargs["source_url"],
                "archive_url": (
                    "https://codeload.github.com/example/superpowers/zip/"
                    + "b" * 40
                ),
            },
            "normalization": normalization,
            "changelog": {
                "relative_path": None,
                "content_sha256": None,
            },
        }
        _, path = write_package_leaf(
            kwargs["state_home"],
            kind="candidates",
            package_id=kwargs["package_id"],
            value=artifact,
        )
        (tree / "tampered").write_text("changed", encoding="utf-8")
        return artifact, path, tree

    with pytest.raises(PackageResearchError, match="identity"):
        research_package(
            parse_profile_package("superpowers@claude-plugins-official"),
            home=home,
            state_home=state,
            profile_sha256="c" * 64,
            detector_policy=DetectorPolicy(),
            baseline_stamp={
                "version": None,
                "status": "unconfigured",
                "retrieved_at": None,
                "artifact_sha256": None,
            },
            researched_at="2026-08-07T02:00:00Z",
            candidate_resolver=tampered_candidate,
        )


def test_research_rejects_candidate_manifest_for_another_package(
    tmp_path: Path,
) -> None:
    home, _ = _claude_package_fixture(tmp_path)
    state = tmp_path / "state"

    def wrong_manifest_candidate(**kwargs):
        source = tmp_path / "candidate"
        manifest = source / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            '{"name":"another-plugin","version":"6.2.0"}',
            encoding="utf-8",
        )
        tree_hash, tree = store_candidate_tree(kwargs["state_home"], source)
        normalization = {
            "policy": "virtual-internal-file-links-v1",
            "links": [],
        }
        artifact = {
            "schema_version": 1,
            "artifact_type": "package-candidate",
            "package_id": kwargs["package_id"],
            "status": "available",
            "version": "6.2.0",
            "immutable_ref": {
                "tag": "v6.2.0",
                "commit_oid": "b" * 40,
                "archive_sha256": "d" * 64,
                "content_tree_sha256": tree_hash,
                "tree_sha256": sha256_json(
                    {
                        "content_tree_sha256": tree_hash,
                        "normalization": normalization,
                    }
                ),
            },
            "retrieval": {
                "retrieved_at": kwargs["retrieved_at"],
                "source_kind": "github-commit-archive",
                "source_url": kwargs["source_url"],
                "archive_url": (
                    "https://codeload.github.com/example/superpowers/zip/"
                    + "b" * 40
                ),
            },
            "normalization": normalization,
            "changelog": {
                "relative_path": None,
                "content_sha256": None,
            },
        }
        _, path = write_package_leaf(
            kwargs["state_home"],
            kind="candidates",
            package_id=kwargs["package_id"],
            value=artifact,
        )
        return artifact, path, tree

    with pytest.raises(PackageResearchError, match="manifest identity"):
        research_package(
            parse_profile_package("superpowers@claude-plugins-official"),
            home=home,
            state_home=state,
            profile_sha256="c" * 64,
            detector_policy=DetectorPolicy(),
            baseline_stamp={
                "version": None,
                "status": "unconfigured",
                "retrieved_at": None,
                "artifact_sha256": None,
            },
            researched_at="2026-08-07T02:00:00Z",
            candidate_resolver=wrong_manifest_candidate,
        )


def test_research_aborts_if_trees_change_during_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, installed_tree = _claude_package_fixture(tmp_path)
    state = tmp_path / "state"
    original = package_research_module.compare_package_versions

    def mutating_comparison(installed_root, candidate_root, **kwargs):
        result = original(installed_root, candidate_root, **kwargs)
        (Path(candidate_root) / "changed-during-analysis").write_text(
            "changed",
            encoding="utf-8",
        )
        (
            installed_tree
            / "skills"
            / "using-superpowers"
            / "SKILL.md"
        ).write_text("changed", encoding="utf-8")
        return result

    monkeypatch.setattr(
        package_research_module,
        "compare_package_versions",
        mutating_comparison,
    )
    with pytest.raises(PackageResearchError, match="changed while classifier"):
        research_package(
            parse_profile_package("superpowers@claude-plugins-official"),
            home=home,
            state_home=state,
            profile_sha256="c" * 64,
            detector_policy=DetectorPolicy(),
            baseline_stamp={
                "version": None,
                "status": "unconfigured",
                "retrieved_at": None,
                "artifact_sha256": None,
            },
            researched_at="2026-08-07T02:00:00Z",
            candidate_resolver=_candidate_resolver(tmp_path),
        )

    for kind in ("observations", "analyses", "reports"):
        assert not (state / "packages" / kind).exists()


def test_research_aborts_if_provider_authority_changes_after_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, _ = _claude_package_fixture(tmp_path)
    state = tmp_path / "state"
    original = package_research_module.ClaudePackageAdapter.observe
    calls = 0

    def drifting_observation(adapter, package_id):
        nonlocal calls
        observed = original(adapter, package_id)
        calls += 1
        if calls == 3:
            path = home / ".claude" / "plugins" / "installed_plugins.json"
            metadata = json.loads(path.read_text(encoding="utf-8"))
            metadata["plugins"][
                "superpowers@claude-plugins-official"
            ][0]["version"] = "5.0.8"
            path.write_text(json.dumps(metadata), encoding="utf-8")
        return observed

    monkeypatch.setattr(
        package_research_module.ClaudePackageAdapter,
        "observe",
        drifting_observation,
    )
    with pytest.raises(PackageResearchError, match="authority changed"):
        research_package(
            parse_profile_package("superpowers@claude-plugins-official"),
            home=home,
            state_home=state,
            profile_sha256="c" * 64,
            detector_policy=DetectorPolicy(),
            baseline_stamp={
                "version": None,
                "status": "unconfigured",
                "retrieved_at": None,
                "artifact_sha256": None,
            },
            researched_at="2026-08-07T02:00:00Z",
            candidate_resolver=_candidate_resolver(tmp_path),
        )

    for kind in ("observations", "analyses", "reports"):
        assert not (state / "packages" / kind).exists()


@pytest.mark.parametrize(
    ("defect", "message"),
    (
        ("archive-url", "retrieval provenance"),
        ("link-relation", "link relation"),
    ),
)
def test_candidate_leaf_rejects_false_upstream_provenance(
    tmp_path: Path,
    defect: str,
    message: str,
) -> None:
    coordinate = parse_profile_package(
        "superpowers@claude-plugins-official"
    )
    state = tmp_path / "state"
    source = tmp_path / "candidate"
    source.mkdir()
    content = b"Always invoke a skill before every response."
    (source / "CLAUDE.md").write_bytes(content)
    manifest = source / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text(
        '{"name":"superpowers","version":"6.2.0"}',
        encoding="utf-8",
    )
    content_hash, tree = store_candidate_tree(state, source)
    links = []
    if defect == "link-relation":
        links = [
            {
                "relative_path": "AGENTS.md",
                "target": "OTHER.md",
                "resolved_target": "CLAUDE.md",
                "target_content_sha256": sha256_bytes(content),
            }
        ]
    normalization = {
        "policy": "virtual-internal-file-links-v1",
        "links": links,
    }
    artifact = {
        "schema_version": 1,
        "artifact_type": "package-candidate",
        "package_id": coordinate.package_id,
        "status": "available",
        "version": "6.2.0",
        "immutable_ref": {
            "tag": "v6.2.0",
            "commit_oid": "b" * 40,
            "archive_sha256": "d" * 64,
            "content_tree_sha256": content_hash,
            "tree_sha256": sha256_json(
                {
                    "content_tree_sha256": content_hash,
                    "normalization": normalization,
                }
            ),
        },
        "retrieval": {
            "retrieved_at": "2026-08-07T02:00:00Z",
            "source_kind": "github-commit-archive",
            "source_url": "https://github.com/example/superpowers.git",
            "archive_url": (
                "https://codeload.github.com/another/repository/zip/"
                + "b" * 40
                if defect == "archive-url"
                else "https://codeload.github.com/example/superpowers/zip/"
                + "b" * 40
            ),
        },
        "normalization": normalization,
        "changelog": {
            "relative_path": None,
            "content_sha256": None,
        },
    }
    _, artifact_path = write_package_leaf(
        state,
        kind="candidates",
        package_id=coordinate.package_id,
        value=artifact,
    )

    with pytest.raises(PackageResearchError, match=message):
        _verify_candidate_artifacts(
            coordinate,
            state_home=state,
            candidate=artifact,
            candidate_path=artifact_path,
            candidate_tree=tree,
            expected_source_url="https://github.com/example/superpowers.git",
        )

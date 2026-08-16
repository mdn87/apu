from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import apu.adapters.base as adapter_base
from apu.model_registry import (
    ModelObservation,
    PublishedModelSource,
    model_registry_artifact_sha256,
    refresh_model_registry,
)
from apu.system_audit import (
    SYSTEM_INVENTORY_SCHEMA_VERSION,
    EvaluationContext,
    SystemInventory,
    audit_system,
    discover_repositories,
    load_evaluation_context,
    verify_evaluation_context,
)
from apu.system_profile import ProfileRoot, SystemProfile


def write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def profile_for(root: Path, home: Path, *excludes: str) -> SystemProfile:
    return SystemProfile.from_dict(
        {
            "roots": [{"path": str(root), "excludes": list(excludes)}],
            "global_surfaces": [
                str(home / ".claude"),
                str(home / ".codex"),
                str(home / ".agents"),
            ],
        },
        home=home,
    )


def test_repository_discovery_is_deterministic_and_honors_excludes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "projects"
    for name in ("zeta", "alpha", "ignored"):
        (root / name / ".git").mkdir(parents=True)
    (root / "ordinary" / "nested").mkdir(parents=True)

    result = discover_repositories(
        (ProfileRoot(str(root.resolve()), ("ignored",)),)
    )

    assert result.repositories == (
        str((root / "alpha").resolve()),
        str((root / "zeta").resolve()),
    )
    assert result.issues == ()
    assert discover_repositories(
        (ProfileRoot(str(root.resolve()), ("ignored",)),)
    ) == result


def test_system_audit_applies_profile_scope_before_reading_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repository = tmp_path / "projects" / "repo"
    (repository / ".git").mkdir(parents=True)
    global_instruction = write(home / ".claude" / "CLAUDE.md", "global\n")
    excluded_global = write(home / ".claude" / "settings.json", "{}\n")
    included_repository = write(repository / "AGENTS.md", "repository\n")
    excluded_repository = write(
        repository / "fixtures" / "AGENTS.md",
        "fixture\n",
    )
    profile = SystemProfile.from_dict(
        {
            "roots": [
                {
                    "path": str(repository),
                    "excludes": ["fixtures"],
                }
            ],
            "global_surfaces": [str(global_instruction)],
        },
        home=home,
    )
    reads: list[Path] = []
    walked: list[Path] = []
    real_read_bytes = adapter_base.read_bytes
    real_walk = adapter_base.os.walk

    def traced_read_bytes(path: Path) -> bytes | None:
        reads.append(path)
        return real_read_bytes(path)

    def traced_walk(*args, **kwargs):
        for item in real_walk(*args, **kwargs):
            walked.append(Path(item[0]))
            yield item

    monkeypatch.setattr(adapter_base, "read_bytes", traced_read_bytes)
    monkeypatch.setattr(adapter_base.os, "walk", traced_walk)

    result = audit_system(profile, home=home)

    child_paths = {
        Path(surface.path)
        for surface in result.repositories[0].inventory.surfaces
    }
    assert global_instruction in child_paths
    assert included_repository in child_paths
    assert excluded_global not in reads
    assert excluded_repository not in reads
    assert excluded_global not in child_paths
    assert excluded_repository not in child_paths
    assert excluded_repository.parent not in walked


def test_repository_discovery_does_not_follow_links_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "projects"
    outside = tmp_path / "outside"
    (outside / "repo" / ".git").mkdir(parents=True)
    root.mkdir()
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    result = discover_repositories((ProfileRoot(str(root.resolve())),))

    assert result.repositories == ()
    assert any(issue.kind == "outside-root" for issue in result.issues)


def test_repository_discovery_is_cycle_safe(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    repo = root / "repo"
    (repo / ".git").mkdir(parents=True)
    link = repo / "loop"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    result = discover_repositories((ProfileRoot(str(root.resolve())),))

    assert result.repositories == (str(repo.resolve()),)
    assert any(issue.kind == "cycle" for issue in result.issues)


def test_missing_root_is_reported_without_aborting_other_roots(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "valid"
    (valid / "repo" / ".git").mkdir(parents=True)
    missing = tmp_path / "missing"

    result = discover_repositories(
        (
            ProfileRoot(str(missing.resolve())),
            ProfileRoot(str(valid.resolve())),
        )
    )

    assert result.repositories == (str((valid / "repo").resolve()),)
    assert len(result.issues) == 1
    assert result.issues[0].kind == "unreadable"


def test_system_audit_deduplicates_global_findings_but_keeps_local_conflicts(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    cascade = home / "projects"
    repo = cascade / "repo"
    (repo / ".git").mkdir(parents=True)
    text = "You must invoke a workflow skill at the start of every conversation.\n"
    write(home / ".codex" / "AGENTS.md", text)
    local = write(repo / "AGENTS.md", text)

    result = audit_system(
        profile_for(cascade, home),
        home=home,
        generated_at="2026-08-06T12:00:00Z",
    )

    assert len(result.repositories) == 1
    assert any(
        finding.category == "universal-skill-trigger"
        for finding in result.machine_inventory.findings
    )
    child = result.repositories[0].inventory
    matching = [
        finding
        for finding in child.findings
        if finding.category == "universal-skill-trigger"
    ]
    assert len(matching) == 1
    local_surface = next(
        surface for surface in child.surfaces if surface.path == str(local)
    )
    assert matching[0].surface_id == local_surface.id


def test_system_inventory_round_trips_and_contains_one_child_per_repo(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    root = tmp_path / "projects"
    for name in ("b", "a"):
        repo = root / name
        (repo / ".git").mkdir(parents=True)
        write(repo / "CLAUDE.md", f"{name}\n")

    result = audit_system(
        profile_for(root, home),
        home=home,
        generated_at="2026-08-06T12:00:00Z",
    )
    encoded = json.loads(json.dumps(result.to_dict()))

    assert [Path(item.repository).name for item in result.repositories] == [
        "a",
        "b",
    ]
    assert SystemInventory.from_dict(encoded) == result
    assert result.artifact_sha256 == SystemInventory.from_dict(
        encoded
    ).artifact_sha256
    assert result.schema_version == SYSTEM_INVENTORY_SCHEMA_VERSION
    assert result.evaluation_context == EvaluationContext.unconfigured()


def test_v2_inventory_requires_strict_evaluation_context(
    tmp_path: Path,
) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    result = audit_system(profile_for(root, tmp_path / "home"))
    value = result.to_dict()
    value["evaluation_context"]["baseline"]["artifact_sha256"] = "forged"

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        SystemInventory.from_dict(value)


def test_v2_inventory_rejects_missing_and_unknown_fields(
    tmp_path: Path,
) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    value = audit_system(profile_for(root, tmp_path / "home")).to_dict()

    missing = dict(value)
    missing.pop("profile_sha256")
    with pytest.raises(ValueError, match="missing fields: profile_sha256"):
        SystemInventory.from_dict(missing)

    unknown = dict(value)
    unknown["surprise"] = True
    with pytest.raises(ValueError, match="unsupported fields: surprise"):
        SystemInventory.from_dict(unknown)


def test_evaluation_context_rejects_local_model_drift_after_audit(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    observed = ModelObservation(
        runtime_id="codex-cli",
        provider="openai",
        cli_version="1.0.0",
        configured_model="gpt-test",
        raw_alias="gpt-test",
        observed_at="2026-08-07T04:00:00Z",
    )
    registry = refresh_model_registry(
        state,
        (observed,),
        {
            "openai": PublishedModelSource(
                provider="openai",
                source_url="https://api.example.test/v1/models",
            )
        },
        fetcher=lambda _source: {"models": ["gpt-test"]},
        attempted_at="2026-08-07T04:01:00Z",
    )
    context = load_evaluation_context(state)
    assert context.models["artifact_sha256"] == (
        model_registry_artifact_sha256(registry)
    )
    same = replace(observed, observed_at="2026-08-07T04:02:00Z")
    verify_evaluation_context(
        state,
        context,
        model_observations=(same,),
    )

    changed = replace(
        same,
        cli_version="2.0.0",
        configured_model="gpt-next",
        raw_alias="gpt-next",
    )
    with pytest.raises(ValueError, match="changed after audit"):
        verify_evaluation_context(
            state,
            context,
            model_observations=(changed,),
        )


def test_legacy_inventory_reads_as_explicitly_unverified(
    tmp_path: Path,
) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    value = audit_system(profile_for(root, tmp_path / "home")).to_dict()
    value["schema_version"] = 1
    value.pop("evaluation_context")

    loaded = SystemInventory.from_dict(value)

    assert loaded.schema_version == 1
    assert loaded.evaluation_context == EvaluationContext.legacy_unverified()
    assert SystemInventory.from_dict(loaded.to_dict()) == loaded
    assert "evaluation_context" not in loaded.to_dict()

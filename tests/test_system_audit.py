from __future__ import annotations

import json
from pathlib import Path

import pytest

from apu.system_audit import (
    SystemInventory,
    audit_system,
    discover_repositories,
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

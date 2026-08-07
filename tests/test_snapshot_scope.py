from __future__ import annotations

from pathlib import Path

from apu.snapshot_scope import snapshot_surfaces_for_profile
from apu.system_profile import SystemProfile


def test_snapshot_scope_keeps_global_roots_and_project_policy_objects(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    global_root = home / ".codex"
    (global_root / "nested").mkdir(parents=True)
    repository = tmp_path / "projects" / "repo"
    (repository / ".git").mkdir(parents=True)
    policy = repository / "AGENTS.md"
    policy.write_text("policy\n", encoding="utf-8")
    profile = SystemProfile.from_dict(
        {
            "roots": [str(tmp_path / "projects")],
            "global_surfaces": [
                str(global_root),
                str(global_root / "nested"),
            ],
        },
        home=home,
    )

    surfaces, inventory = snapshot_surfaces_for_profile(
        profile,
        home=home,
        generated_at="2026-08-06T12:00:00Z",
    )

    roots = {surface.root for surface in surfaces}
    assert global_root in roots
    assert global_root / "nested" not in roots
    assert policy in roots
    assert len(inventory.repositories) == 1


def test_snapshot_scope_captures_whole_project_skill_tree(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repository = tmp_path / "projects" / "repo"
    (repository / ".git").mkdir(parents=True)
    skill = repository / ".agents" / "skills" / "reviewer"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: reviewer\n---\n", encoding="utf-8"
    )
    (skill / "script.py").write_text("print('review')\n", encoding="utf-8")
    profile = SystemProfile.from_dict(
        {
            "roots": [str(tmp_path / "projects")],
            "global_surfaces": [str(home / ".codex")],
        },
        home=home,
    )

    surfaces, _ = snapshot_surfaces_for_profile(profile, home=home)

    assert skill in {surface.root for surface in surfaces}

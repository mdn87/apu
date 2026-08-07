from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from apu.discovery import discover


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def by_path(result) -> dict[str, object]:
    return {surface.path: surface for surface in result.surfaces}


def test_codex_discovery_keeps_logical_symlink_identity_and_skill_metadata(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    nested = repo / "services" / "api"
    nested.mkdir(parents=True)

    write(home / ".codex" / "AGENTS.md", "global")
    write(repo / "AGENTS.md", "repository")
    write(repo / "services" / "AGENTS.md", "service")
    canonical = write(tmp_path / "policy" / "AGENTS.md", "linked")
    (nested / "AGENTS.md").symlink_to(canonical)
    skill = write(
        home / ".agents" / "skills" / "optimizer" / "SKILL.md",
        "---\nname: optimizer\n---\n",
    )
    manifest = write(
        skill.parent / "agents" / "openai.yaml",
        "interface:\n  display_name: Optimizer\n",
    )

    result = discover([repo], home=home, working_directories=[nested])
    surfaces = by_path(result)

    linked = surfaces[str(nested / "AGENTS.md")]
    assert linked.is_symlink is True
    assert linked.real_path == str(canonical)
    assert linked.content_sha256 == sha256(b"linked").hexdigest()
    assert surfaces[str(skill)].kind == "skill"
    assert surfaces[str(manifest)].kind == "skill-manifest"
    assert any(
        relationship.type == "manifest_for"
        and relationship.from_surface_id == surfaces[str(manifest)].id
        and relationship.to_surface_id == surfaces[str(skill)].id
        for relationship in result.relationships
    )

    codex_stack = next(
        stack
        for stack in result.effective_stacks
        if stack["provider"] == "codex"
    )
    assert codex_stack["surface_ids"] == [
        surfaces[str(home / ".codex" / "AGENTS.md")].id,
        surfaces[str(repo / "AGENTS.md")].id,
        surfaces[str(repo / "services" / "AGENTS.md")].id,
        linked.id,
    ]


def test_claude_discovery_reports_imports_rules_skills_hooks_and_marketplaces(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    cwd = repo / "src" / "service"
    cwd.mkdir(parents=True)

    user_main = write(home / ".claude" / "CLAUDE.md", "user main")
    user_rule = write(home / ".claude" / "rules" / "user.md", "user rule")
    project_main = write(
        repo / "CLAUDE.md",
        "project\n@.claude/imported.md\n@.claude/missing.md\n",
    )
    project_local = write(repo / "CLAUDE.local.md", "local")
    imported = write(
        repo / ".claude" / "imported.md",
        "@nested.md\nimported body\n",
    )
    nested_import = write(repo / ".claude" / "nested.md", "@imported.md\n")
    applicable_rule = write(
        repo / ".claude" / "rules" / "python.md",
        '---\npaths:\n  - "src/**"\n---\npython rule\n',
    )
    ignored_rule = write(
        repo / ".claude" / "rules" / "docs.md",
        '---\npaths: ["docs/**"]\n---\ndocs rule\n',
    )
    user_skill = write(
        home / ".claude" / "skills" / "personal" / "SKILL.md",
        "---\nname: personal\n---\n",
    )
    project_skill = write(
        repo / ".claude" / "skills" / "team" / "SKILL.md",
        "---\nname: team\n---\n",
    )
    settings = write(
        repo / ".claude" / "settings.json",
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "echo TOP_SECRET",
                                }
                            ],
                        }
                    ]
                },
                "extraKnownMarketplaces": {
                    "team": {
                        "source": {
                            "source": "directory",
                            "path": "/private/catalog",
                        }
                    }
                },
            }
        ),
    )
    marketplace = write(
        home / ".claude" / "plugins" / "known_marketplaces.json",
        '{"private":{"source":"/secret/path"}}',
    )

    result = discover([repo], home=home, working_directories=[cwd])
    surfaces = by_path(result)

    for path in (
        user_main,
        user_rule,
        project_main,
        project_local,
        imported,
        nested_import,
        applicable_rule,
        ignored_rule,
        user_skill,
        project_skill,
        settings,
        marketplace,
    ):
        assert str(path) in surfaces

    relationships = result.relationships
    assert any(
        relationship.type == "imports"
        and relationship.from_surface_id == surfaces[str(project_main)].id
        and relationship.to_surface_id == surfaces[str(imported)].id
        and relationship.status == "active"
        for relationship in relationships
    )
    assert any(
        relationship.type == "imports"
        and relationship.from_surface_id == surfaces[str(nested_import)].id
        and relationship.to_surface_id == surfaces[str(imported)].id
        and relationship.status == "cycle"
        for relationship in relationships
    )
    assert any(
        relationship.type == "imports"
        and relationship.from_surface_id == surfaces[str(project_main)].id
        and relationship.to_surface_id is None
        and relationship.status == "missing"
        for relationship in relationships
    )

    hook = next(
        relationship
        for relationship in relationships
        if relationship.type == "session_start_hook"
    )
    assert hook.location == {"event": "SessionStart", "registration_index": 0}
    assert "TOP_SECRET" not in repr(hook.to_dict())

    market = next(
        relationship
        for relationship in relationships
        if relationship.type == "marketplace_registration"
    )
    assert market.location == {"count": 1, "scope": "repository"}
    assert "/private/catalog" not in repr(market.to_dict())

    claude_stack = next(
        stack
        for stack in result.effective_stacks
        if stack["provider"] == "claude"
    )
    ids = claude_stack["surface_ids"]
    assert ids.index(surfaces[str(user_rule)].id) < ids.index(
        surfaces[str(applicable_rule)].id
    )
    assert ids.index(surfaces[str(project_main)].id) < ids.index(
        surfaces[str(project_local)].id
    )
    assert surfaces[str(applicable_rule)].id in ids
    assert surfaces[str(ignored_rule)].id not in ids


def test_provider_discovery_sees_canonical_skill_directory_symlinks(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    canonical = tmp_path / "canonical"
    write(canonical / "SKILL.md", "---\nname: optimizer\n---\n")
    codex_link = (
        home / ".agents" / "skills" / "optimizing-agent-instructions"
    )
    claude_link = (
        home / ".claude" / "skills" / "optimizing-agent-instructions"
    )
    codex_link.parent.mkdir(parents=True)
    claude_link.parent.mkdir(parents=True)
    try:
        codex_link.symlink_to(canonical, target_is_directory=True)
        claude_link.symlink_to(canonical, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    result = discover([repo], home=home, working_directories=[repo])
    paths = {surface.path for surface in result.surfaces if surface.kind == "skill"}

    assert str(codex_link / "SKILL.md") in paths
    assert str(claude_link / "SKILL.md") in paths


def test_claude_import_depth_is_capped_and_orphaned_sidecars_are_reported(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    write(repo / "CLAUDE.md", "@level-1.md\n")
    for index in range(1, 7):
        next_import = f"@level-{index + 1}.md\n" if index < 6 else ""
        write(repo / f"level-{index}.md", next_import)
    orphan = write(repo / "CLAUDE.apu.md", "managed by APU")

    result = discover([repo], home=home, working_directories=[repo])
    surfaces = by_path(result)

    assert str(repo / "level-5.md") in surfaces
    assert str(repo / "level-6.md") not in surfaces
    assert any(
        relationship.type == "imports"
        and relationship.from_surface_id
        == surfaces[str(repo / "level-5.md")].id
        and relationship.status == "max_depth"
        for relationship in result.relationships
    )
    assert any(
        relationship.type == "sidecar"
        and relationship.from_surface_id == surfaces[str(orphan)].id
        and relationship.status == "orphaned"
        for relationship in result.relationships
    )


def test_discovery_does_not_modify_the_tree(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    write(repo / "AGENTS.md", "repo")
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    first = discover([repo], home=home, working_directories=[repo])
    second = discover([repo], home=home, working_directories=[repo])

    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert before == after
    assert [
        (surface.path, surface.content_sha256) for surface in first.surfaces
    ] == [
        (surface.path, surface.content_sha256) for surface in second.surfaces
    ]


def test_nested_root_discovers_ancestor_instructions_and_rule_imports(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = home / "Code" / "repo"
    cwd = repo / "packages" / "api"
    cwd.mkdir(parents=True)
    agents = write(repo / "AGENTS.md", "ancestor codex")
    claude = write(repo / "CLAUDE.md", "ancestor claude")
    settings = write(
        repo / ".claude" / "settings.local.json",
        '{"hooks":{"SessionStart":[{"hooks":[]}]}}',
    )
    rule = write(
        repo / ".claude" / "rules" / "imports.md",
        "@../shared.md\n",
    )
    shared = write(repo / ".claude" / "shared.md", "shared")

    result = discover([cwd], home=home, working_directories=[cwd])
    surfaces = by_path(result)

    for path in (agents, claude, settings, rule, shared):
        assert str(path) in surfaces
    assert surfaces[str(shared)].authority == "repository"
    assert any(
        relationship.type == "imports"
        and relationship.from_surface_id == surfaces[str(rule)].id
        and relationship.to_surface_id == surfaces[str(shared)].id
        and relationship.status == "active"
        for relationship in result.relationships
    )


def test_enabled_plugin_session_start_hooks_are_discovered_without_commands(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    write(repo / "CLAUDE.md", "project")
    write(
        home / ".claude" / "settings.json",
        json.dumps(
            {
                "enabledPlugins": {
                    "superpowers@official": True,
                    "disabled-plugin@official": False,
                }
            }
        ),
    )
    hooks = write(
        home
        / ".claude"
        / "plugins"
        / "cache"
        / "official"
        / "superpowers"
        / "5.0.7"
        / "hooks"
        / "hooks.json",
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup|clear|compact",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "echo TOP_SECRET",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
    )
    plugin_skill = write(
        home
        / ".claude"
        / "plugins"
        / "cache"
        / "official"
        / "superpowers"
        / "5.0.7"
        / "skills"
        / "using-superpowers"
        / "SKILL.md",
        "---\nname: using-superpowers\n---\n",
    )
    write(
        home
        / ".claude"
        / "plugins"
        / "cache"
        / "official"
        / "disabled-plugin"
        / "1.0.0"
        / "hooks"
        / "hooks.json",
        json.dumps({"hooks": {"SessionStart": [{"matcher": "startup"}]}}),
    )

    result = discover([repo], home=home, working_directories=[repo])
    surfaces = by_path(result)

    assert str(hooks) in surfaces
    # The plugin's own skills are offered in every session, so the text the
    # model actually sees is the plugin copy, not any user-local variant.
    assert str(plugin_skill) in surfaces
    assert surfaces[str(plugin_skill)].authority == "package"
    plugin_surface = surfaces[str(hooks)]
    # A session-start injection outranks the user's own instruction files.
    assert plugin_surface.authority == "package"
    assert plugin_surface.precedence < surfaces[str(repo / "CLAUDE.md")].precedence

    hook = next(
        relationship
        for relationship in result.relationships
        if relationship.type == "session_start_hook"
    )
    assert hook.location["plugin"] == "superpowers@official"
    assert hook.location["source"] == "plugin"
    assert "TOP_SECRET" not in repr(hook.to_dict())

    assert not [
        path for path in surfaces if "disabled-plugin" in path
    ]

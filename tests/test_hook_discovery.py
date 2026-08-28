from __future__ import annotations

import json
from pathlib import Path

from apu.adapters.claude import ClaudeAdapter
from apu.adapters.codex import CodexAdapter


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _json(path: Path, value: object) -> Path:
    return _write(path, json.dumps(value))


def _hook_events(result) -> dict[str, object]:
    return {
        str(relationship.location.get("event")): relationship
        for relationship in result.relationships
        if relationship.type in {"lifecycle_hook", "session_start_hook"}
    }


def test_claude_discovers_every_hook_structurally_without_secret_bodies(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    _json(
        repo / ".claude" / "settings.json",
        {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup|resume",
                        "hooks": [
                            {"type": "command", "command": "echo SESSION_SECRET"}
                        ],
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "http", "url": "https://secret.invalid/token"}
                        ],
                    }
                ],
                "Stop": ["not-a-registration"],
            }
        },
    )

    result = ClaudeAdapter().discover([repo], home=home)
    events = _hook_events(result)

    assert set(events) == {"SessionStart", "PreToolUse", "Stop"}
    assert events["SessionStart"].status == "configured"
    assert events["PreToolUse"].status == "configured"
    assert events["Stop"].status == "invalid"
    assert events["PreToolUse"].location["handler_types"] == ["http"]
    encoded = repr([relationship.to_dict() for relationship in result.relationships])
    assert "SESSION_SECRET" not in encoded
    assert "secret.invalid" not in encoded


def test_codex_discovers_json_inline_and_enabled_plugin_hooks_as_trust_unknown(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        home / ".codex" / "config.toml",
        """
[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "echo GLOBAL_SECRET"

[plugins."policy@team"]
enabled = true
""".strip()
        + "\n",
    )
    _json(
        repo / ".codex" / "hooks.json",
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "echo PROJECT_SECRET"}
                        ],
                    }
                ]
            }
        },
    )
    plugin = home / ".codex" / "plugins" / "cache" / "team" / "policy" / "1.0"
    _json(
        plugin / ".codex-plugin" / "plugin.json",
        {"name": "policy", "version": "1.0", "hooks": "./hooks/custom.json"},
    )
    _json(
        plugin / "hooks" / "custom.json",
        {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "echo PLUGIN_SECRET"}]}
                ]
            }
        },
    )

    result = CodexAdapter().discover([repo], home=home)
    events = [
        relationship
        for relationship in result.relationships
        if relationship.type in {"lifecycle_hook", "session_start_hook"}
    ]

    assert {item.location["event"] for item in events} == {
        "Stop",
        "PreToolUse",
        "SessionStart",
    }
    assert {item.status for item in events} == {"trust-unknown"}
    assert any(item.location.get("source") == "plugin" for item in events)
    encoded = repr([relationship.to_dict() for relationship in result.relationships])
    assert "GLOBAL_SECRET" not in encoded
    assert "PROJECT_SECRET" not in encoded
    assert "PLUGIN_SECRET" not in encoded


def test_claude_plugin_hooks_use_authoritative_installed_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = _json(
        home / ".claude" / "settings.json",
        {"enabledPlugins": {"policy@team": True}},
    )
    cache = home / ".claude" / "plugins" / "cache" / "team" / "policy"
    stale = cache / "9.0"
    selected = cache / "10.0"
    _json(
        stale / "hooks" / "hooks.json",
        {
            "hooks": {
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": "echo STALE_SECRET"}]}
                ]
            }
        },
    )
    selected_hooks = _json(
        selected / "hooks" / "hooks.json",
        {
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "echo ACTIVE_SECRET"}]}
                ]
            }
        },
    )
    _json(
        home / ".claude" / "plugins" / "installed_plugins.json",
        {
            "version": 2,
            "plugins": {
                "policy@team": [
                    {
                        "version": "10.0",
                        "scope": "user",
                        "installPath": str(selected.resolve()),
                    }
                ]
            },
        },
    )

    result = ClaudeAdapter().discover([repo], home=home)
    events = _hook_events(result)

    assert set(events) == {"Stop"}
    assert events["Stop"].status == "active-observed"
    assert events["Stop"].location["source"] == "plugin"
    assert str(selected_hooks) in {surface.path for surface in result.surfaces}
    assert str(stale / "hooks" / "hooks.json") not in {
        surface.path for surface in result.surfaces
    }
    assert "ACTIVE_SECRET" not in repr(events["Stop"].to_dict())
    assert settings.is_file()


def test_claude_plugin_resolution_reports_ambiguous_cache_without_guessing(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    _json(
        home / ".claude" / "settings.json",
        {"enabledPlugins": {"policy@team": True}},
    )
    cache = home / ".claude" / "plugins" / "cache" / "team" / "policy"
    _json(cache / "1.0" / "hooks" / "hooks.json", {"hooks": {}})
    _json(cache / "2.0" / "hooks" / "hooks.json", {"hooks": {}})

    result = ClaudeAdapter().discover([repo], home=home)

    ambiguous = [
        relationship
        for relationship in result.relationships
        if relationship.type == "plugin_hook_resolution"
    ]
    assert len(ambiguous) == 1
    assert ambiguous[0].status == "ambiguous"
    assert "policy@team" not in repr(ambiguous[0].to_dict())
    assert not [
        surface for surface in result.surfaces if surface.kind == "claude-plugin-hooks"
    ]

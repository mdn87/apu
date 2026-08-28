from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import pytest

from apu.hooks import (
    add_hooks_parser,
    doctor_hooks,
    hooks_status,
    install_hooks,
    remove_hooks,
    render_hooks,
    run_hooks,
)
from apu.receipts import load_receipt
from apu.rollback import rollback_receipt
from apu.state import load_registry


def _managed_commands(value: object) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get("hooks"), dict):
        return []
    commands: list[str] = []
    for registrations in value["hooks"].values():
        if not isinstance(registrations, list):
            continue
        for registration in registrations:
            if not isinstance(registration, dict):
                continue
            for handler in registration.get("hooks", []):
                if isinstance(handler, dict) and isinstance(
                    handler.get("command"), str
                ):
                    commands.append(handler["command"])
    return commands


def test_render_hooks_is_scope_explicit_and_passive_watching_is_opt_in(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repository = tmp_path / "repo"
    repository.mkdir()

    rendered = render_hooks(
        "claude",
        scope="project",
        home=home,
        repository=repository,
        passive_watch=True,
    )

    assert rendered["target"] == str(repository / ".claude" / "settings.local.json")
    assert rendered["events"] == ["SessionEnd", "Stop"]
    commands = _managed_commands(rendered["fragment"])
    assert len(commands) == 2
    assert all("--passive-watch" in command for command in commands)
    assert all("--provider claude-code" in command for command in commands)
    assert rendered["policy_changes"] is False
    assert rendered["trust_changes"] is False

    without_watch = render_hooks("codex", scope="user", home=home)
    assert without_watch["target"] == str(home / ".codex" / "hooks.json")
    assert all(
        "--passive-watch" not in command
        for command in _managed_commands(without_watch["fragment"])
    )

    with pytest.raises(ValueError, match="scope"):
        render_hooks("claude", scope="implicit", home=home)
    with pytest.raises(ValueError, match="repository"):
        render_hooks("claude", scope="project", home=home)


def test_project_hook_target_rejects_redirected_provider_directory(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repository = tmp_path / "repo"
    outside = tmp_path / "outside"
    repository.mkdir()
    outside.mkdir()
    (repository / ".codex").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="redirect"):
        render_hooks(
            "codex",
            scope="project",
            home=home,
            repository=repository,
        )


def test_claude_install_round_trip_recognizes_claude_code_bridge(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state_home = tmp_path / "state"

    installed = install_hooks(
        "claude",
        scope="user",
        home=home,
        state_home=state_home,
        apply=True,
    )

    target = home / ".claude" / "settings.json"
    commands = _managed_commands(json.loads(target.read_text(encoding="utf-8")))
    assert installed["applied"] is True
    assert all("--provider claude-code" in command for command in commands)
    assert hooks_status("claude", scope="user", home=home)["state"] == "configured"

    removed = remove_hooks(
        "claude",
        scope="user",
        home=home,
        state_home=state_home,
        apply=True,
    )
    assert removed["applied"] is True
    assert not target.exists()


def test_install_status_and_remove_are_reviewable_idempotent_and_redacted(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state_home = tmp_path / "state"
    target = home / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    original = {
        "permissions": {"allow": ["Bash(git status)"]},
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "curl https://secret.invalid/token",
                        }
                    ]
                }
            ]
        },
    }
    target.write_text(json.dumps(original), encoding="utf-8")

    preview = install_hooks("claude", scope="user", home=home)
    assert preview["changed"] is True
    assert preview["applied"] is False
    assert json.loads(target.read_text(encoding="utf-8")) == original
    assert "secret.invalid" not in repr(preview)

    applied = install_hooks(
        "claude",
        scope="user",
        home=home,
        state_home=state_home,
        apply=True,
    )
    assert applied["changed"] is True
    assert applied["applied"] is True
    assert applied["installation_id"]
    assert applied["receipt"]
    receipt = load_receipt(Path(applied["receipt"]))
    assert receipt["operations"][0]["action"] == "configure"
    assert (
        load_registry(state_home)["installations"][applied["installation_id"]]["status"]
        == "active"
    )
    installed = json.loads(target.read_text(encoding="utf-8"))
    assert installed["permissions"] == original["permissions"]
    assert "secret.invalid" in target.read_text(encoding="utf-8")
    assert len(_managed_commands(installed)) == 3

    repeated = install_hooks(
        "claude",
        scope="user",
        home=home,
        state_home=state_home,
        apply=True,
    )
    assert repeated["changed"] is False
    assert len(_managed_commands(json.loads(target.read_text()))) == 3

    status = hooks_status("claude", scope="user", home=home)
    assert status["state"] == "configured"
    assert status["managed_events"] == ["SessionEnd", "Stop"]
    assert status["policy_changes"] is False
    assert status["trust_changes"] is False
    assert "secret.invalid" not in repr(status)

    removal_preview = remove_hooks("claude", scope="user", home=home)
    assert removal_preview["changed"] is True
    assert removal_preview["applied"] is False
    assert len(_managed_commands(json.loads(target.read_text()))) == 3
    assert "secret.invalid" not in repr(removal_preview)

    removed = remove_hooks(
        "claude",
        scope="user",
        home=home,
        state_home=state_home,
        apply=True,
    )
    assert removed["changed"] is True
    remaining = json.loads(target.read_text(encoding="utf-8"))
    assert remaining == original
    assert (
        remove_hooks(
            "claude",
            scope="user",
            home=home,
            state_home=state_home,
            apply=True,
        )["changed"]
        is False
    )


def test_doctor_is_read_only_and_reports_invalid_configuration(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = home / ".codex" / "hooks.json"

    healthy = doctor_hooks("codex", scope="user", home=home)
    assert healthy["ok"] is True
    assert healthy["state"] == "not-configured"
    assert healthy["read_only"] is True
    assert not target.exists()

    target.parent.mkdir(parents=True)
    target.write_text('{"hooks":"broken","token":"DOCTOR_SECRET"}', encoding="utf-8")
    invalid = doctor_hooks("codex", scope="user", home=home)
    assert invalid["ok"] is False
    assert invalid["state"] == "invalid"
    assert "DOCTOR_SECRET" not in repr(invalid)
    assert target.read_text(encoding="utf-8").endswith('"DOCTOR_SECRET"}')


def test_remove_does_not_claim_a_lookalike_shell_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    state_home = tmp_path / "state"
    monkeypatch.setenv("APU_HOME", str(state_home))
    target = home / ".codex" / "hooks.json"
    target.parent.mkdir(parents=True)
    original = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "echo hooks bridge --provider codex --event Stop"
                            ),
                        }
                    ]
                }
            ]
        }
    }
    target.write_text(json.dumps(original), encoding="utf-8")

    result = remove_hooks("codex", scope="user", home=home, apply=True)

    assert result["changed"] is False
    assert json.loads(target.read_text(encoding="utf-8")) == original
    assert list((state_home / "locks").glob("hooks-*.lock"))
    assert not (home / ".local" / "state" / "apu").exists()

    target.write_text('{ "hooks": {} }\n', encoding="utf-8")
    empty = remove_hooks("codex", scope="user", home=home, apply=True)
    assert empty["changed"] is False
    assert empty["after_sha256"] == empty["before_sha256"]
    assert json.loads(target.read_text(encoding="utf-8")) == {"hooks": {}}


def test_transactional_hook_create_remove_and_rollback_round_trip(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state_home = tmp_path / "state"
    target = home / ".codex" / "hooks.json"

    installed = install_hooks(
        "codex",
        scope="user",
        home=home,
        state_home=state_home,
        passive_watch=True,
        apply=True,
    )
    assert installed["receipt"]
    assert target.is_file()

    removed = remove_hooks(
        "codex",
        scope="user",
        home=home,
        state_home=state_home,
        apply=True,
    )
    assert removed["receipt"]
    assert not target.exists()
    remove_receipt = load_receipt(Path(removed["receipt"]))
    assert remove_receipt["operations"][0]["action"] == "remove"

    rollback = rollback_receipt(Path(removed["receipt"]))
    assert rollback["status"] == "rolled_back"
    assert target.is_file()
    assert hooks_status("codex", scope="user", home=home)["state"] == "configured"


def test_reusable_hooks_parser_keeps_bridge_silent_and_mutations_preview_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_hooks_parser(commands)

    render_args = parser.parse_args(
        ["hooks", "render", "--provider", "codex", "--scope", "user"]
    )
    assert run_hooks(render_args, home=tmp_path / "home") == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["provider"] == "codex"

    install_args = parser.parse_args(
        ["hooks", "install", "--provider", "codex", "--scope", "user"]
    )
    assert run_hooks(install_args, home=tmp_path / "home") == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["applied"] is False
    assert not (tmp_path / "home" / ".codex" / "hooks.json").exists()

    bridge_args = parser.parse_args(
        ["hooks", "bridge", "--provider", "codex", "--event", "Stop"]
    )
    assert (
        run_hooks(
            bridge_args,
            home=tmp_path / "home",
            stdin=io.BytesIO(b"not-json"),
            state_home=tmp_path / "state",
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

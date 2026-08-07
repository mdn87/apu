from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apu.state import (
    ensure_state_home,
    load_registry,
    resolve_state_home,
    update_registry,
)


def test_resolve_state_home_prefers_explicit_override_without_creating_it(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "explicit"

    resolved = resolve_state_home(
        env={"APU_HOME": str(state_home), "XDG_STATE_HOME": str(tmp_path / "xdg")},
        platform="linux",
        home=tmp_path / "user",
    )

    assert resolved == state_home
    assert not state_home.exists()


def test_resolve_state_home_uses_posix_xdg_then_home_fallback(
    tmp_path: Path,
) -> None:
    assert resolve_state_home(
        env={"XDG_STATE_HOME": str(tmp_path / "xdg")},
        platform="darwin",
        home=tmp_path / "user",
    ) == tmp_path / "xdg" / "apu"
    assert resolve_state_home(
        env={},
        platform="linux",
        home=tmp_path / "user",
    ) == tmp_path / "user" / ".local" / "state" / "apu"


def test_resolve_state_home_uses_windows_local_app_data(tmp_path: Path) -> None:
    assert resolve_state_home(
        env={"LOCALAPPDATA": str(tmp_path / "local")},
        platform="win32",
        home=tmp_path / "ignored",
    ) == tmp_path / "local" / "apu"

    with pytest.raises(ValueError, match="LOCALAPPDATA"):
        resolve_state_home(env={}, platform="win32", home=tmp_path)


def test_state_creation_is_explicit_and_private(tmp_path: Path) -> None:
    state_home = tmp_path / "apu-state"
    assert load_registry(state_home) == {
        "schema_version": 1,
        "installations": {},
    }
    assert not state_home.exists()

    ensure_state_home(state_home)

    assert {
        child.name for child in state_home.iterdir()
    } == {
        "inventories",
        "plans",
        "installations",
        "outcomes",
        "campaigns",
        "quarantine",
        "snapshots",
        "restore-journals",
        "transactions",
        "guidance",
        "models",
    }
    if os.name == "posix":
        assert state_home.stat().st_mode & 0o777 == 0o700
        for child in state_home.iterdir():
            assert child.stat().st_mode & 0o777 == 0o700


def test_registry_updates_are_canonical_atomic_and_removable(tmp_path: Path) -> None:
    state_home = tmp_path / "state"

    registry = update_registry(
        state_home,
        "install-b",
        {
            "status": "active",
            "receipt": "installations/install-b/receipt.json",
        },
    )
    registry = update_registry(
        state_home,
        "install-a",
        {
            "receipt": "installations/install-a/receipt.json",
            "status": "active",
        },
    )

    registry_path = state_home / "registry.json"
    assert json.loads(registry_path.read_text(encoding="utf-8")) == registry
    assert registry_path.read_text(encoding="utf-8") == (
        '{"installations":{"install-a":{"installation_id":"install-a",'
        '"receipt":"installations/install-a/receipt.json","status":"active"},'
        '"install-b":{"installation_id":"install-b",'
        '"receipt":"installations/install-b/receipt.json","status":"active"}},'
        '"schema_version":1}'
    )
    assert not list(state_home.glob(".registry.json.*"))
    if os.name == "posix":
        assert registry_path.stat().st_mode & 0o777 == 0o600

    removed = update_registry(state_home, "install-a", None)
    assert set(removed["installations"]) == {"install-b"}
    assert load_registry(state_home) == removed


def test_registry_rejects_invalid_installation_ids(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="installation_id"):
        update_registry(tmp_path, "../escape", {"status": "active"})

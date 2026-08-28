from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Barrier, Lock, Thread

import pytest

from apu.locking import ProcessLock
from apu.state import (
    ensure_private_directory,
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
    assert (
        resolve_state_home(
            env={"XDG_STATE_HOME": str(tmp_path / "xdg")},
            platform="darwin",
            home=tmp_path / "user",
        )
        == tmp_path / "xdg" / "apu"
    )
    assert (
        resolve_state_home(
            env={},
            platform="linux",
            home=tmp_path / "user",
        )
        == tmp_path / "user" / ".local" / "state" / "apu"
    )


def test_resolve_state_home_uses_windows_local_app_data(tmp_path: Path) -> None:
    assert (
        resolve_state_home(
            env={"LOCALAPPDATA": str(tmp_path / "local")},
            platform="win32",
            home=tmp_path / "ignored",
        )
        == tmp_path / "local" / "apu"
    )

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

    assert {child.name for child in state_home.iterdir()} == {
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
        "behavior",
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


def test_private_directory_creation_tolerates_concurrent_creators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "private"
    workers = 8
    at_create = Barrier(workers)
    real_mkdir = Path.mkdir

    def synchronized_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == destination:
            at_create.wait(timeout=5)
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", synchronized_mkdir)
    errors: list[BaseException] = []

    def create() -> None:
        try:
            ensure_private_directory(destination)
        except BaseException as error:  # noqa: BLE001 - thread assertion channel
            errors.append(error)

    threads = [Thread(target=create) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert destination.is_dir()
    if os.name == "posix":
        assert destination.stat().st_mode & 0o777 == 0o700


def test_process_lock_serializes_threads_and_creates_private_parent(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "private" / "state.lock"
    start = Barrier(8)
    observation_lock = Lock()
    active = 0
    maximum_active = 0

    def enter() -> None:
        nonlocal active, maximum_active
        start.wait(timeout=5)
        with ProcessLock(lock_path):
            with observation_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.005)
            with observation_lock:
                active -= 1

    threads = [Thread(target=enter) for _ in range(start.parties)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert maximum_active == 1
    assert lock_path.is_file()
    if os.name == "posix":
        assert lock_path.parent.stat().st_mode & 0o777 == 0o700
        assert lock_path.stat().st_mode & 0o777 == 0o600


def test_concurrent_registry_updates_preserve_distinct_installations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apu.state as state_module

    state_home = tmp_path / "state"
    workers = 16
    start = Barrier(workers)
    real_load_registry = state_module.load_registry

    def delayed_load_registry(path: Path) -> dict:
        registry = real_load_registry(path)
        time.sleep(0.01)
        return registry

    monkeypatch.setattr(state_module, "load_registry", delayed_load_registry)
    errors: list[BaseException] = []

    def update(index: int) -> None:
        try:
            start.wait(timeout=5)
            update_registry(
                state_home,
                f"install-{index}",
                {"status": "active"},
            )
        except BaseException as error:  # noqa: BLE001 - thread assertion channel
            errors.append(error)

    threads = [Thread(target=update, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert set(real_load_registry(state_home)["installations"]) == {
        f"install-{index}" for index in range(workers)
    }

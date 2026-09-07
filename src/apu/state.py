from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping

from .models import canonical_json


REGISTRY_SCHEMA_VERSION = 1
STATE_DIRECTORIES = (
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
    "policy-delta-intake",
)


def resolve_state_home(
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve APU's state directory without creating it."""

    environment = os.environ if env is None else env
    current_platform = sys.platform if platform is None else platform

    override = environment.get("APU_HOME")
    if override:
        return _absolute_path(override, "APU_HOME")

    if current_platform.startswith("win"):
        local_app_data = environment.get("LOCALAPPDATA")
        if not local_app_data:
            raise ValueError("LOCALAPPDATA is required to resolve APU_HOME on Windows")
        return _absolute_path(local_app_data, "LOCALAPPDATA") / "apu"

    xdg_state_home = environment.get("XDG_STATE_HOME")
    if xdg_state_home:
        return _absolute_path(xdg_state_home, "XDG_STATE_HOME") / "apu"

    user_home = Path.home() if home is None else Path(home)
    return user_home.expanduser() / ".local" / "state" / "apu"


def ensure_state_home(state_home: Path) -> Path:
    """Create APU's private state directory layout."""

    root = Path(state_home)
    ensure_private_directory(root)
    for name in STATE_DIRECTORIES:
        ensure_private_directory(root / name)
    return root


def validate_installation_id(installation_id: str) -> str:
    if (
        not isinstance(installation_id, str)
        or not installation_id
        or installation_id in {".", ".."}
        or "/" in installation_id
        or "\\" in installation_id
        or "\x00" in installation_id
    ):
        raise ValueError("installation_id must be one safe path component")
    return installation_id


def load_registry(state_home: Path) -> dict[str, Any]:
    """Load the registry, returning an empty registry when none exists."""

    path = Path(state_home) / "registry.json"
    if not path.exists():
        return {"schema_version": REGISTRY_SCHEMA_VERSION, "installations": {}}

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid registry at {path}: {error}") from error
    _validate_registry(value)
    return value


def update_registry(
    state_home: Path,
    installation_id: str,
    entry: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Set or remove an installation entry and atomically persist the registry."""

    installation_id = validate_installation_id(installation_id)
    registry = load_registry(state_home)
    installations = dict(registry["installations"])

    if entry is None:
        installations.pop(installation_id, None)
    else:
        stored_entry = dict(entry)
        supplied_id = stored_entry.get("installation_id", installation_id)
        if supplied_id != installation_id:
            raise ValueError("registry entry installation_id does not match its key")
        stored_entry["installation_id"] = installation_id
        installations[installation_id] = stored_entry

    updated = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "installations": installations,
    }
    write_json_atomic(Path(state_home) / "registry.json", updated)
    return updated


def write_json_atomic(path: Path, value: Any) -> Path:
    """Write canonical JSON through a private same-directory temporary file."""

    destination = Path(path)
    ensure_private_directory(destination.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _absolute_path(value: str, variable: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{variable} must be an absolute path")
    return path


def ensure_private_directory(path: Path) -> Path:
    """Create a directory chain with user-only permissions where meaningful."""

    destination = Path(path)
    missing: list[Path] = []
    cursor = destination
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    if os.name == "posix":
        current_mode = destination.stat().st_mode & 0o777
        destination.chmod(current_mode & 0o700)
    return destination


def _validate_registry(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("registry must be a JSON object")
    if value.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise ValueError("unsupported registry schema_version")
    installations = value.get("installations")
    if not isinstance(installations, dict):
        raise ValueError("registry installations must be an object")
    for installation_id, entry in installations.items():
        validate_installation_id(installation_id)
        if not isinstance(entry, dict):
            raise ValueError(f"registry entry {installation_id} must be an object")
        if entry.get("installation_id", installation_id) != installation_id:
            raise ValueError(
                f"registry entry {installation_id} has a mismatched installation_id"
            )

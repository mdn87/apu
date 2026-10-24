"""Claude updates backed by APU's existing snapshot and restore journal.

The whole plugin store is one restore unit, along with the settings relevant to
the selected installation and any external marketplace checkout. This avoids
restoring an old registry beside new plugin files. Later drift refuses restore.
Claude and other external writers must be idle during update and restoration.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from uuid import uuid4

from .dispatch_process import run_confined
from .models import sha256_json
from .package_adapters import ClaudePackageAdapter
from .package_coordinates import PackageCoordinate
from .package_state import PackageLock, validate_candidate_tree
from .package_upgrade import (
    PackageStateIdentity,
    PackageUpgradeError,
    PackageUpgradeRequest,
)
from .restore_journal import RestoreInterrupted, hash_restore_object, resume_restore
from .snapshot_restore import restore_snapshot
from .snapshots import SnapshotSurface, create_snapshot, load_snapshot
from .state import write_json_atomic


def _json(path):
    if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("unsafe Claude metadata")
    with path.open("rb") as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Claude metadata exceeds the size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("Claude metadata must be an object")
    return value


def _check_path(path):
    for candidate in (path, *path.parents):
        if not os.path.lexists(candidate):
            continue
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Claude restore does not traverse linked paths")
    if path.is_dir():
        validate_candidate_tree(path)
    elif path.exists() and (
        not path.is_file() or path.stat().st_size > 16 * 1024 * 1024
    ):
        raise ValueError("Claude restore file is unsupported or too large")


def claude_restore_surfaces(
    home: Path, coordinate: PackageCoordinate
) -> tuple[SnapshotSurface, ...]:
    home = Path(home).expanduser().absolute()
    plugins = home / ".claude/plugins"
    _check_path(plugins)
    metadata = _json(plugins / "installed_plugins.json")
    if metadata.get("version") != 2:
        raise ValueError("unsupported Claude installation record version")
    records = metadata["plugins"].get(coordinate.profile_selector)
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError("Claude restore requires one unambiguous installation")
    record = records[0]
    scope = record.get("scope")
    if scope not in {"user", "project", "local"}:
        raise ValueError("Claude managed installations cannot be restored by APU")
    roots = [plugins, home / ".claude/settings.json"]
    if scope != "user":
        project = Path(record.get("projectPath", ""))
        if not project.is_absolute() or not project.is_dir():
            raise ValueError(
                "Claude scoped installation needs an absolute project path"
            )
        roots.append(
            project
            / ".claude"
            / ("settings.local.json" if scope == "local" else "settings.json")
        )
    known = _json(plugins / "known_marketplaces.json")
    marketplace = Path(known[coordinate.source]["installLocation"])
    if not marketplace.is_absolute():
        raise ValueError("Claude marketplace path must be absolute")
    if not marketplace.is_relative_to(plugins):
        roots.append(marketplace)
    selected = []
    for path in sorted(set(roots), key=lambda p: (len(p.parts), str(p))):
        if any(path.is_relative_to(parent) for parent in selected):
            continue
        _check_path(path)
        selected.append(path)
    return tuple(
        SnapshotSurface(f"claude-{index}", path) for index, path in enumerate(selected)
    )


def _states(roots):
    values = {}
    for path in roots:
        path = Path(path)
        _check_path(path)
        kind = "directory" if path.is_dir() else "file" if path.is_file() else "absent"
        values[str(path)] = [
            kind,
            hash_restore_object(path) if kind != "absent" else None,
        ]
    return values


def run_claude_update(home: Path, coordinate: PackageCoordinate, scope: str) -> int:
    cwd = Path(home)
    if scope in {"project", "local"}:
        metadata = _json(cwd / ".claude/plugins/installed_plugins.json")
        records = metadata["plugins"][coordinate.profile_selector]
        if len(records) != 1 or records[0].get("scope") != scope:
            raise PackageUpgradeError("Claude installation scope changed before update")
        cwd = Path(records[0].get("projectPath", ""))
        if not cwd.is_absolute() or not cwd.is_dir():
            raise PackageUpgradeError("Claude project directory is unavailable")
    executable = shutil.which("claude")
    if executable is None:
        raise PackageUpgradeError("Claude CLI is unavailable")
    path = Path(executable)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
        native = path.parent / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
        if not native.is_file():
            raise PackageUpgradeError("Claude native executable is unavailable")
        executable = str(native)
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(Path(home) / ".claude")
    env.pop("CLAUDE_CODE_PLUGIN_CACHE_DIR", None)
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    result = run_confined(
        [
            executable,
            "plugin",
            "update",
            coordinate.profile_selector,
            "--scope",
            scope,
            "--json",
        ],
        input=b"",
        timeout=300,
        cwd=cwd,
        env=env,
    )
    return result.returncode


class ClaudePackageUpgradeExecutor:
    provider = "claude"

    def __init__(
        self,
        state_home: Path,
        home: Path,
        request: PackageUpgradeRequest,
        *,
        runner=None,
    ):
        self.state_home = Path(state_home).expanduser().resolve()
        self.home = Path(home).expanduser().absolute()
        self.request = request
        self.runner = runner or run_claude_update
        self.point_path = (
            self.state_home / "claude-restore-points" / f"{request.transaction_id}.json"
        )

    def _lock(self):
        return PackageLock(
            self.state_home, "claude-store:" + os.path.normcase(str(self.home))
        )

    def observe(self, coordinate):
        return PackageStateIdentity.from_observation(
            coordinate,
            ClaudePackageAdapter(self.home).observe(coordinate.profile_selector),
        )

    def _point(self):
        _check_path(self.point_path)
        value = _json(self.point_path)
        if (
            value.get("schema_version") != 1
            or value.get("request") != self.request.to_dict()
            or value.get("home") != str(self.home)
        ):
            raise PackageUpgradeError("Claude restore point does not match the request")
        manifest = load_snapshot(self.state_home, value["snapshot_id"])
        if {item["root"] for item in manifest["surfaces"]} != set(value["before"]):
            raise PackageUpgradeError(
                "Claude restore point does not match its snapshot"
            )
        return value

    def update_exact(self, coordinate, request):
        if request != self.request or coordinate.package_id != request.package_id:
            raise ValueError("Claude update request mismatch")
        with self._lock():
            if self.point_path.exists():
                raise PackageUpgradeError(
                    "Claude restore point exists; recover instead of repeating update"
                )
            if self.observe(coordinate) != request.pre_state:
                raise PackageUpgradeError("Claude package changed before backup")
            surfaces = claude_restore_surfaces(self.home, coordinate)
            for surface in surfaces:
                if self.state_home.is_relative_to(
                    surface.root
                ) or surface.root.is_relative_to(self.state_home):
                    raise ValueError(
                        "APU state must be separate from Claude restore targets"
                    )
            before = _states([surface.root for surface in surfaces])
            with PackageLock(self.state_home, "snapshot-retention"):
                snapshot = create_snapshot(
                    self.state_home, surfaces, label="Claude plugin update"
                )
                if (
                    _states(before) != before
                    or self.observe(coordinate) != request.pre_state
                ):
                    raise PackageUpgradeError(
                        "Claude state changed while creating backup"
                    )
                point = {
                    "schema_version": 1,
                    "request": request.to_dict(),
                    "home": str(self.home),
                    "snapshot_id": snapshot["snapshot_id"],
                    "before": before,
                    "after": None,
                    "status": "prepared",
                    "restore_journal_id": None,
                }
                write_json_atomic(self.point_path, point)
            try:
                result = self.runner(self.home, coordinate, request.pre_state.scope)
            finally:
                # A hard process crash can leave this absent. Never guess that
                # unknown later changes belong to the interrupted update.
                point["after"] = _states(before)
                point["status"] = "update-returned"
                write_json_atomic(self.point_path, point)
            if result != 0:
                raise PackageUpgradeError(
                    "Claude plugin update failed; restoring captured state"
                )
            return {"snapshot_id": snapshot["snapshot_id"]}

    def verify_rollback(self):
        if not self.point_path.exists():
            return True  # No provider call can run before this point is durable.
        point = self._point()
        return _states(point["before"]) == point["before"]

    def current_state_digest(self):
        return sha256_json(_states(self._point()["before"]))

    def rollback_exact(
        self, coordinate, pre_state, *, expected_state: str | None = None
    ):
        if (
            pre_state != self.request.pre_state
            or coordinate.package_id != self.request.package_id
        ):
            raise ValueError("Claude rollback request mismatch")
        with self._lock():
            point = self._point()
            journal = (
                self.state_home
                / "restore-journals"
                / (point["restore_journal_id"] or "unused")
            )
            if (
                point["restore_journal_id"]
                and point["status"] in {"restore-started", "restore-interrupted"}
                and journal.exists()
            ):
                result = resume_restore(
                    self.state_home / "restore-journals", point["restore_journal_id"]
                )
            else:
                current = _states(point["before"])
                if current == point["before"]:
                    return {
                        "snapshot_id": point["snapshot_id"],
                        "status": "already-restored",
                    }
                if expected_state is not None:
                    if sha256_json(current) != expected_state:
                        raise PackageUpgradeError(
                            "Claude state changed since recovery was reviewed"
                        )
                    point["after"] = current
                    point["reconciled_current_state"] = expected_state
                    write_json_atomic(self.point_path, point)
                if point["after"] is None or current != point["after"]:
                    raise PackageUpgradeError(
                        "Claude state changed or update was interrupted; review recovery state before restoring"
                    )
                # Persist the journal identity before the first swap, so a hard
                # interruption cannot lose the route to existing recovery state.
                point["restore_journal_id"] = uuid4().hex
                point["status"] = "restore-started"
                write_json_atomic(self.point_path, point)
                try:
                    result = restore_snapshot(
                        self.state_home,
                        point["snapshot_id"],
                        journal_id=point["restore_journal_id"],
                        expected_states={
                            Path(path): tuple(value)
                            for path, value in point["after"].items()
                        },
                    )
                except RestoreInterrupted as error:
                    point["restore_journal_id"] = error.journal_id
                    point["status"] = "restore-interrupted"
                    write_json_atomic(self.point_path, point)
                    raise
            point["restore_journal_id"] = result.journal_id
            if not self.verify_rollback() or self.observe(coordinate) != pre_state:
                raise PackageUpgradeError(
                    "Claude restoration did not reproduce the captured state"
                )
            point["status"] = "restored"
            write_json_atomic(self.point_path, point)
            return {
                "snapshot_id": point["snapshot_id"],
                "restore_journal_id": result.journal_id,
                "status": "restored",
            }

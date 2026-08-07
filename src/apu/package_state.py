from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Self

from apu.filesystem import hash_object
from apu.models import sha256_json
from apu.state import (
    ensure_private_directory,
    ensure_state_home,
    write_json_atomic,
)

MAX_CANDIDATE_FILES = 20_000
MAX_CANDIDATE_BYTES = 256 * 1024 * 1024
MAX_CANDIDATE_DEPTH = 48

_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "aux",
        "clock$",
        "con",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


class PackageStateError(RuntimeError):
    """Raised when immutable package state cannot be stored safely."""


class PackageLock(AbstractContextManager["PackageLock"]):
    """Fail-fast, OS-released lock for one canonical package identity."""

    def __init__(self, state_home: Path, package_id: str) -> None:
        root = ensure_state_home(Path(state_home).expanduser().resolve())
        lock_root = ensure_private_directory(root / "packages" / "locks")
        self.path = lock_root / f"{sha256_json(package_id)}.lock"
        self._stream = None

    def __enter__(self) -> Self:
        self._stream = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                self._stream.write(b"\0")
                self._stream.flush()
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    self._stream.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        except (OSError, BlockingIOError) as error:
            self._stream.close()
            self._stream = None
            raise PackageStateError(
                f"package research is already locked: {self.path}"
            ) from error
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._stream is None:
            return
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None


def validate_candidate_tree(
    root: Path,
    *,
    max_files: int = MAX_CANDIDATE_FILES,
    max_bytes: int = MAX_CANDIDATE_BYTES,
    max_depth: int = MAX_CANDIDATE_DEPTH,
) -> dict[str, int]:
    """Validate an inert candidate tree without following links or junctions."""

    candidate = Path(root).expanduser().resolve()
    if not candidate.is_dir():
        raise PackageStateError(f"candidate tree is not a directory: {candidate}")
    if any(
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        for limit in (max_files, max_bytes, max_depth)
    ):
        raise ValueError("candidate tree limits must be positive integers")

    normalized_paths: set[str] = set()
    file_count = 0
    byte_count = 0
    for current, directory_names, file_names in os.walk(
        candidate,
        topdown=True,
        onerror=_raise_walk_error,
        followlinks=False,
    ):
        current_path = Path(current)
        relative_current = current_path.relative_to(candidate)
        if len(relative_current.parts) > max_depth:
            raise PackageStateError("candidate tree exceeds the depth limit")
        for name in sorted((*directory_names, *file_names)):
            path = current_path / name
            relative = path.relative_to(candidate)
            _validate_relative_path(relative, normalized_paths)
            if len(normalized_paths) > max_files:
                raise PackageStateError(
                    "candidate tree exceeds the entry-count limit"
                )
            if relative.parts[0] in {".git", ".hg", ".svn"}:
                raise PackageStateError(
                    f"candidate tree contains version-control metadata: {relative}"
                )
            if _is_link_or_junction(path):
                raise PackageStateError(
                    f"candidate tree contains a link or junction: {relative}"
                )
            try:
                mode = path.stat(follow_symlinks=False).st_mode
            except OSError as error:
                raise PackageStateError(
                    f"candidate tree entry is unreadable: {relative}"
                ) from error
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise PackageStateError(
                    f"candidate tree contains an unsupported object: {relative}"
                )
            file_count += 1
            byte_count += path.stat(follow_symlinks=False).st_size
            if byte_count > max_bytes:
                raise PackageStateError("candidate tree exceeds the byte limit")
    return {
        "entry_count": len(normalized_paths),
        "file_count": file_count,
        "byte_count": byte_count,
    }


def store_candidate_tree(state_home: Path, source: Path) -> tuple[str, Path]:
    """Copy a validated candidate into immutable, content-addressed state."""

    root = ensure_state_home(Path(state_home).expanduser().resolve())
    source_path = Path(source).expanduser().resolve()
    validate_candidate_tree(source_path)
    tree_sha256 = hash_object(source_path)
    tree_root = ensure_private_directory(root / "packages" / "trees")
    destination = tree_root / tree_sha256
    if destination.exists():
        validate_candidate_tree(destination)
        if hash_object(destination) != tree_sha256:
            raise PackageStateError(
                f"stored candidate tree does not match its identity: {tree_sha256}"
            )
        return tree_sha256, destination

    temporary = Path(tempfile.mkdtemp(prefix=".candidate-", dir=tree_root))
    staged = temporary / "tree"
    try:
        shutil.copytree(source_path, staged, symlinks=True)
        validate_candidate_tree(staged)
        if hash_object(staged) != tree_sha256:
            raise PackageStateError("candidate tree changed while it was copied")
        try:
            os.replace(staged, destination)
        except FileExistsError:
            if hash_object(destination) != tree_sha256:
                raise PackageStateError(
                    "concurrent candidate storage produced a hash mismatch"
                )
        return tree_sha256, destination
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def write_package_leaf(
    state_home: Path,
    *,
    kind: str,
    package_id: str,
    value: Mapping[str, Any],
) -> tuple[str, Path]:
    """Write one immutable, canonical package artifact leaf."""

    if (
        not kind
        or any(character not in "abcdefghijklmnopqrstuvwxyz-" for character in kind)
    ):
        raise ValueError("package leaf kind must be lowercase letters and hyphens")
    artifact = dict(value)
    artifact_id = sha256_json(artifact)
    root = ensure_state_home(Path(state_home).expanduser().resolve())
    package_key = sha256_json(package_id)
    leaf_root = ensure_private_directory(
        root / "packages" / kind / package_key
    )
    path = leaf_root / f"{artifact_id}.json"
    if path.exists():
        try:
            import json

            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PackageStateError(f"invalid existing package leaf: {path}") from error
        if existing != artifact:
            raise PackageStateError(
                f"package leaf identity collision at {path}"
            )
        return artifact_id, path
    write_json_atomic(path, artifact)
    return artifact_id, path


def _raise_walk_error(error: OSError) -> None:
    raise PackageStateError(f"candidate tree traversal failed: {error}") from error


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _validate_relative_path(
    path: Path,
    normalized_paths: set[str],
) -> None:
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise PackageStateError(f"candidate path escapes its tree: {path}")
    for part in path.parts:
        trimmed = part.rstrip(" .")
        stem = trimmed.split(".", 1)[0].casefold()
        if (
            not trimmed
            or trimmed != part
            or ":" in part
            or stem in _WINDOWS_RESERVED_NAMES
            or part in {".", ".."}
        ):
            raise PackageStateError(f"candidate path is not portable: {path}")
    normalized = "/".join(part.casefold() for part in path.parts)
    if normalized in normalized_paths:
        raise PackageStateError(
            f"candidate tree contains a case-colliding path: {path}"
        )
    normalized_paths.add(normalized)

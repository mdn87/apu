from __future__ import annotations

import fnmatch
import os
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Iterable

from .models import sha256_bytes


def matches_exclude(relative: str, patterns: Iterable[str]) -> bool:
    """True when a surface-relative path matches any exclude pattern.

    Shared by the audit walk and the snapshot walk so a profile's excludes mean
    the same thing in both. They are separate traversals, and an exclude honoured
    by only one of them silently reappears in the other's output.
    """
    relative = relative.replace("\\", "/").strip("/")
    path = PurePosixPath(relative)
    for raw_pattern in patterns:
        pattern = raw_pattern.replace("\\", "/").strip("/")
        if not pattern:
            continue
        if "/" not in pattern and pattern in path.parts:
            return True
        if fnmatch.fnmatchcase(relative, pattern) or path.match(pattern):
            return True
        if pattern.endswith("/**"):
            prefix = pattern[:-3].rstrip("/")
            if relative == prefix or relative.startswith(prefix + "/"):
                return True
    return False


def hash_object(path: Path) -> str:
    """Hash a file or tree while preserving filesystem object identity."""

    candidate = Path(path)
    if candidate.is_symlink():
        return sha256_bytes(b"L\0" + os.fsencode(os.readlink(candidate)))
    if candidate.is_file():
        return sha256_bytes(candidate.read_bytes())
    if candidate.is_dir():
        digest = sha256()
        for child in sorted(candidate.rglob("*"), key=lambda item: item.as_posix()):
            relative = child.relative_to(candidate).as_posix().encode("utf-8")
            if child.is_symlink():
                digest.update(b"L\0" + relative + b"\0")
                digest.update(os.fsencode(os.readlink(child)))
            elif child.is_dir():
                digest.update(b"D\0" + relative)
            elif child.is_file():
                digest.update(b"F\0" + relative + b"\0")
                digest.update(child.read_bytes())
            else:
                digest.update(b"O\0" + relative)
            digest.update(b"\0")
        return digest.hexdigest()
    raise OSError(f"unsupported filesystem object: {candidate}")


def symlink_points_to(link: Path, expected: Path | str) -> bool:
    """Compare a symlink destination using native path semantics."""

    candidate = Path(link)
    if not candidate.is_symlink():
        return False
    try:
        raw_target = os.readlink(candidate)
    except OSError:
        return False
    actual = Path(raw_target)
    if not actual.is_absolute():
        actual = candidate.parent / actual
    return _path_identity(actual) == _path_identity(Path(expected))


def _path_identity(path: Path) -> str:
    normalized = os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))
    if os.name == "nt":
        if normalized.startswith("\\\\?\\unc\\"):
            normalized = "\\\\" + normalized[8:]
        elif normalized.startswith("\\\\?\\"):
            normalized = normalized[4:]
    return normalized

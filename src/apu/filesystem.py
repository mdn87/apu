from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path

from .models import sha256_bytes


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
    normalized = os.path.normcase(
        os.path.normpath(os.path.abspath(os.fspath(path)))
    )
    if os.name == "nt":
        if normalized.startswith("\\\\?\\unc\\"):
            normalized = "\\\\" + normalized[8:]
        elif normalized.startswith("\\\\?\\"):
            normalized = normalized[4:]
    return normalized

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

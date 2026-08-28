#!/usr/bin/env python3
"""Synchronize authoring assets with the package-local resource mirror."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

RESOURCE_DIRECTORIES = ("fixtures", "schemas", "skills", "templates")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "src" / "apu" / "_resources"


def _files(root: Path) -> dict[str, Path]:
    return {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }


def differences() -> list[str]:
    result: list[str] = []
    for directory in RESOURCE_DIRECTORIES:
        source = _files(REPOSITORY_ROOT / directory)
        bundled = _files(PACKAGE_ROOT / directory)
        for relative in sorted(source.keys() - bundled.keys()):
            result.append(f"missing: {directory}/{relative}")
        for relative in sorted(bundled.keys() - source.keys()):
            result.append(f"stale: {directory}/{relative}")
        for relative in sorted(source.keys() & bundled.keys()):
            if source[relative].read_bytes() != bundled[relative].read_bytes():
                result.append(f"changed: {directory}/{relative}")
    return result


def synchronize() -> None:
    for directory in RESOURCE_DIRECTORIES:
        source_root = REPOSITORY_ROOT / directory
        bundled_root = PACKAGE_ROOT / directory
        source = _files(source_root)
        bundled = _files(bundled_root)
        for relative in bundled.keys() - source.keys():
            bundled[relative].unlink()
        for relative, path in source.items():
            destination = bundled_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        for path in sorted(bundled_root.rglob("*"), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write",
        action="store_true",
        help="update the package-local mirror instead of checking it",
    )
    args = parser.parse_args()
    if args.write:
        synchronize()
    drift = differences()
    if drift:
        print("resource mirror differs from authoring assets:")
        for item in drift:
            print(f"- {item}")
        return 1
    print("resource mirror matches authoring assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

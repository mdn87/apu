#!/usr/bin/env python3
"""Verify one installed APU distribution and its packaged resources."""

from __future__ import annotations

import argparse
import subprocess
import sys
from hashlib import sha256
from importlib.metadata import distribution, version
from pathlib import Path

from apu import __version__
from apu.resources import behavioral_fixtures_path, optimizer_skill_path, resource_root

COMMANDS = ("apu", "apu-event", "apu-wtf", "apu-intervene", "apu-watch")
RESOURCE_DIRECTORIES = ("fixtures", "schemas", "skills", "templates")


def _files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }


def _entry_point(scripts: Path, command: str) -> Path:
    candidates = (scripts / command, scripts / f"{command}.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AssertionError(f"installed entry point is missing: {command}")


def verify(source_root: Path, scripts: Path) -> None:
    installed_version = version("agent-policy-updater")
    assert installed_version == __version__, (
        f"metadata version {installed_version!r} does not match runtime "
        f"version {__version__!r}"
    )

    bundled = resource_root()
    assert bundled.name == "_resources" and bundled.parent.name == "apu", (
        f"resources are not package-local: {bundled}"
    )
    assert optimizer_skill_path().is_dir()
    assert behavioral_fixtures_path().is_dir()

    for directory in RESOURCE_DIRECTORIES:
        expected = _files(source_root / directory)
        actual = _files(bundled / directory)
        assert actual == expected, f"packaged {directory} resources differ from source"

    installed_files = {
        item.as_posix() for item in distribution("agent-policy-updater").files or ()
    }
    assert not any("share/apu" in item for item in installed_files), (
        "legacy scheme-level share/apu data remains in the distribution"
    )
    for directory in RESOURCE_DIRECTORIES:
        for relative in _files(source_root / directory):
            expected = f"apu/_resources/{directory}/{relative}"
            assert expected in installed_files, (
                f"distribution record is missing {expected}"
            )

    for command in COMMANDS:
        completed = subprocess.run(
            [str(_entry_point(scripts, command)), "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, (
            f"{command} --help failed ({completed.returncode}): {completed.stderr}"
        )

    completed = subprocess.run(
        [str(_entry_point(scripts, "apu")), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == __version__


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--scripts-dir", type=Path, default=Path(sys.executable).parent)
    args = parser.parse_args()
    verify(args.source_root.resolve(), args.scripts_dir.resolve())
    print(f"verified APU {__version__} from {resource_root()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Validate an APU release tag, runtime version, and changelog entry."""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path

from apu import __version__

STABLE_TAG = re.compile(r"v(?P<version>\d+\.\d+\.\d+)")


def validate_release(tag: str, release_version: str, changelog: str) -> None:
    match = STABLE_TAG.fullmatch(tag)
    if match is None:
        raise ValueError("release tag must have the stable form vMAJOR.MINOR.PATCH")
    tagged_version = match.group("version")
    if release_version != tagged_version:
        raise ValueError(
            f"tag version {tagged_version!r} does not match runtime version "
            f"{release_version!r}"
        )

    heading = re.search(
        rf"^## {re.escape(release_version)} — (?P<date>\d{{4}}-\d{{2}}-\d{{2}})$",
        changelog,
        flags=re.MULTILINE,
    )
    if heading is None:
        raise ValueError(
            f"CHANGELOG.md must contain a dated '## {release_version} — YYYY-MM-DD' heading"
        )
    date.fromisoformat(heading.group("date"))
    if re.search(
        rf"^## {re.escape(release_version)} \(Unreleased\)$",
        changelog,
        flags=re.MULTILINE,
    ):
        raise ValueError("release changelog entry is still marked Unreleased")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version", default=__version__)
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    args = parser.parse_args()
    try:
        changelog = args.changelog.read_text(encoding="utf-8")
        validate_release(args.tag, args.version, changelog)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"release contract verified for {args.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

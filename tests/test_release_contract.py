from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scripts.check_release import validate_release

ROOT = Path(__file__).parents[1]


def test_project_metadata_uses_runtime_version_as_its_single_source() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "version" not in project["project"]
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "apu.__version__"
    }


def test_release_contract_accepts_matching_tag_version_and_dated_changelog() -> None:
    validate_release("v1.2.3", "1.2.3", "# Changelog\n\n## 1.2.3 — 2026-08-28\n")


@pytest.mark.parametrize(
    ("tag", "version", "changelog", "message"),
    [
        ("release-1.2.3", "1.2.3", "## 1.2.3 — 2026-08-28\n", "stable form"),
        ("v1.2.3", "1.2.4", "## 1.2.4 — 2026-08-28\n", "does not match"),
        ("v1.2.3", "1.2.3", "## 1.2.3 (Unreleased)\n", "dated"),
    ],
)
def test_release_contract_rejects_incomplete_release_state(
    tag: str,
    version: str,
    changelog: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_release(tag, version, changelog)

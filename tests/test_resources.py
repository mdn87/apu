from __future__ import annotations

from pathlib import Path

import pytest

from apu import resources

SOURCE_ROOT = Path(__file__).parents[1]
RESOURCE_DIRECTORIES = ("fixtures", "schemas", "skills", "templates")


def _resource_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }


def test_resource_root_uses_data_bundled_inside_apu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APU_RESOURCE_ROOT", raising=False)

    root = resources.resource_root()

    assert root == Path(resources.__file__).parent / "_resources"
    assert resources.optimizer_skill_path() == (
        root / "skills" / "optimizing-agent-instructions"
    )
    assert resources.behavioral_fixtures_path() == root / "fixtures" / "behavioral"


def test_resource_root_override_must_be_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APU_RESOURCE_ROOT", "relative/resources")

    with pytest.raises(ValueError, match="must be absolute"):
        resources.resource_root()


def test_bundled_resources_match_authoring_assets() -> None:
    bundled = resources.resource_root()

    for directory in RESOURCE_DIRECTORIES:
        assert _resource_files(bundled / directory) == _resource_files(
            SOURCE_ROOT / directory
        )

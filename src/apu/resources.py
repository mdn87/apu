from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path


def resource_root() -> Path:
    override = os.environ.get("APU_RESOURCE_ROOT")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError("APU_RESOURCE_ROOT must be absolute")
        return path

    bundled = Path(str(files("apu").joinpath("_resources")))
    if bundled.is_dir():
        return bundled
    raise FileNotFoundError("APU package resources are unavailable")


def optimizer_skill_path() -> Path:
    path = resource_root() / "skills" / "optimizing-agent-instructions"
    if not (path / "SKILL.md").is_file():
        raise FileNotFoundError(f"optimizer skill is unavailable at {path}")
    return path


def behavioral_fixtures_path() -> Path:
    path = resource_root() / "fixtures" / "behavioral"
    if not path.is_dir():
        raise FileNotFoundError(f"behavioral fixtures are unavailable at {path}")
    return path

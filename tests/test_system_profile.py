from __future__ import annotations

from pathlib import Path

import pytest

from apu.system_profile import (
    ProfileError,
    SystemProfile,
    default_profile_path,
    load_system_profile,
    resolve_profile_path,
)


def test_profile_loads_toml_resolves_paths_and_is_immutable(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    config = tmp_path / "config" / "profile.toml"
    config.parent.mkdir()
    config.write_text(
        """
schema_version = 1
global_surfaces = ["~/.codex", "./global"]
packages = ["optimizer@catalog"]
guidance_sources = ["https://example.test/guide"]

[[roots]]
path = "../projects"
excludes = ["node_modules", "archive/**"]

[remediation_policy]
duplicate-instruction = "auto"
guidance-conflict = "work-order"
""",
        encoding="utf-8",
    )

    profile = load_system_profile(config, home=home)

    assert profile.roots[0].path == str(
        (config.parent / "../projects").resolve()
    )
    assert profile.global_surfaces == (
        str(home / ".codex"),
        str((config.parent / "global").resolve()),
    )
    assert profile.remediation_policy["duplicate-instruction"] == "auto"
    with pytest.raises(TypeError):
        profile.remediation_policy["new"] = "ignore"  # type: ignore[index]
    assert SystemProfile.from_dict(
        profile.to_dict(), base_directory=config.parent, home=home
    ) == profile


def test_profile_uses_global_defaults_and_validates_policy(
    tmp_path: Path,
) -> None:
    profile = SystemProfile.from_dict(
        {"roots": [str(tmp_path / "projects")]},
        home=tmp_path / "home",
    )
    assert profile.global_surfaces == (
        str(tmp_path / "home" / ".claude"),
        str(tmp_path / "home" / ".codex"),
        str(tmp_path / "home" / ".agents"),
        str(tmp_path / "home" / ".claude" / "plugins" / "cache"),
    )

    with pytest.raises(ProfileError, match="unsupported remediation"):
        SystemProfile.from_dict(
            {
                "roots": [str(tmp_path)],
                "remediation_policy": {"conflict": "sometimes"},
            }
        )
    with pytest.raises(ProfileError, match="do not escape"):
        SystemProfile.from_dict(
            {
                "roots": [
                    {"path": str(tmp_path), "excludes": ["../outside"]}
                ]
            }
        )


def test_profile_default_and_explicit_paths_are_cross_platform(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    assert default_profile_path(
        home=home,
        environ={"APPDATA": str(tmp_path / "roaming")},
        platform="win32",
    ) == tmp_path / "roaming" / "apu" / "profile.toml"
    assert default_profile_path(
        home=home,
        environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")},
        platform="linux",
    ) == tmp_path / "xdg" / "apu" / "profile.toml"
    assert default_profile_path(
        home=home, environ={}, platform="linux"
    ) == home / ".config" / "apu" / "profile.toml"
    assert resolve_profile_path(tmp_path / "chosen.toml") == (
        tmp_path / "chosen.toml"
    ).resolve()


def test_invalid_toml_is_reported_as_profile_error(tmp_path: Path) -> None:
    path = tmp_path / "profile.toml"
    path.write_text("roots = [", encoding="utf-8")
    with pytest.raises(ProfileError, match="invalid TOML"):
        load_system_profile(path)

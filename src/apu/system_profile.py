from __future__ import annotations

import json
import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

_POLICIES = frozenset({"auto", "work-order", "ignore"})


class ProfileError(ValueError):
    """Raised when a system profile does not satisfy its public contract."""


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{field} must be a non-empty string")
    return value.strip()


def _path_from_config(
    value: object,
    *,
    field: str,
    base_directory: Path,
    home: Path,
) -> str:
    raw = _nonempty_string(value, field)
    if raw == "~":
        candidate = home
    elif raw.startswith(("~/", "~\\")):
        candidate = home / raw[2:]
    else:
        candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base_directory / candidate
    return str(candidate.resolve(strict=False))


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ProfileError(f"{field} must be an array")
    result = tuple(
        _nonempty_string(item, f"{field}[{index}]") for index, item in enumerate(value)
    )
    if len(set(result)) != len(result):
        raise ProfileError(f"{field} must not contain duplicates")
    return result


def _validate_excludes(value: object, field: str) -> tuple[str, ...]:
    patterns = _string_tuple(value, field)
    normalized: list[str] = []
    for pattern in patterns:
        pattern = pattern.replace("\\", "/").removeprefix("./").rstrip("/")
        path = PurePosixPath(pattern)
        if not pattern or path.is_absolute() or ".." in path.parts:
            raise ProfileError(
                f"{field} entries must be relative patterns that do not escape"
            )
        normalized.append(pattern)
    return tuple(normalized)


@dataclass(frozen=True)
class ProfileRoot:
    path: str
    excludes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.path or not Path(self.path).is_absolute():
            raise ProfileError("root path must be absolute")
        if any(not item for item in self.excludes):
            raise ProfileError("root excludes must be non-empty patterns")

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "excludes": list(self.excludes)}

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any] | str,
        *,
        base_directory: Path | None = None,
        home: Path | None = None,
        field: str = "roots[]",
    ) -> ProfileRoot:
        base = (base_directory or Path.cwd()).resolve(strict=False)
        selected_home = (home or Path.home()).resolve(strict=False)
        if isinstance(value, str):
            raw_path: object = value
            raw_excludes: object = ()
        elif isinstance(value, Mapping):
            unknown = set(value) - {"path", "excludes"}
            if unknown:
                raise ProfileError(
                    f"{field} has unsupported fields: {', '.join(sorted(unknown))}"
                )
            raw_path = value.get("path")
            raw_excludes = value.get("excludes", ())
        else:
            raise ProfileError(f"{field} must be a string or table")
        return cls(
            path=_path_from_config(
                raw_path,
                field=f"{field}.path",
                base_directory=base,
                home=selected_home,
            ),
            excludes=_validate_excludes(raw_excludes, f"{field}.excludes"),
        )


@dataclass(frozen=True)
class ProfileSurface:
    """One machine-global instruction surface, with optional exclusions.

    Roots have carried ``excludes`` since the beginning; global surfaces did
    not, so a directory like ``~/.codex`` was scanned wholesale. That swept in
    agent runtime scratch -- ``~/.codex/tmp/arg0`` holds symlinked dispatch
    shims that all resolve to the same binary, which reads downstream as
    ambiguous target coverage.
    """

    path: str
    excludes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.path or not Path(self.path).is_absolute():
            raise ProfileError("global_surfaces must contain absolute paths")
        if any(not item for item in self.excludes):
            raise ProfileError("surface excludes must be non-empty patterns")

    def to_dict(self) -> dict[str, Any] | str:
        # Emit a bare string when there is nothing to exclude so existing
        # profiles round-trip byte-identically and artifact_sha256 is stable.
        if not self.excludes:
            return self.path
        return {"path": self.path, "excludes": list(self.excludes)}

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any] | str,
        *,
        base_directory: Path | None = None,
        home: Path | None = None,
        field: str = "global_surfaces[]",
    ) -> ProfileSurface:
        base = (base_directory or Path.cwd()).resolve(strict=False)
        selected_home = (home or Path.home()).resolve(strict=False)
        if isinstance(value, str):
            raw_path: object = value
            raw_excludes: object = ()
        elif isinstance(value, Mapping):
            unknown = set(value) - {"path", "excludes"}
            if unknown:
                raise ProfileError(
                    f"{field} has unsupported fields: {', '.join(sorted(unknown))}"
                )
            raw_path = value.get("path")
            raw_excludes = value.get("excludes", ())
        else:
            raise ProfileError(f"{field} must be a string or table")
        return cls(
            path=_path_from_config(
                raw_path,
                field=f"{field}.path",
                base_directory=base,
                home=selected_home,
            ),
            excludes=_validate_excludes(raw_excludes, f"{field}.excludes"),
        )


@dataclass(frozen=True)
class SystemProfile:
    schema_version: int
    roots: tuple[ProfileRoot, ...]
    global_surfaces: tuple[ProfileSurface, ...]
    packages: tuple[str, ...]
    guidance_sources: tuple[str, ...]
    remediation_policy: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ProfileError(
                f"unsupported system profile schema version: {self.schema_version}"
            )
        if not self.roots:
            raise ProfileError("profile must declare at least one root")
        root_paths = [os.path.normcase(root.path) for root in self.roots]
        if len(set(root_paths)) != len(root_paths):
            raise ProfileError("profile roots must not contain duplicates")
        # Callers may still pass plain strings; coerce so the excludes-aware
        # type is an addition rather than a breaking change.
        object.__setattr__(
            self,
            "global_surfaces",
            tuple(
                item if isinstance(item, ProfileSurface) else ProfileSurface(path=item)
                for item in self.global_surfaces
            ),
        )
        globals_normalized = [
            os.path.normcase(surface.path) for surface in self.global_surfaces
        ]
        if len(set(globals_normalized)) != len(globals_normalized):
            raise ProfileError("global_surfaces must not contain duplicates")
        policy = dict(self.remediation_policy)
        for category, action in policy.items():
            _nonempty_string(category, "remediation_policy category")
            if action not in _POLICIES:
                raise ProfileError(
                    f"unsupported remediation policy for {category}: {action}"
                )
        object.__setattr__(
            self,
            "remediation_policy",
            MappingProxyType(dict(sorted(policy.items()))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "roots": [root.to_dict() for root in self.roots],
            "global_surfaces": [s.to_dict() for s in self.global_surfaces],
            "packages": list(self.packages),
            "guidance_sources": list(self.guidance_sources),
            "remediation_policy": dict(self.remediation_policy),
        }

    @property
    def artifact_sha256(self) -> str:
        encoded = json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        base_directory: Path | None = None,
        home: Path | None = None,
    ) -> SystemProfile:
        if not isinstance(value, Mapping):
            raise ProfileError("system profile must be a table")
        aliases = {
            "version": "schema_version",
            "pinned_packages": "packages",
        }
        normalized = dict(value)
        for alias, canonical in aliases.items():
            if alias in normalized:
                if canonical in normalized:
                    raise ProfileError(
                        f"profile cannot contain both {alias} and {canonical}"
                    )
                normalized[canonical] = normalized.pop(alias)
        allowed = {
            "schema_version",
            "roots",
            "global_surfaces",
            "packages",
            "guidance_sources",
            "remediation_policy",
        }
        unknown = set(normalized) - allowed
        if unknown:
            raise ProfileError(
                f"profile has unsupported fields: {', '.join(sorted(unknown))}"
            )

        base = (base_directory or Path.cwd()).resolve(strict=False)
        selected_home = (home or Path.home()).resolve(strict=False)
        raw_roots = normalized.get("roots")
        if not isinstance(raw_roots, (list, tuple)):
            raise ProfileError("roots must be an array of paths or tables")
        roots = tuple(
            ProfileRoot.from_dict(
                item,
                base_directory=base,
                home=selected_home,
                field=f"roots[{index}]",
            )
            for index, item in enumerate(raw_roots)
        )

        raw_globals = normalized.get("global_surfaces")
        if raw_globals is None:
            raw_globals = (
                "~/.claude",
                # The Codex CLI writes arg0 dispatch shims under ~/.codex/tmp.
                # They are runtime scratch, not instruction surfaces, and every
                # shim in one directory symlinks to the same binary -- so
                # snapshotting them reports ambiguous target coverage on any
                # machine where Codex is installed.
                {"path": "~/.codex", "excludes": ["tmp/**"]},
                "~/.agents",
                "~/.claude/plugins/cache",
            )
        if not isinstance(raw_globals, (list, tuple)):
            raise ProfileError("global_surfaces must be an array")
        globals_ = tuple(
            ProfileSurface.from_dict(
                item,
                base_directory=base,
                home=selected_home,
                field=f"global_surfaces[{index}]",
            )
            for index, item in enumerate(raw_globals)
        )

        packages = _string_tuple(normalized.get("packages", ()), "packages")
        guidance = _string_tuple(
            normalized.get("guidance_sources", ()), "guidance_sources"
        )
        raw_policy = normalized.get("remediation_policy", {})
        if not isinstance(raw_policy, Mapping):
            raise ProfileError("remediation_policy must be a table")
        policy: dict[str, str] = {}
        for raw_category, raw_action in raw_policy.items():
            category = _nonempty_string(raw_category, "remediation_policy category")
            action = _nonempty_string(raw_action, f"remediation_policy.{category}")
            policy[category] = action

        raw_version = normalized.get("schema_version", 1)
        if not isinstance(raw_version, int) or isinstance(raw_version, bool):
            raise ProfileError("schema_version must be an integer")
        return cls(
            schema_version=raw_version,
            roots=roots,
            global_surfaces=globals_,
            packages=packages,
            guidance_sources=guidance,
            remediation_policy=policy,
        )


def default_profile_path(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    """Return the platform-native default without requiring it to exist."""

    selected_home = (home or Path.home()).resolve(strict=False)
    selected_environment = os.environ if environ is None else environ
    selected_platform = sys.platform if platform is None else platform
    if selected_platform.startswith("win"):
        appdata = selected_environment.get("APPDATA")
        base = (
            Path(appdata).resolve(strict=False)
            if appdata
            else selected_home / "AppData" / "Roaming"
        )
    else:
        xdg = selected_environment.get("XDG_CONFIG_HOME")
        base = Path(xdg).resolve(strict=False) if xdg else selected_home / ".config"
    return base / "apu" / "profile.toml"


def resolve_profile_path(
    path: Path | str | None = None,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    if path is None:
        return default_profile_path(home=home, environ=environ, platform=platform)
    raw = Path(path).expanduser()
    return raw.resolve(strict=False)


def load_system_profile(
    path: Path | str | None = None,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> SystemProfile:
    selected_home = (home or Path.home()).resolve(strict=False)
    profile_path = resolve_profile_path(
        path,
        home=selected_home,
        environ=environ,
        platform=platform,
    )
    try:
        content = profile_path.read_bytes()
    except OSError as error:
        raise ProfileError(f"cannot read profile {profile_path}: {error}") from error
    try:
        value = tomllib.loads(content.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ProfileError(f"invalid TOML profile {profile_path}: {error}") from error
    return SystemProfile.from_dict(
        value,
        base_directory=profile_path.parent,
        home=selected_home,
    )


load_profile = load_system_profile
SystemRoot = ProfileRoot

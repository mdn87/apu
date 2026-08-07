from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import total_ordering


class PackageCoordinateError(ValueError):
    """Raised when a package coordinate is malformed or unsupported."""


class SemanticVersionError(ValueError):
    """Raised when a value is not an exact Semantic Version 2.0.0 value."""


class VersionSelectionError(ValueError):
    """Raised when a deterministic version selection cannot be made."""


_PROVIDER_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,63})\Z")
_SEGMENT_PATTERN = r"[a-z0-9](?:[a-z0-9._-]{0,127})"
_PACKAGE_PATTERN = re.compile(
    rf"(?:{_SEGMENT_PATTERN}|@{_SEGMENT_PATTERN}/{_SEGMENT_PATTERN})\Z"
)
_SOURCE_PATTERN = re.compile(_SEGMENT_PATTERN + r"\Z")
_SUPPORTED_PROVIDERS = frozenset({"claude"})

_SEMANTIC_VERSION_PATTERN = re.compile(
    r"(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>"
    r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*"
    r"))?"
    r"(?:\+(?P<build>"
    r"[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*"
    r"))?\Z"
)


@dataclass(frozen=True)
class PackageCoordinate:
    """A canonical provider/package/source coordinate.

    Claude's profile selector syntax uses the final ``@`` as the marketplace
    separator. Splitting from the right preserves scoped package names such as
    ``@scope/tool@marketplace``.
    """

    provider: str
    package: str
    source: str

    def __post_init__(self) -> None:
        provider = _normalize_identifier(
            self.provider, "package coordinate provider"
        )
        package = _normalize_identifier(
            self.package, "package coordinate package"
        )
        source = _normalize_identifier(
            self.source, "package coordinate source"
        )
        if _PROVIDER_PATTERN.fullmatch(provider) is None:
            raise PackageCoordinateError(
                f"invalid package coordinate provider: {self.provider!r}"
            )
        if provider not in _SUPPORTED_PROVIDERS:
            raise PackageCoordinateError(
                f"unsupported package coordinate provider: {provider}"
            )
        if _PACKAGE_PATTERN.fullmatch(package) is None:
            raise PackageCoordinateError(
                f"invalid {provider} package name: {self.package!r}"
            )
        if _SOURCE_PATTERN.fullmatch(source) is None:
            raise PackageCoordinateError(
                f"invalid {provider} package source: {self.source!r}"
            )
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "package", package)
        object.__setattr__(self, "source", source)

    @property
    def profile_selector(self) -> str:
        return f"{self.package}@{self.source}"

    @property
    def package_id(self) -> str:
        return f"{self.provider}:{self.profile_selector}"

    def __str__(self) -> str:
        return self.package_id

    @classmethod
    def from_profile_selector(
        cls,
        selector: str,
        *,
        provider: str = "claude",
    ) -> PackageCoordinate:
        """Parse a legacy profile selector with an explicit provider context."""

        value = _strict_text(selector, "package selector")
        package, separator, source = value.rpartition("@")
        if not separator or not package or not source:
            raise PackageCoordinateError(
                "package selector must be PACKAGE@SOURCE"
            )
        return cls(provider=provider, package=package, source=source)

    @classmethod
    def parse(cls, value: str) -> PackageCoordinate:
        """Parse a canonical, provider-prefixed package coordinate."""

        coordinate = _strict_text(value, "package coordinate")
        provider, separator, selector = coordinate.partition(":")
        if not separator or not provider or not selector:
            raise PackageCoordinateError(
                "canonical package coordinate must be PROVIDER:PACKAGE@SOURCE"
            )
        return cls.from_profile_selector(selector, provider=provider)


def parse_package_coordinate(value: str) -> PackageCoordinate:
    return PackageCoordinate.parse(value)


def parse_profile_package(
    selector: str, *, provider: str = "claude"
) -> PackageCoordinate:
    return PackageCoordinate.from_profile_selector(selector, provider=provider)


@total_ordering
@dataclass(frozen=True)
class SemanticVersion:
    """An exact SemVer 2.0.0 value ordered by SemVer precedence."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        reparsed = type(self).parse(str(self))
        if (
            self.major,
            self.minor,
            self.patch,
            self.prerelease,
            self.build,
        ) != (
            reparsed.major,
            reparsed.minor,
            reparsed.patch,
            reparsed.prerelease,
            reparsed.build,
        ):
            raise SemanticVersionError("invalid semantic version components")

    @property
    def is_stable(self) -> bool:
        return not self.prerelease

    @property
    def precedence_identity(
        self,
    ) -> tuple[int, int, int, tuple[str, ...]]:
        """Return the identity used by SemVer precedence (build is ignored)."""

        return (self.major, self.minor, self.patch, self.prerelease)

    def compare_precedence(self, other: SemanticVersion) -> int:
        if not isinstance(other, SemanticVersion):
            raise TypeError("semantic versions can only be compared to each other")
        core = _compare_values(
            (self.major, self.minor, self.patch),
            (other.major, other.minor, other.patch),
        )
        if core:
            return core
        return _compare_prerelease(self.prerelease, other.prerelease)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self.compare_precedence(other) == 0

    def __lt__(self, other: SemanticVersion) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self.compare_precedence(other) < 0

    def __hash__(self) -> int:
        return hash(self.precedence_identity)

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += "-" + ".".join(self.prerelease)
        if self.build:
            value += "+" + ".".join(self.build)
        return value

    @classmethod
    def parse(cls, value: str) -> SemanticVersion:
        text = _strict_semantic_version_text(value)
        match = _SEMANTIC_VERSION_PATTERN.fullmatch(text)
        if match is None:
            raise SemanticVersionError(
                f"invalid Semantic Version 2.0.0 value: {value!r}"
            )
        prerelease = match.group("prerelease")
        build = match.group("build")
        return cls.__new_from_parsed(
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
            tuple(prerelease.split(".")) if prerelease else (),
            tuple(build.split(".")) if build else (),
        )

    @classmethod
    def __new_from_parsed(
        cls,
        major: int,
        minor: int,
        patch: int,
        prerelease: tuple[str, ...],
        build: tuple[str, ...],
    ) -> SemanticVersion:
        instance = object.__new__(cls)
        object.__setattr__(instance, "major", major)
        object.__setattr__(instance, "minor", minor)
        object.__setattr__(instance, "patch", patch)
        object.__setattr__(instance, "prerelease", prerelease)
        object.__setattr__(instance, "build", build)
        return instance


def parse_semantic_version(value: str) -> SemanticVersion:
    return SemanticVersion.parse(value)


def compare_semantic_versions(
    left: str | SemanticVersion,
    right: str | SemanticVersion,
) -> int:
    return _coerce_version(left).compare_precedence(_coerce_version(right))


def select_latest_stable(
    versions: Iterable[str | SemanticVersion],
) -> SemanticVersion:
    """Select one latest stable version, rejecting SemVer precedence ties."""

    stable = [_coerce_version(value) for value in versions]
    stable = [version for version in stable if version.is_stable]
    if not stable:
        raise VersionSelectionError("no stable semantic versions are available")
    latest = max(stable)
    tied = {
        str(version)
        for version in stable
        if version.compare_precedence(latest) == 0
    }
    if len(tied) > 1:
        raise VersionSelectionError(
            "latest stable version is ambiguous because build metadata "
            "does not affect SemVer precedence: "
            + ", ".join(sorted(tied))
        )
    return latest


def _normalize_identifier(value: str, field: str) -> str:
    return _strict_text(value, field).lower()


def _strict_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PackageCoordinateError(f"{field} must be non-empty text")
    if value != value.strip():
        raise PackageCoordinateError(
            f"{field} must not contain surrounding whitespace"
        )
    if not value.isascii():
        raise PackageCoordinateError(f"{field} must contain only ASCII text")
    return value


def _strict_semantic_version_text(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise SemanticVersionError("semantic version must be non-empty text")
    if value != value.strip():
        raise SemanticVersionError(
            "semantic version must not contain surrounding whitespace"
        )
    if not value.isascii():
        raise SemanticVersionError(
            "semantic version must contain only ASCII text"
        )
    return value


def _coerce_version(value: str | SemanticVersion) -> SemanticVersion:
    if isinstance(value, SemanticVersion):
        return value
    if not isinstance(value, str):
        raise SemanticVersionError(
            "semantic version must be text or a SemanticVersion"
        )
    return SemanticVersion.parse(value)


def _compare_values(
    left: tuple[int, ...],
    right: tuple[int, ...],
) -> int:
    return (left > right) - (left < right)


def _compare_prerelease(
    left: tuple[str, ...],
    right: tuple[str, ...],
) -> int:
    if not left and not right:
        return 0
    if not left:
        return 1
    if not right:
        return -1
    for left_identifier, right_identifier in zip(left, right, strict=False):
        if left_identifier == right_identifier:
            continue
        left_numeric = left_identifier.isdigit()
        right_numeric = right_identifier.isdigit()
        if left_numeric and right_numeric:
            return _compare_values(
                (int(left_identifier),), (int(right_identifier),)
            )
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return (left_identifier > right_identifier) - (
            left_identifier < right_identifier
        )
    return _compare_values((len(left),), (len(right),))

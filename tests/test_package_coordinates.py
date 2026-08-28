from __future__ import annotations

from itertools import pairwise

import pytest

from apu.package_coordinates import (
    PackageCoordinate,
    PackageCoordinateError,
    SemanticVersion,
    SemanticVersionError,
    VersionSelectionError,
    compare_semantic_versions,
    parse_package_coordinate,
    parse_profile_package,
    select_latest_stable,
)


def test_profile_selector_becomes_provider_prefixed_coordinate() -> None:
    coordinate = parse_profile_package("superpowers@claude-plugins-official")

    assert coordinate == PackageCoordinate(
        provider="claude",
        package="superpowers",
        source="claude-plugins-official",
    )
    assert coordinate.profile_selector == "superpowers@claude-plugins-official"
    assert coordinate.package_id == ("claude:superpowers@claude-plugins-official")
    assert str(coordinate) == coordinate.package_id
    assert parse_package_coordinate(coordinate.package_id) == coordinate


def test_coordinate_normalizes_ascii_identifiers() -> None:
    coordinate = parse_package_coordinate("CLAUDE:Tool_Box@Team.Catalog")

    assert coordinate.package_id == "claude:tool_box@team.catalog"


def test_final_at_sign_is_marketplace_separator_for_scoped_package() -> None:
    coordinate = parse_profile_package("@openai/tools@official")

    assert coordinate.package == "@openai/tools"
    assert coordinate.source == "official"
    assert coordinate.package_id == "claude:@openai/tools@official"
    assert parse_package_coordinate(coordinate.package_id) == coordinate


@pytest.mark.parametrize(
    "value",
    [
        "superpowers",
        "@catalog",
        "superpowers@",
        "superpowers@@catalog",
        "scope/tool@catalog",
        "@scope@catalog",
        "@scope/tool@catalog@1.2.3",
        "super powers@catalog",
        "superpowers@catalog/path",
        " superpowers@catalog",
        "superpowers@catalog ",
        "superpowers@catalóg",
    ],
)
def test_profile_selector_rejects_malformed_or_ambiguous_values(
    value: str,
) -> None:
    with pytest.raises(PackageCoordinateError):
        parse_profile_package(value)


@pytest.mark.parametrize(
    "value",
    [
        "superpowers@catalog",
        ":superpowers@catalog",
        "claude:",
        "unknown:superpowers@catalog",
        "claude:superpowers",
        "claude:superpowers@catalog:other",
    ],
)
def test_canonical_coordinate_requires_supported_provider_prefix(
    value: str,
) -> None:
    with pytest.raises(PackageCoordinateError):
        parse_package_coordinate(value)


@pytest.mark.parametrize(
    "value",
    [
        "0.0.0",
        "1.2.3",
        "10.20.30-alpha",
        "1.0.0-alpha.1",
        "1.0.0-0.3.7",
        "1.0.0-x.7.z.92",
        "1.0.0+build.1",
        "1.0.0-alpha+001",
    ],
)
def test_semantic_version_accepts_exact_semver_values(value: str) -> None:
    assert str(SemanticVersion.parse(value)) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "1",
        "1.2",
        "v1.2.3",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-",
        "1.2.3-alpha..1",
        "1.2.3-01",
        "1.2.3+",
        "1.2.3+build..1",
        "1.2.3 ",
        " 1.2.3",
        "1.2.3-α",
    ],
)
def test_semantic_version_rejects_non_exact_values(value: str) -> None:
    with pytest.raises((SemanticVersionError, PackageCoordinateError)):
        SemanticVersion.parse(value)


def test_semantic_version_uses_spec_precedence_order() -> None:
    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
    ]

    parsed = [SemanticVersion.parse(value) for value in ordered]

    assert sorted(parsed) == parsed
    assert all(left < right for left, right in pairwise(parsed))


def test_build_metadata_does_not_affect_semver_precedence() -> None:
    left = SemanticVersion.parse("1.2.3+build.1")
    right = SemanticVersion.parse("1.2.3+build.2")

    assert left == right
    assert compare_semantic_versions(left, right) == 0
    assert str(left) != str(right)


def test_latest_stable_ignores_prereleases_and_deduplicates_exact_values() -> None:
    latest = select_latest_stable(["2.0.0-rc.1", "1.9.0", "1.10.0", "1.10.0"])

    assert str(latest) == "1.10.0"


def test_latest_stable_rejects_missing_stable_and_build_tie() -> None:
    with pytest.raises(VersionSelectionError, match="no stable"):
        select_latest_stable(["1.0.0-alpha", "2.0.0-rc.1"])

    with pytest.raises(VersionSelectionError, match="ambiguous"):
        select_latest_stable(["1.0.0+linux", "1.0.0+windows"])

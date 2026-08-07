from __future__ import annotations

import json
from pathlib import Path

import pytest

from apu.filesystem import hash_object
from apu.package_state import (
    PackageLock,
    PackageStateError,
    _validate_relative_path,
    store_candidate_tree,
    validate_candidate_tree,
    write_package_leaf,
)


def test_candidate_tree_is_stored_by_verified_content_hash(tmp_path: Path) -> None:
    source = tmp_path / "candidate"
    source.mkdir()
    (source / "SKILL.md").write_text("safe guidance", encoding="utf-8")
    nested = source / "skills" / "one"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("nested guidance", encoding="utf-8")

    identity, stored = store_candidate_tree(tmp_path / "state", source)
    repeated_identity, repeated = store_candidate_tree(
        tmp_path / "state",
        source,
    )

    assert identity == hash_object(source)
    assert hash_object(stored) == identity
    assert (repeated_identity, repeated) == (identity, stored)
    assert stored != source


def test_candidate_tree_rejects_links_case_collisions_and_limits(
    tmp_path: Path,
) -> None:
    linked = tmp_path / "linked"
    linked.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    try:
        (linked / "escape").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(PackageStateError, match="link or junction"):
        validate_candidate_tree(linked)

    colliding = tmp_path / "colliding"
    colliding.mkdir()
    (colliding / "Policy.md").write_text("one", encoding="utf-8")
    try:
        (colliding / "policy.md").write_text("two", encoding="utf-8")
    except OSError:
        pass
    if len(list(colliding.iterdir())) == 2:
        with pytest.raises(PackageStateError, match="case-colliding"):
            validate_candidate_tree(colliding)

    bounded = tmp_path / "bounded"
    bounded.mkdir()
    (bounded / "large").write_bytes(b"1234")
    with pytest.raises(PackageStateError, match="byte limit"):
        validate_candidate_tree(bounded, max_bytes=3)

    for nonportable in (Path("unsafe."), Path("unsafe ")):
        with pytest.raises(PackageStateError, match="not portable"):
            _validate_relative_path(nonportable, set())


def test_package_leaves_are_immutable_and_canonical(tmp_path: Path) -> None:
    value = {
        "schema_version": 1,
        "artifact_type": "package-observation",
        "status": "installed",
    }

    artifact_id, path = write_package_leaf(
        tmp_path / "state",
        kind="observations",
        package_id="claude:superpowers@official",
        value=value,
    )
    repeated_id, repeated_path = write_package_leaf(
        tmp_path / "state",
        kind="observations",
        package_id="claude:superpowers@official",
        value=value,
    )

    assert (repeated_id, repeated_path) == (artifact_id, path)
    assert json.loads(path.read_text(encoding="utf-8")) == value

    path.write_text("{}", encoding="utf-8")
    with pytest.raises(PackageStateError, match="identity collision"):
        write_package_leaf(
            tmp_path / "state",
            kind="observations",
            package_id="claude:superpowers@official",
            value=value,
        )


def test_package_lock_is_fail_fast_and_os_released(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with (
        PackageLock(state, "claude:superpowers@official"),
        pytest.raises(PackageStateError, match="already locked"),
        PackageLock(state, "claude:superpowers@official"),
    ):
        pass
    with PackageLock(state, "claude:superpowers@official"):
        pass

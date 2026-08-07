from __future__ import annotations

from pathlib import Path

from apu.snapshot_restore import restore_snapshot
from apu.snapshots import SnapshotSurface, create_snapshot


def test_restore_snapshot_round_trips_a_declared_tree(tmp_path: Path) -> None:
    state = tmp_path / "state"
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "AGENTS.md").write_text("before\n", encoding="utf-8")
    empty = policy / "empty"
    empty.mkdir()
    manifest = create_snapshot(
        state,
        (SnapshotSurface("policy", policy),),
        created_at="2026-08-06T12:00:00Z",
    )
    (policy / "AGENTS.md").write_text("after\n", encoding="utf-8")
    (policy / "extra.txt").write_text("extra\n", encoding="utf-8")
    empty.rmdir()

    result = restore_snapshot(state, manifest["snapshot_id"])

    assert result.status == "completed"
    assert (policy / "AGENTS.md").read_text(encoding="utf-8") == "before\n"
    assert not (policy / "extra.txt").exists()
    assert empty.is_dir()


def test_restore_snapshot_can_select_one_exact_file(tmp_path: Path) -> None:
    state = tmp_path / "state"
    policy = tmp_path / "policy"
    policy.mkdir()
    first = policy / "first.md"
    second = policy / "second.md"
    first.write_text("first-before\n", encoding="utf-8")
    second.write_text("second-before\n", encoding="utf-8")
    manifest = create_snapshot(
        state,
        (SnapshotSurface("policy", policy),),
        created_at="2026-08-06T12:00:00Z",
    )
    first.write_text("first-after\n", encoding="utf-8")
    second.write_text("second-after\n", encoding="utf-8")

    restore_snapshot(state, manifest["snapshot_id"], paths=(first,))

    assert first.read_text(encoding="utf-8") == "first-before\n"
    assert second.read_text(encoding="utf-8") == "second-after\n"


def test_restore_snapshot_removes_a_surface_that_was_absent(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    missing = tmp_path / "missing-policy"
    manifest = create_snapshot(
        state,
        (SnapshotSurface("missing-policy", missing),),
        created_at="2026-08-06T12:00:00Z",
    )
    missing.mkdir()
    (missing / "created.md").write_text("created later\n", encoding="utf-8")

    restore_snapshot(state, manifest["snapshot_id"])

    assert not missing.exists()

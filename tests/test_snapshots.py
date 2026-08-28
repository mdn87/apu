from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from threading import Event, Thread

import pytest

from apu.snapshots import (
    SnapshotSurface,
    create_snapshot,
    diff_snapshot,
    enforce_retention,
    list_snapshots,
    load_snapshot,
    materialize_snapshot_object,
    resolve_blob_path,
)


def is_junction(path: Path) -> bool:
    path_method = getattr(path, "is_junction", None)
    if path_method is not None:
        return bool(path_method())
    os_method = getattr(os.path, "isjunction", None)
    if os_method is not None:
        return bool(os_method(path))
    metadata = path.lstat()
    mount_point_tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    return (
        mount_point_tag is not None
        and getattr(metadata, "st_reparse_tag", None) == mount_point_tag
    )


def test_create_honours_surface_excludes(tmp_path: Path) -> None:
    """Excluded subtrees never reach the manifest.

    Regression: excludes were dropped twice on the way in -- once when
    _normalize_surfaces rebuilt SnapshotSurface without them, and once because
    the snapshot walk did not consult them at all. The visible symptom was a
    campaign snapshot reporting ambiguous target coverage, because agent runtime
    scratch such as ~/.codex/tmp/arg0 holds several symlinked dispatch shims that
    all resolve to the same binary.
    """
    state_home = tmp_path / "state"
    live = tmp_path / "live"
    live.mkdir()
    (live / "AGENTS.md").write_text("keep me\n", encoding="utf-8")
    scratch = live / "tmp" / "arg0"
    scratch.mkdir(parents=True)
    (scratch / "apply_patch").write_text("shim\n", encoding="utf-8")
    (scratch / "applypatch").write_text("shim\n", encoding="utf-8")

    manifest = create_snapshot(
        state_home,
        (
            SnapshotSurface(
                logical_path="surface-0001",
                root=live,
                excludes=("tmp/**",),
            ),
        ),
    )
    captured = {entry["relative_path"] for entry in manifest["entries"]}
    assert "AGENTS.md" in captured
    assert not any(item.startswith("tmp") for item in captured), captured

    # Without the exclude the same tree captures the scratch, so the test is
    # asserting the exclude rather than an incidental traversal limit.
    unfiltered = create_snapshot(
        tmp_path / "state-unfiltered",
        (SnapshotSurface(logical_path="surface-0001", root=live),),
    )
    unfiltered_paths = {entry["relative_path"] for entry in unfiltered["entries"]}
    assert "tmp/arg0/apply_patch" in unfiltered_paths


def test_create_captures_files_empty_directories_missing_and_links(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    live = tmp_path / "live"
    live.mkdir()
    (live / "policy.md").write_text("private policy\n", encoding="utf-8")
    (live / "empty").mkdir()
    external = tmp_path / "external-secret.txt"
    external.write_text("must-not-be-copied", encoding="utf-8")
    link = live / "external-link"
    try:
        link.symlink_to(external)
    except OSError:
        link = None

    manifest = create_snapshot(
        state_home,
        {
            "policy-stack": live,
            "optional-settings": tmp_path / "missing.json",
        },
        label="before campaign",
        campaign_id="campaign-1",
        created_at="2026-08-06T12:00:00Z",
    )

    assert manifest["schema_version"] == 1
    assert manifest["campaign_id"] == "campaign-1"
    assert manifest["label"] == "before campaign"
    assert manifest["acl_restoration"] == "out_of_scope"
    assert manifest["surfaces"] == [
        {
            "logical_path": "optional-settings",
            "root": str((tmp_path / "missing.json").absolute()),
            "present": False,
        },
        {
            "logical_path": "policy-stack",
            "root": str(live.absolute()),
            "present": True,
        },
    ]
    entries = {entry["logical_path"]: entry for entry in manifest["entries"]}
    assert entries["policy-stack"]["object_type"] == "directory"
    assert entries["policy-stack"]["empty"] is False
    assert entries["policy-stack/empty"]["object_type"] == "directory"
    assert entries["policy-stack/empty"]["empty"] is True
    policy = entries["policy-stack/policy.md"]
    assert policy["object_type"] == "file"
    assert policy["blob_sha256"] == policy["hash"]
    assert resolve_blob_path(state_home, policy["blob_sha256"]).read_bytes() == (
        (live / "policy.md").read_bytes()
    )
    if link is not None:
        linked = entries["policy-stack/external-link"]
        assert linked["object_type"] == "symlink"
        assert os.path.samefile(linked["link_target"], external)
        assert all(
            blob.read_bytes() != b"must-not-be-copied"
            for blob in (state_home / "snapshots" / "blobs").glob("*/*")
            if blob.is_file()
        )

    duplicate = create_snapshot(
        state_home,
        (
            SnapshotSurface("policy-stack", live),
            SnapshotSurface("optional-settings", tmp_path / "missing.json"),
        ),
        label="before campaign",
        campaign_id="campaign-1",
        created_at="2026-08-06T12:00:00Z",
    )
    assert duplicate == manifest
    assert list_snapshots(state_home) == [manifest]
    if os.name == "posix":
        assert (state_home / "snapshots").stat().st_mode & 0o777 == 0o700
        assert (
            resolve_blob_path(
                state_home,
                policy["blob_sha256"],
            )
            .stat()
            .st_mode
            & 0o777
            == 0o600
        )


def test_diff_reports_added_removed_content_type_link_and_mode_drift(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    live = tmp_path / "live"
    live.mkdir()
    changed = live / "changed.txt"
    changed.write_text("before", encoding="utf-8")
    removed = live / "removed.txt"
    removed.write_text("remove me", encoding="utf-8")
    type_drift = live / "type"
    type_drift.write_text("a file", encoding="utf-8")
    mode_drift = live / "mode.sh"
    mode_drift.write_text("#!/bin/sh\n", encoding="utf-8")
    link = live / "link"
    first_target = tmp_path / "first"
    second_target = tmp_path / "second"
    first_target.write_text("one", encoding="utf-8")
    second_target.write_text("two", encoding="utf-8")
    has_link = True
    try:
        link.symlink_to(first_target)
    except OSError:
        has_link = False

    manifest = create_snapshot(
        state_home,
        {"stack": live},
        created_at="2026-08-06T12:00:00Z",
    )
    changed.write_text("after", encoding="utf-8")
    removed.unlink()
    type_drift.unlink()
    type_drift.mkdir()
    (live / "added.txt").write_text("new", encoding="utf-8")
    if os.name == "posix":
        mode_drift.chmod(0o755)
    if has_link:
        link.unlink()
        link.symlink_to(second_target)

    changes = {
        item["logical_path"]: item
        for item in diff_snapshot(state_home, manifest["snapshot_id"])
    }

    assert changes["stack/added.txt"]["status"] == "added"
    assert changes["stack/removed.txt"]["status"] == "removed"
    assert changes["stack/changed.txt"]["drift"] == ["content"]
    assert changes["stack/type"]["drift"] == ["type"]
    if os.name == "posix":
        assert changes["stack/mode.sh"]["drift"] == ["mode"]
    if has_link:
        assert changes["stack/link"]["drift"] == ["link"]
    assert "stack" not in changes


def test_list_validates_manifest_content_and_blob_integrity(tmp_path: Path) -> None:
    state_home = tmp_path / "state"
    live = tmp_path / "live.txt"
    live.write_text("content", encoding="utf-8")
    manifest = create_snapshot(
        state_home,
        {"file": live},
        created_at="2026-08-06T12:00:00Z",
    )
    blob_hash = manifest["entries"][0]["blob_sha256"]
    resolve_blob_path(state_home, blob_hash).write_text(
        "corrupted",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="integrity"):
        list_snapshots(state_home)

    manifest_path = (
        state_home / "snapshots" / "manifests" / f"{manifest['snapshot_id']}.json"
    )
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["label"] = "tampered"
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest content"):
        load_snapshot(state_home, manifest["snapshot_id"], verify_blobs=False)


def test_list_is_newest_first_and_retention_preserves_protected_and_open(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    live = tmp_path / "live.txt"
    snapshots = []
    for index in range(4):
        live.write_text(f"version {index}", encoding="utf-8")
        snapshots.append(
            create_snapshot(
                state_home,
                {"file": live},
                created_at=f"2026-08-0{index + 1}T12:00:00Z",
            )
        )

    assert [item["snapshot_id"] for item in list_snapshots(state_home)] == [
        item["snapshot_id"] for item in reversed(snapshots)
    ]
    removed = enforce_retention(
        state_home,
        keep_last=1,
        protected_snapshot_ids=[snapshots[0]["snapshot_id"]],
        open_snapshot_ids=[snapshots[1]["snapshot_id"]],
    )

    assert removed == [snapshots[2]["snapshot_id"]]
    assert {item["snapshot_id"] for item in list_snapshots(state_home)} == {
        snapshots[0]["snapshot_id"],
        snapshots[1]["snapshot_id"],
        snapshots[3]["snapshot_id"],
    }
    removed_blob = snapshots[2]["entries"][0]["blob_sha256"]
    with pytest.raises(ValueError, match="missing"):
        resolve_blob_path(state_home, removed_blob)
    for retained in (snapshots[0], snapshots[1], snapshots[3]):
        resolve_blob_path(
            state_home,
            retained["entries"][0]["blob_sha256"],
        )


def test_snapshot_creation_is_serialized_with_retention_gc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apu.snapshots as snapshots_module

    state_home = tmp_path / "state"
    live = tmp_path / "live.txt"
    live.write_bytes(b"payload")
    manifest_write_ready = Event()
    release_manifest_write = Event()
    retention_done = Event()
    real_write_json_atomic = snapshots_module.write_json_atomic
    result: dict[str, object] = {}
    errors: list[BaseException] = []

    def paused_manifest_write(path: Path, value: object) -> Path:
        if Path(path).parent.name == "manifests":
            manifest_write_ready.set()
            if not release_manifest_write.wait(timeout=5):
                raise TimeoutError("test did not release manifest publication")
        return real_write_json_atomic(path, value)

    monkeypatch.setattr(
        snapshots_module,
        "write_json_atomic",
        paused_manifest_write,
    )

    def create() -> None:
        try:
            result["manifest"] = create_snapshot(state_home, {"live": live})
        except BaseException as error:  # noqa: BLE001 - thread assertion channel
            errors.append(error)

    def retain() -> None:
        try:
            enforce_retention(state_home, keep_last=1)
        except BaseException as error:  # noqa: BLE001 - thread assertion channel
            errors.append(error)
        finally:
            retention_done.set()

    creator = Thread(target=create)
    creator.start()
    assert manifest_write_ready.wait(timeout=5)
    retention = Thread(target=retain)
    retention.start()
    serialized = not retention_done.wait(timeout=0.2)
    release_manifest_write.set()
    creator.join(timeout=10)
    retention.join(timeout=10)

    assert serialized
    assert not errors
    manifest = result["manifest"]
    assert isinstance(manifest, dict)
    assert load_snapshot(state_home, manifest["snapshot_id"]) == manifest


def test_snapshot_capture_enforces_entry_limit_before_publication(
    tmp_path: Path,
) -> None:
    live = tmp_path / "live"
    live.mkdir()
    (live / "one").write_bytes(b"1")
    (live / "two").write_bytes(b"2")
    state_home = tmp_path / "state"

    with pytest.raises(ValueError, match="entry limit"):
        create_snapshot(
            state_home,
            {"live": live},
            max_entries=2,
        )

    assert not list((state_home / "snapshots" / "manifests").glob("*.json"))


def test_snapshot_capture_enforces_byte_limit_before_publication(
    tmp_path: Path,
) -> None:
    live = tmp_path / "live"
    live.mkdir()
    (live / "payload").write_bytes(b"12345")
    state_home = tmp_path / "state"

    with pytest.raises(ValueError, match="byte limit"):
        create_snapshot(
            state_home,
            {"live": live},
            max_bytes=4,
        )

    assert not list((state_home / "snapshots" / "manifests").glob("*.json"))


def test_snapshot_capture_enforces_depth_limit_before_publication(
    tmp_path: Path,
) -> None:
    live = tmp_path / "live"
    nested = live / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "payload").write_bytes(b"content")
    state_home = tmp_path / "state"

    with pytest.raises(ValueError, match="depth limit"):
        create_snapshot(
            state_home,
            {"live": live},
            max_depth=1,
        )

    assert not list((state_home / "snapshots" / "manifests").glob("*.json"))


def test_snapshot_rejects_duplicate_roots_and_unsafe_logical_paths(
    tmp_path: Path,
) -> None:
    live = tmp_path / "live"
    live.mkdir()
    with pytest.raises(ValueError, match="duplicate surface root"):
        create_snapshot(
            tmp_path / "state",
            (
                SnapshotSurface("one", live),
                SnapshotSurface("two", live),
            ),
        )
    with pytest.raises(ValueError, match="safe relative"):
        create_snapshot(tmp_path / "state", {"../escape": live})


def test_materialize_reconstructs_directory_file_empty_dir_and_nested_link(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    live = tmp_path / "live"
    nested = live / "nested"
    nested.mkdir(parents=True)
    (nested / "value.bin").write_bytes(b"\x00snapshot bytes\xff")
    (nested / "empty").mkdir()
    if os.name == "posix":
        (nested / "value.bin").chmod(0o640)
        nested.chmod(0o750)
    link = nested / "relative-link"
    try:
        link.symlink_to("../outside-target")
    except OSError:
        link = None
    manifest = create_snapshot(
        state_home,
        {"stack": live},
        created_at="2026-08-06T12:00:00Z",
    )

    destination = (tmp_path / "prepared-tree").absolute()
    result = materialize_snapshot_object(
        state_home,
        manifest,
        "stack",
        destination,
    )

    assert result == destination
    assert (destination / "nested" / "value.bin").read_bytes() == (
        b"\x00snapshot bytes\xff"
    )
    assert (destination / "nested" / "empty").is_dir()
    if link is not None:
        restored_link = destination / "nested" / "relative-link"
        assert restored_link.is_symlink()
        assert os.readlink(restored_link) == "../outside-target"
    if os.name == "posix":
        assert (destination / "nested").stat().st_mode & 0o777 == 0o750
        assert (destination / "nested" / "value.bin").stat().st_mode & 0o777 == 0o640

    file_destination = (tmp_path / "prepared-file").absolute()
    materialize_snapshot_object(
        state_home,
        manifest,
        "stack/nested/value.bin",
        file_destination,
    )
    assert file_destination.read_bytes() == b"\x00snapshot bytes\xff"


def test_materialize_missing_surface_returns_none_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    live = tmp_path / "live.txt"
    live.write_bytes(b"captured")
    missing = tmp_path / "missing.txt"
    manifest = create_snapshot(
        state_home,
        {"file": live, "optional": missing},
        created_at="2026-08-06T12:00:00Z",
    )
    unused = (tmp_path / "unused").absolute()

    assert (
        materialize_snapshot_object(
            state_home,
            manifest,
            "optional",
            unused,
        )
        is None
    )
    assert not unused.exists()

    occupied = tmp_path / "occupied"
    occupied.write_bytes(b"do not overwrite")
    with pytest.raises(FileExistsError, match="already exists"):
        materialize_snapshot_object(
            state_home,
            manifest,
            "file",
            occupied.absolute(),
        )
    assert occupied.read_bytes() == b"do not overwrite"
    with pytest.raises(KeyError, match="not present"):
        materialize_snapshot_object(
            state_home,
            manifest,
            "file/unknown",
            (tmp_path / "unknown").absolute(),
        )


def test_materialize_refuses_linked_destination_ancestor(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    live = tmp_path / "live.txt"
    live.write_bytes(b"captured")
    manifest = create_snapshot(
        state_home,
        {"file": live},
        created_at="2026-08-06T12:00:00Z",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    with pytest.raises(ValueError, match="linked ancestor"):
        materialize_snapshot_object(
            state_home,
            manifest,
            "file",
            linked_parent / "escaped",
        )
    assert not (outside / "escaped").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction assertion")
def test_materialize_reconstructs_nested_junction(tmp_path: Path) -> None:
    try:
        import _winapi
    except ImportError:
        pytest.skip("the Python runtime cannot create junctions")
    create_junction = getattr(_winapi, "CreateJunction", None)
    if create_junction is None:
        pytest.skip("the Python runtime cannot create junctions")

    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "secret").write_bytes(b"not copied into snapshot")
    live = tmp_path / "live"
    live.mkdir()
    junction = live / "nested-junction"
    try:
        create_junction(str(target), str(junction))
    except OSError as error:
        pytest.skip(f"junction creation is unavailable: {error}")

    state_home = tmp_path / "state"
    manifest = create_snapshot(
        state_home,
        {"stack": live},
        created_at="2026-08-06T12:00:00Z",
    )
    destination = (tmp_path / "materialized").absolute()
    materialize_snapshot_object(
        state_home,
        manifest,
        "stack",
        destination,
    )

    restored = destination / "nested-junction"
    assert is_junction(restored)
    assert restored.resolve() == target.resolve()

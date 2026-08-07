from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apu.filesystem import hash_object
from apu.restore_journal import (
    RestoreError,
    RestoreInterrupted,
    RestoreItem,
    RestorePreflightError,
    hash_restore_object,
    list_restore_journals,
    restore_items,
    resume_restore,
)


def item(target: Path, replacement: Path | None) -> RestoreItem:
    if os.path.lexists(target):
        if getattr(os.path, "isjunction", lambda _: False)(target):
            object_type = "junction"
        elif target.is_symlink():
            object_type = "symlink"
        elif target.is_file():
            object_type = "file"
        else:
            object_type = "directory"
        digest = hash_restore_object(target)
    else:
        object_type = "absent"
        digest = None
    return RestoreItem(
        target=target.absolute(),
        replacement=replacement.absolute() if replacement is not None else None,
        expected_type=object_type,
        expected_sha256=digest,
    )


def read_journal(root: Path, journal_id: str) -> dict[str, object]:
    return json.loads(
        (root / journal_id / "journal.json").read_text(encoding="utf-8")
    )


def test_list_restore_journals_is_validated_deterministic_and_read_only(
    tmp_path: Path,
) -> None:
    journal_root = tmp_path / "journals"
    assert list_restore_journals(journal_root) == []
    assert not journal_root.exists()

    targets = [tmp_path / "target-z", tmp_path / "target-a"]
    replacements = [tmp_path / "new-z", tmp_path / "new-a"]
    for index, target in enumerate(targets):
        target.write_text(f"old-{index}", encoding="utf-8")
        replacements[index].write_text(f"new-{index}", encoding="utf-8")
    restore_items(
        [item(targets[0], replacements[0])],
        journal_root,
        journal_id="zeta",
    )
    restore_items(
        [item(targets[1], replacements[1])],
        journal_root,
        journal_id="alpha",
    )
    targets[0].write_bytes(b"out-of-band drift")
    journal_paths = sorted(journal_root.glob("*/journal.json"))
    mtimes = {path: path.stat().st_mtime_ns for path in journal_paths}

    first = list_restore_journals(journal_root)
    second = list_restore_journals(journal_root)

    assert first == second
    assert [summary["journal_id"] for summary in first] == ["alpha", "zeta"]
    assert [summary["item_count"] for summary in first] == [1, 1]
    assert first[0]["status"] == "completed"
    assert first[0]["targets"][0]["state"] == "desired"
    assert first[1]["targets"][0]["state"] == "unknown"
    assert (
        first[1]["targets"][0]["observed"]["target"]["sha256"]
        == hash_restore_object(targets[0])
    )
    assert {path: path.stat().st_mtime_ns for path in journal_paths} == mtimes


def test_snapshot_id_is_persisted_validated_and_listed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    replacement = tmp_path / "replacement"
    target.write_bytes(b"old")
    replacement.write_bytes(b"new")
    snapshot_id = "a" * 64
    journal_root = tmp_path / "journals"

    restore_items(
        [item(target, replacement)],
        journal_root,
        snapshot_id=snapshot_id,
        journal_id="snapshot-bound",
    )

    journal = read_journal(journal_root, "snapshot-bound")
    assert journal["snapshot_id"] == snapshot_id
    summaries = list_restore_journals(journal_root)
    assert summaries[0]["snapshot_id"] == snapshot_id


@pytest.mark.parametrize("snapshot_id", ["short", "A" * 64])
def test_invalid_snapshot_id_is_rejected_before_journal_creation(
    tmp_path: Path, snapshot_id: str
) -> None:
    target = tmp_path / "target"
    replacement = tmp_path / "replacement"
    target.write_bytes(b"old")
    replacement.write_bytes(b"new")
    journal_root = tmp_path / "journals"

    with pytest.raises(RestorePreflightError, match="snapshot_id"):
        restore_items(
            [item(target, replacement)],
            journal_root,
            snapshot_id=snapshot_id,
            journal_id="invalid-snapshot",
        )

    assert not journal_root.exists()


def test_legacy_journal_without_snapshot_id_loads_as_unbound(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    replacement = tmp_path / "replacement"
    target.write_bytes(b"old")
    replacement.write_bytes(b"new")
    journal_root = tmp_path / "journals"
    result = restore_items(
        [item(target, replacement)],
        journal_root,
        journal_id="legacy",
    )
    journal = read_journal(journal_root, "legacy")
    assert journal.pop("snapshot_id") is None
    result.journal_path.write_text(
        json.dumps(journal, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    assert list_restore_journals(journal_root)[0]["snapshot_id"] is None
    resumed = resume_restore(journal_root, "legacy")
    assert resumed.status == "completed"
    assert read_journal(journal_root, "legacy")["snapshot_id"] is None


def test_corrupt_persisted_snapshot_id_blocks_list_and_resume(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    replacement = tmp_path / "replacement"
    target.write_bytes(b"old")
    replacement.write_bytes(b"new")
    journal_root = tmp_path / "journals"
    result = restore_items(
        [item(target, replacement)],
        journal_root,
        snapshot_id="b" * 64,
        journal_id="bad-binding",
    )
    journal = read_journal(journal_root, "bad-binding")
    journal["snapshot_id"] = "B" * 64
    result.journal_path.write_text(
        json.dumps(journal, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(RestorePreflightError, match="snapshot_id"):
        list_restore_journals(journal_root)
    with pytest.raises(RestorePreflightError, match="snapshot_id"):
        resume_restore(journal_root, "bad-binding")


def test_list_restore_journals_fails_closed_on_corruption(
    tmp_path: Path,
) -> None:
    journal_root = tmp_path / "journals"
    corrupt = journal_root / "corrupt"
    corrupt.mkdir(parents=True)
    (corrupt / "journal.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(RestoreError, match="cannot load restore journal"):
        list_restore_journals(journal_root)

    (corrupt / "journal.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "journal_id": "wrong-id",
                "status": "completed",
                "items": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RestoreError, match="identity"):
        list_restore_journals(journal_root)

    (corrupt / "journal.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "journal_id": "corrupt",
                "status": "invented-state",
                "items": [{"index": 0}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RestoreError):
        list_restore_journals(journal_root)


def test_list_restore_journals_rejects_unexpected_root_entries(
    tmp_path: Path,
) -> None:
    journal_root = tmp_path / "journals"
    journal_root.mkdir()
    (journal_root / "stray-file").write_bytes(b"not a journal")

    with pytest.raises(RestoreError, match="unexpected entry"):
        list_restore_journals(journal_root)


def test_restore_journals_original_before_replacing_exact_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "policy.md"
    target.write_bytes(b"current")
    replacement = tmp_path / "snapshot-policy.md"
    replacement.write_bytes(b"snapshot")
    journal_root = tmp_path / "state" / "restore-journals"

    result = restore_items(
        [item(target, replacement)],
        journal_root,
        journal_id="restore-1",
    )

    assert result.status == "completed"
    assert target.read_bytes() == b"snapshot"
    journal = read_journal(journal_root, "restore-1")
    assert journal["schema_version"] == 1
    assert journal["status"] == "completed"
    record = journal["items"][0]
    assert Path(record["original"]["artifact"]).read_bytes() == b"current"
    assert record["original"]["sha256"] != record["desired"]["sha256"]
    assert record["observed"]["target"]["sha256"] == record["desired"]["sha256"]
    assert not os.path.lexists(record["prepared_path"])
    assert not os.path.lexists(record["displaced_path"])
    # Atomic JSON serialization is canonical and stable.
    raw = result.journal_path.read_text(encoding="utf-8")
    assert raw == json.dumps(journal, sort_keys=True, separators=(",", ":"))


def test_preflight_checks_every_item_before_mutating_any_target(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"first-current")
    second.write_bytes(b"second-current")
    first_replacement = tmp_path / "first-new"
    second_replacement = tmp_path / "second-new"
    first_replacement.write_bytes(b"first-new")
    second_replacement.write_bytes(b"second-new")
    first_item = item(first, first_replacement)
    second_item = item(second, second_replacement)
    second.write_bytes(b"drift")

    with pytest.raises(RestorePreflightError, match="drifted"):
        restore_items(
            [first_item, second_item],
            tmp_path / "journals",
            journal_id="drift",
        )

    assert first.read_bytes() == b"first-current"
    assert second.read_bytes() == b"drift"
    assert not (tmp_path / "journals" / "drift").exists()


def test_force_is_scoped_to_an_exact_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"before")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"after")
    restore_item = item(target, replacement)
    target.write_bytes(b"drift")

    with pytest.raises(RestorePreflightError, match="exact restore targets"):
        restore_items(
            [restore_item],
            tmp_path / "journals",
            force_paths=[tmp_path.absolute()],
            journal_id="bad-force",
        )

    result = restore_items(
        [restore_item],
        tmp_path / "journals",
        force_paths=[target.absolute()],
        journal_id="forced",
    )

    assert result.status == "completed"
    assert target.read_bytes() == b"after"
    journal = read_journal(tmp_path / "journals", "forced")
    assert journal["items"][0]["forced"] is True
    assert Path(journal["items"][0]["original"]["artifact"]).read_bytes() == b"drift"


def test_restore_preserves_directory_empty_entries_and_symlinks(
    tmp_path: Path,
) -> None:
    target = tmp_path / "surface"
    target.mkdir()
    (target / "old").write_bytes(b"old")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "empty").mkdir()
    (replacement / "value").write_bytes(b"value")
    link = replacement / "link"
    try:
        link.symlink_to("missing-target")
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    expected_hash = hash_object(replacement)

    restore_items(
        [item(target, replacement)],
        tmp_path / "journals",
        journal_id="tree",
    )

    assert hash_object(target) == expected_hash
    assert (target / "empty").is_dir()
    assert (target / "link").is_symlink()
    assert os.readlink(target / "link") == "missing-target"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction assertion")
def test_restore_round_trips_junctions_without_hashing_their_contents(
    tmp_path: Path,
) -> None:
    try:
        import _winapi
    except ImportError:
        pytest.skip("the Python runtime cannot create junctions")
    create_junction = getattr(_winapi, "CreateJunction", None)
    if create_junction is None:
        pytest.skip("the Python runtime cannot create junctions")

    original_destination = tmp_path / "original-destination"
    desired_destination = tmp_path / "desired-destination"
    original_destination.mkdir()
    desired_destination.mkdir()
    (original_destination / "content").write_bytes(b"original contents")
    (desired_destination / "content").write_bytes(b"desired contents")
    target = tmp_path / "target-junction"
    replacement = tmp_path / "replacement-junction"
    wrapper = tmp_path / "wrapper"
    nested = wrapper / "nested-junction"
    wrapper.mkdir()
    try:
        create_junction(str(original_destination), str(target))
        create_junction(str(desired_destination), str(replacement))
        create_junction(str(original_destination), str(nested))
    except OSError as error:
        pytest.skip(f"junction creation is unavailable: {error}")

    target_hash = hash_restore_object(target)
    wrapper_hash = hash_restore_object(wrapper)
    (original_destination / "content").write_bytes(b"changed behind junction")
    assert hash_restore_object(target) == target_hash
    assert hash_restore_object(wrapper) == wrapper_hash

    restore_items(
        [item(target, replacement)],
        tmp_path / "journals",
        journal_id="junction",
    )

    assert os.path.isjunction(target)
    assert target.resolve() == desired_destination.resolve()
    journal = read_journal(tmp_path / "journals", "junction")
    original_artifact = Path(journal["items"][0]["original"]["artifact"])
    assert journal["items"][0]["original"]["type"] == "junction"
    assert original_artifact.is_junction()
    assert original_artifact.resolve() == original_destination.resolve()

    resume_restore(tmp_path / "journals", "junction", unwind=True)
    assert target.is_junction()
    assert target.resolve() == original_destination.resolve()


def test_first_swap_failure_reverses_completed_targets(tmp_path: Path) -> None:
    targets = [tmp_path / "one", tmp_path / "two"]
    replacements = [tmp_path / "new-one", tmp_path / "new-two"]
    for index, target in enumerate(targets):
        target.write_text(f"old-{index}", encoding="utf-8")
        replacements[index].write_text(f"new-{index}", encoding="utf-8")

    def fail_second(event: str, index: int, target: Path) -> None:
        del target
        if event == "before_swap" and index == 1:
            raise OSError("injected swap failure")

    with pytest.raises(RestoreError, match="all completed swaps were reversed"):
        restore_items(
            [item(targets[0], replacements[0]), item(targets[1], replacements[1])],
            tmp_path / "journals",
            journal_id="rollback",
            failure_hook=fail_second,
        )

    assert [path.read_text(encoding="utf-8") for path in targets] == [
        "old-0",
        "old-1",
    ]
    journal = read_journal(tmp_path / "journals", "rollback")
    assert journal["status"] == "rolled_back"
    assert [entry["state"] for entry in journal["items"]] == [
        "original",
        "original",
    ]


def test_incomplete_reverse_is_journaled_and_can_resume_to_completion(
    tmp_path: Path,
) -> None:
    targets = [tmp_path / "one", tmp_path / "two"]
    replacements = [tmp_path / "new-one", tmp_path / "new-two"]
    for index, target in enumerate(targets):
        target.write_text(f"old-{index}", encoding="utf-8")
        replacements[index].write_text(f"new-{index}", encoding="utf-8")

    def fail_swap_and_reverse(event: str, index: int, target: Path) -> None:
        del target
        if event == "before_swap" and index == 1:
            raise OSError("injected swap failure")
        if event == "before_unwind" and index == 0:
            raise OSError("injected reverse failure")

    with pytest.raises(RestoreInterrupted) as raised:
        restore_items(
            [item(targets[0], replacements[0]), item(targets[1], replacements[1])],
            tmp_path / "journals",
            journal_id="resume-complete",
            failure_hook=fail_swap_and_reverse,
        )

    assert raised.value.journal_id == "resume-complete"
    journal = read_journal(tmp_path / "journals", "resume-complete")
    assert journal["status"] == "needs_recovery"
    assert journal["items"][0]["state"] == "desired"
    assert journal["items"][1]["state"] == "original"

    result = resume_restore(tmp_path / "journals", "resume-complete")
    again = resume_restore(tmp_path / "journals", "resume-complete")

    assert result.status == again.status == "completed"
    assert [path.read_text(encoding="utf-8") for path in targets] == [
        "new-0",
        "new-1",
    ]


def test_incomplete_reverse_can_resume_as_an_idempotent_unwind(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    replacement = tmp_path / "replacement"
    target.write_bytes(b"old")
    replacement.write_bytes(b"new")

    def fail_after_swap_and_reverse(event: str, index: int, path: Path) -> None:
        del index, path
        if event == "after_swap":
            raise OSError("injected post-swap failure")
        if event == "before_unwind":
            raise OSError("injected reverse failure")

    with pytest.raises(RestoreInterrupted):
        restore_items(
            [item(target, replacement)],
            tmp_path / "journals",
            journal_id="resume-unwind",
            failure_hook=fail_after_swap_and_reverse,
        )

    first = resume_restore(
        tmp_path / "journals", "resume-unwind", unwind=True
    )
    second = resume_restore(
        tmp_path / "journals", "resume-unwind", unwind=True
    )

    assert first.status == second.status == "unwound"
    assert target.read_bytes() == b"old"


def test_absence_intents_are_journaled_and_recoverable(tmp_path: Path) -> None:
    removed = tmp_path / "removed"
    created = tmp_path / "created"
    source = tmp_path / "source"
    removed.write_bytes(b"remove-me")
    source.write_bytes(b"create-me")

    restore_items(
        [item(removed, None), item(created, source)],
        tmp_path / "journals",
        journal_id="absence",
    )

    assert not os.path.lexists(removed)
    assert created.read_bytes() == b"create-me"
    resume_restore(tmp_path / "journals", "absence", unwind=True)
    assert removed.read_bytes() == b"remove-me"
    assert not os.path.lexists(created)


def test_protected_roots_and_symlinked_ancestors_are_rejected(
    tmp_path: Path,
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"new")

    with pytest.raises(RestorePreflightError, match="protected root"):
        restore_items(
            [item(protected, replacement)],
            tmp_path / "journals",
            protected_roots=[protected.absolute()],
            journal_id="protected",
        )

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    target = linked / "target"
    target.write_bytes(b"old")
    with pytest.raises(RestorePreflightError, match="symlinked ancestor"):
        restore_items(
            [item(target, replacement)],
            tmp_path / "journals",
            journal_id="linked-parent",
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode assertion")
def test_journal_directory_is_private(tmp_path: Path) -> None:
    target = tmp_path / "target"
    replacement = tmp_path / "replacement"
    target.write_bytes(b"old")
    replacement.write_bytes(b"new")

    result = restore_items(
        [item(target, replacement)],
        tmp_path / "journals",
        journal_id="private",
    )

    assert (result.journal_path.parent.stat().st_mode & 0o777) == 0o700

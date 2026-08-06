from pathlib import Path

import pytest

from apu.filesystem import hash_object


def test_tree_hash_tracks_empty_directories(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    before = hash_object(tree)
    (tree / "empty").mkdir()

    assert hash_object(tree) != before


def test_tree_hash_distinguishes_file_from_symlink(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    target = tmp_path / "target"
    target.write_text("same bytes", encoding="utf-8")
    entry = tree / "entry"
    entry.write_text("same bytes", encoding="utf-8")
    regular = hash_object(tree)
    entry.unlink()
    try:
        entry.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    assert hash_object(tree) != regular

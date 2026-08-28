from __future__ import annotations

import json
from pathlib import Path

from apu.cli import main


def test_cli_system_audit_writes_rollup_inventory(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    repository = tmp_path / "projects" / "repo"
    (repository / ".git").mkdir(parents=True)
    (repository / "AGENTS.md").write_text("repository policy\n", encoding="utf-8")
    profile = tmp_path / "profile.toml"
    profile.write_text(
        f'roots = ["{(tmp_path / "projects").as_posix()}"]\n'
        f'global_surfaces = ["{(home / ".codex").as_posix()}"]\n',
        encoding="utf-8",
    )
    output = tmp_path / "system-inventory.json"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    assert (
        main(
            [
                "system",
                "audit",
                "--profile",
                str(profile),
                "--json",
                str(output),
            ]
        )
        == 0
    )

    inventory = json.loads(output.read_text(encoding="utf-8"))
    assert inventory["schema_version"] == 2
    assert inventory["repositories"][0]["repository"] == str(repository.resolve())
    assert inventory["profile_sha256"]


def test_cli_snapshot_create_diff_and_list(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    global_root = home / ".codex"
    global_root.mkdir(parents=True)
    policy = global_root / "AGENTS.md"
    policy.write_text("before\n", encoding="utf-8")
    projects = tmp_path / "projects"
    projects.mkdir()
    profile = tmp_path / "profile.toml"
    profile.write_text(
        f'roots = ["{projects.as_posix()}"]\n'
        f'global_surfaces = ["{global_root.as_posix()}"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APU_HOME", str(state))

    assert (
        main(
            [
                "snapshot",
                "create",
                "--profile",
                str(profile),
                "--label",
                "before-change",
            ]
        )
        == 0
    )
    manifest = json.loads(capsys.readouterr().out)
    snapshot_id = manifest["snapshot_id"]
    policy.write_text("after\n", encoding="utf-8")

    assert main(["snapshot", "diff", snapshot_id]) == 0
    diff = json.loads(capsys.readouterr().out)
    assert any(change["status"] == "changed" for change in diff["changes"])

    assert main(["snapshot", "list"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["snapshots"][0]["snapshot_id"] == snapshot_id

    assert main(["snapshot", "restore", snapshot_id]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["status"] == "completed"
    assert policy.read_text(encoding="utf-8") == "before\n"

    assert main(["system", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["snapshots"][0]["snapshot_id"] == snapshot_id
    assert status["restore_journals"][0]["journal_id"] == restored["journal_id"]
    assert status["restore_journals"][0]["snapshot_id"] == snapshot_id

    for index in range(10):
        assert (
            main(
                [
                    "snapshot",
                    "create",
                    "--profile",
                    str(profile),
                    "--label",
                    f"retention-{index}",
                ]
            )
            == 0
        )
        capsys.readouterr()
    assert main(["snapshot", "list"]) == 0
    retained = json.loads(capsys.readouterr().out)["snapshots"]
    assert len(retained) == 10
    assert snapshot_id not in {item["snapshot_id"] for item in retained}

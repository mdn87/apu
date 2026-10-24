from __future__ import annotations

import json

import pytest
from test_package_upgrade import _installed_claude_package, _latest_only_help

from apu.claude_package_upgrade import ClaudePackageUpgradeExecutor
from apu.package_coordinates import parse_profile_package
from apu.package_transactions import execute_package_upgrade
from apu.package_upgrade import PackageUpgradeRequest, assess_claude_package_upgrade
from apu.snapshots import enforce_retention, load_snapshot


@pytest.fixture
def setup(tmp_path):
    home, tree = _installed_claude_package(tmp_path)
    state = tmp_path / "state"
    coordinate = parse_profile_package("superpowers@claude-plugins-official")
    settings = home / ".claude/settings.json"
    settings.write_text(
        '{"enabledPlugins":{"superpowers@claude-plugins-official":true},"other":"keep"}'
    )
    before = {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}

    def update(*args, **kwargs):
        target = tree.parent / "6.0.0"
        (target / ".claude-plugin").mkdir(parents=True)
        (target / ".claude-plugin/plugin.json").write_text(
            '{"name":"superpowers","version":"6.0.0"}'
        )
        (target / "SKILL.md").write_text("new version")
        metadata = home / ".claude/plugins/installed_plugins.json"
        value = json.loads(metadata.read_text())
        value["plugins"][coordinate.profile_selector][0].update(
            version="6.0.0", installPath=str(target)
        )
        metadata.write_text(json.dumps(value))
        settings.write_text('{"changed-by-update":true}')
        return 0

    capability = assess_claude_package_upgrade(
        coordinate, home=home, help_runner=_latest_only_help
    )
    request = PackageUpgradeRequest(
        coordinate.package_id, "upgrade", 1, capability.pre_state, "6.0.0", "b" * 64
    )
    executor = ClaudePackageUpgradeExecutor(state, home, request, runner=update)
    return state, home, coordinate, request, capability, executor, before


def test_latest_only_update_can_restore_complete_claude_state(setup):
    state, home, coordinate, request, capability, executor, before = setup
    assert capability.status == "available"
    assert not capability.exact_version_supported
    assert capability.rollback_method == "apu-snapshot"
    receipt = execute_package_upgrade(
        state, coordinate, request, capability, executor=executor
    )
    assert receipt["status"] == "rolled-back"  # Deliberately wrong target hash.
    assert {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()} == before
    assert executor.verify_rollback()
    point = json.loads(executor.point_path.read_text())
    assert point["restore_journal_id"]
    assert point["snapshot_id"] not in enforce_retention(state, keep_last=0)
    load_snapshot(state, point["snapshot_id"])


@pytest.mark.parametrize("drift", ["settings", "other-plugin"])
def test_restore_refuses_later_changes_without_touching_files(setup, drift):
    state, home, coordinate, request, _capability, executor, _before = setup
    executor.update_exact(coordinate, request)
    changed = home / (
        ".claude/settings.json" if drift == "settings" else ".claude/plugins/other.txt"
    )
    changed.write_text("a later user change")
    before = {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    fresh = ClaudePackageUpgradeExecutor(state, home, request)
    with pytest.raises(RuntimeError, match="changed"):
        fresh.rollback_exact(coordinate, request.pre_state)
    assert {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


def test_restore_point_survives_new_executor_and_verifies_settings(setup):
    state, home, coordinate, request, _capability, executor, before = setup
    executor.update_exact(coordinate, request)
    fresh = ClaudePackageUpgradeExecutor(state, home, request)
    fresh.rollback_exact(coordinate, request.pre_state)
    assert fresh.observe(coordinate) == request.pre_state
    assert {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()} == before
    assert fresh.verify_rollback()


def test_changed_pre_state_is_refused_before_snapshot_or_update(setup):
    _state, home, coordinate, request, _capability, executor, _before = setup
    (
        home
        / ".claude/plugins/cache/claude-plugins-official/superpowers/5.0.7/SKILL.md"
    ).write_text("changed")
    with pytest.raises(RuntimeError, match="changed"):
        executor.update_exact(coordinate, request)
    assert not executor.point_path.exists()


def test_backup_failure_never_runs_provider(setup, monkeypatch):
    _state, _home, coordinate, request, _capability, executor, _before = setup
    calls = []
    executor.runner = lambda *a, **kw: calls.append(True)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("apu.claude_package_upgrade.create_snapshot", fail)
    with pytest.raises(OSError, match="disk full"):
        executor.update_exact(coordinate, request)
    assert not calls


@pytest.mark.parametrize("abrupt", [False, True])
def test_interrupted_restore_resumes_existing_journal(setup, monkeypatch, abrupt):
    from apu.restore_journal import RestoreInterrupted
    from apu.snapshot_restore import restore_items

    state, home, coordinate, request, _capability, executor, _before = setup
    executor.update_exact(coordinate, request)

    def fail(event, index, path):
        if event == "before_swap" and index == 1:
            raise OSError("simulated swap failure")
        if event == "before_unwind" and index == 0:
            raise OSError("simulated unwind failure")

    def interrupted(*args, **kwargs):
        try:
            return restore_items(*args, **kwargs, failure_hook=fail)
        except RestoreInterrupted:
            if abrupt:
                raise KeyboardInterrupt(
                    "process stopped before executor saved recovery state"
                )
            raise

    with monkeypatch.context() as patch:
        patch.setattr("apu.snapshot_restore.restore_items", interrupted)
        with pytest.raises(KeyboardInterrupt if abrupt else RestoreInterrupted):
            executor.rollback_exact(coordinate, request.pre_state)
    fresh = ClaudePackageUpgradeExecutor(state, home, request)
    restored = fresh.rollback_exact(coordinate, request.pre_state)
    assert restored["status"] == "restored"
    assert fresh.verify_rollback()


def test_unknown_post_crash_state_requires_explicit_digest(setup):
    _state, _home, coordinate, request, _capability, executor, _before = setup
    executor.update_exact(coordinate, request)
    point = json.loads(executor.point_path.read_text())
    point["after"] = None
    point["status"] = "prepared"
    executor.point_path.write_text(json.dumps(point))
    with pytest.raises(RuntimeError, match="interrupted"):
        executor.rollback_exact(coordinate, request.pre_state)
    with pytest.raises(RuntimeError, match="changed"):
        executor.rollback_exact(coordinate, request.pre_state, expected_state="a" * 64)
    digest = executor.current_state_digest()
    executor.rollback_exact(coordinate, request.pre_state, expected_state=digest)
    assert executor.verify_rollback()


def test_corrupt_snapshot_never_overwrites_current_state(setup):
    state, home, coordinate, request, _capability, executor, _before = setup
    executor.update_exact(coordinate, request)
    blob = next(p for p in (state / "snapshots/blobs").rglob("*") if p.is_file())
    blob.write_bytes(b"corrupt")
    before = {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        executor.rollback_exact(coordinate, request.pre_state)
    assert {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


def test_package_identity_alone_cannot_claim_settings_restored(setup, monkeypatch):
    state, home, coordinate, request, capability, executor, _before = setup
    original = executor.rollback_exact

    def incomplete(*args, **kwargs):
        original(*args, **kwargs)
        (home / ".claude/settings.json").write_text("not restored")

    monkeypatch.setattr(executor, "rollback_exact", incomplete)
    receipt = execute_package_upgrade(
        state, coordinate, request, capability, executor=executor
    )
    assert receipt["status"] == "rollback-failed"
    assert executor.observe(coordinate) == request.pre_state


def test_cli_update_retry_status_and_manual_restore(setup, monkeypatch, capsys):
    from apu.cli import main

    state, home, coordinate, request, capability, executor, _before = setup
    candidate = home.parent / "reviewed"
    (candidate / ".claude-plugin").mkdir(parents=True)
    (candidate / ".claude-plugin/plugin.json").write_text(
        '{"name":"superpowers","version":"6.0.0"}'
    )
    (candidate / "SKILL.md").write_text("new version")
    calls = []

    def update(*args, **kwargs):
        calls.append(True)
        return executor.runner(*args, **kwargs)

    monkeypatch.setenv("APU_HOME", str(state))
    monkeypatch.setattr(
        "apu.package_upgrade_cli.assess_claude_package_upgrade",
        lambda *a, **kw: capability,
    )
    monkeypatch.setattr("apu.claude_package_upgrade.run_claude_update", update)
    args = [
        "packages",
        "upgrade",
        coordinate.profile_selector,
        "--target-version",
        "6.0.0",
        "--target-tree",
        str(candidate),
        "--operation-id",
        "cli",
        "--claude-home",
        str(home),
    ]
    assert main(args) == 0
    receipt = json.loads(capsys.readouterr().out)
    identifier = receipt["transaction_id"]
    assert main(args) == 0
    capsys.readouterr()
    assert len(calls) == 1
    assert main(["packages", "status", identifier, "--claude-home", str(home)]) == 0
    assert json.loads(capsys.readouterr().out)["current_state_sha256"]
    assert main(["packages", "rollback", identifier, "--claude-home", str(home)]) == 0
    assert json.loads(capsys.readouterr().out)["receipt"]["status"] == "rolled-back"
    assert executor.observe(coordinate) == request.pre_state


@pytest.mark.parametrize("scope", ["project", "local"])
def test_scoped_update_runs_in_project_and_restores_its_settings(
    setup, monkeypatch, scope
):
    from pathlib import Path
    from types import SimpleNamespace

    from apu.claude_package_upgrade import run_claude_update

    state, home, coordinate, _request, _capability, executor, _before = setup
    project = home.parent / "project"
    (project / ".claude").mkdir(parents=True)
    settings = (
        project
        / ".claude"
        / ("settings.local.json" if scope == "local" else "settings.json")
    )
    settings.write_text('{"keep": true}')
    registry = home / ".claude/plugins/installed_plugins.json"
    value = json.loads(registry.read_text())
    value["plugins"][coordinate.profile_selector][0].update(
        scope=scope, projectPath=str(project)
    )
    registry.write_text(json.dumps(value))
    capability = assess_claude_package_upgrade(
        coordinate, home=home, help_runner=_latest_only_help
    )
    request = PackageUpgradeRequest(
        coordinate.package_id, "scoped", 1, capability.pre_state, "6.0.0", "b" * 64
    )
    monkeypatch.setattr(
        "apu.claude_package_upgrade.shutil.which", lambda _: "claude.exe"
    )
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        executor.runner()
        settings.write_text('{"changed": true}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("apu.claude_package_upgrade.run_confined", run)
    scoped = ClaudePackageUpgradeExecutor(
        state, home, request, runner=run_claude_update
    )
    scoped.update_exact(coordinate, request)
    assert Path(calls[0][1]["cwd"]) == project
    assert calls[0][0][-3:] == ["--scope", scope, "--json"]
    scoped.rollback_exact(coordinate, request.pre_state)
    assert settings.read_text() == '{"keep": true}'


def test_recovery_and_completed_retry_require_full_restore(setup, monkeypatch):
    from apu.package_transactions import (
        PackageUpgradeRecoveryRequired,
        recover_package_upgrade,
    )

    state, home, coordinate, request, capability, executor, _before = setup
    original = executor.rollback_exact

    def incomplete(*args, **kwargs):
        original(*args, **kwargs)
        (home / ".claude/settings.json").write_text("later change")

    monkeypatch.setattr(executor, "rollback_exact", incomplete)
    assert (
        execute_package_upgrade(
            state, coordinate, request, capability, executor=executor
        )["status"]
        == "rollback-failed"
    )
    receipt = recover_package_upgrade(
        state,
        coordinate,
        request,
        capability,
        executor=executor,
        expected_current=request.pre_state,
    )
    assert receipt["status"] == "rollback-failed"
    # Simulate a prior completed receipt followed by a settings-only change.
    journal = next((state / "transactions/package-upgrades").rglob("*.json"))
    value = json.loads(journal.read_text())
    value["status"] = "rolled-back"
    journal.write_text(json.dumps(value))
    with pytest.raises(PackageUpgradeRecoveryRequired):
        execute_package_upgrade(
            state, coordinate, request, capability, executor=executor
        )

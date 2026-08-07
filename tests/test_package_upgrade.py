from __future__ import annotations

import json
from pathlib import Path

import pytest

from apu.models import sha256_bytes
from apu.package_coordinates import parse_profile_package
from apu.package_upgrade import (
    HelpCommandResult,
    PackageStateIdentity,
    PackageUpgradeRequest,
    PackageUpgradeUnavailable,
    assess_claude_package_upgrade,
    require_package_upgrade,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _installed_claude_package(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    plugins = home / ".claude" / "plugins"
    tree = plugins / "cache" / "claude-plugins-official" / "superpowers" / "5.0.7"
    _write_json(
        tree / ".claude-plugin" / "plugin.json",
        {"name": "superpowers", "version": "5.0.7"},
    )
    (tree / "SKILL.md").write_text("safe package evidence", encoding="utf-8")
    marketplace = plugins / "marketplaces" / "claude-plugins-official"
    _write_json(
        marketplace / ".claude-plugin" / "marketplace.json",
        {"plugins": [{"name": "superpowers"}]},
    )
    _write_json(
        plugins / "known_marketplaces.json",
        {"claude-plugins-official": {"installLocation": str(marketplace.resolve())}},
    )
    _write_json(
        plugins / "installed_plugins.json",
        {
            "version": 2,
            "plugins": {
                "superpowers@claude-plugins-official": [
                    {
                        "scope": "user",
                        "installPath": str(tree.resolve()),
                        "version": "5.0.7",
                    }
                ]
            },
        },
    )
    return home, tree


def _latest_only_help(arguments: tuple[str, ...]) -> HelpCommandResult:
    text = {
        ("plugin", "--help"): "Commands: install update uninstall",
        (
            "plugin",
            "update",
            "--help",
        ): "Usage: claude plugin update [options] <plugin>\nUpdate to latest",
        (
            "plugin",
            "install",
            "--help",
        ): "Usage: claude plugin install [options] <plugin>",
        (
            "plugin",
            "uninstall",
            "--help",
        ): "Usage: claude plugin uninstall [options] <plugin>",
    }[tuple(arguments)]
    return HelpCommandResult(exit_code=0, stdout=text.encode())


def test_claude_latest_only_cli_is_explicitly_unavailable_and_read_only(
    tmp_path: Path,
) -> None:
    home, tree = _installed_claude_package(tmp_path)
    coordinate = parse_profile_package("superpowers@claude-plugins-official")
    before = {
        path.relative_to(home).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in home.rglob("*")
        if path.is_file()
    }
    calls: list[tuple[str, ...]] = []

    def runner(arguments):
        calls.append(tuple(arguments))
        return _latest_only_help(tuple(arguments))

    capability = assess_claude_package_upgrade(
        coordinate,
        home=home,
        help_runner=runner,
    )

    assert capability.status == "unavailable"
    assert capability.exact_version_supported is False
    assert capability.verifiable_rollback_supported is False
    assert capability.execution_supported is False
    assert capability.pre_state == PackageStateIdentity(
        package_id=coordinate.package_id,
        version="5.0.7",
        tree_sha256=capability.pre_state.tree_sha256,
        scope="user",
    )
    assert capability.reason_codes == (
        "apu-provider-upgrade-executor-disabled",
        "provider-exact-version-selection-unsupported",
        "provider-verifiable-rollback-unsupported",
    )
    assert calls == [
        ("plugin", "--help"),
        ("plugin", "update", "--help"),
        ("plugin", "install", "--help"),
        ("plugin", "uninstall", "--help"),
    ]
    assert {
        path.relative_to(home).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in home.rglob("*")
        if path.is_file()
    } == before
    assert tree.is_dir()

    serialized = json.dumps(capability.to_dict())
    assert "Update to latest" not in serialized
    assert {item["stdout_sha256"] for item in capability.to_dict()["help_evidence"]}
    assert capability.to_dict()["protocol"] == {
        "journal_before_mutation": True,
        "authoritative_pre_state": True,
        "official_provider_operation_only": True,
        "exact_target_version_required": True,
        "target_tree_verification_required": True,
        "receipt_required": True,
        "official_rollback_required": True,
        "rollback_verification_required": True,
        "idempotency_required": True,
        "journal_states": [
            "prepared",
            "provider-update-returned",
            "verified",
            "rollback-requested",
            "rolled-back",
            "rollback-failed",
        ],
    }


def test_unavailable_upgrade_refuses_before_state_or_provider_mutation(
    tmp_path: Path,
) -> None:
    home, _ = _installed_claude_package(tmp_path)
    coordinate = parse_profile_package("superpowers@claude-plugins-official")
    capability = assess_claude_package_upgrade(
        coordinate,
        home=home,
        help_runner=lambda arguments: _latest_only_help(tuple(arguments)),
    )
    request = PackageUpgradeRequest(
        package_id=coordinate.package_id,
        operation_id="upgrade-superpowers",
        attempt=1,
        pre_state=capability.pre_state,
        target_version="6.0.0",
        target_tree_sha256="a" * 64,
    )
    state = tmp_path / "state"
    before = {
        path.relative_to(home).as_posix(): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }

    with pytest.raises(
        PackageUpgradeUnavailable,
        match="exact-version-selection-unsupported",
    ):
        require_package_upgrade(request, capability, state_home=state)

    assert not state.exists()
    assert {
        path.relative_to(home).as_posix(): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    } == before
    assert request.idempotency_key == {
        "operation_id": "upgrade-superpowers",
        "attempt": 1,
    }
    assert request.transaction_id == request.to_dict()["transaction_id"]
    assert len(request.transaction_id) == 64


def test_non_authoritative_pre_state_keeps_capability_unavailable(
    tmp_path: Path,
) -> None:
    home, tree = _installed_claude_package(tmp_path)
    installed = home / ".claude" / "plugins" / "installed_plugins.json"
    value = json.loads(installed.read_text(encoding="utf-8"))
    value["plugins"]["superpowers@claude-plugins-official"].append(
        {
            "scope": "project",
            "installPath": str(tree),
            "version": "5.0.7",
        }
    )
    installed.write_text(json.dumps(value), encoding="utf-8")

    capability = assess_claude_package_upgrade(
        parse_profile_package("superpowers@claude-plugins-official"),
        home=home,
        help_runner=lambda arguments: _latest_only_help(tuple(arguments)),
    )

    assert capability.status == "unavailable"
    assert capability.pre_state is None
    assert "installed-observation-not-authoritative" in capability.reason_codes


def test_help_evidence_is_hash_only() -> None:
    content = b"Usage: claude plugin update <plugin>"
    result = HelpCommandResult(exit_code=0, stdout=content)

    assert sha256_bytes(result.stdout) == sha256_bytes(content)
    with pytest.raises(ValueError, match="positive integer"):
        PackageUpgradeRequest(
            package_id="claude:superpowers@official",
            operation_id="upgrade",
            attempt=0,
            pre_state=PackageStateIdentity(
                package_id="claude:superpowers@official",
                version="5.0.7",
                tree_sha256="a" * 64,
                scope="user",
            ),
            target_version="6.0.0",
            target_tree_sha256="b" * 64,
        )

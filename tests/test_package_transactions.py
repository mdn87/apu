from __future__ import annotations

import json
from dataclasses import replace

import pytest

from apu.package_coordinates import parse_profile_package
from apu.package_transactions import (
    PackageUpgradeRecoveryRequired,
    execute_package_upgrade,
    recover_package_upgrade,
)
from apu.package_upgrade import (
    PackageStateIdentity,
    PackageUpgradeCapability,
    PackageUpgradeRequest,
    PackageUpgradeUnavailable,
)


class Crash(BaseException):
    pass


class Provider:
    provider = "claude"

    def __init__(self, home, before, target):
        self.home, self.state, self.before, self.target = home, before, before, target
        self.updates = self.rollbacks = 0
        self.mode = "success"
        self.fail_rollback = False

    def observe(self, coordinate):
        return self.state

    def update_exact(self, coordinate, request):
        journal = next(self.home.glob("transactions/package-upgrades/*/*.json"))
        assert json.loads(journal.read_text())["status"] == "prepared"
        self.updates += 1
        self.state = self.target
        if self.mode == "crash":
            raise Crash()
        if self.mode == "wrong":
            self.state = replace(self.target, tree_sha256="c" * 64)
        if self.mode == "error":
            raise RuntimeError("private provider output")
        return {"private output": "must never be stored"}

    def rollback_exact(self, coordinate, before):
        self.rollbacks += 1
        if self.fail_rollback:
            raise RuntimeError("private rollback output")
        self.state = before
        return {"private output": "must never be stored"}


@pytest.fixture
def context(tmp_path):
    coordinate = parse_profile_package("superpowers@official")
    before = PackageStateIdentity(coordinate.package_id, "5.0.7", "a" * 64, "user")
    target = replace(before, version="6.0.0", tree_sha256="b" * 64)
    request = PackageUpgradeRequest(
        coordinate.package_id, "upgrade", 1, before, target.version, target.tree_sha256
    )
    capability = PackageUpgradeCapability(
        "claude", coordinate.package_id, "available", True, True, True, before, (), ()
    )
    home = tmp_path / "state"
    provider = Provider(home, before, target)
    return home, coordinate, request, capability, provider


def run(context, **kwargs):
    home, coordinate, request, capability, provider = context
    return execute_package_upgrade(
        home, coordinate, request, capability, executor=provider, **kwargs
    )


def test_success_is_journaled_verified_private_and_idempotent(context):
    home, coordinate, request, capability, provider = context
    receipt = run(context)
    assert receipt["status"] == "verified"
    assert provider.state == provider.target
    assert run(context) == receipt
    assert provider.updates == 1
    for path in home.rglob("*.json"):
        assert "private output" not in path.read_text()
        assert "must never" not in path.read_text()
    with pytest.raises(ValueError, match="request"):
        execute_package_upgrade(
            home,
            coordinate,
            replace(request, target_version="7.0.0"),
            capability,
            executor=provider,
        )
    provider.state = replace(provider.target, tree_sha256="d" * 64)
    with pytest.raises(PackageUpgradeRecoveryRequired):
        run(context)
    assert provider.updates == 1


@pytest.mark.parametrize("mode", ["wrong", "error"])
def test_failure_rolls_back_and_verifies_before_reporting(context, mode):
    provider = context[-1]
    provider.mode = mode
    receipt = run(context)
    assert receipt["status"] == "rolled-back"
    assert provider.state == provider.before
    assert provider.rollbacks == 1
    assert run(context) == receipt
    assert provider.updates == 1


def test_crash_after_update_recovers_without_repeating_operation(context):
    provider = context[-1]
    provider.mode = "crash"
    with pytest.raises(Crash):
        run(context)
    provider.mode = "success"
    receipt = run(context)
    assert receipt["status"] == "verified"
    assert provider.updates == 1


def test_failed_rollback_requires_explicit_recovery_and_state_binding(context):
    home, coordinate, request, capability, provider = context
    provider.mode = "wrong"
    provider.fail_rollback = True
    receipt = run(context)
    assert receipt["status"] == "rollback-failed"
    with pytest.raises(PackageUpgradeRecoveryRequired):
        run(context)
    with pytest.raises(PackageUpgradeRecoveryRequired):
        recover_package_upgrade(
            home,
            coordinate,
            request,
            capability,
            executor=provider,
            expected_current=provider.before,
        )
    provider.fail_rollback = False
    recovered = recover_package_upgrade(
        home,
        coordinate,
        request,
        capability,
        executor=provider,
        expected_current=provider.state,
    )
    assert recovered["status"] == "rolled-back"
    assert provider.state == provider.before


def test_unavailable_or_drifted_pre_state_cannot_update(context):
    home, coordinate, request, capability, provider = context
    unavailable = replace(
        capability,
        status="unavailable",
        execution_supported=False,
        reason_codes=("provider-pin-unsupported",),
    )
    with pytest.raises(PackageUpgradeUnavailable):
        execute_package_upgrade(
            home, coordinate, request, unavailable, executor=provider
        )
    assert not home.exists()
    provider.state = replace(provider.before, tree_sha256="d" * 64)
    with pytest.raises(PackageUpgradeRecoveryRequired):
        run(context)
    assert provider.updates == 0


def test_other_attempt_is_blocked_by_unfinished_transaction(context):
    home, coordinate, request, capability, provider = context
    provider.mode = "wrong"
    provider.fail_rollback = True
    run(context)
    with pytest.raises(PackageUpgradeRecoveryRequired):
        execute_package_upgrade(
            home, coordinate, replace(request, attempt=2), capability, executor=provider
        )
    assert provider.updates == 1


def test_interruption_before_mutation_does_not_repeat_update(context):
    provider = context[-1]

    def interrupted(*args):
        raise Crash()

    provider.update_exact = interrupted
    with pytest.raises(Crash):
        run(context)
    assert run(context)["status"] == "rolled-back"
    assert provider.updates == provider.rollbacks == 0


def test_lock_and_corrupt_journal_prevent_mutation(context):
    from apu.package_state import PackageLock, PackageStateError

    home, coordinate, _request, _capability, provider = context
    with PackageLock(home, coordinate.package_id), pytest.raises(PackageStateError):
        run(context)
    assert provider.updates == 0
    run(context)
    path = next(home.glob("transactions/package-upgrades/*/*.json"))
    path.write_text("{}")
    with pytest.raises(PackageUpgradeRecoveryRequired, match="invalid"):
        run(context)
    assert provider.updates == 1


def test_rollback_exit_success_is_not_enough(context):
    provider = context[-1]
    provider.mode = "wrong"
    provider.rollback_exact = lambda *args: {}
    assert run(context)["status"] == "rollback-failed"

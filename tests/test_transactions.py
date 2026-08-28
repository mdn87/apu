from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

import apu.apply as apply_module
from apu.apply import ApplyError, _replace_with_retry, apply_plan
from apu.filesystem import hash_object
from apu.models import Approval, Plan, PlanOperation, sha256_bytes
from apu.receipts import load_receipt
from apu.rollback import rollback_receipt
from apu.state import load_registry


def operation(
    operation_id: str,
    *,
    action: str,
    target: Path,
    source: Path | None = None,
    precondition: str | None = None,
    proposed: str | None = None,
    approval: str = "approved",
    strategy: str = "full_file",
) -> PlanOperation:
    return PlanOperation(
        id=operation_id,
        action=action,
        target=str(target),
        source=str(source) if source else None,
        ownership="apu",
        strategy=strategy,
        precondition_sha256=precondition,
        proposed_sha256=proposed,
        backup_required=action != "create",
        requires_confirmation=False,
        approval=Approval(status=approval),
        reason="fixture",
        evidence=(),
    )


def plan(*operations: PlanOperation, status: str = "approved") -> Plan:
    return Plan(
        schema_version=1,
        apu_version="0.1.0",
        created_at="2026-08-06T10:00:00Z",
        inventory_sha256="a" * 64,
        status=status,
        operations=operations,
    )


def test_apply_executes_only_approved_operations(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"approved")
    rejected_source = tmp_path / "rejected-source"
    rejected_source.write_bytes(b"rejected")
    approved_target = tmp_path / "approved-target"
    rejected_target = tmp_path / "rejected-target"
    approved = operation(
        "approved",
        action="create",
        target=approved_target,
        source=source,
        proposed=sha256_bytes(b"approved"),
    )
    rejected = operation(
        "rejected",
        action="create",
        target=rejected_target,
        source=rejected_source,
        proposed=sha256_bytes(b"rejected"),
        approval="rejected",
    )

    receipt_path = apply_plan(
        plan(approved, rejected),
        state_home=tmp_path / "state",
        installation_id="install-1",
    )

    assert approved_target.read_bytes() == b"approved"
    assert not rejected_target.exists()
    receipt = load_receipt(receipt_path)
    assert [item["operation_id"] for item in receipt["operations"]] == ["approved"]


def test_campaign_apply_stamps_snapshot_and_idempotency_keys(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"campaign")
    target = tmp_path / "target"
    change = operation(
        "campaign-op",
        action="create",
        target=target,
        source=source,
        proposed=sha256_bytes(b"campaign"),
    )

    receipt_path = apply_plan(
        plan(change),
        state_home=tmp_path / "state",
        installation_id="install-campaign",
        campaign_id="campaign-1",
        snapshot_id="a" * 64,
    )

    receipt = load_receipt(receipt_path)
    assert receipt["campaign_id"] == "campaign-1"
    assert receipt["snapshot_id"] == "a" * 64
    assert receipt["idempotency_keys"] == {
        "campaign-op": {
            "operation_id": "campaign-op",
            "attempt": 1,
        }
    }


def test_apply_rejects_draft_plan_even_when_confirmation_is_suppressed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"content")
    pending = operation(
        "pending",
        action="create",
        target=tmp_path / "target",
        source=source,
        proposed=sha256_bytes(b"content"),
        approval="pending",
    )

    with pytest.raises(ApplyError, match="approved"):
        apply_plan(
            plan(pending, status="draft"),
            state_home=tmp_path / "state",
            installation_id="install-1",
            confirmed=True,
        )


def test_preflight_failure_happens_before_any_mutation(tmp_path: Path) -> None:
    first_source = tmp_path / "first-source"
    first_source.write_bytes(b"first")
    existing = tmp_path / "existing"
    existing.write_bytes(b"changed")
    first_target = tmp_path / "first-target"
    operations = (
        operation(
            "first",
            action="create",
            target=first_target,
            source=first_source,
            proposed=sha256_bytes(b"first"),
        ),
        operation(
            "stale",
            action="remove",
            target=existing,
            precondition=sha256_bytes(b"original"),
        ),
    )

    with pytest.raises(ApplyError, match="precondition"):
        apply_plan(
            plan(*operations),
            state_home=tmp_path / "state",
            installation_id="install-1",
        )

    assert not first_target.exists()
    assert existing.read_bytes() == b"changed"


def test_apply_then_rollback_restores_bytes_and_mode(tmp_path: Path) -> None:
    target = tmp_path / "policy.md"
    target.write_bytes(b"original")
    target.chmod(0o640)
    source = tmp_path / "rendered.md"
    source.write_bytes(b"replacement")
    update = operation(
        "update",
        action="merge",
        target=target,
        source=source,
        precondition=sha256_bytes(b"original"),
        proposed=sha256_bytes(b"replacement"),
    )

    receipt_path = apply_plan(
        plan(update),
        state_home=tmp_path / "state",
        installation_id="install-1",
    )
    assert target.read_bytes() == b"replacement"
    receipt = load_receipt(receipt_path)
    backup = Path(receipt["operations"][0]["backup_path"])
    assert backup.read_bytes() == b"original"
    assert (
        load_registry(tmp_path / "state")["installations"]["install-1"]["status"]
        == "active"
    )
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o640
        assert backup.stat().st_mode & 0o777 == 0o640

    result = rollback_receipt(receipt_path)

    assert result["status"] == "rolled_back"
    assert target.read_bytes() == b"original"
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o640
    assert (
        load_registry(tmp_path / "state")["installations"]["install-1"]["status"]
        == "rolled_back"
    )


def test_relocation_is_applied_and_rolled_back_as_one_transaction(
    tmp_path: Path,
) -> None:
    source = tmp_path / "old.md"
    source.write_bytes(b"policy")
    destination = tmp_path / "new.md"
    content_hash = sha256_bytes(b"policy")
    approval = Approval(status="approved")
    remove = replace(
        operation(
            "move-remove",
            action="remove",
            target=source,
            precondition=content_hash,
            proposed=None,
        ),
        atomic_group_id="move",
        group_content_sha256=content_hash,
        approval=approval,
    )
    create = replace(
        operation(
            "move-create",
            action="create",
            target=destination,
            source=source,
            proposed=content_hash,
        ),
        atomic_group_id="move",
        group_content_sha256=content_hash,
        approval=approval,
    )

    receipt_path = apply_plan(
        plan(remove, create),
        state_home=tmp_path / "state",
        installation_id="install-1",
    )

    assert not source.exists()
    assert destination.read_bytes() == b"policy"

    result = rollback_receipt(receipt_path)
    assert result["status"] == "rolled_back"
    assert source.read_bytes() == b"policy"
    assert not destination.exists()


def test_relocation_failure_restores_source_and_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "old.md"
    source.write_bytes(b"policy")
    destination = tmp_path / "new.md"
    content_hash = sha256_bytes(b"policy")
    approval = Approval(status="approved")
    remove = replace(
        operation(
            "move-remove",
            action="remove",
            target=source,
            precondition=content_hash,
        ),
        atomic_group_id="move",
        group_content_sha256=content_hash,
        approval=approval,
    )
    create = replace(
        operation(
            "move-create",
            action="create",
            target=destination,
            source=source,
            proposed=content_hash,
        ),
        atomic_group_id="move",
        group_content_sha256=content_hash,
        approval=approval,
    )
    real_replace = os.replace

    def fail_destination(staged: str | os.PathLike, target: str | os.PathLike) -> None:
        if Path(target) == destination:
            raise OSError("simulated relocation failure")
        real_replace(staged, target)

    monkeypatch.setattr(os, "replace", fail_destination)
    with pytest.raises(ApplyError, match="move-create"):
        apply_plan(
            plan(remove, create),
            state_home=tmp_path / "state",
            installation_id="install-1",
        )

    assert source.read_bytes() == b"policy"
    assert not destination.exists()


def test_rollback_leaves_drifted_created_symlink_untouched(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    target = tmp_path / "installed-skill"
    install = operation(
        "install",
        action="symlink",
        target=target,
        source=canonical,
        proposed=None,
        strategy="sidecar",
    )

    receipt_path = apply_plan(
        plan(install),
        state_home=tmp_path / "state",
        installation_id="install-1",
    )
    target.unlink()
    target.symlink_to(alternate, target_is_directory=True)

    result = rollback_receipt(receipt_path)

    assert result["status"] == "drifted"
    assert target.resolve() == alternate.resolve()


def test_rollback_removes_unchanged_created_symlink(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    target = tmp_path / "installed-skill"

    receipt_path = apply_plan(
        plan(
            operation(
                "install",
                action="symlink",
                target=target,
                source=canonical,
                strategy="sidecar",
            )
        ),
        state_home=tmp_path / "state",
        installation_id="install-1",
    )

    assert target.is_symlink()
    assert rollback_receipt(receipt_path)["status"] == "rolled_back"
    assert not os.path.lexists(target)


def test_rollback_leaves_drifted_updated_file_untouched(tmp_path: Path) -> None:
    target = tmp_path / "policy.md"
    target.write_bytes(b"original")
    rendered = tmp_path / "rendered.md"
    rendered.write_bytes(b"installed")

    receipt_path = apply_plan(
        plan(
            operation(
                "update",
                action="merge",
                target=target,
                source=rendered,
                precondition=sha256_bytes(b"original"),
                proposed=sha256_bytes(b"installed"),
            )
        ),
        state_home=tmp_path / "state",
        installation_id="install-1",
    )
    target.write_bytes(b"user-change")

    result = rollback_receipt(receipt_path)

    assert result["status"] == "drifted"
    assert target.read_bytes() == b"user-change"


def test_source_hash_is_verified_before_mutation(tmp_path: Path) -> None:
    source = tmp_path / "rendered"
    source.write_bytes(b"changed")
    target = tmp_path / "target"

    with pytest.raises(ApplyError, match="output hash"):
        apply_plan(
            plan(
                operation(
                    "create",
                    action="create",
                    target=target,
                    source=source,
                    proposed=sha256_bytes(b"expected"),
                )
            ),
            state_home=tmp_path / "state",
            installation_id="install-1",
        )

    assert not target.exists()


def test_directory_create_and_rollback_preserve_tree(tmp_path: Path) -> None:
    source = tmp_path / "skill"
    source.mkdir()
    (source / "SKILL.md").write_bytes(b"skill")
    nested = source / "references"
    nested.mkdir()
    (nested / "guide.md").write_bytes(b"guide")
    target = tmp_path / "installed" / "skill"

    receipt_path = apply_plan(
        plan(
            operation(
                "copy-tree",
                action="create",
                target=target,
                source=source,
                proposed=hash_object(source),
            )
        ),
        state_home=tmp_path / "state",
        installation_id="install-1",
    )

    assert (target / "SKILL.md").read_bytes() == b"skill"
    assert (target / "references" / "guide.md").read_bytes() == b"guide"
    assert rollback_receipt(receipt_path)["status"] == "rolled_back"
    assert not target.exists()


def test_configure_can_create_missing_metadata_then_rollback(
    tmp_path: Path,
) -> None:
    rendered = tmp_path / "marketplace.json"
    rendered.write_bytes(b'{"apu":{"source":"directory"}}')
    target = tmp_path / "home" / ".claude" / "plugins" / "known.json"
    configure = operation(
        "configure-marketplace",
        action="configure",
        target=target,
        source=rendered,
        precondition=None,
        proposed=sha256_bytes(rendered.read_bytes()),
    )

    receipt_path = apply_plan(
        plan(configure),
        state_home=tmp_path / "state",
        installation_id="install-configure",
    )

    assert target.read_bytes() == rendered.read_bytes()
    assert rollback_receipt(receipt_path)["status"] == "rolled_back"
    assert not target.exists()


def test_mid_transaction_failure_restores_prior_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    first.write_bytes(b"first-original")
    first_rendered = tmp_path / "first-rendered"
    first_rendered.write_bytes(b"first-new")
    second = tmp_path / "second"
    second.write_bytes(b"second-original")
    second_rendered = tmp_path / "second-rendered"
    second_rendered.write_bytes(b"second-new")

    real_replace = os.replace

    def fail_second(source: str | os.PathLike, destination: str | os.PathLike) -> None:
        if Path(destination) == second:
            raise OSError("simulated commit failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_second)
    with pytest.raises(ApplyError, match="second"):
        apply_plan(
            plan(
                operation(
                    "first",
                    action="merge",
                    target=first,
                    source=first_rendered,
                    precondition=sha256_bytes(b"first-original"),
                    proposed=sha256_bytes(b"first-new"),
                ),
                operation(
                    "second",
                    action="merge",
                    target=second,
                    source=second_rendered,
                    precondition=sha256_bytes(b"second-original"),
                    proposed=sha256_bytes(b"second-new"),
                ),
            ),
            state_home=tmp_path / "state",
            installation_id="install-1",
        )

    assert first.read_bytes() == b"first-original"
    assert second.read_bytes() == b"second-original"


def test_apply_revalidates_each_target_immediately_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    first.write_bytes(b"first-original")
    first_rendered = tmp_path / "first-rendered"
    first_rendered.write_bytes(b"first-new")
    second = tmp_path / "second"
    second.write_bytes(b"second-original")
    second_rendered = tmp_path / "second-rendered"
    second_rendered.write_bytes(b"second-new")
    real_preflight = apply_module._preflight_all

    def mutate_after_preflight(*args: object, **kwargs: object):
        prepared = real_preflight(*args, **kwargs)
        second.write_bytes(b"external-change")
        return prepared

    monkeypatch.setattr(apply_module, "_preflight_all", mutate_after_preflight)

    with pytest.raises(ApplyError, match="changed after preflight"):
        apply_plan(
            plan(
                operation(
                    "first",
                    action="merge",
                    target=first,
                    source=first_rendered,
                    precondition=sha256_bytes(b"first-original"),
                    proposed=sha256_bytes(b"first-new"),
                ),
                operation(
                    "second",
                    action="merge",
                    target=second,
                    source=second_rendered,
                    precondition=sha256_bytes(b"second-original"),
                    proposed=sha256_bytes(b"second-new"),
                ),
            ),
            state_home=tmp_path / "state",
            installation_id="install-revalidate",
        )

    assert first.read_bytes() == b"first-original"
    assert second.read_bytes() == b"external-change"


def test_keyboard_interrupt_after_commit_fully_unwinds_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"original")
    rendered = tmp_path / "rendered"
    rendered.write_bytes(b"installed")
    state_home = tmp_path / "state"
    real_commit = apply_module._commit

    def interrupt_after_commit(item: apply_module._PreparedOperation) -> None:
        real_commit(item)
        raise KeyboardInterrupt

    monkeypatch.setattr(apply_module, "_commit", interrupt_after_commit)

    with pytest.raises(KeyboardInterrupt):
        apply_plan(
            plan(
                operation(
                    "update",
                    action="merge",
                    target=target,
                    source=rendered,
                    precondition=sha256_bytes(b"original"),
                    proposed=sha256_bytes(b"installed"),
                )
            ),
            state_home=state_home,
            installation_id="install-interrupted",
        )

    assert target.read_bytes() == b"original"
    assert "install-interrupted" not in load_registry(state_home)["installations"]
    assert not (state_home / "installations" / "install-interrupted").exists()


def test_directory_replacement_failure_restores_current_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "installed"
    target.mkdir()
    (target / "old.txt").write_bytes(b"old")
    rendered = tmp_path / "rendered"
    rendered.mkdir()
    (rendered / "new.txt").write_bytes(b"new")

    from hashlib import sha256

    def tree_hash(root: Path) -> str:
        digest = sha256()
        for child in sorted(item for item in root.rglob("*") if item.is_file()):
            digest.update(child.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(child.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    real_replace = os.replace

    def fail_target(staged: str | os.PathLike, destination: str | os.PathLike) -> None:
        if Path(destination) == target:
            raise OSError("simulated directory replacement failure")
        real_replace(staged, destination)

    monkeypatch.setattr(os, "replace", fail_target)
    with pytest.raises(ApplyError, match="directory-update"):
        apply_plan(
            plan(
                operation(
                    "directory-update",
                    action="merge",
                    target=target,
                    source=rendered,
                    precondition=tree_hash(target),
                    proposed=tree_hash(rendered),
                )
            ),
            state_home=tmp_path / "state",
            installation_id="install-1",
        )

    assert (target / "old.txt").read_bytes() == b"old"
    assert not (target / "new.txt").exists()


def test_symlink_capability_failure_is_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "canonical"
    source.mkdir()
    target = tmp_path / "skill"
    install = operation(
        "install",
        action="symlink",
        target=target,
        source=source,
        strategy="sidecar",
    )

    def unsupported(*args: object, **kwargs: object) -> None:
        raise OSError("privilege unavailable")

    monkeypatch.setattr(os, "symlink", unsupported)
    with pytest.raises(ApplyError, match="unsupported"):
        apply_plan(
            plan(install),
            state_home=tmp_path / "state",
            installation_id="install-1",
        )

    assert not os.path.lexists(target)


def test_null_create_precondition_rejects_broken_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"new")
    target = tmp_path / "target"
    target.symlink_to(tmp_path / "missing")

    with pytest.raises(ApplyError, match="missing target"):
        apply_plan(
            plan(
                operation(
                    "create",
                    action="create",
                    target=target,
                    source=source,
                    proposed=sha256_bytes(b"new"),
                )
            ),
            state_home=tmp_path / "state",
            installation_id="install-1",
        )

    assert target.is_symlink()


def test_windows_replace_retries_a_bounded_sharing_violation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_text("new", encoding="utf-8")
    attempts = 0

    def flaky_replace(left: Path, right: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(13, "sharing violation", os.fspath(right))
        os.rename(left, right)

    delays: list[float] = []
    _replace_with_retry(
        source,
        target,
        windows=True,
        replace=flaky_replace,
        sleep=delays.append,
    )

    assert attempts == 3
    assert delays == [0.05, 0.1]
    assert target.read_text(encoding="utf-8") == "new"


def test_non_windows_replace_failure_is_not_retried(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_text("new", encoding="utf-8")
    attempts = 0

    def failing_replace(left: Path, right: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise PermissionError(13, "denied", os.fspath(right))

    with pytest.raises(PermissionError):
        _replace_with_retry(
            source,
            target,
            windows=False,
            replace=failing_replace,
            sleep=lambda _: None,
        )

    assert attempts == 1


def test_symlink_commit_never_deletes_collateral_temporary_name(
    tmp_path: Path,
) -> None:
    source = tmp_path / "canonical"
    source.mkdir()
    target = tmp_path / "installed"
    collateral = tmp_path / ".installed.apu-link"
    collateral.write_text("user-owned", encoding="utf-8")

    apply_plan(
        plan(
            operation(
                "install",
                action="symlink",
                target=target,
                source=source,
                proposed=hash_object(source),
                strategy="sidecar",
            )
        ),
        state_home=tmp_path / "state",
        installation_id="install-1",
    )

    assert collateral.read_text(encoding="utf-8") == "user-owned"


def test_apply_rejects_protected_or_standalone_directory_removal(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "AGENTS.md").write_text("policy", encoding="utf-8")
    remove = operation(
        "remove-root",
        action="remove",
        target=repository,
        precondition=hash_object(repository),
    )
    protected_plan = replace(
        plan(remove),
        validation={"protected_roots": [str(repository)]},
    )

    with pytest.raises(ApplyError, match="protected root"):
        apply_plan(
            protected_plan,
            state_home=tmp_path / "state",
            installation_id="protected",
        )
    assert (repository / "AGENTS.md").is_file()

    with pytest.raises(ApplyError, match="recursively remove"):
        apply_plan(
            plan(remove),
            state_home=tmp_path / "state-2",
            installation_id="standalone",
        )
    assert (repository / "AGENTS.md").is_file()


def test_apply_rejects_reused_installation_id_without_orphaning_first_target(
    tmp_path: Path,
) -> None:
    first_source = tmp_path / "first-source"
    first_source.write_text("first", encoding="utf-8")
    first_target = tmp_path / "first-target"
    state = tmp_path / "state"
    apply_plan(
        plan(
            operation(
                "first",
                action="create",
                target=first_target,
                source=first_source,
                proposed=sha256_bytes(b"first"),
            )
        ),
        state_home=state,
        installation_id="same-id",
    )
    second_source = tmp_path / "second-source"
    second_source.write_text("second", encoding="utf-8")
    second_target = tmp_path / "second-target"

    with pytest.raises(ApplyError, match="already exists"):
        apply_plan(
            plan(
                operation(
                    "second",
                    action="create",
                    target=second_target,
                    source=second_source,
                    proposed=sha256_bytes(b"second"),
                )
            ),
            state_home=state,
            installation_id="same-id",
        )

    assert first_target.read_text(encoding="utf-8") == "first"
    assert not second_target.exists()
    receipt = load_receipt(state / "installations" / "same-id" / "receipt.json")
    assert receipt["applied_operation_ids"] == ["first"]


def test_rollback_rejects_copied_receipt_before_mutation(tmp_path: Path) -> None:
    target = tmp_path / "policy.md"
    target.write_text("original", encoding="utf-8")
    rendered = tmp_path / "rendered.md"
    rendered.write_text("installed", encoding="utf-8")
    receipt = apply_plan(
        plan(
            operation(
                "update",
                action="merge",
                target=target,
                source=rendered,
                precondition=sha256_bytes(b"original"),
                proposed=sha256_bytes(b"installed"),
            )
        ),
        state_home=tmp_path / "state",
        installation_id="install-1",
    )
    copied = tmp_path / "copied-receipt.json"
    copied.write_bytes(receipt.read_bytes())

    from apu.rollback import RollbackError

    with pytest.raises(RollbackError, match="preflight"):
        rollback_receipt(copied)
    assert target.read_text(encoding="utf-8") == "installed"


def test_managed_json_configuration_preserves_unrelated_entries(
    tmp_path: Path,
) -> None:
    target = tmp_path / "known_marketplaces.json"
    target.write_text(
        '{"team":{"source":"existing"},"apu":{"enabled":false}}',
        encoding="utf-8",
    )
    rendered = tmp_path / "apu.json"
    rendered.write_text(
        '{"apu":{"source":"canonical","enabled":true}}',
        encoding="utf-8",
    )
    from apu.render import render_bytes

    expected = render_bytes(
        action="configure",
        strategy="managed_section",
        source=rendered.read_bytes(),
        current=target.read_bytes(),
        target=target,
    )
    apply_plan(
        plan(
            operation(
                "configure",
                action="configure",
                target=target,
                source=rendered,
                precondition=sha256_bytes(target.read_bytes()),
                proposed=sha256_bytes(expected),
                strategy="managed_section",
            )
        ),
        state_home=tmp_path / "state",
        installation_id="configure",
    )

    import json

    stored = json.loads(target.read_text(encoding="utf-8"))
    assert stored["team"] == {"source": "existing"}
    assert stored["apu"] == {"enabled": True, "source": "canonical"}

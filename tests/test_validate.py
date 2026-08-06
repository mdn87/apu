from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from apu.models import Approval, Plan, PlanOperation, sha256_bytes
from apu.receipts import write_receipt
from apu.runners import CODEX_CAPABILITIES
from apu.state import update_registry
from apu.validate import (
    RunnerInvocationAdapter,
    load_behavioral_fixture,
    run_behavioral_fixture,
    validate_plan_path,
    validate_receipt_path,
    validate_registered_installations,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "behavioral"


def _plan(path: Path) -> Path:
    operation = PlanOperation(
        id="create-policy",
        action="create",
        target=str(path.parent / "AGENTS.md"),
        source=str(path.parent / "policy.md"),
        ownership="apu",
        strategy="full_file",
        precondition_sha256=None,
        proposed_sha256="b" * 64,
        backup_required=False,
        requires_confirmation=False,
        approval=Approval(status="approved"),
        reason="fixture",
        evidence=(),
    )
    plan = Plan(
        schema_version=1,
        apu_version="0.1.0",
        created_at="2026-08-06T12:00:00Z",
        inventory_sha256="a" * 64,
        status="approved",
        operations=(operation,),
    )
    path.write_text(json.dumps(plan.to_dict()), encoding="utf-8")
    return path


def _receipt(state_home: Path, installation_id: str, target: Path) -> Path:
    content = target.read_bytes()
    return write_receipt(
        state_home,
        {
            "schema_version": 1,
            "installation_id": installation_id,
            "created_at": "2026-08-06T12:00:00Z",
            "operations": [
                {
                    "id": "op-1",
                    "target": str(target),
                    "original_sha256": None,
                    "installed_sha256": sha256_bytes(content),
                    "backup_path": None,
                }
            ],
            "rollback_status": "available",
        },
    )


def test_structural_validation_accepts_valid_plan_and_detects_drifted_receipt(
    tmp_path: Path,
) -> None:
    plan_result = validate_plan_path(_plan(tmp_path / "plan.json"))
    assert plan_result.status == "passed"
    assert plan_result.checks[0].name == "plan:schema"

    state_home = tmp_path / "state"
    target = tmp_path / "installed.md"
    target.write_text("installed", encoding="utf-8")
    receipt_path = _receipt(state_home, "install-1", target)

    assert validate_receipt_path(receipt_path).status == "passed"

    target.write_text("user edit", encoding="utf-8")
    drifted = validate_receipt_path(receipt_path)
    assert drifted.status == "failed"
    assert any("does not match receipt" in check.reason for check in drifted.checks)


def test_registry_validation_checks_only_active_installations(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    target = tmp_path / "installed.md"
    target.write_text("installed", encoding="utf-8")
    active_receipt = _receipt(state_home, "active-install", target)
    update_registry(
        state_home,
        "active-install",
        {
            "status": "active",
            "receipt": str(active_receipt.relative_to(state_home)),
        },
    )
    update_registry(
        state_home,
        "disabled-install",
        {
            "status": "disabled",
            "receipt": "installations/disabled-install/missing.json",
        },
    )

    result = validate_registered_installations(state_home)

    assert result.status == "passed"
    assert [check.name for check in result.checks] == [
        "installation:active-install"
    ]


def test_receipt_validation_checks_recorded_symlink_target(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    installed = tmp_path / "installed-skill"
    try:
        installed.symlink_to(canonical, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    receipt_path = write_receipt(
        tmp_path / "state",
        {
            "schema_version": 1,
            "installation_id": "skill-install",
            "created_at": "2026-08-06T12:00:00Z",
            "operations": [
                {
                    "id": "skill-link",
                    "target": str(installed),
                    "original_sha256": None,
                    "installed_sha256": sha256_bytes(str(canonical).encode()),
                    "backup_path": None,
                    "created_symlink_target": str(canonical),
                }
            ],
            "rollback_status": "available",
        },
    )

    assert validate_receipt_path(receipt_path).status == "passed"

    installed.unlink()
    installed.symlink_to(alternate, target_is_directory=True)
    assert validate_receipt_path(receipt_path).status == "failed"


def test_receipt_validation_checks_copied_directory_tree(tmp_path: Path) -> None:
    target = tmp_path / "installed-skill"
    target.mkdir()
    (target / "SKILL.md").write_text("canonical", encoding="utf-8")
    nested = target / "references"
    nested.mkdir()
    (nested / "guide.md").write_text("guide", encoding="utf-8")

    from apu.apply import _hash_object

    receipt_path = write_receipt(
        tmp_path / "state",
        {
            "schema_version": 1,
            "installation_id": "copied-skill",
            "created_at": "2026-08-06T12:00:00Z",
            "operations": [
                {
                    "id": "skill-copy",
                    "target": str(target),
                    "original_sha256": None,
                    "installed_sha256": _hash_object(target),
                    "backup_path": None,
                }
            ],
            "rollback_status": "available",
        },
    )

    assert validate_receipt_path(receipt_path).status == "passed"

    (nested / "guide.md").write_text("drift", encoding="utf-8")
    assert validate_receipt_path(receipt_path).status == "failed"


def test_empty_registry_is_a_clear_success_without_creating_state(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"

    result = validate_registered_installations(state_home)

    assert result.status == "passed"
    assert result.reason == "no active installations registered"
    assert not state_home.exists()


def test_behavioral_fixture_loader_covers_balanced_cases() -> None:
    expected = {
        "direct-config-edit",
        "planned-coupled-change",
        "delegated-independent-analysis",
        "high-risk-auth-migration",
        "seeded-boundary-defect",
        "explicit-named-skill",
    }

    loaded = {
        load_behavioral_fixture(path).name
        for path in FIXTURES.iterdir()
        if path.is_dir()
    }

    assert loaded == expected
    for name in expected:
        fixture = load_behavioral_fixture(FIXTURES / name)
        assert fixture.prompt
        assert fixture.repo.is_dir()
        assert fixture.checks.is_dir()
        assert fixture.validation_commands


def test_runtime_execution_is_opt_in_and_never_calls_executor() -> None:
    called = False

    def executor(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("executor must not run")

    result = run_behavioral_fixture(
        FIXTURES / "direct-config-edit",
        "codex",
        runtime_enabled=False,
        executor=executor,
    )

    assert result.status == "unavailable"
    assert "explicitly enabled" in (result.reason or "")
    assert called is False


def test_fixture_runs_in_isolated_copy_and_sanitizes_runner_output(
    tmp_path: Path,
) -> None:
    observed_worktree: Path | None = None

    def executor(command, *, cwd, input_text, timeout):
        nonlocal observed_worktree
        observed_worktree = cwd
        assert tuple(command) == (
            sys.executable,
            *CODEX_CAPABILITIES.invocation[1:],
        )
        assert "configuration" in input_text
        assert timeout == 30
        (cwd / "settings.toml").write_text("mode = \"safe\"\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"type":"item.completed","item":{"type":"command_execution",'
            '"status":"completed","output":"secret-value"}}\n',
            stderr="private authentication material",
        )

    adapter = RunnerInvocationAdapter.codex(executable=sys.executable)
    result = run_behavioral_fixture(
        FIXTURES / "direct-config-edit",
        "codex",
        runtime_enabled=True,
        executor=executor,
        adapters={"codex": adapter},
    )

    assert result.status == "passed"
    assert result.runner is not None
    assert result.runner["cli_name"] == "codex"
    assert result.runner["version"]
    assert result.runner["authenticated"] is True
    assert result.runner["observable_events"] == [
        "delegation",
        "review",
        "tool_use",
    ]
    assert observed_worktree is not None
    assert not observed_worktree.exists()
    serialized = json.dumps(result.to_dict())
    assert "secret-value" not in serialized
    assert "private authentication" not in serialized
    assert not (FIXTURES / "direct-config-edit" / "repo" / "settings.toml").exists()


def test_unobservable_event_check_is_skipped_after_commands_pass() -> None:
    def executor(command, *, cwd, input_text, timeout):
        (cwd / "settings.toml").write_text("mode = \"safe\"\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    adapter = RunnerInvocationAdapter.codex(
        executable=sys.executable,
        observable_events={"tool_use"},
    )
    result = run_behavioral_fixture(
        FIXTURES / "direct-config-edit",
        "codex",
        runtime_enabled=True,
        executor=executor,
        adapters={"codex": adapter},
        required_events=("delegation",),
    )

    assert result.status == "skipped"
    assert any(
        check.name == "required:delegation" and check.status == "skipped"
        for check in result.checks
    )


def test_unsupported_runner_is_skipped_and_timeout_is_failed() -> None:
    unsupported = run_behavioral_fixture(
        FIXTURES / "direct-config-edit",
        "claude",
        runtime_enabled=True,
    )
    assert unsupported.status == "skipped"

    def timeout_executor(command, *, cwd, input_text, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    adapter = RunnerInvocationAdapter.codex(executable=sys.executable)
    timed_out = run_behavioral_fixture(
        FIXTURES / "direct-config-edit",
        "codex",
        runtime_enabled=True,
        executor=timeout_executor,
        adapters={"codex": adapter},
    )
    assert timed_out.status == "failed"
    assert timed_out.checks[0].name == "runner:exit"
    assert "timed out" in timed_out.checks[0].reason

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence

from .models import Plan, sha256_bytes
from .filesystem import hash_object, symlink_points_to
from .receipts import load_receipt
from .runners import (
    BehavioralResult,
    CheckResult,
    CLAUDE_CAPABILITIES,
    CODEX_CAPABILITIES,
    NormalizedEvent,
    RunnerCapabilities,
    RunnerParseError,
    evaluate_event_checks,
    parse_claude_jsonl,
    parse_codex_jsonl,
    unavailable_result,
    unsupported_result,
)
from .state import load_registry, resolve_state_home


_VALID_STATUSES = frozenset({"passed", "failed", "skipped", "unavailable"})


@dataclass(frozen=True)
class ValidationResult:
    kind: str
    target: str | None
    status: str
    checks: tuple[CheckResult, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(f"unsupported validation status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BehavioralFixture:
    root: Path
    name: str
    prompt: str
    repo: Path
    checks: Path
    supported_runners: tuple[str, ...]
    expected_tier: str
    required_events: tuple[str, ...]
    forbidden_events: tuple[str, ...]
    expected_outputs: tuple[Mapping[str, str], ...]
    validation_commands: tuple[tuple[str, ...], ...]
    timeout_seconds: int
    cleanup: str
    seeded_defect: Mapping[str, Any] | None


RunnerParser = Callable[[str | Iterable[str]], tuple[NormalizedEvent, ...]]
ProcessExecutor = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class RunnerInvocationAdapter:
    """A selected CLI invocation plus only the events it can prove observable."""

    name: str
    capabilities: RunnerCapabilities
    parser: RunnerParser
    executable: str | None = None

    @classmethod
    def codex(
        cls,
        *,
        executable: str | None = None,
        observable_events: Iterable[str] | None = None,
    ) -> RunnerInvocationAdapter:
        capabilities = CODEX_CAPABILITIES
        if observable_events is not None:
            capabilities = capabilities.with_observable_events(observable_events)
        return cls("codex", capabilities, parse_codex_jsonl, executable)

    @classmethod
    def claude(
        cls,
        *,
        executable: str | None = None,
        observable_events: Iterable[str] | None = None,
    ) -> RunnerInvocationAdapter:
        capabilities = CLAUDE_CAPABILITIES
        if observable_events is not None:
            capabilities = capabilities.with_observable_events(observable_events)
        return cls("claude", capabilities, parse_claude_jsonl, executable)

    def resolved_executable(self) -> str | None:
        return self.executable or shutil.which(self.capabilities.cli_name)


RUNNER_ADAPTERS: Mapping[str, RunnerInvocationAdapter] = {
    "codex": RunnerInvocationAdapter.codex(),
    "claude": RunnerInvocationAdapter.claude(),
}


def _execute_runner(
    command: Sequence[str],
    *,
    cwd: Path,
    input_text: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def validate_plan_path(path: Path) -> ValidationResult:
    """Validate a serialized plan without running any model or mutation."""

    plan_path = Path(path)
    try:
        value = json.loads(plan_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("plan must be a JSON object")
        plan = Plan.from_dict(value)
        plan.validate()
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        return _failed_result(
            "plan",
            plan_path,
            "plan:schema",
            _safe_error("invalid plan", error),
        )
    return ValidationResult(
        kind="plan",
        target=str(plan_path),
        status="passed",
        checks=(
            CheckResult(
                name="plan:schema",
                status="passed",
                reason="plan structure and approval state are valid",
            ),
        ),
    )


def validate_receipt_path(path: Path) -> ValidationResult:
    """Validate a receipt and any installed targets it identifies."""

    receipt_path = Path(path)
    try:
        receipt = load_receipt(receipt_path)
    except (OSError, TypeError, ValueError) as error:
        return _failed_result(
            "receipt",
            receipt_path,
            "receipt:schema",
            _safe_error("invalid receipt", error),
        )

    checks = [
        CheckResult(
            name="receipt:schema",
            status="passed",
            reason="receipt structure is valid",
        )
    ]
    for index, operation in enumerate(receipt["operations"]):
        target = operation.get("target")
        if not isinstance(target, str) or not target:
            continue
        operation_id = str(
            operation.get("operation_id", operation.get("id", index))
        )
        checks.append(
            _validate_installed_target(
                Path(target),
                operation.get("installed_sha256"),
                operation_id,
                operation,
            )
        )
    return ValidationResult(
        kind="receipt",
        target=str(receipt_path),
        status=_aggregate_status(checks),
        checks=tuple(checks),
    )


def validate_registered_installations(state_home: Path) -> ValidationResult:
    """Validate every active registry entry, succeeding clearly when empty."""

    root = Path(state_home)
    try:
        registry = load_registry(root)
    except (OSError, TypeError, ValueError) as error:
        return _failed_result(
            "registry",
            root / "registry.json",
            "registry:schema",
            _safe_error("invalid registry", error),
        )

    active = [
        (installation_id, entry)
        for installation_id, entry in registry["installations"].items()
        if entry.get("status") == "active"
    ]
    if not active:
        return ValidationResult(
            kind="registry",
            target=str(root / "registry.json"),
            status="passed",
            reason="no active installations registered",
        )

    checks: list[CheckResult] = []
    for installation_id, entry in sorted(active):
        try:
            receipt = _registry_receipt_path(root, installation_id, entry)
            result = validate_receipt_path(receipt)
            checks.append(
                CheckResult(
                    name=f"installation:{installation_id}",
                    status=result.status,
                    reason=(
                        "receipt and installed targets are valid"
                        if result.status == "passed"
                        else _first_failure_reason(result)
                    ),
                )
            )
        except (OSError, TypeError, ValueError) as error:
            checks.append(
                CheckResult(
                    name=f"installation:{installation_id}",
                    status="failed",
                    reason=_safe_error("invalid registry receipt reference", error),
                )
            )
    return ValidationResult(
        kind="registry",
        target=str(root / "registry.json"),
        status=_aggregate_status(checks),
        checks=tuple(checks),
    )


def validate(
    *,
    plan_path: Path | None = None,
    receipt_path: Path | None = None,
    state_home: Path | None = None,
) -> ValidationResult:
    """Dispatch structural validation for one selector or the active registry."""

    if plan_path is not None and receipt_path is not None:
        raise ValueError("select either a plan or a receipt, not both")
    if plan_path is not None:
        return validate_plan_path(plan_path)
    if receipt_path is not None:
        return validate_receipt_path(receipt_path)
    return validate_registered_installations(
        resolve_state_home() if state_home is None else state_home
    )


def load_behavioral_fixture(path: Path) -> BehavioralFixture:
    """Load and validate a self-contained behavioral fixture definition."""

    root = Path(path)
    case_path = root / "case.json"
    prompt_path = root / "prompt.md"
    repo = root / "repo"
    checks = root / "checks"
    try:
        case = json.loads(case_path.read_text(encoding="utf-8"))
        prompt = prompt_path.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(_safe_error(f"invalid fixture {root.name}", error)) from error
    if not isinstance(case, dict):
        raise ValueError("fixture case.json must be an object")
    if case.get("schema_version") != 1:
        raise ValueError("unsupported fixture schema_version")
    name = _required_string(case, "name")
    if name != root.name:
        raise ValueError("fixture name must match its directory")
    if not prompt.strip():
        raise ValueError("fixture prompt.md must not be empty")
    if not repo.is_dir() or not checks.is_dir():
        raise ValueError("fixture requires repo and checks directories")

    supported = _string_tuple(case.get("supported_runners"), "supported_runners")
    if not supported:
        raise ValueError("fixture supported_runners must not be empty")
    tier = _required_string(case, "expected_tier")
    events = case.get("events")
    if not isinstance(events, dict):
        raise ValueError("fixture events must be an object")
    required = _string_tuple(events.get("required", []), "events.required")
    forbidden = _string_tuple(events.get("forbidden", []), "events.forbidden")

    expected_outputs_value = case.get("expected_outputs", [])
    if not isinstance(expected_outputs_value, list):
        raise ValueError("fixture expected_outputs must be a list")
    expected_outputs: list[Mapping[str, str]] = []
    for index, output in enumerate(expected_outputs_value):
        if not isinstance(output, dict):
            raise ValueError(f"fixture expected_outputs[{index}] must be an object")
        relative_path = _required_string(output, "path")
        _safe_relative_path(relative_path, f"expected_outputs[{index}].path")
        contains = output.get("contains")
        if contains is not None and not isinstance(contains, str):
            raise ValueError(
                f"fixture expected_outputs[{index}].contains must be a string"
            )
        expected_outputs.append(
            {"path": relative_path, **({"contains": contains} if contains else {})}
        )

    command_values = case.get("validation_commands")
    if not isinstance(command_values, list) or not command_values:
        raise ValueError("fixture validation_commands must be a non-empty list")
    commands: list[tuple[str, ...]] = []
    for index, command in enumerate(command_values):
        values = _string_tuple(command, f"validation_commands[{index}]")
        if not values:
            raise ValueError(f"validation_commands[{index}] must not be empty")
        commands.append(values)

    timeout = case.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("fixture timeout_seconds must be a positive integer")
    cleanup = case.get("cleanup")
    if cleanup != "always":
        raise ValueError("fixture cleanup must be 'always'")
    seeded_defect = case.get("seeded_defect")
    if seeded_defect is not None and not isinstance(seeded_defect, dict):
        raise ValueError("fixture seeded_defect must be an object or null")

    return BehavioralFixture(
        root=root,
        name=name,
        prompt=prompt,
        repo=repo,
        checks=checks,
        supported_runners=supported,
        expected_tier=tier,
        required_events=required,
        forbidden_events=forbidden,
        expected_outputs=tuple(expected_outputs),
        validation_commands=tuple(commands),
        timeout_seconds=timeout,
        cleanup=cleanup,
        seeded_defect=seeded_defect,
    )


def run_behavioral_fixture(
    path: Path,
    runner: str,
    *,
    runtime_enabled: bool = False,
    executor: ProcessExecutor = _execute_runner,
    command_executor: ProcessExecutor = subprocess.run,
    adapters: Mapping[str, RunnerInvocationAdapter] = RUNNER_ADAPTERS,
    required_events: Iterable[str] | None = None,
    forbidden_events: Iterable[str] | None = None,
) -> BehavioralResult:
    """Run one fixture only through an explicitly enabled, selected CLI."""

    fixture = load_behavioral_fixture(path)
    adapter = adapters.get(runner)
    static_metadata = (
        adapter.capabilities.to_dict() if adapter is not None else None
    )
    if runner not in fixture.supported_runners:
        return unsupported_result(runner, metadata=static_metadata)
    if not runtime_enabled:
        return unavailable_result(
            "behavioral runtime was not explicitly enabled",
            runner=static_metadata,
        )
    if adapter is None:
        return unavailable_result(f"runner adapter is unavailable: {runner}")
    if adapter.capabilities.authenticated is False:
        return unavailable_result(
            f"supported runner is not authenticated: {runner}",
            runner=adapter.capabilities.to_dict(),
        )
    executable = adapter.resolved_executable()
    if executable is None:
        return unavailable_result(
            f"supported runner is not installed: {runner}",
            runner=adapter.capabilities.to_dict(),
        )
    capabilities = _detected_capabilities(adapter, executable)

    required = (
        tuple(required_events)
        if required_events is not None
        else fixture.required_events
    )
    forbidden = (
        tuple(forbidden_events)
        if forbidden_events is not None
        else fixture.forbidden_events
    )

    with tempfile.TemporaryDirectory(prefix=f"apu-{fixture.name}-") as temporary:
        temporary_root = Path(temporary)
        worktree = temporary_root / "repo"
        copied_checks = temporary_root / "checks"
        shutil.copytree(fixture.repo, worktree, symlinks=True)

        try:
            process = executor(
                (
                    executable,
                    *adapter.capabilities.invocation[1:],
                ),
                cwd=worktree,
                input_text=fixture.prompt,
                timeout=fixture.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return BehavioralResult(
                status="failed",
                checks=(
                    CheckResult(
                        name="runner:exit",
                        status="failed",
                        reason=(
                            f"{runner} timed out after "
                            f"{fixture.timeout_seconds} seconds"
                        ),
                    ),
                ),
                runner=capabilities.to_dict(),
            )
        except OSError:
            return unavailable_result(
                f"could not start supported runner: {runner}",
                runner=capabilities.to_dict(),
            )

        if process.returncode != 0:
            if _looks_like_authentication_failure(process.stdout, process.stderr):
                return unavailable_result(
                    f"supported runner is not authenticated: {runner}",
                    runner=replace(
                        capabilities, authenticated=False
                    ).to_dict(),
                )
            return BehavioralResult(
                status="failed",
                checks=(
                    CheckResult(
                        name="runner:exit",
                        status="failed",
                        reason=f"{runner} exited with status {process.returncode}",
                    ),
                ),
                runner=capabilities.to_dict(),
            )

        try:
            events = adapter.parser(process.stdout or "")
        except RunnerParseError:
            return BehavioralResult(
                status="failed",
                checks=(
                    CheckResult(
                        name="runner:events",
                        status="failed",
                        reason=f"{runner} emitted invalid structured events",
                    ),
                ),
                runner=replace(
                    capabilities, authenticated=True
                ).to_dict(),
            )

        event_result = evaluate_event_checks(
            events,
            capabilities,
            required=required,
            forbidden=forbidden,
        )
        if copied_checks.is_symlink():
            copied_checks.unlink()
        elif copied_checks.exists():
            shutil.rmtree(copied_checks)
        shutil.copytree(fixture.checks, copied_checks, symlinks=True)
        checks_results = list(event_result.checks)
        checks_results.append(
            CheckResult(
                name="runner:exit",
                status="passed",
                reason=f"{runner} completed successfully",
            )
        )
        output_results = _check_expected_outputs(fixture, worktree)
        checks_results.extend(output_results)
        if not any(result.status == "failed" for result in output_results):
            checks_results.extend(
                _run_validation_commands(
                    fixture,
                    worktree,
                    command_executor=command_executor,
                )
            )
        return BehavioralResult(
            status=_aggregate_status(checks_results),
            checks=tuple(checks_results),
            events=tuple(events),
            runner=replace(capabilities, authenticated=True).to_dict(),
        )


def _detected_capabilities(
    adapter: RunnerInvocationAdapter,
    executable: str,
) -> RunnerCapabilities:
    version: str | None = None
    try:
        result = subprocess.run(
            (executable, "--version"),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        candidate = (result.stdout or result.stderr).splitlines()
        if candidate:
            version = candidate[0].strip()[:120]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return replace(
        adapter.capabilities,
        version=version,
        authenticated=adapter.capabilities.authenticated,
    )


def _validate_installed_target(
    target: Path,
    installed_sha256: Any,
    operation_id: str,
    operation: Mapping[str, Any],
) -> CheckResult:
    name = f"receipt:target:{operation_id}"
    expected_link = operation.get(
        "created_symlink_target",
        operation.get("installed_link_target"),
    )
    if expected_link is not None:
        if not target.is_symlink():
            return CheckResult(name, "failed", f"{target} is not the installed link")
        if not symlink_points_to(target, str(expected_link)):
            return CheckResult(name, "failed", f"{target} does not match receipt")
        return CheckResult(name, "passed", f"{target} matches receipt")

    if installed_sha256 is None:
        if target.exists() or target.is_symlink():
            return CheckResult(name, "failed", f"{target} should be absent")
        return CheckResult(name, "passed", f"{target} is absent as recorded")
    if not target.exists() or target.is_symlink():
        return CheckResult(name, "failed", f"{target} is missing or has changed type")
    try:
        actual = _hash_installed_object(target)
    except OSError:
        return CheckResult(name, "failed", f"{target} could not be read")
    if actual != installed_sha256:
        return CheckResult(name, "failed", f"{target} does not match receipt")
    return CheckResult(name, "passed", f"{target} matches receipt")


def _hash_installed_object(path: Path) -> str:
    return hash_object(path)


def _registry_receipt_path(
    state_home: Path,
    installation_id: str,
    entry: Mapping[str, Any],
) -> Path:
    value = entry.get("receipt", entry.get("receipt_path"))
    if not isinstance(value, str) or not value:
        raise ValueError(f"installation {installation_id} has no receipt")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = state_home / candidate
    resolved_root = state_home.resolve()
    resolved_candidate = candidate.resolve()
    if not resolved_candidate.is_relative_to(resolved_root):
        raise ValueError(f"installation {installation_id} receipt escapes APU_HOME")
    return candidate


def _check_expected_outputs(
    fixture: BehavioralFixture,
    worktree: Path,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for output in fixture.expected_outputs:
        relative = output["path"]
        target = worktree / relative
        if target.is_symlink() or not target.is_file():
            results.append(
                CheckResult(
                    f"output:{relative}",
                    "failed",
                    f"expected output is missing: {relative}",
                )
            )
            continue
        contains = output.get("contains")
        if contains is not None:
            try:
                matches = contains in target.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                matches = False
            if not matches:
                results.append(
                    CheckResult(
                        f"output:{relative}",
                        "failed",
                        f"expected output content is absent: {relative}",
                    )
                )
                continue
        results.append(
            CheckResult(
                f"output:{relative}",
                "passed",
                f"expected output is present: {relative}",
            )
        )
    return results


def _run_validation_commands(
    fixture: BehavioralFixture,
    worktree: Path,
    *,
    command_executor: ProcessExecutor,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for index, declared in enumerate(fixture.validation_commands):
        command = tuple(
            sys.executable if value == "{python}" else value for value in declared
        )
        name = f"command:{index + 1}"
        try:
            process = command_executor(
                command,
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=fixture.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            results.append(
                CheckResult(
                    name,
                    "failed",
                    f"validation command timed out after {fixture.timeout_seconds} seconds",
                )
            )
            continue
        except OSError:
            results.append(
                CheckResult(name, "failed", "validation command could not start")
            )
            continue
        if process.returncode == 0:
            results.append(
                CheckResult(name, "passed", "validation command passed")
            )
        else:
            results.append(
                CheckResult(
                    name,
                    "failed",
                    f"validation command exited with status {process.returncode}",
                )
            )
    return results


def _looks_like_authentication_failure(stdout: str | None, stderr: str | None) -> bool:
    text = f"{stdout or ''}\n{stderr or ''}".casefold()
    return any(
        marker in text
        for marker in (
            "not authenticated",
            "not logged in",
            "authentication required",
            "login required",
            "missing api key",
            "invalid api key",
            "credentials required",
        )
    )


def _aggregate_status(checks: Iterable[CheckResult]) -> str:
    statuses = {check.status for check in checks}
    if "failed" in statuses:
        return "failed"
    if "unavailable" in statuses:
        return "unavailable"
    if "skipped" in statuses:
        return "skipped"
    return "passed"


def _failed_result(
    kind: str,
    target: Path,
    check_name: str,
    reason: str,
) -> ValidationResult:
    return ValidationResult(
        kind=kind,
        target=str(target),
        status="failed",
        checks=(CheckResult(check_name, "failed", reason),),
        reason=reason,
    )


def _first_failure_reason(result: ValidationResult) -> str:
    return next(
        (
            check.reason
            for check in result.checks
            if check.status == "failed"
        ),
        result.reason or "validation failed",
    )


def _safe_error(prefix: str, error: BaseException) -> str:
    if isinstance(error, json.JSONDecodeError):
        return f"{prefix}: invalid JSON"
    message = str(error)
    return f"{prefix}: {message}" if message else prefix


def _required_string(value: Mapping[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise ValueError(f"fixture {field} must be a non-empty string")
    return item


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"fixture {field} must be a list of non-empty strings")
    return tuple(value)


def _safe_relative_path(value: str, field: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"fixture {field} must stay within the worktree")
    return path

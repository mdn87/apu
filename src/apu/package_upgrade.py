from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from apu.models import sha256_bytes, sha256_json
from apu.package_adapters import ClaudePackageAdapter, PackageObservation
from apu.package_coordinates import PackageCoordinate, parse_semantic_version

_HELP_COMMANDS = (
    ("plugin", "--help"),
    ("plugin", "update", "--help"),
    ("plugin", "install", "--help"),
    ("plugin", "uninstall", "--help"),
)
_MAX_HELP_BYTES = 1024 * 1024
_SHA256_LENGTH = 64

HelpRunner = Callable[[Sequence[str]], "HelpCommandResult"]


class PackageUpgradeError(RuntimeError):
    """Base error for provider-managed package upgrades."""


class PackageUpgradeUnavailable(PackageUpgradeError):
    """Raised before mutation when the safe upgrade contract is unavailable."""


@dataclass(frozen=True)
class HelpCommandResult:
    exit_code: int
    stdout: bytes
    stderr: bytes = b""

    def __post_init__(self) -> None:
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise TypeError("help command exit_code must be an integer")
        if not isinstance(self.stdout, bytes) or not isinstance(self.stderr, bytes):
            raise TypeError("help command output must be bytes")
        if len(self.stdout) + len(self.stderr) > _MAX_HELP_BYTES:
            raise ValueError("help command output exceeds the evidence limit")


@dataclass(frozen=True)
class HelpEvidence:
    command: tuple[str, ...]
    exit_code: int
    stdout_sha256: str
    stderr_sha256: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["command"] = list(self.command)
        return value


@dataclass(frozen=True)
class PackageStateIdentity:
    package_id: str
    version: str
    tree_sha256: str
    scope: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.package_id, self.version, self.scope)
        ):
            raise ValueError("package state identity fields must be non-empty")
        parse_semantic_version(self.version)
        _require_sha256(self.tree_sha256, "package state tree_sha256")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_observation(
        cls,
        coordinate: PackageCoordinate,
        observation: PackageObservation,
    ) -> PackageStateIdentity:
        if (
            observation.status != "verified"
            or observation.version is None
            or observation.tree_sha256 is None
            or observation.scope is None
        ):
            raise PackageUpgradeUnavailable(
                "installed package observation is not authoritative"
            )
        return cls(
            package_id=coordinate.package_id,
            version=observation.version,
            tree_sha256=observation.tree_sha256,
            scope=observation.scope,
        )


@dataclass(frozen=True)
class PackageUpgradeRequest:
    package_id: str
    operation_id: str
    attempt: int
    pre_state: PackageStateIdentity
    target_version: str
    target_tree_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.package_id, str) or not self.package_id:
            raise ValueError("package upgrade package_id is required")
        if not isinstance(self.operation_id, str) or not self.operation_id:
            raise ValueError("package upgrade operation_id is required")
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
        ):
            raise ValueError("package upgrade attempt must be a positive integer")
        if self.pre_state.package_id != self.package_id:
            raise ValueError("package upgrade pre-state identifies another package")
        parse_semantic_version(self.target_version)
        _require_sha256(
            self.target_tree_sha256,
            "package upgrade target_tree_sha256",
        )

    @property
    def idempotency_key(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "attempt": self.attempt,
        }

    @property
    def transaction_id(self) -> str:
        return sha256_json(
            {
                "package_id": self.package_id,
                "idempotency_key": self.idempotency_key,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "operation_id": self.operation_id,
            "attempt": self.attempt,
            "idempotency_key": self.idempotency_key,
            "transaction_id": self.transaction_id,
            "pre_state": self.pre_state.to_dict(),
            "target": {
                "version": self.target_version,
                "tree_sha256": self.target_tree_sha256,
            },
        }


@dataclass(frozen=True)
class PackageUpgradeProtocolContract:
    """Invariants an executable provider adapter must satisfy."""

    journal_before_mutation: bool = True
    authoritative_pre_state: bool = True
    official_provider_operation_only: bool = True
    exact_target_version_required: bool = True
    target_tree_verification_required: bool = True
    receipt_required: bool = True
    official_rollback_required: bool = True
    rollback_verification_required: bool = True
    idempotency_required: bool = True
    journal_states: tuple[str, ...] = (
        "prepared",
        "provider-update-returned",
        "verified",
        "rollback-requested",
        "rolled-back",
        "rollback-failed",
    )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["journal_states"] = list(self.journal_states)
        return value


@dataclass(frozen=True)
class PackageUpgradeCapability:
    provider: str
    package_id: str
    status: str
    exact_version_supported: bool
    verifiable_rollback_supported: bool
    execution_supported: bool
    pre_state: PackageStateIdentity | None
    reason_codes: tuple[str, ...]
    help_evidence: tuple[HelpEvidence, ...]
    protocol: PackageUpgradeProtocolContract = PackageUpgradeProtocolContract()

    def __post_init__(self) -> None:
        if self.status not in {"available", "unavailable"}:
            raise ValueError("unsupported package upgrade capability status")
        if self.status == "available" and (
            not self.exact_version_supported
            or not self.verifiable_rollback_supported
            or not self.execution_supported
            or self.pre_state is None
            or self.reason_codes
        ):
            raise ValueError("available upgrade capability is incomplete")
        if self.status == "unavailable" and not self.reason_codes:
            raise ValueError("unavailable upgrade capability requires reason codes")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("capability reason codes must be sorted and unique")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "artifact_type": "package-upgrade-capability",
            "provider": self.provider,
            "package_id": self.package_id,
            "status": self.status,
            "exact_version_supported": self.exact_version_supported,
            "verifiable_rollback_supported": self.verifiable_rollback_supported,
            "execution_supported": self.execution_supported,
            "pre_state": (
                self.pre_state.to_dict() if self.pre_state is not None else None
            ),
            "reason_codes": list(self.reason_codes),
            "help_evidence": [item.to_dict() for item in self.help_evidence],
            "protocol": self.protocol.to_dict(),
        }


class PackageUpgradeExecutor(Protocol):
    """Future executable adapter boundary; all operations must be provider-owned."""

    provider: str

    def observe(self, coordinate: PackageCoordinate) -> PackageStateIdentity:
        """Return authoritative version and tree identity."""

    def update_exact(
        self,
        coordinate: PackageCoordinate,
        request: PackageUpgradeRequest,
    ) -> Mapping[str, Any]:
        """Run an official exact-version provider operation."""

    def rollback_exact(
        self,
        coordinate: PackageCoordinate,
        pre_state: PackageStateIdentity,
    ) -> Mapping[str, Any]:
        """Run an official provider operation restoring the captured version."""


def assess_claude_package_upgrade(
    coordinate: PackageCoordinate,
    *,
    home: Path,
    help_runner: HelpRunner | None = None,
) -> PackageUpgradeCapability:
    """Probe Claude's read-only help and installed state without mutation."""

    if coordinate.provider != "claude":
        raise ValueError("Claude upgrade capability requires a Claude coordinate")
    adapter = ClaudePackageAdapter(home=home)
    first = adapter.observe(coordinate.profile_selector)
    reasons: list[str] = []
    try:
        pre_state = PackageStateIdentity.from_observation(coordinate, first)
    except PackageUpgradeUnavailable:
        pre_state = None
        reasons.append("installed-observation-not-authoritative")

    runner = help_runner or _run_claude_help
    evidence: list[HelpEvidence] = []
    help_text: dict[tuple[str, ...], str] = {}
    for arguments in _HELP_COMMANDS:
        try:
            result = runner(arguments)
        except (OSError, subprocess.SubprocessError, ValueError):
            reasons.append("provider-help-unavailable")
            continue
        evidence.append(
            HelpEvidence(
                command=("claude", *arguments),
                exit_code=result.exit_code,
                stdout_sha256=sha256_bytes(result.stdout),
                stderr_sha256=sha256_bytes(result.stderr),
            )
        )
        if result.exit_code != 0:
            reasons.append("provider-help-unavailable")
            continue
        help_text[arguments] = result.stdout.decode("utf-8", errors="replace")

    update_help = help_text.get(("plugin", "update", "--help"), "")
    install_help = help_text.get(("plugin", "install", "--help"), "")
    exact_update = _documents_exact_version(update_help)
    exact_restore = _documents_exact_version(install_help)
    if not exact_update:
        reasons.append("provider-exact-version-selection-unsupported")
    if not exact_restore:
        reasons.append("provider-verifiable-rollback-unsupported")

    second = adapter.observe(coordinate.profile_selector)
    if second.to_dict() != first.to_dict():
        reasons.append("provider-state-changed-during-capability-probe")

    # The installed Claude CLI currently documents latest-only updates and no
    # exact reinstall/rollback. Keep execution disabled even if future help
    # text changes until a provider adapter implements and verifies both legs.
    reasons.append("apu-provider-upgrade-executor-disabled")
    return PackageUpgradeCapability(
        provider="claude",
        package_id=coordinate.package_id,
        status="unavailable",
        exact_version_supported=exact_update,
        verifiable_rollback_supported=exact_restore,
        execution_supported=False,
        pre_state=pre_state,
        reason_codes=tuple(sorted(set(reasons))),
        help_evidence=tuple(evidence),
    )


def require_package_upgrade(
    request: PackageUpgradeRequest,
    capability: PackageUpgradeCapability,
    *,
    state_home: Path,
) -> None:
    """Refuse before state creation unless an executable safe adapter exists."""

    del state_home
    if capability.package_id != request.package_id:
        raise PackageUpgradeUnavailable(
            "package upgrade capability identifies another package"
        )
    if capability.pre_state != request.pre_state:
        raise PackageUpgradeUnavailable(
            "package upgrade pre-state no longer matches the capability probe"
        )
    if capability.status != "available":
        raise PackageUpgradeUnavailable(
            "package upgrade is unavailable: " + ",".join(capability.reason_codes)
        )
    raise PackageUpgradeUnavailable(
        "package upgrade executor is not implemented for this provider"
    )


def _documents_exact_version(help_text: str) -> bool:
    lowered = help_text.casefold()
    return (
        "--version <version>" in lowered
        or "--version [version]" in lowered
        or "<plugin>@<version>" in lowered
    )


def _run_claude_help(arguments: Sequence[str]) -> HelpCommandResult:
    executable = shutil.which("claude")
    if executable is None:
        raise OSError("Claude CLI is unavailable")
    command: list[str]
    if os.name == "nt" and Path(executable).suffix.casefold() in {".bat", ".cmd"}:
        if any(character in executable for character in "&|<>^()%!"):
            raise OSError("Claude CLI wrapper path is unsafe")
        command_processor = os.environ.get("COMSPEC")
        if not command_processor:
            raise OSError("Windows command processor is unavailable")
        command = [
            command_processor,
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline([executable, *arguments]),
        ]
    else:
        command = [executable, *arguments]
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PATHEXT": os.environ.get("PATHEXT", ""),
        "COMSPEC": os.environ.get("COMSPEC", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "WINDIR": os.environ.get("WINDIR", ""),
        "CLAUDE_CODE_SAFE_MODE": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        env=environment,
        timeout=30,
    )
    return HelpCommandResult(
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _require_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value

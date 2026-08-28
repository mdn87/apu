from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping


class RunnerParseError(ValueError):
    """Raised when a runner emits malformed JSONL."""


@dataclass(frozen=True)
class RunnerCapabilities:
    provider: str
    cli_name: str
    observable_events: frozenset[str]
    invocation: tuple[str, ...]
    version: str | None = None
    authenticated: bool | None = None

    def with_observable_events(self, events: Iterable[str]) -> RunnerCapabilities:
        return replace(self, observable_events=frozenset(events))

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "cli_name": self.cli_name,
            "observable_events": sorted(self.observable_events),
            "invocation": list(self.invocation),
            "version": self.version,
            "authenticated": self.authenticated,
        }


@dataclass(frozen=True)
class NormalizedEvent:
    type: str
    provider_event: str
    metadata: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "provider_event": self.provider_event,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BehavioralResult:
    status: str
    checks: tuple[CheckResult, ...] = ()
    events: tuple[NormalizedEvent, ...] = ()
    reason: str | None = None
    runner: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
            "events": [event.to_dict() for event in self.events],
            "reason": self.reason,
            "runner": dict(self.runner) if self.runner is not None else None,
        }


def parse_jsonl(
    content: str | Iterable[str],
) -> tuple[Mapping[str, Any], ...]:
    lines = content.splitlines() if isinstance(content, str) else content
    records: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError) as error:
            raise RunnerParseError(
                f"invalid runner JSON on line {line_number}"
            ) from error
        if not isinstance(value, dict):
            raise RunnerParseError(
                f"runner JSON on line {line_number} must be an object"
            )
        records.append(value)
    return tuple(records)


def evaluate_event_checks(
    events: Iterable[NormalizedEvent],
    capabilities: RunnerCapabilities,
    *,
    required: Iterable[str] = (),
    forbidden: Iterable[str] = (),
) -> BehavioralResult:
    recorded_events = tuple(events)
    observed = {event.type for event in recorded_events}
    checks: list[CheckResult] = []

    for event_type in required:
        name = f"required:{event_type}"
        if event_type not in capabilities.observable_events:
            checks.append(
                CheckResult(
                    name=name,
                    status="skipped",
                    reason=(f"{capabilities.cli_name} cannot observe {event_type}"),
                )
            )
        elif event_type in observed:
            checks.append(
                CheckResult(
                    name=name,
                    status="passed",
                    reason=f"observed required event: {event_type}",
                )
            )
        else:
            checks.append(
                CheckResult(
                    name=name,
                    status="failed",
                    reason=f"required event was not observed: {event_type}",
                )
            )

    for event_type in forbidden:
        name = f"forbidden:{event_type}"
        if event_type not in capabilities.observable_events:
            checks.append(
                CheckResult(
                    name=name,
                    status="skipped",
                    reason=(f"{capabilities.cli_name} cannot observe {event_type}"),
                )
            )
        elif event_type in observed:
            checks.append(
                CheckResult(
                    name=name,
                    status="failed",
                    reason=f"observed forbidden event: {event_type}",
                )
            )
        else:
            checks.append(
                CheckResult(
                    name=name,
                    status="passed",
                    reason=f"forbidden event was not observed: {event_type}",
                )
            )

    statuses = {check.status for check in checks}
    if "failed" in statuses:
        status = "failed"
    elif "skipped" in statuses:
        status = "skipped"
    else:
        status = "passed"
    return BehavioralResult(
        status=status,
        checks=tuple(checks),
        events=recorded_events,
    )


def unavailable_result(
    reason: str,
    *,
    runner: Mapping[str, Any] | None = None,
) -> BehavioralResult:
    return BehavioralResult(status="unavailable", reason=reason, runner=runner)


def unsupported_result(
    runner: str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> BehavioralResult:
    return BehavioralResult(
        status="skipped",
        reason=f"case does not support runner: {runner}",
        runner=metadata,
    )

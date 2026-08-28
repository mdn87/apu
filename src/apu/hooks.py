from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, BinaryIO

from apu import __version__

from .apply import apply_plan
from .evidence import ingest_hook_event, observe_repository_state
from .filesystem import hash_object
from .locking import ProcessLock
from .models import (
    Approval,
    Plan,
    PlanOperation,
    canonical_json,
    sha256_bytes,
)
from .state import resolve_state_home

MAX_HOOK_INPUT_BYTES = 1024 * 1024
_TERMINAL_WATCH_EVENTS = frozenset({"Stop", "SessionEnd"})
_SUPPORTED_HOOK_PROVIDERS = frozenset({"claude", "codex"})
_SUPPORTED_HOOK_SCOPES = frozenset({"user", "project"})
_EVIDENCE_PROVIDER_BY_HOOK_PROVIDER = {
    "claude": "claude-code",
    "codex": "codex",
}


class HookInputError(ValueError):
    """Raised when a provider hook payload cannot be safely ingested."""


def read_hook_payload(
    stream: BinaryIO,
    *,
    max_bytes: int = MAX_HOOK_INPUT_BYTES,
) -> dict[str, Any]:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("hook input byte limit must be positive")
    raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HookInputError(f"hook input exceeds the {max_bytes}-byte limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HookInputError("hook input must be one UTF-8 JSON object") from error
    if not isinstance(value, dict):
        raise HookInputError("hook input must be one JSON object")
    return value


def hook_event_name(
    payload: Mapping[str, Any], *, expected_event: str | None = None
) -> str:
    value = payload.get("hook_event_name", payload.get("hookEventName"))
    if not isinstance(value, str) or not value:
        raise HookInputError("hook input requires hook_event_name")
    if expected_event is not None and value != expected_event:
        raise HookInputError(
            f"hook_event_name {value!r} does not match expected event {expected_event!r}"
        )
    return value


def ingest_hook_stream(
    state_home: Path,
    provider: str,
    stream: BinaryIO,
    *,
    expected_event: str | None = None,
    observe_state: bool = False,
    passive_watch: bool = False,
    max_bytes: int = MAX_HOOK_INPUT_BYTES,
) -> dict[str, Any]:
    """Strictly ingest one provider hook stream.

    Provider-facing entry points should call :func:`hook_bridge_main`, which
    converts all ingestion failures into a silent success so telemetry can
    never block the provider lifecycle.
    """

    payload = read_hook_payload(stream, max_bytes=max_bytes)
    event_name = hook_event_name(payload, expected_event=expected_event)
    if passive_watch and event_name not in _TERMINAL_WATCH_EVENTS:
        raise HookInputError("passive watch accepts only Stop or SessionEnd events")
    _, event, appended = ingest_hook_event(
        Path(state_home),
        provider,
        event_name,
        payload,
    )
    state_event = None
    state_appended = False
    should_observe = observe_state or passive_watch
    if should_observe:
        cwd = event["state"]["cwd"]
        if cwd is None:
            raise HookInputError("repository observation requires an absolute cwd")
        _, state_event, state_appended = observe_repository_state(
            Path(state_home),
            provider=provider,
            session_id=event["session_id"],
            cwd=Path(cwd),
            sequence=event["sequence"] + 1,
        )
    return {
        "accepted": True,
        "provider": provider,
        "event": event_name,
        "session_id": event["session_id"],
        "event_id": event["event_id"],
        "appended": appended,
        "state_event_id": state_event["event_id"] if state_event else None,
        "state_appended": state_appended,
        "passive_watch": passive_watch,
        "durable_policy_mutation": False,
        "trust_changed": False,
    }


def hook_bridge_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="apu hooks bridge")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--event")
    parser.add_argument("--observe-state", action="store_true")
    parser.add_argument("--passive-watch", action="store_true")
    return parser


def hook_bridge_main(
    argv: Sequence[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    state_home: Path | None = None,
) -> int:
    """Run the provider-facing bridge silently and fail open.

    Hook telemetry is advisory. Invalid payloads, unavailable state, and lock
    contention must not alter the provider action or trust decision.
    """

    try:
        args = hook_bridge_parser().parse_args(argv)
        selected_stream = stdin
        if selected_stream is None:
            selected_stream = sys.stdin.buffer
        ingest_hook_stream(
            state_home or resolve_state_home(),
            args.provider,
            selected_stream,
            expected_event=args.event,
            observe_state=args.observe_state,
            passive_watch=args.passive_watch,
        )
    except (Exception, SystemExit):
        return 0
    return 0


def add_hooks_parser(commands: argparse._SubParsersAction) -> None:
    """Register reusable ``apu hooks`` parsers for the root CLI."""

    hooks = commands.add_parser(
        "hooks",
        help="review and manage passive provider lifecycle hooks",
    )
    subcommands = hooks.add_subparsers(dest="hooks_command", required=True)

    bridge = subcommands.add_parser(
        "bridge",
        help="silently ingest one provider lifecycle event",
    )
    bridge.add_argument("--provider", required=True)
    bridge.add_argument("--event")
    bridge.add_argument("--observe-state", action="store_true")
    bridge.add_argument("--passive-watch", action="store_true")

    for name in ("render", "status", "doctor", "install", "remove"):
        command = subcommands.add_parser(name)
        command.add_argument(
            "--provider",
            required=True,
            choices=sorted(_SUPPORTED_HOOK_PROVIDERS),
        )
        command.add_argument(
            "--scope",
            required=True,
            choices=sorted(_SUPPORTED_HOOK_SCOPES),
        )
        command.add_argument("--repository", type=Path)
        if name in {"render", "doctor", "install"}:
            command.add_argument("--executable", default="apu")
        if name in {"render", "install"}:
            command.add_argument("--passive-watch", action="store_true")
        if name in {"render", "install", "remove"}:
            command.add_argument(
                "--event",
                dest="events",
                action="append",
                choices=sorted(_TERMINAL_WATCH_EVENTS),
            )
        if name in {"install", "remove"}:
            command.add_argument(
                "--apply",
                action="store_true",
                help="apply the previewed provider configuration change",
            )


def run_hooks(
    args: argparse.Namespace,
    *,
    home: Path | None = None,
    stdin: BinaryIO | None = None,
    state_home: Path | None = None,
) -> int:
    """Dispatch a parsed hook command; only ``bridge`` is intentionally silent."""

    command = args.hooks_command
    if command == "bridge":
        try:
            selected_stream = stdin if stdin is not None else sys.stdin.buffer
            ingest_hook_stream(
                state_home or resolve_state_home(),
                args.provider,
                selected_stream,
                expected_event=args.event,
                observe_state=args.observe_state,
                passive_watch=args.passive_watch,
            )
        except (Exception, SystemExit):
            return 0
        return 0

    selected_home = _absolute_logical_path(home or Path.home())
    common = {
        "scope": args.scope,
        "home": selected_home,
        "repository": args.repository,
    }
    events = (
        tuple(args.events)
        if getattr(args, "events", None)
        else (
            "SessionEnd",
            "Stop",
        )
    )
    if command == "render":
        result = render_hooks(
            args.provider,
            **common,
            executable=args.executable,
            passive_watch=args.passive_watch,
            events=events,
        )
    elif command == "status":
        result = hooks_status(args.provider, **common)
    elif command == "doctor":
        result = doctor_hooks(
            args.provider,
            **common,
            executable=args.executable,
        )
    elif command == "install":
        result = install_hooks(
            args.provider,
            **common,
            executable=args.executable,
            passive_watch=args.passive_watch,
            events=events,
            state_home=state_home or resolve_state_home(),
            apply=args.apply,
        )
    elif command == "remove":
        result = remove_hooks(
            args.provider,
            **common,
            events=events,
            state_home=state_home or resolve_state_home(),
            apply=args.apply,
        )
    else:
        raise ValueError(f"unsupported hooks command: {command}")
    print(canonical_json(result))
    return 0


def render_hooks(
    provider: str,
    *,
    scope: str,
    home: Path,
    repository: Path | None = None,
    passive_watch: bool = False,
    executable: str | Path = "apu",
    events: Sequence[str] = ("SessionEnd", "Stop"),
) -> dict[str, Any]:
    """Render a reviewable provider fragment without touching configuration."""

    provider = _validate_hook_provider(provider)
    evidence_provider = _hook_evidence_provider(provider)
    scope = _validate_hook_scope(scope)
    selected_events = _validate_hook_events(events)
    if not isinstance(passive_watch, bool):
        raise ValueError("passive_watch must be boolean")
    executable_value = str(executable)
    if not executable_value or "\x00" in executable_value:
        raise ValueError("hook executable must be non-empty")
    if Path(executable_value).name.lower() not in {"apu", "apu.exe"}:
        raise ValueError("hook executable must name the apu entry point")
    target = hook_config_path(
        provider,
        scope=scope,
        home=home,
        repository=repository,
    )
    hooks: dict[str, list[dict[str, Any]]] = {}
    for event in selected_events:
        tokens = [
            executable_value,
            "hooks",
            "bridge",
            "--provider",
            evidence_provider,
            "--event",
            event,
        ]
        if passive_watch:
            tokens.append("--passive-watch")
        hooks[event] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": shlex.join(tokens),
                    }
                ]
            }
        ]
    return {
        "provider": provider,
        "scope": scope,
        "target": str(target),
        "events": list(selected_events),
        "passive_watch": passive_watch,
        "fragment": {"hooks": hooks},
        "policy_changes": False,
        "trust_changes": False,
    }


def hook_config_path(
    provider: str,
    *,
    scope: str,
    home: Path,
    repository: Path | None = None,
) -> Path:
    provider = _validate_hook_provider(provider)
    scope = _validate_hook_scope(scope)
    normalized_home = _absolute_logical_path(home)
    if scope == "project":
        if repository is None:
            raise ValueError("project hook scope requires an explicit repository")
        base = _absolute_logical_path(repository)
        if not base.is_dir():
            raise ValueError("project hook repository must be an existing directory")
        if provider == "claude":
            target = base / ".claude" / "settings.local.json"
        else:
            target = base / ".codex" / "hooks.json"
    elif provider == "claude":
        target = normalized_home / ".claude" / "settings.json"
    else:
        target = normalized_home / ".codex" / "hooks.json"
    _validate_hook_config_target(target)
    return target


def hooks_status(
    provider: str,
    *,
    scope: str,
    home: Path,
    repository: Path | None = None,
) -> dict[str, Any]:
    """Inspect only APU bridge registrations, never command bodies from others."""

    provider = _validate_hook_provider(provider)
    scope = _validate_hook_scope(scope)
    target = hook_config_path(
        provider,
        scope=scope,
        home=home,
        repository=repository,
    )
    exists = target.is_file()
    value, valid = _load_hook_config(target)
    managed = _managed_hook_entries(value, provider) if valid else ()
    events = sorted({entry[0] for entry in managed})
    passive_events = sorted({entry[0] for entry in managed if entry[1]})
    state = "invalid" if not valid else ("configured" if managed else "not-configured")
    return {
        "provider": provider,
        "scope": scope,
        "target": str(target),
        "exists": exists,
        "state": state,
        "managed_events": events,
        "managed_registration_count": len(managed),
        "passive_watch_events": passive_events,
        "policy_changes": False,
        "trust_changes": False,
    }


def doctor_hooks(
    provider: str,
    *,
    scope: str,
    home: Path,
    repository: Path | None = None,
    executable: str | Path = "apu",
) -> dict[str, Any]:
    """Run non-mutating structural checks for one explicit provider scope."""

    status = hooks_status(
        provider,
        scope=scope,
        home=home,
        repository=repository,
    )
    rendered = render_hooks(
        provider,
        scope=scope,
        home=home,
        repository=repository,
        executable=executable,
    )
    bridge_valid = all(
        _managed_command_details(command) is not None
        for command in _fragment_commands(rendered["fragment"])
    )
    return {
        **status,
        "ok": status["state"] != "invalid" and bridge_valid,
        "bridge_render_valid": bridge_valid,
        "read_only": True,
    }


def install_hooks(
    provider: str,
    *,
    scope: str,
    home: Path,
    repository: Path | None = None,
    passive_watch: bool = False,
    executable: str | Path = "apu",
    events: Sequence[str] = ("SessionEnd", "Stop"),
    state_home: Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or atomically install APU-owned bridge registrations."""

    if not isinstance(apply, bool):
        raise ValueError("apply must be boolean")
    rendered = render_hooks(
        provider,
        scope=scope,
        home=home,
        repository=repository,
        passive_watch=passive_watch,
        executable=executable,
        events=events,
    )
    return _mutate_hooks(
        rendered,
        home=home,
        state_home=state_home,
        action="install",
        apply=apply,
    )


def remove_hooks(
    provider: str,
    *,
    scope: str,
    home: Path,
    repository: Path | None = None,
    events: Sequence[str] = ("SessionEnd", "Stop"),
    state_home: Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or atomically remove only recognizable APU bridge handlers."""

    if not isinstance(apply, bool):
        raise ValueError("apply must be boolean")
    rendered = render_hooks(
        provider,
        scope=scope,
        home=home,
        repository=repository,
        events=events,
    )
    return _mutate_hooks(
        rendered,
        home=home,
        state_home=state_home,
        action="remove",
        apply=apply,
    )


def _mutate_hooks(
    rendered: Mapping[str, Any],
    *,
    home: Path,
    state_home: Path | None,
    action: str,
    apply: bool,
) -> dict[str, Any]:
    target = Path(str(rendered["target"]))
    provider = str(rendered["provider"])
    events = tuple(str(event) for event in rendered["events"])

    def prepare() -> tuple[dict[str, Any], dict[str, Any], bool, str | None]:
        _validate_hook_config_target(target)
        current, valid = _load_hook_config(target)
        if not valid:
            raise ValueError(f"invalid hook configuration at {target}")
        without_managed = _without_managed_hooks(current, provider, events)
        if action == "install":
            proposed = _merge_hook_fragment(
                without_managed,
                rendered["fragment"],
            )
        elif action == "remove":
            proposed = without_managed
        else:  # pragma: no cover - internal contract
            raise ValueError(f"unsupported hook mutation: {action}")
        before_sha256 = hash_object(target) if target.is_file() else None
        return current, proposed, current != proposed, before_sha256

    receipt: Path | None = None
    installation_id: str | None = None
    if apply:
        selected_state_home = (
            _absolute_logical_path(state_home)
            if state_home is not None
            else resolve_state_home(env={}, home=_absolute_logical_path(home))
        )
        lock_name = sha256(str(target).encode("utf-8")).hexdigest()
        with ProcessLock(
            selected_state_home / "locks" / f"hooks-{lock_name}.lock",
            timeout=10.0,
        ):
            current, proposed, changed, before_sha256 = prepare()
            if changed:
                installation_id, receipt = _apply_hook_plan(
                    target=target,
                    provider=provider,
                    scope=str(rendered["scope"]),
                    requested_action=action,
                    current=current,
                    proposed=proposed,
                    before_sha256=before_sha256,
                    state_home=selected_state_home,
                )
                readback, valid = _load_hook_config(target)
                should_remove = action == "remove" and not proposed
                if (should_remove and target.exists()) or (
                    not should_remove and (not valid or readback != proposed)
                ):
                    raise RuntimeError("hook configuration readback did not match")
    else:
        current, proposed, changed, before_sha256 = prepare()

    should_remove = action == "remove" and not proposed and changed
    if not changed:
        after_sha256 = before_sha256
    elif should_remove:
        after_sha256 = None
    else:
        after_sha256 = sha256_bytes(canonical_json(proposed).encode("utf-8"))

    return {
        "action": action,
        "provider": provider,
        "scope": rendered["scope"],
        "target": str(target),
        "events": list(events),
        "changed": changed,
        "applied": apply and changed,
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "installation_id": installation_id,
        "receipt": str(receipt) if receipt is not None else None,
        "policy_changes": False,
        "trust_changes": False,
    }


def _apply_hook_plan(
    *,
    target: Path,
    provider: str,
    scope: str,
    requested_action: str,
    current: Mapping[str, Any],
    proposed: Mapping[str, Any],
    before_sha256: str | None,
    state_home: Path,
) -> tuple[str, Path]:
    should_remove = requested_action == "remove" and not proposed
    operation_action = "remove" if should_remove else "configure"
    proposed_bytes = canonical_json(proposed).encode("utf-8")
    proposed_sha256 = None if should_remove else sha256_bytes(proposed_bytes)
    recorded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    installation_id = f"hooks-{provider}-{scope}-{secrets.token_hex(8)}"

    with tempfile.TemporaryDirectory(prefix="apu-hooks-candidate-") as directory:
        source: Path | None = None
        if not should_remove:
            source = Path(directory) / "hooks.json"
            source.write_bytes(proposed_bytes)
            if os.name == "posix":
                source.chmod(0o600)
        operation = PlanOperation(
            id=f"hooks-{requested_action}-{provider}-{scope}",
            action=operation_action,
            target=str(target),
            source=str(source) if source is not None else None,
            ownership="user" if before_sha256 is not None else "apu",
            strategy="full_file",
            precondition_sha256=before_sha256,
            proposed_sha256=proposed_sha256,
            backup_required=before_sha256 is not None,
            requires_confirmation=False,
            approval=Approval(
                status="approved",
                recorded_at=recorded_at,
                method="explicit-hooks-apply",
            ),
            reason=(
                f"Explicitly {requested_action} APU {provider} hooks in {scope} scope."
            ),
            evidence=(),
        )
        plan_identity = {
            "target": str(target),
            "provider": provider,
            "scope": scope,
            "requested_action": requested_action,
            "precondition_sha256": before_sha256,
            "proposed_sha256": proposed_sha256,
        }
        plan = Plan(
            schema_version=1,
            apu_version=__version__,
            created_at=recorded_at,
            inventory_sha256=sha256(
                canonical_json(plan_identity).encode("utf-8")
            ).hexdigest(),
            status="approved",
            operations=(operation,),
            validation={
                "commands": [],
                "fixtures": [],
                "required": [],
                "protected_roots": [],
            },
        )
        receipt = apply_plan(
            plan,
            state_home=state_home,
            installation_id=installation_id,
            confirmed=True,
        )
    return installation_id, receipt


def _load_hook_config(path: Path) -> tuple[dict[str, Any], bool]:
    if path.is_symlink():
        return {}, False
    if not path.exists():
        return {}, True
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, False
    if not isinstance(value, dict):
        return {}, False
    hooks = value.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        return value, False
    if isinstance(hooks, dict):
        for registrations in hooks.values():
            if not isinstance(registrations, list):
                return value, False
            for registration in registrations:
                if not isinstance(registration, dict):
                    return value, False
                handlers = registration.get("hooks")
                if not isinstance(handlers, list):
                    return value, False
                if not all(isinstance(handler, dict) for handler in handlers):
                    return value, False
    return value, True


def _managed_hook_entries(
    value: Mapping[str, Any], provider: str
) -> tuple[tuple[str, bool], ...]:
    hooks = value.get("hooks")
    if not isinstance(hooks, dict):
        return ()
    found: list[tuple[str, bool]] = []
    for registrations in hooks.values():
        for registration in registrations:
            for handler in registration["hooks"]:
                details = _managed_command_details(handler.get("command"))
                if details is not None and details[0] == _hook_evidence_provider(
                    provider
                ):
                    found.append((details[1], details[2]))
    return tuple(found)


def _managed_command_details(value: Any) -> tuple[str, str, bool] | None:
    if not isinstance(value, str):
        return None
    try:
        tokens = shlex.split(value)
    except ValueError:
        return None
    try:
        bridge_index = next(
            index
            for index in range(1, len(tokens) - 1)
            if tokens[index : index + 2] == ["hooks", "bridge"]
        )
    except StopIteration:
        return None
    if bridge_index != 1 or Path(tokens[0]).name.lower() not in {"apu", "apu.exe"}:
        return None
    arguments = tokens[bridge_index + 2 :]
    provider = None
    event = None
    passive_watch = False
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"--provider", "--event"}:
            if index + 1 >= len(arguments):
                return None
            if argument == "--provider":
                provider = arguments[index + 1]
            else:
                event = arguments[index + 1]
            index += 2
            continue
        if argument == "--passive-watch":
            passive_watch = True
            index += 1
            continue
        return None
    if provider not in _EVIDENCE_PROVIDER_BY_HOOK_PROVIDER.values() or event is None:
        return None
    try:
        selected_event = _validate_hook_events((event,))[0]
    except ValueError:
        return None
    return provider, selected_event, passive_watch


def _without_managed_hooks(
    value: Mapping[str, Any], provider: str, events: Sequence[str]
) -> dict[str, Any]:
    selected = set(events)
    updated = dict(value)
    hooks = value.get("hooks")
    if not isinstance(hooks, dict):
        return updated
    updated_hooks: dict[str, Any] = {}
    removed_any = False
    for event, registrations in hooks.items():
        retained_registrations: list[dict[str, Any]] = []
        for registration in registrations:
            retained_handlers = []
            for handler in registration["hooks"]:
                details = _managed_command_details(handler.get("command"))
                if (
                    details is not None
                    and details[0] == _hook_evidence_provider(provider)
                    and details[1] in selected
                ):
                    removed_any = True
                    continue
                retained_handlers.append(handler)
            if retained_handlers:
                retained = dict(registration)
                retained["hooks"] = retained_handlers
                retained_registrations.append(retained)
        if retained_registrations:
            updated_hooks[event] = retained_registrations
    if not removed_any:
        return updated
    if updated_hooks:
        updated["hooks"] = updated_hooks
    else:
        updated.pop("hooks", None)
    return updated


def _merge_hook_fragment(
    current: Mapping[str, Any], fragment: Mapping[str, Any]
) -> dict[str, Any]:
    updated = dict(current)
    current_hooks = dict(updated.get("hooks", {}))
    fragment_hooks = fragment.get("hooks")
    if not isinstance(fragment_hooks, dict):  # pragma: no cover - renderer contract
        raise ValueError("rendered hook fragment is invalid")
    for event, registrations in fragment_hooks.items():
        current_hooks[event] = [
            *current_hooks.get(event, []),
            *registrations,
        ]
    updated["hooks"] = current_hooks
    return updated


def _fragment_commands(fragment: Mapping[str, Any]) -> tuple[str, ...]:
    commands: list[str] = []
    for registrations in fragment.get("hooks", {}).values():
        for registration in registrations:
            for handler in registration.get("hooks", []):
                command = handler.get("command")
                if isinstance(command, str):
                    commands.append(command)
    return tuple(commands)


def _validate_hook_provider(provider: str) -> str:
    if provider not in _SUPPORTED_HOOK_PROVIDERS:
        raise ValueError("hook provider must be claude or codex")
    return provider


def _hook_evidence_provider(provider: str) -> str:
    return _EVIDENCE_PROVIDER_BY_HOOK_PROVIDER[_validate_hook_provider(provider)]


def _validate_hook_config_target(target: Path) -> None:
    """Reject provider configuration redirects before preview or mutation."""

    provider_directory = target.parent
    if target.is_symlink() or provider_directory.is_symlink():
        raise ValueError("hook configuration target cannot be a filesystem redirect")
    if provider_directory.exists() and not provider_directory.is_dir():
        raise ValueError("hook provider configuration path must be a directory")


def _validate_hook_scope(scope: str) -> str:
    if scope not in _SUPPORTED_HOOK_SCOPES:
        raise ValueError("hook scope must be explicitly user or project")
    return scope


def _validate_hook_events(events: Sequence[str]) -> tuple[str, ...]:
    if isinstance(events, (str, bytes)):
        raise ValueError("hook events must be a sequence of event names")
    selected = tuple(events)
    if not selected:
        raise ValueError("at least one hook event is required")
    if any(event not in _TERMINAL_WATCH_EVENTS for event in selected):
        raise ValueError("managed hook events must be Stop or SessionEnd")
    if len(set(selected)) != len(selected):
        raise ValueError("managed hook events must be unique")
    return tuple(sorted(selected))


def _absolute_logical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))

from __future__ import annotations

import re
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from .models import InstructionSurface, SurfaceRelationship, canonical_json

HOOK_STATUSES = frozenset(
    {"configured", "trust-unknown", "active-observed", "invalid", "ambiguous"}
)
_SAFE_METADATA_LABEL = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_RESERVED_CODEX_HOOK_KEYS = frozenset({"state", "managed_dir", "windows_managed_dir"})


def digest_metadata(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def structural_hook_relationships(
    surface: InstructionSurface,
    value: Any,
    *,
    status: str,
    source: str,
    scope: str,
    plugin_identity: str | None = None,
) -> tuple[SurfaceRelationship, ...]:
    """Project provider hook config into content-free structural metadata."""

    if status not in HOOK_STATUSES:
        raise ValueError(f"unsupported hook status: {status}")
    if not isinstance(value, Mapping):
        return (
            _invalid_relationship(
                surface,
                event="invalid",
                index=0,
                source=source,
                scope=scope,
                reason="configuration-not-an-object",
                plugin_identity=plugin_identity,
            ),
        )
    hooks = value.get("hooks")
    if hooks is None:
        return ()
    if not isinstance(hooks, Mapping):
        return (
            _invalid_relationship(
                surface,
                event="invalid",
                index=0,
                source=source,
                scope=scope,
                reason="hooks-not-an-object",
                plugin_identity=plugin_identity,
            ),
        )

    relationships: list[SurfaceRelationship] = []
    for raw_event in sorted(hooks, key=str):
        if raw_event in _RESERVED_CODEX_HOOK_KEYS:
            continue
        event = _metadata_label(raw_event, fallback="unknown")
        registrations = hooks[raw_event]
        if not isinstance(registrations, list):
            relationships.append(
                _invalid_relationship(
                    surface,
                    event=event,
                    index=0,
                    source=source,
                    scope=scope,
                    reason="registrations-not-an-array",
                    plugin_identity=plugin_identity,
                )
            )
            continue
        if not registrations:
            relationships.append(
                _invalid_relationship(
                    surface,
                    event=event,
                    index=0,
                    source=source,
                    scope=scope,
                    reason="registrations-empty",
                    plugin_identity=plugin_identity,
                )
            )
            continue
        for index, registration in enumerate(registrations):
            if not isinstance(registration, Mapping):
                relationships.append(
                    _invalid_relationship(
                        surface,
                        event=event,
                        index=index,
                        source=source,
                        scope=scope,
                        reason="registration-not-an-object",
                        plugin_identity=plugin_identity,
                    )
                )
                continue
            handlers = registration.get("hooks")
            if not isinstance(handlers, list) or not handlers:
                relationships.append(
                    _invalid_relationship(
                        surface,
                        event=event,
                        index=index,
                        source=source,
                        scope=scope,
                        reason="handlers-missing-or-empty",
                        plugin_identity=plugin_identity,
                    )
                )
                continue
            handler_types: list[str] = []
            timeouts: set[int] = set()
            async_values: set[bool] = set()
            valid = True
            for handler in handlers:
                if not isinstance(handler, Mapping):
                    valid = False
                    continue
                raw_type = handler.get("type")
                if not isinstance(raw_type, str) or not raw_type:
                    valid = False
                    handler_types.append("unknown")
                else:
                    handler_types.append(_metadata_label(raw_type, fallback="other"))
                timeout = handler.get("timeout")
                if (
                    isinstance(timeout, int)
                    and not isinstance(timeout, bool)
                    and timeout >= 0
                ):
                    timeouts.add(timeout)
                elif timeout is not None:
                    valid = False
                asynchronous = handler.get("async")
                if isinstance(asynchronous, bool):
                    async_values.add(asynchronous)
                elif asynchronous is not None:
                    valid = False
            location: dict[str, Any] = {
                "event": event,
                "registration_index": index,
                "handler_count": len(handlers),
                "handler_types": sorted(handler_types),
                "source": source,
                "scope": scope,
            }
            matcher = registration.get("matcher")
            if matcher is not None:
                location["matcher_sha256"] = digest_metadata(matcher)
            if timeouts:
                location["timeout_seconds"] = sorted(timeouts)
            if async_values:
                location["async_values"] = sorted(async_values)
            if plugin_identity is not None:
                location["plugin_sha256"] = digest_metadata(plugin_identity)
            relationships.append(
                SurfaceRelationship(
                    type=(
                        "session_start_hook"
                        if event == "SessionStart"
                        else "lifecycle_hook"
                    ),
                    from_surface_id=surface.id,
                    to_surface_id=None,
                    status=status if valid else "invalid",
                    location=location,
                )
            )
    return tuple(relationships)


def plugin_resolution_relationship(
    surface: InstructionSurface,
    plugin_identity: str,
    *,
    status: str,
    provider: str,
) -> SurfaceRelationship:
    if status not in {"invalid", "ambiguous"}:
        raise ValueError("plugin resolution status must be invalid or ambiguous")
    return SurfaceRelationship(
        type="plugin_hook_resolution",
        from_surface_id=surface.id,
        to_surface_id=None,
        status=status,
        location={
            "provider": provider,
            "plugin_sha256": digest_metadata(plugin_identity),
            "source": "plugin",
        },
    )


def _invalid_relationship(
    surface: InstructionSurface,
    *,
    event: str,
    index: int,
    source: str,
    scope: str,
    reason: str,
    plugin_identity: str | None,
) -> SurfaceRelationship:
    location: dict[str, Any] = {
        "event": event,
        "registration_index": index,
        "handler_count": 0,
        "handler_types": [],
        "source": source,
        "scope": scope,
        "reason": reason,
    }
    if plugin_identity is not None:
        location["plugin_sha256"] = digest_metadata(plugin_identity)
    return SurfaceRelationship(
        type="session_start_hook" if event == "SessionStart" else "lifecycle_hook",
        from_surface_id=surface.id,
        to_surface_id=None,
        status="invalid",
        location=location,
    )


def _metadata_label(value: Any, *, fallback: str) -> str:
    return (
        value
        if isinstance(value, str) and _SAFE_METADATA_LABEL.fullmatch(value)
        else fallback
    )

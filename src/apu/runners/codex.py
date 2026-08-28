from __future__ import annotations

from typing import Any, Iterable, Mapping

from .base import NormalizedEvent, RunnerCapabilities, parse_jsonl

CODEX_CAPABILITIES = RunnerCapabilities(
    provider="codex",
    cli_name="codex",
    observable_events=frozenset({"tool_use", "delegation", "review"}),
    invocation=("codex", "exec", "--json", "-"),
)

_TOOL_ITEMS = frozenset(
    {
        "command_execution",
        "dynamic_tool_call",
        "image_generation",
        "image_view",
        "mcp_tool_call",
        "web_search",
    }
)
_SAFE_STATUSES = frozenset({"in_progress", "completed", "failed", "declined"})


def _safe_metadata(
    item: Mapping[str, Any],
    *,
    tool: str,
    phase: str,
) -> dict[str, str]:
    metadata = {"tool": tool, "phase": phase}
    status = item.get("status")
    if status in _SAFE_STATUSES:
        metadata["status"] = status
    return metadata


def parse_codex_jsonl(
    content: str | Iterable[str],
) -> tuple[NormalizedEvent, ...]:
    events: list[NormalizedEvent] = []
    for record in parse_jsonl(content):
        envelope_type = record.get("type")
        if envelope_type not in {"item.started", "item.completed"}:
            continue
        item = record.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if not isinstance(item_type, str):
            continue
        phase = envelope_type.removeprefix("item.")
        provider_event = f"{envelope_type}.{item_type}"

        if item_type in _TOOL_ITEMS:
            events.append(
                NormalizedEvent(
                    type="tool_use",
                    provider_event=provider_event,
                    metadata=_safe_metadata(
                        item,
                        tool=item_type,
                        phase=phase,
                    ),
                )
            )
            continue

        if item_type == "collab_agent_tool_call":
            tool = item.get("tool")
            safe_tool = (
                tool
                if tool
                in {
                    "spawn_agent",
                    "send_input",
                    "resume_agent",
                    "wait",
                    "close_agent",
                }
                else "collaboration"
            )
            metadata = _safe_metadata(
                item,
                tool=safe_tool,
                phase=phase,
            )
            events.append(
                NormalizedEvent(
                    type="tool_use",
                    provider_event=provider_event,
                    metadata=metadata,
                )
            )
            if tool == "spawn_agent":
                events.append(
                    NormalizedEvent(
                        type="delegation",
                        provider_event=provider_event,
                        metadata={"phase": phase},
                    )
                )
            continue

        if item_type in {"entered_review_mode", "exited_review_mode"}:
            events.append(
                NormalizedEvent(
                    type="review",
                    provider_event=provider_event,
                    metadata={"phase": phase},
                )
            )

    return tuple(events)

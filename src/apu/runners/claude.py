from __future__ import annotations

from typing import Any, Iterable, Mapping

from .base import NormalizedEvent, RunnerCapabilities, parse_jsonl

CLAUDE_CAPABILITIES = RunnerCapabilities(
    provider="claude",
    cli_name="claude",
    observable_events=frozenset({"tool_use", "delegation", "review"}),
    invocation=(
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
    ),
)

_SAFE_TOOL_NAMES = {
    "bash": "shell",
    "edit": "edit",
    "glob": "glob",
    "grep": "grep",
    "read": "read",
    "write": "write",
    "webfetch": "web_fetch",
    "websearch": "web_search",
}
_DELEGATION_TOOLS = frozenset({"agent", "task"})


def _tool_blocks(record: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    if record.get("type") == "assistant":
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    yield block

    if record.get("type") == "stream_event":
        event = record.get("event")
        if not isinstance(event, dict):
            return
        if event.get("type") != "content_block_start":
            return
        block = event.get("content_block")
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block


def _is_review_delegation(block: Mapping[str, Any]) -> bool:
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return False
    values = (
        tool_input.get("subagent_type"),
        tool_input.get("description"),
    )
    return any(
        isinstance(value, str) and "review" in value.casefold() for value in values
    )


def parse_claude_jsonl(
    content: str | Iterable[str],
) -> tuple[NormalizedEvent, ...]:
    events: list[NormalizedEvent] = []
    for record in parse_jsonl(content):
        for block in _tool_blocks(record):
            name = block.get("name")
            normalized_name = name.casefold() if isinstance(name, str) else ""
            is_delegation = normalized_name in _DELEGATION_TOOLS
            safe_tool = (
                "agent"
                if is_delegation
                else _SAFE_TOOL_NAMES.get(normalized_name, "other")
            )
            events.append(
                NormalizedEvent(
                    type="tool_use",
                    provider_event="assistant.tool_use",
                    metadata={"tool": safe_tool},
                )
            )
            if is_delegation:
                events.append(
                    NormalizedEvent(
                        type="delegation",
                        provider_event="assistant.tool_use",
                        metadata={"tool": "agent"},
                    )
                )
                if _is_review_delegation(block):
                    events.append(
                        NormalizedEvent(
                            type="review",
                            provider_event="assistant.tool_use",
                            metadata={"kind": "code_review"},
                        )
                    )
    return tuple(events)

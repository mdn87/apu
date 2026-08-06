from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Iterable


def _jsonl_files(paths: Iterable[Path]) -> tuple[Path, ...]:
    files: set[Path] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_file() and path.suffix == ".jsonl":
            files.add(path.resolve())
        elif path.is_dir():
            files.update(item.resolve() for item in path.rglob("*.jsonl"))
    return tuple(sorted(files))


def _token_total(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    total = value.get("total_tokens")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        return int(total)
    return int(
        sum(
            item
            for key, item in value.items()
            if key in {"input_tokens", "output_tokens", "reasoning_tokens"}
            and isinstance(item, (int, float))
            and not isinstance(item, bool)
        )
    )


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _session_index(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for path in _jsonl_files(paths):
        entry: dict[str, Any] = {
            "id": None,
            "parent": None,
            "tools": Counter(),
            "tokens": 0,
            "timestamps": [],
        }
        try:
            stream = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                parsed = _timestamp(record.get("timestamp"))
                if parsed is not None:
                    entry["timestamps"].append(parsed)
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "session_meta" and entry["id"] is None:
                    entry["id"] = payload.get("id") or payload.get("session_id")
                    entry["parent"] = payload.get("parent_thread_id")
                if (
                    record.get("type") == "response_item"
                    and payload.get("type") in {"function_call", "custom_tool_call"}
                    and isinstance(payload.get("name"), str)
                ):
                    entry["tools"][payload["name"]] += 1
                if (
                    record.get("type") == "event_msg"
                    and payload.get("type") == "token_count"
                ):
                    usage = payload.get("info", {}).get("total_token_usage", {})
                    entry["tokens"] = max(entry["tokens"], _token_total(usage))
        if isinstance(entry["id"], str) and entry["id"]:
            index[entry["id"]] = entry
    return index


def summarize_sessions(
    paths: Iterable[Path],
    *,
    root_session_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate orchestration metadata without retaining message or tool input."""

    index = _session_index(paths)
    if root_session_id is not None and root_session_id not in index:
        raise ValueError(f"root session not found: {root_session_id}")
    if root_session_id is None:
        selected_ids = set(index)
    else:
        selected_ids = {root_session_id}
        changed = True
        while changed:
            changed = False
            for session_id, entry in index.items():
                if entry["parent"] in selected_ids and session_id not in selected_ids:
                    selected_ids.add(session_id)
                    changed = True

    tools: Counter[str] = Counter()
    timestamps: list[datetime] = []
    tokens = 0
    for session_id in selected_ids:
        entry = index[session_id]
        tools.update(entry["tools"])
        timestamps.extend(entry["timestamps"])
        tokens += int(entry["tokens"])
    elapsed_seconds = 0
    if timestamps:
        elapsed_seconds = max(
            0, int((max(timestamps) - min(timestamps)).total_seconds())
        )
    return {
        "root_session_id": root_session_id,
        "sessions": len(selected_ids),
        "descendants": (
            max(0, len(selected_ids) - 1) if root_session_id is not None else None
        ),
        "elapsed_seconds": elapsed_seconds,
        "tool_calls": dict(sorted(tools.items())),
        "total_token_usage": tokens,
        "privacy": "Message, prompt, tool input, and environment content is not emitted.",
    }

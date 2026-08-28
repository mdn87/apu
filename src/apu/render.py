from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import canonical_json

BEGIN_MARKER = "<!-- apu:begin policy version=0.1.0 -->"
END_MARKER = "<!-- apu:end policy -->"


def render_bytes(
    *,
    action: str,
    strategy: str,
    source: bytes,
    current: bytes | None,
    target: Path,
) -> bytes:
    """Render one reviewed operation without discarding unrelated content."""

    if strategy in {"full_file", "sidecar"}:
        rendered = source
    elif strategy == "managed_section" and action == "configure":
        rendered = _merge_json_objects(current, source)
    elif strategy == "managed_section":
        rendered = _merge_markdown_section(current or b"", source)
    else:
        raise ValueError(f"unsupported mutation strategy: {strategy}")
    _validate_rendered(target, action, rendered)
    return rendered


def _merge_json_objects(current: bytes | None, source: bytes) -> bytes:
    proposed = _json_object(source, "configured metadata")
    if current is None:
        return source
    existing = _json_object(current, "existing metadata")
    merged = _deep_merge(existing, proposed)
    return (canonical_json(merged) + "\n").encode("utf-8")


def _json_object(content: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{description} must be valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be a JSON object")
    return value


def _deep_merge(existing: dict[str, Any], proposed: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in proposed.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def _merge_markdown_section(current: bytes, source: bytes) -> bytes:
    try:
        text = current.decode("utf-8")
        body = source.decode("utf-8").strip()
    except UnicodeError as error:
        raise ValueError("managed sections require UTF-8 text") from error
    if text.count(BEGIN_MARKER) != text.count(END_MARKER):
        raise ValueError("managed-section markers are unbalanced")
    if text.count(BEGIN_MARKER) > 1:
        raise ValueError("managed-section markers must be unique")
    section = f"{BEGIN_MARKER}\n{body}\n{END_MARKER}"
    if BEGIN_MARKER in text:
        start = text.index(BEGIN_MARKER)
        end = text.index(END_MARKER, start) + len(END_MARKER)
        rendered = text[:start] + section + text[end:]
    else:
        separator = "" if not text or text.endswith("\n") else "\n"
        rendered = text + separator + section + "\n"
    return rendered.encode("utf-8")


def _validate_rendered(target: Path, action: str, rendered: bytes) -> None:
    if action == "configure" or target.suffix.casefold() == ".json":
        _json_object(rendered, "rendered metadata")

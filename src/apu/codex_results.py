"""Interpret command status without mining command output for nested evidence."""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

_EXIT_CODE = re.compile(r"exit code:\s*(-?\d+)", re.IGNORECASE)
_FAILURE = re.compile(r"(?:script failed|command failed)", re.IGNORECASE)


def _envelope_status(value: Any) -> tuple[str, int | None]:
    if not isinstance(value, Mapping):
        return "unknown", None
    duration = value.get("wall_time_seconds")
    if not (
        isinstance(value.get("chunk_id"), str)
        and bool(value["chunk_id"])
        and isinstance(value.get("output"), str)
        and type(duration) in (int, float)
        and math.isfinite(duration)
        and duration >= 0
    ):
        return "unknown", None
    code = value.get("exit_code")
    if type(code) is int:
        return ("failed" if code else "completed"), code
    if code is None and type(value.get("session_id")) is int and value["session_id"] > 0:
        return "running", None
    return "unknown", None


def codex_result_status(output: Any) -> tuple[str, int | None]:
    """Return safe aggregate status and an explicit exit code, if available.

    A result is a tool response, not necessarily a finished command. Nonzero
    siblings dominate; otherwise opaque siblings prevent a success claim.
    Only direct typed text envelopes are parsed, never their output bodies.
    Legacy non-JSON strings retain the historical prose interpretation.
    """
    if isinstance(output, str):
        try:
            value = json.loads(output)
        except ValueError:
            match = _EXIT_CODE.search(output)
            code = int(match.group(1)) if match else None
            failed = (code is not None and code != 0) or bool(_FAILURE.search(output))
            return ("failed" if failed else "completed"), code
        return _envelope_status(value)
    if not isinstance(output, list):
        return "unknown", None
    statuses = []
    for block in output:
        if isinstance(block, Mapping) and block.get("type") in {"input_image", "image"}:
            continue
        if not isinstance(block, Mapping) or block.get("type") not in {"input_text", "output_text"}:
            statuses.append(("unknown", None))
            continue
        try:
            value = json.loads(block.get("text"))
        except (ValueError, TypeError):
            statuses.append(("unknown", None))
            continue
        statuses.append(_envelope_status(value))
    for status, code in statuses:
        if status == "failed":
            return status, code
    if not statuses or any(status == "unknown" for status, _ in statuses):
        return "unknown", None
    if any(status == "running" for status, _ in statuses):
        return "running", None
    return "completed", 0

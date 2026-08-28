from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from apu.evidence import read_evidence
from apu.hooks import (
    MAX_HOOK_INPUT_BYTES,
    HookInputError,
    hook_bridge_main,
    ingest_hook_stream,
)


def _payload(**updates: object) -> bytes:
    value: dict[str, object] = {
        "session_id": "hook-session",
        "cwd": "/tmp/repository",
        "hook_event_name": "Stop",
        "sequence": 4,
    }
    value.update(updates)
    return json.dumps(value).encode("utf-8")


def test_hook_stream_derives_event_name_and_rejects_a_mismatch(tmp_path: Path) -> None:
    state = tmp_path / "state"

    accepted = ingest_hook_stream(state, "codex", io.BytesIO(_payload()))

    assert accepted["accepted"] is True
    assert accepted["event"] == "Stop"
    assert [
        event["event_type"] for event in read_evidence(state, "codex", "hook-session")
    ] == ["turn.completed"]

    with pytest.raises(HookInputError, match="does not match"):
        ingest_hook_stream(
            state,
            "codex",
            io.BytesIO(_payload()),
            expected_event="PreToolUse",
        )

    assert len(read_evidence(state, "codex", "hook-session")) == 1


def test_hook_stream_rejects_oversized_input_before_json_decode(tmp_path: Path) -> None:
    oversized = io.BytesIO(b"{" + b"x" * MAX_HOOK_INPUT_BYTES + b"}")

    with pytest.raises(HookInputError, match="byte limit"):
        ingest_hook_stream(tmp_path / "state", "codex", oversized)


def test_provider_bridge_is_silent_and_fail_open(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state"

    assert (
        hook_bridge_main(
            ["--provider", "codex", "--event", "PreToolUse"],
            stdin=io.BytesIO(_payload()),
            state_home=state,
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert not (state / "behavior" / "evidence").exists()


def test_passive_stop_watcher_only_observes_repository_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    repository = tmp_path / "repo"
    repository.mkdir()
    payload = _payload(cwd=str(repository), hook_event_name="SessionEnd")

    result = ingest_hook_stream(
        state,
        "claude-code",
        io.BytesIO(payload),
        passive_watch=True,
    )

    assert result["passive_watch"] is True
    assert result["state_appended"] is True
    assert [
        event["event_type"]
        for event in read_evidence(state, "claude-code", "hook-session")
    ] == ["session.ended", "state.observed"]


def test_passive_watcher_rejects_non_terminal_events(tmp_path: Path) -> None:
    with pytest.raises(HookInputError, match="Stop or SessionEnd"):
        ingest_hook_stream(
            tmp_path / "state",
            "codex",
            io.BytesIO(_payload(hook_event_name="PreToolUse")),
            passive_watch=True,
        )

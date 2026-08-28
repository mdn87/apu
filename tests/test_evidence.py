from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from apu.cli import main
from apu.evidence import (
    append_evidence_events,
    ingest_codex_trace,
    ingest_hook_event,
    observe_repository_state,
    read_evidence,
    read_evidence_version,
    reconcile_evidence,
    validate_evidence_event,
    verify_evidence_source,
)


def _codex_trace(path: Path, cwd: Path) -> Path:
    records = [
        {
            "timestamp": "2026-08-14T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "evidence-session",
                "cwd": str(cwd),
                "base_instructions": "private base instructions",
            },
        },
        {
            "timestamp": "2026-08-14T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
        {
            "timestamp": "2026-08-14T10:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "call-1",
                "arguments": '{"command":"pytest --token private-value"}',
            },
        },
        {
            "timestamp": "2026-08-14T10:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "Exit code: 1\nprivate failure output",
            },
        },
        {
            "timestamp": "2026-08-14T10:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "call-2",
                "arguments": '{"command":"pytest --token private-value"}',
            },
        },
        {
            "timestamp": "2026-08-14T10:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-2",
                "output": "Exit code: 1\nprivate failure output",
            },
        },
    ]
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def test_codex_ingestion_is_content_minimized_correlated_and_replayable(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    trace = _codex_trace(tmp_path / "sessions" / "rollout.jsonl", cwd)

    path, events, boundary = ingest_codex_trace(state, trace)

    assert path is not None and path.is_file()
    assert {event["schema_version"] for event in events} == {2}
    assert {event["attribution"]["selector_mode"] for event in events} == {
        "exact_trace_path"
    }
    assert boundary["event_count"] == 6
    assert boundary["appended_count"] == 6
    reconciliation = reconcile_evidence(events)
    assert reconciliation["paired_tool_calls"] == 2
    assert reconciliation["reason_codes"] == ["repeated-identical-tool-failure"]
    encoded = path.read_text(encoding="utf-8")
    assert "private-value" not in encoded
    assert "private failure output" not in encoded
    assert "private base instructions" not in encoded

    with trace.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "timestamp": "2026-08-14T10:00:06Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete"},
                }
            )
            + "\n"
        )
    assert {verify_evidence_source(event)["status"] for event in events} == {"verified"}

    _, expanded, expanded_boundary = ingest_codex_trace(state, trace)
    assert expanded_boundary["appended_count"] == 1
    assert len(read_evidence(state, "codex", "evidence-session")) == 7
    assert expanded[-1]["event_type"] == "task.completed"


def test_source_verification_detects_changed_prefix(tmp_path: Path) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    trace = _codex_trace(tmp_path / "sessions" / "rollout.jsonl", cwd)
    _, events, _ = ingest_codex_trace(state, trace)

    content = trace.read_text(encoding="utf-8")
    trace.write_text(content.replace("evidence-session", "different-session", 1))

    result = verify_evidence_source(events[0])
    assert result["status"] == "contradicted"
    assert result["reason_codes"] == ["source-prefix-changed"]


def test_hook_ingestion_projects_only_safe_metadata(tmp_path: Path) -> None:
    state = tmp_path / "state"
    transcript = tmp_path / "private-transcript.jsonl"
    payload = {
        "session_id": "claude-session",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "tool_use_id": "toolu-private-id",
        "tool_name": "Bash",
        "tool_input": {"command": "echo secret-hook-input"},
        "tool_response": "secret-hook-output",
        "duration_ms": 42,
        "unrecognized_private_field": "must not persist",
    }

    path, event, appended = ingest_hook_event(
        state, "claude-code", "PostToolUse", payload
    )

    assert appended is True
    assert event["event_type"] == "tool.completed"
    assert event["correlation_sha256"]
    assert event["observation"]["input_sha256"]
    assert event["observation"]["result_sha256"]
    assert event["observation"]["command_class"] == "shell"
    encoded = path.read_text(encoding="utf-8") if path else ""
    assert "secret-hook-input" not in encoded
    assert "secret-hook-output" not in encoded
    assert "toolu-private-id" not in encoded
    assert "must not persist" not in encoded

    _, duplicate, duplicate_appended = ingest_hook_event(
        state, "claude-code", "PostToolUse", payload
    )
    assert duplicate["event_id"] == event["event_id"]
    assert duplicate_appended is False

    invalid = dict(event)
    invalid["raw_payload"] = payload
    with pytest.raises(ValueError, match="fields do not match"):
        validate_evidence_event(invalid)


def test_v2_reader_accepts_legacy_v1_and_rejects_partial_v2(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    trace = _codex_trace(tmp_path / "sessions" / "rollout.jsonl", cwd)
    v2_path, events, _ = ingest_codex_trace(state, trace)
    legacy = dict(events[0])
    legacy["schema_version"] = 1
    legacy["event_id"] = "legacy-" + legacy["event_id"]
    legacy["sequence"] += 1_000
    legacy.pop("attribution")

    validate_evidence_event(legacy)
    legacy_path, appended = append_evidence_events(state, [legacy])
    assert appended == (legacy,)
    assert legacy_path != v2_path
    assert legacy_path is not None and "\"schema_version\":2" not in legacy_path.read_text(
        encoding="utf-8"
    )
    assert v2_path is not None and "\"schema_version\":1" not in v2_path.read_text(
        encoding="utf-8"
    )
    stored = read_evidence(state, "codex", "evidence-session")
    assert {event["schema_version"] for event in stored} == {1, 2}

    partial_v2 = dict(legacy)
    partial_v2["schema_version"] = 2
    with pytest.raises(ValueError, match=r"missing=\['attribution'\]"):
        validate_evidence_event(partial_v2)

    conflict_state = tmp_path / "conflict-state"
    append_evidence_events(conflict_state, [events[0]])
    conflicting_legacy = dict(events[0])
    conflicting_legacy["schema_version"] = 1
    conflicting_legacy["event_id"] = "conflict-legacy"
    conflicting_legacy.pop("attribution")
    conflicting_legacy["observation"] = dict(conflicting_legacy["observation"])
    conflicting_legacy["observation"]["status"] = "completed"
    append_evidence_events(conflict_state, [conflicting_legacy])
    with pytest.raises(ValueError, match="cross-version evidence projections disagree"):
        read_evidence(conflict_state, "codex", "evidence-session")

    rollback_state = tmp_path / "rollback-state"
    _, rollback_events, rollback_boundary = ingest_codex_trace(
        rollback_state,
        trace,
        schema_version=1,
    )
    assert rollback_boundary["schema_version"] == 1
    assert {event["schema_version"] for event in rollback_events} == {1}
    assert all("attribution" not in event for event in rollback_events)


def test_v1_to_v2_writer_transition_keeps_routes_complete_and_reads_logically(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    trace = _codex_trace(tmp_path / "sessions" / "rollout.jsonl", cwd)

    _, legacy_events, legacy_boundary = ingest_codex_trace(
        state,
        trace,
        schema_version=1,
    )
    _, replayed, replay_boundary = ingest_codex_trace(
        state,
        trace,
        schema_version=2,
    )
    assert len(legacy_events) == len(replayed) == 6
    assert legacy_boundary["appended_count"] == 6
    assert replay_boundary["appended_count"] == 6
    assert len(
        read_evidence_version(
            state,
            "codex",
            "evidence-session",
            schema_version=1,
        )
    ) == 6
    assert len(
        read_evidence_version(
            state,
            "codex",
            "evidence-session",
            schema_version=2,
        )
    ) == 6

    with trace.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "timestamp": "2026-08-14T10:00:06Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete"},
                }
            )
            + "\n"
        )
    _, _, advanced_boundary = ingest_codex_trace(
        state,
        trace,
        schema_version=2,
    )

    stored = read_evidence(state, "codex", "evidence-session")
    assert advanced_boundary["appended_count"] == 1
    assert len(stored) == 7
    assert {event["schema_version"] for event in stored} == {2}


def test_repository_observation_hashes_changed_paths(tmp_path: Path) -> None:
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "apu@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "APU Test"], cwd=repo, check=True)
    tracked = repo / "private-name.txt"
    tracked.write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "private-name.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "test: initial"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    tracked.write_text("changed\n", encoding="utf-8")

    path, event, appended = observe_repository_state(
        state,
        provider="claude-code",
        session_id="state-session",
        cwd=repo,
        observed_at="2026-08-14T10:10:00Z",
    )

    assert appended is True
    assert event["state"]["repository_available"] is True
    assert event["state"]["head_sha"]
    assert event["state"]["tree_sha"]
    assert event["state"]["dirty"] is True
    assert len(event["state"]["changed_path_sha256"]) == 1
    assert "private-name.txt" not in (path.read_text(encoding="utf-8") if path else "")


def test_evidence_cli_ingests_hook_json(tmp_path: Path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    payload_path = tmp_path / "hook.json"
    payload_path.write_text(
        json.dumps(
            {
                "session_id": "cli-hook-session",
                "cwd": str(tmp_path),
                "tool_use_id": "tool-1",
                "tool_name": "Bash",
                "tool_input": {"command": "pytest"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APU_HOME", str(state))

    assert (
        main(
            [
                "evidence",
                "ingest-hook",
                "--provider",
                "claude-code",
                "--event",
                "PreToolUse",
                "--input",
                str(payload_path),
                "--schema-version",
                "1",
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1
    assert result["event_type"] == "tool.requested"
    assert result["appended"] is True


def test_evidence_cli_returns_nonzero_for_no_attribution(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    state = tmp_path / "state"
    other = tmp_path / "other"
    requested = tmp_path / "requested"
    other.mkdir()
    requested.mkdir()
    trace_root = tmp_path / "sessions"
    _codex_trace(trace_root / "rollout.jsonl", other)
    monkeypatch.setenv("APU_HOME", str(state))

    assert (
        main(
            [
                "evidence",
                "ingest-codex",
                "--trace-root",
                str(trace_root),
                "--cwd",
                str(requested),
                "--json",
            ]
        )
        == 2
    )
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "no_attribution"
    assert result["reason_code"] == "no_exact_cwd_candidate"

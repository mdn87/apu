from __future__ import annotations

import json
from pathlib import Path

import pytest

from apu.behavior_watch import (
    WATCHER_ID,
    configure_watcher,
    diagnose_incident,
    intervene,
    mark_incident,
    record_intervention_result,
    select_codex_session,
    watcher_status,
)


def _write_trace(
    root: Path,
    cwd: Path,
    *,
    session_id: str = "session-123",
    active: bool = True,
    non_interactive: bool = False,
) -> Path:
    path = root / "2026" / "08" / "12" / f"rollout-{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-08-12T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "session_id": session_id,
                "cwd": str(cwd),
                "source": "exec" if non_interactive else "vscode",
                "originator": "Codex CLI" if non_interactive else "Codex Desktop",
                "cli_version": "0.147.0",
                "base_instructions": "private base instructions",
                "dynamic_tools": [{"name": "shell_command", "private": "value"}],
            },
        },
        {
            "timestamp": "2026-08-12T10:00:01Z",
            "type": "turn_context",
            "payload": {
                "collaboration_mode": {"kind": "default"},
                "sandbox_policy": {"type": "workspace-write"},
                "permission_profile": {"type": "disabled"},
                "model": "gpt-test",
            },
        },
        {
            "timestamp": "2026-08-12T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "task_started",
                "turn_id": "turn-1",
            },
        },
        {
            "timestamp": "2026-08-12T10:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "secret user prompt must never persist",
            },
        },
        {
            "timestamp": "2026-08-12T10:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "arguments": '{"command":"private command"}',
            },
        },
        {
            "timestamp": "2026-08-12T10:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": "Exit code: 1\nprivate command output",
            },
        },
        {
            "timestamp": "2026-08-12T10:00:06Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "Would you like me to choose which file name?",
            },
        },
    ]
    if not active:
        records.append(
            {
                "timestamp": "2026-08-12T10:00:07Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1"},
            }
        )
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def test_selects_active_session_and_marks_content_free_evidence(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text(
        "Ask for approval before continuing with any file choice.\n",
        encoding="utf-8",
    )
    traces = tmp_path / "sessions"
    completed = _write_trace(
        traces,
        cwd,
        session_id="completed-session",
        active=False,
    )
    active = _write_trace(traces, cwd, session_id="active-session", active=True)
    completed.touch()
    active.touch()

    selected = select_codex_session(trace_root=traces, cwd=cwd)
    assert selected.session_id == "active-session"
    assert selected.active is True

    path, incident = mark_incident(
        tmp_path / "state",
        "asked me to approve a reversible filename choice",
        trace_root=traces,
        cwd=cwd,
        recorded_at="2026-08-12T10:01:00Z",
    )

    encoded = path.read_text(encoding="utf-8")
    assert incident["session"]["session_id"] == "active-session"
    assert incident["claim"]["verification_status"] == "asserted"
    assert "reversible-choice-escalation" in incident["observed_signals"]
    assert incident["nearby_evidence"]["tool_calls"] == {"shell_command": 1}
    assert incident["runtime_context"]["base_instructions_sha256"]
    assert incident["evidence_plane"]["provider"] == "codex"
    assert incident["evidence_plane"]["verification_status"] == "observed"
    assert incident["evidence_plane"]["event_refs"]
    assert incident["evidence_plane"]["source_boundary"]["snapshot_sha256"]
    evidence_path = Path(incident["evidence_plane"]["evidence_path"])
    evidence_encoded = evidence_path.read_text(encoding="utf-8")
    assert "secret user prompt" not in encoded
    assert "private command" not in encoded
    assert "private base instructions" not in encoded
    assert "private command output" not in encoded
    assert "secret user prompt" not in evidence_encoded
    assert "private command" not in evidence_encoded
    assert "private command output" not in evidence_encoded


def test_diagnosis_ranks_active_instruction_without_copying_content(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    agents = cwd / "AGENTS.md"
    agents.write_text(
        "Ask for approval before continuing with any reversible choice.\n",
        encoding="utf-8",
    )
    traces = tmp_path / "sessions"
    _write_trace(traces, cwd)
    state = tmp_path / "state"
    _, incident = mark_incident(
        state,
        "asked me to approve a reversible filename choice",
        trace_root=traces,
        cwd=cwd,
    )

    path, diagnosis = diagnose_incident(
        state,
        incident_id=incident["incident_id"],
        diagnosed_at="2026-08-12T10:02:00Z",
    )

    assert diagnosis["status"] == "likely-autonomy-loss"
    assert diagnosis["likely_sources"][0]["path"] == str(agents)
    assert diagnosis["likely_sources"][0]["line_numbers"] == [1]
    assert diagnosis["recommended_intervention"]["durable_policy_mutation"] is False
    assert diagnosis["evaluation"]["verification_status"] == "observed"
    assert "Ask for approval" not in path.read_text(encoding="utf-8")


def test_legitimate_barrier_blocks_intervention(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "sessions"
    _write_trace(traces, cwd)
    state = tmp_path / "state"
    _, incident = mark_incident(
        state,
        "stopped because production credentials are unavailable",
        trace_root=traces,
        cwd=cwd,
    )
    _, diagnosis = diagnose_incident(state, incident_id=incident["incident_id"])

    assert diagnosis["status"] == "possible-legitimate-barrier"
    with pytest.raises(ValueError, match="legitimate barrier"):
        intervene(state, diagnosis_id=diagnosis["diagnosis_id"], dry_run=True)


def test_noninteractive_intervention_resumes_and_records_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "sessions"
    _write_trace(traces, cwd, non_interactive=True)
    state = tmp_path / "state"
    _, incident = mark_incident(
        state,
        "stopped before completing the requested task",
        trace_root=traces,
        cwd=cwd,
    )
    _, diagnosis = diagnose_incident(state, incident_id=incident["incident_id"])
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps({"type": "turn.completed"}) + "\n",
            },
        )()

    monkeypatch.setattr("apu.behavior_watch.subprocess.run", fake_run)
    path, result = intervene(
        state,
        diagnosis_id=diagnosis["diagnosis_id"],
        executable="codex",
        created_at="2026-08-12T10:03:00Z",
    )

    assert result["status"] == "completed"
    assert result["executed"] is True
    assert seen["command"][:3] == ["codex", "exec", "resume"]
    assert seen["kwargs"]["cwd"] == cwd
    assert result["durable_policy_mutation"] is False
    assert result["verification_status"] == "observed"
    assert path.is_file()


def test_desktop_intervention_returns_continuation_without_launching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "sessions"
    _write_trace(traces, cwd)
    state = tmp_path / "state"
    _, incident = mark_incident(
        state,
        "stopped before completing the requested task",
        trace_root=traces,
        cwd=cwd,
    )
    _, diagnosis = diagnose_incident(state, incident_id=incident["incident_id"])
    monkeypatch.setattr(
        "apu.behavior_watch.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("desktop continuation must not launch"),
    )

    _, result = intervene(
        state,
        diagnosis_id=diagnosis["diagnosis_id"],
        executable="codex",
    )

    assert result["status"] == "continuation-ready"
    assert result["executed"] is False
    assert result["command"][:2] == ["codex", "resume"]

    result_path, attestation = record_intervention_result(
        state,
        "completed",
        intervention_id=result["intervention_id"],
        recorded_at="2026-08-12T10:03:30Z",
    )
    assert attestation["result"] == "completed"
    assert attestation["verification_status"] == "asserted"
    assert attestation["intervention_id"] == result["intervention_id"]
    assert result_path.is_file()


def test_watcher_configuration_defaults_enabled_and_can_fail_closed(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    assert watcher_status(state) == {
        "watcher": WATCHER_ID,
        "enabled": True,
        "updated_at": None,
        "provider": "codex",
        "background_service": False,
    }
    status = configure_watcher(
        state,
        enabled=False,
        updated_at="2026-08-12T10:04:00Z",
    )
    assert status["enabled"] is False

    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "sessions"
    _write_trace(traces, cwd)
    with pytest.raises(ValueError, match="disabled"):
        mark_incident(
            state,
            "stopped before completing the requested task",
            trace_root=traces,
            cwd=cwd,
        )


def test_incident_description_rejects_credentials_before_state_creation(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="credential-shaped"):
        mark_incident(
            tmp_path / "state",
            "api_key=sk-proj-abcdefghijklmnop",
            trace_root=tmp_path / "missing",
        )
    assert not (tmp_path / "state").exists()

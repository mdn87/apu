from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apu.behavior_watch import (
    WATCHER_ID,
    NoAttribution,
    SelectedSession,
    _peek_session,
    configure_watcher,
    diagnose_incident,
    intervene,
    mark_incident,
    normalized_cwd_key,
    record_intervention_result,
    select_codex_session,
    watcher_status,
)

_NOW = datetime.now(UTC).replace(microsecond=0)
_TRACE_START = _NOW - timedelta(minutes=1)


def _trace_timestamp(offset: int) -> str:
    return (_TRACE_START + timedelta(seconds=offset)).isoformat().replace(
        "+00:00", "Z"
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
            "timestamp": _trace_timestamp(0),
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
            "timestamp": _trace_timestamp(1),
            "type": "turn_context",
            "payload": {
                "collaboration_mode": {"kind": "default"},
                "sandbox_policy": {"type": "workspace-write"},
                "permission_profile": {"type": "disabled"},
                "model": "gpt-test",
            },
        },
        {
            "timestamp": _trace_timestamp(2),
            "type": "event_msg",
            "payload": {
                "type": "task_started",
                "turn_id": "turn-1",
            },
        },
        {
            "timestamp": _trace_timestamp(3),
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "secret user prompt must never persist",
            },
        },
        {
            "timestamp": _trace_timestamp(4),
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "arguments": '{"command":"private command"}',
            },
        },
        {
            "timestamp": _trace_timestamp(5),
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": "Exit code: 1\nprivate command output",
            },
        },
        {
            "timestamp": _trace_timestamp(6),
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
                "timestamp": _trace_timestamp(7),
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

    selected = select_codex_session(trace_root=traces, cwd=cwd, now=_NOW)
    assert isinstance(selected, SelectedSession)
    assert selected.session.session_id == "active-session"
    assert selected.session.active is True
    assert selected.provenance.selector_mode == "unique_active_exact_cwd"
    assert selected.provenance.candidate_count == 1

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
    initial = watcher_status(state)
    assert initial["watcher"] == WATCHER_ID
    assert initial["enabled"] is True
    assert initial["updated_at"] is None
    assert initial["provider"] == "codex"
    assert initial["background_service"] is False
    assert initial["selector_mode"] == "strict"
    assert initial["last_successful_attribution"] is None
    assert initial["ambiguity_count"] == 0
    assert initial["service_heartbeat"] is None
    assert initial["package_version"] == "0.9.0"
    assert initial["build_revision"].startswith("sha256:")
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


def test_explicit_session_rejects_cwd_mismatch(tmp_path: Path) -> None:
    expected = tmp_path / "expected"
    other = tmp_path / "other"
    expected.mkdir()
    other.mkdir()
    traces = tmp_path / "sessions"
    _write_trace(traces, other, session_id="explicit-session")

    result = select_codex_session(
        trace_root=traces,
        session_id="explicit-session",
        cwd=expected,
        now=_NOW,
    )

    assert isinstance(result, NoAttribution)
    assert result.reason_code == "cwd_mismatch"
    assert result.provenance.candidate_count == 0


def test_cross_project_recency_never_becomes_a_candidate(tmp_path: Path) -> None:
    expected = tmp_path / "expected"
    other = tmp_path / "other"
    expected.mkdir()
    other.mkdir()
    traces = tmp_path / "sessions"
    _write_trace(traces, other, session_id="recent-other-project")

    result = select_codex_session(trace_root=traces, cwd=expected, now=_NOW)

    assert isinstance(result, NoAttribution)
    assert result.reason_code == "no_exact_cwd_candidate"


def test_automatic_selection_rejects_ambiguous_active_sessions(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "sessions"
    _write_trace(traces, cwd, session_id="active-one")
    _write_trace(traces, cwd, session_id="active-two")
    state = tmp_path / "state"

    result = select_codex_session(
        trace_root=traces,
        cwd=cwd,
        state_home=state,
        now=_NOW,
    )

    assert isinstance(result, NoAttribution)
    assert result.reason_code == "ambiguous_active_candidates"
    assert result.provenance.candidate_count == 2
    health = watcher_status(state)
    assert health["ambiguity_count"] == 1
    assert health["service_heartbeat"] == _NOW.isoformat().replace("+00:00", "Z")
    encoded = (state / "behavior" / "selector-health.json").read_text(
        encoding="utf-8"
    )
    assert "active-one" not in encoded
    assert str(cwd) not in encoded


def test_stale_and_unparsable_traces_are_not_candidates(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "sessions"
    _write_trace(traces, cwd, session_id="stale")

    stale = select_codex_session(
        trace_root=traces,
        cwd=cwd,
        now=_NOW + timedelta(minutes=20),
    )
    assert isinstance(stale, NoAttribution)
    assert stale.reason_code == "stale_trace"

    invalid_path = traces / "2026" / "08" / "12" / "rollout-invalid.jsonl"
    invalid_path.write_text(
        json.dumps(
            {
                "timestamp": "not-a-timestamp",
                "type": "session_meta",
                "payload": {"id": "invalid", "cwd": str(cwd)},
            }
        )
        + "\n"
        + json.dumps(
            {
                "timestamp": "still-invalid",
                "type": "event_msg",
                "payload": {"type": "task_started"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (traces / "2026" / "08" / "12" / "rollout-stale.jsonl").unlink()

    unparsable = select_codex_session(trace_root=traces, cwd=cwd, now=_NOW)
    assert isinstance(unparsable, NoAttribution)
    assert unparsable.reason_code == "unparsable_trace"


def test_peek_handles_short_input_and_metadata_after_old_scan_limit(
    tmp_path: Path,
) -> None:
    short = tmp_path / "short.jsonl"
    short.write_text("{\n", encoding="utf-8")
    assert _peek_session(short) == (None, None)

    cwd = tmp_path.resolve()
    delayed = tmp_path / "delayed.jsonl"
    delayed.write_text(
        "".join(json.dumps({"type": "noise"}) + "\n" for _ in range(45))
        + json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "delayed", "cwd": str(cwd)},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert _peek_session(delayed) == ("delayed", cwd)


def test_windows_cwd_normalization_is_case_separator_and_alias_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows cwd alias semantics")
    realpath = os.path.realpath

    def resolve_alias(value: str) -> str:
        if str(value).casefold() == r"c:\work\longre~1".casefold():
            return r"C:\Work\Long Repository"
        return realpath(value)

    monkeypatch.setattr("apu.behavior_watch.os.path.realpath", resolve_alias)
    assert normalized_cwd_key(r"C:\Work\Repo\.") == normalized_cwd_key(
        "c:/work/repo"
    )
    assert normalized_cwd_key(r"C:\WORK\LONGRE~1") == normalized_cwd_key(
        r"c:\work\long repository"
    )


def test_intervention_revalidates_exact_trace_binding(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    other = tmp_path / "other"
    cwd.mkdir()
    other.mkdir()
    traces = tmp_path / "sessions"
    trace = _write_trace(traces, cwd)
    state = tmp_path / "state"
    _, incident = mark_incident(
        state,
        "stopped before completing the requested task",
        trace_root=traces,
        cwd=cwd,
    )
    _, diagnosis = diagnose_incident(state, incident_id=incident["incident_id"])
    records = [
        json.loads(line)
        for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    records[0]["payload"]["cwd"] = str(other)
    trace.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="no_attribution: cwd_mismatch"):
        intervene(
            state,
            diagnosis_id=diagnosis["diagnosis_id"],
            dry_run=True,
            executable="codex",
        )


def test_intervention_asserts_non_mutating_policy_invariant(tmp_path: Path) -> None:
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
    diagnosis_path, diagnosis = diagnose_incident(
        state, incident_id=incident["incident_id"]
    )
    diagnosis["recommended_intervention"]["durable_policy_mutation"] = True
    diagnosis_path.write_text(json.dumps(diagnosis), encoding="utf-8")

    with pytest.raises(ValueError, match="durable_policy_mutation"):
        intervene(
            state,
            diagnosis_id=diagnosis["diagnosis_id"],
            dry_run=True,
            executable="codex",
        )

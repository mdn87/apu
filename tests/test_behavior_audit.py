from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import apu.behavior_audit as behavior_audit_module
from apu.behavior_audit import audit_behavior
from apu.cli import main
from apu.evidence import ingest_hook_event

_NOW = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)


def _write_trace(
    path: Path,
    cwd: Path,
    *,
    session_id: str,
    timestamp: datetime,
    repeated_failure: bool = False,
) -> Path:
    base = timestamp.isoformat().replace("+00:00", "Z")
    records: list[dict[str, object]] = [
        {
            "timestamp": base,
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": str(cwd),
                "base_instructions": "private instructions must not persist",
            },
        },
        {
            "timestamp": base,
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
    ]
    attempts = 2 if repeated_failure else 1
    for index in range(attempts):
        call_id = f"call-{index}"
        records.extend(
            [
                {
                    "timestamp": base,
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "shell_command",
                        "call_id": call_id,
                        "arguments": '{"command":"pytest --token private-value"}',
                    },
                },
                {
                    "timestamp": base,
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": "Exit code: 1\nprivate failure output",
                    },
                },
            ]
        )
    records.append(
        {
            "timestamp": base,
            "type": "event_msg",
            "payload": {"type": "task_complete"},
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    epoch = timestamp.timestamp()
    os.utime(path, (epoch, epoch))
    return path


def _write_incident(
    state: Path,
    *,
    session_id: str,
    barriers: list[str] | None = None,
) -> None:
    path = state / "behavior" / "incidents" / f"incident-{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "incident_id": f"incident-{session_id}",
                "session": {"session_id": session_id},
                "observed_signals": ["reversible-choice-escalation"],
                "possible_barriers": barriers or [],
                "evidence_plane": {"event_refs": []},
                "surface_refs": [],
            }
        ),
        encoding="utf-8",
    )


def test_audit_is_bounded_verified_and_content_minimized(tmp_path: Path) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    other = tmp_path / "other"
    traces = tmp_path / "sessions"
    cwd.mkdir()
    other.mkdir()
    _write_trace(
        traces / "recent.jsonl",
        cwd,
        session_id="recent",
        timestamp=_NOW - timedelta(hours=1),
        repeated_failure=True,
    )
    _write_trace(
        traces / "old.jsonl",
        cwd,
        session_id="old",
        timestamp=_NOW - timedelta(days=10),
    )
    _write_trace(
        traces / "other.jsonl",
        other,
        session_id="other",
        timestamp=_NOW - timedelta(hours=1),
    )

    path, report = audit_behavior(
        state,
        cwd=cwd,
        providers=("codex",),
        trace_root=traces,
        now=_NOW,
    )

    assert path.is_file()
    assert report["selection"]["audited_session_count"] == 1
    assert any(
        item["reason"] == "outside-lookback" for item in report["selection"]["skipped"]
    )
    finding = next(
        item
        for item in report["findings"]
        if item["detector"] == "repeated-identical-tool-failure"
    )
    assert finding["verification_status"] == "verified"
    encoded = path.read_text(encoding="utf-8")
    assert "private-value" not in encoded
    assert "private failure output" not in encoded
    assert "private instructions" not in encoded


def test_marked_session_bypasses_age_and_wins_session_cap(tmp_path: Path) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    traces = tmp_path / "sessions"
    cwd.mkdir()
    _write_trace(
        traces / "recent.jsonl",
        cwd,
        session_id="recent",
        timestamp=_NOW - timedelta(hours=1),
    )
    _write_trace(
        traces / "marked-old.jsonl",
        cwd,
        session_id="marked-old",
        timestamp=_NOW - timedelta(days=40),
    )
    _write_incident(state, session_id="marked-old")

    _, report = audit_behavior(
        state,
        cwd=cwd,
        providers=("codex",),
        trace_root=traces,
        now=_NOW,
        session_limit=1,
    )

    assert [item["session_id"] for item in report["sessions"]] == ["marked-old"]
    assert any(
        item["session_id"] == "recent" and item["reason"] == "session-limit"
        for item in report["selection"]["skipped"]
    )


def test_source_byte_cap_stops_selection_before_ingestion(tmp_path: Path) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    traces = tmp_path / "sessions"
    cwd.mkdir()
    _write_trace(
        traces / "older.jsonl",
        cwd,
        session_id="older",
        timestamp=_NOW - timedelta(hours=2),
    )
    newer = _write_trace(
        traces / "newer.jsonl",
        cwd,
        session_id="newer",
        timestamp=_NOW - timedelta(hours=1),
    )

    _, report = audit_behavior(
        state,
        cwd=cwd,
        providers=("codex",),
        trace_root=traces,
        now=_NOW,
        source_byte_limit=newer.stat().st_size,
    )

    assert report["selection"]["source_bytes"] <= newer.stat().st_size
    assert [item["session_id"] for item in report["sessions"]] == ["newer"]
    assert any(
        item["session_id"] == "older" and item["reason"] == "source-byte-limit"
        for item in report["selection"]["skipped"]
    )
    evidence_files = list(
        (state / "behavior" / "evidence" / "v2" / "codex").glob("*.jsonl")
    )
    assert len(evidence_files) == 1
    assert not list(
        (state / "behavior" / "evidence" / "codex").glob("*.jsonl")
    )
    stored_session_ids = {
        json.loads(line)["session_id"]
        for line in evidence_files[0].read_text(encoding="utf-8").splitlines()
        if line
    }
    assert stored_session_ids == {"newer"}


def test_audit_can_use_complete_v1_writer_during_rollback(tmp_path: Path) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    traces = tmp_path / "sessions"
    cwd.mkdir()
    _write_trace(
        traces / "legacy.jsonl",
        cwd,
        session_id="legacy-session",
        timestamp=_NOW - timedelta(minutes=1),
    )

    audit_behavior(
        state,
        cwd=cwd,
        providers=("codex",),
        trace_root=traces,
        now=_NOW,
    )
    assert len(
        list(
            (state / "behavior" / "evidence" / "v2" / "codex").glob("*.jsonl")
        )
    ) == 1

    _, report = audit_behavior(
        state,
        cwd=cwd,
        providers=("codex",),
        trace_root=traces,
        now=_NOW,
        evidence_schema_version=1,
    )

    assert report["scope"]["evidence_writer_schema_version"] == 1
    assert len(
        list((state / "behavior" / "evidence" / "codex").glob("*.jsonl"))
    ) == 1
    _, repeated = audit_behavior(
        state,
        cwd=cwd,
        providers=("codex",),
        trace_root=traces,
        now=_NOW,
        evidence_schema_version=1,
    )
    assert repeated["sessions"][0]["ingest_status"] == "unchanged-reused"


def test_metadata_discovery_cap_prioritizes_marked_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    traces = tmp_path / "sessions"
    cwd.mkdir()
    _write_trace(
        traces / "newer.jsonl",
        cwd,
        session_id="newer",
        timestamp=_NOW - timedelta(hours=1),
    )
    marked = _write_trace(
        traces / "marked.jsonl",
        cwd,
        session_id="marked",
        timestamp=_NOW - timedelta(days=40),
    )
    _write_incident(state, session_id="marked")
    incident_path = state / "behavior" / "incidents" / "incident-marked.json"
    incident = json.loads(incident_path.read_text(encoding="utf-8"))
    incident["session"]["trace_path"] = str(marked)
    incident_path.write_text(json.dumps(incident), encoding="utf-8")
    monkeypatch.setattr(behavior_audit_module, "_DISCOVERY_FILE_LIMIT", 1)

    _, report = audit_behavior(
        state,
        cwd=cwd,
        providers=("codex",),
        trace_root=traces,
        now=_NOW,
    )

    assert [item["session_id"] for item in report["sessions"]] == ["marked"]
    discovery_skip = next(
        item
        for item in report["selection"]["skipped"]
        if item["reason"] == "discovery-file-limit"
    )
    assert discovery_skip["skipped_file_count"] == 1


def test_operator_signal_is_suppressed_when_barrier_is_recorded(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    traces = tmp_path / "sessions"
    cwd.mkdir()
    _write_trace(
        traces / "barrier.jsonl",
        cwd,
        session_id="barrier",
        timestamp=_NOW - timedelta(hours=1),
    )
    _write_incident(
        state,
        session_id="barrier",
        barriers=["production-credential-boundary"],
    )

    _, report = audit_behavior(
        state,
        cwd=cwd,
        providers=("codex",),
        trace_root=traces,
        now=_NOW,
    )

    finding = next(
        item
        for item in report["findings"]
        if item["detector"] == "reversible-choice-escalation"
    )
    assert finding["status"] == "suppressed"
    assert finding["verification_status"] == "asserted"
    assert finding["barrier_codes"] == ["production-credential-boundary"]


def test_hook_audit_detects_completion_after_stale_test(tmp_path: Path) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    common = {"session_id": "hook-session", "cwd": str(cwd)}
    hook_events = [
        (
            "PreToolUse",
            {
                **common,
                "timestamp": "2026-08-14T15:00:00Z",
                "sequence": 1,
                "tool_use_id": "test-1",
                "tool_name": "Bash",
                "tool_input": {"command": "pytest"},
            },
        ),
        (
            "PostToolUse",
            {
                **common,
                "timestamp": "2026-08-14T15:00:01Z",
                "sequence": 2,
                "tool_use_id": "test-1",
                "tool_name": "Bash",
                "tool_input": {"command": "pytest"},
                "tool_response": "passed",
            },
        ),
        (
            "PreToolUse",
            {
                **common,
                "timestamp": "2026-08-14T15:00:02Z",
                "sequence": 3,
                "tool_use_id": "edit-1",
                "tool_name": "Write",
                "tool_input": {"file_path": "private-name.py", "content": "secret"},
            },
        ),
        (
            "TaskCompleted",
            {
                **common,
                "timestamp": "2026-08-14T15:00:03Z",
                "sequence": 4,
                "task_id": "task-1",
            },
        ),
    ]
    for index, (event_name, payload) in enumerate(hook_events):
        ingest_hook_event(
            state,
            "claude-code",
            event_name,
            payload,
            schema_version=1 if index < 2 else 2,
        )

    path, report = audit_behavior(
        state,
        cwd=cwd,
        providers=("claude-code",),
        now=_NOW,
    )

    assert any(
        item["detector"] == "completion-after-stale-gate" for item in report["findings"]
    )
    stored_bytes = sum(
        path.stat().st_size
        for path in (state / "behavior" / "evidence").rglob("*.jsonl")
    )
    assert report["sessions"][0]["source_bytes"] == stored_bytes
    assert "private-name.py" not in path.read_text(encoding="utf-8")
    assert "secret" not in path.read_text(encoding="utf-8")


def test_completion_findings_do_not_cross_task_windows(tmp_path: Path) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    common = {"session_id": "windowed-session", "cwd": str(cwd)}
    events = [
        ("TaskCreated", "2026-08-14T14:00:00Z", 1, "task-1", None, None),
        ("PreToolUse", "2026-08-14T14:00:01Z", 2, "test-1", "Bash", "pytest"),
        ("PostToolUse", "2026-08-14T14:00:02Z", 3, "test-1", "Bash", "pytest"),
        ("PreToolUse", "2026-08-14T14:00:03Z", 4, "edit-1", "Write", None),
        ("TaskCompleted", "2026-08-14T14:00:04Z", 5, "task-1", None, None),
        ("TaskCreated", "2026-08-14T14:00:05Z", 6, "task-2", None, None),
        ("TaskCompleted", "2026-08-14T14:00:06Z", 7, "task-2", None, None),
    ]
    for event_name, timestamp, sequence, correlation, tool_name, command in events:
        payload: dict[str, object] = {
            **common,
            "timestamp": timestamp,
            "sequence": sequence,
            "task_id" if event_name.startswith("Task") else "tool_use_id": correlation,
        }
        if tool_name is not None:
            payload["tool_name"] = tool_name
            payload["tool_input"] = (
                {"command": command}
                if command is not None
                else {"file_path": "private.py", "content": "secret"}
            )
        if event_name == "PostToolUse":
            payload["tool_response"] = "passed"
        ingest_hook_event(state, "claude-code", event_name, payload)

    _, report = audit_behavior(
        state,
        cwd=cwd,
        providers=("claude-code",),
        now=_NOW,
    )

    stale_gate_findings = [
        item
        for item in report["findings"]
        if item["detector"] == "completion-after-stale-gate"
    ]
    assert len(stale_gate_findings) == 1
    assert len(stale_gate_findings[0]["evidence_refs"]) == 3


def test_behavior_cli_exposes_caps_and_has_no_unbounded_mode(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    traces = tmp_path / "sessions"
    cwd.mkdir()
    traces.mkdir()
    monkeypatch.setenv("APU_HOME", str(state))

    result = main(
        [
            "behavior",
            "audit",
            "--provider",
            "codex",
            "--cwd",
            str(cwd),
            "--trace-root",
            str(traces),
            "--since",
            "12h",
            "--sessions",
            "3",
            "--max-bytes",
            "1MiB",
            "--evidence-schema-version",
            "1",
            "--json",
        ]
    )

    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["scope"]["lookback_seconds"] == 12 * 60 * 60
    assert report["scope"]["session_limit"] == 3
    assert report["scope"]["source_byte_limit"] == 1024 * 1024
    assert report["scope"]["evidence_writer_schema_version"] == 1

    with pytest.raises(SystemExit):
        main(["behavior", "audit", "--all-history"])

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apu.behavior_cli import event_main, intervene_main, watch_main, wtf_main
from apu.behavior_watch import (
    NoAttribution,
    SelectedSession,
    claude_code_trace_root,
    diagnose_incident,
    intervene,
    mark_incident,
    select_session,
    watcher_status,
)
from apu.evidence import read_evidence

_NOW = datetime.now(UTC).replace(microsecond=0)
_START = _NOW - timedelta(seconds=5)
_SESSION_ID = "05a17f11-6cfb-46eb-9fb2-6ca95025439c"
_PRIVATE_PROMPT = "run the existing recorder; private fixture words"
_PRIVATE_SETTING = "private configured hook command"
_FAKE_SECRET = "sk-proj-abcdefghijklmnop"


def _timestamp(offset: int) -> str:
    return (_START + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")


def _claude_record(
    record_type: str,
    cwd: Path,
    *,
    timestamp: str,
    message: dict[str, object] | None = None,
    **extra: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "type": record_type,
        "timestamp": timestamp,
        "cwd": str(cwd),
        "sessionId": _SESSION_ID,
        "entrypoint": "claude-desktop",
        "version": "2.1.260",
        "uuid": f"record-{timestamp}",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
    }
    if message is not None:
        value["message"] = message
    value.update(extra)
    return value


def _write_claude_trace(
    root: Path,
    cwd: Path,
    *,
    session_id: str = _SESSION_ID,
    completed: bool = False,
    configured_denial: bool = False,
    invalid_prefix: bool = False,
) -> Path:
    project = root / "C--fixture-repo"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    records = [
        _claude_record(
            "user",
            cwd,
            timestamp=_timestamp(0),
            message={"role": "user", "content": _PRIVATE_PROMPT},
        )
    ]
    records[0]["sessionId"] = session_id
    if completed:
        records.append(
            _claude_record(
                "assistant",
                cwd,
                timestamp=_timestamp(1),
                message={
                    "role": "assistant",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "completed privately"}],
                },
            )
        )
    else:
        records.append(
            _claude_record(
                "assistant",
                cwd,
                timestamp=_timestamp(1),
                message={
                    "role": "assistant",
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_fixture",
                            "name": "Write",
                            "input": {"private": _FAKE_SECRET},
                        }
                    ],
                },
            )
        )
        if configured_denial:
            records.append(
                _claude_record(
                    "user",
                    cwd,
                    timestamp=_timestamp(2),
                    message={
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_fixture",
                                "is_error": True,
                                "content": "configured hook denied the write",
                            }
                        ],
                    },
                    toolDenialKind="permission-rule",
                    toolUseResult="configured hook denied the write",
                )
            )
    for record in records:
        record["sessionId"] = session_id
    encoded = "\n".join(json.dumps(record) for record in records) + "\n"
    if invalid_prefix:
        encoded = "not-json\n" + encoded
    path.write_text(encoded, encoding="utf-8")
    return path


def _write_codex_trace(root: Path, cwd: Path) -> Path:
    path = root / "2026" / "09" / "07" / "rollout-codex-session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": _timestamp(0),
            "type": "session_meta",
            "payload": {
                "id": "codex-session",
                "cwd": str(cwd),
                "source": "vscode",
                "originator": "Codex Desktop",
            },
        },
        {
            "timestamp": _timestamp(1),
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def test_claude_trace_root_honors_config_override(tmp_path: Path) -> None:
    config = tmp_path / "custom-claude"
    assert (
        claude_code_trace_root(
            environment={"CLAUDE_CONFIG_DIR": str(config)}, home=tmp_path / "home"
        )
        == config / "projects"
    )


def test_selects_incomplete_claude_session_and_rejects_completed_or_stale(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "projects"
    selected_path = _write_claude_trace(traces, cwd, invalid_prefix=True)

    selected = select_session(
        provider="claude-code", trace_root=traces, cwd=cwd, now=_NOW
    )

    assert isinstance(selected, SelectedSession)
    assert selected.session.provider == "claude-code"
    assert selected.session.path == selected_path.resolve()
    assert selected.session.invalid_record_count == 1
    assert selected.provenance.provider == "claude-code"

    _write_claude_trace(traces, cwd, completed=True)
    completed = select_session(
        provider="claude-code", trace_root=traces, cwd=cwd, now=_NOW
    )
    assert isinstance(completed, NoAttribution)
    assert completed.reason_code == "no_active_candidate"

    _write_claude_trace(traces, cwd)
    stale = select_session(
        provider="claude-code",
        trace_root=traces,
        cwd=cwd,
        now=_NOW + timedelta(minutes=20),
    )
    assert isinstance(stale, NoAttribution)
    assert stale.reason_code == "stale_trace"


def test_claude_session_binds_to_latest_cwd_and_scopes_evidence(
    tmp_path: Path,
) -> None:
    earlier_cwd = tmp_path / "earlier-repo"
    current_cwd = tmp_path / "current-repo"
    earlier_cwd.mkdir()
    current_cwd.mkdir()
    traces = tmp_path / "projects"
    trace = _write_claude_trace(traces, earlier_cwd, completed=True)
    records = [
        json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    records.extend(
        [
            _claude_record(
                "user",
                current_cwd,
                timestamp=_timestamp(2),
                message={"role": "user", "content": "record the current failure"},
            ),
            _claude_record(
                "assistant",
                current_cwd,
                timestamp=_timestamp(3),
                message={
                    "role": "assistant",
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_current",
                            "name": "Write",
                            "input": {"private": _FAKE_SECRET},
                        }
                    ],
                },
            ),
        ]
    )
    trace.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    selected = select_session(
        provider="claude-code", trace_root=traces, cwd=current_cwd, now=_NOW
    )
    old_cwd = select_session(
        provider="claude-code", trace_root=traces, cwd=earlier_cwd, now=_NOW
    )

    assert isinstance(selected, SelectedSession)
    assert selected.session.cwd == current_cwd.resolve()
    assert selected.session.record_count == 2
    assert {event["line"] for event in selected.session.evidence_events} == {3, 4}
    assert isinstance(old_cwd, NoAttribution)
    assert old_cwd.reason_code == "no_exact_cwd_candidate"

    state = tmp_path / "state"
    _, incident = mark_incident(
        state,
        "the current request was not completed",
        provider="claude-code",
        trace_root=traces,
        cwd=current_cwd,
    )
    evidence_path = Path(incident["evidence_plane"]["evidence_path"])
    evidence = [
        json.loads(line)
        for line in evidence_path.read_text(encoding="utf-8").splitlines()
    ]
    assert evidence
    assert {Path(event["state"]["cwd"]) for event in evidence} == {
        current_cwd.resolve()
    }
    assert {event["source"]["line"] for event in evidence} <= {3, 4}


def test_claude_completed_turn_ignores_stale_unmatched_tool_ids(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "projects"
    trace = _write_claude_trace(traces, cwd)
    records = [
        json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    records.extend(
        [
            _claude_record(
                "user",
                cwd,
                timestamp=_timestamp(2),
                message={"role": "user", "content": "a later request"},
            ),
            _claude_record(
                "assistant",
                cwd,
                timestamp=_timestamp(3),
                message={
                    "role": "assistant",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "finished"}],
                },
            ),
        ]
    )
    trace.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    result = select_session(
        provider="claude-code", trace_root=traces, cwd=cwd, now=_NOW
    )

    assert isinstance(result, NoAttribution)
    assert result.reason_code == "no_active_candidate"


def test_claude_evidence_keeps_exact_cwd_when_one_session_moves(
    tmp_path: Path,
) -> None:
    first_cwd = tmp_path / "first-repo"
    second_cwd = tmp_path / "second-repo"
    first_cwd.mkdir()
    second_cwd.mkdir()
    traces = tmp_path / "projects"
    trace = _write_claude_trace(traces, first_cwd)
    state = tmp_path / "state"

    mark_incident(
        state,
        "the first request did not complete",
        provider="claude-code",
        trace_root=traces,
        cwd=first_cwd,
    )
    records = [
        json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    records.extend(
        [
            _claude_record(
                "user",
                second_cwd,
                timestamp=_timestamp(2),
                message={"role": "user", "content": "record another failure"},
            ),
            _claude_record(
                "assistant",
                second_cwd,
                timestamp=_timestamp(3),
                message={
                    "role": "assistant",
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_second",
                            "name": "Write",
                            "input": {},
                        }
                    ],
                },
            ),
        ]
    )
    trace.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    mark_incident(
        state,
        "the second request did not complete",
        provider="claude-code",
        trace_root=traces,
        cwd=second_cwd,
    )
    evidence = read_evidence(state, "claude-code", _SESSION_ID)

    assert {Path(event["state"]["cwd"]).resolve() for event in evidence} == {
        first_cwd.resolve(),
        second_cwd.resolve(),
    }
    assert all(
        Path(event["state"]["cwd"]).resolve() == first_cwd.resolve()
        for event in read_evidence(state, "claude-code", _SESSION_ID, cwd=first_cwd)
    )
    # A repeat append must be able to read the already mixed-cwd evidence file.
    mark_incident(
        state,
        "the second request still did not complete",
        provider="claude-code",
        trace_root=traces,
        cwd=second_cwd,
    )


def test_claude_selector_ignores_nested_subagent_transcripts(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "projects"
    parent = _write_claude_trace(traces, cwd)
    subagent = parent.parent / _SESSION_ID / "subagents" / "agent-a123.jsonl"
    subagent.parent.mkdir(parents=True)
    subagent.write_text(parent.read_text(encoding="utf-8"), encoding="utf-8")

    selected = select_session(
        provider="claude-code", trace_root=traces, cwd=cwd, now=_NOW
    )

    assert isinstance(selected, SelectedSession)
    assert selected.provenance.candidate_count == 1
    assert selected.session.path == parent.resolve()


def test_cross_provider_selection_fails_closed_and_override_resolves_it(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    _write_codex_trace(codex_root, cwd)
    _write_claude_trace(claude_root, cwd)
    roots = {"codex": codex_root, "claude-code": claude_root}

    ambiguous = select_session(trace_roots=roots, cwd=cwd, now=_NOW)
    selected = select_session(
        provider="claude-code", trace_roots=roots, cwd=cwd, now=_NOW
    )

    assert isinstance(ambiguous, NoAttribution)
    assert ambiguous.reason_code == "ambiguous_provider"
    assert ambiguous.provenance.provider is None
    assert ambiguous.provenance.candidate_count == 2
    assert isinstance(selected, SelectedSession)
    assert selected.session.provider == "claude-code"


def test_two_incomplete_claude_sessions_are_ambiguous(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "projects"
    _write_claude_trace(traces, cwd, session_id="11111111-1111-4111-8111-111111111111")
    _write_claude_trace(traces, cwd, session_id="22222222-2222-4222-8222-222222222222")

    result = select_session(
        provider="claude-code", trace_root=traces, cwd=cwd, now=_NOW
    )

    assert isinstance(result, NoAttribution)
    assert result.reason_code == "ambiguous_active_candidates"
    assert result.provenance.candidate_count == 2


def test_claude_incident_flow_is_provider_bound_private_and_resumable(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "CLAUDE.md").write_text("private instruction body\n", encoding="utf-8")
    settings_path = cwd / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {"PreToolUse": [{"command": _PRIVATE_SETTING}]},
                "permissions": {"deny": ["Write(private)"]},
            }
        ),
        encoding="utf-8",
    )
    traces = tmp_path / "projects"
    trace = _write_claude_trace(traces, cwd)
    original_trace = trace.read_bytes()
    state = tmp_path / "state"

    _, incident = mark_incident(
        state,
        "asked me to run apu-wtf and record a failure; it designed a new command instead",
        provider="claude-code",
        trace_root=traces,
        cwd=cwd,
    )
    _, diagnosis = diagnose_incident(state, incident_id=incident["incident_id"])
    _, result = intervene(
        state,
        diagnosis_id=diagnosis["diagnosis_id"],
        dry_run=True,
        executable="claude",
    )

    assert incident["provider"] == "claude-code"
    assert incident["session"]["provider"] == "claude-code"
    assert incident["evidence_plane"]["provider"] == "claude-code"
    evidence_path = Path(incident["evidence_plane"]["evidence_path"])
    assert evidence_path.parent.name == "claude-code"
    assert evidence_path.parent.parent.name == "v2"
    assert {
        json.loads(line)["provider"]
        for line in evidence_path.read_text(encoding="utf-8").splitlines()
    } == {"claude-code"}
    assert "request-substitution" in incident["observed_signals"]
    assert diagnosis["provider"] == "claude-code"
    assert result["provider"] == "claude-code"
    assert result["command"][:2] == ["claude", "--resume"]
    assert result["executed"] is False
    assert any(
        surface["path"] == str(settings_path.resolve())
        for surface in incident["surface_refs"]
    )
    health = watcher_status(state)
    assert set(health["providers"]) == {"claude-code", "codex"}
    assert health["provider_health"]["claude-code"]["last_successful_attribution"]
    stored_health = json.loads(
        (state / "behavior" / "selector-health.json").read_text(encoding="utf-8")
    )
    assert set(stored_health["providers"]) == {"claude-code", "codex"}

    stored = "\n".join(
        path.read_text(encoding="utf-8") for path in state.rglob("*") if path.is_file()
    )
    assert _PRIVATE_PROMPT not in stored
    assert _PRIVATE_SETTING not in stored
    assert "private instruction body" not in stored
    assert _FAKE_SECRET not in stored
    assert trace.read_bytes() == original_trace


def test_claude_intervention_revalidates_provider_session_and_cwd(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "projects"
    trace = _write_claude_trace(traces, cwd)
    state = tmp_path / "state"
    _, incident = mark_incident(
        state,
        "stopped before completing the requested task",
        provider="claude-code",
        trace_root=traces,
        cwd=cwd,
    )
    _, diagnosis = diagnose_incident(state, incident_id=incident["incident_id"])
    records = [
        json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    for record in records:
        record["sessionId"] = "33333333-3333-4333-8333-333333333333"
    trace.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="no_attribution: session_not_found"):
        intervene(
            state,
            diagnosis_id=diagnosis["diagnosis_id"],
            dry_run=True,
            executable="claude",
        )


def test_configured_claude_denial_is_a_barrier_not_an_invented_gate(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "projects"
    _write_claude_trace(traces, cwd, configured_denial=True)
    state = tmp_path / "state"

    _, incident = mark_incident(
        state,
        "the requested write did not happen",
        provider="claude-code",
        trace_root=traces,
        cwd=cwd,
    )
    _, diagnosis = diagnose_incident(state, incident_id=incident["incident_id"])

    assert "operator-designed-gate" in incident["possible_barriers"]
    assert "invented-gate" not in incident["observed_signals"]
    assert diagnosis["status"] == "possible-legitimate-barrier"
    with pytest.raises(ValueError, match="legitimate barrier"):
        intervene(state, diagnosis_id=diagnosis["diagnosis_id"], dry_run=True)


def test_harness_failure_is_not_misclassified_as_operator_designed_gate(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "projects"
    trace = _write_claude_trace(traces, cwd, configured_denial=True)
    records = [
        json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    records[-1].pop("toolDenialKind")
    trace.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    _, incident = mark_incident(
        tmp_path / "state",
        "the requested write did not happen",
        provider="claude-code",
        trace_root=traces,
        cwd=cwd,
    )

    assert "operator-designed-gate" not in incident["possible_barriers"]


def test_claude_trace_detects_plan_substitution_after_imperative(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "projects"
    trace = _write_claude_trace(traces, cwd, completed=True)
    records = [
        json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    records[0]["message"]["content"] = "run apu-wtf"
    records[1]["message"]["content"][0]["text"] = (
        "Here is the plan for a replacement. Say go ahead."
    )
    trace.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    _, incident = mark_incident(
        tmp_path / "state",
        "the requested task was not performed",
        provider="claude-code",
        trace_root=traces,
        session_id=_SESSION_ID,
        cwd=cwd,
    )

    assert "request-substitution" in incident["observed_signals"]


def test_claude_cli_provider_override_runs_the_short_command_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "projects"
    _write_claude_trace(traces, cwd)
    state = tmp_path / "state"
    monkeypatch.setenv("APU_HOME", str(state))
    monkeypatch.setattr(
        "apu.behavior_watch.shutil.which",
        lambda name: name if name in {"claude", "codex"} else None,
    )

    assert (
        event_main(
            [
                "asked me to run the existing tool; it designed another one instead",
                "--provider",
                "claude-code",
                "--trace-root",
                str(traces),
                "--cwd",
                str(cwd),
            ]
        )
        == 0
    )
    assert "Provider: claude-code" in capsys.readouterr().out
    assert wtf_main(["--provider", "claude-code", "--json"]) == 0
    diagnosis = json.loads(capsys.readouterr().out)
    assert diagnosis["provider"] == "claude-code"
    assert intervene_main(["--provider", "claude-code", "--dry-run", "--json"]) == 0
    intervention = json.loads(capsys.readouterr().out)
    assert intervention["provider"] == "claude-code"
    assert watch_main(["--provider", "claude-code"]) == 0
    assert "Claude Code JSONL" in capsys.readouterr().out


def test_wtf_explicit_provider_does_not_diagnose_a_stale_incident_of_another_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A latest incident for Codex must not hijack ``apu-wtf --provider claude-code``.

    Before the fix, the latest incident was diagnosed whenever it existed and the
    explicit selectors were ignored, so the command failed with a provider
    mismatch against an unrelated, stale incident.
    """

    state = tmp_path / "state"
    monkeypatch.setenv("APU_HOME", str(state))
    monkeypatch.setattr(
        "apu.behavior_watch.shutil.which",
        lambda name: name if name in {"claude", "codex"} else None,
    )
    codex_cwd = tmp_path / "codex-repo"
    codex_cwd.mkdir()
    codex_traces = tmp_path / "sessions"
    codex_trace = codex_traces / "rollout.jsonl"
    codex_trace.parent.mkdir(parents=True)
    codex_trace.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {
                    "timestamp": _timestamp(0),
                    "type": "session_meta",
                    "payload": {
                        "id": "codex-session",
                        "cwd": str(codex_cwd),
                        "source": "vscode",
                        "originator": "Codex Desktop",
                    },
                },
                {
                    "timestamp": _timestamp(1),
                    "type": "event_msg",
                    "payload": {"type": "task_started"},
                },
                {
                    "timestamp": _timestamp(2),
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "message": "Would you prefer that I choose which file?",
                    },
                },
            ]
        ),
        encoding="utf-8",
    )
    assert (
        event_main(
            [
                "codex asked me to pick a file name",
                "--provider",
                "codex",
                "--trace-root",
                str(codex_traces),
                "--cwd",
                str(codex_cwd),
            ]
        )
        == 0
    )
    capsys.readouterr()

    claude_cwd = tmp_path / "claude-repo"
    claude_cwd.mkdir()
    claude_traces = tmp_path / "projects"
    _write_claude_trace(claude_traces, claude_cwd)

    assert (
        wtf_main(
            [
                "--provider",
                "claude-code",
                "--trace-root",
                str(claude_traces),
                "--cwd",
                str(claude_cwd),
                "--json",
            ]
        )
        == 0
    )
    diagnosis = json.loads(capsys.readouterr().out)
    assert diagnosis["provider"] == "claude-code"
    latest = json.loads(
        (state / "behavior" / "latest-incident.json").read_text(encoding="utf-8")
    )
    assert latest["incident_id"] == diagnosis["incident_id"]

    # Without selectors the latest (now Claude) incident is reused, and the text
    # output says which incident and provider it diagnosed.
    assert wtf_main([]) == 0
    output = capsys.readouterr().out
    assert f"Incident: {diagnosis['incident_id']} (claude-code, previously marked)" in output

    # A matching explicit provider reuses it too instead of marking again.
    assert wtf_main(["--provider", "claude-code", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["incident_id"] == diagnosis["incident_id"]

    # A session id that the latest incident does not carry fails closed on
    # attribution rather than diagnosing the wrong incident.
    assert (
        wtf_main(
            [
                "--provider",
                "claude-code",
                "--session-id",
                "00000000-0000-0000-0000-000000000000",
                "--trace-root",
                str(claude_traces),
                "--cwd",
                str(claude_cwd),
            ]
        )
        == 2
    )

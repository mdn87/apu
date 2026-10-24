from __future__ import annotations

import json

import pytest

from apu.codex_results import codex_result_status


def block(code=0, **extra):
    envelope = {
        "chunk_id": "private-id",
        "output": "private output",
        "wall_time_seconds": 0.1,
        "exit_code": code,
        **extra,
    }
    return {"type": "input_text", "text": json.dumps(envelope)}


@pytest.mark.parametrize(
    "code,status",
    [
        (0, "completed"),
        (1, "failed"),
        (-9, "failed"),
        (True, "unknown"),
        ("1", "unknown"),
        (None, "unknown"),
    ],
)
def test_typed_command_results_keep_only_explicit_status(code, status):
    result = codex_result_status([block(code)])
    assert result == (status, code if type(code) is int else None)


def test_running_requires_session_identity_and_never_implies_success():
    assert codex_result_status([block(None, session_id=17)]) == ("running", None)
    assert codex_result_status([block(None, session_id=True)]) == ("unknown", None)


def test_envelope_output_and_arbitrary_json_are_not_searched_for_failures():
    assert codex_result_status([block(0, output="Exit code: 9")]) == ("completed", 0)
    for value in (
        {"exit_code": 9},
        {"value": json.loads(block(9)["text"])},
        {"chunk_id": 2, "output": "x", "wall_time_seconds": 1, "exit_code": 9},
    ):
        assert codex_result_status(
            [{"type": "input_text", "text": json.dumps(value)}]
        ) == ("unknown", None)


def test_sibling_aggregation_is_conservative_and_ignores_images():
    assert codex_result_status([block(0), block(1), block(-9)]) == ("failed", 1)
    assert codex_result_status([block(0), block(None, session_id=17)]) == (
        "running",
        None,
    )
    assert codex_result_status(
        [block(0), {"type": "input_text", "text": "opaque"}]
    ) == ("unknown", None)
    assert codex_result_status(
        [block(0), {"type": "input_image", "image_url": "private"}]
    ) == ("completed", 0)
    assert codex_result_status([]) == ("unknown", None)


def test_legacy_strings_and_forward_envelope_metadata():
    assert codex_result_status("Exit code: -9\nprivate output") == ("failed", -9)
    assert codex_result_status("Exit code: 0") == ("completed", 0)
    assert codex_result_status("Command failed") == ("failed", None)
    assert codex_result_status("ordinary legacy response") == ("completed", None)
    assert codex_result_status([block(0, new_metadata="ignored")]) == ("completed", 0)
    assert codex_result_status(block(1)["text"]) == ("failed", 1)


def test_unrecognized_json_string_does_not_fall_back_to_prose_matching():
    assert codex_result_status('{"output": "Command failed"}') == ("unknown", None)


@pytest.mark.parametrize("kind", ["function_call_output", "custom_tool_call_output"])
@pytest.mark.parametrize(
    "code,failed", [(0, False), (1, True), (-9, True), (None, False)]
)
def test_live_watcher_uses_the_same_explicit_status(tmp_path, kind, code, failed):
    from apu.behavior_watch import CodexProvider

    trace = tmp_path / "trace.jsonl"
    records = [
        {
            "type": "session_meta",
            "payload": {"id": "status-session", "cwd": str(tmp_path)},
        },
        {
            "type": "response_item",
            "payload": {"type": kind, "call_id": "call", "output": [block(code)]},
        },
    ]
    trace.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    assert (
        "tool-failure-observed" in CodexProvider.parse(trace).signal_codes
    ) is failed


def test_versioned_replay_preserves_old_bytes_and_selects_corrected_results(tmp_path):
    from apu.evidence import ingest_codex_trace, read_evidence, reconcile_evidence

    trace = tmp_path / "trace.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "replay", "cwd": str(tmp_path)}}
    ]
    for i, code in enumerate((0, 1, None)):
        records.extend(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "shell_command",
                        "call_id": str(i),
                        "arguments": '{"command":"pytest"}',
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": str(i),
                        "output": [block(code)],
                    },
                },
            ]
        )
    trace.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    state = tmp_path / "state"
    old_path, old, _ = ingest_codex_trace(state, trace, schema_version=2)
    old_bytes = old_path.read_bytes()
    with trace.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}})
            + "\n"
        )
    new_path, new, boundary = ingest_codex_trace(state, trace, schema_version=3)
    assert "private output" not in new_path.read_text()
    assert "private-id" not in new_path.read_text()
    selected = read_evidence(state, "codex", "replay")
    assert len(selected) == len(new) == len(old) + 1
    assert all(event["schema_version"] == 3 for event in selected)
    assert old_path.read_bytes() == old_bytes
    results = [
        e
        for e in selected
        if e["evidence_class"] == "result" and e["correlation_sha256"]
    ]
    assert [
        (e["event_type"], e["observation"]["status"], e["observation"]["exit_code"])
        for e in results
    ] == [
        ("tool.completed", "completed", 0),
        ("tool.failed", "failed", 1),
        ("tool.completed", "unknown", None),
    ]
    assert reconcile_evidence(selected)["paired_tool_calls"] == 3
    _, _, replay = ingest_codex_trace(state, trace, schema_version=3)
    assert replay["appended_count"] == 0
    assert boundary["appended_count"] == len(new)


def test_new_projection_does_not_hide_unrelated_evidence_disagreement(tmp_path):
    from copy import deepcopy

    from apu.evidence import append_evidence_events, ingest_codex_trace, read_evidence

    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "conflict", "cwd": str(tmp_path)},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    ingest_codex_trace(state, trace, schema_version=2)
    _, new, _ = ingest_codex_trace(tmp_path / "new", trace, schema_version=3)
    changed = deepcopy(new[0])
    changed["observation"]["status"] = "unexpected"
    append_evidence_events(state, [changed])
    with pytest.raises(ValueError, match="projections disagree"):
        read_evidence(state, "codex", "conflict")

    changed_again = deepcopy(new[0])
    changed_again["observation"]["status"] = "different"
    with pytest.raises(ValueError, match="same-version evidence projections disagree"):
        append_evidence_events(state, [changed_again])


def test_duration_metadata_cannot_crash_or_falsely_validate_an_envelope():
    assert codex_result_status([block(0, wall_time_seconds=10**400)]) == (
        "completed",
        0,
    )
    for duration in (float("inf"), float("nan"), True, -1, "1"):
        assert codex_result_status([block(0, wall_time_seconds=duration)]) == (
            "unknown",
            None,
        )


@pytest.mark.parametrize(
    "signals,expected",
    [
        (["tool-failure-observed"], "insufficient-evidence"),
        (
            ["tool-failure-observed", "repeated-identical-tool-failure"],
            "insufficient-evidence",
        ),
        (
            ["tool-failure-observed", "reversible-choice-escalation"],
            "likely-autonomy-loss",
        ),
    ],
)
def test_codex_exit_status_alone_cannot_authorize_recovery(tmp_path, signals, expected):
    from apu.behavior_watch import diagnose_incident, intervene

    path = tmp_path / "behavior/incidents/incident-status.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "incident_id": "incident-status",
                "provider": "codex",
                "observed_signals": signals,
                "surface_refs": [],
            }
        ),
        encoding="utf-8",
    )
    _, diagnosis = diagnose_incident(tmp_path, incident_id="incident-status")
    assert diagnosis["status"] == expected
    assert diagnosis["observed_signals"] == sorted(signals)
    if expected == "insufficient-evidence":
        with pytest.raises(ValueError, match="does not support continuation"):
            intervene(tmp_path, diagnosis_id=diagnosis["diagnosis_id"], dry_run=True)


@pytest.mark.parametrize(
    "code,extra,expected",
    [(0, {}, True), (None, {}, False), (None, {"session_id": 17}, False)],
)
def test_audit_upgrade_and_unknown_results_never_count_as_passing_tests(
    tmp_path, code, extra, expected
):
    from datetime import UTC, datetime

    from apu.behavior_audit import audit_behavior
    from apu.evidence import ingest_codex_trace

    traces = tmp_path / "traces"
    traces.mkdir()
    path = traces / "session.jsonl"
    stamp = datetime.now(UTC).isoformat()
    records = [
        {
            "type": "session_meta",
            "payload": {"id": "audit-status", "cwd": str(tmp_path)},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "test",
                "arguments": '{"command":"pytest"}',
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "test",
                "output": [block(code, **extra)],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "apply_patch",
                "call_id": "edit",
                "arguments": "fixture",
            },
        },
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    path.write_text(
        "".join(json.dumps({"timestamp": stamp, **r}) + "\n" for r in records),
        encoding="utf-8",
    )
    state = tmp_path / "state"
    ingest_codex_trace(state, path, schema_version=2)
    _, report = audit_behavior(
        state, cwd=tmp_path, providers=("codex",), trace_root=traces, gate_log=None
    )
    assert report["sessions"][0]["ingest_status"] == "ingested"
    assert (
        "completion-after-stale-gate" in {f["detector"] for f in report["findings"]}
    ) is expected
    _, replay = audit_behavior(
        state, cwd=tmp_path, providers=("codex",), trace_root=traces, gate_log=None
    )
    assert replay["sessions"][0]["ingest_status"] == "unchanged-reused"
    empty = tmp_path / "empty"
    empty.mkdir()
    _, existing = audit_behavior(
        state, cwd=tmp_path, providers=("codex",), trace_root=empty, gate_log=None
    )
    assert len(existing["sessions"]) == 1
    assert existing["sessions"][0]["ingest_status"] == "existing-evidence"

from __future__ import annotations

import json

import pytest

from apu.codex_results import codex_result_status


def block(code=0, **extra):
    envelope = {"chunk_id": "private-id", "output": "private output",
                "wall_time_seconds": 0.1, "exit_code": code, **extra}
    return {"type": "input_text", "text": json.dumps(envelope)}


@pytest.mark.parametrize("code,status", [(0, "completed"), (1, "failed"),
                                         (-9, "failed"), (True, "unknown"),
                                         ("1", "unknown"), (None, "unknown")])
def test_typed_command_results_keep_only_explicit_status(code, status):
    result = codex_result_status([block(code)])
    assert result == (status, code if type(code) is int else None)


def test_running_requires_session_identity_and_never_implies_success():
    assert codex_result_status([block(None, session_id=17)]) == ("running", None)
    assert codex_result_status([block(None, session_id=True)]) == ("unknown", None)


def test_envelope_output_and_arbitrary_json_are_not_searched_for_failures():
    assert codex_result_status([block(0, output='Exit code: 9')]) == ("completed", 0)
    for value in ({"exit_code": 9}, {"value": json.loads(block(9)["text"])},
                  {"chunk_id": 2, "output": "x", "wall_time_seconds": 1, "exit_code": 9}):
        assert codex_result_status([{"type": "input_text", "text": json.dumps(value)}]) == ("unknown", None)


def test_sibling_aggregation_is_conservative_and_ignores_images():
    assert codex_result_status([block(0), block(1), block(-9)]) == ("failed", 1)
    assert codex_result_status([block(0), block(None, session_id=17)]) == ("running", None)
    assert codex_result_status([block(0), {"type": "input_text", "text": "opaque"}]) == ("unknown", None)
    assert codex_result_status([block(0), {"type": "input_image", "image_url": "private"}]) == ("completed", 0)
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
@pytest.mark.parametrize("code,failed", [(0, False), (1, True), (-9, True), (None, False)])
def test_live_watcher_uses_the_same_explicit_status(tmp_path, kind, code, failed):
    from apu.behavior_watch import CodexProvider

    trace = tmp_path / "trace.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "status-session", "cwd": str(tmp_path)}},
        {"type": "response_item", "payload": {"type": kind, "call_id": "call", "output": [block(code)]}},
    ]
    trace.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    assert ("tool-failure-observed" in CodexProvider.parse(trace).signal_codes) is failed

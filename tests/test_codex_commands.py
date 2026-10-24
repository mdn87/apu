from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from apu.behavior_audit import audit_behavior
from apu.evidence import append_evidence_events, ingest_codex_trace, read_evidence


def trace(
    tmp_path, command="pytest -q", *, name="exec_command", arguments=None, code=0
):
    root = tmp_path / "traces"
    root.mkdir(exist_ok=True)
    path = root / "commands.jsonl"
    stamp = datetime.now(UTC).isoformat()
    records = [
        {"type": "session_meta", "payload": {"id": "commands", "cwd": str(tmp_path)}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": name,
                "call_id": "test",
                "arguments": json.dumps({"cmd": command})
                if arguments is None
                else arguments,
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "test",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "chunk_id": "private",
                                "output": "private output",
                                "wall_time_seconds": 0.1,
                                "exit_code": code,
                            }
                        ),
                    }
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "apply_patch",
                "call_id": "edit",
                "arguments": "private patch",
            },
        },
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    path.write_text(
        "".join(json.dumps({"timestamp": stamp, **r}) + "\n" for r in records),
        encoding="utf-8",
    )
    return path


def request(events):
    return next(e for e in events if e["event_type"] == "tool.requested")


@pytest.mark.parametrize(
    "command,expected",
    [
        ("python -m pytest -q", "test"),
        ("npm test", "test"),
        ("git status --short", "git-read"),
        ("git commit -m fixture", "git-write"),
        ("curl https://example.invalid", "network"),
        ("rg pattern", "shell"),
    ],
)
def test_exec_commands_are_classified_without_retaining_content(
    tmp_path, command, expected
):
    path = trace(tmp_path, command)
    destination, events, _ = ingest_codex_trace(tmp_path / "state", path)
    assert request(events)["observation"]["command_class"] == expected
    assert command not in destination.read_text()
    assert "private output" not in destination.read_text()


@pytest.mark.parametrize(
    "name,args,expected",
    [
        ("exec_command", {"cmd": "pytest", "command": "git status"}, "test"),
        ("functions.exec_command", {"cmd": "git diff"}, "git-read"),
        ("exec_command", {"cmd": None}, "shell"),
        ("exec_command", {"command": "pytest"}, "shell"),
        ("shell_command", {"command": "pytest", "cmd": "git status"}, "test"),
        ("unrelated.exec_command", {"cmd": "pytest"}, None),
    ],
)
def test_provider_arguments_and_names_are_explicit(tmp_path, name, args, expected):
    path = trace(tmp_path, name=name, arguments=args)
    _, events, _ = ingest_codex_trace(tmp_path / "state", path)
    assert request(events)["observation"]["command_class"] == expected


@pytest.mark.parametrize("version", [1, 2, 3])
def test_replay_preserves_legacy_bytes_and_selects_one_corrected_request(
    tmp_path, monkeypatch, version
):
    import apu.evidence as module

    path = trace(tmp_path)
    state = tmp_path / "state"
    real_classify = module._command_class
    with monkeypatch.context() as old:
        old.setattr(
            module,
            "_command_class",
            lambda *a, **k: None if a[0] == "exec_command" else real_classify(*a, **k),
        )
        old_path, before, _ = ingest_codex_trace(state, path, schema_version=version)
    old_bytes = old_path.read_bytes()
    assert request(before)["observation"]["command_class"] is None
    _, report = audit_behavior(
        state, cwd=tmp_path, providers=("codex",), trace_root=path.parent, gate_log=None
    )
    assert report["sessions"][0]["ingest_status"] == "ingested"
    selected = read_evidence(state, "codex", "commands")
    assert len(selected) == len(before)
    assert request(selected)["observation"]["command_class"] == "test"
    assert old_path.read_bytes().startswith(old_bytes)
    if version < 3:
        assert old_path.read_bytes() == old_bytes
    corrected_bytes = old_path.read_bytes()
    _, repeated = audit_behavior(
        state, cwd=tmp_path, providers=("codex",), trace_root=path.parent, gate_log=None
    )
    assert repeated["sessions"][0]["ingest_status"] == "unchanged-reused"
    _, _, boundary = ingest_codex_trace(state, path)
    assert boundary["appended_count"] == 0
    assert old_path.read_bytes() == corrected_bytes
    assert "completion-after-stale-gate" in {f["detector"] for f in report["findings"]}


@pytest.mark.parametrize("code,expected", [(0, True), (1, False), (None, False)])
def test_classified_test_requires_successful_result(tmp_path, code, expected):
    path = trace(tmp_path, code=code)
    _, report = audit_behavior(
        tmp_path / "state",
        cwd=tmp_path,
        providers=("codex",),
        trace_root=path.parent,
        gate_log=None,
    )
    assert (
        "completion-after-stale-gate" in {f["detector"] for f in report["findings"]}
    ) is expected


def test_old_writers_remain_unchanged_and_unrelated_conflicts_fail(tmp_path):
    path = trace(tmp_path)
    state = tmp_path / "state"
    _, legacy, _ = ingest_codex_trace(state, path, schema_version=2)
    assert request(legacy)["observation"]["command_class"] is None
    _, fresh, _ = ingest_codex_trace(state, path)
    changed = deepcopy(request(legacy))
    changed["observation"]["input_sha256"] = "f" * 64
    # Simulate already-stored conflicting historical content, without replacing it.
    changed["event_id"] += "x"
    append_evidence_events(state, [changed])
    with pytest.raises(ValueError, match="projections disagree"):
        read_evidence(state, "codex", "commands")
    changed = deepcopy(request(fresh))
    changed["observation"]["command_class"] = "network"
    with pytest.raises(ValueError, match="same-version evidence projections disagree"):
        append_evidence_events(state, [changed])


def test_orchestration_code_is_not_treated_as_a_direct_command(tmp_path):
    path = trace(
        tmp_path, name="exec", arguments="await tools.exec_command({cmd: 'pytest'})"
    )
    _, events, _ = ingest_codex_trace(tmp_path / "state", path)
    assert request(events)["observation"]["command_class"] is None


def test_evidence_only_read_keeps_legacy_classification(tmp_path, monkeypatch):
    import apu.evidence as module

    path = trace(tmp_path)
    with monkeypatch.context() as old:
        old.setattr(module, "_command_class", lambda *a, **k: None)
        destination, _, _ = ingest_codex_trace(tmp_path / "state", path)
    before = destination.read_bytes()
    assert (
        request(read_evidence(tmp_path / "state", "codex", "commands"))["observation"][
            "command_class"
        ]
        is None
    )
    assert destination.read_bytes() == before


def test_conflicting_known_classifications_are_not_silently_superseded(tmp_path):
    path = trace(tmp_path)
    state = tmp_path / "state"
    _, fresh, _ = ingest_codex_trace(state, path)
    changed = deepcopy(request(fresh))
    changed["event_id"] += "x"
    changed["observation"]["command_class"] = "network"
    append_evidence_events(state, [changed])
    with pytest.raises(ValueError, match="projections disagree"):
        read_evidence(state, "codex", "commands")

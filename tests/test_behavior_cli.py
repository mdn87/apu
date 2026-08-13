from __future__ import annotations

import json
from pathlib import Path

import pytest

from apu.behavior_cli import event_main, intervene_main, watch_main, wtf_main


def _trace(root: Path, cwd: Path) -> Path:
    path = root / "rollout.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {
            "timestamp": "2026-08-12T12:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "cli-session",
                "cwd": str(cwd),
                "source": "vscode",
                "originator": "Codex Desktop",
            },
        },
        {
            "timestamp": "2026-08-12T12:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
        {
            "timestamp": "2026-08-12T12:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "Would you prefer that I choose which file?",
            },
        },
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records),
        encoding="utf-8",
    )
    return path


def test_short_command_flow_marks_diagnoses_and_prepares_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "sessions"
    _trace(traces, cwd)
    monkeypatch.setenv("APU_HOME", str(state))
    monkeypatch.setattr("apu.behavior_watch.shutil.which", lambda _name: "codex")

    assert watch_main([]) == 0
    assert "primary-agent-autonomy-loss: enabled" in capsys.readouterr().out
    assert (
        event_main(
            [
                "asked me to approve a reversible filename choice",
                "--trace-root",
                str(traces),
                "--cwd",
                str(cwd),
            ]
        )
        == 0
    )
    assert "Next: apu-wtf" in capsys.readouterr().out
    assert wtf_main([]) == 0
    assert "likely-autonomy-loss" in capsys.readouterr().out
    assert intervene_main(["--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "Intervention: planned" in output
    assert "Continuation command:" in output


def test_watch_alias_can_disable_and_emit_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.setenv("APU_HOME", str(tmp_path / "state"))
    assert watch_main(["autonomy-loss", "--disable", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["watchers"][0]["enabled"] is False


def test_wtf_can_select_recent_incomplete_run_without_an_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "sessions"
    _trace(traces, cwd)
    monkeypatch.setenv("APU_HOME", str(state))

    assert wtf_main(["--trace-root", str(traces), "--cwd", str(cwd), "--json"]) == 0
    diagnosis = json.loads(capsys.readouterr().out)
    assert diagnosis["status"] == "likely-autonomy-loss"
    assert (state / "behavior" / "latest-incident.json").is_file()

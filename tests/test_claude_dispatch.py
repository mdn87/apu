from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from apu.dispatch import (
    DispatchRejectedError,
    DispatchUnavailableError,
    IsolationProbeRequest,
    RunnerRequest,
)
from apu.dispatch_runtime import ClaudeDispatchRuntime


def fixture(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    staged = stage / "input.md"
    staged.write_text("staged content", encoding="utf-8")
    live = tmp_path / "live.md"
    live.write_text("live unchanged", encoding="utf-8")
    return stage, staged, live


def test_claude_uses_proven_sandbox_and_returns_only_proposals(tmp_path, monkeypatch):
    stage, staged, live = fixture(tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "--print" not in command:
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": {
                        "edits": [{"path": str(live), "content": "proposed"}]
                    },
                }
            ).encode(),
        )

    monkeypatch.setattr("apu.dispatch_runtime.subprocess.run", run)
    monkeypatch.setattr("apu.dispatch_runtime.run_confined", run)
    runtime = ClaudeDispatchRuntime(executable="claude.exe", sandbox_executable="codex")
    probe = runtime.probe(IsolationProbeRequest(stage, live, tmp_path))
    result = runtime.run(
        RunnerRequest("rewrite", stage, {str(live): staged}, probe.context_id)
    )
    assert result == {"files": {str(live): "proposed"}}
    command, options = calls[-1]
    assert command[:2] == ["codex", "sandbox"]
    assert 'permissions.apu-claude-dispatch.extends=":workspace"' in command
    assert "--include-managed-config" in command
    assert (
        'permissions.apu-claude-dispatch.network.domains={"api.anthropic.com"="allow"}'
        in command
    )
    assert "features.network_proxy=true" in command
    assert calls[0][0][: calls[0][0].index("-C")] == command[: command.index("-C")]
    assert command[command.index("-C") + 1] == str(stage.resolve())
    assert command[command.index("--tools") + 1] == ""
    assert "--safe-mode" in command and "--restricted" in command
    assert "--no-session-persistence" in command and "--strict-mcp-config" in command
    assert "--dangerously-skip-permissions" not in command
    assert "staged content" in options["input"].decode()
    assert (
        live.read_text() == "live unchanged" and staged.read_text() == "staged content"
    )
    with pytest.raises(DispatchUnavailableError):
        runtime.run(
            RunnerRequest(
                "retry without probe", stage, {str(live): staged}, probe.context_id
            )
        )


@pytest.mark.parametrize("code", [41, 2])
def test_failed_probe_cannot_authorize_a_run(tmp_path, monkeypatch, code):
    stage, staged, live = fixture(tmp_path)
    monkeypatch.setattr(
        "apu.dispatch_runtime.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=code),
    )
    runtime = ClaudeDispatchRuntime(executable="claude.exe", sandbox_executable="codex")
    probe = runtime.probe(IsolationProbeRequest(stage, live, tmp_path))
    assert not probe.write_denied
    with pytest.raises(DispatchUnavailableError):
        runtime.run(RunnerRequest("work", stage, {str(live): staged}, probe.context_id))


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "structured_output": {"edits": []},
        },
        {
            "type": "result",
            "subtype": "error_max_turns",
            "structured_output": {"edits": []},
        },
        {"type": "result", "subtype": "success", "result": '{"edits":[]}'},
        {
            "type": "result",
            "subtype": "success",
            "structured_output": {"edits": [{"path": "outside", "content": "bad"}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "structured_output": {"edits": "not an array"},
        },
    ],
)
def test_claude_rejects_failed_or_unbound_results(tmp_path, monkeypatch, payload):
    stage, staged, live = fixture(tmp_path)

    def run(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode())

    monkeypatch.setattr("apu.dispatch_runtime.subprocess.run", run)
    monkeypatch.setattr("apu.dispatch_runtime.run_confined", run)
    runtime = ClaudeDispatchRuntime(executable="claude.exe", sandbox_executable="codex")
    probe = runtime.probe(IsolationProbeRequest(stage, live, tmp_path))
    with pytest.raises(DispatchRejectedError):
        runtime.run(RunnerRequest("work", stage, {str(live): staged}, probe.context_id))


def test_claude_rejects_stage_changes_and_outside_inputs(tmp_path, monkeypatch):
    stage, staged, live = fixture(tmp_path)
    monkeypatch.setattr(
        "apu.dispatch_runtime.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0),
    )
    runtime = ClaudeDispatchRuntime(executable="claude.exe", sandbox_executable="codex")
    probe = runtime.probe(IsolationProbeRequest(stage, live, tmp_path))
    with pytest.raises(DispatchUnavailableError):
        runtime.run(
            RunnerRequest("work", tmp_path, {str(live): staged}, probe.context_id)
        )
    probe = runtime.probe(IsolationProbeRequest(stage, live, tmp_path))
    with pytest.raises(DispatchRejectedError):
        runtime.run(RunnerRequest("work", stage, {str(live): live}, probe.context_id))


def test_confined_timeout_stops_child_processes(tmp_path):
    import subprocess
    import sys
    import time

    from apu.dispatch_process import run_confined

    marker = tmp_path / "escaped.txt"
    child = (
        "import time; from pathlib import Path; time.sleep(2); Path("
        + repr(str(marker))
        + ").write_text('late')"
    )
    parent = (
        "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
        + repr(child)
        + "]); time.sleep(20)"
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_confined(
            [sys.executable, "-c", parent], input=b"", timeout=0.5, cwd=tmp_path
        )
    assert time.monotonic() - started < 5
    time.sleep(2)
    assert not marker.exists()

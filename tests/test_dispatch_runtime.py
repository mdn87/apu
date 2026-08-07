from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apu.dispatch import (
    DispatchUnavailableError,
    IsolationProbeRequest,
    RunnerRequest,
)
from apu.dispatch_runtime import CodexDispatchRuntime, runtime_for


def test_probe_uses_same_workspace_policy_and_reports_denied(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    live = tmp_path / "live.txt"
    live.write_text("unchanged", encoding="utf-8")
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs["env"]
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("apu.dispatch_runtime.subprocess.run", fake_run)
    runtime = CodexDispatchRuntime(executable="codex")
    result = runtime.probe(
        IsolationProbeRequest(
            stage_root=stage,
            probe_target=live,
            live_root=tmp_path,
        )
    )

    assert result.attempted is True
    assert result.write_denied is True
    assert result.context_id == result.mechanism
    assert ":workspace" in seen["command"]
    if os.name == "nt":
        assert 'windows.sandbox="unelevated"' in seen["command"]
        assert str(live.resolve()) in seen["command"][-1]
    else:
        assert 'windows.sandbox="unelevated"' not in seen["command"]
        assert seen["env"]["APU_DISPATCH_PROBE_TARGET"] == str(live.resolve())


def test_runner_uses_ephemeral_workspace_and_parses_schema_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stage = tmp_path / "stage"
    staged = stage / "files" / "target.txt"
    staged.parent.mkdir(parents=True)
    staged.write_text("before", encoding="utf-8")
    target = str((tmp_path / "live.txt").resolve())
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["prompt"] = kwargs["input"].decode()
        result_path = Path(command[command.index("--output-last-message") + 1])
        result_path.write_text(
            json.dumps({"edits": [{"path": target, "content": "after"}]}),
            encoding="utf-8",
        )
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("apu.dispatch_runtime.subprocess.run", fake_run)
    runtime = CodexDispatchRuntime(executable="codex")
    value = runtime.run(
        RunnerRequest(
            work_order="work order",
            stage_root=stage,
            staged_files={target: staged},
            isolation_context_id="codex-workspace-write-unelevated-v1",
        )
    )

    assert value == {"files": {target: "after"}}
    assert "--ephemeral" in seen["command"]
    assert "--ignore-user-config" in seen["command"]
    assert "workspace-write" in seen["command"]
    assert json.loads(
        seen["prompt"]
        .split("## Confined staged inputs\n\n", 1)[1]
        .split(
            "\n\nRead only",
            1,
        )[0]
    ) == {target: "files/target.txt"}


def test_runtime_fails_closed_for_unimplemented_claude() -> None:
    with pytest.raises(DispatchUnavailableError, match="Claude"):
        runtime_for("claude")

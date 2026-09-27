from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apu.behavior_cli import (
    EZPZ_DEFAULT_DESCRIPTION,
    event_main,
    ezpz_main,
    intervene_main,
    watch_main,
    wtf_main,
)
from apu.behavior_watch import (
    EASY_DECISION_SIGNAL,
    EASY_DECISION_TEMPLATE_ID,
    RESUME_TEMPLATE_ID,
    intervention_prompt,
    load_incident,
    mark_incident,
)
from apu.cli import main
from apu.models import sha256_bytes


def _trace(root: Path, cwd: Path) -> Path:
    path = root / "rollout.jsonl"
    path.parent.mkdir(parents=True)
    base = datetime.now(UTC) - timedelta(seconds=3)

    def timestamp(offset: int) -> str:
        return (base + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")

    records = [
        {
            "timestamp": timestamp(0),
            "type": "session_meta",
            "payload": {
                "id": "cli-session",
                "cwd": str(cwd),
                "source": "vscode",
                "originator": "Codex Desktop",
            },
        },
        {
            "timestamp": timestamp(1),
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
        {
            "timestamp": timestamp(2),
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
    monkeypatch.chdir(cwd)
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


def test_ezpz_marks_an_easy_decision_and_intervenes_with_decide_and_continue(
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

    assert ezpz_main(["--trace-root", str(traces), "--cwd", str(cwd), "--json"]) == 0
    diagnosis = json.loads(capsys.readouterr().out)
    assert diagnosis["status"] == "likely-autonomy-loss"
    assert EASY_DECISION_SIGNAL in diagnosis["observed_signals"]
    assert diagnosis["possible_barriers"] == []
    recommendation = diagnosis["recommended_intervention"]
    assert recommendation["template_id"] == EASY_DECISION_TEMPLATE_ID
    assert recommendation["prompt_sha256"] == sha256_bytes(
        intervention_prompt(EASY_DECISION_TEMPLATE_ID).encode("utf-8")
    )
    assert recommendation["durable_policy_mutation"] is False

    incident = load_incident(state)
    assert incident["description"] == EZPZ_DEFAULT_DESCRIPTION
    assert incident["claim"]["asserted_signals"] == [EASY_DECISION_SIGNAL]

    assert intervene_main(["--dry-run", "--json"]) == 0
    intervention = json.loads(capsys.readouterr().out)
    assert intervention["prompt_template_id"] == EASY_DECISION_TEMPLATE_ID
    assert intervention["prompt_sha256"] == recommendation["prompt_sha256"]
    assert intervention["status"] == "planned"


def test_ezpz_text_output_names_the_template_and_next_step(
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

    assert (
        ezpz_main(
            [
                "asked which of two equivalent test file names to use",
                "--trace-root",
                str(traces),
                "--cwd",
                str(cwd),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "marked now" in output
    assert f"Attested: {EASY_DECISION_SIGNAL}" in output
    assert f"Resume template: {EASY_DECISION_TEMPLATE_ID}" in output
    assert "Next: apu-intervene" in output


def test_ezpz_attestation_never_overrides_a_barrier(
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

    assert (
        ezpz_main(
            [
                "stopped to ask for an API key it did not have",
                "--trace-root",
                str(traces),
                "--cwd",
                str(cwd),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "possible-legitimate-barrier" in output
    assert "contradicts the easy-decision attestation" in output
    assert "Next: apu-intervene" not in output
    assert intervene_main(["--dry-run"]) == 1
    assert "legitimate barrier" in capsys.readouterr().err


def test_wtf_diagnosis_keeps_the_general_resume_template(
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
    assert diagnosis["recommended_intervention"]["template_id"] == RESUME_TEMPLATE_ID
    assert load_incident(state)["claim"]["asserted_signals"] == []


def test_mark_incident_rejects_unknown_asserted_signals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "sessions"
    _trace(traces, cwd)
    monkeypatch.setenv("APU_HOME", str(state))

    with pytest.raises(ValueError, match="unknown asserted signal: made-up"):
        mark_incident(
            state,
            "anything",
            trace_root=traces,
            cwd=cwd,
            asserted_signals=("made-up",),
        )
    with pytest.raises(ValueError, match="unknown intervention template"):
        intervention_prompt("no-such-template")


def test_event_cli_returns_nonzero_for_no_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    state = tmp_path / "state"
    requested = tmp_path / "requested"
    other = tmp_path / "other"
    requested.mkdir()
    other.mkdir()
    traces = tmp_path / "sessions"
    _trace(traces, other)
    monkeypatch.setenv("APU_HOME", str(state))

    assert (
        event_main(
            [
                "stopped before completing the requested task",
                "--trace-root",
                str(traces),
                "--cwd",
                str(requested),
            ]
        )
        == 2
    )
    assert "no_attribution: no_exact_cwd_candidate" in capsys.readouterr().err
    assert not (state / "behavior" / "latest-incident.json").exists()


def test_wtf_default_does_not_reuse_another_directorys_incident(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    state = tmp_path / "state"
    other = tmp_path / "other"
    current = tmp_path / "current"
    other.mkdir()
    current.mkdir()
    traces = tmp_path / "sessions"
    _trace(traces, other)
    monkeypatch.setenv("APU_HOME", str(state))
    _, incident = mark_incident(
        state,
        "asked me to choose a filename",
        trace_root=traces,
        cwd=other,
    )
    monkeypatch.chdir(current)
    # Empty provider homes keep selection isolated from real user sessions.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-claude"))

    assert wtf_main(["--json"]) == 2
    assert json.loads(capsys.readouterr().out)["kind"] == "no_attribution"
    assert load_incident(state)["incident_id"] == incident["incident_id"]
    assert not (state / "behavior" / "latest-diagnosis.json").exists()


@pytest.mark.parametrize("command", ["wtf", "ezpz"])
def test_spaced_commands_diagnose_and_pin_the_next_action(
    command: str,
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
    selectors = ["--trace-root", str(traces), "--cwd", str(cwd)]

    assert main([command, *selectors]) == 0
    output = capsys.readouterr().out
    assert "Session: cli-session (codex)" in output
    assert f"Directory: {cwd}" in output
    assert "The agent appears to have paused on work it could continue." in output
    assert "clues, not proof" in output
    continuation = next(
        line for line in output.splitlines() if line.startswith("Next: ")
    )
    diagnosis_id = continuation.split("--diagnosis ")[1]

    # A newer diagnosis must not redirect the printed continuation command.
    assert main([command, *selectors, "--json"]) == 0
    newer = json.loads(capsys.readouterr().out)
    assert newer["diagnosis_id"] != diagnosis_id
    assert intervene_main(["--diagnosis", diagnosis_id, "--dry-run", "--json"]) == 0
    intervention = json.loads(capsys.readouterr().out)
    assert intervention["diagnosis_id"] == diagnosis_id
    assert intervention["executed"] is False


def test_wtf_fresh_reads_new_evidence_and_explicit_incident_can_cross_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    traces = tmp_path / "codex" / "sessions"
    trace = _trace(traces, cwd)
    monkeypatch.setenv("APU_HOME", str(state))
    monkeypatch.setenv("CODEX_HOME", str(traces.parent))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-claude"))
    monkeypatch.chdir(cwd)
    assert wtf_main(["--json"]) == 0
    original = json.loads(capsys.readouterr().out)
    assert wtf_main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["incident_id"] == original["incident_id"]

    trace.write_text(
        trace.read_text(encoding="utf-8").replace(
            "Would you prefer that I choose which file?",
            "I am checking the implementation.",
        ),
        encoding="utf-8",
    )
    assert main(["wtf", "--fresh", "--json"]) == 0
    fresh = json.loads(capsys.readouterr().out)
    assert fresh["incident_id"] != original["incident_id"]
    assert fresh["status"] == "insufficient-evidence"
    _, barrier_incident = mark_incident(
        state,
        "stopped to ask for an API key",
        trace_root=traces,
        cwd=cwd,
    )

    monkeypatch.chdir(tmp_path)
    assert main(["wtf", "--incident", barrier_incident["incident_id"]]) == 0
    output = capsys.readouterr().out
    assert "Possible barriers: credential-barrier" in output
    assert "Review the barrier and its evidence" in output
    assert "Next: apu-intervene" not in output


@pytest.mark.parametrize("command", ["wtf", "ezpz"])
def test_spaced_commands_explain_attribution_failure_and_preserve_json(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.setenv("APU_HOME", str(tmp_path / "state"))
    args = [command, "--provider", "codex", "--trace-root", str(tmp_path / "missing")]
    assert main(args) == 2
    output = capsys.readouterr()
    assert not output.out
    assert f"apu {command}: no_attribution: trace_root_unavailable" in output.err
    assert "set --trace-root" in output.err
    assert main([*args, "--json"]) == 2
    output = capsys.readouterr()
    assert not output.err
    result = json.loads(output.out)
    assert set(result) == {"kind", "reason_code", "provenance"}
    assert result["reason_code"] == "trace_root_unavailable"


def test_insufficient_evidence_asks_for_context_instead_of_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.setenv("APU_HOME", str(tmp_path / "state"))
    trace = _trace(tmp_path / "sessions", tmp_path)
    trace.write_text(
        trace.read_text(encoding="utf-8").replace(
            "Would you prefer that I choose which file?",
            "I am checking the implementation.",
        ),
        encoding="utf-8",
    )
    assert main(["wtf", "--trace-root", str(trace.parent), "--cwd", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "insufficient-evidence" in output
    assert "Next: describe the stop" in output
    assert "Next: apu-intervene" not in output


@pytest.mark.parametrize("selector", ["--cwd", "--session-id", "--trace-root"])
def test_wtf_rejects_ignored_selectors_with_saved_incident(
    selector: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("APU_HOME", str(state))
    assert main(["wtf", "--incident", "incident-example", selector, "example"]) == 1
    assert "--incident cannot be combined" in capsys.readouterr().err
    assert not state.exists()

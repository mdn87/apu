from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from apu.cli import main
from apu.evidence import (
    append_evidence_events,
    evidence_path,
    ingest_codex_trace,
    ingest_hook_event,
    normalize_hook_event,
    observe_repository_state,
    read_evidence,
    reconcile_evidence,
    validate_evidence_event,
    verify_evidence_source,
)


def _codex_trace(path: Path, cwd: Path) -> Path:
    records = [
        {
            "timestamp": "2026-08-14T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "evidence-session",
                "cwd": str(cwd),
                "base_instructions": "private base instructions",
            },
        },
        {
            "timestamp": "2026-08-14T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
        {
            "timestamp": "2026-08-14T10:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "call-1",
                "arguments": '{"command":"pytest --token private-value"}',
            },
        },
        {
            "timestamp": "2026-08-14T10:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "Exit code: 1\nprivate failure output",
            },
        },
        {
            "timestamp": "2026-08-14T10:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "call-2",
                "arguments": '{"command":"pytest --token private-value"}',
            },
        },
        {
            "timestamp": "2026-08-14T10:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-2",
                "output": "Exit code: 1\nprivate failure output",
            },
        },
    ]
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def test_codex_ingestion_is_content_minimized_correlated_and_replayable(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    trace = _codex_trace(tmp_path / "sessions" / "rollout.jsonl", cwd)

    path, events, boundary = ingest_codex_trace(state, trace)

    assert path is not None and path.is_file()
    assert boundary["event_count"] == 6
    assert boundary["appended_count"] == 6
    reconciliation = reconcile_evidence(events)
    assert reconciliation["paired_tool_calls"] == 2
    assert reconciliation["reason_codes"] == ["repeated-identical-tool-failure"]
    encoded = path.read_text(encoding="utf-8")
    assert "private-value" not in encoded
    assert "private failure output" not in encoded
    assert "private base instructions" not in encoded

    with trace.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "timestamp": "2026-08-14T10:00:06Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete"},
                }
            )
            + "\n"
        )
    assert {verify_evidence_source(event)["status"] for event in events} == {"verified"}

    _, expanded, expanded_boundary = ingest_codex_trace(state, trace)
    assert expanded_boundary["appended_count"] == 1
    assert len(read_evidence(state, "codex", "evidence-session")) == 7
    assert expanded[-1]["event_type"] == "task.completed"


def test_source_verification_detects_changed_prefix(tmp_path: Path) -> None:
    state = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    trace = _codex_trace(tmp_path / "sessions" / "rollout.jsonl", cwd)
    _, events, _ = ingest_codex_trace(state, trace)

    content = trace.read_text(encoding="utf-8")
    trace.write_text(content.replace("evidence-session", "different-session", 1))

    result = verify_evidence_source(events[0])
    assert result["status"] == "contradicted"
    assert result["reason_codes"] == ["source-prefix-changed"]


def test_hook_ingestion_projects_only_safe_metadata(tmp_path: Path) -> None:
    state = tmp_path / "state"
    transcript = tmp_path / "private-transcript.jsonl"
    payload = {
        "session_id": "claude-session",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "tool_use_id": "toolu-private-id",
        "tool_name": "Bash",
        "tool_input": {"command": "echo secret-hook-input"},
        "tool_response": "secret-hook-output",
        "duration_ms": 42,
        "unrecognized_private_field": "must not persist",
    }

    path, event, appended = ingest_hook_event(
        state, "claude-code", "PostToolUse", payload
    )

    assert appended is True
    assert event["event_type"] == "tool.completed"
    assert event["correlation_sha256"]
    assert event["observation"]["input_sha256"]
    assert event["observation"]["result_sha256"]
    assert event["observation"]["command_class"] == "shell"
    encoded = path.read_text(encoding="utf-8") if path else ""
    assert "secret-hook-input" not in encoded
    assert "secret-hook-output" not in encoded
    assert "toolu-private-id" not in encoded
    assert "must not persist" not in encoded

    _, duplicate, duplicate_appended = ingest_hook_event(
        state, "claude-code", "PostToolUse", payload
    )
    assert duplicate["event_id"] == event["event_id"]
    assert duplicate_appended is False

    invalid = dict(event)
    invalid["raw_payload"] = payload
    with pytest.raises(ValueError, match="fields do not match"):
        validate_evidence_event(invalid)


@pytest.mark.parametrize(
    "provider",
    (
        "",
        ".",
        "..",
        "../escape",
        "nested/provider",
        r"nested\provider",
        "C:escape",
        r"C:\escape",
        r"\\server\share",
        "provider.",
        "provider ",
        "provider\nname",
        "provider\0name",
        "CON",
        "con.txt",
        "PRN",
        "AUX.log",
        "nul",
        "COM1",
        "com9.trace",
        "LPT1",
        "lpt9.json",
        "CODEX",
        "x" * 129,
    ),
)
def test_provider_component_rejects_portable_path_and_device_forms(
    tmp_path: Path, provider: str
) -> None:
    state = tmp_path / "state"
    payload = {
        "session_id": "provider-boundary-session",
        "cwd": str(tmp_path),
    }

    with pytest.raises(ValueError, match="provider"):
        evidence_path(state, provider, "provider-boundary-session")
    with pytest.raises(ValueError, match="provider"):
        normalize_hook_event(provider, "Stop", payload)

    event = normalize_hook_event("fixture.v2", "Stop", payload)
    event["provider"] = provider
    with pytest.raises(ValueError, match="provider"):
        validate_evidence_event(event)
    with pytest.raises(ValueError, match="provider"):
        append_evidence_events(state, [event])

    assert not list(tmp_path.rglob("*.jsonl"))
    assert not list(tmp_path.rglob("*.lock"))
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_portable_provider_components_keep_all_evidence_sidecars_contained(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    evidence_root = (state / "behavior" / "evidence").resolve(strict=False)

    for index, provider in enumerate(("codex", "claude-code", "openai", "fixture.v2")):
        path, event, appended = ingest_hook_event(
            state,
            provider,
            "Stop",
            {
                "session_id": f"normal-provider-{index}",
                "cwd": str(tmp_path),
            },
        )
        assert appended is True
        assert event["provider"] == provider
        assert path is not None
        provider_directory = path.parent.resolve(strict=True)
        assert provider_directory.parent == evidence_root
        assert provider_directory.name == provider
        for artifact in provider_directory.iterdir():
            assert artifact.resolve(strict=True).parent == provider_directory


def test_existing_provider_directory_symlink_cannot_redirect_sidecars(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    evidence_root = state / "behavior" / "evidence"
    evidence_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    provider_directory = evidence_root / "codex"
    try:
        provider_directory.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    payload = {"session_id": "symlink-session", "cwd": str(tmp_path)}
    with pytest.raises(ValueError, match="provider"):
        evidence_path(state, "codex", "symlink-session")
    with pytest.raises(ValueError, match="provider"):
        ingest_hook_event(state, "codex", "Stop", payload)

    assert not list(outside.iterdir())
    assert not list(outside.rglob("*.jsonl"))
    assert not list(outside.rglob("*.lock"))
    assert not list(outside.rglob("*.sqlite3"))


def test_existing_evidence_root_symlink_cannot_redirect_sidecars(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    behavior_root = state / "behavior"
    behavior_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (behavior_root / "evidence").symlink_to(
            outside,
            target_is_directory=True,
        )
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    with pytest.raises(ValueError, match="evidence root"):
        ingest_hook_event(
            state,
            "codex",
            "Stop",
            {"session_id": "root-symlink-session", "cwd": str(tmp_path)},
        )

    assert not list(outside.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows junction assertion")
def test_existing_windows_junction_cannot_redirect_evidence_directories(
    tmp_path: Path,
) -> None:
    try:
        import _winapi
    except ImportError:
        pytest.skip("the Python runtime cannot create junctions")
    create_junction = getattr(_winapi, "CreateJunction", None)
    if create_junction is None:
        pytest.skip("the Python runtime cannot create junctions")

    for component in ("behavior", "evidence", "provider"):
        state = tmp_path / component / "state"
        outside = tmp_path / component / "outside"
        outside.mkdir(parents=True)
        if component == "behavior":
            state.mkdir(parents=True)
            redirected = state / "behavior"
        elif component == "evidence":
            redirected = state / "behavior" / "evidence"
            redirected.parent.mkdir(parents=True)
        else:
            redirected = state / "behavior" / "evidence" / "codex"
            redirected.parent.mkdir(parents=True)
        try:
            create_junction(str(outside), str(redirected))
        except OSError as error:
            pytest.skip(f"junction creation is unavailable: {error}")

        with pytest.raises(ValueError, match="redirect"):
            ingest_hook_event(
                state,
                "codex",
                "Stop",
                {"session_id": f"junction-{component}", "cwd": str(tmp_path)},
            )
        assert not list(outside.iterdir())


@pytest.mark.parametrize("artifact", ("jsonl", "index.sqlite3"))
def test_existing_evidence_artifact_symlink_cannot_redirect_writes(
    tmp_path: Path,
    artifact: str,
) -> None:
    state = tmp_path / "state"
    path = evidence_path(state, "codex", "artifact-symlink-session")
    path.parent.mkdir(parents=True)
    redirected = tmp_path / f"redirected-{artifact}"
    selected = (
        path if artifact == "jsonl" else path.with_name(f"{path.name}.index.sqlite3")
    )
    try:
        selected.symlink_to(redirected)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"file symlinks unavailable: {error}")

    with pytest.raises(ValueError, match="evidence artifact"):
        ingest_hook_event(
            state,
            "codex",
            "Stop",
            {"session_id": "artifact-symlink-session", "cwd": str(tmp_path)},
        )

    assert not redirected.exists()


def test_existing_evidence_artifact_hardlink_cannot_redirect_writes(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    path = evidence_path(state, "codex", "artifact-hardlink-session")
    path.parent.mkdir(parents=True)
    redirected = tmp_path / "redirected.jsonl"
    redirected.write_text("outside\n", encoding="utf-8")
    try:
        os.link(redirected, path)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"hard links unavailable: {error}")

    with pytest.raises(ValueError, match="evidence artifact"):
        ingest_hook_event(
            state,
            "codex",
            "Stop",
            {"session_id": "artifact-hardlink-session", "cwd": str(tmp_path)},
        )

    assert redirected.read_text(encoding="utf-8") == "outside\n"


def test_repository_observation_hashes_changed_paths(tmp_path: Path) -> None:
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "apu@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "APU Test"], cwd=repo, check=True)
    tracked = repo / "private-name.txt"
    tracked.write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "private-name.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "test: initial"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    tracked.write_text("changed\n", encoding="utf-8")

    path, event, appended = observe_repository_state(
        state,
        provider="claude-code",
        session_id="state-session",
        cwd=repo,
        observed_at="2026-08-14T10:10:00Z",
    )

    assert appended is True
    assert event["state"]["repository_available"] is True
    assert event["state"]["head_sha"]
    assert event["state"]["tree_sha"]
    assert event["state"]["dirty"] is True
    assert len(event["state"]["changed_path_sha256"]) == 1
    assert "private-name.txt" not in (path.read_text(encoding="utf-8") if path else "")


def test_concurrent_hook_ingestion_is_locked_and_deduplicated(tmp_path: Path) -> None:
    state = tmp_path / "state"
    payload = {
        "session_id": "concurrent-session",
        "cwd": str(tmp_path),
        "hook_event_name": "Stop",
        "sequence": 9,
    }
    workers = 8
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(
            executor.map(
                lambda _index: ingest_hook_event(state, "claude-code", "Stop", payload),
                range(workers),
            )
        )

    assert sum(int(appended) for _, _, appended in results) == 1
    assert len(read_evidence(state, "claude-code", "concurrent-session")) == 1


def test_evidence_cli_ingests_hook_json(tmp_path: Path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    payload_path = tmp_path / "hook.json"
    payload_path.write_text(
        json.dumps(
            {
                "session_id": "cli-hook-session",
                "cwd": str(tmp_path),
                "tool_use_id": "tool-1",
                "tool_name": "Bash",
                "tool_input": {"command": "pytest"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APU_HOME", str(state))

    assert (
        main(
            [
                "evidence",
                "ingest-hook",
                "--provider",
                "claude-code",
                "--event",
                "PreToolUse",
                "--input",
                str(payload_path),
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["event_type"] == "tool.requested"
    assert result["appended"] is True

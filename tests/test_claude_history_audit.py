from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from apu.behavior_audit import audit_behavior
from apu.cli import main

NOW = datetime(2026, 10, 24, 3, tzinfo=UTC)


def trace(path, cwd, sid="claude-history", *, when=NOW, other_cwd=None):
    records = []
    if other_cwd:
        records.append(
            {
                "type": "user",
                "cwd": str(other_cwd),
                "sessionId": sid,
                "message": {"role": "user", "content": "private other project"},
            }
        )
    common = {"cwd": str(cwd), "sessionId": sid, "timestamp": when.isoformat()}
    records.extend(
        [
            {
                **common,
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-1",
                            "name": "Bash",
                            "input": {"command": "private command"},
                        }
                    ],
                },
            },
            {
                **common,
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-1",
                            "content": "private output",
                        }
                    ],
                },
            },
            {
                **common,
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "stop_reason": "end_turn",
                    "content": [],
                },
            },
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    os.utime(path, (when.timestamp(), when.timestamp()))
    return path


def audit(tmp_path, root, **options):
    return audit_behavior(
        tmp_path / "state",
        cwd=tmp_path,
        providers=("claude-code",),
        claude_trace_root=root,
        gate_log=None,
        now=NOW,
        **options,
    )


def test_discovers_verifies_and_reuses_primary_history_without_preingestion(tmp_path):
    root = tmp_path / "projects"
    primary = trace(
        root / "untrusted-slug" / "session.jsonl", tmp_path, other_cwd=tmp_path / "old"
    )
    trace(
        root / "untrusted-slug" / "session" / "subagents" / "agent.jsonl",
        tmp_path,
        "child",
    )
    trace(root / "other" / "foreign.jsonl", tmp_path / "foreign", "foreign")
    _, report = audit(tmp_path, root)
    assert [s["session_id"] for s in report["sessions"]] == ["claude-history"]
    session = report["sessions"][0]
    assert session["source_kind"] == "transcript"
    assert session["ingest_status"] == "ingested"
    assert session["reconciliation"]["paired_tool_calls"] == 1
    assert session["verification_counts"] == {"verified": session["event_count"]}
    assert report["selection"]["source_bytes"] == primary.stat().st_size
    evidence = next(
        (tmp_path / "state/behavior/evidence/v3/claude-code").glob("*.jsonl")
    )
    old_bytes = evidence.read_bytes()
    for secret in ("private command", "private output", "private other project"):
        assert secret not in evidence.read_text()
        assert secret not in json.dumps(report)
    assert all(
        json.loads(line)["state"]["cwd"] == str(tmp_path)
        for line in evidence.read_text().splitlines()
    )
    _, second = audit(tmp_path, root)
    assert second["sessions"][0]["ingest_status"] == "unchanged-reused"
    assert evidence.read_bytes() == old_bytes


def test_lookback_explicit_selection_and_byte_cap(tmp_path):
    root = tmp_path / "projects"
    old = trace(
        root / "project/old.jsonl", tmp_path, "old", when=NOW - timedelta(days=30)
    )
    trace(root / "project/new.jsonl", tmp_path, "new")
    _, ordinary = audit(tmp_path, root)
    assert [s["session_id"] for s in ordinary["sessions"]] == ["new"]
    _, explicit = audit(tmp_path, root, session_id="old")
    assert [s["session_id"] for s in explicit["sessions"]] == ["old"]
    _, capped = audit(
        tmp_path, root, session_id="old", source_byte_limit=old.stat().st_size - 1
    )
    assert capped["selection"]["audited_session_count"] == 0
    assert any(
        s["reason"] == "source-byte-limit" for s in capped["selection"]["skipped"]
    )


def test_metadata_limit_prioritizes_explicit_session_without_full_scan(
    tmp_path, monkeypatch
):
    import apu.behavior_audit as module

    root = tmp_path / "projects"
    trace(
        root / "project/older.jsonl", tmp_path, "older", when=NOW - timedelta(days=30)
    )
    trace(root / "project/newer.jsonl", tmp_path, "newer")
    monkeypatch.setattr(module, "_DISCOVERY_FILE_LIMIT", 1)
    _, report = audit(tmp_path, root, session_id="older")
    assert [s["session_id"] for s in report["sessions"]] == ["older"]
    assert any(
        s["reason"] == "discovery-file-limit" for s in report["selection"]["skipped"]
    )


def test_cli_uses_claude_config_root(tmp_path, monkeypatch, capsys):
    config = tmp_path / "claude"
    trace(config / "projects/slug/session.jsonl", tmp_path, when=datetime.now(UTC))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("APU_HOME", str(tmp_path / "state"))
    assert (
        main(
            [
                "behavior",
                "audit",
                "--provider",
                "claude-code",
                "--cwd",
                str(tmp_path),
                "--no-gate-log",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["sessions"][0]["provider"] == "claude-code"


def test_metadata_missing_in_bounded_tail_is_skipped(tmp_path):
    root = tmp_path / "projects"
    path = trace(root / "project/session.jsonl", tmp_path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"opaque": "x" * (1024 * 1024 + 1)}) + "\n")
    _, report = audit(tmp_path, root)
    assert not report["sessions"]
    assert any(
        s["reason"] == "session-metadata-unavailable"
        for s in report["selection"]["skipped"]
    )


@pytest.mark.parametrize("mutation", ["grow", "cwd", "session", "malformed"])
def test_changed_or_invalid_source_is_skipped_without_evidence(
    tmp_path, monkeypatch, mutation
):
    import apu.behavior_audit as module

    root = tmp_path / "projects"
    path = trace(root / "project/changed.jsonl", tmp_path, "changed")
    trace(root / "project/valid.jsonl", tmp_path, "valid")
    real_ingest = module.ingest_claude_code_trace

    def change_before_ingestion(state, selected, **kwargs):
        if selected == path:
            if mutation == "grow":
                with path.open("ab") as stream:
                    stream.write(b"{}\n")
            elif mutation == "cwd":
                path.write_text(
                    path.read_text().replace(
                        str(tmp_path).replace("\\", "\\\\"),
                        str(tmp_path.parent).replace("\\", "\\\\"),
                    )
                )
            elif mutation == "session":
                path.write_text(
                    path.read_text().replace(
                        '"sessionId": "changed"', '"sessionId": "another"'
                    )
                )
            else:
                path.write_text(
                    path.read_text().replace(
                        '"sessionId": "changed"', '"sessionId": "another"', 1
                    )
                )
        return real_ingest(state, selected, **kwargs)

    monkeypatch.setattr(module, "ingest_claude_code_trace", change_before_ingestion)
    _, report = audit(tmp_path, root)
    assert [s["session_id"] for s in report["sessions"]] == ["valid"]
    assert report["selection"]["audited_session_count"] == 1
    assert any(
        s["reason"] == "source-ingestion-failed" and s["session_id"] == "changed"
        for s in report["selection"]["skipped"]
    )
    from apu.evidence import read_evidence

    assert not read_evidence(tmp_path / "state", "claude-code", "changed")
    assert not read_evidence(tmp_path / "state", "claude-code", "another")


def test_shared_provider_session_budget_and_explicit_cli_root(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "projects"
    trace(root / "slug/claude.jsonl", tmp_path, "claude", when=datetime.now(UTC))
    codex = tmp_path / "codex"
    codex.mkdir()
    (codex / "codex.jsonl").write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"id": "codex", "cwd": str(tmp_path)}}
        )
        + "\n"
    )
    monkeypatch.setenv("APU_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "missing"))
    assert (
        main(
            [
                "behavior",
                "audit",
                "--cwd",
                str(tmp_path),
                "--sessions",
                "1",
                "--trace-root",
                str(codex),
                "--claude-trace-root",
                str(root),
                "--no-gate-log",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["selection"]["discovered_session_count"] == 2
    assert report["selection"]["audited_session_count"] == 1
    assert any(s["reason"] == "session-limit" for s in report["selection"]["skipped"])


def test_ingestion_rechecks_snapshot_hash_before_appending(tmp_path, monkeypatch):
    import apu.evidence as module

    path = trace(tmp_path / "session.jsonl", tmp_path)
    real_snapshot = module._claude_code_snapshot

    def change_after_snapshot(selected, **kwargs):
        boundary = real_snapshot(selected, **kwargs)
        selected.write_bytes(
            selected.read_bytes().replace(b"private command", b"changed command")
        )
        return boundary

    monkeypatch.setattr(module, "_claude_code_snapshot", change_after_snapshot)
    with pytest.raises(ValueError, match="changed during ingestion"):
        module.ingest_claude_code_trace(tmp_path / "state", path)
    assert not module.read_evidence(tmp_path / "state", "claude-code", "claude-history")

from __future__ import annotations

import json
from pathlib import Path

from apu.audit import build_inventory
from apu.classify import DetectorPolicy
from apu.trace import summarize_sessions


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_audit_builds_findings_and_sanitized_trace_summary(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    agents = write(
        repo / "AGENTS.md",
        "You must invoke a workflow skill at the start of every conversation.\n",
    )
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    records = [
        {
            "timestamp": "2026-08-06T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "root", "parent_thread_id": None},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "arguments": '{"prompt":"secret prompt"}',
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": 123}},
            },
        },
    ]
    write(
        sessions / "root.jsonl",
        "\n".join(json.dumps(record) for record in records),
    )

    inventory = build_inventory(
        [repo],
        home=home,
        working_directories=[repo],
        session_paths=[sessions],
        root_session_id="root",
        generated_at="2026-08-06T11:00:00Z",
    )

    assert any(surface.path == str(agents) for surface in inventory.surfaces)
    assert any(
        finding.category == "universal-skill-trigger" for finding in inventory.findings
    )
    assert inventory.evidence_summary["sessions"]["sessions"] == 1
    encoded = json.dumps(inventory.to_dict())
    assert "secret prompt" not in encoded
    assert "arguments" not in encoded


def test_session_summary_selects_descendants_without_message_content(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for session_id, parent in (
        ("root", None),
        ("child", "root"),
        ("unrelated", None),
    ):
        records = [
            {
                "timestamp": "2026-08-06T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": session_id, "parent_thread_id": parent},
            },
            {
                "timestamp": "2026-08-06T10:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "wait_agent",
                    "input": "private message",
                },
            },
        ]
        write(
            sessions / f"{session_id}.jsonl",
            "\n".join(json.dumps(record) for record in records),
        )

    summary = summarize_sessions([sessions], root_session_id="root")

    assert summary["sessions"] == 2
    assert summary["descendants"] == 1
    assert summary["tool_calls"] == {"wait_agent": 2}
    assert "private message" not in json.dumps(summary)


def test_audit_finding_delta_can_be_caused_by_guidance_detector_policy(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    write(
        repo / "AGENTS.md",
        "Run focused tests now.\nRun focused tests now.\n",
    )

    before = build_inventory(
        [repo],
        home=home,
        generated_at="2026-08-07T10:00:00Z",
    )
    after = build_inventory(
        [repo],
        home=home,
        generated_at="2026-08-07T10:00:00Z",
        detector_policy=DetectorPolicy(
            duplicate_instruction_minimum_words=4,
        ),
    )

    assert before.findings == ()
    assert [finding.category for finding in after.findings] == ["duplicate-instruction"]

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apu.behavior_audit import audit_behavior
from apu.cli import main as apu_main
from apu.gate_decisions import (
    DEFAULT_GATE_LOG,
    gate_cost,
    gate_cost_report,
    parse_gate_log,
)

SID_A = "a947bd78-e4d7-471a-971d-b090c698ba37"
SID_B = "3d0a223b-bf72-433d-94d9-be551ad561a4"
BASE_MS = int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000)


def _line(offset_ms: int, event: str, detail: str = "") -> str:
    return f"{BASE_MS + offset_ms}\t{event}\t{detail}"


def _write_log(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _session_a_lines() -> list[str]:
    """Objective 0: request, three read-only turns while pending, then approval.

    Objective 1: one mixed turn, a halt, one read-only turn while pending,
    re-approval, work. Session B is an unrelated session with no decisions.
    """

    a8 = SID_A[:8]
    return [
        # Turn 1: new request -> pending; plan-only turn (no tools).
        _line(0, "prompt", f"{SID_A} Select the case and SECRET-PROMPT-TEXT"),
        _line(10, "gate", f"{a8} none->pending"),
        _line(500, "stop", SID_A),
        # Turn 2: question, state untouched; read-only tools.
        _line(1_000, "prompt", f"{SID_A} what would the audit count?"),
        _line(1_100, "tool", f"{SID_A} Read"),
        _line(1_200, "tool", f"{SID_A} Grep"),
        _line(1_500, "stop", SID_A),
        # Turn 3: statement re-arms; read-only again.
        _line(2_000, "prompt", f"{SID_A} the voice commands go elsewhere"),
        _line(2_010, "gate", f"{a8} pending->pending"),
        _line(2_100, "tool", f"{SID_A} Glob"),
        _line(2_500, "stop", SID_A),
        # Turn 4: approval opens objective 1; mutating work.
        _line(3_000, "prompt", f"{SID_A} go ahead"),
        _line(3_010, "gate", f"{a8} pending->approved"),
        _line(3_100, "tool", f"{SID_A} Bash"),
        _line(3_200, "tool", f"{SID_A} Edit"),
        _line(3_500, "stop", SID_A),
        # Turn 5: halt re-arms; read-only.
        _line(4_000, "prompt", f"{SID_A} hold on"),
        _line(4_010, "gate", f"{a8} approved->pending"),
        _line(4_100, "tool", f"{SID_A} Read"),
        _line(4_500, "stop", SID_A),
        # Turn 6: approval opens objective 2; abandoned turn (no stop).
        _line(5_000, "prompt", f"{SID_A} go ahead"),
        _line(5_010, "gate", f"{a8} pending->approved"),
        _line(5_100, "tool", f"{SID_A} Bash"),
        # Session B: no gate decisions at all.
        _line(100, "prompt", f"{SID_B} hello there"),
        _line(150, "tool", f"{SID_B} mcp__lugos__lugos_agents"),
        _line(200, "stop", SID_B),
        # Noise the parser must survive.
        "garbage line without tabs",
        _line(6_000, "utterance-end", ""),
        _line(6_100, "gate", "zzzzzzzz none->pending"),  # no such session
        _line(6_200, "gate", f"{a8} bogus->pending"),  # invalid state
    ]


def test_parse_drops_prompt_text_and_keeps_only_safe_fields(tmp_path: Path) -> None:
    log = _write_log(tmp_path / "latency.log", _session_a_lines())
    events, boundary = parse_gate_log(log)
    serialized = json.dumps(events)
    assert "SECRET-PROMPT-TEXT" not in serialized
    assert "hello" not in serialized
    kinds = {event["kind"] for event in events}
    assert kinds == {"prompt", "tool", "stop", "gate"}
    assert boundary["invalid_line_count"] == 2  # garbage line, bogus state
    assert boundary["truncated"] is False
    assert all(set(event) <= {"ts", "kind", "session_id", "tool", "session_prefix", "before", "after"} for event in events)


def test_gate_cost_computes_i3_per_objective(tmp_path: Path) -> None:
    log = _write_log(tmp_path / "latency.log", _session_a_lines())
    events, _ = parse_gate_log(log)
    report = gate_cost(events)

    by_id = {item["session_id"]: item for item in report["sessions"]}
    a = by_id[SID_A]
    assert a["decisions_joined"] is True
    assert a["decision_count"] == 5
    assert a["transitions"] == {
        "approved->pending": 1,
        "none->pending": 1,
        "pending->approved": 2,
        "pending->pending": 1,
    }
    assert a["turn_count"] == 6 and a["completed_turn_count"] == 5
    objectives = {item["index"]: item for item in a["objectives"]}
    assert objectives[0]["pending_readonly_turns"] == 3
    assert objectives[0]["pending_plan_only_turns"] == 1
    assert objectives[1]["pending_readonly_turns"] == 1  # the halt turn
    assert objectives[1]["turns"] == 2
    assert objectives[2]["pending_readonly_turns"] == 0
    assert a["i3_max"] == 3 and a["i3_total"] == 4 and a["i3_over_target"] is True

    b = by_id[SID_B]
    assert b["decision_count"] == 0 and b["decisions_joined"] is False
    assert b["i3_max"] == 0 and b["i3_over_target"] is False

    summary = report["summary"]
    assert summary["session_count"] == 2
    assert summary["sessions_with_decisions"] == 1
    assert summary["unmatched_prefix_decision_count"] == 1  # zzzzzzzz
    assert summary["sessions_over_target"] == [SID_A]
    assert "SECRET-PROMPT-TEXT" not in json.dumps(report)


def test_ambiguous_prefix_never_joins_decisions(tmp_path: Path) -> None:
    twin = SID_A[:8] + "-twin-session"
    lines = _session_a_lines() + [
        _line(7_000, "prompt", f"{twin} another session sharing the prefix"),
        _line(7_500, "stop", twin),
    ]
    events, _ = parse_gate_log(_write_log(tmp_path / "latency.log", lines))
    report = gate_cost(events)
    assert report["summary"]["ambiguous_prefixes"] == [SID_A[:8]]
    for item in report["sessions"]:
        assert item["decisions_joined"] is False
        assert item["i3_max"] == 0


def test_since_and_session_filters_and_byte_cap(tmp_path: Path) -> None:
    log = _write_log(tmp_path / "latency.log", _session_a_lines())
    since = datetime.fromtimestamp((BASE_MS + 3_000) / 1000, tz=UTC)
    events, _ = parse_gate_log(log, since=since, session_id=SID_A)
    assert all(event["ts"] >= BASE_MS + 3_000 for event in events)
    assert all(event.get("session_id", SID_A) == SID_A for event in events)
    assert all(event.get("session_prefix", SID_A[:8]) == SID_A[:8] for event in events)

    events, boundary = parse_gate_log(log, max_bytes=200)
    assert boundary["truncated"] is True and boundary["read_bytes"] <= 200
    with pytest.raises(ValueError):
        parse_gate_log(log, max_bytes=0)


def test_report_stub_when_log_is_missing(tmp_path: Path) -> None:
    report = gate_cost_report(tmp_path / "missing.log")
    assert report["status"] == "no-log"
    assert "sessions" not in report
    assert DEFAULT_GATE_LOG.name == "latency.log"


def test_audit_joins_gate_log_and_reports_i3_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apu.behavior_audit import _gate_cost_finding

    state = tmp_path / "state"
    log = _write_log(tmp_path / "latency.log", _session_a_lines())
    monkeypatch.setenv("APU_HOME", str(state))
    _, report = audit_behavior(state, cwd=tmp_path, gate_log=log)
    gate = report["gate_decisions"]
    assert gate["status"] == "measured"
    assert gate["summary"]["sessions_over_target"] == [SID_A]
    assert "SECRET-PROMPT-TEXT" not in json.dumps(report)
    # No evidence for the session, so no finding is attached to an audited session.
    assert all(item["detector"] != "read-only-turns-while-pending" for item in report["findings"])

    _, skipped = audit_behavior(state, cwd=tmp_path, gate_log=None)
    assert skipped["gate_decisions"] == {"status": "skipped"}

    # The finding an audited session would receive, built from the same report.
    (session,) = [item for item in gate["sessions"] if item["session_id"] == SID_A]
    finding = _gate_cost_finding(session, provider="claude-code")
    assert finding["detector"] == "read-only-turns-while-pending"
    assert finding["severity"] == "medium"
    assert finding["status"] == "reportable"
    assert finding["verification_status"] == "observed"
    assert finding["evidence_refs"] == [f"gate-log:{SID_A[:8]}"]
    assert "3 completed read-only turns" in finding["summary"]


def test_gate_cost_cli_text_and_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    log = _write_log(tmp_path / "latency.log", _session_a_lines())
    monkeypatch.setenv("APU_HOME", str(tmp_path / "state"))
    assert apu_main(["behavior", "gate-cost", "--log", str(log), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "measured"
    assert report["summary"]["i3_max"] == 3

    assert apu_main(["behavior", "gate-cost", "--log", str(log), "--session-id", SID_A]) == 0
    out = capsys.readouterr().out
    assert "Gate cost (I3)" in out
    assert f"- {SID_A}: I3 max 3" in out
    assert "objective 0: 3 read-only turn(s) while pending (1 with no tools at all)" in out
    assert "SECRET-PROMPT-TEXT" not in out

    assert apu_main(["behavior", "gate-cost", "--log", str(tmp_path / "nope.log")]) == 0
    assert "no-log" in capsys.readouterr().out

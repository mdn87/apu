from __future__ import annotations

import json
from pathlib import Path

import pytest

from apu.runners import (
    CLAUDE_CAPABILITIES,
    CODEX_CAPABILITIES,
    RunnerParseError,
    evaluate_event_checks,
    parse_claude_jsonl,
    parse_codex_jsonl,
    unavailable_result,
    unsupported_result,
)


FIXTURES = Path(__file__).parent / "fixtures" / "runners"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("parser", "name"),
    [
        (parse_codex_jsonl, "codex-complete.jsonl"),
        (parse_claude_jsonl, "claude-complete.jsonl"),
    ],
)
def test_complete_streams_normalize_events_without_sensitive_payloads(
    parser, name: str
) -> None:
    events = parser(fixture(name))

    assert {event.type for event in events} == {
        "tool_use",
        "delegation",
        "review",
    }
    serialized = json.dumps([event.to_dict() for event in events])
    for sensitive in (
        "secret-value",
        "private authentication",
        "private finding",
        "session-secret",
        "thread-secret",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ):
        assert sensitive.lower() not in serialized.lower()


def test_codex_capabilities_match_jsonl_events_and_safe_invocation() -> None:
    assert CODEX_CAPABILITIES.observable_events == frozenset(
        {"tool_use", "delegation", "review"}
    )
    assert CODEX_CAPABILITIES.invocation == ("codex", "exec", "--json", "-")
    assert "prompt" not in CODEX_CAPABILITIES.to_dict()


def test_claude_capabilities_match_stream_json_events_and_safe_invocation() -> None:
    assert CLAUDE_CAPABILITIES.observable_events == frozenset(
        {"tool_use", "delegation", "review"}
    )
    assert CLAUDE_CAPABILITIES.invocation == (
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
    )
    assert "prompt" not in CLAUDE_CAPABILITIES.to_dict()


def test_unobservable_required_event_is_skipped_not_failed() -> None:
    capabilities = CODEX_CAPABILITIES.with_observable_events({"tool_use"})
    events = parse_codex_jsonl(fixture("codex-complete.jsonl"))

    result = evaluate_event_checks(
        events,
        capabilities,
        required=("delegation",),
    )

    assert result.status == "skipped"
    assert result.checks[0].status == "skipped"
    assert "cannot observe" in result.checks[0].reason


def test_observable_forbidden_event_fails() -> None:
    events = parse_claude_jsonl(fixture("claude-complete.jsonl"))

    result = evaluate_event_checks(
        events,
        CLAUDE_CAPABILITIES,
        forbidden=("review",),
    )

    assert result.status == "failed"
    assert result.checks[0].status == "failed"


def test_observable_required_and_absent_forbidden_events_pass() -> None:
    events = parse_codex_jsonl(fixture("codex-complete.jsonl"))

    result = evaluate_event_checks(
        events,
        CODEX_CAPABILITIES,
        required=("delegation",),
        forbidden=("not_a_real_event",),
    )

    assert result.status == "skipped"
    assert [check.status for check in result.checks] == ["passed", "skipped"]


def test_shared_nonexecution_results_are_serializable() -> None:
    unavailable = unavailable_result("no authenticated runtime")
    unsupported = unsupported_result("gemini")

    assert unavailable.to_dict() == {
        "status": "unavailable",
        "checks": [],
        "events": [],
        "reason": "no authenticated runtime",
        "runner": None,
    }
    assert unsupported.status == "skipped"
    assert unsupported.reason == "case does not support runner: gemini"


@pytest.mark.parametrize("parser", [parse_codex_jsonl, parse_claude_jsonl])
def test_invalid_json_reports_line_number_without_echoing_input(parser) -> None:
    secret = '{"credential":"secret-value"'

    with pytest.raises(RunnerParseError) as error:
        parser('{"type":"ok"}\n' + secret)

    assert "line 2" in str(error.value)
    assert "secret-value" not in str(error.value)

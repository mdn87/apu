"""Exercise review through normal ingestion and prefix verification for both providers."""

import json
import os
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from apu import behavior_audit, evidence
from apu.behavior_audit import audit_behavior
from apu.manual_review import review_command

PROVIDERS = ["codex", "claude-code", "codex-items"]
REQUEST = "Please run the project tests."
ANSWER = "Please run `pytest -q` and tell me the result."
SECRET = "private output, never include this in a report"
SESSION = "sample-session"
TURN = "sample-turn"


class Trace:
    def __init__(self, tmp_path, provider, stamp=None):
        self.provider = provider
        self.source_provider = "codex" if provider == "codex-items" else provider
        self.cwd = tmp_path / "dias"
        self.state = tmp_path / "state"
        self.root = tmp_path / provider
        self.cwd.mkdir(exist_ok=True)
        self.root.mkdir()
        self.stamp = (stamp or datetime.now(UTC)).isoformat()
        self.path = self.root / "trace.jsonl"
        self.rows = []
        if self.source_provider == "codex":
            self.event("session_meta", id=SESSION, cwd=str(self.cwd))

    def event(self, kind, **payload):
        self.rows.append({"timestamp": self.stamp, "type": kind, "payload": payload})

    def item(self, item_kind, **kw):
        self.event(
            "event_msg",
            type="item_completed",
            thread_id=SESSION,
            turn_id=TURN,
            item={"type": item_kind, **kw},
        )

    def claude(self, role, content, *, stop=None, **extra):
        self.rows.append(
            {
                "timestamp": self.stamp,
                "type": role,
                "cwd": str(self.cwd),
                "uuid": f"msg-{len(self.rows)}",
                "sessionId": SESSION,
                "parentUuid": self.rows[-1]["uuid"] if self.rows else None,
                "message": {"role": role, "content": content, "stop_reason": stop},
                **extra,
            }
        )

    def start(self, request=REQUEST):
        if self.source_provider == "codex":
            self.event("event_msg", type="task_started", turn_id=TURN)
            self.event(
                "turn_context",
                cwd=str(self.cwd),
                turn_id=TURN,
                sandbox_policy={"type": "workspace-write"},
            )
            if self.provider == "codex-items":
                self.item("UserMessage", content=[{"type": "text", "text": request}])
            else:
                self.event("event_msg", type="user_message", message=request)
        else:
            self.claude("user", request)

    def shell(self, command="git status", *, workdir=None, ok=True):
        if self.provider == "codex-items":
            self.item(
                "CommandExecution",
                id="exec-one",
                cwd=str(workdir or self.cwd),
                command=["bash", "-lc", command],
                status="completed" if ok else "failed",
                exit_code=0 if ok else 1,
                source="unified_exec_startup",
                aggregated_output=SECRET,
            )
        elif self.provider == "codex":
            args = {"cmd": command}
            if workdir is not None:
                args["workdir"] = str(workdir)
            self.event(
                "response_item",
                type="function_call",
                name="exec_command",
                call_id="one",
                arguments=json.dumps(args),
            )
            self.event(
                "response_item",
                type="function_call_output",
                call_id="one",
                output=f"Exit code: {int(not ok)}\n{SECRET}",
            )
        else:
            self.claude(
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "id": "one",
                        "input": {"command": command},
                    }
                ],
            )
            self.claude(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "one",
                        "content": SECRET,
                        "is_error": not ok,
                    }
                ],
            )
            if workdir is not None:
                self.rows[-2]["cwd"] = self.rows[-1]["cwd"] = str(workdir)

    def finish(self, answer=ANSWER):
        if self.source_provider == "codex":
            self.event(
                "event_msg",
                type="task_complete",
                turn_id=TURN,
                last_agent_message=answer,
            )
        else:
            self.claude(
                "assistant", [{"type": "text", "text": answer}], stop="end_turn"
            )

    def write(self):
        self.path.write_bytes(
            b"".join((json.dumps(r) + "\n").encode() for r in self.rows)
        )

    def audit(self, **kwargs):
        root_key = (
            "trace_root" if self.source_provider == "codex" else "claude_trace_root"
        )
        return audit_behavior(
            self.state,
            cwd=self.cwd,
            providers=[self.source_provider],
            review_handoffs=True,
            gate_log=None,
            **{root_key: self.root},
            **kwargs,
        )[1]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    ("instruction", "answer", "issued", "decision"),
    [
        (REQUEST, ANSWER, "git status", "candidate"),
        (
            "Stop if the board is dirty. Please run the tests otherwise.",
            "The board is dirty. " + ANSWER,
            "git status",
            "suppressed",
        ),
        (
            "Stop if the board is dirty. Please run the tests otherwise.",
            "The board is clean. " + ANSWER,
            "git status",
            "unknown",
        ),
        (
            "Stop if the board is dirty. Please run the tests otherwise.",
            ANSWER,
            "git status",
            "unknown",
        ),
        (
            "Do not stop until you run the tests. Please run the project tests.",
            ANSWER,
            "git status",
            "candidate",
        ),
        (
            REQUEST + " Our product handles credentials.",
            ANSWER,
            "git status",
            "candidate",
        ),
        (REQUEST, ANSWER, "echo hello", "unknown"),
        ("Run only the integration tests.", ANSWER, "git status", "unknown"),
        ("Please run `pytest tests/one.py`.", ANSWER, "git status", "unknown"),
        (
            "Please verify this through physical observation.",
            ANSWER,
            "git status",
            "suppressed",
        ),
        (
            REQUEST,
            ANSWER + " I can't access the test environment.",
            "git status",
            "suppressed",
        ),
        (REQUEST, ANSWER, "python -m pytest -q", "suppressed"),
        (REQUEST, ANSWER, None, "unknown"),
        ("Can you explain how to run the tests?", ANSWER, "git status", "unknown"),
        ("Please run the production build.", ANSWER, "git status", "unknown"),
        (
            REQUEST,
            "Please run `pytest ../other-project/tests`.",
            "git status",
            "unknown",
        ),
        (REQUEST, ANSWER, "cd ../sibling && git status", "unknown"),
        (REQUEST, "> " + ANSWER + "\nI handled this.", "git status", None),
        (REQUEST, "The documentation says:\n" + ANSWER, "git status", None),
        (REQUEST, "Example:\n```text\n" + ANSWER + "\n```", "git status", None),
    ],
)
def test_manual_review_native_completion(
    tmp_path, provider, instruction, answer, issued, decision
):
    trace = Trace(tmp_path, provider)
    trace.start(instruction)
    if issued:
        trace.shell(issued)
    trace.finish(answer)
    trace.write()
    report = trace.audit()
    assert [s["review_completed_turns"] for s in report["sessions"]] == [1]
    assert [r["decision"] for r in report["manual_review"]] == (
        [decision] if decision else []
    )
    serialized = json.dumps(report)
    assert SECRET not in serialized and "pytest -q" not in serialized


# Modeled on a real hold the operator rejected: a soak is no gate for a product
# that is still being built.
SOAK_HOLD = (
    "**Not deployed yet, on purpose.** The 24-hour run began at 13:49 UTC. "
    "Pushing `release` restarts the service, which would restart that run."
)
ASKED = "Make the display reload itself when a new build goes live."


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    ("instruction", "answer", "issued", "decision"),
    [
        (ASKED, SOAK_HOLD, "git status", "candidate"),
        # Any shell command that worked shows the agent could have shipped.
        (ASKED, SOAK_HOLD, "cd ../service && git log -1", "candidate"),
        (
            ASKED,
            "Done and tested. Want me to deploy now? It would restart the 24-hour soak.",
            "git status",
            "candidate",
        ),
        (
            ASKED,
            "Fixed. I'm holding the merge until the overnight soak finishes.",
            "git status",
            "candidate",
        ),
        (
            "We aren't doing a soak, deploy it. " + ASKED,
            SOAK_HOLD,
            "git status",
            "candidate",
        ),
        (
            "Let it soak for 24 hours before you deploy.",
            "Holding the deploy until the 24-hour soak finishes.",
            "git status",
            "suppressed",
        ),
        ("Do not deploy today. " + ASKED, SOAK_HOLD, "git status", "suppressed"),
        (
            ASKED,
            "Holding the release to production until the 24-hour soak finishes.",
            "git status",
            "unknown",
        ),
        (ASKED, SOAK_HOLD, None, "unknown"),
        (
            ASKED,
            "Deployed. We aren't doing a soak, so nothing is held back.",
            "git status",
            None,
        ),
        (
            ASKED,
            "Stopped the soak sampler and deployed; I'm not waiting for the 24-hour run.",
            "git status",
            None,
        ),
        (
            ASKED,
            "The overnight soak passed, so I deployed the fix.",
            "git status",
            None,
        ),
        (ASKED, "Not deployed yet: the build fails on main.", "git status", None),
        (
            ASKED,
            "> Not deployed yet, the 24-hour soak is running.\nI deployed it.",
            "git status",
            None,
        ),
    ],
)
def test_an_unrequested_soak_hold_is_reviewed_like_a_handoff(
    tmp_path, provider, instruction, answer, issued, decision
):
    trace = Trace(tmp_path, provider)
    trace.start(instruction)
    if issued:
        trace.shell(issued)
    trace.finish(answer)
    trace.write()
    report = trace.audit()
    assert [(r["handoff"], r["decision"]) for r in report["manual_review"]] == (
        [("soak-hold", decision)] if decision else []
    )
    serialized = json.dumps(report)
    assert "24-hour" not in serialized and "on purpose" not in serialized


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("elsewhere", [False, True])
def test_a_soak_hold_needs_a_shell_command_that_worked_here(
    tmp_path, provider, elsewhere
):
    trace = Trace(tmp_path, provider)
    trace.start(ASKED)
    if elsewhere:
        trace.shell("git log -1", workdir=tmp_path / "elsewhere")
    else:
        trace.shell("git push origin main:release", ok=False)
    trace.finish(SOAK_HOLD)
    trace.write()
    assert all(r["decision"] != "candidate" for r in trace.audit()["manual_review"])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_one_answer_can_hand_back_a_check_and_hold_for_a_soak(tmp_path, provider):
    trace = Trace(tmp_path, provider)
    trace.start()
    trace.shell()
    trace.finish(ANSWER + " I'm holding the deploy until the 24-hour soak finishes.")
    trace.write()
    review = trace.audit()["manual_review"]
    assert sorted((r["handoff"], r["decision"]) for r in review) == [
        ("soak-hold", "candidate"),
        ("test-command", "candidate"),
    ]
    assert len({r["ref"] for r in review}) == 2


@pytest.mark.parametrize("provider", PROVIDERS)
def test_lookup_returns_the_sentences_that_held_for_the_soak(tmp_path, provider):
    from apu.manual_review import review_hold

    trace = Trace(tmp_path, provider)
    trace.start(ASKED)
    trace.shell()
    trace.finish(
        "Done and tested. Want me to deploy now? It would restart the 24-hour soak."
    )
    trace.write()
    (result,) = trace.audit()["manual_review"]
    with trace.path.open("rb") as source:
        source.seek(result["offset"])
        record = source.read(result["length"])
    assert review_command(trace.source_provider, record) is None
    assert (
        review_hold(trace.source_provider, record)
        == "Want me to deploy now? It would restart the 24-hour soak."
    )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_occurrence_links_one_original_completion_across_replay(tmp_path, provider):
    trace = Trace(tmp_path, provider)
    trace.start()
    trace.shell()
    trace.finish()
    trace.write()
    (result,) = trace.audit()["manual_review"]
    assert result["source"] == "verified" and re.fullmatch(
        r"ar-[a-f0-9]{32}", result["ref"]
    )
    with trace.path.open("rb") as source:
        source.seek(result["offset"])
        record = source.read(result["length"])
        assert sha256(record.rstrip(b"\r\n")).hexdigest() == result["record_sha256"]
        assert review_command(trace.source_provider, record) == "pytest -q"
    assert (
        sha256(trace.path.read_bytes()[: result["prefix_bytes"]]).hexdigest()
        == result["prefix_sha256"]
    )
    trace.rows.append(trace.rows[-1])
    trace.write()
    replay = trace.audit()
    assert replay["manual_review"] == [result]
    assert replay["sessions"][0]["review_completed_turns"] == 1


@pytest.mark.parametrize("provider", PROVIDERS)
def test_foreign_shell_or_unfinished_turn_cannot_prove_capability(tmp_path, provider):
    trace = Trace(tmp_path, provider)
    trace.start()
    trace.shell(workdir=tmp_path / "elsewhere")
    trace.finish()
    trace.start()
    trace.shell()
    trace.write()
    report = trace.audit()
    assert all(r["decision"] != "candidate" for r in report["manual_review"])
    assert report["sessions"][0]["review_completed_turns"] <= 1


@pytest.mark.parametrize("provider", PROVIDERS)
def test_unfinished_and_old_turns_are_not_current_coverage(tmp_path, provider):
    trace = Trace(tmp_path, provider, datetime.now(UTC) - timedelta(days=8))
    trace.start()
    trace.shell()
    trace.finish()
    trace.stamp = datetime.now(UTC).isoformat()
    trace.start()
    trace.write()
    report = trace.audit()
    assert report["manual_review"] == []
    assert report["sessions"][0]["review_completed_turns"] == 0


@pytest.mark.parametrize("provider", PROVIDERS)
def test_same_size_changed_prefix_is_reported_as_a_skip(tmp_path, provider):
    trace = Trace(tmp_path, provider)
    trace.start()
    trace.shell()
    trace.finish()
    trace.write()
    assert len(trace.audit()["manual_review"]) == 1
    old = trace.path.read_bytes()
    trace.path.write_bytes(old.replace(b"private output", b"changed output"))
    report = trace.audit()
    assert not report["manual_review"] and not report["sessions"]
    assert [s["reason"] for s in report["selection"]["skipped"]] == ["source-changed"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_recent_discovery_limits_skip_old_sources_before_counting_the_cap(
    tmp_path, provider, monkeypatch
):
    trace = Trace(tmp_path, provider)
    trace.start()
    trace.shell()
    trace.finish()
    trace.write()
    old = trace.root / "old.jsonl"
    old.write_bytes(trace.path.read_bytes())
    stamp = (datetime.now(UTC) - timedelta(days=9)).timestamp()
    os.utime(old, (stamp, stamp))
    monkeypatch.setattr(behavior_audit, "_DISCOVERY_FILE_LIMIT", 1)
    report = trace.audit(source_byte_limit=trace.path.stat().st_size - 1)
    assert not report["manual_review"] and not report["sessions"]
    assert Counter(s["reason"] for s in report["selection"]["skipped"]) == {
        "source-byte-limit": 1
    }


def test_codex_changed_between_reads_is_not_ingested(tmp_path, monkeypatch):
    trace = Trace(tmp_path, "codex")
    trace.start()
    trace.shell()
    trace.finish()
    trace.write()
    original = evidence._codex_snapshot

    def replace_after_snapshot(path, **kwargs):
        result = original(path, **kwargs)
        path.write_bytes(path.read_bytes().replace(b"git status", b"git branch"))
        return result

    monkeypatch.setattr(evidence, "_codex_snapshot", replace_after_snapshot)
    report = trace.audit()
    assert not report["sessions"] and not report["manual_review"]
    assert [s["reason"] for s in report["selection"]["skipped"]] == [
        "source-ingestion-failed"
    ]
    assert not evidence.read_evidence(trace.state, "codex", SESSION)


def test_claude_sibling_and_thinking_block_do_not_close_the_completed_branch(tmp_path):
    trace = Trace(tmp_path, "claude-code")
    trace.start()
    trace.shell()
    result_id = trace.rows[-1]["uuid"]
    trace.claude(
        "user", "Please explain this example.", parentUuid=trace.rows[0]["uuid"]
    )
    trace.claude(
        "assistant",
        [{"type": "thinking", "thinking": SECRET}],
        stop="end_turn",
        parentUuid=result_id,
    )
    trace.finish()
    trace.write()
    (reviewed,) = trace.audit()["manual_review"]
    assert (
        reviewed["decision"] == "candidate"
        and reviewed["turn"] == trace.rows[0]["uuid"]
    )


def test_claude_sibling_shell_never_establishes_capability(tmp_path):
    trace = Trace(tmp_path, "claude-code")
    trace.start()
    trace.shell()
    trace.finish()
    trace.rows[-1]["parentUuid"] = trace.rows[0]["uuid"]
    trace.write()
    (reviewed,) = trace.audit()["manual_review"]
    assert reviewed["decision"] == "unknown"


def test_codex_fork_with_parent_history_never_acquires_the_parents_turn(tmp_path):
    trace = Trace(tmp_path, "codex")
    trace.event("session_meta", id="original-thread", cwd=str(trace.cwd))
    trace.start()
    trace.shell()
    trace.finish()
    trace.write()
    report = trace.audit()
    assert not report["sessions"] and not report["manual_review"]
    assert [s["reason"] for s in report["selection"]["skipped"]] == [
        "source-ingestion-failed"
    ]
    # The existing general-purpose normalization contract selects the first session.
    _, _, boundary = evidence.ingest_codex_trace(trace.state, trace.path)
    assert boundary["session_id"] == SESSION
    cached = trace.audit()
    assert not cached["manual_review"] and not cached["sessions"]
    assert [s["reason"] for s in cached["selection"]["skipped"]] == ["source-changed"]


def test_codex_context_switch_drops_the_original_user_and_shell(tmp_path):
    trace = Trace(tmp_path, "codex-items")
    trace.start()
    trace.shell()
    trace.event("turn_context", turn_id=TURN, cwd=str(tmp_path / "another"))
    trace.event("turn_context", turn_id=TURN, cwd=str(trace.cwd))
    trace.finish()
    trace.write()
    report = trace.audit()
    assert (
        not report["manual_review"]
        and report["sessions"][0]["review_completed_turns"] == 0
    )


def test_codex_nested_output_and_malformed_native_command_do_not_prove_execution(
    tmp_path,
):
    trace = Trace(tmp_path, "codex")
    trace.start()
    trace.shell()
    trace.rows[-1]["payload"]["output"] = "A file I read says\nExit code: 0"
    trace.item(
        "CommandExecution",
        source=[],
        status="completed",
        exit_code=0,
        cwd=str(trace.cwd),
        command=["bash", "-lc", "git status"],
    )
    trace.finish()
    trace.write()
    (result,) = trace.audit()["manual_review"]
    assert result["decision"] == "unknown"


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    ("stdout", "decision"), [(" M .dias/chips.json\n", "suppressed"), ("", "candidate")]
)
def test_dirty_board_condition_uses_native_git_status(
    tmp_path, provider, stdout, decision
):
    trace = Trace(tmp_path, provider)
    trace.start("Stop if the board is dirty. Please run the tests otherwise.")
    trace.shell("git status --porcelain")
    if provider == "codex":
        trace.rows[-1]["payload"]["output"] = "Exit code: 0\nOutput:\n" + stdout
    elif provider == "codex-items":
        trace.rows[-1]["payload"]["item"]["aggregated_output"] = stdout
    else:
        trace.rows[-1]["message"]["content"][0]["content"] = stdout
    trace.finish()
    trace.write()
    (result,) = trace.audit()["manual_review"]
    assert result["decision"] == decision

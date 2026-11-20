import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(("second", "exit_code"), [("ok", 0), ("failed", 1)])
def test_scheduled_run_waits_once_and_returns_the_final_result(
    tmp_path, monkeypatch, second, exit_code
):
    import time

    from apu import review_pilot as pilot

    attempts, waits = [], []

    def run(*args, **options):
        attempts.append(options)
        if len(attempts) == 1:
            raise RuntimeError("state is busy")
        return {"run_status": second}

    monkeypatch.setattr(pilot, "run_review", run)
    monkeypatch.setattr(time, "sleep", waits.append)
    args = SimpleNamespace(
        review_action="run",
        cwd=tmp_path,
        force=False,
        delivery_ledger=None,
        retry_once=True,
    )
    assert pilot.run_cli(tmp_path, args) == exit_code
    assert len(attempts) == 2 and waits == [300]


def test_review_publishes_only_safe_fields_and_keeps_coverage_on_failure(
    tmp_path, monkeypatch
):
    from apu import review_pilot as pilot

    state, workspace = tmp_path / "state", tmp_path / "dias"
    workspace.mkdir()
    date = datetime(2026, 11, 20, 9, tzinfo=UTC)
    calls = []
    ref = "ar-" + "a" * 32

    def audit(home, **options):
        calls.append(options)
        if len(calls) > 1:
            raise OSError("private workstation failure")
        return None, {
            "audit_id": "private-audit",
            "sessions": [
                {
                    "provider": "codex",
                    "session_id": "private-session",
                    "source_kind": "transcript",
                    "review_completed_turns": 1,
                }
            ],
            "selection": {"skipped": []},
            "manual_review": [
                {
                    "ref": ref,
                    "provider": "codex",
                    "session_id": "private-session",
                    "line": 12,
                    "turn": "private-turn",
                    "source": "verified",
                    "decision": "candidate",
                    "reason": "authorized-executable-check",
                    "observed_at": "2026-11-20T08:00:00Z",
                }
            ],
        }

    monkeypatch.setattr(pilot, "audit_behavior", audit)
    summary = pilot.run_review(state, workspace, now=date)
    assert summary["coverage"]["codex"] == 1
    assert summary["actions"] == [
        {"kind": "manual-handoff", "ref": ref, "source": "verified"}
    ]
    assert "private" not in json.dumps(summary)
    assert str(workspace) not in json.dumps(summary)
    assert pilot.run_review(state, workspace, now=date) == summary
    assert len(calls) == 1
    assert pilot.show_review(state, ref)["line"] == 12
    pilot.label_review(state, ref, "useful")
    pilot.label_review(state, ref, "useful")
    assert pilot.read_summary(state)["actions"] == []
    failed = pilot.run_review(state, workspace, now=date + timedelta(days=1))
    assert failed["run_status"] == "failed"
    assert failed["last_success_at"] == summary["attempted_at"]
    assert "private" not in json.dumps(failed)
    assert not list((state / "outcomes").glob("*.jsonl"))
    with pytest.raises(ValueError):
        pilot.show_review(state, "../private")


def test_local_lookup_checks_the_request_and_completion_but_allows_appends(tmp_path):
    from apu import review_pilot as pilot

    path = tmp_path / "trace.jsonl"
    request = b'{"type":"user","text":"run tests"}\n'
    answer = b'{"type":"event_msg","payload":{"type":"task_complete","last_agent_message":"Please run `pytest`."}}\n'
    path.write_bytes(request + answer)
    ref = "ar-" + "1" * 32
    state = {
        "version": 1,
        "labels": {},
        "records": {
            ref: {
                "provider": "codex",
                "ref": ref,
                "source_path": str(path),
                "offset": len(request),
                "length": len(answer),
                "record_sha256": sha256(answer.rstrip()).hexdigest(),
                "prefix_bytes": len(request + answer),
                "prefix_sha256": sha256(request + answer).hexdigest(),
            }
        },
    }
    pilot.write_json_atomic(tmp_path / "behavior/review/pilot.json", state)
    with path.open("ab") as stream:
        stream.write(b"{}\n")
    assert pilot.show_review(tmp_path, ref)["requested_command"] == "pytest"
    path.write_bytes(request.replace(b"run", b"ban") + answer)
    changed = pilot.show_review(tmp_path, ref)
    assert (
        changed["evidence_status"] == "changed" and "requested_command" not in changed
    )
    path.rename(tmp_path / "expired.jsonl")
    assert pilot.show_review(tmp_path, ref)["evidence_status"] == "expired"


def test_a_pretrigger_catchup_and_scheduled_run_have_different_dates(
    tmp_path, monkeypatch
):
    from apu import review_pilot as pilot
    from apu.review_contract import schedule_day

    repo = tmp_path / "dias"
    repo.mkdir()
    calls = []

    def audit(*args, **kwargs):
        calls.append(kwargs["now"])
        return None, {
            "audit_id": "empty",
            "sessions": [],
            "selection": {"skipped": []},
            "manual_review": [],
        }

    monkeypatch.setattr(pilot, "audit_behavior", audit)
    state = tmp_path / "state"
    for day, before, after in [
        ("2026-03-08", "2026-03-08T07:14:59+00:00", "2026-03-08T07:15:00+00:00"),
        ("2026-11-01", "2026-11-01T08:14:59+00:00", "2026-11-01T08:15:00+00:00"),
    ]:
        early, due = datetime.fromisoformat(before), datetime.fromisoformat(after)
        assert schedule_day(early).isoformat() < day
        assert pilot.run_review(state, repo, now=early)["schedule_date"] < day
        assert pilot.run_review(state, repo, now=due)["schedule_date"] == day
        pilot.run_review(state, repo, now=due + timedelta(hours=2))
    assert len(calls) == 4


def test_evaluation_is_due_with_coverage_and_delivery_denominators(
    tmp_path, monkeypatch
):
    from apu import review_pilot as pilot

    state, repo = tmp_path / "state", tmp_path / "dias"
    repo.mkdir()
    start = datetime(2026, 11, 20, 9, tzinfo=UTC)
    candidates = [
        {
            "ref": "ar-" + format(i, "032x"),
            "decision": "candidate",
            "source": "verified",
            "observed_at": "2026-11-20T08:00:00Z",
            "line": i,
        }
        for i in range(1, 7)
    ]
    audit = {
        "audit_id": "test",
        "manual_review": candidates,
        "selection": {"skipped": []},
        "sessions": [
            {
                "provider": "codex",
                "source_kind": "transcript",
                "review_completed_turns": 6,
            },
            {
                "provider": "claude-code",
                "source_kind": "transcript",
                "review_completed_turns": 0,
            },
        ],
    }
    monkeypatch.setattr(pilot, "audit_behavior", lambda *args, **kwargs: (None, audit))
    ledger = tmp_path / "apu-deliveries.json"
    first = pilot.run_review(state, repo, now=start, delivery_ledger=ledger)
    assert first["coverage"]["skipped"] == {"no-completed-turn": 1}
    assert first["coverage"]["claude-code"] == 0
    refs = [item["ref"] for item in candidates]
    for ref, label in zip(
        refs,
        ("useful", "useful", "useful", "dismissed", "dismissed", "unsure"),
        strict=True,
    ):
        pilot.label_review(state, ref, label)
    later = start + timedelta(days=13)
    summary = pilot.run_review(state, repo, now=later)
    assert [action["kind"] for action in summary["actions"]] == ["pilot-evaluation"]
    receipts = {
        ref: {
            "date": (start + timedelta(days=i)).date().isoformat(),
            "kind": "manual-handoff",
            "coverage": "full",
            "sha256": "a" * 64,
        }
        for i, ref in enumerate(refs)
    }
    ledger.write_text(
        json.dumps({"schema": "taskmaster.apu-deliveries/v1", "shown": receipts})
    )
    stats = pilot.evaluate(state, now=later)
    assert stats["review_due"] and stats["calendar_nights"] == 14
    assert stats["unique_handoffs_delivered"] == 6 and stats["covered_nights"] == 2
    assert stats["unlabeled_deliveries"] == 0 and stats["missed_nights"] == 12
    assert stats["useful_share"] == 0.6 and stats["unsure"] == 1
    pilot.evaluate(state, now=later, decision="continue", useful_ref=refs[0])
    assert not pilot.read_summary(state)["actions"]


def test_interrupted_export_reuses_local_commit(tmp_path, monkeypatch):
    from apu import review_pilot as pilot

    state, repo = tmp_path / "state", tmp_path / "dias"
    repo.mkdir()
    future = datetime(2026, 11, 20, 9, tzinfo=UTC)
    calls = []

    def audit(*args, **opts):
        calls.append(1)
        return None, {
            "audit_id": "fixture",
            "manual_review": [],
            "selection": {"skipped": []},
            "sessions": [],
        }

    writer = pilot.write_json_atomic

    def fail_export(path, value):
        if path.name == "summary.json":
            raise OSError("simulated interruption")
        return writer(path, value)

    monkeypatch.setattr(pilot, "audit_behavior", audit)
    monkeypatch.setattr(pilot, "write_json_atomic", fail_export)
    with pytest.raises(OSError):
        pilot.run_review(state, repo, now=future)
    monkeypatch.setattr(pilot, "write_json_atomic", writer)
    assert pilot.run_review(state, repo, now=future) == pilot.read_summary(state)
    assert len(calls) == 1

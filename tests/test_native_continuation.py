from __future__ import annotations

import json
from pathlib import Path

import pytest

from apu.behavior_watch import diagnose_incident, mark_incident, record_intervention_result
from apu.native_continuation import admit, claim, observe
from apu.behavior_cli import intervene_main
from test_behavior_provider_neutral import _SESSION_ID, _write_claude_trace


@pytest.fixture
def prepared(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    trace = _write_claude_trace(tmp_path / "traces", cwd)
    state = tmp_path / "state"
    _, incident = mark_incident(
        state, "stopped before completing the requested task", provider="claude-code",
        trace_root=tmp_path / "traces", session_id=_SESSION_ID, cwd=cwd,
    )
    _, diagnosis = diagnose_incident(state, incident_id=incident["incident_id"])
    return state, cwd, trace, diagnosis


def reserve(prepared):
    state, cwd, _, diagnosis = prepared
    return claim(state, diagnosis_id=diagnosis["diagnosis_id"],
                 session_id=_SESSION_ID, cwd=cwd)


def test_claim_is_durable_and_replay_never_returns_a_prompt(prepared):
    first = reserve(prepared)
    assert first["should_submit"] is True
    assert first["prompt"]
    assert first["executed"] is False
    again = reserve(prepared)
    assert again["should_submit"] is False
    assert "prompt" not in again
    assert again["intervention_id"] == first["intervention_id"]


def test_new_diagnosis_of_same_evidence_cannot_replay(prepared):
    first = reserve(prepared)
    state, cwd, _, old = prepared
    _, diagnosis = diagnose_incident(state, incident_id=old["incident_id"])
    again = claim(state, diagnosis_id=diagnosis["diagnosis_id"], session_id=_SESSION_ID, cwd=cwd)
    assert again["should_submit"] is False
    assert again["intervention_id"] == first["intervention_id"]


def bookkeeping(command="apu-diagnose", *, output=False):
    value = {"type": "system", "subtype": "local_command", "sessionId": _SESSION_ID,
             "isSidechain": False, "level": "info", "isMeta": False}
    if output:
        value.update(content="<local-command-stdout>apu-session: APU: planned</local-command-stdout>",
                     commandRun={"command": command, "args": ""})
    else:
        value["content"] = (f"<command-name>/{command}</command-name>\n"
                            f"            <command-message>{command}</command-message>\n"
                            "            <command-args></command-args>")
    return value


def append_record(trace, value):
    with trace.open("a") as stream:
        stream.write(json.dumps(value) + "\n")


def test_native_command_bookkeeping_preserves_evidence_and_replay_identity(prepared):
    state, cwd, trace, old = prepared
    first = reserve(prepared)
    for command in ("apu-diagnose", "apu-prepare", "apu-continue"):
        append_record(trace, bookkeeping(command))
        append_record(trace, bookkeeping(command, output=True))
    assert admit(state, intervention_id=first["intervention_id"],
                 session_id=_SESSION_ID, cwd=cwd)["should_enter"] is True
    _, incident = mark_incident(state, "stopped before completing the requested task",
                               provider="claude-code", trace_root=trace.parent.parent,
                               session_id=_SESSION_ID, cwd=cwd)
    _, diagnosis = diagnose_incident(state, incident_id=incident["incident_id"])
    again = claim(state, diagnosis_id=diagnosis["diagnosis_id"], session_id=_SESSION_ID, cwd=cwd)
    assert again["should_submit"] is False
    assert again["intervention_id"] == first["intervention_id"]


@pytest.mark.parametrize("change", ["user", "assistant", "other-command", "args", "barrier", "malformed", "policy"])
def test_only_recognized_native_bookkeeping_is_ignored(prepared, change):
    _, _, trace, _ = prepared
    value = bookkeeping(output=True)
    if change in {"user", "assistant"}:
        value["type"] = change
        value["message"] = {"role": change, "content": value["content"]}
    elif change == "other-command":
        value = bookkeeping("permissions", output=True)
    elif change == "args":
        value["commandRun"]["args"] = "changed request"
    elif change == "barrier":
        value["toolDenialKind"] = "permission-rule"
    elif change == "malformed":
        value["content"] += "another request"
    else:
        value = {"type": "permission-mode", "mode": "default"}
    append_record(trace, value)
    with pytest.raises(ValueError, match="changed"):
        reserve(prepared)


@pytest.mark.parametrize("field", ["session", "cwd", "prompt", "policy", "trace", "barrier"])
def test_invalid_or_changed_context_never_reserves(prepared, field):
    state, cwd, trace, diagnosis = prepared
    session = _SESSION_ID
    if field == "session":
        session = "different-session"
    elif field == "cwd":
        cwd = cwd.parent
    elif field in {"prompt", "policy", "barrier"}:
        if field == "prompt":
            diagnosis["recommended_intervention"]["prompt_sha256"] = "0" * 64
        elif field == "policy":
            diagnosis["recommended_intervention"]["durable_policy_mutation"] = True
        else:
            diagnosis["status"] = "possible-legitimate-barrier"
        (state / "behavior/diagnoses" / (diagnosis["diagnosis_id"] + ".json")).write_text(json.dumps(diagnosis))
    else:
        trace.write_text(trace.read_text() + "\n")
    with pytest.raises((ValueError, RuntimeError)):
        claim(state, diagnosis_id=diagnosis["diagnosis_id"], session_id=session, cwd=cwd)
    assert not list((state / "behavior/interventions").glob("native-*.json"))


def test_admission_rechecks_and_can_only_be_claimed_once(prepared):
    first = reserve(prepared)
    state, cwd, trace, _ = prepared
    admitted = admit(state, intervention_id=first["intervention_id"], session_id=_SESSION_ID, cwd=cwd)
    assert admitted["should_enter"] is True
    assert admit(state, intervention_id=first["intervention_id"], session_id=_SESSION_ID, cwd=cwd)["should_enter"] is False


def test_new_evidence_between_reservation_and_admission_refuses(prepared):
    first = reserve(prepared)
    state, cwd, trace, _ = prepared
    trace.write_text(trace.read_text() + "\n")
    with pytest.raises(ValueError, match="changed"):
        admit(state, intervention_id=first["intervention_id"], session_id=_SESSION_ID, cwd=cwd)
    with pytest.raises(ValueError, match="changed"):
        reserve(prepared)
    assert len(list((state / "behavior/interventions").glob("native-*.json"))) == 1


def test_delivery_observations_never_attest_recovery_and_do_not_regress(prepared):
    first = reserve(prepared)
    state, cwd, _, _ = prepared
    iid = first["intervention_id"]
    admit(state, intervention_id=iid, session_id=_SESSION_ID, cwd=cwd)
    observe(state, intervention_id=iid, observation="started", turn_id="turn-one")
    queued = observe(state, intervention_id=iid, observation="queued")
    assert queued["native_delivery"]["state"] == "started"
    with pytest.raises(ValueError, match="turn"):
        observe(state, intervention_id=iid, observation="turn-ended", turn_id="other", reason="answer")
    ended = observe(state, intervention_id=iid, observation="turn-ended", turn_id="turn-one", reason="answer")
    assert ended["outcome_verification"] == "unverified"
    assert ended["executed"] is True
    assert ended["turn_completed"] is True
    assert not (state / "behavior/intervention-results").exists()
    _, result = record_intervention_result(state, "completed", intervention_id=iid)
    assert result["source"] == "operator-attestation"


def test_unknown_or_dropped_attempt_is_not_resubmitted(prepared):
    first = reserve(prepared)
    state, _, _, _ = prepared
    observe(state, intervention_id=first["intervention_id"], observation="outcome-unknown")
    assert reserve(prepared)["should_submit"] is False


def test_cli_routes_native_actions_without_spawning_provider(prepared, monkeypatch, capsys):
    state, cwd, _, diagnosis = prepared
    monkeypatch.setenv("APU_HOME", str(state))
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("must not launch a provider"))
    args = ["--provider", "claude-code", "--native-action", "claim", "--diagnosis",
            diagnosis["diagnosis_id"], "--session-id", _SESSION_ID, "--cwd", str(cwd), "--json"]
    assert intervene_main(args) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["should_submit"] is True
    assert intervene_main(args) == 0
    assert json.loads(capsys.readouterr().out)["should_submit"] is False
    assert intervene_main(args + ["--execute"]) == 1
    assert "cannot execute" in capsys.readouterr().err


def test_simultaneous_claims_produce_only_one_submit_authority(prepared):
    from concurrent.futures import ThreadPoolExecutor
    def attempt():
        try:
            return reserve(prepared)["should_submit"]
        except RuntimeError as error:
            assert "busy" in str(error)
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert results.count(True) == 1

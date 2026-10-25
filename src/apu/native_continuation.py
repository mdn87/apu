"""Reserve Claude-native continuations before effect; never launch a provider.

The adapter owns native observations. APU retains attribution, prompt integrity,
the durable no-replay record and the separate operator-attested outcome.
"""

from __future__ import annotations

from pathlib import Path

from .behavior_lock import behavior_lock
from .behavior_watch import (
    _load_leaf,
    _timestamp,
    normalized_cwd_key,
    validated_intervention_context,
)
from .models import sha256_bytes, sha256_json
from .state import write_json_atomic


def _context(state_home, diagnosis_id, session_id, cwd):
    diagnosis, incident, prompt, session, template = validated_intervention_context(
        state_home, diagnosis_id=diagnosis_id, provider="claude-code"
    )
    if session.session_id != session_id or normalized_cwd_key(session.cwd) != normalized_cwd_key(cwd):
        raise ValueError("native continuation session or directory mismatch")
    if session.barrier_codes:
        raise ValueError("native continuation refused because a current barrier may exist")
    if (not session.native_evidence_sha256
            or session.native_evidence_sha256 != incident["session"].get("native_evidence_sha256")):
        raise ValueError("session evidence changed; diagnose the current request again")
    return diagnosis, incident, prompt, session, template


def _save(state_home, value):
    path = Path(state_home) / "behavior/interventions" / (value["intervention_id"] + ".json")
    write_json_atomic(path, value)
    return value


def _load(state_home, intervention_id):
    value = _load_leaf(state_home, "interventions", intervention_id)
    if value.get("type") != "native-continuation" or value.get("provider") != "claude-code":
        raise ValueError("not a Claude-native intervention")
    return value


def claim(state_home: Path, *, diagnosis_id: str, session_id: str, cwd: Path) -> dict:
    """Return the prompt only to the single durable reservation winner."""
    with behavior_lock(state_home):
        diagnosis, incident, prompt, session, template = _context(
            state_home, diagnosis_id, session_id, cwd
        )
        key = sha256_json({
            "provider": session.provider, "session_id": session_id,
            "cwd": normalized_cwd_key(cwd), "native_evidence_sha256": session.native_evidence_sha256,
            "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
        })
        iid = "native-" + key
        path = Path(state_home) / "behavior/interventions" / (iid + ".json")
        if path.exists():
            return {**_load(state_home, iid), "should_submit": False}
        value = {
            "schema_version": 1, "intervention_id": iid,
            "diagnosis_id": diagnosis["diagnosis_id"], "incident_id": incident["incident_id"],
            "provider": "claude-code", "session_id": session_id,
            "created_at": _timestamp(), "type": "native-continuation",
            "status": "planned", "command": [], "executed": False,
            "returncode": None, "result_signals": [], "turn_completed": None,
            "outcome_verification": "unverified", "verification_status": "unverifiable",
            "prompt_template_id": template, "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
            "durable_policy_mutation": False,
            "native_delivery": {"state": "reserved", "cwd": str(session.cwd),
                                "trace_sha256": session.trace_sha256, "turn_id": None,
                                "native_evidence_sha256": session.native_evidence_sha256,
                                "observations": []},
        }
        _save(state_home, value)  # A crash or lost response after this point never permits replay.
        write_json_atomic(Path(state_home) / "behavior/latest-intervention.json",
                          {"schema_version": 1, "intervention_id": iid})
        return {**value, "should_submit": True, "prompt": prompt}


def admit(state_home: Path, *, intervention_id: str, session_id: str, cwd: Path) -> dict:
    """Recheck immediately in the native ingress hook, once per reservation."""
    with behavior_lock(state_home):
        value = _load(state_home, intervention_id)
        if value["native_delivery"]["state"] != "reserved":
            return {**value, "should_enter": False}
        _, _, prompt, session, _ = _context(state_home, value["diagnosis_id"], session_id, cwd)
        if (value["session_id"] != session.session_id
                or value["prompt_sha256"] != sha256_bytes(prompt.encode("utf-8"))
                or value["native_delivery"]["native_evidence_sha256"] != session.native_evidence_sha256):
            raise ValueError("native reservation context changed")
        value["native_delivery"]["state"] = "admitted"
        _save(state_home, value)
        return {**value, "should_enter": True}


def observe(state_home: Path, *, intervention_id: str, observation: str,
            turn_id: str | None = None, reason: str | None = None) -> dict:
    """Record transport observations without asserting the user's task succeeded."""
    if observation not in {"queued", "started", "turn-ended", "dropped", "outcome-unknown"}:
        raise ValueError("unknown native delivery observation")
    with behavior_lock(state_home):
        value = _load(state_home, intervention_id)
        delivery = value["native_delivery"]
        current = delivery["state"]
        if observation in {"started", "turn-ended"}:
            if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 200:
                raise ValueError("native observation requires an exact turn")
            if delivery["turn_id"] not in {None, turn_id}:
                raise ValueError("native observation belongs to another turn")
        if observation == "turn-ended":
            if not delivery["turn_id"] or reason not in {"answer", "aborted", "refusal", "error"}:
                raise ValueError("turn-ended requires a previously observed turn and reason")
        if current in {"dropped", "turn-ended"}:
            return value
        if observation == "queued" and current in {"started", "outcome-unknown"}:
            return value  # A late submit acknowledgement cannot downgrade newer evidence.
        if current == "reserved" and observation in {"queued", "started", "turn-ended"}:
            raise ValueError("native prompt was not admitted")
        if observation == "started":
            delivery["turn_id"] = turn_id
            value.update(status="resumed", executed=True, verification_status="observed")
        elif observation == "turn-ended":
            value["turn_completed"] = reason == "answer"
        elif observation == "dropped" and current == "started":
            raise ValueError("a started native turn cannot be recorded as dropped")
        entry = {"state": observation, "at": _timestamp()}
        if turn_id is not None:
            entry["turn_id"] = turn_id
        if reason is not None:
            entry["reason"] = reason
        if observation != current:
            delivery["observations"].append(entry)
        delivery["state"] = observation
        return _save(state_home, value)

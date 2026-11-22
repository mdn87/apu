"""Nightly review of the manual-work policy for one explicitly selected workspace."""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from contextlib import nullcontext
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

from .behavior_audit import audit_behavior
from .behavior_lock import behavior_lock
from .models import canonical_json
from .outcomes import read_outcomes, summarize_outcomes
from .review_contract import (
    MAX_BYTES,
    REF,
    SCHEMA,
    SKIPS,
    SOURCES,
    schedule_day,
    validate,
)
from .state import load_registry, write_json_atomic

POLICY = "manual-user-work-policy-20261017"
LABELS = ("useful", "dismissed", "unsure")


def _directory(home: Path) -> Path:
    return home / "behavior" / "review"


def _state(home: Path) -> dict:
    path = _directory(home) / "pilot.json"
    if not path.exists():
        return {
            "version": 1,
            "workspace_path": None,
            "records": {},
            "labels": {},
            "nights": {},
            "eligible": [],
            "summary": None,
        }
    if path.is_symlink():
        raise ValueError("invalid private review state")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("unsupported private review state")
    return data


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _coverage() -> dict:
    return {"codex": 0, "claude-code": 0, "skipped": {}, "unknown": 0, "overflow": 0}


def _window(home: Path, now: datetime) -> dict | None:
    entry = load_registry(home)["installations"].get(POLICY)
    if not entry or entry.get("status") != "active":
        return None
    start = entry.get("monitoring_started_at") or entry["applied_at"]
    progress = summarize_outcomes(
        read_outcomes(home, POLICY), monitoring_started_at=start, now=now
    )
    if not progress["days_complete"] or progress["material_task_count"]:
        return None
    ref = "ar-" + sha256(f"{POLICY}|{start}".encode()).hexdigest()[:32]
    return {
        "kind": "monitoring-window",
        "ref": ref,
        "source": "recorded",
        "installation": POLICY,
        "monitoring_started_at": start,
        "monitoring": progress,
    }


def _publish(home: Path, state: dict) -> dict:
    doc = state["summary"]
    refs = [ref for ref in state["eligible"] if ref not in state["labels"]]
    stats = _metrics(state, date.fromisoformat(doc["schedule_date"]))
    if doc["run_status"] == "ok" and stats["review_due"] and not state.get("decision"):
        first = min(state["nights"])
        ref = "ar-" + sha256(f"pilot-evaluation|dias|{first}".encode()).hexdigest()[:32]
        state["records"][ref] = {
            "ref": ref,
            "kind": "pilot-evaluation",
            "source": "recorded",
            "first_night": first,
        }
        refs.insert(0, ref)
    doc["coverage"]["overflow"] = max(0, len(refs) - 20)
    doc["actions"] = [
        {k: state["records"][r][k] for k in ("kind", "ref", "source")}
        for r in refs[:20]
    ]
    validate(doc)
    write_json_atomic(_directory(home) / "pilot.json", state)
    write_json_atomic(_directory(home) / "summary.json", doc)
    return doc


def run_review(
    home: Path,
    workspace: Path,
    *,
    now: datetime | None = None,
    force: bool = False,
    delivery_ledger: Path | None = None,
) -> dict:
    """Run once per local date. Reconcile a private commit interrupted before export."""
    if (
        not workspace.is_absolute()
        or workspace.name.casefold() != "dias"
        or not workspace.is_dir()
    ):
        raise ValueError("review requires the absolute Dias workspace directory")
    workspace = workspace.resolve()
    now = now or datetime.now(UTC)
    day, stamp = schedule_day(now).isoformat(), _stamp(now)
    with behavior_lock(home):
        state = _state(home)
        if state["workspace_path"] not in (None, str(workspace)):
            raise ValueError("review pilot is already bound to another workspace")
        state["workspace_path"] = str(workspace)
        if delivery_ledger is not None:
            if (
                not delivery_ledger.is_absolute()
                or delivery_ledger.name != "apu-deliveries.json"
            ):
                raise ValueError(
                    "expected the absolute Taskmaster apu-deliveries.json path"
                )
            state["delivery_ledger"] = str(delivery_ledger)
        previous = state["summary"]
        if (
            previous
            and previous["schedule_date"] == day
            and previous["run_status"] == "ok"
            and not force
        ):
            return _publish(home, state)
        summary = {
            "schema": SCHEMA,
            "workspace": "dias",
            "schedule_date": day,
            "attempted_at": stamp,
            "last_success_at": previous["last_success_at"] if previous else None,
            "run_status": "failed",
            "coverage": _coverage(),
            "actions": [],
        }
        state["eligible"] = []
        try:
            _, audit = audit_behavior(
                home, cwd=workspace, gate_log=None, now=now, review_handoffs=True
            )
            cover = _coverage()
            skips = Counter()
            for session in audit["sessions"]:
                if session["source_kind"] != "transcript":
                    skips["unavailable-evidence"] += 1
                elif session.get("review_completed_turns", 0) > 0:
                    cover[session["provider"]] += 1
                else:
                    skips["no-completed-turn"] += 1
            for skip in audit["selection"]["skipped"]:
                reason = skip["reason"]
                if reason != "outside-lookback":
                    skips[reason if reason in SKIPS else "other"] += skip.get(
                        "skipped_file_count", 1
                    )
            cover["skipped"] = dict(skips)
            records = {}
            for result in audit["manual_review"]:
                if result["decision"] == "unknown":
                    cover["unknown"] += 1
                elif (
                    result["decision"] == "candidate" and result["source"] == "verified"
                ):
                    records[result["ref"]] = {
                        **result,
                        "kind": "manual-handoff",
                        "audit_id": audit["audit_id"],
                    }
            # Sort recent candidates first so a batch that exceeds the transport cap can still admit new work.
            refs = sorted(
                records,
                key=lambda ref: (records[ref]["observed_at"], ref),
                reverse=True,
            )
            window = _window(home, now)
            if window:
                records[window["ref"]] = window
                refs.append(window["ref"])
            state["records"].update(records)
            state["eligible"] = refs
            state.pop("last_error", None)
            summary.update(run_status="ok", last_success_at=stamp, coverage=cover)
        except (OSError, ValueError, RuntimeError) as exc:
            # Full diagnostics remain private. No stale actions can be offered by a failed run.
            state["last_error"] = type(exc).__name__
        state["summary"] = summary
        state["nights"][day] = {
            "status": summary["run_status"],
            "coverage": dict(summary["coverage"]),
        }
        return _publish(home, state)


def read_summary(home: Path) -> dict:
    path = _directory(home) / "summary.json"
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("review summary exceeds byte cap")
    return validate(json.loads(data))


def _record(state: dict, ref: str) -> dict:
    if not REF.fullmatch(ref):
        raise ValueError("invalid review reference")
    if ref not in state["records"]:
        raise ValueError("review reference unavailable or expired")
    return state["records"][ref]


def show_review(home: Path, ref: str) -> dict:
    state = _state(home)
    result = dict(_record(state, ref))
    if "source_path" in result:
        from .manual_review import review_command, review_hold

        try:
            with Path(result["source_path"]).open("rb") as stream:
                offset, size = result["offset"], result["length"]
                if (
                    type(offset) is not int
                    or type(size) is not int
                    or offset < 0
                    or size <= 0
                    or offset + size > 256 * 1024 * 1024
                ):
                    raise ValueError("invalid review record length")
                context = sha256()
                if "prefix_sha256" in result:
                    if result.get("prefix_bytes") != offset + size:
                        raise ValueError("invalid review source boundary")
                    remaining = offset
                    while remaining:
                        block = stream.read(min(remaining, 1024 * 1024))
                        if not block:
                            break
                        context.update(block)
                        remaining -= len(block)
                else:
                    stream.seek(offset)
                raw = stream.read(size)
            match = sha256(raw.rstrip(b"\r\n")).hexdigest() == result["record_sha256"]
            if "prefix_sha256" in result:
                context.update(raw)
                match = match and context.hexdigest() == result["prefix_sha256"]
            result["evidence_status"] = "available" if match else "changed"
            if match and result.get("handoff") == "soak-hold":
                held = review_hold(result["provider"], raw)
                if held:
                    result["held_for"] = held
            elif match:
                command = review_command(result["provider"], raw)
                if command:
                    result["requested_command"] = command
        except OSError:
            result["evidence_status"] = "expired"
    result["label"] = state["labels"].get(ref)
    return result


def label_review(home: Path, ref: str, label: str) -> dict:
    if label not in LABELS:
        raise ValueError("unknown review label")
    with behavior_lock(home):
        state = _state(home)
        record = _record(state, ref)
        if record["kind"] == "pilot-evaluation":
            raise ValueError(
                "record a pilot decision with behavior review evaluate --decision"
            )
        state["labels"][ref] = label
        _publish(home, state)
    return {"ref": ref, "label": label}


def _metrics(state: dict, day: date) -> dict:
    labels = Counter(
        label
        for ref, label in state["labels"].items()
        if state["records"][ref]["kind"] == "manual-handoff"
    )
    decisive = labels["useful"] + labels["dismissed"]
    first = min(state["nights"], default=None)
    nights = max(0, (day - date.fromisoformat(first)).days + 1) if first else 0
    covered = sum(
        n["status"] == "ok"
        and (n["coverage"]["codex"] + n["coverage"]["claude-code"] > 0)
        for n in state["nights"].values()
    )
    attempted = len(state["nights"])
    failed = sum(n["status"] == "failed" for n in state["nights"].values())
    return {
        "calendar_nights": nights,
        "attempted_nights": attempted,
        "covered_nights": covered,
        "empty_nights": attempted - failed - covered,
        "failed_nights": failed,
        "missed_nights": max(0, nights - attempted),
        "useful": labels["useful"],
        "dismissed": labels["dismissed"],
        "unsure": labels["unsure"],
        "decisive": decisive,
        "useful_share": labels["useful"] / decisive if decisive >= 5 else None,
        "review_due": (nights >= 14 or decisive >= 10) and not state.get("decision"),
    }


def _delivered_handoffs(path: Path, records: dict) -> set[str]:
    limit = 2 * 1024 * 1024  # Taskmaster's maximum verified-delivery ledger size.
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Taskmaster delivery ledger exceeds its size limit")
    doc = json.loads(raw)
    if (
        not isinstance(doc, dict)
        or set(doc) != {"schema", "shown"}
        or doc["schema"] != "taskmaster.apu-deliveries/v1"
        or not isinstance(doc["shown"], dict)
    ):
        raise ValueError("invalid Taskmaster delivery ledger")
    handoffs, days = set(), set()
    for ref, row in doc["shown"].items():
        if (
            not REF.fullmatch(ref)
            or not isinstance(row, dict)
            or set(row) != {"date", "kind", "coverage", "sha256"}
            or row["kind"] not in tuple(SOURCES)
            or row["coverage"] not in ("full", "partial", "empty", "capped", "overflow")
            or not isinstance(row["sha256"], str)
            or not re.fullmatch("[a-f0-9]{64}", row["sha256"])
            or not isinstance(row["date"], str)
            or date.fromisoformat(row["date"]).isoformat() != row["date"]
            or row["date"] in days
        ):
            raise ValueError("invalid Taskmaster delivery receipt")
        days.add(row["date"])
        if (
            row["kind"] == "manual-handoff"
            and records.get(ref, {}).get("kind") == "manual-handoff"
        ):
            handoffs.add(ref)
    return handoffs


def evaluate(
    home: Path,
    *,
    delivered: Path | None = None,
    now: datetime | None = None,
    decision: str | None = None,
    useful_ref: str | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    if decision is not None and decision not in ("continue", "narrow", "stop"):
        raise ValueError("unknown pilot decision")
    if useful_ref and decision is None:
        raise ValueError("--useful-ref requires an explicit pilot decision")
    day = schedule_day(now)
    with behavior_lock(home) if decision else nullcontext():
        state = _state(home)
        if delivered is None and state.get("delivery_ledger"):
            configured = Path(state["delivery_ledger"])
            if configured.is_file():
                delivered = configured
        shown = (
            _delivered_handoffs(delivered, state["records"])
            if delivered is not None
            else None
        )
        if decision:
            if not state["summary"]:
                raise ValueError("no pilot runs have been recorded")
            if useful_ref and (
                _record(state, useful_ref)["kind"] != "manual-handoff"
                or state["labels"].get(useful_ref) != "useful"
            ):
                raise ValueError("this reference requires a useful handoff label")
            state["decision"] = {
                "at": _stamp(now),
                "decision": decision,
                "useful_ref": useful_ref,
            }
            _publish(home, state)
    return {
        **_metrics(state, day),
        "unique_handoffs_delivered": len(shown) if shown is not None else None,
        "unlabeled_deliveries": len(shown - state["labels"].keys())
        if shown is not None
        else None,
        "decision": state.get("decision"),
    }


def add_parser(commands) -> None:
    review = commands.add_parser(
        "review", help="run and inspect the nightly manual-work pilot"
    )
    actions = review.add_subparsers(dest="review_action", required=True)
    run = actions.add_parser("run")
    run.add_argument("--cwd", required=True, type=Path)
    run.add_argument(
        "--retry-once",
        action="store_true",
        help="retry a failed or busy audit after five minutes",
    )
    run.add_argument(
        "--force", action="store_true", help="refresh this local schedule date"
    )
    run.add_argument(
        "--delivery-ledger",
        type=Path,
        help="remember the local Taskmaster verified-delivery ledger",
    )
    actions.add_parser("status")
    actions.add_parser("show").add_argument("ref")
    label = actions.add_parser("label")
    label.add_argument("ref")
    label.add_argument("label", choices=LABELS)
    evaluation = actions.add_parser("evaluate")
    evaluation.add_argument("--delivery-ledger", type=Path)
    evaluation.add_argument("--decision", choices=("continue", "narrow", "stop"))
    evaluation.add_argument("--useful-ref")


def run_cli(home: Path, args) -> int:
    if args.review_action == "run":
        for attempt in range(2 if args.retry_once else 1):
            if attempt:
                time.sleep(300)
            try:
                result = run_review(
                    home,
                    args.cwd,
                    force=args.force,
                    delivery_ledger=args.delivery_ledger,
                )
            except (OSError, RuntimeError):
                if attempt or not args.retry_once:
                    raise
                continue
            if result["run_status"] == "ok":
                break
    elif args.review_action == "status":
        result = read_summary(home)
    elif args.review_action == "evaluate":
        result = evaluate(
            home,
            delivered=args.delivery_ledger,
            decision=args.decision,
            useful_ref=args.useful_ref,
        )
    elif args.review_action == "label":
        result = label_review(home, args.ref, args.label)
    else:
        result = show_review(home, args.ref)
    print(canonical_json(result))
    return int(result.get("run_status") == "failed")

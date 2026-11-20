"""Version one of the bounded APU workstation review summary."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SCHEMA = "apu.behavior-review/v1"
MAX_BYTES = 8192
REF = re.compile(r"ar-[a-f0-9]{32}\Z")
SKIPS = frozenset(
    {
        "source-root-unavailable",
        "discovery-file-limit",
        "session-metadata-unavailable",
        "source-ingestion-failed",
        "session-limit",
        "source-byte-limit",
        "unavailable-evidence",
        "source-changed",
        "no-completed-turn",
        "other",
    }
)
SOURCES = {
    "manual-handoff": "verified",
    "monitoring-window": "recorded",
    "pilot-evaluation": "recorded",
}


def timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
    ):
        raise ValueError("invalid review timestamp")
    return datetime.fromisoformat(value)


def eastern(wall_clock: datetime) -> datetime:
    """Use the pilot's civil time even when the report host runs in UTC."""
    if wall_clock.utcoffset() is None:
        raise ValueError("review clock must be timezone-aware")
    return wall_clock.astimezone(ZoneInfo("America/New_York"))


def schedule_day(when: datetime) -> date:
    local = eastern(when)
    return local.date() - timedelta(days=local.time() < time(3, 15))


def _keys(value: object, required: set) -> dict:
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid review summary fields")
    return value


def _count(value: object, limit: int = 1_000_000_000) -> None:
    if type(value) is not int or not 0 <= value <= limit:
        raise ValueError("invalid review count")


def validate(value: object) -> dict:
    doc = _keys(
        value,
        {
            "schema",
            "workspace",
            "attempted_at",
            "last_success_at",
            "schedule_date",
            "run_status",
            "coverage",
            "actions",
        },
    )
    if (
        doc["schema"] != SCHEMA
        or doc["workspace"] != "dias"
        or doc["run_status"] not in ("ok", "failed")
    ):
        raise ValueError("unsupported review summary")
    attempted = timestamp(doc["attempted_at"])
    schedule = doc["schedule_date"]
    if not isinstance(schedule, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}", schedule
    ):
        raise ValueError("invalid review date")
    if date.fromisoformat(schedule) != schedule_day(attempted):
        raise ValueError("review date does not match attempt")
    last = doc["last_success_at"]
    if last is not None and timestamp(last) > attempted:
        raise ValueError("invalid last successful review")
    if doc["run_status"] == "ok" and last != doc["attempted_at"]:
        raise ValueError("successful review must identify its coverage time")
    cover = _keys(
        doc["coverage"], {"codex", "claude-code", "skipped", "unknown", "overflow"}
    )
    for k in ("codex", "claude-code", "unknown", "overflow"):
        _count(cover[k], 20 if k in ("codex", "claude-code") else 1_000_000_000)
    if cover["codex"] + cover["claude-code"] > 20:
        raise ValueError("review session cap exceeded")
    skipped = cover["skipped"]
    if not isinstance(skipped, dict) or not set(skipped) <= SKIPS:
        raise ValueError("invalid review skip reasons")
    for count in skipped.values():
        _count(count)
    actions = doc["actions"]
    if (
        not isinstance(actions, list)
        or len(actions) > 20
        or (doc["run_status"] == "failed" and actions)
    ):
        raise ValueError("invalid review action count")
    seen = set()
    for item in actions:
        _keys(item, {"kind", "ref", "source"})
        kind, ref = item["kind"], item["ref"]
        if (
            not isinstance(kind, str)
            or kind not in SOURCES
            or item["source"] != SOURCES[kind]
            or not isinstance(ref, str)
            or not REF.fullmatch(ref)
            or ref in seen
        ):
            raise ValueError("invalid review action")
        seen.add(ref)
    if (
        len(json.dumps(doc, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        > MAX_BYTES
    ):
        raise ValueError("review summary exceeds byte cap")
    return doc

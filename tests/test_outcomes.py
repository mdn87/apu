from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

from apu.outcomes import append_outcome, read_outcomes, summarize_outcomes


def _outcome(
    *,
    task_id: str,
    recorded_at: str,
    material: bool,
    elapsed_seconds: float | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "installation_id": "install-123",
        "recorded_at": recorded_at,
        "task_id": task_id,
        "material": material,
        "source": "user",
        "elapsed_seconds": elapsed_seconds,
        "agent_count": None,
        "review_count": None,
        "remediation_count": None,
        "validation": "partial",
        "rework": False,
        "escaped_defect": {
            "present": False,
            "severity": "none",
            "category": None,
        },
        "notes": None,
    }


def test_append_and_read_outcomes_with_partial_metrics(tmp_path: Path) -> None:
    state_home = tmp_path / "state"
    first = _outcome(
        task_id="task-1",
        recorded_at="2026-07-01T00:00:00Z",
        material=True,
        elapsed_seconds=None,
    )
    second = _outcome(
        task_id="task-2",
        recorded_at="2026-07-02T00:00:00+00:00",
        material=False,
        elapsed_seconds=12.5,
    )

    path = append_outcome(state_home, first)
    append_outcome(state_home, second)

    assert path == state_home / "outcomes" / "install-123.jsonl"
    assert read_outcomes(state_home, "install-123") == [first, second]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_outcomes_are_isolated_by_installation(tmp_path: Path) -> None:
    state_home = tmp_path / "state"
    one = _outcome(
        task_id="one", recorded_at="2026-07-01T00:00:00Z", material=True
    )
    two = _outcome(
        task_id="two", recorded_at="2026-07-01T00:00:00Z", material=True
    )
    two["installation_id"] = "install-456"

    append_outcome(state_home, one)
    append_outcome(state_home, two)

    assert read_outcomes(state_home, "install-123") == [one]
    assert read_outcomes(state_home, "install-456") == [two]


def test_reading_missing_outcomes_does_not_create_state(tmp_path: Path) -> None:
    assert read_outcomes(tmp_path / "state", "install-123") == []
    assert not (tmp_path / "state").exists()


def test_summary_requires_both_30_days_and_10_material_tasks() -> None:
    start = "2026-07-01T00:00:00Z"
    nine_material = [
        _outcome(task_id=f"task-{index}", recorded_at=start, material=True)
        for index in range(9)
    ]
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)

    days_only = summarize_outcomes(nine_material, now=now)
    assert days_only == {
        "record_count": 9,
        "material_task_count": 9,
        "elapsed_days": 30,
        "required_days": 30,
        "required_material_tasks": 10,
        "days_complete": True,
        "tasks_complete": False,
        "complete": False,
    }

    complete = summarize_outcomes(
        nine_material
        + [_outcome(task_id="task-10", recorded_at=start, material=True)],
        now=now,
    )
    assert complete["days_complete"] is True
    assert complete["tasks_complete"] is True
    assert complete["complete"] is True


def test_summary_can_use_installation_time_before_first_outcome() -> None:
    records = [
        _outcome(
            task_id=f"task-{index}",
            recorded_at="2026-07-25T00:00:00Z",
            material=True,
        )
        for index in range(10)
    ]

    summary = summarize_outcomes(
        records,
        now=datetime(2026, 7, 31, tzinfo=timezone.utc),
        monitoring_started_at="2026-07-01T00:00:00Z",
    )

    assert summary["elapsed_days"] == 30
    assert summary["complete"] is True


def test_invalid_outcome_is_rejected_before_creating_state(tmp_path: Path) -> None:
    invalid = _outcome(
        task_id="task-1",
        recorded_at="not-a-time",
        material=True,
    )

    with pytest.raises(ValueError, match="recorded_at"):
        append_outcome(tmp_path / "state", invalid)

    assert not (tmp_path / "state").exists()

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from apu.efficacy_policy import (
    clear_demotion_override,
    demotion_status_overlay,
    evaluate_category_promotion,
    load_demotion_overrides,
)
from apu.outcomes import append_outcome, read_outcomes, validate_outcome

CATEGORY = "instruction-conflict"
STAMP = "a" * 64


def _outcome_v2(
    index: int,
    *,
    material: bool = True,
    activation_source_id: str | None = None,
    window_status: str = "closed",
    close_fixture: bool = False,
    defect: bool = False,
) -> dict:
    activation_source_id = activation_source_id or f"marker-source-{index}"
    fixtures = []
    if close_fixture:
        fixtures.append(
            {
                "fixture_id": "close-check",
                "run_id": f"close-run-{index}",
                "category": CATEGORY,
                "phase": "window-close",
                "result": "passed",
                "artifact_sha256": "b" * 64,
                "recorded_at": "2026-08-06T23:00:00Z",
            }
        )
    return {
        "schema_version": 2,
        "installation_id": "install-m9",
        "recorded_at": f"2026-08-06T22:{index:02d}:00Z",
        "task_id": f"task-{index}",
        "material": material,
        "source": "trace",
        "elapsed_seconds": 10.0,
        "agent_count": 1,
        "review_count": 1,
        "remediation_count": 1,
        "validation": "passed",
        "rework": False,
        "escaped_defect": {
            "present": defect,
            "severity": "serious" if defect else "none",
            "category": CATEGORY if defect else None,
        },
        "notes": "operator notes are not copied into override state",
        "campaign_id": "campaign-m9",
        "campaign_provenance": {
            "source": "campaign-manifest",
            "manifest_sha256": STAMP,
        },
        "categories_installed": [CATEGORY],
        "categories_activated": [
            {
                "category": CATEGORY,
                "activation_source_id": activation_source_id,
                "source_kind": "deterministic-marker",
                "provenance": {
                    "marker_id": f"marker-{index}",
                    "artifact_sha256": "c" * 64,
                    "recorded_at": f"2026-08-06T22:{index:02d}:00Z",
                },
            }
        ],
        "baseline_version": "baseline-2026-08",
        "model_generation": "gpt-5-generation",
        "fixture_results": fixtures,
        "monitoring_window": {
            "window_id": "window-1",
            "status": window_status,
            "opened_at": "2026-08-01T00:00:00Z",
            "closed_at": (
                "2026-08-06T23:30:00Z" if window_status == "closed" else None
            ),
        },
    }


def _promotion(records: list[dict], **overrides: object) -> dict:
    arguments = {
        "category": CATEGORY,
        "deterministic_remediation": True,
        "profile_sha256": "d" * 64,
        "baseline_version": "baseline-2026-08",
        "model_generation": "gpt-5-generation",
        "current_policy": "work-order",
    }
    arguments.update(overrides)
    return evaluate_category_promotion(records, **arguments)


def test_v2_outcome_round_trips_and_v1_reader_remains_compatible(
    tmp_path: Path,
) -> None:
    record = _outcome_v2(1)

    append_outcome(tmp_path / "state", record)

    assert read_outcomes(tmp_path / "state", "install-m9") == [record]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda record: record["categories_activated"][0].update(
                {"source_kind": "unprovenanced"}
            ),
            "source_kind",
        ),
        (
            lambda record: record["categories_activated"][0].update(
                {"category": "not-installed"}
            ),
            "not installed",
        ),
        (
            lambda record: record["categories_activated"][0].update(
                {
                    "source_kind": "category-fixture",
                    "provenance": {
                        "fixture_id": "fixture-1",
                        "run_id": "rerun-1",
                        "phase": "rerun",
                        "result": "passed",
                        "artifact_sha256": "e" * 64,
                    },
                }
            ),
            "validation-only",
        ),
    ],
)
def test_activation_evidence_is_provenance_bearing_and_fail_closed(
    mutate,
    message: str,
) -> None:
    record = _outcome_v2(1)
    mutate(record)

    with pytest.raises(ValueError, match=message):
        validate_outcome(record)


def test_promotion_counts_distinct_material_activations_and_only_proposes() -> None:
    records = [_outcome_v2(index) for index in range(10)]
    records[-1]["fixture_results"] = _outcome_v2(9, close_fixture=True)[
        "fixture_results"
    ]
    duplicate = deepcopy(records[0])
    duplicate["categories_activated"][0]["source_kind"] = "user-attestation"
    duplicate["categories_activated"][0]["provenance"] = {
        "attested_by": "reviewer",
        "attested_at": "2026-08-06T23:20:00Z",
        "statement_sha256": "f" * 64,
    }
    fixture_rerun = _outcome_v2(10, material=False)
    fixture_rerun["categories_activated"] = []
    fixture_rerun["fixture_results"] = [
        {
            "fixture_id": "close-check",
            "run_id": "rerun-validation",
            "category": CATEGORY,
            "phase": "rerun",
            "result": "passed",
            "artifact_sha256": "1" * 64,
            "recorded_at": "2026-08-06T23:25:00Z",
        }
    ]
    profile = {"remediation_policy": {CATEGORY: "work-order"}}

    evaluation = _promotion(records + [duplicate, fixture_rerun])

    assert evaluation["eligible"] is True
    assert evaluation["evidence"]["distinct_material_activation_count"] == 10
    assert evaluation["proposal"]["artifact_type"] == "proposed-profile-edit"
    assert evaluation["proposal"]["path"] == ["remediation_policy", CATEGORY]
    assert evaluation["proposal"]["before"] == "work-order"
    assert evaluation["proposal"]["after"] == "auto"
    assert evaluation["proposal"]["requires_review"] is True
    assert profile == {"remediation_policy": {CATEGORY: "work-order"}}


def test_promotion_requires_every_contract_gate() -> None:
    records = [_outcome_v2(index) for index in range(10)]
    records[-1]["fixture_results"] = _outcome_v2(9, close_fixture=True)[
        "fixture_results"
    ]

    assert _promotion(records[:-1])["eligible"] is False
    assert _promotion(records, deterministic_remediation=False)["eligible"] is False
    assert _promotion(records, model_generation="other-generation")["eligible"] is False

    failed_fixture = deepcopy(records)
    failed_fixture[-1]["fixture_results"][0]["result"] = "failed"
    assert _promotion(failed_fixture)["eligible"] is False

    with_defect = deepcopy(records)
    with_defect[-1]["escaped_defect"] = {
        "present": True,
        "severity": "ordinary",
        "category": CATEGORY,
    }
    evaluation = _promotion(with_defect)
    assert evaluation["eligible"] is False
    assert "implicating-escaped-defect" in evaluation["reasons"]
    assert evaluation["proposal"] is None


def test_defect_writes_private_override_and_reviewed_clear_is_reactivatable(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    first_defect = _outcome_v2(1, defect=True)

    append_outcome(state_home, first_defect)

    overrides = load_demotion_overrides(state_home)
    assert len(overrides) == 1
    assert overrides[0]["active"] is True
    assert overrides[0]["active_triggers"][0]["campaign_id"] == "campaign-m9"
    assert overrides[0]["active_triggers"][0]["task_id"] == "task-1"
    override_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (state_home / "overrides").rglob("*.json")
    )
    assert first_defect["notes"] not in override_text

    profile_policy = {CATEGORY: "auto", "manual-only": "ignore"}
    overlay = demotion_status_overlay(profile_policy, state_home)
    assert overlay["configured"] == profile_policy
    assert overlay["effective"] == {
        CATEGORY: "work-order",
        "manual-only": "ignore",
    }
    assert profile_policy[CATEGORY] == "auto"

    clearance_path = clear_demotion_override(
        state_home,
        CATEGORY,
        {
            "decision": "clear",
            "reviewed_by": "Matt",
            "reviewed_at": "2026-08-07T00:10:00Z",
            "evidence_sha256": "2" * 64,
        },
    )
    assert (
        json.loads(clearance_path.read_text(encoding="utf-8"))["review"]["reviewed_by"]
        == "Matt"
    )
    assert (
        demotion_status_overlay(profile_policy, state_home)["effective"][CATEGORY]
        == "auto"
    )

    second_defect = _outcome_v2(2, defect=True)
    append_outcome(state_home, second_defect)
    reactivated = demotion_status_overlay(profile_policy, state_home)
    assert reactivated["effective"][CATEGORY] == "work-order"
    assert len(reactivated["demotion_overrides"][0]["active_triggers"]) == 1

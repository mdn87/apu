from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from test_dispatch import _campaign

from apu.cli import main
from apu.dispatch import IsolationProbeResult, dispatch_work_order
from apu.dispatch_apply import apply_dispatched_plan
from apu.models import Approval, Plan
from apu.outcomes import read_outcomes
from apu.planning import update_plan_status


def test_dispatch_cli_emits_reviewable_plan(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    state, bundle, target, _ = _campaign(tmp_path)
    work_order = bundle["work_orders"][0]

    class Runtime:
        @staticmethod
        def probe(_request):
            return IsolationProbeResult(
                context_id="test-isolation",
                mechanism="test-denial",
                attempted=True,
                write_denied=True,
            )

        @staticmethod
        def run(_request):
            return {"files": {str(target.resolve()): "reviewed\n"}}

    monkeypatch.setenv("APU_HOME", str(state))
    monkeypatch.setattr(
        "apu.dispatch_runtime.runtime_for",
        lambda *_args, **_kwargs: Runtime(),
    )

    assert main(["dispatch", work_order["path"]]) == 0

    emitted = json.loads(capsys.readouterr().out)
    assert emitted["status"] == "accepted"
    assert Path(emitted["plan_path"]).is_file()
    assert emitted["next"].startswith("apu review ")


def test_dispatch_cli_rejects_exported_work_order(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    exported = tmp_path / "exported.md"
    exported.write_text("# outside private state", encoding="utf-8")
    monkeypatch.setenv("APU_HOME", str(tmp_path / "state"))

    assert main(["dispatch", str(exported)]) == 1
    assert "private campaign work-order" in capsys.readouterr().err


def test_outcome_cli_derives_campaign_category_and_records_attestation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    state, bundle, target, _ = _campaign(tmp_path)
    order = bundle["work_orders"][0]
    result = dispatch_work_order(
        state,
        bundle["campaign_id"],
        order["work_order_id"],
        runner=lambda _request: {"files": {str(target.resolve()): "reviewed\n"}},
        isolation_probe=lambda _request: IsolationProbeResult(
            context_id="test",
            mechanism="test-denial",
            attempted=True,
            write_denied=True,
        ),
    )
    draft = Plan.from_dict(json.loads(result.plan_path.read_text(encoding="utf-8")))
    reviewed = replace(
        draft,
        operations=tuple(
            replace(
                operation,
                approval=Approval(
                    status="approved",
                    recorded_at="2026-08-07T01:03:00Z",
                    method="test-review",
                ),
            )
            for operation in draft.operations
        ),
    )
    reviewed = update_plan_status(reviewed)
    receipt = apply_dispatched_plan(
        state,
        reviewed,
        installation_id="dispatch-install",
    )
    monkeypatch.setenv("APU_HOME", str(state))

    assert (
        main(
            [
                "outcome",
                "record",
                "--receipt",
                str(receipt),
                "--task-id",
                "task-1",
                "--activate",
                "guidance-conflict",
                "--validation",
                "passed",
            ]
        )
        == 0
    )
    capsys.readouterr()

    record = read_outcomes(state, "dispatch-install")[0]
    assert record["schema_version"] == 2
    assert record["campaign_id"] == bundle["campaign_id"]
    assert record["categories_installed"] == ["guidance-conflict"]
    assert record["categories_activated"][0]["source_kind"] == "user-attestation"

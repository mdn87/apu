from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path

import pytest

from apu.work_orders import (
    GuidanceCitation,
    RedactionEntry,
    RedactionMap,
    WorkOrderFinding,
    export_work_order,
    load_redaction_map,
    render_work_order,
    sanitize_staged_files,
    verify_plan_candidate,
    write_redaction_map,
    write_work_order,
)

SECRET = "sk-proj-" + "x" * 30
CONTENT_HASH = "a" * 64


def finding(
    *,
    category: str = "universal-skill-trigger",
    sensitive: bool = False,
    offending_text: str | None = "Always invoke every available skill.",
) -> WorkOrderFinding:
    return WorkOrderFinding(
        id="finding-1",
        category=category,
        path="/repo/AGENTS.md",
        line=7,
        content_sha256=CONTENT_HASH,
        summary="A universal trigger requires judgment.",
        offending_text=offending_text,
        surface_sensitive=sensitive,
    )


def render(
    item: WorkOrderFinding,
):
    return render_work_order(
        campaign_id="campaign-1",
        work_order_id="work-order-1",
        findings=(item,),
        guidance=(
            GuidanceCitation(
                guidance="Trigger workflows only when the task warrants them.",
                source="APU baseline",
                locator="multipliers/universal-trigger",
            ),
        ),
        constraints=("Keep the repository-specific exception.",),
        acceptance_criteria=("The trigger is risk-scoped and reviewable.",),
        validation_steps=("Run the universal-trigger classifier fixture.",),
    )


def test_renderer_is_deterministic_and_self_contained() -> None:
    artifact = render(finding())

    assert artifact == render(finding())
    assert artifact.dispatchable is True
    assert artifact.manual_only is False
    assert artifact.requires_sanitized_stage is False
    assert "finding-1" in artifact.rendered
    assert "/repo/AGENTS.md" in artifact.rendered
    assert "Line: 7" in artifact.rendered
    assert CONTENT_HASH in artifact.rendered
    assert "APU baseline (multipliers/universal-trigger)" in artifact.rendered
    assert "Keep the repository-specific exception." in artifact.rendered
    assert "The trigger is risk-scoped and reviewable." in artifact.rendered
    assert "Run the universal-trigger classifier fixture." in artifact.rendered
    assert "Return one plan-candidate object" in artifact.rendered
    assert '"Always invoke every available skill."' in artifact.rendered


def test_sensitive_material_exposure_is_manual_only_and_never_renders_value() -> None:
    artifact = render(
        finding(
            category="sensitive-material-exposure",
            sensitive=True,
            offending_text=f"OPENAI_API_KEY={SECRET}",
        )
    )

    assert artifact.manual_only is True
    assert artifact.dispatchable is False
    assert "MANUAL ONLY" in artifact.rendered
    assert "/repo/AGENTS.md" in artifact.rendered
    assert "Line: 7" in artifact.rendered
    assert CONTENT_HASH in artifact.rendered
    assert SECRET not in artifact.rendered
    assert "OPENAI_API_KEY" not in artifact.rendered
    assert "elided by the APU privacy contract" in artifact.rendered


def test_other_finding_on_sensitive_surface_requires_sanitized_stage() -> None:
    artifact = render(
        finding(
            sensitive=True,
            offending_text=f"rule with {SECRET}",
        )
    )

    assert artifact.manual_only is False
    assert artifact.dispatchable is True
    assert artifact.requires_sanitized_stage is True
    assert "SANITIZED STAGE ONLY" in artifact.rendered
    assert SECRET not in artifact.rendered
    assert "rule with" not in artifact.rendered


def test_non_sensitive_finding_still_elides_credential_shaped_offending_text() -> None:
    artifact = render(finding(offending_text=f"unexpected {SECRET}"))

    assert SECRET not in artifact.rendered
    assert "unexpected" not in artifact.rendered
    assert "elided by the APU privacy contract" in artifact.rendered


def test_renderer_rejects_secret_in_authored_public_prose() -> None:
    with pytest.raises(ValueError, match="credential-shaped"):
        render_work_order(
            campaign_id="campaign-1",
            work_order_id="work-order-1",
            findings=(finding(offending_text=None),),
            guidance=(
                GuidanceCitation(
                    guidance=f"Do this with {SECRET}",
                    source="bad guidance",
                ),
            ),
            constraints=(),
            acceptance_criteria=("Done.",),
            validation_steps=("Test.",),
        )


def test_sanitization_is_stable_and_keeps_values_only_in_private_map() -> None:
    second_secret = "ghp_" + "y" * 30
    stage = sanitize_staged_files(
        {
            "z.env": f"TOKEN={second_secret}\n",
            "a.env": f"OPENAI_API_KEY={SECRET}\n",
        }
    )

    assert stage.files == {
        "a.env": "OPENAI_API_KEY=«APU-REDACTED-1»\n",
        "z.env": "TOKEN=«APU-REDACTED-2»\n",
    }
    assert [entry.file for entry in stage.redactions.entries] == [
        "a.env",
        "z.env",
    ]
    assert SECRET not in repr(stage.redactions)
    assert second_secret not in repr(stage.redactions)
    assert stage.redactions.entries[0].line == 1
    assert stage.redactions.entries[0].authorized_context == (
        "OPENAI_API_KEY=«APU-REDACTED-1»"
    )


@pytest.mark.parametrize(
    ("source", "secret"),
    [
        ("password=hunter2", "hunter2"),
        ("access_token=short-token", "short-token"),
        ("api_key=abc12345", "abc12345"),
        ('{"api_key":"abc12345"}', "abc12345"),
    ],
)
def test_short_credential_assignments_are_sanitized(
    source: str,
    secret: str,
) -> None:
    stage = sanitize_staged_files({"settings": source})

    assert secret not in stage.files["settings"]
    assert "«APU-REDACTED-1»" in stage.files["settings"]
    assert len(stage.redactions.entries) == 1
    assert stage.redactions.entries[0].original_value == secret
    assert stage.redactions.entries[0].detector == "credential-assignment"


def test_assignment_placeholder_is_not_rescanned_as_a_live_secret() -> None:
    stage = sanitize_staged_files({"settings": "api_key=abc12345\n"})

    result = verify_plan_candidate(stage.files, stage.redactions)

    assert result.accepted is True
    assert result.materialized_files == {"settings": "api_key=abc12345\n"}


@pytest.mark.parametrize(
    "candidate",
    (
        "password=hunter2",
        "access_token=short-token",
        "api_key=abc12345",
    ),
)
def test_short_unexpected_secret_in_candidate_quarantines(
    candidate: str,
) -> None:
    result = verify_plan_candidate(
        {"settings": candidate},
        RedactionMap(()),
    )

    assert result.quarantined
    assert any("before persistence" in reason for reason in result.reasons)


def test_valid_candidate_materializes_only_after_structural_verification() -> None:
    stage = sanitize_staged_files(
        {"settings.env": f"OPENAI_API_KEY={SECRET}\nMODE=strict\n"}
    )
    candidate = {"settings.env": ("OPENAI_API_KEY=«APU-REDACTED-1»\nMODE=reviewed\n")}

    result = verify_plan_candidate(candidate, stage.redactions)

    assert result.accepted is True
    assert result.quarantined is False
    assert result.reasons == ()
    assert result.materialized_files == {
        "settings.env": f"OPENAI_API_KEY={SECRET}\nMODE=reviewed\n"
    }


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (
            {"settings.env": "OPENAI_API_KEY=\nMODE=strict\n"},
            "missing placeholder",
        ),
        (
            {
                "settings.env": (
                    "OPENAI_API_KEY=«APU-REDACTED-1»\nCOPY=«APU-REDACTED-1»\n"
                )
            },
            "duplicated placeholder",
        ),
        (
            {"settings.env": "MOVED=«APU-REDACTED-1»\nMODE=strict\n"},
            "relocated placeholder",
        ),
        (
            {
                "settings.env": "OPENAI_API_KEY=\nMODE=strict\n",
                "other.env": "OPENAI_API_KEY=«APU-REDACTED-1»\n",
            },
            "wrong file",
        ),
    ],
)
def test_missing_duplicate_and_moved_placeholders_quarantine(
    candidate: dict[str, str],
    reason: str,
) -> None:
    stage = sanitize_staged_files(
        {"settings.env": f"OPENAI_API_KEY={SECRET}\nMODE=strict\n"}
    )

    result = verify_plan_candidate(candidate, stage.redactions)

    assert result.accepted is False
    assert result.quarantined is True
    assert result.materialized_files is None
    assert any(reason in item for item in result.reasons)


def test_unexpected_placeholder_quarantines() -> None:
    stage = sanitize_staged_files({"settings.env": f"OPENAI_API_KEY={SECRET}\n"})
    candidate = {
        "settings.env": ("OPENAI_API_KEY=«APU-REDACTED-1»\nOTHER=«APU-REDACTED-99»\n")
    }

    result = verify_plan_candidate(candidate, stage.redactions)

    assert result.quarantined
    assert "unexpected placeholder «APU-REDACTED-99»" in result.reasons


def test_live_secret_in_returned_candidate_quarantines_before_persistence() -> None:
    stage = sanitize_staged_files(
        {"settings.env": f"OPENAI_API_KEY={SECRET}\nMODE=strict\n"}
    )
    candidate = {
        "settings.env": (
            f"OPENAI_API_KEY=«APU-REDACTED-1»\nNEW_API_KEY={('sk-proj-' + 'z' * 30)}\n"
        )
    }

    result = verify_plan_candidate(candidate, stage.redactions)

    assert result.accepted is False
    assert result.quarantined is True
    assert result.materialized_files is None
    assert any("before persistence" in reason for reason in result.reasons)


def test_exact_private_map_value_in_candidate_quarantines() -> None:
    stage = sanitize_staged_files({"settings.env": f"OPENAI_API_KEY={SECRET}\n"})

    result = verify_plan_candidate(
        {"settings.env": f"OPENAI_API_KEY={SECRET}\n"},
        stage.redactions,
    )

    assert result.quarantined
    assert any("redacted value exposed" in reason for reason in result.reasons)


def test_post_materialization_scan_rejects_secret_outside_authorized_span() -> None:
    token = "«APU-REDACTED-1»"
    context = token + "TAIL"
    redactions = RedactionMap(
        (
            RedactionEntry(
                token=token,
                file="settings.env",
                line=1,
                original_start=0,
                original_end=len(SECRET),
                authorized_context=context,
                context_sha256=sha256(context.encode()).hexdigest(),
                original_value=SECRET,
                detector="openai-key",
            ),
        )
    )

    result = verify_plan_candidate({"settings.env": context}, redactions)

    assert result.quarantined
    assert any("outside an original sanitized span" in r for r in result.reasons)


def test_work_order_and_redaction_map_are_private_and_separate(
    tmp_path: Path,
) -> None:
    artifact = render(finding(sensitive=True))
    stage = sanitize_staged_files({"settings.env": f"KEY={SECRET}\n"})
    campaign = tmp_path / "campaign"

    work_order_path = write_work_order(campaign, artifact)
    map_path = write_redaction_map(
        campaign,
        artifact.work_order_id,
        stage.redactions,
    )

    assert work_order_path.read_text(encoding="utf-8") == artifact.rendered
    assert SECRET not in work_order_path.read_text(encoding="utf-8")
    assert SECRET in map_path.read_text(encoding="utf-8")
    assert load_redaction_map(map_path) == stage.redactions
    if os.name == "posix":
        assert work_order_path.stat().st_mode & 0o777 == 0o600
        assert map_path.stat().st_mode & 0o777 == 0o600
        assert work_order_path.parent.stat().st_mode & 0o777 == 0o700
        assert map_path.parent.stat().st_mode & 0o777 == 0o700


def test_export_warns_that_copy_is_outside_apu_protection(
    tmp_path: Path,
) -> None:
    artifact = render(finding())

    exported = export_work_order(artifact, tmp_path / "export")

    assert exported.path.read_text(encoding="utf-8") == artifact.rendered
    assert "WARNING" in exported.warning
    assert "outside APU state protection" in exported.warning

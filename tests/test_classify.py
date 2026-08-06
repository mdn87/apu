from __future__ import annotations

from apu.classify import classify_surface
from apu.models import InstructionSurface


def surface(*, scope: str = "global") -> InstructionSurface:
    return InstructionSurface(
        id="sha256:" + "a" * 64,
        path="/tmp/AGENTS.md",
        kind="agents",
        provider="codex",
        authority="user",
        scope=scope,
        real_path="/tmp/AGENTS.md",
        is_symlink=False,
        content_sha256="b" * 64,
        mode="0644",
        precedence=10,
        sensitive=False,
    )


def test_classifies_universal_skill_trigger_with_stable_id() -> None:
    text = "You must invoke the using-superpowers skill at the start of every turn."

    first = classify_surface(surface(), text)
    second = classify_surface(surface(), text)

    assert [finding.category for finding in first] == ["universal-skill-trigger"]
    assert first[0].analysis_method == "heuristic"
    assert first[0].id == second[0].id
    assert first[0].location == {"line": 1}


def test_classifies_review_loop_without_inventing_semantic_certainty() -> None:
    text = "After every task, dispatch an implementer and then a code reviewer."

    findings = classify_surface(surface(), text)

    assert [finding.category for finding in findings] == [
        "per-task-review-loop"
    ]
    assert findings[0].analysis_method == "heuristic"


def test_duplicate_rule_is_structural() -> None:
    text = "Run focused tests.\nRun focused tests.\n"

    findings = classify_surface(surface(scope="repository"), text)

    duplicate = next(
        finding for finding in findings if finding.category == "duplicate-instruction"
    )
    assert duplicate.analysis_method == "structural"
    assert duplicate.location == {"line": 2}


def test_global_build_command_is_a_residency_candidate() -> None:
    text = "Run npm test before release."

    findings = classify_surface(surface(scope="global"), text)

    assert any(
        finding.category == "misplaced-repository-fact" for finding in findings
    )


def test_secret_shaped_text_is_reported_without_secret_in_evidence() -> None:
    secret = "sk-proj-" + "x" * 30

    findings = classify_surface(surface(), f"API key: {secret}")

    finding = next(
        finding
        for finding in findings
        if finding.category == "sensitive-material-exposure"
    )
    assert secret not in repr(finding.to_dict())
    assert finding.evidence == ("credential-shaped-value",)

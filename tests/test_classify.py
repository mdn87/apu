from __future__ import annotations

from apu.classify import classify_surface
from apu.models import InstructionSurface


def surface(
    *, scope: str = "global", kind: str = "agents"
) -> InstructionSurface:
    return InstructionSurface(
        id="sha256:" + "a" * 64,
        path="/tmp/AGENTS.md",
        kind=kind,
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
    text = (
        "Run focused tests for the changed behavior only.\n"
        "Run focused tests for the changed behavior only.\n"
    )

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


def test_fenced_examples_are_not_instructions() -> None:
    text = (
        "Verify the change.\n"
        "```bash\n"
        "npm test path/to/test.test.ts\n"
        "```\n"
        "Then inspect the result.\n"
        "```bash\n"
        "npm test\n"
        "```\n"
    )

    findings = classify_surface(surface(scope="global"), text)

    assert findings == ()


def test_separators_and_fences_are_not_duplicate_instructions() -> None:
    text = "Rule one.\n\n---\n\nRule two.\n\n---\n\nRule three.\n"

    findings = classify_surface(surface(scope="repository"), text)

    assert findings == ()


def test_detects_real_world_universal_trigger_phrasing() -> None:
    text = "Invoke relevant or requested skills BEFORE any response or action."

    findings = classify_surface(surface(), text)

    assert [finding.category for finding in findings] == [
        "universal-skill-trigger"
    ]


def test_detects_speculative_threshold_trigger() -> None:
    text = (
        "If there is even a 1% chance a skill might apply, "
        "you absolutely must invoke the skill."
    )

    findings = classify_surface(surface(), text)

    finding = next(
        item
        for item in findings
        if item.category == "universal-skill-trigger"
    )
    assert "speculative-threshold-trigger" in finding.evidence


def test_detects_unconditional_process_gate() -> None:
    text = "You MUST use this before any creative work."

    findings = classify_surface(surface(), text)

    assert [finding.category for finding in findings] == [
        "unconditional-approval-gate"
    ]


def test_rule_that_forbids_a_multiplier_is_not_a_multiplier() -> None:
    text = (
        "Do not run a reviewer after every task.\n"
        "Never invoke a skill at the start of every conversation.\n"
    )

    findings = classify_surface(surface(), text)

    assert findings == ()


def test_skill_examples_are_not_misplaced_repository_facts() -> None:
    text = "Run npm test to confirm the failure."

    findings = classify_surface(surface(scope="global", kind="skill"), text)

    assert findings == ()


def test_repeated_markers_and_headings_are_not_duplicate_instructions() -> None:
    text = (
        "<Good>\n"
        "Some example.\n"
        "</Good>\n"
        "<Good>\n"
        "Another example.\n"
        "</Good>\n"
        "**Test with:**\n"
        "**Test with:**\n"
    )

    findings = classify_surface(surface(scope="repository"), text)

    assert findings == ()


def test_unfenced_diagram_source_is_not_an_instruction() -> None:
    text = (
        "How the process runs:\n"
        "digraph process {\n"
        '    "Dispatch implementer subagent" -> "Review each task";\n'
        "}\n"
        "Then continue.\n"
    )

    findings = classify_surface(surface(), text)

    assert findings == ()

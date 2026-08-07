from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apu.classify import DetectorPolicy
from apu.models import canonical_json, sha256_bytes, sha256_json
from apu.package_analysis import (
    PackageAnalysisError,
    analyze_package_version,
    compare_package_versions,
    diff_package_analyses,
)

BASELINE = {
    "version": "a" * 64,
    "status": "adopted",
    "retrieved_at": "2026-08-07T10:00:00Z",
    "artifact_sha256": "b" * 64,
}
POLICY = DetectorPolicy(duplicate_instruction_minimum_words=4)


def write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def comparison(installed: Path, candidate: Path) -> dict:
    return compare_package_versions(
        installed,
        candidate,
        package_id="claude-plugin:superpowers@official",
        installed_version="1.0.0",
        candidate_version="2.0.0",
        detector_policy=POLICY,
        baseline_stamp=BASELINE,
    )


def test_collects_only_bounded_instruction_surfaces_and_dynamic_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "package"
    root.mkdir()
    secret = "sk-proj-" + "x" * 30
    write(
        root,
        "skills/example/SKILL.md",
        f"API key: {secret}\n",
    )
    write(root, "CLAUDE.md", "Run focused tests now.\n")
    write(root, "nested/AGENTS.md", "Repository policy.\n")
    write(
        root,
        ".claude/rules/testing.md",
        "Run focused tests now.\nRun focused tests now.\n",
    )
    write(root, "docs/guide.md", "Ignored ordinary markdown.\n")
    write(root, "hooks/hooks.json", '{"hooks": {}}')
    write(root, "scripts/setup.py", "print('dynamic')")

    result = analyze_package_version(
        root,
        package_id="claude-plugin:superpowers@official",
        version="1.0.0",
        detector_policy=POLICY,
        baseline_stamp=BASELINE,
    )

    assert result["surface_count"] == 4
    assert {item["relative_path"] for item in result["unclassified"]} == {
        "hooks/hooks.json",
        "scripts/setup.py",
    }
    assert any(
        item["category"] == "sensitive-material-exposure"
        for item in result["findings"]
    )
    encoded = canonical_json(result)
    assert secret not in encoded
    assert "print('dynamic')" not in encoded
    assert str(root) not in encoded


def test_resolved_finding_is_improved_and_counts_are_exact(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "installed"
    candidate = tmp_path / "candidate"
    installed.mkdir()
    candidate.mkdir()
    duplicated = "Run focused tests now.\nRun focused tests now.\n"
    write(installed, "skills/example/SKILL.md", duplicated)
    write(candidate, "skills/example/SKILL.md", "Run focused tests now.\n")

    result = comparison(installed, candidate)

    assert result["delta"]["verdict"] == "improved"
    assert len(result["delta"]["resolved"]) == 1
    assert result["delta"]["introduced"] == []
    assert result["delta"]["finding_counts"] == {
        "duplicate-instruction": {
            "installed": 1,
            "candidate": 0,
            "delta": -1,
        }
    }
    finding = result["delta"]["resolved"][0]
    assert finding["relative_path"] == "skills/example/SKILL.md"
    assert len(finding["normalized_line_sha256"]) == 64
    assert len(finding["semantic_key"]) == 64


def test_introduced_and_mixed_verdicts(tmp_path: Path) -> None:
    clean = tmp_path / "clean"
    worse = tmp_path / "worse"
    mixed_old = tmp_path / "mixed-old"
    mixed_new = tmp_path / "mixed-new"
    for root in (clean, worse, mixed_old, mixed_new):
        root.mkdir()
    duplicate = "Run focused tests now.\nRun focused tests now.\n"
    write(clean, "SKILL.md", "Run focused tests now.\n")
    write(worse, "SKILL.md", duplicate)
    write(mixed_old, "old/SKILL.md", duplicate)
    write(mixed_new, "new/SKILL.md", duplicate)

    assert comparison(clean, worse)["delta"]["verdict"] == "worse"
    mixed = comparison(mixed_old, mixed_new)["delta"]
    assert mixed["verdict"] == "mixed"
    assert len(mixed["resolved"]) == 1
    assert len(mixed["introduced"]) == 1


def test_semantic_key_survives_line_movement(tmp_path: Path) -> None:
    installed = tmp_path / "installed"
    candidate = tmp_path / "candidate"
    installed.mkdir()
    candidate.mkdir()
    trigger = "Always invoke a skill before every response."
    write(installed, "SKILL.md", trigger + "\n")
    write(candidate, "SKILL.md", "\n\n" + trigger + "\n")

    delta = comparison(installed, candidate)["delta"]

    assert delta["verdict"] == "unchanged"
    assert delta["resolved"] == []
    assert delta["introduced"] == []


def test_severity_changes_affect_verdict_without_resolve_introduce() -> None:
    context = {
        "detector_version": "2",
        "baseline": BASELINE,
        "detector_policy": {
            "duplicate_instruction_minimum_words": 4,
            "speculative_skill_threshold_enabled": True,
        },
        "detector_policy_sha256": sha256_json(
            {
                "duplicate_instruction_minimum_words": 4,
                "speculative_skill_threshold_enabled": True,
            }
        ),
    }

    def analysis(severity: str) -> dict:
        finding = {
            "semantic_key": "d" * 64,
            "relative_path": "SKILL.md",
            "surface_kind": "skill",
            "source_object_type": "file",
            "link_target": None,
            "category": "universal-skill-trigger",
            "severity": severity,
            "confidence": "high",
            "analysis_method": "heuristic",
            "line": 1,
            "normalized_line_sha256": "e" * 64,
            "evidence": ["universal-trigger-pattern"],
            "summary": "A universal trigger.",
        }
        return {
            "schema_version": 1,
            "artifact_type": "package-version-analysis",
            "package_id": "claude-plugin:superpowers@official",
            "version": severity,
            "classifier_context": context,
            "surface_manifest_sha256": "f" * 64,
            "surface_count": 1,
            "finding_counts": {"universal-skill-trigger": 1},
            "findings": [finding],
            "unclassified": [],
        }

    improved = diff_package_analyses(analysis("high"), analysis("medium"))
    worse = diff_package_analyses(analysis("medium"), analysis("high"))

    assert improved["verdict"] == "improved"
    assert worse["verdict"] == "worse"
    assert improved["resolved"] == []
    assert improved["introduced"] == []
    assert improved["severity_changed"][0]["installed"] == "high"
    assert improved["severity_changed"][0]["candidate"] == "medium"


def test_comparison_rejects_mismatched_classifier_contexts(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    write(first, "SKILL.md", "Run focused tests now.\n")
    write(second, "SKILL.md", "Run focused tests now.\n")
    installed = analyze_package_version(
        first,
        package_id="package",
        version="1",
        detector_policy=POLICY,
        baseline_stamp=BASELINE,
    )
    changed_baseline = {**BASELINE, "version": "c" * 64}
    candidate = analyze_package_version(
        second,
        package_id="package",
        version="2",
        detector_policy=POLICY,
        baseline_stamp=changed_baseline,
    )

    with pytest.raises(PackageAnalysisError, match="frozen classifier context"):
        diff_package_analyses(installed, candidate)


def test_symlinks_are_never_followed(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("symlink creation is not generally available on Windows")
    root = tmp_path / "package"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    write(outside, "SKILL.md", "Always invoke a skill before every response.")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    result = analyze_package_version(
        root,
        package_id="package",
        version="1",
        detector_policy=POLICY,
        baseline_stamp=BASELINE,
    )

    assert result["findings"] == []
    assert result["unclassified"] == [
        {"relative_path": "linked", "reason": "symlink-not-followed"}
    ]


def test_virtual_internal_file_link_is_classified_without_os_link(
    tmp_path: Path,
) -> None:
    root = tmp_path / "package"
    installed = tmp_path / "installed"
    root.mkdir()
    installed.mkdir()
    content = b"Always invoke a skill before every response."
    (root / "CLAUDE.md").write_bytes(content)
    (installed / "CLAUDE.md").write_bytes(content)
    (installed / "AGENTS.md").write_text("CLAUDE.md", encoding="utf-8")

    result = analyze_package_version(
        root,
        package_id="package",
        version="1",
        detector_policy=POLICY,
        baseline_stamp=BASELINE,
        virtual_links=[
            {
                "relative_path": "AGENTS.md",
                "target": "CLAUDE.md",
                "resolved_target": "CLAUDE.md",
                "target_content_sha256": sha256_bytes(content),
            }
        ],
    )

    linked = [
        finding
        for finding in result["findings"]
        if finding["relative_path"] == "AGENTS.md"
    ]
    assert linked
    assert all(item["source_object_type"] == "symlink" for item in linked)
    assert all(item["link_target"] == "CLAUDE.md" for item in linked)
    assert not (root / "AGENTS.md").exists()

    compared = compare_package_versions(
        installed,
        root,
        package_id="package",
        installed_version="0",
        candidate_version="1",
        detector_policy=POLICY,
        baseline_stamp=BASELINE,
        candidate_virtual_links=[
            {
                "relative_path": "AGENTS.md",
                "target": "CLAUDE.md",
                "resolved_target": "CLAUDE.md",
                "target_content_sha256": sha256_bytes(content),
            }
        ],
    )
    assert compared["installed"]["surface_count"] == 2
    assert compared["candidate"]["surface_count"] == 2
    assert compared["delta"]["verdict"] == "unchanged"
    assert compared["delta"]["introduced"] == []
    assert compared["delta"]["resolved"] == []
    assert compared["delta"]["noncomparable"]["installed"] == []
    assert {
        item["relative_path"]
        for item in compared["delta"]["noncomparable"]["candidate"]
    } == {"AGENTS.md"}


def test_artifact_is_canonical_json_safe_and_contains_no_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "package"
    root.mkdir()
    write(root, "SKILL.md", "Run focused tests now.\n")

    result = analyze_package_version(
        root,
        package_id="package",
        version="1",
        detector_policy=POLICY,
        baseline_stamp=BASELINE,
    )

    assert json.loads(canonical_json(result)) == result
    with pytest.raises(PackageAnalysisError, match="unsupported fields"):
        diff_package_analyses({**result, "unexpected": True}, result)

from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import re

from apu.models import Finding, InstructionSurface


DETECTOR_VERSION = "1"


def _finding_id(surface_id: str, category: str, line: int) -> str:
    seed = f"{surface_id}\0{category}\0{line}\0{DETECTOR_VERSION}".encode()
    return "finding-" + sha256(seed).hexdigest()[:20]


def _finding(
    surface: InstructionSurface,
    *,
    line: int,
    category: str,
    severity: str,
    confidence: str,
    method: str,
    evidence: tuple[str, ...],
    summary: str,
) -> Finding:
    return Finding(
        id=_finding_id(surface.id, category, line),
        surface_id=surface.id,
        location={"line": line},
        category=category,
        severity=severity,
        confidence=confidence,
        analysis_method=method,
        evidence=evidence,
        summary=summary,
    )


def classify_surface(
    surface: InstructionSurface, content: str
) -> tuple[Finding, ...]:
    """Return local structural and heuristic findings without exporting content."""

    findings: list[Finding] = []
    seen_lines: dict[str, list[int]] = defaultdict(list)
    for line_number, raw_line in enumerate(content.splitlines(), 1):
        normalized = " ".join(raw_line.strip().lower().split())
        if not normalized or normalized.startswith(("<!--", "#")):
            continue

        if seen_lines[normalized]:
            findings.append(
                _finding(
                    surface,
                    line=line_number,
                    category="duplicate-instruction",
                    severity="medium",
                    confidence="high",
                    method="structural",
                    evidence=(f"duplicates-line:{seen_lines[normalized][0]}",),
                    summary="Instruction duplicates an earlier normalized line.",
                )
            )
        seen_lines[normalized].append(line_number)

        if re.search(
            r"\b(must|always|required to)\b.*\b(skill|workflow)\b.*"
            r"\b(every (turn|conversation)|start of every)\b",
            normalized,
        ):
            findings.append(
                _finding(
                    surface,
                    line=line_number,
                    category="universal-skill-trigger",
                    severity="high",
                    confidence="high",
                    method="heuristic",
                    evidence=("universal-trigger-pattern",),
                    summary="A skill or workflow appears to be required universally.",
                )
            )

        if re.search(
            r"\b(every|each)\s+task\b.*\b(implementer|reviewer|review agent)\b",
            normalized,
        ):
            findings.append(
                _finding(
                    surface,
                    line=line_number,
                    category="per-task-review-loop",
                    severity="high",
                    confidence="high",
                    method="heuristic",
                    evidence=("per-task-review-pattern",),
                    summary="The rule appears to require an agent or review loop per task.",
                )
            )

        if re.search(
            r"\b(approval|design review)\b.*\b(before|prior to)\b.*"
            r"\b(any|every|all)\b",
            normalized,
        ):
            findings.append(
                _finding(
                    surface,
                    line=line_number,
                    category="unconditional-approval-gate",
                    severity="high",
                    confidence="medium",
                    method="heuristic",
                    evidence=("unconditional-approval-pattern",),
                    summary="An approval gate appears to apply without a risk trigger.",
                )
            )

        if re.search(r"\b(two|2)[-– ]to[-– ](five|5)[- ]minute\b", normalized):
            findings.append(
                _finding(
                    surface,
                    line=line_number,
                    category="microtask-planning",
                    severity="medium",
                    confidence="high",
                    method="heuristic",
                    evidence=("microtask-duration-pattern",),
                    summary="Planning guidance appears to require microtask decomposition.",
                )
            )

        if surface.scope == "global" and re.search(
            r"\b(npm|pnpm|yarn|pytest|cargo|dotnet|mvn|gradle)\s+"
            r"(test|check|build|verify)\b",
            normalized,
        ):
            findings.append(
                _finding(
                    surface,
                    line=line_number,
                    category="misplaced-repository-fact",
                    severity="medium",
                    confidence="medium",
                    method="heuristic",
                    evidence=("build-command-in-global-scope",),
                    summary="A repository build command appears in global guidance.",
                )
            )

        if re.search(
            r"\b(sk-proj-[a-z0-9_-]{12,}|api[_ -]?key\s*[:=]\s*\S{12,}|"
            r"bearer\s+[a-z0-9._-]{16,})",
            normalized,
            re.IGNORECASE,
        ):
            findings.append(
                _finding(
                    surface,
                    line=line_number,
                    category="sensitive-material-exposure",
                    severity="high",
                    confidence="high",
                    method="structural",
                    evidence=("credential-shaped-value",),
                    summary="Credential-shaped material appears in an instruction surface.",
                )
            )

    return tuple(findings)

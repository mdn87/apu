from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256

from apu.models import Finding, InstructionSurface

DETECTOR_VERSION = "2"

#: Categories whose only correct remediation is deleting the flagged line.
#: Every other category is surfaced for an explicit human decision because
#: removing the line would discard the rule rather than rewrite it.
AUTO_REMOVABLE_CATEGORIES = frozenset({"duplicate-instruction"})

_FENCE_PATTERN = re.compile(r"^\s*(?:`{3,}|~{3,})")
_GRAPH_START_PATTERN = re.compile(r"^\s*(?:di)?graph\s+\w*\s*\{")
_ALPHANUMERIC_PATTERN = re.compile(r"[0-9a-z]")

#: A repeated heading, marker, or short label is document structure. Only a
#: sentence-length line is substantive enough to call a duplicated instruction.
_DUPLICATE_MINIMUM_WORDS = 6

#: A rule that states when process is *not* required is not a multiplier.
_NEGATION_PATTERN = re.compile(
    r"\b(?:do not|don't|never|avoid|no need|not required|rather than|"
    r"instead of|does not|doesn't|without)\b"
)

_OBLIGATION_PATTERN = re.compile(
    r"\b(?:must|always|required|requiring|require|mandatory|shall|"
    r"absolutely|need to)\b|\bbefore (?:any|every|each)\b"
)
_UNIVERSAL_SCOPE_PATTERN = re.compile(
    r"\b(?:any|every|each|all)\s+"
    r"(?:conversation|conversations|response|responses|turn|turns|task|tasks|"
    r"message|messages|prompt|prompts|request|requests|action|actions|reply)\b"
    r"|\bstart(?:ing)? (?:of )?(?:any|every|each)\b"
    r"|\bbefore (?:any|every|each)\b"
)
_SKILL_PATTERN = re.compile(r"\b(?:skill|skills|workflow|workflows)\b")
_SPECULATIVE_THRESHOLD_PATTERN = re.compile(r"\b\d{1,3}\s*%\s*chance\b")

#: "each task" is per item; "all tasks" happens once. Only the former is a
#: per-task loop.
_REVIEW_QUANTIFIER_PATTERN = re.compile(
    r"\b(?:every|each)\s+"
    r"(?:task|tasks|change|changes|commit|commits|step|steps|feature|features|"
    r"milestone|milestones|pr|prs)\b"
)
_REVIEW_ROLE_PATTERN = re.compile(
    r"\b(?:reviewer|reviewers|review agent|code[- ]review|implementer|"
    r"subagent|sub-agent|review cycle|review loop)\b"
)

_GATE_PATTERN = re.compile(
    r"\b(?:approval|approve|design review|sign[- ]?off|brainstorm\w*|design|"
    r"plan|spec|review|use this)\b"
)
#: A gate is unconditional only when the process is demanded *before* work,
#: not merely mentioned alongside a quantifier.
_GATE_SCOPE_PATTERN = re.compile(r"\bbefore (?:any|every|each|all)\b")

_MICROTASK_PATTERN = re.compile(
    r"\b(?:two|2)[-– ]to[-– ](?:five|5)[- ]minute\b"
    r"|\b\d+\s*(?:to|[-–])\s*\d+[- ]minute\b"
    r"|\bmicro-?tasks?\b"
    r"|\bbite[- ]sized\b"
)

_BUILD_COMMAND_PATTERN = re.compile(
    r"\b(?:npm|pnpm|yarn|pytest|cargo|dotnet|mvn|gradle)\s+"
    r"(?:test|check|build|verify)\b"
)
_CREDENTIAL_PATTERN = re.compile(
    r"\b(?:sk-proj-[a-z0-9_-]{12,}|"
    r"(?:api[_ -]?key|access[_ -]?token|password)"
    r"\b[\"']?\s*[:=]\s*[\"']?[^\s\"',;}\]]+|"
    r"bearer\s+[a-z0-9._-]{16,})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DetectorPolicy:
    """Allowlisted, typed guidance inputs consumed by deterministic detectors."""

    duplicate_instruction_minimum_words: int = _DUPLICATE_MINIMUM_WORDS
    speculative_skill_threshold_enabled: bool = True

    def __post_init__(self) -> None:
        minimum = self.duplicate_instruction_minimum_words
        if (
            not isinstance(minimum, int)
            or isinstance(minimum, bool)
            or not 2 <= minimum <= 100
        ):
            raise ValueError(
                "duplicate_instruction_minimum_words must be an integer "
                "between 2 and 100"
            )
        if not isinstance(self.speculative_skill_threshold_enabled, bool):
            raise TypeError(
                "speculative_skill_threshold_enabled must be boolean"
            )


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
    surface: InstructionSurface,
    content: str,
    *,
    detector_policy: DetectorPolicy | None = None,
) -> tuple[Finding, ...]:
    """Return local structural and heuristic findings without exporting content."""

    policy = detector_policy or DetectorPolicy()
    findings: list[Finding] = []
    seen_lines: dict[str, list[int]] = defaultdict(list)
    in_fenced_block = False
    graph_depth = 0
    for line_number, raw_line in enumerate(content.splitlines(), 1):
        if _FENCE_PATTERN.match(raw_line):
            in_fenced_block = not in_fenced_block
            continue
        if in_fenced_block:
            # Fenced content is an example or a transcript, not an instruction.
            continue
        if graph_depth or _GRAPH_START_PATTERN.match(raw_line):
            # Diagram source describes a workflow; it does not impose one.
            graph_depth += raw_line.count("{") - raw_line.count("}")
            continue

        normalized = " ".join(raw_line.strip().lower().split())
        if not normalized or normalized.startswith(("<!--", "#")):
            continue
        if not _ALPHANUMERIC_PATTERN.search(normalized):
            # Rules, separators, and table borders repeat by design.
            continue

        if (
            seen_lines[normalized]
            and len(normalized.split())
            >= policy.duplicate_instruction_minimum_words
        ):
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

        multiplier_candidate = not _NEGATION_PATTERN.search(normalized)
        universal_trigger = False

        if multiplier_candidate and _SKILL_PATTERN.search(normalized):
            evidence: list[str] = []
            if _UNIVERSAL_SCOPE_PATTERN.search(
                normalized
            ) and _OBLIGATION_PATTERN.search(normalized):
                evidence.append("universal-trigger-pattern")
            if (
                policy.speculative_skill_threshold_enabled
                and _SPECULATIVE_THRESHOLD_PATTERN.search(normalized)
            ):
                evidence.append("speculative-threshold-trigger")
            if evidence:
                universal_trigger = True
                findings.append(
                    _finding(
                        surface,
                        line=line_number,
                        category="universal-skill-trigger",
                        severity="high",
                        confidence="high",
                        method="heuristic",
                        evidence=tuple(evidence),
                        summary="A skill or workflow appears to be required universally.",
                    )
                )

        if (
            multiplier_candidate
            and _REVIEW_QUANTIFIER_PATTERN.search(normalized)
            and _REVIEW_ROLE_PATTERN.search(normalized)
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

        if (
            multiplier_candidate
            # One line gets one primary diagnosis.
            and not universal_trigger
            and _OBLIGATION_PATTERN.search(normalized)
            and _GATE_PATTERN.search(normalized)
            and _GATE_SCOPE_PATTERN.search(normalized)
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

        if multiplier_candidate and _MICROTASK_PATTERN.search(normalized):
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

        if (
            surface.scope == "global"
            # A skill is portable method; example commands inside one are
            # documentation, not a repository fact that belongs elsewhere.
            and surface.kind != "skill"
            and _BUILD_COMMAND_PATTERN.search(normalized)
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

        if _CREDENTIAL_PATTERN.search(normalized):
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

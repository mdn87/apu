from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import subprocess
from typing import Any, Iterable

from apu import __version__
from apu.classify import classify_surface
from apu.discovery import discover
from apu.models import Finding, Inventory, SurfaceRelationship
from apu.trace import summarize_sessions


_CLASSIFIABLE_KINDS = frozenset(
    {
        "codex-instructions",
        "claude-instructions",
        "claude-local-instructions",
        "claude-rule",
        "claude-import",
        "skill",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _relationship_finding(relationship: SurfaceRelationship) -> Finding | None:
    if relationship.status not in {
        "missing",
        "cycle",
        "max_depth",
        "unreadable",
        "orphaned",
    }:
        return None
    category = (
        "unsupported-or-broken-import"
        if relationship.type == "imports"
        else "orphaned-managed-surface"
    )
    line = relationship.location.get("line")
    normalized_line = line if isinstance(line, int) else 0
    digest = sha256(
        (
            f"{relationship.from_surface_id}\0{category}\0"
            f"{normalized_line}\0{relationship.status}"
        ).encode()
    ).hexdigest()[:20]
    return Finding(
        id=f"finding-{digest}",
        surface_id=relationship.from_surface_id,
        location={"line": normalized_line},
        category=category,
        severity="medium",
        confidence="high",
        analysis_method="structural",
        evidence=(f"relationship:{relationship.status}",),
        summary=f"Instruction relationship is {relationship.status}.",
    )


def _git_summary(repository: Path) -> dict[str, Any]:
    root = repository.expanduser().resolve()
    try:
        commits = subprocess.run(
            ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        files = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError) as error:
        return {"available": False, "error": type(error).__name__}
    return {
        "available": True,
        "commits": int(commits or 0),
        "tracked_files": len(files),
    }


def build_inventory(
    roots: Iterable[Path],
    *,
    home: Path,
    working_directories: Iterable[Path] = (),
    session_paths: Iterable[Path] = (),
    root_session_id: str | None = None,
    git_repository: Path | None = None,
    generated_at: str | None = None,
) -> Inventory:
    roots = tuple(Path(root).expanduser().resolve() for root in roots)
    working_directories = tuple(
        Path(path).expanduser().resolve() for path in working_directories
    )
    session_paths = tuple(Path(path).expanduser() for path in session_paths)
    if root_session_id is not None and not session_paths:
        raise ValueError("root_session_id requires at least one session path")

    discovery = discover(
        roots,
        home=home,
        working_directories=working_directories or roots,
    )
    findings: list[Finding] = []
    for surface in discovery.surfaces:
        if surface.kind not in _CLASSIFIABLE_KINDS:
            continue
        try:
            content = Path(surface.path).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
        findings.extend(classify_surface(surface, content))
    findings.extend(
        finding
        for relationship in discovery.relationships
        if (finding := _relationship_finding(relationship)) is not None
    )

    evidence: dict[str, Any] = {
        "sessions": (
            summarize_sessions(session_paths, root_session_id=root_session_id)
            if session_paths
            else None
        ),
        "git": _git_summary(git_repository) if git_repository else None,
        "privacy": "Message, prompt, tool input, and environment content is not emitted.",
    }
    return Inventory(
        schema_version=1,
        apu_version=__version__,
        generated_at=generated_at or _now(),
        scope={
            "roots": [str(path) for path in roots],
            "working_directories": [
                str(path) for path in (working_directories or roots)
            ],
            "root_session_id": root_session_id,
        },
        surfaces=discovery.surfaces,
        relationships=discovery.relationships,
        effective_stacks=discovery.effective_stacks,
        findings=tuple(sorted(findings, key=lambda item: item.id)),
        evidence_summary=evidence,
    )

from __future__ import annotations

from pathlib import Path

from apu.adapters.base import DiscoveryResult
from apu.models import InstructionSurface, SurfaceRelationship
from apu.precedence import effective_stack


def surface(path: Path, *, kind: str, authority: str, precedence: int):
    identifier = f"id:{path.name}:{precedence}"
    return InstructionSurface(
        id=identifier,
        path=str(path),
        kind=kind,
        provider="claude",
        authority=authority,
        scope=authority,
        real_path=str(path),
        is_symlink=False,
        content_sha256="a" * 64,
        mode="0644",
        precedence=precedence,
        sensitive=False,
    )


def test_active_import_is_inserted_after_its_importer(tmp_path: Path) -> None:
    cwd = tmp_path / "repo" / "src"
    cwd.mkdir(parents=True)
    main = surface(
        tmp_path / "repo" / "CLAUDE.md",
        kind="claude-instructions",
        authority="repository",
        precedence=20,
    )
    imported = surface(
        tmp_path / "repo" / "policy.md",
        kind="claude-import",
        authority="repository",
        precedence=20,
    )
    local = surface(
        tmp_path / "repo" / "CLAUDE.local.md",
        kind="claude-local-instructions",
        authority="repository",
        precedence=21,
    )
    discovery = DiscoveryResult(
        surfaces=(main, imported, local),
        relationships=(
            SurfaceRelationship(
                type="imports",
                from_surface_id=main.id,
                to_surface_id=imported.id,
                status="active",
            ),
        ),
    )

    assert effective_stack(cwd, discovery, "claude") == (
        main.id,
        imported.id,
        local.id,
    )


def test_inactive_import_and_non_instruction_metadata_are_not_effective(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    main = surface(
        cwd / "CLAUDE.md",
        kind="claude-instructions",
        authority="repository",
        precedence=20,
    )
    missing = surface(
        cwd / "missing.md",
        kind="claude-import",
        authority="repository",
        precedence=20,
    )
    marketplace = surface(
        cwd / ".claude" / "plugins" / "known_marketplaces.json",
        kind="claude-marketplace",
        authority="repository",
        precedence=90,
    )
    discovery = DiscoveryResult(
        surfaces=(main, missing, marketplace),
        relationships=(
            SurfaceRelationship(
                type="imports",
                from_surface_id=main.id,
                to_surface_id=missing.id,
                status="missing",
            ),
        ),
    )

    assert effective_stack(cwd, discovery, "claude") == (main.id,)

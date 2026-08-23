from __future__ import annotations

import json
from pathlib import Path

from apu.cli import build_parser, main
from apu.models import Finding, InstructionSurface, Inventory, sha256_bytes
from apu.system_audit import (
    SYSTEM_INVENTORY_SCHEMA_VERSION,
    EvaluationContext,
    SystemInventory,
)
from apu.system_profile import load_system_profile


def test_system_propose_accepts_explicit_instruction_consolidation_task(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    args = build_parser().parse_args(
        [
            "system",
            "propose",
            "--inventory",
            str(tmp_path / "inventory.json"),
            "--consolidate-instructions",
            str(repository),
        ]
    )

    assert args.consolidate_instructions == repository


def test_cli_campaign_propose_apply_and_status(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    projects = tmp_path / "projects"
    projects.mkdir()
    global_root = home / ".codex"
    global_root.mkdir(parents=True)
    target = global_root / "AGENTS.md"
    target.write_text("keep\nremove duplicate\n", encoding="utf-8")
    secret = "sk-proj-ABCDEFGHIJKLMNOP"
    sensitive = global_root / "settings.json"
    sensitive.write_text(f'{{"api_key":"{secret}"}}\n', encoding="utf-8")
    profile_path = tmp_path / "profile.toml"
    profile_path.write_text(
        f'roots = ["{projects.as_posix()}"]\n'
        # Exclude the Codex runtime scratch. Without this the outcome depends
        # on whether a real Codex CLI is installed on the machine running the
        # tests: it writes symlinked arg0 dispatch shims under .codex/tmp/arg0
        # that all resolve to the same binary, which reads as ambiguous target
        # coverage.
        f'global_surfaces = [{{ path = "{global_root.as_posix()}", '
        'excludes = ["tmp/**"] }]\n'
        "[remediation_policy]\n"
        'duplicate-instruction = "auto"\n'
        'sensitive-material-exposure = "ignore"\n',
        encoding="utf-8",
    )
    profile = load_system_profile(profile_path, home=home)
    auto_surface = InstructionSurface(
        id="auto",
        path=str(target.resolve()),
        kind="agents",
        provider="codex",
        authority="user",
        scope="global",
        real_path=str(target.resolve()),
        is_symlink=False,
        content_sha256=sha256_bytes(target.read_bytes()),
        mode="0644",
        precedence=10,
        sensitive=False,
    )
    sensitive_surface = InstructionSurface(
        id="sensitive",
        path=str(sensitive.resolve()),
        kind="settings",
        provider="codex",
        authority="user",
        scope="global",
        real_path=str(sensitive.resolve()),
        is_symlink=False,
        content_sha256=sha256_bytes(sensitive.read_bytes()),
        mode="0600",
        precedence=10,
        sensitive=True,
    )
    machine = Inventory(
        schema_version=1,
        apu_version="0.3.0.dev0",
        generated_at="2026-08-06T22:00:00Z",
        scope={"roots": [str(global_root)]},
        surfaces=(auto_surface, sensitive_surface),
        findings=(
            Finding(
                id="duplicate",
                surface_id="auto",
                location={"line": 2},
                category="duplicate-instruction",
                severity="medium",
                confidence="high",
                analysis_method="structural",
                evidence=("fixture",),
                summary="Duplicate instruction.",
            ),
            Finding(
                id="credential",
                surface_id="sensitive",
                location={"line": 1},
                category="sensitive-material-exposure",
                severity="high",
                confidence="high",
                analysis_method="structural",
                evidence=("credential-shaped-value",),
                summary="Credential-shaped material.",
            ),
        ),
    )
    inventory = SystemInventory(
        schema_version=SYSTEM_INVENTORY_SCHEMA_VERSION,
        apu_version="0.3.0.dev0",
        generated_at="2026-08-06T22:00:00Z",
        profile_sha256=profile.artifact_sha256,
        machine_inventory=machine,
        repositories=(),
        evaluation_context=EvaluationContext.unconfigured(),
    )
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(
        json.dumps(inventory.to_dict()),
        encoding="utf-8",
    )
    bundle_path = tmp_path / "campaign.json"
    exports = tmp_path / "exports"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APU_HOME", str(state))

    assert (
        main(
            [
                "system",
                "propose",
                "--inventory",
                str(inventory_path),
                "--profile",
                str(profile_path),
                "--emit-prompts",
                str(exports),
                "--output",
                str(bundle_path),
            ]
        )
        == 0
    )
    assert "outside APU state protection" in capsys.readouterr().err
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert bundle["campaign_id"].startswith("campaign-")
    exported = "\n".join(
        path.read_text(encoding="utf-8") for path in exports.glob("*.md")
    )
    assert secret not in exported
    assert "MANUAL ONLY" in exported

    assert (
        main(
            [
                "system",
                "apply",
                str(bundle_path),
                "--profile",
                str(profile_path),
                "--yes",
            ]
        )
        == 1
    )
    assert "--auto-only" in capsys.readouterr().err
    assert (
        main(
            [
                "system",
                "apply",
                str(bundle_path),
                "--profile",
                str(profile_path),
                "--auto-only",
                "--yes",
                "--installation-id",
                "cli-campaign",
            ]
        )
        == 0
    )
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"
    assert target.read_text(encoding="utf-8") == "keep\n"

    assert main(["system", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["campaigns"][0]["campaign_id"] == bundle["campaign_id"]
    assert status["campaigns"][0]["snapshot_id"] == applied["snapshot_id"]

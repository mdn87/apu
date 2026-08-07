from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from apu.cli import main
from apu.guidance import FetchResponse
from apu.model_registry import ModelObservation
from apu.system_audit import SYSTEM_INVENTORY_SCHEMA_VERSION


def _profile(tmp_path: Path, *, guidance: bool = False) -> Path:
    projects = tmp_path / "projects"
    projects.mkdir()
    path = tmp_path / "profile.toml"
    source = (
        'guidance_sources = ["https://example.test/guidance"]\n'
        if guidance
        else ""
    )
    path.write_text(
        "schema_version = 1\n"
        "global_surfaces = []\n"
        f'roots = ["{projects.as_posix()}"]\n'
        f"{source}",
        encoding="utf-8",
    )
    return path


def test_system_audit_stamps_offline_local_observations_without_network(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile = _profile(tmp_path)
    state = tmp_path / "missing-state"
    output = tmp_path / "inventory.json"
    monkeypatch.setenv("APU_HOME", str(state))
    monkeypatch.setattr(
        "apu.refresh.fetch_guidance_source",
        lambda _url: (_ for _ in ()).throw(AssertionError("networked")),
    )
    monkeypatch.setattr(
        "apu.refresh.fetch_provider_models",
        lambda _source: (_ for _ in ()).throw(AssertionError("networked")),
    )
    monkeypatch.setattr(
        "apu.model_registry.observe_local_models",
        lambda *_args, **_kwargs: (
            ModelObservation(
                runtime_id="codex-cli",
                provider="openai",
                cli_version="1.0.0",
                configured_model="gpt-test",
                raw_alias="gpt-test",
                observed_at="2026-08-07T04:00:00Z",
            ),
        ),
    )

    assert (
        main(
            [
                "system",
                "audit",
                "--profile",
                str(profile),
                "--json",
                str(output),
            ]
        )
        == 0
    )

    inventory = json.loads(output.read_text(encoding="utf-8"))
    assert inventory["schema_version"] == SYSTEM_INVENTORY_SCHEMA_VERSION
    context = inventory["evaluation_context"]
    assert context["baseline"]["status"] == "unconfigured"
    assert context["models"]["status"] == "unverified"
    assert context["models"]["generation"] is None
    assert len(context["models"]["artifact_sha256"]) == 64
    assert context["models"]["identities"][0]["raw_alias"] == "gpt-test"
    artifact = (
        state
        / "models"
        / "registries"
        / f"{context['models']['artifact_sha256']}.json"
    )
    assert artifact.is_file()


def test_refresh_guidance_fetches_only_on_explicit_command(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    profile = _profile(tmp_path, guidance=True)
    state = tmp_path / "state"
    raw = b"Use narrowly scoped instructions."
    monkeypatch.setenv("APU_HOME", str(state))
    monkeypatch.setattr(
        "apu.refresh.fetch_guidance_source",
        lambda _url: FetchResponse(raw, "text/plain"),
    )

    assert main(["refresh", "guidance", "--profile", str(profile)]) == 0

    captured = capsys.readouterr().out
    result = json.loads(captured)
    assert result["refresh"]["sources"][0]["status"] == "fresh"
    assert "content" not in result["refresh"]["sources"][0]
    assert raw not in captured.encode()
    object_path = next((state / "guidance" / "objects").glob("*.bin"))
    assert object_path.read_bytes() == raw
    assert result["distillation_work_order"]["candidate_schema"][
        "fixed_values"
    ]["artifact_type"] == "guidance-baseline-candidate"


def test_refresh_models_persists_verified_immutable_registry(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    profile = _profile(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setenv("APU_HOME", str(state))
    observations = (
        ModelObservation(
            runtime_id="claude-cli",
            provider="anthropic",
            cli_version="2.0.0",
            configured_model="claude-sonnet-4-20250514",
            raw_alias="claude-sonnet-4-20250514",
            observed_at="2026-08-07T04:00:00Z",
        ),
        ModelObservation(
            runtime_id="codex-cli",
            provider="openai",
            cli_version="1.0.0",
            configured_model="gpt-5.6-sol",
            raw_alias="gpt-5.6-sol",
            observed_at="2026-08-07T04:00:00Z",
        ),
    )
    monkeypatch.setattr(
        "apu.model_registry.observe_local_models",
        lambda *_args, **_kwargs: observations,
    )
    monkeypatch.setattr(
        "apu.refresh.fetch_provider_models",
        lambda source: {
            "models": [
                (
                    "gpt-5.6-sol"
                    if source.provider == "openai"
                    else "claude-sonnet-4-20250514"
                )
            ]
        },
    )

    assert main(["refresh", "models", "--profile", str(profile)]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["registry"]["refresh_status"] == "current"
    assert result["registry"]["generation"].startswith("models-sha256:")
    artifact = (
        state
        / "models"
        / "registries"
        / f"{result['artifact_sha256']}.json"
    )
    assert artifact.is_file()
    pointer = json.loads(
        (state / "models" / "registry.json").read_text(encoding="utf-8")
    )
    assert pointer["artifact_sha256"] == result["artifact_sha256"]

    later = tuple(
        replace(item, observed_at="2026-08-07T05:00:00Z")
        for item in observations
    )
    monkeypatch.setattr(
        "apu.model_registry.observe_local_models",
        lambda *_args, **_kwargs: later,
    )
    assert main(["system", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    context = status["evaluation_context"]["models"]
    assert context["status"] == "current"
    assert context["generation"] == result["registry"]["generation"]
    assert context["artifact_sha256"] == result["artifact_sha256"]


def test_system_status_is_offline_and_exposes_evaluation_context(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("APU_HOME", str(state))
    monkeypatch.setattr(
        "apu.refresh.fetch_guidance_source",
        lambda _url: (_ for _ in ()).throw(AssertionError("networked")),
    )
    monkeypatch.setattr(
        "apu.refresh.fetch_provider_models",
        lambda _source: (_ for _ in ()).throw(AssertionError("networked")),
    )

    assert main(["system", "status"]) == 0

    status = json.loads(capsys.readouterr().out)
    assert status["evaluation_context"]["baseline"]["status"] == "unconfigured"
    assert status["evaluation_context"]["models"]["status"] == "unverified"


def test_cli_reports_malformed_artifact_shapes_without_traceback(
    tmp_path: Path,
    capsys,
) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("[]", encoding="utf-8")

    assert main(["guidance", "diff", str(malformed), str(malformed)]) == 1

    captured = capsys.readouterr()
    assert "JSON object" in captured.err
    assert "Traceback" not in captured.err

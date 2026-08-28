from __future__ import annotations

import json
from pathlib import Path

import pytest

from apu.refresh import (
    ANTHROPIC_MODELS_URL,
    OPENAI_MODELS_URL,
    _normalize_models,
    _validate_guidance_url,
    fetch_provider_models,
    published_model_sources,
    runtime_model_configs,
)


def test_runtime_model_configs_observe_documented_local_selectors(
    tmp_path: Path,
) -> None:
    codex = tmp_path / ".codex"
    claude = tmp_path / ".claude"
    codex.mkdir()
    claude.mkdir()
    (codex / "config.toml").write_text(
        'model = "gpt-5.6-sol"\nmodel_provider = "openai"\n',
        encoding="utf-8",
    )
    (claude / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_MODEL": "claude-sonnet-4-20250514"}}),
        encoding="utf-8",
    )

    configs = runtime_model_configs(tmp_path, environment={})

    assert [(item.runtime_id, item.configured_model) for item in configs] == [
        ("claude-cli", "claude-sonnet-4-20250514"),
        ("codex-cli", "gpt-5.6-sol"),
    ]
    assert configs[0].version_command == ("claude", "--version")
    assert configs[1].version_command == ("codex", "--version")


def test_runtime_model_configs_surface_malformed_provider_configuration(
    tmp_path: Path,
) -> None:
    codex = tmp_path / ".codex"
    claude = tmp_path / ".claude"
    codex.mkdir()
    claude.mkdir()
    (codex / "config.toml").write_text("model = [not-valid\n", encoding="utf-8")
    (claude / "settings.json").write_text("{not-valid", encoding="utf-8")

    configs = runtime_model_configs(tmp_path, environment={})

    assert [(item.runtime_id, item.configuration_error) for item in configs] == [
        ("claude-cli", "invalid_json"),
        ("codex-cli", "invalid_toml"),
    ]
    assert all(item.configured_model is None for item in configs)


def test_runtime_model_configs_surface_unreadable_provider_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_config = tmp_path / ".codex" / "config.toml"
    codex_config.parent.mkdir()
    codex_config.write_text('model = "gpt-test"\n', encoding="utf-8")
    original_read_text = Path.read_text

    def read_text(path: Path, *args, **kwargs) -> str:
        if path == codex_config:
            raise PermissionError("fixture is unreadable")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    configs = runtime_model_configs(tmp_path, environment={})

    codex = next(item for item in configs if item.runtime_id == "codex-cli")
    assert codex.configuration_error == "unreadable_toml"
    assert codex.configured_model is None


def test_model_sources_are_fixed_and_runtime_authenticated() -> None:
    sources = published_model_sources()

    assert sources["openai"].source_url == OPENAI_MODELS_URL
    assert sources["openai"].credential_env == "OPENAI_API_KEY"
    assert sources["anthropic"].source_url == ANTHROPIC_MODELS_URL
    assert sources["anthropic"].credential_env == "ANTHROPIC_API_KEY"


def test_provider_payload_normalization_never_invents_aliases() -> None:
    normalized = _normalize_models(
        {"data": [{"id": "model-b"}, {"id": "model-a"}]},
        provider="fixture",
    )

    assert normalized == {"models": [{"id": "model-a"}, {"id": "model-b"}]}
    assert "aliases" not in normalized
    assert "default" not in normalized


def test_anthropic_listing_uses_safe_shared_transport(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fetch(source, **kwargs):
        captured["source"] = source
        captured.update(kwargs)
        return {"data": [{"id": "claude-test"}]}

    monkeypatch.setattr("apu.refresh.fetch_published_listing", fetch)

    result = fetch_provider_models(published_model_sources()["anthropic"])

    assert result == {"models": [{"id": "claude-test"}]}
    assert captured["additional_headers"] == {"anthropic-version": "2023-06-01"}


@pytest.mark.parametrize(
    "source",
    [
        "http://example.test/guide",
        "https://user:secret@example.test/guide",
        "file:///tmp/guide",
        "https://example.test/guide#fragment",
    ],
)
def test_guidance_transport_rejects_unsafe_sources(source: str) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        _validate_guidance_url(source)

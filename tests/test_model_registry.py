from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import pytest

import apu.model_registry as model_registry_module
from apu.model_registry import (
    DEFAULT_MODEL_ALIAS,
    ModelObservation,
    PublishedModelSource,
    RuntimeModelConfig,
    derive_model_generation,
    derive_registry_generation,
    fetch_published_listing,
    load_model_registry,
    load_model_registry_artifact,
    model_registry_artifact_sha256,
    observe_local_models,
    qualify_canonical_identity,
    reconcile_model_registry_observations,
    refresh_model_registry,
)
from apu.models import canonical_json

NOW = datetime(2026, 8, 7, 1, 2, 3, tzinfo=UTC)
LATER = datetime(2026, 8, 8, 4, 5, 6, tzinfo=UTC)


def _observation(
    *,
    runtime_id: str = "codex",
    provider: str = "openai",
    cli_version: str | None = "codex-cli 1.2.3",
    configured_model: str | None = "stable",
    observed_at: str = "2026-08-07T01:00:00Z",
) -> ModelObservation:
    return ModelObservation(
        runtime_id=runtime_id,
        provider=provider,
        cli_version=cli_version,
        configured_model=configured_model,
        raw_alias=configured_model or DEFAULT_MODEL_ALIAS,
        observed_at=observed_at,
    )


def _source(*, credential_env: str | None = None) -> PublishedModelSource:
    return PublishedModelSource(
        provider="openai",
        source_url="https://api.provider.test/v1/models",
        credential_env=credential_env,
    )


def test_observe_local_models_is_offline_and_preserves_default_selector() -> None:
    commands: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...]) -> str:
        commands.append(command)
        return " codex-cli 1.2.3 \n"

    observed = observe_local_models(
        [
            RuntimeModelConfig(
                runtime_id="codex",
                provider="openai",
                version_command=("codex", "--version"),
                configured_model=None,
            )
        ],
        command_runner=run,
        observed_at=NOW,
    )

    assert commands == [("codex", "--version")]
    assert observed == (
        ModelObservation(
            runtime_id="codex",
            provider="openai",
            cli_version="codex-cli 1.2.3",
            configured_model=None,
            raw_alias=DEFAULT_MODEL_ALIAS,
            observed_at="2026-08-07T01:02:03Z",
            observation_error=None,
        ),
    )


def test_observation_failure_is_visible_without_blocking_other_runtimes() -> None:
    def run(command: tuple[str, ...]) -> str:
        if command[0] == "missing":
            raise FileNotFoundError("not installed")
        return "claude 9.0"

    observed = observe_local_models(
        [
            RuntimeModelConfig(
                "missing-runtime", "openai", ("missing", "--version"), "alpha"
            ),
            RuntimeModelConfig(
                "claude", "anthropic", ("claude", "--version"), "sonnet"
            ),
        ],
        command_runner=run,
        observed_at=NOW,
    )

    assert [item.runtime_id for item in observed] == ["claude", "missing-runtime"]
    assert observed[0].cli_version == "claude 9.0"
    assert observed[1].cli_version is None
    assert observed[1].observation_error == "FileNotFoundError"


def test_refresh_resolves_alias_with_provenance_and_atomic_private_state(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    source = _source(credential_env="PROVIDER_TOKEN")
    fetch_calls: list[PublishedModelSource] = []

    def fetch(item: PublishedModelSource) -> dict:
        fetch_calls.append(item)
        return {
            "aliases": {"stable": "openai/gpt-5.7-2026-08-01"},
            "models": ["openai/gpt-5.7-2026-08-01"],
        }

    registry = refresh_model_registry(
        state_home,
        [_observation()],
        {"openai": source},
        fetcher=fetch,
        attempted_at=NOW,
    )

    assert fetch_calls == [source]
    entry = registry["models"]["codex"]
    qualified = qualify_canonical_identity(
        "openai",
        "codex",
        "openai/gpt-5.7-2026-08-01",
    )
    assert entry["resolution"] == {
        "canonical_identity": qualified,
        "provider": "openai",
        "provider_model_id": "openai/gpt-5.7-2026-08-01",
        "raw_alias": "stable",
        "retrieval_date": "2026-08-07",
        "retrieved_at": "2026-08-07T01:02:03Z",
        "runtime_id": "codex",
        "source_url": "https://api.provider.test/v1/models",
    }
    assert entry["generation"] == derive_model_generation(qualified)
    assert entry["last_known_generation"] == entry["generation"]
    assert entry["last_known_resolution"] == entry["resolution"]
    assert registry["generation"] == derive_registry_generation([qualified])
    assert registry["last_known_generation"] == registry["generation"]
    assert registry["last_known_identities"] == [qualified]
    assert registry["refresh_status"] == "current"
    assert registry["last_successful_refresh_at"] == "2026-08-07T01:02:03Z"

    pointer_path = state_home / "models" / "registry.json"
    artifact_sha256 = model_registry_artifact_sha256(registry)
    artifact_path = state_home / "models" / "registries" / f"{artifact_sha256}.json"
    pointer = {
        "artifact_path": f"models/registries/{artifact_sha256}.json",
        "artifact_sha256": artifact_sha256,
        "schema_version": 2,
    }
    assert pointer_path.read_text(encoding="utf-8") == canonical_json(pointer)
    assert artifact_path.read_text(encoding="utf-8") == canonical_json(registry)
    assert load_model_registry(state_home) == registry
    assert load_model_registry_artifact(state_home, artifact_sha256) == registry
    assert not list(pointer_path.parent.glob(".registry.json.*"))
    assert not list(artifact_path.parent.glob(f".{artifact_sha256}.json.*"))
    assert "PROVIDER_TOKEN" not in pointer_path.read_text(encoding="utf-8")
    assert "PROVIDER_TOKEN" not in artifact_path.read_text(encoding="utf-8")
    if os.name == "posix":
        assert artifact_path.parent.stat().st_mode & 0o777 == 0o700
        assert pointer_path.stat().st_mode & 0o777 == 0o600
        assert artifact_path.stat().st_mode & 0o777 == 0o600


def test_generation_is_qualified_by_provider_runtime_and_active_set() -> None:
    openai_codex = qualify_canonical_identity("openai", "codex", "shared-id")
    anthropic_claude = qualify_canonical_identity(
        "anthropic",
        "claude",
        "shared-id",
    )
    openai_other = qualify_canonical_identity("openai", "other", "shared-id")

    assert derive_model_generation(openai_codex) != derive_model_generation(
        anthropic_claude
    )
    assert derive_model_generation(openai_codex) != derive_model_generation(
        openai_other
    )
    assert derive_registry_generation([openai_codex]) != derive_registry_generation(
        [openai_codex, openai_other]
    )


def test_provider_default_move_is_a_visible_generation_change(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    source = _source()
    current = {"identity": "openai/gpt-5.7-2026-08-01"}

    def fetch(_: PublishedModelSource) -> dict:
        return {
            "default": current["identity"],
            "models": [current["identity"]],
        }

    observation = _observation(configured_model=None)
    first = refresh_model_registry(
        state_home,
        [observation],
        {"openai": source},
        fetcher=fetch,
        attempted_at=NOW,
    )
    current["identity"] = "openai/gpt-5.8-2026-09-01"
    second = refresh_model_registry(
        state_home,
        [observation],
        {"openai": source},
        fetcher=fetch,
        attempted_at=LATER,
    )

    first_entry = first["models"]["codex"]
    second_entry = second["models"]["codex"]
    assert second_entry["resolution"]["raw_alias"] == DEFAULT_MODEL_ALIAS
    assert second_entry["previous_generation"] == first_entry["generation"]
    assert second_entry["generation_changed"] is True
    assert second["previous_generation"] == first["generation"]
    assert second["generation_changed"] is True
    first_hash = model_registry_artifact_sha256(first)
    second_hash = model_registry_artifact_sha256(second)
    assert first_hash != second_hash
    assert load_model_registry_artifact(state_home, first_hash) == first
    assert load_model_registry_artifact(state_home, second_hash) == second
    assert len(list((state_home / "models" / "registries").glob("*.json"))) == 2


def test_failed_refresh_retains_matching_last_known_identity_as_stale(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    source = _source()
    refresh_model_registry(
        state_home,
        [_observation()],
        {"openai": source},
        fetcher=lambda _: {
            "aliases": {"stable": "openai/gpt-5.7"},
            "models": ["openai/gpt-5.7"],
        },
        attempted_at=NOW,
    )

    def offline(_: PublishedModelSource) -> dict:
        raise ConnectionError("Authorization: Bearer must-not-persist")

    stale = refresh_model_registry(
        state_home,
        [_observation(cli_version="codex-cli 2.0")],
        {"openai": source},
        fetcher=offline,
        attempted_at=LATER,
    )

    entry = stale["models"]["codex"]
    assert entry["resolution"]["canonical_identity"] == qualify_canonical_identity(
        "openai",
        "codex",
        "openai/gpt-5.7",
    )
    assert entry["observation"]["cli_version"] == "codex-cli 2.0"
    assert entry["verification"] == {
        "refresh_error": "ConnectionError",
        "status": "stale",
        "status_message": "model identity unverified since 2026-08-07",
        "unverified_since": "2026-08-07",
    }
    assert stale["generation_changed"] is False
    assert stale["last_successful_refresh_at"] == "2026-08-07T01:02:03Z"
    assert stale["refresh_status"] == "degraded"
    assert "must-not-persist" not in (
        state_home / "models" / "registry.json"
    ).read_text(encoding="utf-8")
    assert all(
        "must-not-persist" not in path.read_text(encoding="utf-8")
        for path in (state_home / "models" / "registries").glob("*.json")
    )


def test_generation_change_survives_g1_failure_g2_sequence(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    source = _source()
    selected = {"model": "openai/g1"}

    def fetch(_: PublishedModelSource) -> dict:
        return {
            "aliases": {"stable": selected["model"]},
            "models": [selected["model"]],
        }

    first = refresh_model_registry(
        state_home,
        [_observation()],
        {"openai": source},
        fetcher=fetch,
        attempted_at=NOW,
    )

    def fail(_: PublishedModelSource) -> dict:
        raise ConnectionError("offline")

    failed = refresh_model_registry(
        state_home,
        [_observation()],
        {"openai": source},
        fetcher=fail,
        attempted_at=LATER,
    )
    selected["model"] = "openai/g2"
    recovered = refresh_model_registry(
        state_home,
        [_observation()],
        {"openai": source},
        fetcher=fetch,
        attempted_at="2026-08-09T04:05:06Z",
    )

    g1 = first["models"]["codex"]["generation"]
    g2 = recovered["models"]["codex"]["generation"]
    assert failed["models"]["codex"]["last_known_generation"] == g1
    assert failed["last_known_generation"] == first["generation"]
    assert recovered["models"]["codex"]["previous_generation"] == g1
    assert recovered["models"]["codex"]["generation_changed"] is True
    assert recovered["previous_generation"] == first["generation"]
    assert recovered["generation_changed"] is True
    assert recovered["last_known_generation"] == recovered["generation"]
    assert g2 != g1


def test_failed_refresh_never_carries_identity_across_alias_change(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    source = _source()
    refresh_model_registry(
        state_home,
        [_observation(configured_model="old-alias")],
        {"openai": source},
        fetcher=lambda _: {
            "aliases": {"old-alias": "openai/gpt-5.7"},
            "models": ["openai/gpt-5.7"],
        },
        attempted_at=NOW,
    )

    def offline(_: PublishedModelSource) -> dict:
        raise ConnectionError("offline")

    result = refresh_model_registry(
        state_home,
        [_observation(configured_model="new-alias")],
        {"openai": source},
        fetcher=offline,
        attempted_at=LATER,
    )

    entry = result["models"]["codex"]
    assert entry["resolution"] is None
    assert entry["generation"] is None
    assert entry["verification"]["status"] == "unverified"
    assert entry["verification"]["unverified_since"] == "2026-08-08"
    assert result["generation"] is None
    assert result["last_known_generation"] is not None


def test_pure_observation_reconciliation_invalidates_selector_drift(
    tmp_path: Path,
) -> None:
    registry = refresh_model_registry(
        tmp_path / "state",
        [_observation()],
        {"openai": _source()},
        fetcher=lambda _: {
            "aliases": {"stable": "openai/g1"},
            "models": ["openai/g1"],
        },
        attempted_at=NOW,
    )
    reconciled = reconcile_model_registry_observations(
        registry,
        [_observation(configured_model="new-local-alias")],
    )

    entry = reconciled["models"]["codex"]
    assert entry["resolution"] is None
    assert entry["generation"] is None
    assert entry["last_known_generation"] == registry["models"]["codex"]["generation"]
    assert entry["verification"]["refresh_error"] == "_LocalObservationDrift"
    assert reconciled["generation"] is None
    assert reconciled["last_known_generation"] == registry["generation"]
    assert reconciled["refresh_attempted_at"] == registry["refresh_attempted_at"]


def test_pure_observation_reconciliation_invalidates_cli_version_drift(
    tmp_path: Path,
) -> None:
    registry = refresh_model_registry(
        tmp_path / "state",
        [_observation()],
        {"openai": _source()},
        fetcher=lambda _: {
            "aliases": {"stable": "openai/g1"},
            "models": ["openai/g1"],
        },
        attempted_at=NOW,
    )

    reconciled = reconcile_model_registry_observations(
        registry,
        [
            _observation(
                cli_version="codex-cli 2.0.0",
                observed_at="2026-08-07T02:00:00Z",
            )
        ],
    )

    entry = reconciled["models"]["codex"]
    assert entry["resolution"] is None
    assert entry["generation"] is None
    assert entry["verification"]["status"] == "unverified"
    assert entry["last_known_generation"] == registry["models"]["codex"]["generation"]
    assert reconciled["generation"] is None
    assert reconciled["last_known_generation"] == registry["generation"]


def test_partial_resolution_never_presents_a_subset_as_system_generation(
    tmp_path: Path,
) -> None:
    result = refresh_model_registry(
        tmp_path / "state",
        [
            _observation(),
            _observation(
                runtime_id="other",
                configured_model="unknown",
            ),
        ],
        {"openai": _source()},
        fetcher=lambda _: {
            "aliases": {"stable": "openai/gpt-5.7"},
            "models": ["openai/gpt-5.7"],
        },
        attempted_at=NOW,
    )

    assert result["models"]["codex"]["generation"] is not None
    assert result["models"]["other"]["generation"] is None
    assert result["generation"] is None
    assert result["refresh_status"] == "degraded"


def test_fetch_authentication_is_runtime_only_and_uses_injected_opener() -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"models":["openai/gpt-test"]}'

        def geturl(self) -> str:
            return "https://api.provider.test/v1/models"

    def opener(request: object, *, timeout: int) -> Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    source = _source(credential_env="PROVIDER_TOKEN")
    listing = fetch_published_listing(
        source,
        env={"PROVIDER_TOKEN": "runtime-only-value"},
        opener=opener,
    )

    request = captured["request"]
    assert listing == {"models": ["openai/gpt-test"]}
    assert captured["timeout"] == 30
    assert request.get_header("Authorization") == "Bearer runtime-only-value"
    assert request.full_url == source.source_url
    assert "runtime-only-value" not in repr(source)


def test_default_authenticated_transport_installs_no_redirect_handler(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"models":[]}'

        def geturl(self) -> str:
            return "https://api.provider.test/v1/models"

    class Opener:
        def open(self, request: object, *, timeout: int) -> Response:
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    def build(*handlers: object) -> Opener:
        captured["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(model_registry_module, "build_opener", build)

    fetch_published_listing(
        _source(credential_env="PROVIDER_TOKEN"),
        env={"PROVIDER_TOKEN": "runtime-only-value"},
    )

    handler = captured["handlers"][0]
    assert (
        handler.redirect_request(
            captured["request"],
            None,
            302,
            "Found",
            {},
            "https://other.test/models",
        )
        is None
    )


def test_listing_fetch_rejects_non_https_sources_and_changed_origins() -> None:
    insecure = PublishedModelSource(
        provider="openai",
        source_url="http://api.provider.test/v1/models",
    )
    with pytest.raises(ValueError, match="HTTPS"):
        fetch_published_listing(
            insecure,
            env={},
            opener=lambda *_args, **_kwargs: pytest.fail("must not open"),
        )

    class RedirectedResponse:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def geturl(self) -> str:
            return "https://other.test/v1/models"

    with pytest.raises(ValueError, match="changed origin"):
        fetch_published_listing(
            _source(),
            env={},
            opener=lambda *_args, **_kwargs: RedirectedResponse(),
        )


def test_missing_runtime_credential_fails_before_opening_request() -> None:
    source = _source(credential_env="PROVIDER_TOKEN")

    with pytest.raises(RuntimeError, match="PROVIDER_TOKEN"):
        fetch_published_listing(
            source,
            env={},
            opener=lambda *_args, **_kwargs: pytest.fail("must not open"),
        )


def test_source_url_cannot_embed_credentials() -> None:
    source = PublishedModelSource(
        provider="openai",
        source_url="https://user:password@api.provider.test/v1/models",
    )

    with pytest.raises(ValueError, match="without credentials"):
        fetch_published_listing(
            source,
            env={},
            opener=lambda *_args, **_kwargs: pytest.fail("must not open"),
        )


def test_load_missing_registry_is_read_only_and_corruption_fails_closed(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    empty = load_model_registry(state_home)

    assert empty["models"] == {}
    assert empty["refresh_status"] == "unverified"
    assert not state_home.exists()

    path = state_home / "models" / "registry.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"schema_version":999,"models":{}}', encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        load_model_registry(state_home)


def test_load_rejects_generation_tampering(tmp_path: Path) -> None:
    state_home = tmp_path / "state"
    registry = refresh_model_registry(
        state_home,
        [_observation()],
        {"openai": _source()},
        fetcher=lambda _: {
            "aliases": {"stable": "openai/gpt-5.7"},
            "models": ["openai/gpt-5.7"],
        },
        attempted_at=NOW,
    )
    artifact_sha256 = model_registry_artifact_sha256(registry)
    path = state_home / "models" / "registries" / f"{artifact_sha256}.json"
    registry["models"]["codex"]["generation"] = "model-sha256:tampered"
    path.write_text(canonical_json(registry), encoding="utf-8")

    with pytest.raises(ValueError, match="hash mismatch"):
        load_model_registry(state_home)

# ruff: noqa: TRY004
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import canonical_json, sha256_bytes
from .state import write_json_atomic

MODEL_REGISTRY_SCHEMA_VERSION = 2
DEFAULT_MODEL_ALIAS = "<provider-default>"

CommandRunner = Callable[[Sequence[str]], Any]
ListingFetcher = Callable[["PublishedModelSource"], Any]
ListingResolver = Callable[["PublishedModelSource", Any, str], str]


@dataclass(frozen=True)
class RuntimeModelConfig:
    """The local inputs needed to observe one agent runtime."""

    runtime_id: str
    provider: str
    version_command: tuple[str, ...]
    configured_model: str | None = None


@dataclass(frozen=True)
class ModelObservation:
    """Facts obtainable from local state without contacting a provider."""

    runtime_id: str
    provider: str
    cli_version: str | None
    configured_model: str | None
    raw_alias: str
    observed_at: str
    observation_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PublishedModelSource:
    """An authoritative provider listing and its runtime-only auth contract."""

    provider: str
    source_url: str
    credential_env: str | None = None
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"


def observe_local_models(
    configs: Iterable[RuntimeModelConfig],
    *,
    command_runner: CommandRunner | None = None,
    observed_at: datetime | str | None = None,
) -> tuple[ModelObservation, ...]:
    """Observe CLI versions and configured model selectors without networking."""

    runner = _run_command if command_runner is None else command_runner
    timestamp = _timestamp(observed_at)
    observations: list[ModelObservation] = []
    runtime_ids: set[str] = set()

    for config in configs:
        _validate_runtime_config(config)
        if config.runtime_id in runtime_ids:
            raise ValueError(f"duplicate runtime_id: {config.runtime_id}")
        runtime_ids.add(config.runtime_id)

        cli_version: str | None
        error: str | None = None
        try:
            cli_version = _command_output(runner(config.version_command))
            if not cli_version:
                raise ValueError("version command returned no output")
        # An injected runner is an isolation boundary: its failure is data.
        except Exception as exc:  # noqa: BLE001
            cli_version = None
            error = _safe_error(exc)

        observations.append(
            ModelObservation(
                runtime_id=config.runtime_id,
                provider=config.provider,
                cli_version=cli_version,
                configured_model=config.configured_model,
                raw_alias=config.configured_model or DEFAULT_MODEL_ALIAS,
                observed_at=timestamp,
                observation_error=error,
            )
        )

    return tuple(sorted(observations, key=lambda item: item.runtime_id))


def refresh_model_registry(
    state_home: Path,
    observations: Iterable[ModelObservation],
    sources: Mapping[str, PublishedModelSource],
    *,
    fetcher: ListingFetcher | None = None,
    resolver: ListingResolver | None = None,
    attempted_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Resolve observed selectors and atomically persist the model registry.

    A failed provider fetch or alias resolution retains a matching last-known
    canonical identity and marks it stale. It never carries a resolution across
    a provider or raw-alias change.
    """

    timestamp = _timestamp(attempted_at)
    retrieval_date = timestamp[:10]
    prior = load_model_registry(state_home)
    prior_models = prior["models"]
    fetch_listing = fetch_published_listing if fetcher is None else fetcher
    resolve_listing = resolve_published_identity if resolver is None else resolver

    observed = _validate_observations(observations)
    listing_results: dict[str, tuple[Any | None, Exception | None]] = {}
    models: dict[str, dict[str, Any]] = {}

    for observation in observed:
        source = sources.get(observation.provider)
        previous = prior_models.get(observation.runtime_id)

        if source is None:
            models[observation.runtime_id] = _degraded_entry(
                observation,
                previous,
                timestamp,
                ValueError(
                    f"no authoritative model source for provider {observation.provider}"
                ),
            )
            continue
        _validate_source(source, observation.provider)

        if observation.provider not in listing_results:
            try:
                listing_results[observation.provider] = (fetch_listing(source), None)
            # Provider adapters may raise their own non-network exceptions.
            except Exception as exc:  # noqa: BLE001
                listing_results[observation.provider] = (None, exc)
        listing, fetch_error = listing_results[observation.provider]

        if fetch_error is not None:
            models[observation.runtime_id] = _degraded_entry(
                observation, previous, timestamp, fetch_error
            )
            continue

        try:
            provider_model_id = resolve_listing(source, listing, observation.raw_alias)
            provider_model_id = _canonical_identity(provider_model_id)
            canonical_identity = qualify_canonical_identity(
                observation.provider,
                observation.runtime_id,
                provider_model_id,
            )
        # An injected resolver is also an isolation boundary.
        except Exception as exc:  # noqa: BLE001
            models[observation.runtime_id] = _degraded_entry(
                observation, previous, timestamp, exc
            )
            continue

        model_generation = derive_model_generation(canonical_identity)
        previous_generation = _last_known_model_generation(previous)
        resolution = {
            "canonical_identity": canonical_identity,
            "provider": observation.provider,
            "provider_model_id": provider_model_id,
            "raw_alias": observation.raw_alias,
            "retrieval_date": retrieval_date,
            "retrieved_at": timestamp,
            "runtime_id": observation.runtime_id,
            "source_url": source.source_url,
        }
        models[observation.runtime_id] = {
            "generation": model_generation,
            "generation_changed": (
                previous_generation is not None
                and previous_generation != model_generation
            ),
            "last_known_generation": model_generation,
            "last_known_resolution": resolution,
            "observation": observation.to_dict(),
            "previous_generation": previous_generation,
            "resolution": resolution,
            "verification": {
                "refresh_error": None,
                "status": "current",
                "status_message": "model identity verified",
                "unverified_since": None,
            },
        }

    resolved_identities = [
        entry["resolution"]["canonical_identity"]
        for entry in models.values()
        if entry["resolution"] is not None
    ]
    generation = (
        derive_registry_generation(resolved_identities)
        if models and len(resolved_identities) == len(models)
        else None
    )
    previous_registry_generation = prior.get("last_known_generation")
    any_resolved = any(entry["resolution"] is not None for entry in models.values())
    all_current = bool(models) and all(
        entry["verification"]["status"] == "current" for entry in models.values()
    )
    current_identities = sorted(resolved_identities) if generation is not None else []
    if generation is not None:
        last_known_generation = generation
        last_known_identities = current_identities
    else:
        last_known_generation = prior.get("last_known_generation")
        last_known_identities = list(prior.get("last_known_identities", []))

    registry = {
        "generation": generation,
        "generation_changed": (
            previous_registry_generation is not None
            and generation is not None
            and generation != previous_registry_generation
        ),
        "last_successful_refresh_at": (
            timestamp if all_current else prior.get("last_successful_refresh_at")
        ),
        "last_known_generation": last_known_generation,
        "last_known_identities": last_known_identities,
        "models": models,
        "previous_generation": previous_registry_generation,
        "refresh_attempted_at": timestamp,
        "refresh_status": (
            "current" if all_current else "degraded" if any_resolved else "unverified"
        ),
        "schema_version": MODEL_REGISTRY_SCHEMA_VERSION,
    }
    artifact_sha256 = persist_model_registry_artifact(state_home, registry)
    pointer = {
        "artifact_path": (f"models/registries/{artifact_sha256}.json"),
        "artifact_sha256": artifact_sha256,
        "schema_version": MODEL_REGISTRY_SCHEMA_VERSION,
    }
    write_json_atomic(model_registry_path(state_home), pointer)
    return registry


def load_model_registry(state_home: Path) -> dict[str, Any]:
    """Load the registry without creating APU state."""

    path = model_registry_path(state_home)
    if not path.exists():
        return _empty_registry()
    try:
        pointer = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid model registry pointer at {path}: {exc}") from exc
    artifact_sha256 = _validate_registry_pointer(pointer)
    return load_model_registry_artifact(state_home, artifact_sha256)


def reconcile_model_registry_observations(
    registry: Mapping[str, Any],
    observations: Iterable[ModelObservation],
) -> dict[str, Any]:
    """Purely reconcile fresh local observations with a resolved registry.

    No command, network, or filesystem access occurs. A provider or raw-alias
    drift invalidates the current resolution but retains historical last-known
    evidence for the next successful refresh comparison.
    """

    prior = json.loads(canonical_json(dict(registry)))
    _validate_registry(prior)
    observed = _validate_observations(observations)
    prior_models = prior["models"]
    models: dict[str, dict[str, Any]] = {}

    for observation in observed:
        previous = prior_models.get(observation.runtime_id)
        local_observation_matches = _local_observation_matches(
            previous,
            observation,
        )
        resolution = (
            _matching_previous_resolution(previous, observation)
            if local_observation_matches
            else None
        )
        if (
            resolution is not None
            and isinstance(previous, Mapping)
            and previous.get("resolution") == resolution
        ):
            entry = json.loads(canonical_json(previous))
            models[observation.runtime_id] = entry
        else:
            entry = _degraded_entry(
                observation,
                previous,
                observation.observed_at,
                _LocalObservationDrift(),
            )
            if not local_observation_matches:
                entry["generation"] = None
                entry["resolution"] = None
                entry["verification"]["status"] = "unverified"
                entry["verification"]["unverified_since"] = (
                    observation.observed_at[:10]
                )
                entry["verification"]["status_message"] = (
                    "model identity unverified since "
                    f"{observation.observed_at[:10]}"
                )
            models[observation.runtime_id] = entry

    if models == prior_models:
        return prior

    resolved_identities = [
        entry["resolution"]["canonical_identity"]
        for entry in models.values()
        if entry["resolution"] is not None
    ]
    generation = (
        derive_registry_generation(resolved_identities)
        if models and len(resolved_identities) == len(models)
        else None
    )
    previous_generation = prior.get("last_known_generation")
    any_resolved = any(entry["resolution"] is not None for entry in models.values())
    all_current = bool(models) and all(
        entry["verification"]["status"] == "current" for entry in models.values()
    )
    if generation is not None:
        last_known_generation = generation
        last_known_identities = sorted(resolved_identities)
    else:
        last_known_generation = prior.get("last_known_generation")
        last_known_identities = list(prior.get("last_known_identities", []))

    reconciled = {
        **prior,
        "generation": generation,
        "generation_changed": (
            previous_generation is not None
            and generation is not None
            and previous_generation != generation
        ),
        "last_known_generation": last_known_generation,
        "last_known_identities": last_known_identities,
        "models": models,
        "previous_generation": previous_generation,
        "refresh_status": (
            "current" if all_current else "degraded" if any_resolved else "unverified"
        ),
    }
    _validate_registry(reconciled)
    return reconciled


def _local_observation_matches(
    previous: Any,
    observation: ModelObservation,
) -> bool:
    if not isinstance(previous, Mapping):
        return False
    old = previous.get("observation")
    return (
        isinstance(old, Mapping)
        and old.get("provider") == observation.provider
        and old.get("raw_alias") == observation.raw_alias
        and old.get("cli_version") == observation.cli_version
        and old.get("observation_error") == observation.observation_error
    )


def load_model_registry_artifact(
    state_home: Path,
    artifact_sha256: str,
) -> dict[str, Any]:
    """Load and verify one immutable registry artifact by its audit-stamp hash."""

    artifact_sha256 = _validate_artifact_sha256(artifact_sha256)
    path = model_registry_artifact_path(state_home, artifact_sha256)
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise ValueError(
            f"model registry artifact is unavailable: {artifact_sha256}"
        ) from exc
    if sha256_bytes(encoded) != artifact_sha256:
        raise ValueError(f"model registry artifact hash mismatch: {artifact_sha256}")
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid model registry artifact: {artifact_sha256}") from exc
    if encoded != canonical_json(value).encode("utf-8"):
        raise ValueError(f"model registry artifact is not canonical: {artifact_sha256}")
    _validate_registry(value)
    return value


def persist_model_registry_artifact(
    state_home: Path,
    registry: Mapping[str, Any],
) -> str:
    """Persist a canonical content-addressed artifact without mutable metadata."""

    value = dict(registry)
    _validate_registry(value)
    encoded = canonical_json(value).encode("utf-8")
    artifact_sha256 = sha256_bytes(encoded)
    path = model_registry_artifact_path(state_home, artifact_sha256)
    if path.exists():
        if path.read_bytes() != encoded:
            raise ValueError(f"model registry artifact collision: {artifact_sha256}")
        return artifact_sha256
    write_json_atomic(path, value)
    return artifact_sha256


def model_registry_path(state_home: Path) -> Path:
    return Path(state_home) / "models" / "registry.json"


def model_registry_artifact_path(
    state_home: Path,
    artifact_sha256: str,
) -> Path:
    artifact_sha256 = _validate_artifact_sha256(artifact_sha256)
    return Path(state_home) / "models" / "registries" / f"{artifact_sha256}.json"


def model_registry_artifact_sha256(registry: Mapping[str, Any]) -> str:
    """Return the immutable stamp for a validated registry value."""

    value = dict(registry)
    _validate_registry(value)
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def qualify_canonical_identity(
    provider: str,
    runtime_id: str,
    provider_model_id: str,
) -> str:
    """Qualify a provider model id with its runtime assignment."""

    provider = _safe_component(provider, "provider")
    runtime_id = _safe_component(runtime_id, "runtime_id")
    provider_model_id = _canonical_identity(provider_model_id)
    return (
        f"provider:{quote(provider, safe='')}/"
        f"runtime:{quote(runtime_id, safe='')}/"
        f"model:{quote(provider_model_id, safe='')}"
    )


def derive_model_generation(canonical_identity: str) -> str:
    """Derive a stable generation solely from one canonical identity."""

    identity = _qualified_canonical_identity(canonical_identity)
    digest = sha256_bytes(identity.encode("utf-8"))
    return f"model-sha256:{digest}"


def derive_registry_generation(
    canonical_identities: Iterable[str],
) -> str | None:
    """Derive the aggregate generation solely from canonical identities."""

    identities = sorted(
        {_qualified_canonical_identity(identity) for identity in canonical_identities}
    )
    if not identities:
        return None
    digest = sha256_bytes(canonical_json(identities).encode("utf-8"))
    return f"models-sha256:{digest}"


def resolve_published_identity(
    source: PublishedModelSource,
    listing: Any,
    raw_alias: str,
) -> str:
    """Resolve the small provider-neutral listing schema used by the core.

    Provider adapters may inject a resolver for a different published schema.
    The neutral schema accepts ``aliases``, ``default``, and ``models`` fields.
    Model entries may be ids or objects containing ``id`` and optional aliases.
    """

    if not isinstance(listing, Mapping):
        raise ValueError(f"{source.provider} model listing must be an object")

    aliases: dict[str, str] = {}
    supplied_aliases = listing.get("aliases", {})
    if not isinstance(supplied_aliases, Mapping):
        raise ValueError(f"{source.provider} listing aliases must be an object")
    for alias, identity in supplied_aliases.items():
        aliases[_nonempty_string(alias, "model alias")] = _canonical_identity(identity)

    model_ids: set[str] = set()
    supplied_models = listing.get("models", [])
    if not isinstance(supplied_models, list):
        raise ValueError(f"{source.provider} listing models must be an array")
    for item in supplied_models:
        if isinstance(item, str):
            model_ids.add(_canonical_identity(item))
            continue
        if not isinstance(item, Mapping):
            raise ValueError("model listing entry must be a string or object")
        identity = _canonical_identity(item.get("id"))
        model_ids.add(identity)
        item_aliases = item.get("aliases", [])
        if not isinstance(item_aliases, list):
            raise ValueError("model entry aliases must be an array")
        for alias in item_aliases:
            aliases[_nonempty_string(alias, "model alias")] = identity
        if item.get("default") is True:
            aliases[DEFAULT_MODEL_ALIAS] = identity

    default_identity = listing.get("default")
    if default_identity is not None:
        aliases[DEFAULT_MODEL_ALIAS] = _canonical_identity(default_identity)

    if raw_alias in aliases:
        resolved = aliases[raw_alias]
    elif raw_alias in model_ids:
        resolved = raw_alias
    else:
        raise ValueError(
            f"{source.provider} published listing does not resolve {raw_alias}"
        )
    if model_ids and resolved not in model_ids:
        raise ValueError(
            f"{source.provider} alias resolves outside its published model ids"
        )
    return resolved


def fetch_published_listing(
    source: PublishedModelSource,
    *,
    env: Mapping[str, str] | None = None,
    opener: Callable[..., Any] | None = None,
    additional_headers: Mapping[str, str] | None = None,
) -> Any:
    """Fetch a published listing without redirects or persisted credentials.

    The default transport rejects every redirect before a credential-bearing
    follow-up request can be constructed. An injected opener is a trusted test
    or provider-adapter boundary and must implement the same no-redirect rule.
    The final URL is checked as defense in depth.
    """

    _validate_source(source, source.provider)
    headers = {"Accept": "application/json"}
    for name, value in (additional_headers or {}).items():
        header_name = _nonempty_string(name, "additional header name")
        header_value = _nonempty_string(value, f"additional header {header_name}")
        if "\r" in header_name or "\n" in header_name:
            raise ValueError("additional header names cannot contain newlines")
        if "\r" in header_value or "\n" in header_value:
            raise ValueError("additional header values cannot contain newlines")
        if header_name.casefold() in {
            "authorization",
            source.auth_header.casefold(),
        }:
            raise ValueError("additional headers cannot override authentication")
        headers[header_name] = header_value
    environment = os.environ if env is None else env
    if source.credential_env is not None:
        credential = environment.get(source.credential_env)
        if not credential:
            raise RuntimeError(
                f"required credential environment variable is unavailable: "
                f"{source.credential_env}"
            )
        prefix = f"{source.auth_scheme} " if source.auth_scheme else ""
        headers[source.auth_header] = f"{prefix}{credential}"

    request = Request(source.source_url, headers=headers)
    if opener is None:
        response_context = build_opener(_RejectRedirectHandler()).open(
            request,
            timeout=30,
        )
    else:
        response_context = opener(request, timeout=30)
    with response_context as response:
        final_url = response.geturl()
        _validate_same_origin(source.source_url, final_url)
        return json.load(response)


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Prevent urllib from constructing any redirected authenticated request."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class _LocalObservationDrift(Exception):
    """Internal marker persisted only as its non-sensitive type name."""


def _degraded_entry(
    observation: ModelObservation,
    previous: Any,
    attempted_at: str,
    error: Exception,
) -> dict[str, Any]:
    previous_resolution = _matching_previous_resolution(previous, observation)
    previous_generation = (
        derive_model_generation(previous_resolution["canonical_identity"])
        if previous_resolution is not None
        else None
    )
    last_known_resolution = _last_known_model_resolution(previous)
    last_known_generation = _last_known_model_generation(previous)

    unverified_since = (
        previous_resolution["retrieval_date"]
        if previous_resolution is not None
        else attempted_at[:10]
    )
    status = "stale" if previous_resolution is not None else "unverified"
    return {
        "generation": previous_generation,
        "generation_changed": False,
        "last_known_generation": last_known_generation,
        "last_known_resolution": last_known_resolution,
        "observation": observation.to_dict(),
        "previous_generation": last_known_generation,
        "resolution": previous_resolution,
        "verification": {
            "refresh_error": _safe_error(error),
            "status": status,
            "status_message": (f"model identity unverified since {unverified_since}"),
            "unverified_since": unverified_since,
        },
    }


def _matching_previous_resolution(
    previous: Any,
    observation: ModelObservation,
) -> dict[str, Any] | None:
    if not isinstance(previous, Mapping):
        return None
    old_observation = previous.get("observation")
    if (
        not isinstance(old_observation, Mapping)
        or old_observation.get("provider") != observation.provider
    ):
        return None
    for field in ("resolution", "last_known_resolution"):
        resolution = previous.get(field)
        if (
            isinstance(resolution, Mapping)
            and resolution.get("raw_alias") == observation.raw_alias
            and isinstance(resolution.get("canonical_identity"), str)
        ):
            return dict(resolution)
    return None


def _last_known_model_resolution(previous: Any) -> dict[str, Any] | None:
    if not isinstance(previous, Mapping):
        return None
    resolution = previous.get("last_known_resolution")
    if isinstance(resolution, Mapping):
        return dict(resolution)
    resolution = previous.get("resolution")
    return dict(resolution) if isinstance(resolution, Mapping) else None


def _last_known_model_generation(previous: Any) -> str | None:
    if not isinstance(previous, Mapping):
        return None
    generation = previous.get("last_known_generation")
    if generation is None:
        generation = previous.get("generation")
    return generation if isinstance(generation, str) else None


def _validate_runtime_config(config: RuntimeModelConfig) -> None:
    _safe_component(config.runtime_id, "runtime_id")
    _nonempty_string(config.provider, "provider")
    if not config.version_command or not all(
        isinstance(part, str) and part for part in config.version_command
    ):
        raise ValueError("version_command must contain non-empty strings")
    if config.configured_model is not None:
        _nonempty_string(config.configured_model, "configured_model")


def _validate_observations(
    observations: Iterable[ModelObservation],
) -> tuple[ModelObservation, ...]:
    by_runtime: dict[str, ModelObservation] = {}
    for observation in observations:
        if not isinstance(observation, ModelObservation):
            raise ValueError("observations must contain ModelObservation values")
        _safe_component(observation.runtime_id, "runtime_id")
        _nonempty_string(observation.provider, "provider")
        _nonempty_string(observation.raw_alias, "raw_alias")
        _timestamp(observation.observed_at)
        if observation.configured_model is not None:
            _nonempty_string(observation.configured_model, "configured_model")
        expected_alias = observation.configured_model or DEFAULT_MODEL_ALIAS
        if observation.raw_alias != expected_alias:
            raise ValueError(
                f"observation {observation.runtime_id} raw_alias does not match "
                "its configured model selector"
            )
        if observation.cli_version is not None:
            _nonempty_string(observation.cli_version, "cli_version")
        if observation.observation_error is not None:
            _nonempty_string(observation.observation_error, "observation_error")
        if observation.runtime_id in by_runtime:
            raise ValueError(f"duplicate runtime_id: {observation.runtime_id}")
        by_runtime[observation.runtime_id] = observation
    return tuple(by_runtime[key] for key in sorted(by_runtime))


def _validate_source(source: PublishedModelSource, provider: str) -> None:
    if not isinstance(source, PublishedModelSource):
        raise ValueError(f"model source for {provider} has an invalid type")
    if source.provider != provider:
        raise ValueError(f"model source provider mismatch for {provider}")
    _validate_public_source_url(source.source_url)
    if source.credential_env is not None:
        _nonempty_string(source.credential_env, "credential_env")
    _nonempty_string(source.auth_header, "auth_header")
    if "\r" in source.auth_scheme or "\n" in source.auth_scheme:
        raise ValueError("auth_scheme cannot contain newlines")


def _validate_registry(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("model registry must be an object")
    if value.get("schema_version") != MODEL_REGISTRY_SCHEMA_VERSION:
        raise ValueError("unsupported model registry schema_version")
    for field in ("generation", "previous_generation"):
        if value.get(field) is not None:
            _nonempty_string(value[field], field)
    if value.get("last_known_generation") is not None:
        _nonempty_string(
            value["last_known_generation"],
            "last_known_generation",
        )
    last_known_identities = value.get("last_known_identities")
    if not isinstance(last_known_identities, list):
        raise ValueError("model registry last_known_identities must be an array")
    validated_last_known_identities = [
        _qualified_canonical_identity(identity) for identity in last_known_identities
    ]
    if validated_last_known_identities != sorted(set(validated_last_known_identities)):
        raise ValueError(
            "model registry last_known_identities must be sorted and unique"
        )
    expected_last_known_generation = derive_registry_generation(
        validated_last_known_identities
    )
    if value.get("last_known_generation") != expected_last_known_generation:
        raise ValueError(
            "model registry last_known_generation is not derived from "
            "last_known_identities"
        )
    if not isinstance(value.get("generation_changed"), bool):
        raise ValueError("model registry generation_changed must be boolean")
    for field in (
        "last_successful_refresh_at",
        "refresh_attempted_at",
    ):
        if value.get(field) is not None:
            _timestamp(value[field])
    if value.get("refresh_status") not in {"current", "degraded", "unverified"}:
        raise ValueError("model registry refresh_status is invalid")
    models = value.get("models")
    if not isinstance(models, dict):
        raise ValueError("model registry models must be an object")
    identities: list[str] = []
    for runtime_id, entry in models.items():
        _safe_component(runtime_id, "runtime_id")
        if not isinstance(entry, dict):
            raise ValueError(f"model registry entry {runtime_id} must be an object")
        observation = entry.get("observation")
        verification = entry.get("verification")
        if not isinstance(observation, dict) or not isinstance(verification, dict):
            raise ValueError(f"model registry entry {runtime_id} is incomplete")
        if observation.get("runtime_id") != runtime_id:
            raise ValueError(f"model registry entry {runtime_id} id mismatch")
        provider = _safe_component(observation.get("provider"), "provider")
        configured = observation.get("configured_model")
        if configured is not None:
            _nonempty_string(configured, "configured_model")
        raw_alias = _nonempty_string(observation.get("raw_alias"), "raw_alias")
        if raw_alias != (configured or DEFAULT_MODEL_ALIAS):
            raise ValueError(
                f"model registry entry {runtime_id} raw_alias is inconsistent"
            )
        _timestamp(observation.get("observed_at"))
        if verification.get("status") not in {"current", "stale", "unverified"}:
            raise ValueError(f"model registry entry {runtime_id} status invalid")
        resolution = entry.get("resolution")
        generation = entry.get("generation")
        last_known_resolution = entry.get("last_known_resolution")
        last_known_generation = entry.get("last_known_generation")
        if (last_known_resolution is None) != (last_known_generation is None):
            raise ValueError(
                f"model registry entry {runtime_id} last-known fields must "
                "both be set or null"
            )
        if last_known_resolution is not None:
            last_known_identity = _validate_stored_resolution(
                last_known_resolution,
                runtime_id=runtime_id,
            )
            if last_known_generation != derive_model_generation(last_known_identity):
                raise ValueError(
                    f"model registry entry {runtime_id} last-known generation "
                    "is invalid"
                )
        if resolution is None:
            if generation is not None:
                raise ValueError(
                    f"model registry entry {runtime_id} has generation without "
                    "a resolution"
                )
            if verification.get("status") != "unverified":
                raise ValueError(
                    f"model registry entry {runtime_id} unresolved status invalid"
                )
            continue
        identity = _validate_stored_resolution(
            resolution,
            runtime_id=runtime_id,
            provider=provider,
            raw_alias=raw_alias,
        )
        identities.append(identity)
        if generation != derive_model_generation(identity):
            raise ValueError(
                f"model registry entry {runtime_id} generation is not derived "
                "from its canonical identity"
            )
        if verification.get("status") == "unverified":
            raise ValueError(
                f"model registry entry {runtime_id} resolved status is invalid"
            )
        if verification.get("status") == "current" and (
            resolution != last_known_resolution or generation != last_known_generation
        ):
            raise ValueError(
                f"model registry entry {runtime_id} current resolution must "
                "be its last-known resolution"
            )

    expected_generation = (
        derive_registry_generation(identities)
        if models and len(identities) == len(models)
        else None
    )
    if value.get("generation") != expected_generation:
        raise ValueError(
            "model registry generation is not derived from canonical identities"
        )


def _validate_stored_resolution(
    value: Any,
    *,
    runtime_id: str,
    provider: str | None = None,
    raw_alias: str | None = None,
) -> str:
    required = {
        "canonical_identity",
        "provider",
        "provider_model_id",
        "raw_alias",
        "retrieval_date",
        "retrieved_at",
        "runtime_id",
        "source_url",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError(
            f"model registry resolution fields must be exactly: "
            f"{', '.join(sorted(required))}"
        )
    stored_provider = _safe_component(value.get("provider"), "provider")
    stored_runtime = _safe_component(value.get("runtime_id"), "runtime_id")
    if stored_runtime != runtime_id:
        raise ValueError("model registry resolution runtime_id mismatch")
    if provider is not None and stored_provider != provider:
        raise ValueError("model registry resolution provider mismatch")
    stored_alias = _nonempty_string(value.get("raw_alias"), "raw_alias")
    if raw_alias is not None and stored_alias != raw_alias:
        raise ValueError("model registry resolution alias mismatch")
    provider_model_id = _canonical_identity(value.get("provider_model_id"))
    expected_identity = qualify_canonical_identity(
        stored_provider,
        stored_runtime,
        provider_model_id,
    )
    identity = _qualified_canonical_identity(value.get("canonical_identity"))
    if identity != expected_identity:
        raise ValueError("model registry canonical identity context mismatch")
    _validate_public_source_url(value.get("source_url"))
    _timestamp(value.get("retrieved_at"))
    try:
        retrieval_date = date.fromisoformat(
            _nonempty_string(value.get("retrieval_date"), "retrieval_date")
        )
    except ValueError as exc:
        raise ValueError("model registry resolution retrieval_date is invalid") from exc
    if retrieval_date.isoformat() != value["retrieval_date"]:
        raise ValueError("model registry resolution retrieval_date is invalid")
    return identity


def _empty_registry() -> dict[str, Any]:
    return {
        "generation": None,
        "generation_changed": False,
        "last_successful_refresh_at": None,
        "last_known_generation": None,
        "last_known_identities": [],
        "models": {},
        "previous_generation": None,
        "refresh_attempted_at": None,
        "refresh_status": "unverified",
        "schema_version": MODEL_REGISTRY_SCHEMA_VERSION,
    }


def _validate_registry_pointer(value: Any) -> str:
    if not isinstance(value, dict):
        raise ValueError("model registry pointer must be an object")
    if value.get("schema_version") != MODEL_REGISTRY_SCHEMA_VERSION:
        raise ValueError("unsupported model registry pointer schema_version")
    artifact_sha256 = _validate_artifact_sha256(value.get("artifact_sha256"))
    expected_path = f"models/registries/{artifact_sha256}.json"
    if value.get("artifact_path") != expected_path:
        raise ValueError("model registry pointer artifact_path is not canonical")
    return artifact_sha256


def _validate_artifact_sha256(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("artifact_sha256 must be 64 lowercase hexadecimal digits")
    return value


def _validate_public_source_url(value: Any) -> str:
    url = _nonempty_string(value, "source_url")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(
            "source_url must be an HTTPS endpoint without credentials or fragments"
        )
    return url


def _validate_same_origin(source_url: str, final_url: Any) -> None:
    final = _validate_public_source_url(final_url)
    source = urlsplit(source_url)
    destination = urlsplit(final)
    source_port = source.port or 443
    destination_port = destination.port or 443
    if source.hostname != destination.hostname or source_port != destination_port:
        raise ValueError("provider model listing response changed origin")
    if final != source_url:
        raise ValueError("provider model listing redirects are not allowed")


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        parsed = datetime.now(UTC)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"invalid timestamp: {value}") from exc
    else:
        raise ValueError("timestamp must be an ISO string or datetime")
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _command_output(result: Any) -> str:
    if isinstance(result, str):
        return result.strip()
    return_code = getattr(result, "returncode", 0)
    if return_code:
        stderr = getattr(result, "stderr", "")
        raise RuntimeError(str(stderr).strip() or f"command exited {return_code}")
    output = getattr(result, "stdout", None)
    if not isinstance(output, str):
        raise ValueError("command runner must return text or an object with stdout")
    return output.strip()


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _canonical_identity(value: Any) -> str:
    return _nonempty_string(value, "canonical model identity")


def _qualified_canonical_identity(value: Any) -> str:
    identity = _canonical_identity(value)
    parts = identity.split("/")
    if (
        len(parts) != 3
        or not parts[0].startswith("provider:")
        or not parts[1].startswith("runtime:")
        or not parts[2].startswith("model:")
    ):
        raise ValueError(
            "canonical model identity must include provider and runtime context"
        )
    provider = unquote(parts[0].removeprefix("provider:"))
    runtime_id = unquote(parts[1].removeprefix("runtime:"))
    provider_model_id = unquote(parts[2].removeprefix("model:"))
    if qualify_canonical_identity(provider, runtime_id, provider_model_id) != identity:
        raise ValueError("canonical model identity is not canonically qualified")
    return identity


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _safe_component(value: Any, name: str) -> str:
    text = _nonempty_string(value, name)
    if text in {".", ".."} or "/" in text or "\\" in text or "\x00" in text:
        raise ValueError(f"{name} must be one safe path component")
    return text


def _safe_error(error: Exception) -> str:
    # Adapter and command errors are untrusted and may echo request headers.
    return type(error).__name__

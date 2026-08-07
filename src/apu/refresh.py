from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .guidance import FetchResponse
from .model_registry import (
    PublishedModelSource,
    RuntimeModelConfig,
    fetch_published_listing,
)

MAX_GUIDANCE_BYTES = 5 * 1024 * 1024
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models?limit=1000"


def runtime_model_configs(
    home: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> tuple[RuntimeModelConfig, ...]:
    """Observe documented local selectors without contacting a provider."""

    selected_home = Path(home).expanduser().resolve(strict=False)
    env = os.environ if environment is None else environment

    codex_config = _load_toml(selected_home / ".codex" / "config.toml")
    codex_provider = _optional_text(codex_config.get("model_provider")) or "openai"
    codex_model = _optional_text(codex_config.get("model"))

    claude_settings = _load_json(selected_home / ".claude" / "settings.json")
    claude_env = claude_settings.get("env", {})
    if not isinstance(claude_env, Mapping):
        claude_env = {}
    claude_model = (
        _optional_text(env.get("ANTHROPIC_MODEL"))
        or _optional_text(claude_env.get("ANTHROPIC_MODEL"))
        or _optional_text(claude_settings.get("model"))
    )

    return (
        RuntimeModelConfig(
            runtime_id="claude-cli",
            provider="anthropic",
            version_command=("claude", "--version"),
            configured_model=claude_model,
        ),
        RuntimeModelConfig(
            runtime_id="codex-cli",
            provider=codex_provider,
            version_command=("codex", "--version"),
            configured_model=codex_model,
        ),
    )


def published_model_sources() -> dict[str, PublishedModelSource]:
    """Return fixed authoritative endpoints; profile URLs never receive auth."""

    return {
        "anthropic": PublishedModelSource(
            provider="anthropic",
            source_url=ANTHROPIC_MODELS_URL,
            credential_env="ANTHROPIC_API_KEY",
            auth_header="x-api-key",
            auth_scheme="",
        ),
        "openai": PublishedModelSource(
            provider="openai",
            source_url=OPENAI_MODELS_URL,
            credential_env="OPENAI_API_KEY",
        ),
    }


def fetch_provider_models(source: PublishedModelSource) -> dict[str, Any]:
    """Fetch and normalize official provider list responses without guessing."""

    if source.provider == "openai":
        raw = fetch_published_listing(source)
        return _normalize_models(raw, provider="openai")
    if source.provider == "anthropic":
        raw = _fetch_anthropic_listing(source)
        return _normalize_models(raw, provider="anthropic")
    raise ValueError(f"unsupported authoritative model provider: {source.provider}")


def fetch_guidance_source(source_url: str) -> FetchResponse:
    """Fetch one bounded HTTPS guidance object with redirect revalidation."""

    _validate_guidance_url(source_url)
    opener = build_opener(_SafeRedirectHandler())
    request = Request(
        source_url,
        headers={
            "Accept": "text/plain, text/markdown, application/json, text/html",
            "User-Agent": "apu-guidance-refresh/1",
        },
    )
    with opener.open(request, timeout=30) as response:
        final_url = response.geturl()
        _validate_guidance_url(final_url)
        content = response.read(MAX_GUIDANCE_BYTES + 1)
        if len(content) > MAX_GUIDANCE_BYTES:
            raise ValueError("guidance source exceeds the maximum response size")
        media_type = response.headers.get_content_type()
    return FetchResponse(content=content, media_type=media_type)


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        _validate_guidance_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_anthropic_listing(source: PublishedModelSource) -> Any:
    return fetch_published_listing(
        source,
        additional_headers={"anthropic-version": "2023-06-01"},
    )


def _normalize_models(value: Any, *, provider: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{provider} model listing must be an object")
    data = value.get("data")
    if not isinstance(data, list):
        raise TypeError(f"{provider} model listing data must be an array")
    models: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, Mapping):
            raise TypeError(f"{provider} model entries must be objects")
        identifier = _optional_text(item.get("id"))
        if identifier is None:
            raise ValueError(f"{provider} model entry id is required")
        models.append({"id": identifier})
    return {"models": sorted(models, key=lambda item: item["id"])}


def _validate_guidance_url(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("guidance source URL must be non-empty text")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(
            "guidance sources must be HTTPS URLs without credentials or fragments"
        )


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None

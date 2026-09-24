from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

import dotenv
import instructor
from instructor import Instructor, Mode
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError, model_validator

from src import REPO_ROOT

from .cli_transport import DEFAULT_TIMEOUT, CliClient, Transport, build_cli_client

PROJECT_ROOT = REPO_ROOT

# Load the environment variables from the .env file if it exists
dotenv.load_dotenv(PROJECT_ROOT / ".env")


class ProviderInfo(BaseModel):
    """Configuration information for a provider."""

    transport: Transport = Field(
        default=Transport.OPENAI,
        description=(
            "How to reach the model. 'openai' calls base_url directly with an "
            "API key; 'claude_cli' and 'codex_cli' delegate to a locally "
            "signed-in Claude Code / Codex install, which supplies the "
            "credentials itself so no API key is needed."
        ),
    )
    base_url: str = Field(
        default="",
        description="Base URL for the provider's API. Required by the 'openai' transport, unused by the CLI transports.",
    )
    api_key: str = Field(
        default="",
        description="API key for authenticating with the provider. Can include environment variable placeholders like ${ENV_VAR}. Required by the 'openai' transport; the CLI transports authenticate through the CLI's own login instead.",
    )
    model: str = Field(
        ...,
        description="Model name or identifier to use with the provider. May be blank for a CLI transport, which then uses whatever model that CLI is configured to use.",
    )
    mode: Mode = Field(
        ..., description="Mode of instructor parsing to use with the model."
    )
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="LLM sampling temperature. None means provider default. 0.0 recommended for grading to minimize variance. Ignored by the CLI transports, which expose no sampling controls.",
    )
    cli_path: str | None = Field(
        default=None,
        description="Path to the CLI executable for a CLI transport. None looks 'claude'/'codex' up on PATH.",
    )
    timeout: int = Field(
        default=DEFAULT_TIMEOUT,
        gt=0,
        description="Seconds to wait on one CLI invocation. Unused by the 'openai' transport.",
    )

    @model_validator(mode="after")
    def _check_transport_fields(self) -> ProviderInfo:
        """An API-key provider is unusable without an endpoint to call.

        The CLI transports have the opposite requirement — they must *not* be
        given a key, since the whole point is that the CLI holds the
        credential — so the check is per-transport rather than on the field.
        """
        if self.transport is Transport.OPENAI:
            missing = [
                name
                for name in ("base_url", "api_key")
                if not getattr(self, name).strip()
            ]
            if missing:
                msg = f"{', '.join(missing)} must be set for the 'openai' transport"
                raise ValueError(msg)
        return self


def resolve_env_placeholders(value: str) -> str:
    """Resolve every ``${VAR}`` placeholder against ``os.environ``.

    A var absent from the environment resolves to an empty string (the
    pre-existing behavior of both the old shared getitem and the TUI's
    local resolver — kept identical for the pinned test contract).
    """
    return re.sub(r"\$\{(\w+?)\}", lambda m: os.environ.get(m.group(1), ""), value)


def build_provider_client(  # ruff: ignore[too-many-arguments]
    base_url: str,
    api_key: str,
    mode: Mode,
    temperature: float | None = None,
    *,
    transport: Transport = Transport.OPENAI,
    cli_path: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Instructor | CliClient:
    """The single construction site for every client the pipeline uses
    (grading, rubric generation, TUI provider probe).

    Returns an instructor-wrapped OpenAI client for the ``openai`` transport
    and a :class:`CliClient` otherwise. Both expose the same
    ``chat.completions.create`` surface, so callers do not branch on which
    one they got."""
    if transport.is_cli:
        return build_cli_client(transport, cli_path=cli_path, timeout=timeout)
    kwargs: dict[str, Any] = {"base_url": base_url, "api_key": api_key}
    if temperature is not None:
        kwargs["temperature"] = temperature
    raw_client = OpenAI(**kwargs)
    return instructor.from_openai(raw_client, mode=mode)


class ProviderList(BaseModel):
    providers: dict[str, ProviderInfo] = Field(default_factory=dict)

    model_config = {
        "title": "Provider List",
    }

    def __getitem__(self, provider_name: str) -> ProviderInfo:
        provider = self.providers.get(provider_name, None)
        if provider is None:
            msg = (
                f"Provider '{provider_name}' not found in the provider list.\n"
                f"Available providers: {sorted(self.providers.keys())}"
            )
            raise KeyError(msg)

        # Replace all API key placeholders from the environment.
        provider.api_key = resolve_env_placeholders(provider.api_key)
        return provider


def get_providers(providers_dir: Path | None = None) -> ProviderList:
    """Load every ``*.toml`` in ``data/providers/``; one provider per file.

    The provider name is the filename stem; keys are flat top-level fields
    (base_url, api_key, model, mode, temperature).
    """
    providers_dir = providers_dir or (PROJECT_ROOT / "data" / "providers")
    provider_files = sorted(providers_dir.glob("*.toml"))
    if not provider_files:
        msg = f"no provider files at {providers_dir}"
        raise FileNotFoundError(msg)

    providers: dict[str, ProviderInfo] = {}
    for file in provider_files:
        name = file.stem
        try:
            provider = ProviderInfo.model_validate(
                tomllib.loads(file.read_text(encoding="utf-8"))
            )
        except (tomllib.TOMLDecodeError, ValidationError) as exc:
            msg = f"invalid provider file {file}: {exc}"
            raise ValueError(msg) from exc
        providers[name] = provider

    return ProviderList(providers=providers)

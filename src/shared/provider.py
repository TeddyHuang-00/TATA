from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

import dotenv
from instructor import Mode
from pydantic import BaseModel, Field, ValidationError

from src import REPO_ROOT

PROJECT_ROOT = REPO_ROOT

# Load the environment variables from the .env file if it exists
dotenv.load_dotenv(PROJECT_ROOT / ".env")


class ProviderInfo(BaseModel):
    """Configuration information for a provider."""

    base_url: str = Field(..., description="Base URL for the provider's API.")
    api_key: str = Field(
        ...,
        description="API key for authenticating with the provider. Can include environment variable placeholders like ${ENV_VAR}.",
    )
    model: str = Field(
        ..., description="Model name or identifier to use with the provider."
    )
    mode: Mode = Field(
        ..., description="Mode of instructor parsing to use with the model."
    )
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="LLM sampling temperature. None means provider default. 0.0 recommended for grading to minimize variance.",
    )


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

        # Replace the API key placeholder with the actual value from the environment variable
        key_pattern = r"\$\{(\w+?)\}"
        if match := re.search(key_pattern, provider.api_key):
            env_var = match.group(1)
            provider.api_key = provider.api_key.replace(
                f"${{{env_var}}}", os.getenv(env_var, "")
            )
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

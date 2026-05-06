from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

import dotenv
from instructor import Mode
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]

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


def get_providers() -> ProviderList:

    config_file = PROJECT_ROOT / "config" / "provider.toml"
    if not config_file.exists():
        msg = f"Provider configuration file not found: {config_file}"
        raise FileNotFoundError(msg)

    config = tomllib.loads(config_file.read_text())

    return ProviderList.model_validate(config)


if __name__ == "__main__":
    import json

    schema_file = PROJECT_ROOT / "config" / "provider.schema.json"
    with schema_file.open("w") as f:
        json.dump(ProviderList.model_json_schema(), f, indent=4)

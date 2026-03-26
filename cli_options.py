from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def validate_existing_file(path: Path, *, option_name: str = "--config") -> Path:
    """Validate CLI file path option exists and points to a file."""
    if not path.exists():
        msg = f"{option_name} not found: {path}"
        raise ValueError(msg)
    if not path.is_file():
        msg = f"{option_name} must be a file: {path}"
        raise ValueError(msg)
    return path


class CliOptions(BaseSettings):
    """Base class for CLI option models parsed by pydantic-settings."""

    model_config = SettingsConfigDict(
        cli_parse_args=True,
        cli_implicit_flags=True,
        cli_kebab_case=True,
        cli_enforce_required=True,
        extra="forbid",
    )


class ConfigFileCliOptions(CliOptions):
    config: Path = Field(description="Path to assignment config TOML.")

    @field_validator("config")
    @classmethod
    def _validate_config(cls, value: Path) -> Path:
        return validate_existing_file(value)


def parse_cli_args[TModel: CliOptions](
    model_cls: type[TModel],
    *,
    argv: Sequence[str] | None = None,
) -> TModel:
    """Parse CLI args via pydantic-settings automatic CLI support."""
    settings_kwargs: dict[str, Any] = {}
    if argv is not None:
        settings_kwargs["_cli_parse_args"] = list(argv)
    return model_cls(**settings_kwargs)

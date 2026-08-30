from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    CliPositionalArg,
    CliSubCommand,
    SettingsConfigDict,
    SettingsError,
)


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
        env_prefix="TATA_",
    )


class ConfigFileCliOptions(CliOptions):
    """Legacy single-command options for module entry points (src/*.py)."""

    config: Path = Field(
        validation_alias=AliasChoices("config", "c"),
        description="Path to assignment config TOML.",
    )

    @field_validator("config")
    @classmethod
    def _validate_config(cls, value: Path) -> Path:
        return validate_existing_file(value)


class ConfigFileOptions(BaseModel):
    """Subcommand options with a required --config/-c path (CLI-only, no env)."""

    config: Path = Field(
        validation_alias=AliasChoices("config", "c"),
        description="Path to assignment config TOML.",
    )

    @field_validator("config")
    @classmethod
    def _validate_config(cls, value: Path) -> Path:
        return validate_existing_file(value)


class PreprocessCliOptions(ConfigFileOptions):
    """Preprocess raw submissions into markdown."""


class PlagiarismCliOptions(ConfigFileOptions):
    """Detect plagiarism across submissions.

    ``--config`` accepts either an assignment config (that assignment only,
    code + text as applicable) or the course root ``data/config.toml``
    (every assignment under it).
    """

    aggregate: bool = Field(
        default=False,
        description="After per-assignment runs, produce the cross-assignment "
        "z-score aggregate report over the assignments root.",
    )
    output: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("output", "o"),
        description="Write the aggregate report to this file instead of stdout.",
    )


class GradeCliOptions(ConfigFileOptions):
    """Grade submissions with the configured LLM provider."""

    force: bool = Field(
        default=False,
        description="Ignore checkpoint and regrade all submissions.",
    )


class ScoreCliOptions(ConfigFileOptions):
    """Compute scores from grading results."""


class AnalyzeCliOptions(ConfigFileOptions):
    """Run meta analysis on scores."""


class SchemaCliOptions(BaseModel):
    """Generate JSON schemas from the config models."""


class FetchCliOptions(BaseModel):
    """Fetch submissions from Canvas; --retry re-fetches recorded configs."""

    course: CliPositionalArg[int | None] = None
    assignment: CliPositionalArg[int | None] = None
    config: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("config", "c"),
        description="Course config.toml holding the [fetch] course_id and "
        "assignment list; defaults to ./config.toml when run from an "
        "assignment dir.",
    )
    retry: bool = Field(
        default=False,
        description="Re-fetch all assignments recorded in configs "
        "(filter with [+/-] course/assignment).",
    )

    @field_validator("config")
    @classmethod
    def _validate_config(cls, value: Path | None) -> Path | None:
        if value is None:
            return value
        return validate_existing_file(value)

    @model_validator(mode="after")
    def _validate_fetch_args(self) -> FetchCliOptions:
        # The together-check only applies to the non-retry path; in retry mode
        # a single positional acts as a course filter (original behavior).
        if not self.retry and (self.course is None) != (self.assignment is None):
            msg = "--course and --assignment must be given together."
            raise ValueError(msg)
        return self


class ScoreReviewCliOptions(BaseModel):
    """Score review viewer options: TUI by default, --web serves it over HTTP."""

    score_dir: CliPositionalArg[Path] = Field(
        description="Directory containing per-student JSON grading outputs.",
    )
    web: bool = Field(
        default=False,
        description="Serve the viewer over HTTP (http://localhost:8000) "
        "via textual-serve instead of running the TUI.",
    )

    @model_validator(mode="after")
    def _validate(self) -> ScoreReviewCliOptions:
        d = self.score_dir.resolve()
        if not d.is_dir():
            msg = f"score dir not found or not a directory: {d}"
            raise ValueError(msg)
        self.score_dir = d
        return self


class ConfigSetCliOptions(BaseModel):
    """Set one config value (dotted ``section.key``), preserving comments.

    Value is coerced TOML-style: true/false -> bool, int, float, else string.
    The result is validated with the same pydantic models the settings screen
    uses before anything is written (no write on failure). The config file is
    created if missing; full model validation applies to existing files only
    (a new file has no prior state — section sanity still applies).
    """

    config: Path = Field(
        validation_alias=AliasChoices("config", "c"),
        description="Path to config TOML to edit (created if missing).",
    )
    key: CliPositionalArg[str]
    value: CliPositionalArg[str]


class ConfigCliOptions(BaseModel):
    """Config root: ``config set`` edits one dotted ``section.key`` value."""

    set: CliSubCommand[ConfigSetCliOptions]


class TataCli(CliOptions):
    """TATA CLI root: one subcommand per pipeline operation."""

    model_config = SettingsConfigDict(cli_exit_on_error=False)

    preprocess: CliSubCommand[PreprocessCliOptions]
    plagiarism: CliSubCommand[PlagiarismCliOptions]
    grade: CliSubCommand[GradeCliOptions]
    score: CliSubCommand[ScoreCliOptions]
    analyze: CliSubCommand[AnalyzeCliOptions]
    # "schema_gen" avoids shadowing BaseModel.schema; alias keeps the CLI name.
    schema_gen: CliSubCommand[SchemaCliOptions] = Field(alias="schema")
    fetch: CliSubCommand[FetchCliOptions]
    view: CliSubCommand[ScoreReviewCliOptions]
    config: CliSubCommand[ConfigCliOptions]


def parse_cli_args[TModel: CliOptions](
    model_cls: type[TModel],
    *,
    argv: Sequence[str] | None = None,
) -> TModel:
    """Parse CLI args via pydantic-settings automatic CLI support.

    Model validation errors (Literal/int coercion, model_validators) are
    reported as one clean ``error: ...`` line on stderr and exit 2, matching
    the behavior pydantic-settings already has for CLI parse errors.
    """
    settings_kwargs: dict[str, Any] = {}
    if argv is not None:
        settings_kwargs["_cli_parse_args"] = list(argv)
    try:
        return model_cls(**settings_kwargs)
    except ValidationError as exc:
        err = exc.errors()[0]
        msg = err["msg"]
        if msg.startswith("Value error, "):
            msg = msg.removeprefix("Value error, ")
        elif msg == "Field required":
            msg = f"{'.'.join(map(str, err['loc']))} is required."
        print(f"error: {msg}", file=sys.stderr)
        raise SystemExit(2) from exc
    except SettingsError as exc:
        msg = str(exc).removeprefix("error parsing CLI: ")
        print(f"error: {msg}", file=sys.stderr)
        raise SystemExit(2) from exc

from __future__ import annotations

from dataclasses import dataclass
import json
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

InputFormat = Literal["ipynb", "html", "markdown"]


class AssignmentSection(BaseModel):
    name: str = Field(default="assignment")
    raw_dir: str | None = Field(default=None)
    processed_dir: str | None = Field(default=None)
    graded_dir: str | None = Field(default=None)
    logs_dir: str | None = Field(default=None)
    reference_file: str | None = Field(default=None)

    def resolve_raw_dir(self, base_dir: Path) -> Path:
        return (base_dir / (self.raw_dir or "raw")).resolve()

    def resolve_processed_dir(self, base_dir: Path) -> Path:
        return (base_dir / (self.processed_dir or "processed")).resolve()

    def resolve_graded_dir(self, base_dir: Path) -> Path:
        return (base_dir / (self.graded_dir or "graded")).resolve()

    def resolve_logs_dir(self, base_dir: Path) -> Path:
        return (base_dir / (self.logs_dir or "logs")).resolve()

    def resolve_reference_file(self, base_dir: Path) -> Path:
        return (base_dir / (self.reference_file or "reference.md")).resolve()


class ProcessingSection(BaseModel):
    input_format: InputFormat | list[InputFormat] | None = Field(default=None)
    remove_base64_images: bool = Field(default=True)
    clean_filenames: bool = Field(default=True)
    strip_canvas_suffix: bool = Field(default=True)
    strip_html_callouts: bool = Field(default=True)
    strip_html_div_tags: bool = Field(default=True)
    strip_html_escaped_backslashes: bool = Field(default=True)
    remove_nbconvert_assets: bool = Field(default=True)
    nbconvert_template: str | None = Field(default=None)
    nbconvert_template_dir: str | None = Field(default=None)


class GradingSection(BaseModel):
    rubric: str = Field(..., min_length=1)
    system_prompt: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    max_parallel_tasks: int = Field(default=10, ge=1, le=10)


class AssignmentFileConfig(BaseModel):
    assignment: AssignmentSection = Field(default_factory=AssignmentSection)
    processing: ProcessingSection = Field(default_factory=ProcessingSection)
    grading: GradingSection


@dataclass(frozen=True)
class AssignmentPaths:
    raw_dir: Path
    processed_dir: Path
    graded_dir: Path
    logs_dir: Path
    reference_file: Path


def resolve_assignment_paths(
    cfg: AssignmentFileConfig, base_dir: Path
) -> AssignmentPaths:
    return AssignmentPaths(
        raw_dir=cfg.assignment.resolve_raw_dir(base_dir),
        processed_dir=cfg.assignment.resolve_processed_dir(base_dir),
        graded_dir=cfg.assignment.resolve_graded_dir(base_dir),
        logs_dir=cfg.assignment.resolve_logs_dir(base_dir),
        reference_file=cfg.assignment.resolve_reference_file(base_dir),
    )


def ensure_assignment_dirs(paths: AssignmentPaths) -> None:
    for directory in (
        paths.raw_dir,
        paths.processed_dir,
        paths.graded_dir,
        paths.logs_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def load_assignment_file(config_path: Path) -> AssignmentFileConfig:
    if not config_path.exists():
        msg = f"Assignment config not found: {config_path}"
        raise FileNotFoundError(msg)

    try:
        cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        msg = (
            f"Invalid TOML in config file: {config_path}\n"
            f"Details: {exc}\n"
            "Tip: start from assignments/example/config.toml and edit only [grading] first."
        )
        raise ValueError(msg) from exc

    try:
        return AssignmentFileConfig.model_validate(cfg)
    except ValidationError as exc:
        missing_required = []
        other_errors = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", []))
            if err.get("type") == "missing":
                missing_required.append(loc)
            else:
                other_errors.append(f"{loc}: {err.get('msg')}")

        detail_lines = []
        if missing_required:
            detail_lines.append(
                "Missing required config fields: " + ", ".join(sorted(missing_required))
            )
        if other_errors:
            detail_lines.append("Validation issues: " + "; ".join(other_errors))

        guidance = (
            "Required minimum is [grading] with rubric, system_prompt, provider.\n"
            "Example:\n"
            "[grading]\n"
            'rubric = "rubrics/example_rubric.toml"\n'
            'system_prompt = "prompt/lab.md"\n'
            'provider = "deepseek_chat_tool"'
        )

        msg = (
            f"Invalid assignment config: {config_path}\n"
            + ("\n".join(detail_lines) + "\n" if detail_lines else "")
            + guidance
        )
        raise ValueError(msg) from exc


def write_assignment_schema(output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(AssignmentFileConfig.model_json_schema(), indent=4),
        encoding="utf-8",
    )
    return output_path

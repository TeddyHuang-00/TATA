from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

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
        return (base_dir / (self.reference_file or "processed/reference.md")).resolve()


class ProcessingSection(BaseModel):
    input_format: InputFormat | None = Field(default=None)
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
    rubric: str
    system_prompt: str
    provider: str
    max_parallel_tasks: int = Field(default=10, ge=1, le=10)


class AssignmentFileConfig(BaseModel):
    assignment: AssignmentSection = Field(default_factory=AssignmentSection)
    processing: ProcessingSection = Field(default_factory=ProcessingSection)
    grading: GradingSection


def load_assignment_file(config_path: Path) -> AssignmentFileConfig:
    if not config_path.exists():
        msg = f"Assignment config not found: {config_path}"
        raise FileNotFoundError(msg)

    cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
    return AssignmentFileConfig.model_validate(cfg)


def write_assignment_schema(output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(AssignmentFileConfig.model_json_schema(), indent=4),
        encoding="utf-8",
    )
    return output_path

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

InputFormat = Literal["ipynb", "html", "markdown", "docx"]
ScoreReportDetail = Literal["full", "slim"]
ScoreOutputStyle = Literal["markdown", "plain", "html"]
HookMountPoint = Literal[
    "before_preprocess",
    "before_preprocess_file",
    "after_preprocess_file",
    "after_preprocess",
    "before_grade",
    "before_grade_submission",
    "after_grade_submission",
    "after_grade",
    "before_score",
    "after_score",
    "before_analyze",
    "after_analyze",
    "before_plagiarism",
    "after_plagiarism",
]


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

    def resolve_reference_file(self, base_dir: Path) -> Path | None:
        if self.reference_file is None:
            return None
        return (base_dir / self.reference_file).resolve()


class ProcessingSection(BaseModel):
    input_format: InputFormat | list[InputFormat] | None = Field(default=None)
    remove_base64_images: bool = Field(default=True)
    clean_filenames: bool = Field(default=True)
    strip_canvas_suffix: bool = Field(default=True)
    strip_html_callouts: bool = Field(default=True)
    strip_html_div_tags: bool = Field(default=True)
    strip_html_escaped_backslashes: bool = Field(default=True)
    strip_html_style_blocks: bool = Field(default=True)
    convert_html_tables_to_markdown: bool = Field(default=True)
    strip_colab_dataframe_widgets: bool = Field(default=True)
    strip_html_script_tags: bool = Field(default=True)
    strip_html_button_tags: bool = Field(default=True)
    strip_html_svg_tags: bool = Field(default=True)
    normalize_dtype_label_html: bool = Field(default=True)
    remove_nbconvert_assets: bool = Field(default=True)
    nbconvert_template: str | None = Field(default=None)
    nbconvert_template_dir: str | None = Field(default=None)
    render_screenshots: bool = Field(
        default=False,
        description="Render docx submissions to page screenshots (PDF->PNG) for multimodal grading. Optional, default off.",
    )
    screenshot_pages: int = Field(default=2, ge=1)


class HooksSection(BaseModel):
    dir: str = Field(default="hooks")
    mounts: dict[HookMountPoint, str | list[str]] = Field(default_factory=dict)

    @field_validator("mounts")
    @classmethod
    def _validate_mounts(
        cls,
        mounts: dict[HookMountPoint, str | list[str]],
    ) -> dict[HookMountPoint, str | list[str]]:
        for mount_point, script_cfg in mounts.items():
            if isinstance(script_cfg, str):
                if not script_cfg.strip():
                    msg = f"Hook entry at '{mount_point}' cannot be empty."
                    raise ValueError(msg)
                continue

            if isinstance(script_cfg, list):
                if not script_cfg:
                    msg = (
                        f"Hook list at '{mount_point}' cannot be empty. "
                        "Use omission to disable hooks for a lifecycle."
                    )
                    raise ValueError(msg)
                if any(not isinstance(v, str) or not v.strip() for v in script_cfg):
                    msg = f"Hook list at '{mount_point}' must contain non-empty script paths."
                    raise ValueError(msg)
                continue

            msg = (
                f"Hook entry at '{mount_point}' must be a string or list of strings, "
                f"got {type(script_cfg).__name__}."
            )
            raise ValueError(msg)

        return mounts


class GradingSection(BaseModel):
    rubric: str = Field(..., min_length=1)
    system_prompt: str | list[str] = Field(...)
    provider: str = Field(..., min_length=1)
    max_parallel_tasks: int = Field(default=10, ge=1, le=10)

    @field_validator("system_prompt")
    @classmethod
    def _validate_system_prompt(cls, value: str | list[str]) -> str | list[str]:
        if isinstance(value, str):
            if not value.strip():
                msg = "grading.system_prompt cannot be empty."
                raise ValueError(msg)
            return value

        if not value:
            msg = "grading.system_prompt list cannot be empty."
            raise ValueError(msg)
        if any(not isinstance(v, str) or not v.strip() for v in value):
            msg = "grading.system_prompt list must contain non-empty prompt file paths."
            raise ValueError(msg)
        return value


class ScoringSection(BaseModel):
    report_detail: ScoreReportDetail = Field(default="full")
    output_style: ScoreOutputStyle = Field(default="markdown")


class PlagiarismSection(BaseModel):
    output_dir: str = Field(default="plagiarism")
    template_file: str = Field(default="template.ipynb")
    submissions_subdir: str = Field(default="submissions")
    template_subdir: str = Field(default="template")
    report_file: str = Field(default="report.html")
    full_pairs_file: str = Field(default="all_pairs.json")
    display_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    extensions: list[str] = Field(default_factory=lambda: [".py"])
    include_python_files: bool = Field(default=True)

    @field_validator("extensions")
    @classmethod
    def _validate_extensions(cls, value: list[str]) -> list[str]:
        if not value:
            msg = "plagiarism.extensions cannot be empty."
            raise ValueError(msg)

        normalized: list[str] = []
        for ext in value:
            item = ext.strip()
            if not item:
                msg = "plagiarism.extensions must contain non-empty items."
                raise ValueError(msg)
            if not item.startswith("."):
                item = f".{item}"
            normalized.append(item.lower())
        return normalized


class AssignmentFileConfig(BaseModel):
    assignment: AssignmentSection = Field(default_factory=AssignmentSection)
    processing: ProcessingSection = Field(default_factory=ProcessingSection)
    hooks: HooksSection = Field(default_factory=HooksSection)
    grading: GradingSection
    scoring: ScoringSection = Field(default_factory=ScoringSection)
    plagiarism: PlagiarismSection = Field(default_factory=PlagiarismSection)


@dataclass(frozen=True)
class AssignmentPaths:
    raw_dir: Path
    processed_dir: Path
    graded_dir: Path
    logs_dir: Path
    reference_file: Path | None


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
            'system_prompt = ["prompt/system.md", "prompt/lab.md"]\n'
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

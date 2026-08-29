from __future__ import annotations

import base64
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import instructor
from openai import OpenAI
from pydantic import AliasChoices, BaseModel, Field

from .assignment_config import (
    ensure_assignment_dirs,
    load_assignment_file,
    resolve_assignment_paths,
)
from .cli_options import ConfigFileCliOptions, parse_cli_args
from .hooks_runtime import HookRuntime
from .provider import get_providers
from .rubric import generate_grading_model, get_rubric_definition


@dataclass
class AssignmentConfig:
    assignment_name: str
    processed_dir: Path
    graded_dir: Path
    logs_dir: Path
    reference_file: Path | None
    rubric_file: Path
    system_prompt_files: list[Path]
    provider_name: str
    max_parallel_tasks: int = 10


class GradingCheckpoint(BaseModel):
    done: list[str] = Field(default_factory=list)


class GradingCliOptions(ConfigFileCliOptions):
    force: bool = Field(
        default=False,
        validation_alias=AliasChoices("force", "f"),
        description="Ignore grading checkpoint and regrade all submissions.",
    )


def _load_assignment_config(config_path: Path) -> AssignmentConfig:
    cfg = load_assignment_file(config_path)
    grading = cfg.grading
    paths = resolve_assignment_paths(cfg, config_path.parent)
    ensure_assignment_dirs(paths)

    name = str(cfg.assignment.name)
    processed_dir = paths.processed_dir
    graded_dir = paths.graded_dir
    logs_dir = paths.logs_dir
    reference_file = paths.reference_file

    rubric_file = (config_path.parents[2] / grading.rubric).resolve()
    if isinstance(grading.system_prompt, str):
        system_prompt_files = [
            (config_path.parents[2] / grading.system_prompt).resolve()
        ]
    else:
        system_prompt_files = [
            (config_path.parents[2] / prompt_path).resolve()
            for prompt_path in grading.system_prompt
        ]
    provider_name = str(grading.provider)
    max_parallel_tasks = grading.max_parallel_tasks

    return AssignmentConfig(
        assignment_name=name,
        processed_dir=processed_dir,
        graded_dir=graded_dir,
        logs_dir=logs_dir,
        reference_file=reference_file,
        rubric_file=rubric_file,
        system_prompt_files=system_prompt_files,
        provider_name=provider_name,
        max_parallel_tasks=max_parallel_tasks,
    )


def _read_system_prompt(system_prompt_files: list[Path]) -> str:
    sections = [
        prompt_file.read_text(encoding="utf-8").strip()
        for prompt_file in system_prompt_files
    ]
    return "\n\n".join(section for section in sections if section)


def _load_checkpoint(checkpoint_file: Path) -> GradingCheckpoint:
    if not checkpoint_file.exists():
        return GradingCheckpoint()
    return GradingCheckpoint.model_validate_json(
        checkpoint_file.read_text(encoding="utf-8")
    )


def _save_checkpoint(checkpoint_file: Path, checkpoint: GradingCheckpoint) -> None:
    checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_file.write_text(checkpoint.model_dump_json(indent=2), encoding="utf-8")


def _collect_submissions(
    processed_dir: Path, reference_file: Path | None
) -> list[Path]:
    if not processed_dir.exists():
        msg = f"Processed directory not found: {processed_dir}"
        raise FileNotFoundError(msg)

    submission_files = sorted(processed_dir.glob("*.md"))
    if reference_file is None:
        return submission_files
    reference_stem = reference_file.stem
    return [p for p in submission_files if p.stem != reference_stem]


def _build_client(provider_name: str) -> tuple[Any, str]:
    providers = get_providers()
    provider = providers[provider_name]

    kwargs: dict[str, Any] = {
        "base_url": provider.base_url,
        "api_key": provider.api_key,
    }
    if provider.temperature is not None:
        kwargs["temperature"] = provider.temperature

    raw_client = OpenAI(**kwargs)
    return instructor.from_openai(raw_client, mode=provider.mode), provider.model


def _read_reference_text(reference_file: Path) -> str:
    suffix = reference_file.suffix.lower()

    if suffix == ".md":
        return reference_file.read_text(encoding="utf-8")

    if suffix == ".ipynb":
        cmd = [
            sys.executable,
            "-m",
            "jupyter",
            "nbconvert",
            "--to",
            "markdown",
            "--stdout",
            str(reference_file),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout
        except subprocess.CalledProcessError as exc:
            msg = (
                f"Failed to convert reference notebook to markdown: {reference_file}\n"
                f"Details: {exc.stderr}"
            )
            raise RuntimeError(msg) from exc

    if suffix == ".html":
        cmd = ["pandoc", "-f", "html", "-t", "markdown", str(reference_file)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout
        except subprocess.CalledProcessError as exc:
            msg = (
                f"Failed to convert reference HTML to markdown: {reference_file}\n"
                f"Details: {exc.stderr}"
            )
            raise RuntimeError(msg) from exc

    msg = (
        f"Unsupported reference file format: {reference_file.suffix}\n"
        "Supported reference formats are .md, .ipynb, and .html."
    )
    raise ValueError(msg)


def _grade_one_submission(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    client: Any,  # ruff: ignore[any-type]
    model_name: str,
    response_model: type[BaseModel],
    system_prompt: str,
    reference_text: str,
    student_text: str,
    images: list[str] | None = None,
) -> BaseModel:
    return client.chat.completions.create(
        model=model_name,
        response_model=response_model,
        messages=_build_grading_messages(
            system_prompt, reference_text, student_text, images
        ),
    )


def _build_grading_messages(
    system_prompt: str,
    reference_text: str,
    student_text: str,
    images: list[str] | None = None,
) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    if reference_text:
        messages.append({
            "role": "user",
            "content": f"Reference Answer:\n{reference_text}",
        })
    content: str | list[dict] = f"Student Answer:\n{student_text}"
    if images:
        content = [{"type": "text", "text": content}] + [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}}
            for img in images
        ]
    messages.append({"role": "user", "content": content})
    return messages


def _run_single_grading_task(  # ruff: ignore[too-many-arguments]
    submission: Path,
    *,
    client: Any,  # ruff: ignore[any-type]
    model_name: str,
    response_model: type[BaseModel],
    system_prompt: str,
    reference_text: str,
    hook_runtime: HookRuntime | None,
    assignment_config_path: Path,
    images: list[str] | None = None,
) -> tuple[str, str, str | None]:
    """Run grading for one submission.

    Returns:
        (submission_name, result_json, error_message)
    """
    try:
        student_text = submission.read_text(encoding="utf-8")

        if hook_runtime is not None:
            before_payload = hook_runtime.run(
                "before_grade_submission",
                {
                    "assignment_config": str(assignment_config_path),
                    "submission_name": submission.name,
                    "submission_path": str(submission),
                    "student_text": student_text,
                    "reference_text": reference_text,
                    "system_prompt": system_prompt,
                },
            )
            student_text = str(before_payload.get("student_text", student_text))
            reference_text = str(before_payload.get("reference_text", reference_text))
            system_prompt = str(before_payload.get("system_prompt", system_prompt))

        result = _grade_one_submission(
            client=client,
            model_name=model_name,
            response_model=response_model,
            system_prompt=system_prompt,
            reference_text=reference_text,
            student_text=student_text,
            images=images,
        )
        result_json = result.model_dump_json(indent=2)

        if hook_runtime is not None:
            after_payload = hook_runtime.run(
                "after_grade_submission",
                {
                    "assignment_config": str(assignment_config_path),
                    "submission_name": submission.name,
                    "submission_path": str(submission),
                    "result_json": result_json,
                    "error": None,
                },
            )
            result_json = str(after_payload.get("result_json", result_json))

        return submission.name, result_json, None
    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        if hook_runtime is not None:
            hook_runtime.run(
                "after_grade_submission",
                {
                    "assignment_config": str(assignment_config_path),
                    "submission_name": submission.name,
                    "submission_path": str(submission),
                    "result_json": "",
                    "error": error_message,
                },
            )
        return submission.name, "", error_message


def grade_assignment(config_path: Path, *, force: bool = False) -> dict | None:  # ruff: ignore[too-many-branches, too-many-statements, too-many-locals]
    cfg = _load_assignment_config(config_path)
    cfg_model = load_assignment_file(config_path)
    hook_runtime = HookRuntime.from_config(
        cfg_model,
        assignment_config_path=config_path,
    )

    cfg.graded_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)

    checkpoint_file = cfg.logs_dir / "grading.checkpoint.json"
    error_log_file = cfg.logs_dir / "grading.errors.log"

    checkpoint = _load_checkpoint(checkpoint_file)

    if not cfg.rubric_file.exists():
        msg = (
            f"Rubric file not found: {cfg.rubric_file}\n"
            "Set [grading.rubric] to an existing TOML file, e.g. rubrics/example_rubric.toml."
        )
        raise FileNotFoundError(msg)

    missing_prompt_files = [p for p in cfg.system_prompt_files if not p.exists()]
    if missing_prompt_files:
        msg = (
            "System prompt file(s) not found:\n"
            + "\n".join(f"- {p}" for p in missing_prompt_files)
            + "\nSet [grading.system_prompt] to an existing markdown file path or list of paths, "
            "e.g. 'prompt/system.md' or ['prompt/system.md', 'prompt/lab.md']."
        )
        raise FileNotFoundError(msg)

    if cfg.reference_file is not None and not cfg.reference_file.exists():
        msg = (
            f"Reference file not found: {cfg.reference_file}\n"
            "Create reference.md in assignment root (or reference.ipynb/reference.html), "
            "or set [assignment.reference_file] in config, "
            "or omit [assignment.reference_file] for rubric-only grading."
        )
        raise FileNotFoundError(msg)

    rubric_def = get_rubric_definition(cfg.rubric_file)
    response_model = generate_grading_model(rubric_def)

    system_prompt = _read_system_prompt(cfg.system_prompt_files)
    reference_text = (
        _read_reference_text(cfg.reference_file)
        if cfg.reference_file is not None
        else ""
    )

    submissions = _collect_submissions(cfg.processed_dir, cfg.reference_file)
    if not submissions:
        hint = "(excluding reference.md)" if cfg.reference_file is not None else ""
        print(
            f"No submissions found in: {cfg.processed_dir}\n"
            f"Run preprocess first and ensure processed/*.md exists {hint}."
        )
        return {
            "stage": "grade",
            "success": 0,
            "errors": 0,
            "total": 0,
            "success_rate": 0,
        }

    if force:
        pending_submissions = submissions
        print("Force mode enabled: ignoring checkpoint and regrading all submissions.")
    else:
        pending_submissions = [s for s in submissions if s.name not in checkpoint.done]
        if not pending_submissions:
            print("All submissions already graded (checkpoint hit).")
            return {
                "stage": "grade",
                "success": 0,
                "errors": 0,
                "total": 0,
                "success_rate": 0,
            }

    client, model_name = _build_client(cfg.provider_name)
    worker_count = min(cfg.max_parallel_tasks, len(pending_submissions))

    if hook_runtime is not None:
        hook_runtime.run(
            "before_grade",
            {
                "assignment_config": str(config_path),
                "submission_count": len(pending_submissions),
                "processed_dir": str(cfg.processed_dir),
                "graded_dir": str(cfg.graded_dir),
            },
        )

    print(
        f"Grading {len(pending_submissions)} submissions with {worker_count} parallel task(s)..."
    )

    done_count = 0
    error_count = 0

    screenshots_dir = cfg.processed_dir / "screenshots"
    use_images = cfg_model.processing.render_screenshots and screenshots_dir.exists()

    def _images_for(submission: Path) -> list[str]:
        if not use_images:
            return []
        imgs = []
        for f in sorted(screenshots_dir.glob(f"{submission.stem}_p*.png")):
            imgs.append(base64.b64encode(f.read_bytes()).decode())
        return imgs

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_submission = {
            executor.submit(
                _run_single_grading_task,
                submission,
                client=client,
                model_name=model_name,
                response_model=response_model,
                system_prompt=system_prompt,
                reference_text=reference_text,
                hook_runtime=hook_runtime,
                assignment_config_path=config_path,
                images=_images_for(submission),
            ): submission
            for submission in pending_submissions
        }

        for future in as_completed(future_to_submission):
            submission = future_to_submission[future]
            output_file = cfg.graded_dir / f"{submission.stem}.json"

            try:
                submission_name, result_json, error_message = future.result()
            except Exception as exc:
                submission_name = submission.name
                result_json = ""
                error_message = f"FutureError: {type(exc).__name__}: {exc}"

            if error_message is None:
                output_file.write_text(result_json, encoding="utf-8")
                if submission_name not in checkpoint.done:
                    checkpoint.done.append(submission_name)
                _save_checkpoint(checkpoint_file, checkpoint)
                print(f"[done] {submission_name}")
                done_count += 1
            else:
                with error_log_file.open("a", encoding="utf-8") as f:
                    f.write(f"{submission_name}: {error_message}\n")
                print(f"[error] {submission_name}: {error_message}")
                error_count += 1

    if hook_runtime is not None:
        hook_runtime.run(
            "after_grade",
            {
                "assignment_config": str(config_path),
                "done_count": done_count,
                "error_count": error_count,
                "graded_dir": str(cfg.graded_dir),
                "errors_log": str(error_log_file),
            },
        )

    total = done_count + error_count
    success_rate = (done_count / total * 100) if total > 0 else 0
    return {
        "stage": "grade",
        "success": done_count,
        "errors": error_count,
        "total": total,
        "success_rate": success_rate,
    }


def main() -> None:
    args = parse_cli_args(GradingCliOptions)

    grade_assignment(args.config, force=args.force)


if __name__ == "__main__":
    main()

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Any

import instructor
from openai import OpenAI
from pydantic import BaseModel, Field

from assignment_config import (
    ensure_assignment_dirs,
    load_assignment_file,
    resolve_assignment_paths,
)
from provider import get_providers
from rubric import generate_grading_model, get_rubric_definition
from hooks_runtime import HookRuntime


@dataclass
class AssignmentConfig:
    assignment_name: str
    processed_dir: Path
    graded_dir: Path
    logs_dir: Path
    reference_file: Path
    rubric_file: Path
    system_prompt_file: Path
    provider_name: str
    max_parallel_tasks: int = 10


class GradingCheckpoint(BaseModel):
    done: list[str] = Field(default_factory=list)


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
    system_prompt_file = (config_path.parents[2] / grading.system_prompt).resolve()
    provider_name = str(grading.provider)
    max_parallel_tasks = grading.max_parallel_tasks

    return AssignmentConfig(
        assignment_name=name,
        processed_dir=processed_dir,
        graded_dir=graded_dir,
        logs_dir=logs_dir,
        reference_file=reference_file,
        rubric_file=rubric_file,
        system_prompt_file=system_prompt_file,
        provider_name=provider_name,
        max_parallel_tasks=max_parallel_tasks,
    )


def _load_checkpoint(checkpoint_file: Path) -> GradingCheckpoint:
    if not checkpoint_file.exists():
        return GradingCheckpoint()
    return GradingCheckpoint.model_validate_json(
        checkpoint_file.read_text(encoding="utf-8")
    )


def _save_checkpoint(checkpoint_file: Path, checkpoint: GradingCheckpoint) -> None:
    checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_file.write_text(checkpoint.model_dump_json(indent=2), encoding="utf-8")


def _collect_submissions(processed_dir: Path, reference_file: Path) -> list[Path]:
    if not processed_dir.exists():
        msg = f"Processed directory not found: {processed_dir}"
        raise FileNotFoundError(msg)

    submission_files = sorted(processed_dir.glob("*.md"))
    reference_stem = reference_file.stem
    return [p for p in submission_files if p.stem != reference_stem]


def _build_client(provider_name: str):
    providers = get_providers()
    provider = providers[provider_name]

    raw_client = OpenAI(
        base_url=provider.base_url,
        api_key=provider.api_key,
    )
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


def _grade_one_submission(
    client,
    model_name: str,
    response_model: type[BaseModel],
    system_prompt: str,
    reference_text: str,
    student_text: str,
) -> BaseModel:
    return client.chat.completions.create(
        model=model_name,
        response_model=response_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"Reference Answer:\n{reference_text}",
            },
            {
                "role": "user",
                "content": f"Student Answer:\n{student_text}",
            },
        ],
    )


def _run_single_grading_task(
    submission: Path,
    *,
    client: Any,
    model_name: str,
    response_model: type[BaseModel],
    system_prompt: str,
    reference_text: str,
    hook_runtime: HookRuntime | None,
    assignment_config_path: Path,
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
    except Exception as exc:  # noqa: BLE001
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


def grade_assignment(config_path: Path, *, force: bool = False) -> None:
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

    if not cfg.system_prompt_file.exists():
        msg = (
            f"System prompt file not found: {cfg.system_prompt_file}\n"
            "Set [grading.system_prompt] to an existing markdown file, e.g. prompt/system.md."
        )
        raise FileNotFoundError(msg)

    if not cfg.reference_file.exists():
        msg = (
            f"Reference file not found: {cfg.reference_file}\n"
            "Create reference.md in assignment root (or reference.ipynb/reference.html), "
            "or set [assignment.reference_file] in config."
        )
        raise FileNotFoundError(msg)

    rubric_def = get_rubric_definition(cfg.rubric_file)
    response_model = generate_grading_model(rubric_def)

    system_prompt = cfg.system_prompt_file.read_text(encoding="utf-8")
    reference_text = _read_reference_text(cfg.reference_file)

    submissions = _collect_submissions(cfg.processed_dir, cfg.reference_file)
    if not submissions:
        print(
            f"No submissions found in: {cfg.processed_dir}\n"
            "Run preprocess first and ensure processed/*.md exists (excluding reference.md)."
        )
        return

    if force:
        pending_submissions = submissions
        print("Force mode enabled: ignoring checkpoint and regrading all submissions.")
    else:
        pending_submissions = [s for s in submissions if s.name not in checkpoint.done]
        if not pending_submissions:
            print("All submissions already graded (checkpoint hit).")
            return

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
            ): submission
            for submission in pending_submissions
        }

        for future in as_completed(future_to_submission):
            submission = future_to_submission[future]
            output_file = cfg.graded_dir / f"{submission.stem}.json"

            try:
                submission_name, result_json, error_message = future.result()
            except Exception as exc:  # noqa: BLE001
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


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run assignment grading pipeline.")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to assignment config TOML.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore grading checkpoint and regrade all submissions.",
    )
    args = parser.parse_args()

    grade_assignment(args.config, force=args.force)


if __name__ == "__main__":
    main()

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
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
                "content": (
                    "Reference Answer:\n"
                    f"{reference_text}\n\n"
                    "Student Answer:\n"
                    f"{student_text}"
                ),
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
) -> tuple[str, str, str | None]:
    """Run grading for one submission.

    Returns:
        (submission_name, result_json, error_message)
    """
    try:
        student_text = submission.read_text(encoding="utf-8")
        result = _grade_one_submission(
            client=client,
            model_name=model_name,
            response_model=response_model,
            system_prompt=system_prompt,
            reference_text=reference_text,
            student_text=student_text,
        )
        return submission.name, result.model_dump_json(indent=2), None
    except Exception as exc:  # noqa: BLE001
        return submission.name, "", f"{type(exc).__name__}: {exc}"


def grade_assignment(config_path: Path) -> None:
    cfg = _load_assignment_config(config_path)

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
            "Set [grading.system_prompt] to an existing markdown file, e.g. prompt/lab.md."
        )
        raise FileNotFoundError(msg)

    if not cfg.reference_file.exists():
        msg = (
            f"Reference file not found: {cfg.reference_file}\n"
            "Create processed/reference.md or set [assignment.reference_file] in config."
        )
        raise FileNotFoundError(msg)

    rubric_def = get_rubric_definition(cfg.rubric_file)
    response_model = generate_grading_model(rubric_def)

    system_prompt = cfg.system_prompt_file.read_text(encoding="utf-8")
    reference_text = cfg.reference_file.read_text(encoding="utf-8")

    submissions = _collect_submissions(cfg.processed_dir, cfg.reference_file)
    if not submissions:
        print(
            f"No submissions found in: {cfg.processed_dir}\n"
            "Run preprocess first and ensure processed/*.md exists (excluding reference.md)."
        )
        return

    pending_submissions = [s for s in submissions if s.name not in checkpoint.done]
    if not pending_submissions:
        print("All submissions already graded (checkpoint hit).")
        return

    client, model_name = _build_client(cfg.provider_name)
    worker_count = min(cfg.max_parallel_tasks, len(pending_submissions))
    print(
        f"Grading {len(pending_submissions)} submissions with {worker_count} parallel task(s)..."
    )

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
                checkpoint.done.append(submission_name)
                _save_checkpoint(checkpoint_file, checkpoint)
                print(f"[done] {submission_name}")
            else:
                with error_log_file.open("a", encoding="utf-8") as f:
                    f.write(f"{submission_name}: {error_message}\n")
                print(f"[error] {submission_name}: {error_message}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run assignment grading pipeline.")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to assignment config TOML.",
    )
    args = parser.parse_args()

    grade_assignment(args.config)


if __name__ == "__main__":
    main()

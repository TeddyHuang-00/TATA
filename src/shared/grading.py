from __future__ import annotations

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, BaseModel, Field

from .assignment_config import (
    AssignmentFileConfig,
    config_root,
    ensure_assignment_dirs,
    load_assignment_file,
    resolve_assignment_paths,
)
from .caching import (
    cache_file,
    content_hash,
    file_digest,
    load_cache_file,
    save_cache_file,
)
from .cli_options import ConfigFileCliOptions, parse_cli_args
from .hooks_runtime import HookRuntime
from .provider import build_provider_client, get_providers
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
    hooks_dir: Path | None
    max_parallel_tasks: int = 10


class GradingCliOptions(ConfigFileCliOptions):
    force: bool = Field(
        default=False,
        validation_alias=AliasChoices("force", "f"),
        description="Ignore the grading hash cache and regrade all submissions.",
    )


def load_assignment_config(config_path: Path) -> AssignmentConfig:
    cfg = load_assignment_file(config_path)
    grading = cfg.grading
    paths = resolve_assignment_paths(cfg, config_path.parent)
    ensure_assignment_dirs(paths)

    name = str(cfg.assignment.name)
    processed_dir = paths.processed_dir
    graded_dir = paths.graded_dir
    logs_dir = paths.logs_dir
    reference_file = paths.reference_file

    rubric_file = (config_root(config_path) / grading.rubric).resolve()
    if isinstance(grading.system_prompt, str):
        system_prompt_files = [
            (config_root(config_path) / grading.system_prompt).resolve()
        ]
    else:
        system_prompt_files = [
            (config_root(config_path) / prompt_path).resolve()
            for prompt_path in grading.system_prompt
        ]
    provider_name = str(grading.provider)
    max_parallel_tasks = grading.max_parallel_tasks
    # Same resolution as HookRuntime.from_config: hooks.dir sits under the
    # data/ root; None when no mount is configured (nothing to hash).
    hooks_dir = (
        (config_root(config_path) / cfg.hooks.dir).resolve()
        if cfg.hooks.mounts
        else None
    )

    return AssignmentConfig(
        assignment_name=name,
        processed_dir=processed_dir,
        graded_dir=graded_dir,
        logs_dir=logs_dir,
        reference_file=reference_file,
        rubric_file=rubric_file,
        system_prompt_files=system_prompt_files,
        provider_name=provider_name,
        hooks_dir=hooks_dir,
        max_parallel_tasks=max_parallel_tasks,
    )


def _read_system_prompt(system_prompt_files: list[Path]) -> str:
    sections = [
        prompt_file.read_text(encoding="utf-8").strip()
        for prompt_file in system_prompt_files
    ]
    return "\n\n".join(section for section in sections if section)


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


def _submission_images(cfg: AssignmentConfig, stem: str) -> list[Path]:
    """Screenshots of ``stem`` in grader order: page renders (``_pN``) first,
    then notebook-extracted outputs (``_iN``), each naturally sorted; ``[]``
    when none exist. Single source for the grading hash and the vision
    payload (``_images_for``)."""
    shots_dir = cfg.processed_dir / "screenshots"
    try:
        mtime_ns = shots_dir.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0  # no screenshots dir -> empty listing below
    return [
        shots_dir / name
        for name in _submission_image_names(str(shots_dir), stem, mtime_ns)
    ]


# Memoized so TUI polling goes stat + cache hit instead of re-globbing per
# tick; the directory's mtime_ns keys it (add/remove/rename bumps it — content
# rewrites need not: only names are cached here, and file_digest keys on the
# file's own mtime_ns/size).
@lru_cache(maxsize=4096)
def _submission_image_names(
    dir_str: str, stem: str, dir_mtime_ns: int
) -> tuple[str, ...]:
    del dir_mtime_ns
    shots_dir = Path(dir_str)
    pages = sorted(shots_dir.glob(f"{stem}_p*.png"))
    extracted = sorted(shots_dir.glob(f"{stem}_i*.png"))
    return tuple([p.name for p in pages] + [p.name for p in extracted])


# Mount points grading.py invokes (docs/hooks.md lifecycle map): only these
# hooks run during the grade stage, so only their config/scripts enter the
# grading hash — other stages' hooks cannot affect it.
_GRADE_HOOK_MOUNTS = (
    "before_grade",
    "before_grade_submission",
    "after_grade_submission",
    "after_grade",
)


def _hook_hash_parts(
    cfg: AssignmentConfig, cfg_model: AssignmentFileConfig
) -> list[bytes]:
    """Hash parts for grade-stage hooks: for each referenced script, its mount
    point + configured path and a ``file_digest`` of its bytes (a missing
    script hashes as a marker — grading fails on it anyway). Runtime behavior
    (env vars, files a script reads, side effects) is not statically
    capturable and stays outside the hash."""
    if cfg.hooks_dir is None:
        return []
    parts: list[bytes] = []
    for mount_point in _GRADE_HOOK_MOUNTS:
        script_cfg = cfg_model.hooks.mounts.get(mount_point)
        if script_cfg is None:
            continue
        script_rels = [script_cfg] if isinstance(script_cfg, str) else script_cfg
        for script_rel in script_rels:
            script_path = (cfg.hooks_dir / script_rel).resolve()
            script_digest = (
                file_digest(script_path).encode()
                if script_path.is_file()
                else b"<missing>"
            )
            parts.extend([f"{mount_point}:{script_rel}".encode(), script_digest])
    return parts


def grading_pending(
    cfg: AssignmentConfig, cfg_model: AssignmentFileConfig
) -> tuple[list[Path], dict[str, str]]:
    """(submissions to (re)grade, stem -> input hash) under the grading cache rule.

    Single source of the rule shared by ``grade_assignment`` and the TUI's
    display: a submission is pending unless ``<assignment>/.cache/grading.json``
    holds a matching hash AND its graded JSON exists. Hash covers the processed
    md, rubric, system prompts, reference, the [grading] section, the
    provider entry (name/base_url/model/mode/temperature), the
    visual_evaluation flag, its screenshots (visual on) and the grade-stage
    hooks (config + script bytes); any change regrades. Every file input goes
    through the memoized ``file_digest`` so TUI polling does not re-read
    screenshots.
    """
    submissions = _collect_submissions(cfg.processed_dir, cfg.reference_file)
    cache = load_cache_file(cache_file(cfg.processed_dir.parent, "grading"))

    provider = get_providers()[cfg_model.grading.provider]
    grading_payload = json.dumps(
        {
            "grading": cfg_model.grading.model_dump_json(),
            "provider": {
                "name": cfg_model.grading.provider,
                "base_url": provider.base_url,
                "model": provider.model,
                "mode": getattr(provider.mode, "value", provider.mode),
                "temperature": provider.temperature,
            },
            "visual_evaluation": cfg_model.processing.visual_evaluation,
        },
        sort_keys=True,
    ).encode("utf-8")
    reference_digest = (
        file_digest(cfg.reference_file).encode()
        if cfg.reference_file is not None
        else b""
    )
    shared_parts = [
        file_digest(cfg.rubric_file).encode(),
        *[file_digest(p).encode() for p in cfg.system_prompt_files],
        reference_digest,
        grading_payload,
        *_hook_hash_parts(cfg, cfg_model),
    ]

    visual_evaluation = cfg_model.processing.visual_evaluation
    sub_hashes: dict[str, str] = {}
    for submission in submissions:
        image_parts = (
            [file_digest(p).encode() for p in _submission_images(cfg, submission.stem)]
            if visual_evaluation
            else []
        )
        sub_hashes[submission.stem] = content_hash([
            file_digest(submission).encode(),
            *shared_parts,
            *image_parts,
        ])

    def cached_valid(submission: Path) -> bool:
        entry = cache.get(submission.stem)
        output_file = cfg.graded_dir / f"{submission.stem}.json"
        return bool(
            isinstance(entry, dict)
            and entry.get("hash") == sub_hashes[submission.stem]
            and output_file.is_file()
        )

    return ([s for s in submissions if not cached_valid(s)], sub_hashes)


def pending_grade_submissions(config_path: Path) -> list[Path]:
    """Submissions ``grade_assignment`` would (re)grade right now (cache rule)."""
    cfg = load_assignment_config(config_path)
    cfg_model = load_assignment_file(config_path)
    return grading_pending(cfg, cfg_model)[0]


def cached_grade_count(config_path: Path) -> int:
    """Submissions currently valid under the grading cache (inverse of pending).

    Same rule ``grade_assignment`` applies; the TUI progress bar polls this
    per tick because the cache is updated per submission during a run.
    """
    cfg = load_assignment_config(config_path)
    cfg_model = load_assignment_file(config_path)
    pending, sub_hashes = grading_pending(cfg, cfg_model)
    return len(sub_hashes) - len(pending)


def build_client(provider_name: str) -> tuple[Any, str]:
    provider = get_providers()[provider_name]
    client = build_provider_client(
        provider.base_url,
        provider.api_key,
        provider.mode,
        provider.temperature,
    )
    return client, provider.model


def _read_reference_text(reference_file: Path) -> str:
    from markitdown import MarkItDown  # ruff: ignore[import-outside-top-level]
    from nbconvert import MarkdownExporter  # ruff: ignore[import-outside-top-level]

    suffix = reference_file.suffix.lower()

    if suffix == ".md":
        return reference_file.read_text(encoding="utf-8")

    if suffix == ".ipynb":
        try:
            return MarkdownExporter().from_filename(str(reference_file))[0]
        except Exception as exc:
            msg = (
                f"Failed to convert reference notebook to markdown: {reference_file}\n"
                f"Details: {exc}"
            )
            raise RuntimeError(msg) from exc

    if suffix == ".html":
        try:
            return MarkItDown().convert(str(reference_file)).text_content
        except Exception as exc:
            msg = (
                f"Failed to convert reference HTML to markdown: {reference_file}\n"
                f"Details: {exc}"
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
    try:  # ruff: ignore[too-many-statements-in-try-clause]
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


def grade_assignment(  # ruff: ignore[too-many-branches, too-many-statements, too-many-locals]
    config_path: Path,
    *,
    force: bool = False,
    cancel_event: threading.Event | None = None,
) -> dict | None:
    """Grade pending submissions; a set ``cancel_event`` (TUI jobs) short
    circuits before submitting and stops at the next submission boundary —
    the screenshot encode + submit pass checks the event per submission, so a
    cancel during encoding drops the remaining submissions; queued futures
    are dropped at the next result boundary and in-flight LLM calls finish
    (the honest ceiling; synchronous calls cannot be killed)."""
    cfg = load_assignment_config(config_path)
    cfg_model = load_assignment_file(config_path)
    hook_runtime = HookRuntime.from_config(
        cfg_model,
        assignment_config_path=config_path,
    )

    cfg.graded_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)

    error_log_file = cfg.logs_dir / "grading.errors.log"

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

    # Grading cache: a submission is pending unless its input hash matches
    # .cache/grading.json AND the graded JSON exists; any change regrades.
    # (Rule lives in grading_pending — shared with the TUI display.)
    cache_path = cache_file(cfg.processed_dir.parent, "grading")
    pending_submissions, sub_hashes = grading_pending(cfg, cfg_model)
    cache = load_cache_file(cache_path)

    if force:
        pending_submissions = submissions
        print("Force mode enabled: ignoring cache and regrading all submissions.")
    elif not pending_submissions:
        print("All submissions already graded (cache hit).")
        return {
            "stage": "grade",
            "success": 0,
            "errors": 0,
            "total": 0,
            "success_rate": 0,
        }

    if cancel_event is not None and cancel_event.is_set():
        print("[cancelled] grade stopped before submitting — nothing queued")
        return {
            "stage": "grade",
            "success": 0,
            "errors": 0,
            "total": 0,
            "success_rate": 0,
        }

    client, model_name = build_client(cfg.provider_name)
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
    use_images = cfg_model.processing.visual_evaluation and screenshots_dir.exists()

    def _images_for(submission: Path) -> list[str]:
        if not use_images:
            return []
        # Same discovery as the grading hash: _submission_images.
        return [
            base64.b64encode(f.read_bytes()).decode()
            for f in _submission_images(cfg, submission.stem)
        ]

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_submission = {}
        for submission in pending_submissions:
            if cancel_event is not None and cancel_event.is_set():
                # Cancel observed while encoding an earlier submission's
                # screenshots: the remaining submissions are neither encoded
                # nor submitted (a text-only grade must not pass the
                # visual-evaluation cache check).
                print("[cancelled] grade stopped — remaining submissions not submitted")
                break
            future_to_submission[
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
                )
            ] = submission

        for future in as_completed(future_to_submission):
            if cancel_event is not None and cancel_event.is_set():
                # Cancel queued (not yet started) submissions; running calls
                # finish (the executor's own shutdown waits for them).
                executor.shutdown(cancel_futures=True)
                print(
                    "[cancelled] grade stopped — queued submissions dropped, "
                    "in-flight calls finish"
                )
                break
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
                try:
                    cache[submission.stem] = {"hash": sub_hashes[submission.stem]}
                    save_cache_file(cache_path, cache)
                except Exception as exc:  # grading must never break on cache
                    print(f"[warn] failed to write grading cache: {exc}")
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
                "cancelled": cancel_event is not None and cancel_event.is_set(),
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

from __future__ import annotations

import sys

from pydantic_settings import CliApp, get_subcommand

from src import REPO_ROOT
from src.shared.analysis import analyze_assignment
from src.shared.assignment_config import (
    config_root,
    load_assignment_file,
    resolve_assignment_paths,
)
from src.shared.cli_options import (
    AnalyzeCliOptions,
    ConfigCliOptions,
    ConfigSetCliOptions,
    FetchCliOptions,
    GradeCliOptions,
    PlagiarismCliOptions,
    PreprocessCliOptions,
    RubricCliOptions,
    RubricGenCliOptions,
    ScoreCliOptions,
    ScoreReviewCliOptions,
    TataCli,
    ValidateCliOptions,
    parse_cli_args,
)
from src.shared.config_edit import edit_config, validate_config_edits
from src.shared.fetch_pipeline import format_job_summary, run_fetch
from src.shared.grading import grade_assignment
from src.shared.plagiarism import detect_plagiarism
from src.shared.processing import preprocess_assignment
from src.shared.provider import get_providers
from src.shared.rubric import get_rubric_definition
from src.shared.rubric_gen import generate_rubric
from src.shared.scoring import score_assignment
from src.tui.score_review import run as run_score_viewer

# Stage subcommands: type -> (label, pipeline function).
_STAGES = {
    PreprocessCliOptions: ("preprocessing", preprocess_assignment),
    PlagiarismCliOptions: ("plagiarism detection", detect_plagiarism),
    GradeCliOptions: ("grading", grade_assignment),
    ScoreCliOptions: ("scoring", score_assignment),
    AnalyzeCliOptions: ("meta analysis", analyze_assignment),
}


# --- config set subcommand ---


def _coerce_config_value(raw: str) -> object:
    """TOML-style coercion for ``config set`` values (tomlkit serializes)."""
    if raw == "true":
        return True
    if raw == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _run_config_set(args: ConfigSetCliOptions) -> None:
    """Edit one dotted ``section.key`` in a config.toml (validated, then write)."""
    if args.key.count(".") != 1 or not all(args.key.split(".", 1)):
        sys.exit(f"error: key must be section.key (exactly one dot): {args.key!r}")
    section, key = args.key.split(".", 1)
    edits = {section: {key: _coerce_config_value(args.value)}}
    try:
        validate_config_edits(args.config, edits)
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    edit_config(args.config, edits)
    print(f"[config] wrote {section}.{key} in {args.config}")


def _run_validate(args: ValidateCliOptions) -> None:  # ruff: ignore[too-many-branches]
    """Validate an assignment config without grading: model load, rubric,
    prompts, provider, reference (same path resolution as grade)."""
    cfg_path = args.config
    try:
        cfg = load_assignment_file(cfg_path)
    except (ValueError, FileNotFoundError) as exc:
        sys.exit(f"error: {exc}")

    ok: list[str] = []
    errors: list[str] = []
    base = config_root(cfg_path)

    rubric_path = (base / cfg.grading.rubric).resolve()
    try:
        rubric = get_rubric_definition(rubric_path)
    except (FileNotFoundError, ValueError) as exc:
        errors.append(f"ERROR rubric: {exc}")
    else:
        ok.append(f"rubric OK: {rubric_path.name} ({len(rubric.criterion)} criteria)")

    prompts = (
        [cfg.grading.system_prompt]
        if isinstance(cfg.grading.system_prompt, str)
        else cfg.grading.system_prompt
    )
    missing_prompts = [p for p in prompts if not (base / p).resolve().exists()]
    if missing_prompts:
        errors.append("ERROR prompt file not found: " + ", ".join(missing_prompts))
    else:
        ok.append(f"prompt OK: {len(prompts)} file(s)")

    reference_file = resolve_assignment_paths(cfg, cfg_path.parent).reference_file
    if reference_file is not None:
        if reference_file.exists():
            ok.append(f"reference OK: {reference_file.name}")
        else:
            errors.append(f"ERROR reference file not found: {reference_file}")

    try:
        providers = get_providers().providers
    except (FileNotFoundError, ValueError) as exc:
        errors.append(f"ERROR providers: {exc}")
    else:
        provider_name = str(cfg.grading.provider)
        if provider_name not in providers:
            errors.append(
                f"ERROR provider '{provider_name}' not found "
                f"(available: {sorted(providers)})"
            )
        else:
            ok.append(f"provider OK: {provider_name}")

    for line in ok:
        print(line)
    for line in errors:
        print(line)
    if errors:
        sys.exit(1)
    print(f"OK {cfg_path}")


def _run_rubric_generate(args: RubricGenCliOptions) -> None:
    """Generate a rubric from the fetched assignment description."""
    out = args.out or (
        REPO_ROOT / "data" / "rubrics" / f"{args.config.resolve().parent.name}.toml"
    )
    try:
        rubric = generate_rubric(args.config, out)
    except (ValueError, FileNotFoundError) as exc:
        sys.exit(f"error: {exc}")
    print(f"[rubric] wrote {len(rubric.criterion)} criteria to {out}")
    print(
        f"[rubric] hint: set [grading].rubric = rubrics/{out.name} "
        f"(uv run cli config set -c {args.config} grading.rubric rubrics/{out.name})"
    )


def main() -> None:
    cmd = parse_cli_args(TataCli)
    sub = get_subcommand(cmd, is_required=False)
    if sub is None:
        print(CliApp.format_help(TataCli), file=sys.stderr)
        raise SystemExit(2)

    if isinstance(sub, ValidateCliOptions):
        _run_validate(sub)
        return

    if isinstance(sub, FetchCliOptions):
        run_fetch(sub)
        return

    if isinstance(sub, ConfigCliOptions):
        if sub.set is None:
            sys.exit(
                "error: config requires a subcommand: config set -c PATH section.key VALUE"
            )
        _run_config_set(sub.set)
        return

    if isinstance(sub, RubricCliOptions):
        if sub.generate is None:
            sys.exit(
                "error: rubric requires a subcommand: rubric generate -c PATH [-o OUT]"
            )
        _run_rubric_generate(sub.generate)
        return

    if isinstance(sub, ScoreReviewCliOptions):
        run_score_viewer(sub)
        return

    label, fn = _STAGES[type(sub)]
    print(f"Running {label}...")
    kwargs = {}
    if isinstance(sub, GradeCliOptions):
        kwargs = {"force": sub.force}
    elif isinstance(sub, PlagiarismCliOptions):
        kwargs = {"aggregate": sub.aggregate, "output": sub.output}
    summary = fn(sub.config, **kwargs)
    if summary is not None:
        print(format_job_summary(summary))


if __name__ == "__main__":
    main()

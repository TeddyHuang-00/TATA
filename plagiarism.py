from __future__ import annotations

import json
from dataclasses import dataclass
from operator import itemgetter
from pathlib import Path

import nbformat
from assignment_config import (
    ensure_assignment_dirs,
    load_assignment_file,
    resolve_assignment_paths,
)
from cli_options import ConfigFileCliOptions, parse_cli_args
from copydetect import CopyDetector
from hooks_runtime import HookRuntime


class PlagiarismCliOptions(ConfigFileCliOptions):
    pass


@dataclass(frozen=True)
class PlagiarismConfig:
    raw_dir: Path
    output_dir: Path
    submissions_dir: Path
    template_dir: Path
    report_file: Path
    full_pairs_file: Path
    template_file: Path
    extensions: list[str]
    display_threshold: float
    include_python_files: bool


def _safe_output_name(file_path: Path, base_dir: Path) -> str:
    rel = file_path.relative_to(base_dir)
    stem = rel.with_suffix("").as_posix().replace("/", "__")
    return f"{stem}.py"


def _extract_notebook_code(input_path: Path) -> str:
    with input_path.open("r", encoding="utf-8") as file:
        notebook = nbformat.read(file, as_version=4)
    code_cells = [
        str(cell.source).strip()
        for cell in notebook.cells
        if cell.cell_type == "code" and str(cell.source).strip()
    ]
    return "\n\n".join(code_cells)


def _write_extracted_code(input_path: Path, output_path: Path) -> None:
    suffix = input_path.suffix.lower()
    if suffix == ".ipynb":
        code = _extract_notebook_code(input_path)
    elif suffix == ".py":
        code = input_path.read_text(encoding="utf-8")
    else:
        msg = f"Unsupported input type for extraction: {input_path}"
        raise ValueError(msg)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(code, encoding="utf-8")


def _load_plagiarism_config(config_path: Path) -> PlagiarismConfig:
    cfg = load_assignment_file(config_path)
    paths = resolve_assignment_paths(cfg, config_path.parent)
    ensure_assignment_dirs(paths)

    plagiarism = cfg.plagiarism
    output_dir = (config_path.parent / plagiarism.output_dir).resolve()
    submissions_dir = (output_dir / plagiarism.submissions_subdir).resolve()
    template_dir = (output_dir / plagiarism.template_subdir).resolve()
    report_file = (output_dir / plagiarism.report_file).resolve()
    full_pairs_file = (output_dir / plagiarism.full_pairs_file).resolve()
    template_file = (config_path.parent / plagiarism.template_file).resolve()

    return PlagiarismConfig(
        raw_dir=paths.raw_dir,
        output_dir=output_dir,
        submissions_dir=submissions_dir,
        template_dir=template_dir,
        report_file=report_file,
        full_pairs_file=full_pairs_file,
        template_file=template_file,
        extensions=plagiarism.extensions,
        display_threshold=plagiarism.display_threshold,
        include_python_files=plagiarism.include_python_files,
    )


def _write_full_pair_data(detector: CopyDetector, output_path: Path) -> int:
    """Export all compared student pairs from copydetect matrices.

    Returns number of exported undirected pairs.
    """
    if len(detector.similarity_matrix) == 0:
        payload = {
            "version": 1,
            "test_file_count": len(detector.test_files),
            "reference_file_count": len(detector.ref_files),
            "pair_count": 0,
            "pairs": [],
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return 0

    seen_pairs: set[tuple[str, str]] = set()
    rows: list[dict] = []

    for test_idx, test_file in enumerate(detector.test_files):
        for ref_idx, ref_file in enumerate(detector.ref_files):
            if test_file == ref_file:
                continue

            pair_key = tuple(sorted((test_file, ref_file)))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            test_similarity = float(detector.similarity_matrix[test_idx, ref_idx, 0])
            reference_similarity = float(detector.similarity_matrix[test_idx, ref_idx, 1])
            if test_similarity < 0 and reference_similarity < 0:
                continue

            token_overlap = int(detector.token_overlap_matrix[test_idx, ref_idx])
            rows.append(
                {
                    "test_file": test_file,
                    "reference_file": ref_file,
                    "test_similarity_pct": test_similarity * 100,
                    "reference_similarity_pct": reference_similarity * 100,
                    "max_similarity_pct": max(test_similarity, reference_similarity) * 100,
                    "token_overlap": token_overlap,
                }
            )

    rows.sort(
        key=itemgetter("max_similarity_pct", "token_overlap"),
        reverse=True,
    )
    payload = {
        "version": 1,
        "test_file_count": len(detector.test_files),
        "reference_file_count": len(detector.ref_files),
        "pair_count": len(rows),
        "pairs": rows,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return len(rows)


def detect_plagiarism(assignment_config_path: Path) -> dict | None:
    cfg_model = load_assignment_file(assignment_config_path)
    hook_runtime = HookRuntime.from_config(
        cfg_model,
        assignment_config_path=assignment_config_path,
    )
    cfg = _load_plagiarism_config(assignment_config_path)

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    cfg.submissions_dir.mkdir(parents=True, exist_ok=True)
    cfg.template_dir.mkdir(parents=True, exist_ok=True)

    if hook_runtime is not None:
        hook_runtime.run(
            "before_plagiarism",
            {
                "assignment_config": str(assignment_config_path),
                "raw_dir": str(cfg.raw_dir),
                "output_dir": str(cfg.output_dir),
                "template_file": str(cfg.template_file),
            },
        )

    if not cfg.template_file.exists():
        msg = (
            f"Template file not found: {cfg.template_file}\n"
            "Create template.ipynb in assignment root (or set [plagiarism.template_file] in config)."
        )
        raise FileNotFoundError(msg)

    extracted_success = 0
    extracted_errors = 0

    _write_extracted_code(cfg.template_file, cfg.template_dir / "template.py")

    submission_files = sorted(cfg.raw_dir.rglob("*.ipynb"))
    if cfg.include_python_files:
        submission_files.extend(sorted(cfg.raw_dir.rglob("*.py")))

    if not submission_files:
        print(
            f"No submission files found in: {cfg.raw_dir}\n"
            "Add .ipynb files to raw/ (or enable python files) before running plagiarism stage."
        )
        return {
            "stage": "plagiarism",
            "success": 0,
            "errors": 0,
            "total": 0,
            "success_rate": 0,
        }

    for submission_file in submission_files:
        try:
            output_name = _safe_output_name(submission_file, cfg.raw_dir)
            _write_extracted_code(submission_file, cfg.submissions_dir / output_name)
            extracted_success += 1
        except Exception as exc:
            print(f"[error] Failed to extract {submission_file.name}: {exc}")
            extracted_errors += 1

    detector = CopyDetector(
        test_dirs=[str(cfg.submissions_dir)],
        boilerplate_dirs=[str(cfg.template_dir)],
        extensions=cfg.extensions,
        display_t=cfg.display_threshold,
        out_file=str(cfg.report_file),
    )
    detector.run()
    exported_pairs = _write_full_pair_data(detector, cfg.full_pairs_file)
    detector.generate_html_report()

    print(f"[plagiarism] {cfg.report_file}")
    print(f"[plagiarism] full pair data -> {cfg.full_pairs_file} ({exported_pairs} pairs)")
    print(f"[plagiarism] extracted submissions -> {cfg.submissions_dir}")
    print(f"[plagiarism] extracted template -> {cfg.template_dir}")

    if hook_runtime is not None:
        hook_runtime.run(
            "after_plagiarism",
            {
                "assignment_config": str(assignment_config_path),
                "report_file": str(cfg.report_file),
                "full_pairs_file": str(cfg.full_pairs_file),
                "submissions_dir": str(cfg.submissions_dir),
                "template_dir": str(cfg.template_dir),
                "success_count": extracted_success,
                "error_count": extracted_errors,
            },
        )

    total = extracted_success + extracted_errors
    success_rate = (extracted_success / total * 100) if total > 0 else 0
    return {
        "stage": "plagiarism",
        "success": extracted_success,
        "errors": extracted_errors,
        "total": total,
        "success_rate": success_rate,
    }


def main() -> None:
    args = parse_cli_args(PlagiarismCliOptions)
    detect_plagiarism(args.config)


if __name__ == "__main__":
    main()

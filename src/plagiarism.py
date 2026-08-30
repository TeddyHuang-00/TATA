from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from operator import itemgetter
from pathlib import Path

import nbformat
import numpy as np
from copydetect import CopyDetector

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    SentenceTransformer = None  # type: ignore[assignment]

from .assignment_config import (
    FetchSection,
    PlagiarismSection,
    ensure_assignment_dirs,
    is_root_config,
    load_assignment_file,
    resolve_assignment_paths,
)
from .cli_options import (
    AliasChoices,
    ConfigFileCliOptions,
    Field,
    parse_cli_args,
)
from .hooks_runtime import HookRuntime
from .plagiarism_aggregate import (
    DEFAULT_PAIRS_GLOB,
    BuildConfig,
    _build_payload,
    _to_text,
)

MIN_TEXT_CHARS = 20


class PlagiarismCliOptions(ConfigFileCliOptions):
    """Direct-run options (``python src/plagiarism.py``); the main CLI lives
    in ``src/cli_options.py``."""

    aggregate: bool = Field(
        default=False,
        description="Produce the cross-assignment z-score aggregate report.",
    )
    output: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("output", "o"),
        description="Write the aggregate report to this file instead of stdout.",
    )


@dataclass(frozen=True)
class PlagiarismConfig:
    raw_dir: Path
    processed_dir: Path
    output_dir: Path
    submissions_dir: Path
    template_dir: Path
    report_file: Path
    full_pairs_file: Path
    template_file: Path
    extensions: list[str]
    display_threshold: float
    include_python_files: bool
    copydetect_weight: float
    embedding_weight: float
    embedding_model: str


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
        processed_dir=paths.processed_dir,
        output_dir=output_dir,
        submissions_dir=submissions_dir,
        template_dir=template_dir,
        report_file=report_file,
        full_pairs_file=full_pairs_file,
        template_file=template_file,
        extensions=plagiarism.extensions,
        display_threshold=plagiarism.display_threshold,
        include_python_files=plagiarism.include_python_files,
        copydetect_weight=plagiarism.copydetect_weight,
        embedding_weight=plagiarism.embedding_weight,
        embedding_model=plagiarism.embedding_model,
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
            reference_similarity = float(
                detector.similarity_matrix[test_idx, ref_idx, 1]
            )
            if test_similarity < 0 and reference_similarity < 0:
                continue

            token_overlap = int(detector.token_overlap_matrix[test_idx, ref_idx])
            rows.append({
                "test_file": test_file,
                "reference_file": ref_file,
                "test_similarity_pct": test_similarity * 100,
                "reference_similarity_pct": reference_similarity * 100,
                "max_similarity_pct": max(test_similarity, reference_similarity) * 100,
                "token_overlap": token_overlap,
            })

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


def _find_submissions(cfg: PlagiarismConfig) -> list[Path]:
    files = sorted(cfg.raw_dir.rglob("*.ipynb"))
    if cfg.include_python_files:
        files.extend(sorted(cfg.raw_dir.rglob("*.py")))
    return files


def _run_code_plagiarism(
    cfg: PlagiarismConfig,
    assignment_config_path: Path,
    hook_runtime: HookRuntime | None,
) -> dict:
    """Copydetect over code extracted from notebook/python submissions."""
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

    extracted_success = 0
    extracted_errors = 0
    has_template = cfg.template_file.exists()
    if has_template:
        _write_extracted_code(cfg.template_file, cfg.template_dir / "template.py")
    else:
        print(
            f"[plagiarism] template not found ({cfg.template_file}); "
            "running without boilerplate removal"
        )

    for submission_file in _find_submissions(cfg):
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
    print(
        f"[plagiarism] full pair data -> {cfg.full_pairs_file} ({exported_pairs} pairs)"
    )
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


def _embedding_pairs(embedding_path: Path) -> dict[tuple[str, str], float]:
    """Map of (file_a, file_b) -> similarity percent from embedding pair data."""
    if not embedding_path.exists():
        return {}
    payload = json.loads(embedding_path.read_text(encoding="utf-8"))
    pairs: dict[tuple[str, str], float] = {}
    for row in payload.get("pairs", []):
        a = Path(row["test_file"]).name
        b = Path(row["reference_file"]).name
        key = (a, b) if a <= b else (b, a)
        pairs[key] = float(row["max_similarity_pct"])
    return pairs


def _pair_key(file_a: str, file_b: str) -> tuple[str, str]:
    a, b = Path(file_a).name, Path(file_b).name
    return (a, b) if a <= b else (b, a)


def _blend_rows(
    copydetect_rows: list[dict],
    embedding_pairs: dict[tuple[str, str], float],
    copydetect_weight: float,
    embedding_weight: float,
) -> list[dict]:
    blended: list[dict] = []
    for row in copydetect_rows:
        key = _pair_key(row["test_file"], row["reference_file"])
        emb = embedding_pairs.get(key)
        value = row["max_similarity_pct"]
        if emb is not None:
            value = copydetect_weight * value + embedding_weight * emb
        blended.append({
            "test_file": row["test_file"],
            "reference_file": row["reference_file"],
            "test_similarity_pct": value,
            "reference_similarity_pct": value,
            "max_similarity_pct": value,
            "token_overlap": row["token_overlap"],
            "embedding_similarity_pct": emb,
        })
    blended.sort(key=itemgetter("max_similarity_pct"), reverse=True)
    return blended


def _top_pairs(embs: np.ndarray) -> list[tuple[int, int, float]]:
    n = embs.shape[0]
    pairs: list[tuple[int, int, float]] = [
        (i, j, float(embs[i] @ embs[j])) for i in range(n) for j in range(i + 1, n)
    ]
    pairs.sort(key=itemgetter(2), reverse=True)
    return pairs


def _embedding_fresh(out_path: Path, processed_dir: Path) -> bool:
    mds = list(processed_dir.glob("*.md"))
    return bool(mds) and out_path.exists() and out_path.stat().st_mtime >= max(
        f.stat().st_mtime for f in mds
    )


def _run_embedding(cfg: PlagiarismConfig) -> bool:
    """Embed processed/*.md and write all_pairs.embedding.json (skip when fresh)."""
    out_path = cfg.output_dir / "all_pairs.embedding.json"
    if _embedding_fresh(out_path, cfg.processed_dir):
        return True
    if SentenceTransformer is None:
        print(
            "[plagiarism] sentence-transformers unavailable; "
            "copydetect-only (no embedding blend)"
        )
        return False

    items: list[tuple[str, str]] = []
    for f in sorted(cfg.processed_dir.glob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace").strip()
        if len(text) >= MIN_TEXT_CHARS:
            items.append((f.name, text))
    if not items:
        return False

    model = SentenceTransformer(
        cfg.embedding_model,
        trust_remote_code=True,
        model_kwargs={"modality": "text"},
    )
    embs = np.asarray(
        model.encode_document(
            [t for _, t in items], batch_size=16, show_progress_bar=False
        ),
        dtype=np.float32,
    )
    rows = [
        {
            "test_file": items[i][0],
            "reference_file": items[j][0],
            "test_similarity_pct": round(s * 100, 6),
            "reference_similarity_pct": round(s * 100, 6),
            "max_similarity_pct": round(s * 100, 6),
            "token_overlap": 0,
        }
        for i, j, s in _top_pairs(embs)
    ]
    payload = {
        "version": 1,
        "test_file_count": len(items),
        "reference_file_count": len(items),
        "pair_count": len(rows),
        "pairs": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[plagiarism] embedding -> {out_path} ({len(items)} files, {len(rows)} pairs)")
    return True


def _run_text_plagiarism(cfg: PlagiarismConfig) -> dict:
    """Copydetect over processed/*.md, blended with embedding similarity.

    The embedding is 5% auxiliary (user decision 2026-08-28: embedding alone
    had too many false positives on short essays).
    """
    _run_embedding(cfg)

    detector = CopyDetector(
        test_dirs=[str(cfg.processed_dir)],
        extensions=[".md"],
        display_t=cfg.display_threshold,
        out_file=str(cfg.report_file),
        autoopen=False,
        silent=True,
        # copydetect's filter_code drops token.Text (comment stripping, for code);
        # prose markdown fingerprints become empty without this.
        disable_filtering=True,
    )
    detector.run()
    copydetect_path = cfg.output_dir / "all_pairs.copydetect.json"
    _write_full_pair_data(detector, copydetect_path)
    copydetect_rows = json.loads(copydetect_path.read_text(encoding="utf-8"))["pairs"]

    embedding_pairs = _embedding_pairs(cfg.output_dir / "all_pairs.embedding.json")
    rows = _blend_rows(
        copydetect_rows,
        embedding_pairs,
        cfg.copydetect_weight,
        cfg.embedding_weight,
    )
    payload = {
        "version": 1,
        "test_file_count": len(detector.test_files),
        "reference_file_count": len(detector.test_files),
        "pair_count": len(rows),
        "weights": {"copydetect": cfg.copydetect_weight, "embedding": cfg.embedding_weight},
        "pairs": rows,
    }
    (cfg.output_dir / "all_pairs.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )

    print(f"[text-plagiarism] {cfg.output_dir / 'all_pairs.json'} ({len(rows)} pairs)")
    for row in rows[:10]:
        emb = row.get("embedding_similarity_pct")
        emb_s = f" (emb {emb:.1f}%)" if emb is not None else ""
        print(
            f"  {row['max_similarity_pct']:7.2f}%  "
            f"{Path(row['test_file']).name} <-> {Path(row['reference_file']).name}{emb_s}"
        )

    total = len(detector.test_files)
    return {
        "stage": "plagiarism",
        "success": total,
        "errors": 0,
        "total": total,
        "success_rate": 100.0,
    }


def _aggregate_report(
    assignments_root: Path,
    plagiarism: PlagiarismSection,
    output: Path | None,
    *,
    assignment_dirs: list[Path] | None = None,
) -> None:
    """Aggregate over the assignments root, or only the listed dirs.

    ``assignment_dirs`` restricts pair data to the root config's
    [[fetch.assignments]] entries; the whole-root glob remains the fallback.
    """
    pair_files = None
    if assignment_dirs is not None:
        pair_files = [
            p
            for d in assignment_dirs
            for p in (d / "plagiarism").glob("all_pairs.json")
        ]
    payload = _build_payload(
        BuildConfig(
            assignments_root=assignments_root,
            pairs_glob=DEFAULT_PAIRS_GLOB,
            pairwise_alpha=plagiarism.pairwise_alpha,
            individual_alpha=plagiarism.individual_alpha,
            score_floor=plagiarism.score_floor,
            score_cap=plagiarism.score_cap,
            pair_data_files=pair_files,
        )
    )
    report = _to_text(payload)
    if output is None:
        print(report)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report + "\n", encoding="utf-8")
    print(f"[plagiarism] aggregate report -> {output}")


def _run_assignment(config_path: Path) -> dict:
    cfg_model = load_assignment_file(config_path)
    hook_runtime = HookRuntime.from_config(
        cfg_model,
        assignment_config_path=config_path,
    )
    cfg = _load_plagiarism_config(config_path)

    if _find_submissions(cfg):
        return _run_code_plagiarism(cfg, config_path, hook_runtime)
    md_files = list(cfg.processed_dir.glob("*.md")) if cfg.processed_dir.exists() else []
    if md_files:
        return _run_text_plagiarism(cfg)
    print(
        f"[plagiarism] nothing to compare in {config_path.parent} "
        "(no raw .ipynb/.py, no processed/*.md)"
    )
    return {
        "stage": "plagiarism",
        "success": 0,
        "errors": 0,
        "total": 0,
        "success_rate": 0,
    }


def _combine_summaries(summaries: list[dict]) -> dict:
    total = sum(s["total"] for s in summaries)
    success = sum(s["success"] for s in summaries)
    errors = sum(s["errors"] for s in summaries)
    return {
        "stage": "plagiarism",
        "success": success,
        "errors": errors,
        "total": total,
        "success_rate": (success / total * 100) if total else 0,
    }


def detect_plagiarism(
    config_path: Path,
    *,
    aggregate: bool = False,
    output: Path | None = None,
) -> dict | None:
    """Run plagiarism for one assignment, or for all under the root config.

    ``--config data/config.toml`` runs every assignment below it;
    ``--config data/X/config.toml`` runs X only. ``--aggregate`` appends
    the cross-assignment z-score report over the assignments root.
    """
    resolved = config_path.resolve()
    is_root = is_root_config(resolved)

    # Root config: run the [[fetch.assignments]] list when present (the
    # source of truth for the course), else every assignment dir below.
    listed: list[Path] | None = None
    if is_root:
        toml = tomllib.loads(resolved.read_text(encoding="utf-8"))
        root_fetch = FetchSection.model_validate(toml.get("fetch", {}))
        if root_fetch.assignments:
            listed = [
                (resolved.parent / entry.out).parent for entry in root_fetch.assignments
            ]

    if is_root:
        summaries = []
        for assignment_cfg in sorted(
            [d / "config.toml" for d in listed]
            if listed is not None
            else resolved.parent.glob("*/config.toml")
        ):
            try:
                summaries.append(_run_assignment(assignment_cfg))
            except (ValueError, FileNotFoundError) as exc:
                print(f"[plagiarism] skipped {assignment_cfg.parent.name}: {exc}")
        if aggregate:
            _aggregate_report(
                resolved.parent,
                _root_plagiarism_section(resolved),
                output,
                assignment_dirs=listed,
            )
        return _combine_summaries(summaries)

    summary = _run_assignment(resolved)
    if aggregate:
        _aggregate_report(
            resolved.parent.parent, load_assignment_file(resolved).plagiarism, output
        )
    return summary


def _root_plagiarism_section(root_config: Path) -> PlagiarismSection:
    toml = tomllib.loads(root_config.read_text(encoding="utf-8"))
    return PlagiarismSection.model_validate(toml.get("plagiarism", {}))


def main() -> None:
    args = parse_cli_args(PlagiarismCliOptions)
    detect_plagiarism(args.config, aggregate=args.aggregate, output=args.output)


if __name__ == "__main__":
    main()

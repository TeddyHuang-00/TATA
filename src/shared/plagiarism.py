from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from operator import itemgetter
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from copydetect import CopyDetector

from .assignment_config import (
    FetchSection,
    PlagiarismSection,
    ProcessingSection,
    ensure_assignment_dirs,
    is_root_config,
    load_assignment_file,
    load_root_section,
    resolve_assignment_paths,
    root_plagiarism_section,
)
from .caching import (
    cache_file,
    content_hash,
    file_digest,
    load_cache_file,
    save_cache_file,
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
    build_payload,
    to_text,
)

# Bumped when the embedding similarity definition changes (v2: normalized
# cosine, was raw dot product) so stale caches with the old scale are not
# blended into fresh runs.
EMBEDDING_CACHE_VERSION = 2


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
    assignment_dir: Path
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
    embedding_enabled: bool = False


def _safe_output_name(file_path: Path, base_dir: Path, taken: set[str]) -> str:
    """Unique extracted-code name for a raw submission.

    Distinct raw paths must never map to the same output file: ``raw/a/b.ipynb``
    and ``raw/a__b.ipynb`` (and ``x.ipynb`` + ``x.py``) all cleaned to
    ``a__b.py``/``x.py`` before, and the later extraction overwrote the
    earlier student's code — a different submission was then compared under
    the first student's name. First in sorted order keeps the base name, the
    rest get ``_2``, ``_3``, ... (mirrors the pipeline stem disambiguation).
    """
    rel = file_path.relative_to(base_dir)
    stem = rel.with_suffix("").as_posix().replace("/", "__")
    name = f"{stem}.py"
    if name not in taken:
        taken.add(name)
        return name
    suffix = 2
    while f"{stem}_{suffix}.py" in taken:
        suffix += 1
    name = f"{stem}_{suffix}.py"
    taken.add(name)
    return name


_UID_PREFIX = re.compile(r"^(\d+)")


def _student_uid(file_name: str) -> str | None:
    """Leading numeric uid of a submission file name (fetch convention:
    ``<uid>``, ``<uid>_LATE_i``, ``<uid>__<member>``); None when absent."""
    match = _UID_PREFIX.match(Path(file_name).stem)
    return match.group(1) if match else None


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically (same-dir temp + replace) so a crash or cancel
    mid-write never leaves a truncated JSON for the aggregate/TUI to parse."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        Path(tmp_name).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
        raise


def _extract_notebook_code(input_path: Path) -> str:
    import nbformat  # ruff: ignore[import-outside-top-level]

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


def _unset_as_default(value: str, default: str) -> str:
    """Empty/whitespace counts as unset -> the field's default value.

    Joining ``""`` onto a base path resolves to the base dir itself, so a
    literal ``template_file = ""`` made the assignment directory the template
    and the stage died with "Unsupported input type for extraction"; the same
    normalisation applies to every path-valued ``[plagiarism]`` key. A
    non-empty value is returned stripped of surrounding whitespace.
    """
    return value.strip() or default


def _load_plagiarism_config(config_path: Path) -> PlagiarismConfig:
    cfg = load_assignment_file(config_path)
    paths = resolve_assignment_paths(cfg, config_path.parent)
    ensure_assignment_dirs(paths)

    plagiarism = cfg.plagiarism
    defaults = PlagiarismSection()
    output_dir = (
        config_path.parent
        / _unset_as_default(plagiarism.output_dir, defaults.output_dir)
    ).resolve()
    submissions_dir = (
        output_dir
        / _unset_as_default(plagiarism.submissions_subdir, defaults.submissions_subdir)
    ).resolve()
    template_dir = (
        output_dir
        / _unset_as_default(plagiarism.template_subdir, defaults.template_subdir)
    ).resolve()
    report_file = (
        output_dir / _unset_as_default(plagiarism.report_file, defaults.report_file)
    ).resolve()
    full_pairs_file = (
        output_dir
        / _unset_as_default(plagiarism.full_pairs_file, defaults.full_pairs_file)
    ).resolve()
    template_file = (
        config_path.parent
        / _unset_as_default(plagiarism.template_file, defaults.template_file)
    ).resolve()

    return PlagiarismConfig(
        assignment_dir=config_path.parent.resolve(),
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
        embedding_enabled=plagiarism.embedding_enabled,
    )


def _write_full_pair_data(detector: CopyDetector, output_path: Path) -> int:
    """Export all compared student pairs from copydetect matrices.

    Pairs whose two files carry the same leading numeric uid are one
    student's own submissions (fetch variants ``<uid>.py`` +
    ``<uid>_LATE_0.py``, multi-file folders) — they are never plagiarism
    and are excluded here so every consumer (scan flags, TUI ranking,
    aggregate) agrees.

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
        _atomic_write_text(output_path, json.dumps(payload, indent=2))
        return 0

    seen_pairs: set[tuple[str, str]] = set()
    rows: list[dict] = []

    for test_idx, test_file in enumerate(detector.test_files):
        for ref_idx, ref_file in enumerate(detector.ref_files):
            if test_file == ref_file:
                continue
            test_uid = _student_uid(test_file)
            if test_uid is not None and test_uid == _student_uid(ref_file):
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
    _atomic_write_text(output_path, json.dumps(payload, indent=2))
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
    *,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Copydetect over code extracted from notebook/python submissions.

    A set ``cancel_event`` stops the extraction loop at the next submission
    and, when observed after the loop, skips the pair pass entirely (no
    detector run, no report/pair-data write — a truncated report must not
    overwrite the previous complete one); ``detector.run()`` itself is one
    library call and cannot be interrupted (the honest ceiling).
    """
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
    # is_file + a supported suffix: a directory or a non-.ipynb/.py file at
    # the template location cannot be extracted — degrade to the no-template
    # branch instead of letting the extraction raise.
    template_file = cfg.template_file
    has_template = template_file.is_file() and template_file.suffix.lower() in {
        ".ipynb",
        ".py",
    }
    if has_template:
        _write_extracted_code(template_file, cfg.template_dir / "template.py")
    elif template_file.is_file():
        print(
            f"[plagiarism] template type not supported ({template_file}); "
            "running without boilerplate removal"
        )
    else:
        print(
            f"[plagiarism] template not found ({template_file}); "
            "running without boilerplate removal"
        )

    taken_names: set[str] = set()
    for submission_file in _find_submissions(cfg):
        if cancel_event is not None and cancel_event.is_set():
            print("[cancelled] plagiarism stopped — remaining submissions skipped")
            break
        try:
            output_name = _safe_output_name(submission_file, cfg.raw_dir, taken_names)
            _write_extracted_code(submission_file, cfg.submissions_dir / output_name)
            extracted_success += 1
        except Exception as exc:
            print(f"[error] Failed to extract {submission_file.name}: {exc}")
            extracted_errors += 1

    if cancel_event is not None and cancel_event.is_set():
        # The extraction loop was stopped early: the pair pass would write a
        # truncated report over the previous complete one — don't start it.
        print("[cancelled] plagiarism stopped before the pair pass")
        return {
            "stage": "plagiarism",
            "success": 0,
            "errors": 0,
            "total": 0,
            "success_rate": 0,
        }

    from copydetect import CopyDetector  # ruff: ignore[import-outside-top-level]

    detector = CopyDetector(
        test_dirs=[str(cfg.submissions_dir)],
        boilerplate_dirs=[str(cfg.template_dir)],
        extensions=cfg.extensions,
        display_t=cfg.display_threshold,
        out_file=str(cfg.report_file),
        autoopen=False,
        silent=True,
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
                "cancelled": cancel_event is not None and cancel_event.is_set(),
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


def _embedding_pairs(cache_path: Path) -> dict[tuple[str, str], float]:
    """Map of (file_a, file_b) -> similarity percent from the embedding cache.

    Tolerant read: missing/broken/wrong-envelope cache reads as ``{}``.
    """
    payload = load_cache_file(cache_path)
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
    """All (i, j, cosine) pairs, descending by similarity.

    Vectors are L2-normalized first (zero-norm guarded): raw ``a @ b`` made
    the score norm-dominated and produced impossible values (real data hit
    100.12%, which is not a cosine). Clamped to [-1, 1] against float drift.
    """
    n = embs.shape[0]
    norms = np.linalg.norm(embs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = embs / norms
    pairs: list[tuple[int, int, float]] = [
        (i, j, float(np.clip(unit[i] @ unit[j], -1.0, 1.0)))
        for i in range(n)
        for j in range(i + 1, n)
    ]
    pairs.sort(key=itemgetter(2), reverse=True)
    return pairs


def embedding_input_hash(processed_dir: Path, model: str) -> str:
    """Input hash for the embedding cache: sorted processed/*.md digests +
    model + cache version (bumped when the similarity definition changes).

    Public so the cache-migration tool can recompute stored hashes with the
    production rule instead of reimplementing it.
    """
    md_digests = [
        file_digest(md).encode("utf-8") for md in sorted(processed_dir.glob("*.md"))
    ]
    return content_hash([
        *md_digests,
        model.encode("utf-8"),
        str(EMBEDDING_CACHE_VERSION).encode("utf-8"),
    ])


def _run_embedding(cfg: PlagiarismConfig) -> bool:
    """Embed processed/*.md into <assignment>/.cache/embedding.json (skip on hash match)."""
    cache_path = cache_file(cfg.assignment_dir, "embedding")
    input_hash = embedding_input_hash(cfg.processed_dir, cfg.embedding_model)
    if load_cache_file(cache_path).get("hash") == input_hash:
        return True
    try:
        from sentence_transformers import SentenceTransformer  # ruff: ignore[import-outside-top-level]
    except ImportError:
        print(
            "[plagiarism] sentence-transformers unavailable; "
            "copydetect-only (no embedding blend)"
        )
        return False

    # Every processed md is embedded (no minimum-length gate): the cache must
    # cover all pairs of the copydetect input, or rows for skipped files fall
    # back to pure copydetect and two score scales mix in one assignment.
    items: list[tuple[str, str]] = [
        (
            f.name,
            f.read_text(encoding="utf-8", errors="replace").strip(),
        )
        for f in sorted(cfg.processed_dir.glob("*.md"))
    ]
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
    save_cache_file(cache_path, {"hash": input_hash, "pairs": rows})
    print(
        f"[plagiarism] embedding -> {cache_path} ({len(items)} files, {len(rows)} pairs)"
    )
    return True


def _run_text_plagiarism(
    cfg: PlagiarismConfig,
    *,
    md_files: list[Path] | None = None,
    merge_code_pairs: bool = False,
) -> dict:
    """Copydetect over processed/*.md, optionally blended with embedding similarity.

    The embedding blend is opt-in (``[plagiarism] embedding_enabled``, default
    false: pure copydetect — no model, no embedding cache). When on it is 5%
    auxiliary (user decision 2026-08-28: embedding alone had too many false
    positives on short essays). The blend only applies when the embedding
    cache is fresh for this input; a failed/unavailable model falls back to
    pure copydetect for the whole run — a stale cache from a previous run
    must never mix old scores into fresh numbers while the payload claims
    the configured weights.

    ``md_files`` restricts the comparison (mixed assignment: only the
    students the code path does not cover). Copydetect takes directories, so
    the subset is staged into ``output_dir/text_submissions/`` and the HTML
    report goes to ``report.text.html`` — it must not clobber the code
    path's ``report.html``. ``merge_code_pairs`` prepends the code path's
    pairs (written to ``full_pairs_file`` earlier in the same run) to the
    shared ``all_pairs.json``.
    """
    embedding_ok = False
    if cfg.embedding_enabled:
        embedding_ok = _run_embedding(cfg)
        if not embedding_ok:
            print(
                "[plagiarism] embedding unavailable; this run is "
                "copydetect-only (no blend)"
            )
    embedding_pairs = (
        _embedding_pairs(cache_file(cfg.assignment_dir, "embedding"))
        if embedding_ok
        else {}
    )

    from copydetect import CopyDetector  # ruff: ignore[import-outside-top-level]

    if md_files is None:
        test_dir = cfg.processed_dir
        report_file = cfg.report_file
    else:
        test_dir = cfg.output_dir / "text_submissions"
        if test_dir.exists():
            shutil.rmtree(test_dir)
        test_dir.mkdir(parents=True, exist_ok=True)
        for md in md_files:
            shutil.copy2(md, test_dir / md.name)
        report_file = cfg.output_dir / "report.text.html"

    detector = CopyDetector(
        test_dirs=[str(test_dir)],
        extensions=[".md"],
        display_t=cfg.display_threshold,
        out_file=str(report_file),
        autoopen=False,
        silent=True,
        # copydetect's filter_code drops token.Text (comment stripping, for code);
        # prose markdown fingerprints become empty without this.
        disable_filtering=True,
    )
    detector.run()
    detector.generate_html_report()
    copydetect_path = cfg.output_dir / "all_pairs.copydetect.json"
    _write_full_pair_data(detector, copydetect_path)
    copydetect_rows = json.loads(copydetect_path.read_text(encoding="utf-8"))["pairs"]

    rows = _blend_rows(
        copydetect_rows,
        embedding_pairs,
        cfg.copydetect_weight,
        cfg.embedding_weight,
    )

    code_rows: list[dict] = []
    code_count = 0
    if merge_code_pairs and cfg.full_pairs_file.exists():
        try:
            old = json.loads(cfg.full_pairs_file.read_text(encoding="utf-8"))
            if isinstance(old.get("pairs"), list):
                code_rows = old["pairs"]
                code_count = int(old.get("test_file_count", 0))
        except (json.JSONDecodeError, OSError):
            # A corrupt code payload must not block this run; the code
            # report.html and extracted submissions are still there.
            print(
                f"[plagiarism] warning: unreadable {cfg.full_pairs_file.name}; "
                "all_pairs.json holds the text pairs only"
            )

    payload = {
        "version": 1,
        "test_file_count": code_count + len(detector.test_files),
        "reference_file_count": code_count + len(detector.test_files),
        "pair_count": len(code_rows) + len(rows),
        # Weights of the blend that actually ran: only claim the configured
        # split when the embedding cache was fresh and applied; otherwise
        # every score is pure copydetect.
        "weights": (
            {
                "copydetect": cfg.copydetect_weight,
                "embedding": cfg.embedding_weight,
            }
            if embedding_ok
            else {"copydetect": 1.0, "embedding": 0.0}
        ),
        "pairs": [*code_rows, *rows],
    }
    _atomic_write_text(cfg.full_pairs_file, json.dumps(payload, indent=2))

    print(f"[text-plagiarism] {cfg.full_pairs_file} ({len(rows)} pairs)")
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
    quiet: bool = False,
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
    payload = build_payload(
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
    report = to_text(payload)
    if output is None:
        if not quiet:
            print(report)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report + "\n", encoding="utf-8")
    print(f"[plagiarism] aggregate report -> {output}")


def _non_code_md_files(
    cfg: PlagiarismConfig, processing: ProcessingSection
) -> list[Path]:
    """processed/*.md whose raw item has no .ipynb/.py file — the students
    the code path does not cover (mixed assignments).

    The mapping is the pipeline's own (same ``_output_stem`` call with the
    same config flags), so a code item's md is recognized even when its name
    was cleaned (``Alice_111_222_lab.ipynb`` -> ``Alice.md``) or disambiguated.
    """
    if not cfg.processed_dir.exists():
        return []
    from .pipeline import (  # ruff: ignore[import-outside-top-level]
        _item_files,
        _iter_raw_items,
        _output_stem,
    )

    items = _iter_raw_items(cfg.raw_dir) if cfg.raw_dir.exists() else []
    item_files_by = {item: _item_files(item, None) for item in items}
    code_stems = {
        _output_stem(
            item,
            item_files_by,
            processing.strip_canvas_suffix,
            processing.clean_filenames,
        )
        for item, files in item_files_by.items()
        if any(f.suffix.lower() in {".ipynb", ".py"} for f, _ in files)
    }
    return [
        md for md in sorted(cfg.processed_dir.glob("*.md")) if md.stem not in code_stems
    ]


def _run_assignment(
    config_path: Path, *, cancel_event: threading.Event | None = None
) -> dict:
    cfg_model = load_assignment_file(config_path)
    hook_runtime = HookRuntime.from_config(
        cfg_model,
        assignment_config_path=config_path,
    )
    cfg = _load_plagiarism_config(config_path)
    # Both strategies write under output_dir and copydetect rejects a missing
    # out_file parent: ensure it once before dispatch (text path had no mkdir).
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    code_files = _find_submissions(cfg)
    summaries: list[dict] = []
    if code_files:
        summaries.append(
            _run_code_plagiarism(
                cfg, config_path, hook_runtime, cancel_event=cancel_event
            )
        )
        if cancel_event is not None and cancel_event.is_set():
            return summaries[0]

    # Mixed assignments: the code path only covers code submissions; the
    # remaining students (their processed md) still need comparing. Both
    # pair sets land in all_pairs.json (code pairs first) — one student is
    # never compared on both scales, and no one is silently dropped.
    text_md = _non_code_md_files(cfg, cfg_model.processing)
    if text_md:
        summaries.append(
            _run_text_plagiarism(
                cfg,
                md_files=None if not code_files else text_md,
                merge_code_pairs=bool(code_files),
            )
        )
    if not summaries:
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
    return _combine_summaries(summaries)


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
    quiet: bool = False,
    cancel_event: threading.Event | None = None,
) -> dict | None:
    """Run plagiarism for one assignment, or for all under the root config.

    ``--config data/config.toml`` runs every assignment below it;
    ``--config data/X/config.toml`` runs X only. ``--aggregate`` appends
    the cross-assignment z-score report over the assignments root.
    ``quiet`` suppresses the stdout text report (TUI jobs; the pane reads
    aggregate.json instead).
    ``cancel_event`` (TUI jobs) stops the per-assignment loop at the next
    assignment boundary and skips the aggregate pass when set — a cancelled
    run never writes a truncated report over a complete one.
    """
    resolved = config_path.resolve()
    is_root = is_root_config(resolved)

    # Root config: run the [[fetch.assignments]] list when present (the
    # source of truth for the course), else every assignment dir below.
    listed: list[Path] | None = None
    if is_root:
        root_fetch = (
            load_root_section(resolved, "fetch", FetchSection) or FetchSection()
        )
        if root_fetch.assignments:
            listed = [
                resolved.parent / str(entry.id) for entry in root_fetch.assignments
            ]

    if is_root:
        summaries = []
        for assignment_cfg in sorted(
            [d / "config.toml" for d in listed]
            if listed is not None
            else resolved.parent.glob("*/config.toml")
        ):
            if cancel_event is not None and cancel_event.is_set():
                print("[cancelled] plagiarism stopped — remaining assignments skipped")
                break
            try:
                summaries.append(
                    _run_assignment(assignment_cfg, cancel_event=cancel_event)
                )
            except (ValueError, FileNotFoundError) as exc:
                print(f"[plagiarism] skipped {assignment_cfg.parent.name}: {exc}")
        if aggregate:
            if cancel_event is not None and cancel_event.is_set():
                # Truncated pair set: skip the aggregate pass (would overwrite
                # the last complete report with a partial ranking).
                print("[cancelled] plagiarism stopped before the aggregate pass")
            else:
                _aggregate_report(
                    resolved.parent,
                    root_plagiarism_section(resolved),
                    output,
                    assignment_dirs=listed,
                    quiet=quiet,
                )
        return _combine_summaries(summaries)

    summary = _run_assignment(resolved, cancel_event=cancel_event)
    if aggregate:
        _aggregate_report(
            resolved.parent.parent,
            load_assignment_file(resolved).plagiarism,
            output,
            quiet=quiet,
        )
    return summary


def main() -> None:
    args = parse_cli_args(PlagiarismCliOptions)
    detect_plagiarism(args.config, aggregate=args.aggregate, output=args.output)


if __name__ == "__main__":
    main()

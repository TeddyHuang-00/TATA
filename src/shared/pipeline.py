"""Preprocess-stage orchestration: pending computation, format routing,
screenshots and the main assignment loop. Conversion helpers live in
:mod:`src.shared.convert`, rendering in :mod:`src.shared.screenshots`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src import REPO_ROOT

from .assignment_config import (
    InputFormat,
    ProcessingSection,
    ensure_assignment_dirs,
    load_assignment_file,
    resolve_assignment_paths,
)
from .caching import (
    cache_file,
    content_hash,
    file_hash,
    load_cache_file,
    save_cache_file,
)
from .cli_options import ConfigFileCliOptions, parse_cli_args
from .convert import (
    SUPPORTED_INPUT_FORMATS,
    _clean_filename,
    _convert_html_tables_to_markdown,
    _convert_markdown,
    _format_for_suffix,
    _normalize_dtype_label_html,
    _remove_base64_images,
    _strip_canvas_suffix,
    _strip_colab_dataframe_widgets,
    convert_docx_to_markdown,
    convert_html_to_markdown,
    convert_ipynb_to_markdown,
    convert_pdf_to_markdown,
)
from .hooks_runtime import HookRuntime
from .screenshots import (
    _cleanup_stem_shots,
    _image_to_pdf,
    _render_screenshots,
    _render_stem_screenshots,
)


class ProcessingCliOptions(ConfigFileCliOptions):
    pass


def _postprocess_markdown(  # ruff: ignore[too-many-arguments]
    content: str,
    *,
    source_format: InputFormat,
    remove_base64: bool,
    strip_html_callouts: bool,
    strip_html_div_tags: bool,
    strip_html_escaped_backslashes: bool,
    strip_html_style_blocks: bool,
    convert_html_tables_to_markdown: bool,
    strip_colab_dataframe_widgets: bool,
    strip_html_script_tags: bool,
    strip_html_button_tags: bool,
    strip_html_svg_tags: bool,
    normalize_dtype_label_html: bool,
) -> str:
    processed = content

    if remove_base64:
        processed = _remove_base64_images(processed)

    if source_format in {"html", "ipynb"}:
        if strip_html_style_blocks:
            processed = re.sub(
                r"<style\b[^>]*>.*?</style>",
                "",
                processed,
                flags=re.IGNORECASE | re.DOTALL,
            )
        if convert_html_tables_to_markdown:
            processed = _convert_html_tables_to_markdown(processed)
        if strip_colab_dataframe_widgets:
            processed = _strip_colab_dataframe_widgets(processed)
        if strip_html_script_tags:
            processed = re.sub(
                r"<script\b[^>]*>.*?</script>",
                "",
                processed,
                flags=re.IGNORECASE | re.DOTALL,
            )
        if strip_html_button_tags:
            processed = re.sub(
                r"<button\b[^>]*>.*?</button>",
                "",
                processed,
                flags=re.IGNORECASE | re.DOTALL,
            )
        if strip_html_svg_tags:
            processed = re.sub(
                r"<svg\b[^>]*>.*?</svg>",
                "",
                processed,
                flags=re.IGNORECASE | re.DOTALL,
            )
        if strip_html_callouts:
            processed = re.sub(r"(?m)^:::.+$\n?", "", processed)
        if strip_html_div_tags:
            processed = re.sub(r"</?div[^>]*>", "", processed)
        if strip_html_escaped_backslashes:
            processed = processed.replace("\\\\", " ")
        if normalize_dtype_label_html:
            processed = _normalize_dtype_label_html(processed)

        # Compact excessive empty lines after stripping bulky HTML blocks.
        processed = re.sub(r"\n{3,}", "\n\n", processed)

    return processed


def _process_single_file(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    input_file: Path,
    output_file: Path,
    input_format: InputFormat,
    remove_base64: bool,
    strip_html_callouts: bool,
    strip_html_div_tags: bool,
    strip_html_escaped_backslashes: bool,
    strip_html_style_blocks: bool,
    convert_html_tables_to_markdown: bool,
    strip_colab_dataframe_widgets: bool,
    strip_html_script_tags: bool,
    strip_html_button_tags: bool,
    strip_html_svg_tags: bool,
    normalize_dtype_label_html: bool,
    remove_nbconvert_assets: bool,
    nbconvert_template: str | None,
    nbconvert_template_dir: Path | None,
) -> None:
    """Process a single input file to markdown output."""
    # Convert based on format
    if input_format == "ipynb":
        convert_ipynb_to_markdown(
            input_file,
            output_file,
            template_name=nbconvert_template,
            template_dir=nbconvert_template_dir,
        )
    elif input_format == "html":
        convert_html_to_markdown(input_file, output_file)
    elif input_format == "markdown":
        _convert_markdown(input_file, output_file)
    elif input_format == "docx":
        convert_docx_to_markdown(input_file, output_file)
    elif input_format == "pdf":
        convert_pdf_to_markdown(input_file, output_file)
    elif input_format == "image":
        fd, pdf_name = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        tmp_pdf = Path(pdf_name)
        try:
            _image_to_pdf(input_file, tmp_pdf)
            convert_pdf_to_markdown(tmp_pdf, output_file)
        finally:
            tmp_pdf.unlink(missing_ok=True)
    else:
        msg = f"Unsupported input format: {input_format}"
        raise ValueError(msg)

    # Post-processing
    if output_file.exists():
        content = output_file.read_text(encoding="utf-8")
        content = _postprocess_markdown(
            content,
            source_format=input_format,
            remove_base64=remove_base64,
            strip_html_callouts=strip_html_callouts,
            strip_html_div_tags=strip_html_div_tags,
            strip_html_escaped_backslashes=strip_html_escaped_backslashes,
            strip_html_style_blocks=strip_html_style_blocks,
            convert_html_tables_to_markdown=convert_html_tables_to_markdown,
            strip_colab_dataframe_widgets=strip_colab_dataframe_widgets,
            strip_html_script_tags=strip_html_script_tags,
            strip_html_button_tags=strip_html_button_tags,
            strip_html_svg_tags=strip_html_svg_tags,
            normalize_dtype_label_html=normalize_dtype_label_html,
        )

        output_file.write_text(content, encoding="utf-8")

    if input_format == "ipynb" and remove_nbconvert_assets:
        assets_dir = output_file.parent / f"{output_file.stem}_files"
        if assets_dir.exists() and assets_dir.is_dir():
            shutil.rmtree(assets_dir)


def _iter_raw_items(raw_dir: Path) -> list[Path]:
    """Top-level raw entries: files and dirs, dot-entries skipped. A
    top-level file whose base uid (stem with any _N/_LATE_N suffix stripped)
    names a top-level dir is a stale flat leftover of a folderized student
    (mixed legacy layout): skipped so the student isn't double-processed."""
    entries = sorted(
        p
        for p in raw_dir.iterdir()
        if not p.name.startswith(".") and (p.is_file() or p.is_dir())
    )
    dirs = {p.name for p in entries if p.is_dir()}
    return [
        p
        for p in entries
        if not (p.is_file() and re.sub(r"_(?:LATE_)?\d+$", "", p.stem) in dirs)
    ]


def _screenshots_missing(shots_dir: Path, output_stem: str) -> bool:
    """True when no page (``_pN``) or embedded (``_iN``) screenshots exist
    for ``output_stem`` — a cache hit re-renders in that case."""
    return not (
        next(shots_dir.glob(f"{output_stem}_p*.png"), None)
        or next(shots_dir.glob(f"{output_stem}_i*.png"), None)
    )


def _cached(cache: dict, stem: str, item_hash: str, output_file: Path) -> bool:
    """True when the cache entry for ``stem`` matches and the output exists.

    The envelope fmt check lives in ``load_cache_file``; an entry is valid on
    hash match alone (plus the output existing).
    """
    entry = cache.get(stem)
    return bool(
        isinstance(entry, dict)
        and entry.get("hash") == item_hash
        and output_file.is_file()
    )


def _submission_stamp(cache: dict[str, str], raw_file: Path) -> str:
    """Stamp for a raw file: the fetch cache entry when known, else the
    file's mtime, else '' — the submitted header part is omitted."""
    stamp = cache.get(raw_file.name)
    if stamp is not None:
        return stamp
    try:
        modified = raw_file.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(modified, tz=UTC).isoformat(timespec="seconds")


def _normalize_input_formats(
    input_format_config: InputFormat | list[InputFormat] | None,
) -> list[InputFormat] | None:
    if input_format_config is None:
        return None

    if isinstance(input_format_config, list):
        deduped: list[InputFormat] = []
        seen: set[str] = set()
        for fmt in input_format_config:
            if fmt not in seen:
                deduped.append(fmt)
                seen.add(fmt)
        return deduped

    return [input_format_config]


def _item_files(
    item: Path, configured_formats: list[InputFormat] | None
) -> list[tuple[Path, InputFormat]]:
    """Supported files of one raw item: a top-level file itself, or the
    files inside a folder (sorted by name), filtered by the configured
    formats when set. Unsupported files inside a folder are logged as
    skips instead of being dropped silently."""
    files = (
        [item]
        if item.is_file()
        else sorted(
            (p for p in item.iterdir() if p.is_file()),
            key=lambda p: (
                # unsuffixed-uid member (the body: <uid>.html) first,
                # then _N/_LATE_N members; stable by name within a group.
                0 if re.sub(r"_(?:LATE_)?\d+$", "", p.stem) == p.stem else 1,
                p.name,
            ),
        )
    )
    found: list[tuple[Path, InputFormat]] = []
    for f in files:
        fmt = _format_for_suffix(f.suffix)
        if fmt is None:
            if item.is_dir():
                print(f"[skip] {f.name} (unsupported format)")
            continue
        if configured_formats is not None and fmt not in configured_formats:
            continue
        found.append((f, fmt))
    return found


def _output_stem(
    item: Path,
    item_files_by: dict[Path, list[tuple[Path, InputFormat]]],
    strip_canvas_suffix: bool,
    clean_filenames: bool,
) -> str:
    """Output md stem of a raw item (file: cleaned filename; dir: folder name)."""
    if item.is_dir():
        return item.name
    output_name = item_files_by[item][0][0].name
    if strip_canvas_suffix:
        output_name = _strip_canvas_suffix(output_name)
    if clean_filenames:
        output_name = _clean_filename(output_name)
    return Path(output_name).stem


def _item_hash_and_src(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    item: Path,
    item_files_by: dict[Path, list[tuple[Path, InputFormat]]],
    raw_dir: Path,
    fetch_cache: dict[str, str],
    cfg_payload: bytes,
    hook_parts: list[bytes],
) -> tuple[list[str], str]:
    """(sorted raw relpaths, content hash) for one raw item."""
    files = item_files_by[item]
    rels = sorted(
        f"{item.name}/{f.name}" if item.is_dir() else f.name for f, _ in files
    )
    parts: list[bytes] = [file_hash(raw_dir, rels).encode("utf-8")]
    if item.is_dir():
        parts.append(
            json.dumps(fetch_cache, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        )
    parts.append(cfg_payload)
    parts.extend(hook_parts)
    return rels, content_hash(parts)


def _preprocess_pending(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    items: list[Path],
    item_files_by: dict[Path, list[tuple[Path, InputFormat]]],
    raw_dir: Path,
    processed_dir: Path,
    cache: dict,
    fetch_cache: dict[str, str],
    cfg_payload: bytes,
    hook_parts: list[bytes],
    *,
    strip_canvas_suffix: bool,
    clean_filenames: bool,
) -> dict[Path, tuple[list[str], str]]:
    """Raw item -> (src relpaths, content hash) that ``preprocess_assignment``
    would reconvert under the preprocess cache rule.

    Single source of the rule shared by ``preprocess_assignment`` and the
    TUI display: an item is pending unless ``<assignment>/.cache/preprocess.json``
    (envelope-checked by ``load_cache_file``) holds a matching hash entry AND
    its output md exists. Hash covers the raw file contents, fetch stamps for
    folders, the [processing] config, template selection and the hook scripts;
    any change reconverts.
    """
    pending: dict[Path, tuple[list[str], str]] = {}
    for item in items:
        if not item_files_by[item]:
            continue
        stem = _output_stem(item, item_files_by, strip_canvas_suffix, clean_filenames)
        output_file = processed_dir / f"{stem}.md"
        src, item_hash = _item_hash_and_src(
            item, item_files_by, raw_dir, fetch_cache, cfg_payload, hook_parts
        )
        if not _cached(cache, stem, item_hash, output_file):
            pending[item] = (src, item_hash)
    return pending


def _resolve_template_base(
    config_path: Path, processing: ProcessingSection
) -> tuple[str | None, Path | None]:
    """(nbconvert_template, template_dir_path) per [processing] settings."""
    nbconvert_template = processing.nbconvert_template
    default_template_dir = REPO_ROOT / "templates"
    if processing.nbconvert_template_dir is not None:
        template_dir_path = (
            config_path.parent / processing.nbconvert_template_dir
        ).resolve()
    elif default_template_dir.exists() and (default_template_dir / "mdoutput").exists():
        template_dir_path = default_template_dir.resolve()
        if nbconvert_template is None:
            nbconvert_template = "mdoutput"
    else:
        template_dir_path = None
    return nbconvert_template, template_dir_path


@dataclass(frozen=True)
class _PreprocessRuleInputs:
    """Everything the preprocess cache rule needs besides the two cache dicts."""

    items: list[Path]
    item_files_by: dict[Path, list[tuple[Path, InputFormat]]]
    raw_dir: Path
    processed_dir: Path
    cfg_payload: bytes
    hook_parts: list[bytes]
    strip_canvas_suffix: bool
    clean_filenames: bool


def _preprocess_rule_inputs(config_path: Path) -> _PreprocessRuleInputs:
    cfg = load_assignment_file(config_path)
    processing = cfg.processing
    paths = resolve_assignment_paths(cfg, config_path.parent)
    configured_formats = _normalize_input_formats(processing.input_format)
    items = _iter_raw_items(paths.raw_dir)
    item_files_by = {item: _item_files(item, configured_formats) for item in items}
    nbconvert_template, template_dir_path = _resolve_template_base(
        config_path, processing
    )
    cfg_payload = json.dumps(
        {
            "processing": processing.model_dump_json(),
            "template_name": nbconvert_template,
            "template_dir": (
                str(template_dir_path) if template_dir_path is not None else None
            ),
        },
        sort_keys=True,
    ).encode("utf-8")
    hook_runtime = HookRuntime.from_config(cfg, assignment_config_path=config_path)
    hook_parts: list[bytes] = []
    if hook_runtime is not None:
        hook_parts = [
            script.read_bytes()
            for script_paths in hook_runtime.mounts.values()
            for script in script_paths
        ]
    return _PreprocessRuleInputs(
        items=items,
        item_files_by=item_files_by,
        raw_dir=paths.raw_dir,
        processed_dir=paths.processed_dir,
        cfg_payload=cfg_payload,
        hook_parts=hook_parts,
        strip_canvas_suffix=processing.strip_canvas_suffix,
        clean_filenames=processing.clean_filenames,
    )


def pending_preprocess_items(config_path: Path) -> list[Path]:
    """Raw items ``preprocess_assignment`` would reconvert right now (cache rule).

    Same rule the run applies (``_preprocess_pending``) — NOT raw-vs-processed
    file counts: raw content can change while the count stays the same, and
    the run reconverts on the hash mismatch.
    """
    rule = _preprocess_rule_inputs(config_path)
    fetch_cache: dict[str, str] = load_cache_file(
        cache_file(rule.raw_dir.parent, "fetch")
    )
    cache = load_cache_file(cache_file(rule.raw_dir.parent, "preprocess"))
    return list(
        _preprocess_pending(
            rule.items,
            rule.item_files_by,
            rule.raw_dir,
            rule.processed_dir,
            cache,
            fetch_cache,
            rule.cfg_payload,
            rule.hook_parts,
            strip_canvas_suffix=rule.strip_canvas_suffix,
            clean_filenames=rule.clean_filenames,
        )
    )


def preprocess_item_hashes(
    config_path: Path, fetch_cache: dict[str, str] | None = None
) -> dict[str, tuple[list[str], str]]:
    """``stem -> (src relpaths, current-rule hash)`` for every raw item.

    Same rule ``pending_preprocess_items`` applies (raw bytes, fetch stamps
    for folders, [processing] payload, hook scripts), computed for every item
    regardless of the cache — the cache-migration tool rewrites stored hashes
    with it (production rule, never reimplemented). ``fetch_cache`` defaults
    to ``.cache/fetch.json``; migrating callers pass the pending payload so
    the hash matches the post-migration file.
    """
    rule = _preprocess_rule_inputs(config_path)
    if fetch_cache is None:
        fetch_cache = load_cache_file(cache_file(rule.raw_dir.parent, "fetch"))
    hashes: dict[str, tuple[list[str], str]] = {}
    for item in rule.items:
        if not rule.item_files_by[item]:
            continue
        stem = _output_stem(
            item, rule.item_files_by, rule.strip_canvas_suffix, rule.clean_filenames
        )
        hashes[stem] = _item_hash_and_src(
            item,
            rule.item_files_by,
            rule.raw_dir,
            fetch_cache,
            rule.cfg_payload,
            rule.hook_parts,
        )
    return hashes


def preprocess_assignment(  # ruff: ignore[too-many-branches, too-many-statements, too-many-locals]
    assignment_config_path: Path,
    *,
    cancel_event: threading.Event | None = None,
) -> dict | None:
    """Preprocess all raw files for an assignment into processed markdown.

    Top-level raw entries are per-student: a file (single submission) or a
    folder (multi-file student). Folders are concatenated into one
    ``<folder>.md`` with a per-file header (``file:``, ``submitted:`` when the
    stamp is known), and before/after_preprocess_file hooks fire per input
    file with output_file set to the final concatenated file.

    ``cancel_event`` (TUI jobs): checked at each item boundary — on a set
    event the loop stops before the next item (finished items stay cached).
    """
    cfg = load_assignment_file(assignment_config_path)
    processing = cfg.processing
    hook_runtime = HookRuntime.from_config(
        cfg,
        assignment_config_path=assignment_config_path,
    )

    paths = resolve_assignment_paths(cfg, assignment_config_path.parent)
    ensure_assignment_dirs(paths)

    raw_dir = paths.raw_dir
    processed_dir = paths.processed_dir

    processed_dir.mkdir(parents=True, exist_ok=True)

    # Determine input format(s): auto (per-file by suffix, no config) or an
    # explicit [processing.input_format] list.
    configured_formats = _normalize_input_formats(processing.input_format)

    items = _iter_raw_items(raw_dir)

    item_files_by = {item: _item_files(item, configured_formats) for item in items}
    if not any(item_files_by.values()):
        if configured_formats is None:
            print(
                "No supported files found in raw directory: "
                f"{raw_dir}\n"
                "Add student files to raw/ (supported: .ipynb, .html, .txt, .md, .docx, .pdf, .jpg, .jpeg, .png), "
                "then run preprocess again."
            )
        else:
            print(
                "No files found for configured input format(s) "
                f"{configured_formats} in: {raw_dir}\n"
                "Check [processing.input_format] in config or place matching files in raw/."
            )
        return {
            "stage": "preprocess",
            "success": 0,
            "errors": 0,
            "total": 0,
            "success_rate": 0,
        }

    if hook_runtime is not None:
        hook_runtime.run(
            "before_preprocess",
            {
                "assignment_config": str(assignment_config_path),
                "raw_dir": str(raw_dir),
                "processed_dir": str(processed_dir),
                "configured_formats": configured_formats,
            },
        )

    # Processing options
    remove_base64 = processing.remove_base64_images
    clean_filenames = processing.clean_filenames
    strip_canvas_suffix = processing.strip_canvas_suffix
    strip_html_callouts = processing.strip_html_callouts
    strip_html_div_tags = processing.strip_html_div_tags
    strip_html_escaped_backslashes = processing.strip_html_escaped_backslashes
    strip_html_style_blocks = processing.strip_html_style_blocks
    convert_html_tables_to_markdown = processing.convert_html_tables_to_markdown
    strip_colab_dataframe_widgets = processing.strip_colab_dataframe_widgets
    strip_html_script_tags = processing.strip_html_script_tags
    strip_html_button_tags = processing.strip_html_button_tags
    strip_html_svg_tags = processing.strip_html_svg_tags
    normalize_dtype_label_html = processing.normalize_dtype_label_html
    remove_nbconvert_assets = processing.remove_nbconvert_assets
    nbconvert_template, template_dir_path = _resolve_template_base(
        assignment_config_path, processing
    )

    # Process each raw item (per-student): a file (single submission) or a
    # folder (multi-file student, concatenated into one per-student md).
    fetch_cache: dict[str, str] = load_cache_file(cache_file(raw_dir.parent, "fetch"))

    # Output cache (<assignment>/.cache/preprocess.json): skip an item whose
    # raw inputs, processing config, template selection, fetch stamps (folder
    # headers) and hook scripts are unchanged and whose output md exists.
    # Old entries with a changed hash are reconverted (no pruning needed).
    cache_path = cache_file(raw_dir.parent, "preprocess")
    cache = load_cache_file(cache_path)
    cfg_payload = json.dumps(
        {
            "processing": processing.model_dump_json(),
            "template_name": nbconvert_template,
            "template_dir": (
                str(template_dir_path) if template_dir_path is not None else None
            ),
        },
        sort_keys=True,
    ).encode("utf-8")
    hook_parts: list[bytes] = []
    if hook_runtime is not None:
        hook_parts = [
            script.read_bytes()
            for script_paths in hook_runtime.mounts.values()
            for script in script_paths
        ]

    # Rule for "would reconvert" (shared with the TUI display via
    # _preprocess_pending): an item is pending unless .cache/preprocess.json
    # holds a matching hash AND its output md exists; any change reconverts.
    pending = _preprocess_pending(
        items,
        item_files_by,
        raw_dir,
        processed_dir,
        cache,
        fetch_cache,
        cfg_payload,
        hook_parts,
        strip_canvas_suffix=strip_canvas_suffix,
        clean_filenames=clean_filenames,
    )

    processed_count = 0
    failed_count = 0

    for item in items:
        if cancel_event is not None and cancel_event.is_set():
            print("[cancelled] preprocess stopped — finished items are cached")
            break
        files = item_files_by[item]
        if not files:
            if item.is_dir():
                print(f"[skip] folder {item.name} (no supported files)")
            continue
        if item.is_file():
            raw_file, file_format = files[0]
            # Output md stem (cleaned filename; shared with the cache rule).
            output_stem = _output_stem(
                item, item_files_by, strip_canvas_suffix, clean_filenames
            )
            output_file = processed_dir / f"{output_stem}.md"

            entry = pending.get(item)
            if entry is None:
                print(f"[cached] {output_file.name} (unchanged)")
                if processing.visual_evaluation and _screenshots_missing(
                    processed_dir / "screenshots", output_stem
                ):
                    _render_stem_screenshots(
                        processed_dir,
                        output_stem,
                        [(raw_file, file_format)],
                        nbconvert_template,
                        template_dir_path,
                    )
                continue

            src, item_hash = entry

            if hook_runtime is not None:
                before_payload = hook_runtime.run(
                    "before_preprocess_file",
                    {
                        "assignment_config": str(assignment_config_path),
                        "input_file": str(raw_file),
                        "output_file": str(output_file),
                        "input_format": file_format,
                    },
                )
                input_file = Path(before_payload.get("input_file", str(raw_file)))
                output_file = Path(before_payload.get("output_file", str(output_file)))
                file_format = str(before_payload.get("input_format", file_format))
            else:
                input_file = raw_file

            try:  # ruff: ignore[too-many-statements-in-try-clause]
                assert file_format in SUPPORTED_INPUT_FORMATS, (
                    f"Unsupported input format: {file_format}. "
                    f"Must be one of: {SUPPORTED_INPUT_FORMATS}"
                )
                _process_single_file(
                    input_file,
                    output_file,
                    file_format,  # ty:ignore[invalid-argument-type]
                    remove_base64,
                    strip_html_callouts,
                    strip_html_div_tags,
                    strip_html_escaped_backslashes,
                    strip_html_style_blocks,
                    convert_html_tables_to_markdown,
                    strip_colab_dataframe_widgets,
                    strip_html_script_tags,
                    strip_html_button_tags,
                    strip_html_svg_tags,
                    normalize_dtype_label_html,
                    remove_nbconvert_assets,
                    nbconvert_template,
                    template_dir_path,
                )
                print(f"[processed] {raw_file.name} -> {output_file.name}")
                processed_count += 1
                cache[output_stem] = {"hash": item_hash, "src": src}
                if processing.visual_evaluation:
                    _render_stem_screenshots(
                        processed_dir,
                        output_stem,
                        [(input_file, file_format)],
                        nbconvert_template,
                        template_dir_path,
                    )
                if hook_runtime is not None:
                    hook_runtime.run(
                        "after_preprocess_file",
                        {
                            "assignment_config": str(assignment_config_path),
                            "input_file": str(raw_file),
                            "output_file": str(output_file),
                            "input_format": file_format,
                            "success": True,
                        },
                    )
            except Exception as exc:
                print(f"[error] Failed to process {raw_file.name}: {exc}")
                failed_count += 1
                if hook_runtime is not None:
                    hook_runtime.run(
                        "after_preprocess_file",
                        {
                            "assignment_config": str(assignment_config_path),
                            "input_file": str(raw_file),
                            "output_file": str(output_file),
                            "input_format": file_format,
                            "success": False,
                            "error": str(exc),
                        },
                    )
        else:
            # Multi-file student folder: convert each supported file to a
            # temp md, then concatenate into one <folder>.md with per-file
            # headers (file:, submitted: when the stamp is known). Hooks fire
            # per input file but always report the final concatenated file as
            # output_file.
            output_file = processed_dir / f"{item.name}.md"
            entry = pending.get(item)
            if entry is None:
                print(f"[cached] {output_file.name} (unchanged)")
                if processing.visual_evaluation and _screenshots_missing(
                    processed_dir / "screenshots", item.name
                ):
                    _render_stem_screenshots(
                        processed_dir,
                        item.name,
                        files,
                        nbconvert_template,
                        template_dir_path,
                    )
                continue
            src, item_hash = entry
            parts: list[str] = []
            converted = 0
            # R2: one render pass per stem with continuous numbering across
            # members (see _render_stem_screenshots for the cached path);
            # clean the stem's old shots once before any member renders.
            page_offset = 0
            img_offset = 0
            if processing.visual_evaluation:
                _cleanup_stem_shots(processed_dir / "screenshots", item.name)
            for raw_file, fmt in files:
                tmp_file: Path | None = None
                input_file = raw_file
                file_format = fmt
                try:  # ruff: ignore[too-many-statements-in-try-clause]
                    if hook_runtime is not None:
                        before_payload = hook_runtime.run(
                            "before_preprocess_file",
                            {
                                "assignment_config": str(assignment_config_path),
                                "input_file": str(raw_file),
                                "output_file": str(output_file),
                                "input_format": file_format,
                            },
                        )
                        input_file = Path(
                            before_payload.get("input_file", str(raw_file))
                        )
                        output_file = Path(
                            before_payload.get("output_file", str(output_file))
                        )
                        file_format = str(
                            before_payload.get("input_format", file_format)
                        )
                    assert file_format in SUPPORTED_INPUT_FORMATS, (
                        f"Unsupported input format: {file_format}. "
                        f"Must be one of: {SUPPORTED_INPUT_FORMATS}"
                    )
                    fd, tmp_name = tempfile.mkstemp(suffix=".md", dir=processed_dir)
                    os.close(fd)
                    tmp_file = Path(tmp_name)
                    _process_single_file(
                        input_file,
                        tmp_file,
                        file_format,  # ty:ignore[invalid-argument-type]
                        remove_base64,
                        strip_html_callouts,
                        strip_html_div_tags,
                        strip_html_escaped_backslashes,
                        strip_html_style_blocks,
                        convert_html_tables_to_markdown,
                        strip_colab_dataframe_widgets,
                        strip_html_script_tags,
                        strip_html_button_tags,
                        strip_html_svg_tags,
                        normalize_dtype_label_html,
                        remove_nbconvert_assets,
                        nbconvert_template,
                        template_dir_path,
                    )
                    text = tmp_file.read_text(encoding="utf-8")
                    stamp = _submission_stamp(fetch_cache, raw_file)
                    submitted = f", submitted: {stamp}" if stamp else ""
                    parts.append(
                        f"---\n<!--- file: {raw_file.name}{submitted} -->\n\n{text}"
                    )
                    output_file.write_text("\n".join(parts), encoding="utf-8")
                    converted += 1
                    print(f"[processed] {raw_file.name} -> {output_file.name}")
                    if processing.visual_evaluation:
                        n_pages, n_images = _render_screenshots(
                            input_file,
                            item.name,
                            processed_dir,
                            file_format,
                            nbconvert_template,
                            template_dir_path,
                            page_offset=page_offset,
                            img_offset=img_offset,
                        )
                        page_offset += n_pages
                        img_offset += n_images
                    if hook_runtime is not None:
                        hook_runtime.run(
                            "after_preprocess_file",
                            {
                                "assignment_config": str(assignment_config_path),
                                "input_file": str(raw_file),
                                "output_file": str(output_file),
                                "input_format": file_format,
                                "success": True,
                            },
                        )
                except Exception as exc:
                    print(f"[error] Failed to process {raw_file.name}: {exc}")
                    failed_count += 1
                    if hook_runtime is not None:
                        hook_runtime.run(
                            "after_preprocess_file",
                            {
                                "assignment_config": str(assignment_config_path),
                                "input_file": str(raw_file),
                                "output_file": str(output_file),
                                "input_format": file_format,
                                "success": False,
                                "error": str(exc),
                            },
                        )
                finally:
                    if tmp_file is not None:
                        tmp_file.unlink(missing_ok=True)
            if converted:
                processed_count += 1
                cache[item.name] = {"hash": item_hash, "src": src}

    try:
        save_cache_file(cache_path, cache)
    except OSError as exc:
        print(f"[warn] failed to write preprocess cache: {exc}")

    if hook_runtime is not None:
        hook_runtime.run(
            "after_preprocess",
            {
                "assignment_config": str(assignment_config_path),
                "raw_dir": str(raw_dir),
                "processed_dir": str(processed_dir),
                "processed_count": processed_count,
                "failed_count": failed_count,
            },
        )

    total = processed_count + failed_count
    success_rate = (processed_count / total * 100) if total > 0 else 0
    return {
        "stage": "preprocess",
        "success": processed_count,
        "errors": failed_count,
        "total": total,
        "success_rate": success_rate,
    }


def main() -> None:
    args = parse_cli_args(ProcessingCliOptions)

    preprocess_assignment(args.config)


if __name__ == "__main__":
    main()

"""Core config editing: comment-preserving field write-back + validation.

Extracted from the Textual settings screen (:mod:`src.tata_settings`) so the
CLI and the TUI share the same writer and validation. No Textual imports.
"""

from __future__ import annotations

import tomllib
from collections.abc import MutableMapping
from pathlib import Path

import tomlkit
from pydantic import ValidationError

from src.assignment_config import (
    AssignmentFileConfig,
    AssignmentSection,
    FetchSection,
    GradingSection,
    PlagiarismSection,
    ProcessingSection,
    load_assignment_file,
)

_WEIGHT_EPS = 1e-6  # tolerance for the copydetect+embedding sum check

#: Section models used for validating single-section edits (course/global
#: contexts, and the ``config set`` CLI for container configs).
_SECTION_MODELS: dict[str, type] = {
    "assignment": AssignmentSection,
    "fetch": FetchSection,
    "grading": GradingSection,
    "processing": ProcessingSection,
    "plagiarism": PlagiarismSection,
}


def read_config(path: Path) -> dict:
    """Tolerant read: {} when the file is missing, unreadable, or invalid TOML."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def dump_toml(data: dict) -> str:
    """Serialize ``data`` back to TOML via tomlkit.

    ponytail: stdlib ``tomllib`` is read-only, so tomlkit does the
    serialization for the shapes these configs contain (scalars, lists,
    ``[table]`` / ``[[table]]`` nesting). None values are skipped (TOML has
    no null).
    """

    def clean(value: object) -> object:
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if v is not None}
        if isinstance(value, list):
            return [clean(v) for v in value if v is not None]
        return value

    return tomlkit.dumps(tomlkit.item(clean(data)))


def edit_config(path: Path, edits: dict[str, dict[str, object]]) -> bool:
    """Overlay ``edits`` (section -> key -> value) in place on the file's TOML.

    The original text is parsed, so comments, formatting and unknown keys
    survive (a whole-file rebuild would destroy user comments) — only the
    edited keys change. Missing or unparseable files start from an empty
    document; None values are never written. Writes with a trailing newline
    and returns True (raises OSError on write failure).
    """
    try:
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, tomlkit.exceptions.ParseError):
        doc = tomlkit.parse("")
    for section, values in edits.items():
        table = doc.get(section)
        if not isinstance(table, MutableMapping):
            doc[section] = tomlkit.table()
            table = doc[section]
        for key, value in values.items():
            if value is None:
                continue
            table[key] = value
    out = tomlkit.dumps(doc)
    if not out.endswith("\n"):
        out += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out, encoding="utf-8")
    return True


def merge_edits(original: dict, edits: dict) -> dict:
    """Overlay ``edits`` onto ``original`` per section-per-key."""
    merged = dict(original)
    for section, values in edits.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section] = {**merged[section], **values}
        else:
            merged[section] = values
    return merged


def _fmt_errors(exc: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in e.get('loc', []))}: {e.get('msg')}"
        for e in exc.errors()
    ]


def _section_errors(edits: dict[str, dict[str, object]]) -> list[str]:
    """Validate edited sections against their models (container-config path).

    ``validate_config_edits`` rejects unknown sections before calling this.
    """
    errors: list[str] = []
    for section, values in edits.items():
        model = _SECTION_MODELS.get(section)
        if model is None:
            continue
        try:
            model.model_validate(values)
        except ValidationError as exc:
            errors.extend(_fmt_errors(exc))
    return errors


def _weight_sum_error(plag: dict) -> str | None:
    w1, w2 = plag.get("copydetect_weight"), plag.get("embedding_weight")
    if not isinstance(w1, (int, float)) or not isinstance(w2, (int, float)):
        return None
    total = w1 + w2
    if abs(total - 1.0) > _WEIGHT_EPS:
        return f"plagiarism weights: sum {total:.2f} ≠ 1.00"
    return None


def validate_config_edits(path: Path, edits: dict[str, dict[str, object]]) -> None:
    """Validate ``edits`` against the project's pydantic models before writing.

    Mirrors the settings screen: an assignment config validates the full
    layered ``AssignmentFileConfig`` (as :func:`load_assignment_file` loads
    it) with the edits applied; course/global containers validate the edited
    sections only. The plagiarism weight-sum rule applies to the effective
    layer either way. Unparseable TOML and unknown sections are rejected
    (the writer would otherwise clobber them). Raises ValueError with a
    one-line message.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        raw: dict = {}
    else:
        try:
            raw = tomllib.loads(raw_text)
        except tomllib.TOMLDecodeError as exc:
            msg = f"invalid TOML in {path}: {exc}"
            raise ValueError(msg) from exc

    errors: list[str] = []
    for section in edits:
        if section not in _SECTION_MODELS:
            known = ", ".join(sorted(_SECTION_MODELS))
            errors.append(f"unknown config section {section!r} (known: {known})")
    try:
        layered = load_assignment_file(path).model_dump()
    except (FileNotFoundError, ValueError):
        # Container config (no [grading]) or already-broken assignment:
        # validate the edited sections directly, like the screen's
        # course/global path does.
        layered = None
        errors.extend(_section_errors(edits))
        effective_plag = merge_edits(
            PlagiarismSection().model_dump(),
            merge_edits(raw, edits).get("plagiarism") or {},
        )
    else:
        merged = merge_edits(layered, edits)
        try:
            AssignmentFileConfig.model_validate(merged)
        except ValidationError as exc:
            errors.extend(_fmt_errors(exc))
        effective_plag = merged.get("plagiarism") or {}

    weight_error = _weight_sum_error(effective_plag)
    if weight_error is not None:
        errors.append(weight_error)
    if errors:
        raise ValueError("; ".join(errors))

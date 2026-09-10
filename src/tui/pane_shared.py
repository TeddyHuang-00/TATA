"""Shared helpers for the Library tab's three panes (rubrics/prompts/providers)."""

from __future__ import annotations

from collections.abc import MutableMapping
from pathlib import Path

import tomlkit

#: Select value for the "New …" file option (never a real file name).
new_value = "__new__"


def validate_name(raw: str, suffix: str) -> str | None:
    """Strip, append the suffix, reject path separators and dot-only names;
    None if invalid."""
    name = raw.strip()
    if not name or not name.strip("."):
        return None
    if not name.endswith(suffix):
        name += suffix
    if Path(name).name != name:
        return None
    return name


def referencing_configs(data_dir: Path, needle: str) -> list[Path]:
    """Read-only scan: config.toml files under ``data/`` whose text mentions
    ``needle`` (e.g. ``rubrics/sample.toml``). Course + assignment level only
    (``data/*/config.toml``, ``data/*/*/config.toml``)."""
    hits: list[Path] = []
    for pattern in ("*/config.toml", "*/*/config.toml"):
        for path in data_dir.glob(pattern):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if needle in text:
                hits.append(path)
    return hits


def grading_reference_configs(data_dir: Path, old: str) -> list[Path]:
    """Parse-based scan: config.toml files whose ``[grading]`` section
    references ``old`` (rubric or a system_prompt entry). Same path patterns
    as :func:`referencing_configs`; unreadable/unparseable files are skipped.
    """
    hits: list[Path] = []
    for pattern in ("*/config.toml", "*/*/config.toml"):
        for path in data_dir.glob(pattern):
            try:
                doc = tomlkit.parse(path.read_text(encoding="utf-8"))
            except (OSError, tomlkit.exceptions.ParseError):
                continue
            grading = doc.get("grading")
            if not isinstance(grading, MutableMapping):
                continue
            if grading.get("rubric") == old:
                hits.append(path)
                continue
            prompts = grading.get("system_prompt")
            if (isinstance(prompts, list) and old in prompts) or (
                isinstance(prompts, str) and prompts == old
            ):
                hits.append(path)
    return hits


def rewrite_grading_refs(
    data_dir: Path, old: str, new: str
) -> tuple[list[Path], list[Path]]:
    """Rewrite ``old`` -> ``new`` in every referencing config's ``[grading]``
    (rubric, and every system_prompt list member). Returns (changed, failed).
    """
    changed: list[Path] = []
    failed: list[Path] = []
    for path in grading_reference_configs(data_dir, old):
        try:
            doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        except (OSError, tomlkit.exceptions.ParseError):
            failed.append(path)
            continue
        grading = doc.get("grading")
        if not isinstance(grading, MutableMapping):
            failed.append(path)
            continue
        if grading.get("rubric") == old:
            grading["rubric"] = new
        prompts = grading.get("system_prompt")
        if isinstance(prompts, list):
            for i, item in enumerate(prompts):
                if item == old:
                    prompts[i] = new
        elif isinstance(prompts, str) and prompts == old:
            grading["system_prompt"] = new
        out = tomlkit.dumps(doc)
        if not out.endswith("\n"):
            out += "\n"
        try:
            path.write_text(out, encoding="utf-8")
        except OSError:
            failed.append(path)
        else:
            changed.append(path)
    return changed, failed

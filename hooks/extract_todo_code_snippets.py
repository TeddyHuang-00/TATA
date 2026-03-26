from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

_FENCE = "```"
_TODO_PATTERN = re.compile(r"^\s*#{1,2}\s*TODO\s*\d", flags=re.IGNORECASE)
_MD_SIGNAL_THRESHOLD = 3
_INSTRUCTION_MARKER_THRESHOLD = 2
_MD_DOMINANCE_RATIO = 2
_HEADER_NOTE = (
    "> NOTE: This extracted file only includes key TODO implementations from the "
    "student submission. Please grade gracefully and account for omitted boilerplate "
    "or instructional context."
)


def _extract_notebook_language(notebook: dict[str, Any]) -> str:
    metadata = notebook.get("metadata", {})
    if not isinstance(metadata, dict):
        return "python"

    language_info = metadata.get("language_info", {})
    if isinstance(language_info, dict):
        language = language_info.get("name")
        if isinstance(language, str) and language.strip():
            return language.strip()

    kernelspec = metadata.get("kernelspec", {})
    if isinstance(kernelspec, dict):
        language = kernelspec.get("language")
        if isinstance(language, str) and language.strip():
            return language.strip()

    return "python"


def _normalize_source(source: list[str] | str) -> str:
    if isinstance(source, list):
        return "".join(str(part) for part in source)
    if isinstance(source, str):
        return source
    return ""


def _is_markdown_like_noise(source: str) -> bool:
    text = source.strip()
    if not text:
        return True

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return True

    md_signal_count = 0
    code_signal_count = 0

    md_patterns = [
        r"^#{1,6}\s+",
        r"^[-*+]\s+",
        r"^\d+\.\s+",
        r"\[[^\]]+\]\([^\)]+\)",
        r"^!\[.*\]\(.*\)",
        r"<\s*(img|table|div|p|span|a|h\d|br|style|script)\b",
        r"\$[^$]+\$",
    ]
    code_patterns = [
        r"^(import|from)\s+",
        r"^(def|class|if|for|while|try|except|with|return|raise|assert)\b",
        r"=",
        r"\(",
        r"^[A-Za-z_][A-Za-z0-9_\.]*\s*\(",
        r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$",
        r"^[A-Za-z_][A-Za-z0-9_]*\s*\[.+\]$",
    ]

    for ln in lines:
        if any(re.search(pat, ln) for pat in md_patterns):
            md_signal_count += 1
        if any(re.search(pat, ln) for pat in code_patterns):
            code_signal_count += 1

    if (
        md_signal_count >= _MD_SIGNAL_THRESHOLD
        and md_signal_count > code_signal_count * _MD_DOMINANCE_RATIO
    ):
        return True

    lower = text.lower()
    instruction_markers = [
        "for this todo",
        "store the output",
        "use the numpy function",
        "you should see",
        "convert the above",
        "let's review",
    ]
    return (
        sum(marker in lower for marker in instruction_markers)
        >= _INSTRUCTION_MARKER_THRESHOLD
    )


def _strip_todo_check_calls(source: str) -> str:
    lines = source.splitlines()
    kept: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if re.match(r"^\s*todo_check\s*\(", line):
            balance = line.count("(") - line.count(")")
            i += 1
            while i < len(lines) and balance > 0:
                balance += lines[i].count("(") - lines[i].count(")")
                i += 1
            continue

        if not line.strip():
            kept.append("")
        else:
            kept.append(line)
        i += 1

    cleaned = "\n".join(kept)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _has_code_signal(source: str) -> bool:
    code_patterns = [
        r"^(import|from)\s+",
        r"^(def|class|if|for|while|try|except|with|return|raise|assert)\b",
        r"=",
        r"^[A-Za-z_][A-Za-z0-9_\.]*\s*\(",
        r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$",
        r"^[A-Za-z_][A-Za-z0-9_]*\s*\[.+\]$",
    ]
    for ln in source.splitlines():
        if any(re.search(pat, ln.strip()) for pat in code_patterns):
            return True
    return False


def _build_todo_only_markdown(notebook_path: Path) -> str:
    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
    cells = notebook.get("cells", [])
    if not isinstance(cells, list):
        return ""

    language = _extract_notebook_language(notebook)
    chunks: list[str] = []

    for cell in cells:
        if not isinstance(cell, dict):
            continue
        if cell.get("cell_type") != "code":
            continue

        source = _normalize_source(cell.get("source", "")).strip()
        if not source:
            continue
        if not any(_TODO_PATTERN.search(ln) for ln in source.splitlines()):
            continue
        if _is_markdown_like_noise(source):
            continue

        source = _strip_todo_check_calls(source)
        if not source:
            continue
        if not _has_code_signal(source):
            continue

        chunks.append(f"{_FENCE}{language}\n{source}\n{_FENCE}")

    body = "\n\n".join(chunks).strip()
    if body:
        return f"{_HEADER_NOTE}\n\n{body}\n"
    return f"{_HEADER_NOTE}\n"


def main() -> None:
    payload = json.load(sys.stdin)

    input_file = Path(str(payload.get("input_file", "")))
    output_file = Path(str(payload.get("output_file", "")))
    input_format = str(payload.get("input_format", ""))
    assignment_config_path = Path(str(payload.get("assignment_config", "")))

    if input_format != "ipynb":
        json.dump(payload, sys.stdout)
        return

    if not input_file.exists() or input_file.suffix.lower() != ".ipynb":
        json.dump(payload, sys.stdout)
        return

    cfg_reference_name = "reference"
    try:
        cfg_toml = tomllib.loads(assignment_config_path.read_text(encoding="utf-8"))
        assignment_cfg = cfg_toml.get("assignment", {})
        if isinstance(assignment_cfg, dict):
            reference_file = str(assignment_cfg.get("reference_file", "reference.md"))
            cfg_reference_name = Path(reference_file).stem.lower()
    except Exception:
        pass

    if input_file.stem.lower() == cfg_reference_name:
        json.dump(payload, sys.stdout)
        return

    todo_only_md = _build_todo_only_markdown(input_file)

    temp_dir = output_file.parent / ".hook_tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / f"{output_file.stem}.todo_only.md"
    temp_file.write_text(todo_only_md, encoding="utf-8")

    payload["input_file"] = str(temp_file)
    payload["input_format"] = "markdown"
    json.dump(payload, sys.stdout)


if __name__ == "__main__":
    main()

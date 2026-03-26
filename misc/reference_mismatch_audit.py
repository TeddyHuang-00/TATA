#!/usr/bin/env python3
"""Reference Notebook Mismatch Auditor.

Workflow (recommended):
1) Wide scan:
   - Find every markdown cell that defines a TODO section.
   - Attach nearby code cells for each TODO (until the next TODO markdown).
2) Hard mismatch checks:
   - Placeholder detection: pass / NotImplementedError / "your code here".
   - Hard token checks from instruction text:
     - Backticked symbols (e.g., np.where, fit_transform, lamb=10)
     - Explicit tuple-like numeric requirements (e.g., (20, 30))
     - Explicit numeric assignments (e.g., lamb = 10, alpha = .1)
3) Manual review pass:
   - Review only TODOs with hard mismatches.
   - Keep equivalent solutions as PASS unless the instruction is explicitly strict.
4) Report and fix loop:
   - Output a concise report with evidence per TODO.
   - Apply minimal fixes and rerun this script until hard mismatches are zero.

Usage examples:
  uv run python misc/reference_mismatch_audit.py \
    --notebook assignments/4-lab-poly-ridge-regres/reference.ipynb

  uv run python misc/reference_mismatch_audit.py \
    --notebook assignments/1-lab-python-review/reference.ipynb \
    --format json --output misc/audit_report.json
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

TODO_RE = re.compile(r"TODO\s*(\d+)", flags=re.IGNORECASE)
BACKTICK_RE = re.compile(r"`([^`]+)`")
TUPLE_RE = re.compile(r"\(\s*\d+\s*,\s*\d+\s*\)")
ASSIGNMENT_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]*\s*=\s*[-.]?\d+(?:\.\d+)?\b")
PASS_RE = re.compile(r"(^|\n)\s*pass\s*(\n|$)")
MIN_TOKEN_LENGTH = 3

# Tokens that are often documentation words, not strict implementation requirements.
IGNORE_TOKENS = {
    "docs",
    "hint",
    "out[]",
    "pandas",
    "numpy",
    "matplotlib",
    "seaborn",
    "todo_check()",
}


@dataclass(frozen=True)
class TodoSection:
    todo_number: int
    instruction_cell: int
    instruction_text: str
    code_cells: list[int]
    code_text: str


@dataclass(frozen=True)
class TodoAuditResult:
    todo_number: int
    instruction_cell: int
    code_cells: list[int]
    unresolved_placeholders: bool
    missing_hard_tokens: list[str]


AuditFormat = Literal["text", "json"]


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _extract_todo_sections(cells: list[dict[str, object]]) -> list[TodoSection]:
    sections: list[TodoSection] = []

    for idx, cell in enumerate(cells):
        if cell.get("cell_type") != "markdown":
            continue

        source = "".join(cell.get("source", []))
        match = TODO_RE.search(source)
        if match is None:
            continue

        todo_number = int(match.group(1))
        code_cells: list[int] = []
        code_chunks: list[str] = []

        for j in range(idx + 1, len(cells)):
            next_cell = cells[j]
            if next_cell.get("cell_type") == "markdown":
                next_source = "".join(next_cell.get("source", []))
                if TODO_RE.search(next_source):
                    break
                continue

            if next_cell.get("cell_type") == "code":
                code_cells.append(j + 1)
                code_chunks.append("".join(next_cell.get("source", [])))

        sections.append(
            TodoSection(
                todo_number=todo_number,
                instruction_cell=idx + 1,
                instruction_text=source,
                code_cells=code_cells,
                code_text="\n\n".join(code_chunks),
            )
        )

    return sections


def _extract_hard_tokens(instruction_text: str) -> list[str]:
    tokens: list[str] = []

    for token in BACKTICK_RE.findall(instruction_text):
        candidate = token.strip()
        if not candidate:
            continue
        if candidate.lower() in IGNORE_TOKENS:
            continue
        if len(candidate) < MIN_TOKEN_LENGTH:
            continue
        tokens.append(candidate)

    tokens.extend(TUPLE_RE.findall(instruction_text))
    tokens.extend(ASSIGNMENT_RE.findall(instruction_text))

    deduped: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        key = token.strip()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)

    return deduped


def _find_missing_tokens(tokens: list[str], code_text: str) -> list[str]:
    compact_code = _normalize_space(code_text)
    return [token for token in tokens if _normalize_space(token) not in compact_code]


def _has_unresolved_placeholders(code_text: str) -> bool:
    lowered = code_text.lower()
    if PASS_RE.search(code_text) is not None:
        return True
    if "notimplementederror" in lowered:
        return True
    return "your code here" in lowered


def audit_notebook(notebook_path: Path) -> tuple[list[TodoAuditResult], int]:
    payload = json.loads(notebook_path.read_text(encoding="utf-8"))
    cells = payload.get("cells", [])
    if not isinstance(cells, list):
        msg = f"Invalid notebook JSON: missing list at cells in {notebook_path}"
        raise ValueError(msg)

    sections = _extract_todo_sections(cells)
    results: list[TodoAuditResult] = []

    for section in sections:
        tokens = _extract_hard_tokens(section.instruction_text)
        missing_tokens = _find_missing_tokens(tokens, section.code_text)
        unresolved = _has_unresolved_placeholders(section.code_text)

        results.append(
            TodoAuditResult(
                todo_number=section.todo_number,
                instruction_cell=section.instruction_cell,
                code_cells=section.code_cells,
                unresolved_placeholders=unresolved,
                missing_hard_tokens=missing_tokens,
            )
        )

    return results, len(sections)


def _to_text_report(notebook_path: Path, results: list[TodoAuditResult], total_todos: int) -> str:
    flagged = [
        result
        for result in results
        if result.unresolved_placeholders or result.missing_hard_tokens
    ]

    lines: list[str] = []
    lines.extend(
        [
            f"Notebook: {notebook_path}",
            f"TODO sections found: {total_todos}",
            f"Flagged TODOs: {len(flagged)}",
            "",
        ]
    )

    for result in results:
        status = "FLAG" if (result.unresolved_placeholders or result.missing_hard_tokens) else "OK"
        lines.append(
            f"TODO {result.todo_number} [{status}] | "
            f"instruction_cell={result.instruction_cell} | code_cells={result.code_cells}"
        )
        if result.unresolved_placeholders:
            lines.append("  - unresolved_placeholders: true")
        if result.missing_hard_tokens:
            lines.append("  - missing_hard_tokens:")
            lines.extend(f"    - {token}" for token in result.missing_hard_tokens)

    if not flagged:
        lines.append("\nNo hard mismatches detected by rule-based checks.")

    lines.append("\nNote: This is a rule-based audit. Always do a manual pass on flagged TODOs.")
    return "\n".join(lines)


def _to_json_report(notebook_path: Path, results: list[TodoAuditResult], total_todos: int) -> str:
    flagged_count = sum(
        1
        for result in results
        if result.unresolved_placeholders or result.missing_hard_tokens
    )

    payload = {
        "notebook": str(notebook_path),
        "todo_count": total_todos,
        "flagged_count": flagged_count,
        "results": [
            {
                "todo_number": result.todo_number,
                "instruction_cell": result.instruction_cell,
                "code_cells": result.code_cells,
                "unresolved_placeholders": result.unresolved_placeholders,
                "missing_hard_tokens": result.missing_hard_tokens,
            }
            for result in results
        ],
        "note": "Rule-based audit. Manually review all flagged TODOs before concluding mismatch.",
    }
    return json.dumps(payload, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit TODO instruction/implementation mismatches in a reference notebook.")
    parser.add_argument(
        "--notebook",
        type=Path,
        required=True,
        help="Path to reference notebook (.ipynb).",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output report format.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output file path. If omitted, prints to stdout.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    notebook_path = args.notebook.resolve()

    if not notebook_path.exists() or not notebook_path.is_file():
        msg = f"Notebook not found: {notebook_path}"
        raise FileNotFoundError(msg)

    results, total_todos = audit_notebook(notebook_path)

    if args.format == "json":
        report = _to_json_report(notebook_path, results, total_todos)
    else:
        report = _to_text_report(notebook_path, results, total_todos)

    if args.output is None:
        print(report)
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report + "\n", encoding="utf-8")
    print(f"Wrote report to: {args.output}")


if __name__ == "__main__":
    main()

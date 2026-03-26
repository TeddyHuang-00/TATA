from __future__ import annotations

import json
import re
import sys

MAX_STUDENT_CHARS = 120_000
MAX_REFERENCE_CHARS = 40_000


def _truncate_markdown(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text

    # Keep TODO-focused and heading lines first, then append a tail window.
    lines = text.splitlines()
    focused: list[str] = []
    for ln in lines:
        if re.search(r"\bTODO\b|^#{1,6}\s", ln, flags=re.IGNORECASE):
            focused.append(ln)

    focused_blob = "\n".join(focused)
    tail_blob = text[-(max_chars // 2) :]
    merged = (focused_blob + "\n\n" + tail_blob).strip()

    if len(merged) > max_chars:
        merged = merged[-max_chars:]

    return "[TRUNCATED FOR TOKEN LIMIT]\n" + merged


def main() -> None:
    payload = json.load(sys.stdin)

    student_text = str(payload.get("student_text", ""))
    reference_text = str(payload.get("reference_text", ""))

    payload["student_text"] = _truncate_markdown(student_text, MAX_STUDENT_CHARS)
    payload["reference_text"] = _truncate_markdown(reference_text, MAX_REFERENCE_CHARS)

    json.dump(payload, sys.stdout)


if __name__ == "__main__":
    main()

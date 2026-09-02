"""Hash-keyed pipeline caches (preprocess output, grading).

Mirrors the ``.fetch-cache.json`` convention: a flat JSON dict inside the
directory it caches, dot-named so scan counters skip it, missing/broken JSON
treated as empty, atomic enough with a single write (no partial-indent
rewrites to worry about).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

CACHE_FMT = 1


def load_cache(path: Path) -> dict:
    """Cache dict from ``path``; missing or broken JSON -> {}."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(path: Path, data: dict) -> None:
    """Write the cache dict as compact JSON, creating the parent dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def content_hash(parts: list[bytes]) -> str:
    """sha256 hexdigest over ``parts``, NUL-separated (deterministic)."""
    return hashlib.sha256(b"\0".join(parts)).hexdigest()


def file_hash(base: Path, relpaths: list[str]) -> str:
    """sha256 over sorted relpaths; each entry is relpath + NUL + file bytes."""
    parts = [
        rel.encode("utf-8") + b"\0" + (base / rel).read_bytes()
        for rel in sorted(relpaths)
    ]
    return content_hash(parts)

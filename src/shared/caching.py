"""Unified cache core for pipeline caches (fetch, preprocess, grading, embedding).

Caches live at ``<assignment_dir>/.cache/<name>.json`` with the envelope
``{"fmt": CACHE_FMT, "data": <dict>}``.  Loads are tolerant — missing file,
broken JSON, wrong ``fmt`` or non-dict ``data`` all read as ``{}`` and never
raise.  Saves are atomic: a temp file in the same directory is ``replace``d
into position, compact JSON, so readers never observe a partial write.

``file_digest`` memoizes its sha256 by ``(path, st_mtime_ns, st_size)`` so the
TUI can poll at 0.1s without re-hashing large files.  Invalidation boundary: a
content swap that keeps both ``st_mtime_ns`` and ``st_size`` identical is not
noticed (in practice, real writes bump mtime_ns).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from functools import lru_cache
from pathlib import Path

CACHE_FMT = 1


def cache_dir(assignment_dir: Path) -> Path:
    """``<assignment_dir>/.cache`` — returned, not created."""
    return Path(assignment_dir) / ".cache"


def cache_file(assignment_dir: Path, name: str) -> Path:
    """``<assignment_dir>/.cache/<name>.json`` — returned, not created."""
    return cache_dir(assignment_dir) / f"{name}.json"


def load_cache_file(path: Path) -> dict:
    """Envelope payload from ``path``; missing/broken/foreign fmt -> ``{}``.

    Never raises: missing file, unreadable bytes, invalid JSON, a ``fmt``
    mismatch or non-dict ``data`` are all treated as an empty cache.
    """
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("fmt") != CACHE_FMT:
        return {}
    data = raw.get("data")
    return data if isinstance(data, dict) else {}


def save_cache_file(path: Path, data: dict) -> None:
    """Atomically write ``{"fmt": CACHE_FMT, "data": data}`` as compact JSON.

    Creates the parent dir, writes a temp file in the same directory and
    ``replace``s it into position; exceptions propagate to the caller.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"fmt": CACHE_FMT, "data": data}, separators=(",", ":"))
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        Path(tmp_name).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
        raise


def file_digest(path: Path) -> str:
    """sha256 hexdigest of ``path``, memoized on ``(path, mtime_ns, size)``.

    A memo hit skips the file read (TUI polls at 0.1s).  A missing file raises
    ``FileNotFoundError`` exactly like ``Path.read_bytes()`` would.
    """
    st = path.stat()
    return _digest_cached(str(path), st.st_mtime_ns, st.st_size)


# Sized for one job's 500+ screenshot working set; raise if cross-job churn bites.
@lru_cache(maxsize=4096)
def _digest_cached(path_str: str, mtime_ns: int, size: int) -> str:
    # mtime_ns/size are cache-key components only; the digest reads fresh bytes.
    del mtime_ns, size
    return hashlib.sha256(Path(path_str).read_bytes()).hexdigest()


# legacy: old flat-file helpers - delete once B1-B4 call sites migrate.
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

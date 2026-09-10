"""Tests for src.shared.caching: .cache/ location, {fmt,data} envelope,
tolerant reads, atomic writes, memoized file_digest (B0)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from src.shared.caching import (
    CACHE_FMT,
    cache_dir,
    cache_file,
    content_hash,
    file_digest,
    load_cache_file,
    save_cache_file,
)

# --- location -------------------------------------------------------------


def test_cache_dir_is_dot_cache_under_assignment(tmp_path: Path) -> None:
    assert cache_dir(tmp_path) == tmp_path / ".cache"


def test_cache_file_does_not_create_directories(tmp_path: Path) -> None:
    path = cache_file(tmp_path, "preprocess")
    assert path == tmp_path / ".cache" / "preprocess.json"
    assert not path.parent.exists()


# --- envelope round-trip --------------------------------------------------


def test_envelope_round_trip_creates_directory(tmp_path: Path) -> None:
    path = cache_file(tmp_path, "grading")
    data = {"alice": {"hash": "abc123"}, "bob": {"hash": "def456"}}

    save_cache_file(path, data)

    assert path.parent.is_dir()  # .cache/ auto-created
    assert load_cache_file(path) == data
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw == {"fmt": CACHE_FMT, "data": data}


def test_envelope_is_compact_json(tmp_path: Path) -> None:
    path = cache_file(tmp_path, "fetch")
    save_cache_file(path, {"a": 1})
    assert path.read_text(encoding="utf-8") == '{"fmt":1,"data":{"a":1}}'


# --- tolerant reads -------------------------------------------------------


def test_load_cache_file_missing_returns_empty(tmp_path: Path) -> None:
    assert load_cache_file(tmp_path / "nope.json") == {}
    assert load_cache_file(tmp_path / "not-a-file" / "nope.json") == {}
    assert load_cache_file(tmp_path) == {}  # a directory, not a file


@pytest.mark.parametrize("content", ["{broken", "", "not json at all"])
def test_load_cache_file_broken_json_returns_empty(
    tmp_path: Path, content: str
) -> None:
    path = cache_file(tmp_path, "grading")
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    assert load_cache_file(path) == {}


def test_load_cache_file_non_utf8_returns_empty(tmp_path: Path) -> None:
    path = cache_file(tmp_path, "grading")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe{}")
    assert load_cache_file(path) == {}


def test_load_cache_file_fmt_mismatch_returns_empty(tmp_path: Path) -> None:
    path = cache_file(tmp_path, "grading")
    path.parent.mkdir(parents=True)
    for envelope in ({"fmt": CACHE_FMT + 1, "data": {"a": 1}}, {"data": {"a": 1}}):
        path.write_text(json.dumps(envelope), encoding="utf-8")
        assert load_cache_file(path) == {}


@pytest.mark.parametrize("data", [["a", "list"], "a string", 42, None])
def test_load_cache_file_data_not_dict_returns_empty(
    tmp_path: Path, data: object
) -> None:
    path = cache_file(tmp_path, "grading")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"fmt": CACHE_FMT, "data": data}), encoding="utf-8")
    assert load_cache_file(path) == {}


def test_load_cache_file_non_dict_top_level_returns_empty(tmp_path: Path) -> None:
    path = cache_file(tmp_path, "grading")
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2]", encoding="utf-8")
    assert load_cache_file(path) == {}


# --- atomic writes --------------------------------------------------------


def test_save_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    path = cache_file(tmp_path, "grading")
    save_cache_file(path, {"a": 1})
    assert [p.name for p in path.parent.iterdir()] == ["grading.json"]


def test_second_save_wins(tmp_path: Path) -> None:
    path = cache_file(tmp_path, "grading")
    save_cache_file(path, {"v": 1})
    save_cache_file(path, {"v": 2})
    assert load_cache_file(path) == {"v": 2}


# --- file_digest ----------------------------------------------------------


def test_file_digest_matches_sha256(tmp_path: Path) -> None:
    f = tmp_path / "f.bin"
    f.write_bytes(b"hello")
    assert file_digest(f) == hashlib.sha256(b"hello").hexdigest()


def test_file_digest_same_content_same_value(tmp_path: Path) -> None:
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"same bytes")
    b.write_bytes(b"same bytes")
    assert file_digest(a) == file_digest(b)


def test_file_digest_content_change_same_size_new_value(tmp_path: Path) -> None:
    f = tmp_path / "f.bin"
    f.write_bytes(b"aaaa")
    before = file_digest(f)
    f.write_bytes(b"bbbb")  # same size, different bytes -> mtime_ns bumps
    assert file_digest(f) != before


def test_file_digest_missing_raises_like_read_bytes(tmp_path: Path) -> None:
    missing = tmp_path / "missing.bin"
    with pytest.raises(FileNotFoundError):
        file_digest(missing)


def test_file_digest_memo_hit_skips_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 4096)
    reads = 0
    original_read_bytes = Path.read_bytes

    def counting_read_bytes(self: Path) -> bytes:
        nonlocal reads
        reads += 1
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)

    first = file_digest(f)
    after_first = reads
    second = file_digest(f)

    assert second == first
    assert after_first == 1  # first call read the file once
    assert reads == 1  # second call hit the memo: no re-read


# --- frozen semantics -----------------------------------------------------


def test_content_hash_semantics_unchanged() -> None:
    assert content_hash([b"a", b"b"]) == hashlib.sha256(b"a\0b").hexdigest()

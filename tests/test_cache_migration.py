"""B5a: cache-migration tool — carry sets, snapshot gating, dry-run, idempotency.

Every hash compared here is recomputed by the production rules (never
hardcoded — Pitfall 18): ``preprocess_item_hashes``, ``grading_pending``,
``embedding_input_hash``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from instructor import Mode
from src.shared.assignment_config import load_assignment_file, resolve_assignment_paths
from src.shared.cache_migration import _assignment_dirs, migrate_assignment_caches
from src.shared.caching import cache_file, load_cache_file, save_cache_file
from src.shared.grading import (
    grading_pending,
    load_assignment_config,
    pending_grade_submissions,
)
from src.shared.pipeline import pending_preprocess_items, preprocess_item_hashes
from src.shared.plagiarism import embedding_input_hash
from src.shared.provider import ProviderInfo, ProviderList

_GRADING = (
    '[grading]\nrubric = "rubrics/r.toml"\n'
    'system_prompt = ["prompt/system.md"]\nprovider = "test"\n'
)
_PAIRS = [
    {
        "test_file": "100.md",
        "reference_file": "200.md",
        "test_similarity_pct": 90.0,
        "reference_similarity_pct": 90.0,
        "max_similarity_pct": 90.0,
        "token_overlap": 0,
    }
]
_LEGACY_FILES = (
    "raw/.fetch-cache.json",
    "processed/.preprocess.cache.json",
    "logs/grading.cache.json",
    "plagiarism/all_pairs.embedding.json",
)


def _patch_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.shared.grading.get_providers",
        lambda: ProviderList(
            providers={
                "test": ProviderInfo(
                    base_url="http://test",
                    api_key="sk-test",
                    model="m1",
                    mode=Mode.TOOLS,
                    temperature=0.0,
                )
            }
        ),
    )


def _write_assignment(tmp_path: Path, *, old_caches: bool = True) -> Path:
    """Course/assignment layout with the four legacy cache files.

    Raw items: two flat files (100, 200) and one folder item (300) — the
    folder's hash includes the fetch stamps, the flat ones do not.
    """
    adir = tmp_path / "course" / "a1"
    for sub in (
        "raw",
        "processed",
        "graded",
        "logs",
        "plagiarism",
        "rubrics",
        "prompt",
    ):
        (adir / sub).mkdir(parents=True)
    (adir / "config.toml").write_text(_GRADING, encoding="utf-8")
    (adir / "rubrics" / "r.toml").write_text(
        '[[criterion]]\nname = "C1"\ndesc = "d"\npts = 10\nrating = "binary"\n'
        'grading = "standard"\n',
        encoding="utf-8",
    )
    (adir / "prompt" / "system.md").write_text("You are a TA.\n", encoding="utf-8")
    (adir / "raw" / "100.md").write_text("# 100\n", encoding="utf-8")
    (adir / "raw" / "200.md").write_text("# 200\n", encoding="utf-8")
    (adir / "raw" / "300").mkdir()
    (adir / "raw" / "300" / "300.md").write_text("# 300\n", encoding="utf-8")
    for stem in ("100", "200", "300"):
        (adir / "processed" / f"{stem}.md").write_text(f"# {stem}\n", encoding="utf-8")
    (adir / "graded" / "100.json").write_text("{}", encoding="utf-8")
    if not old_caches:
        return adir
    (adir / "raw" / ".fetch-cache.json").write_text(
        json.dumps({"300.md": "t1", "gone.md": "t2"}), encoding="utf-8"
    )
    (adir / "processed" / ".preprocess.cache.json").write_text(
        json.dumps({
            "100": {"fmt": 1, "hash": "old", "src": ["100.md"]},
            "200": {"fmt": 1, "hash": "old", "src": ["200.md"]},
        }),
        encoding="utf-8",
    )
    (adir / "logs" / "grading.cache.json").write_text(
        json.dumps({
            "100": {"fmt": 1, "hash": "old"},
            "200": {"fmt": 1, "hash": "old"},
        }),
        encoding="utf-8",
    )
    (adir / "plagiarism" / "all_pairs.embedding.json").write_text(
        json.dumps({"version": 1, "pair_count": len(_PAIRS), "pairs": _PAIRS}),
        encoding="utf-8",
    )
    return adir


def _snapshot(**overrides: object) -> dict:
    entry: dict = {
        "valid_preprocess": [],
        "valid_grading": [],
        "embedding_fresh": False,
    }
    entry.update(overrides)
    return {"assignments": {"a1": entry}}


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_snapshot_gates_carry_and_hashes_match_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshot-valid entries carry with production hashes; the rest do not."""
    _patch_providers(monkeypatch)
    adir = _write_assignment(tmp_path)
    config_path = adir / "config.toml"
    snap = _snapshot(
        valid_preprocess=["100"], valid_grading=["100"], embedding_fresh=True
    )

    report = migrate_assignment_caches(adir, snapshot=snap)
    assert report["snapshot"] is True
    assert report["dry_run"] is False
    assert report["warnings"] == []
    assert report["fetch"] == {"old": 2, "carried": 1, "dropped": 1}
    assert report["preprocess"] == {"old": 2, "carried": 1, "skipped": 1}
    assert report["grading"] == {"old": 2, "carried": 1, "skipped": 1}
    assert report["embedding"] == {"carried": True, "pairs": 1, "reason": "fresh"}

    # fetch: only entries whose file still exists (gone.md dropped), stamp kept
    assert load_cache_file(cache_file(adir, "fetch")) == {"300.md": "t1"}

    # preprocess: hash+src from the production rule, snapshot gate respected
    hashes = preprocess_item_hashes(config_path)
    migrated_preprocess = load_cache_file(cache_file(adir, "preprocess"))
    assert migrated_preprocess == {
        "100": {"hash": hashes["100"][1], "src": hashes["100"][0]}
    }
    assert migrated_preprocess["100"]["src"] == ["100.md"]

    # grading: hash from grading_pending's sub_hashes
    cfg = load_assignment_config(config_path)
    _, sub_hashes = grading_pending(cfg, load_assignment_file(config_path))
    assert load_cache_file(cache_file(adir, "grading")) == {
        "100": {"hash": sub_hashes["100"]}
    }

    # embedding: pairs kept, input hash recomputed by the production rule
    cfg_model = load_assignment_file(config_path)
    processed_dir = resolve_assignment_paths(cfg_model, adir).processed_dir
    assert load_cache_file(cache_file(adir, "embedding")) == {
        "hash": embedding_input_hash(
            processed_dir, cfg_model.plagiarism.embedding_model
        ),
        "pairs": _PAIRS,
    }

    # production views after migration: carried entries stay valid, the rest
    # are pending again
    assert [p.stem for p in pending_grade_submissions(config_path)] == ["200", "300"]
    assert {p.name for p in pending_preprocess_items(config_path)} == {"200.md", "300"}

    # legacy files are never deleted
    for rel in _LEGACY_FILES:
        assert (adir / rel).is_file()


def test_embedding_stale_or_missing_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """embedding_fresh false -> no carry; fresh but no legacy file -> warn, no crash."""
    _patch_providers(monkeypatch)
    adir = _write_assignment(tmp_path)
    snap = _snapshot(valid_preprocess=[], valid_grading=[], embedding_fresh=False)

    report = migrate_assignment_caches(adir, snapshot=snap)
    assert not cache_file(adir, "embedding").exists()
    assert report["embedding"] == {"carried": False, "pairs": 0, "reason": "stale"}
    assert (adir / "plagiarism" / "all_pairs.embedding.json").is_file()

    snap["assignments"]["a1"]["embedding_fresh"] = True
    (adir / "plagiarism" / "all_pairs.embedding.json").unlink()
    report2 = migrate_assignment_caches(adir, snapshot=snap)
    assert not cache_file(adir, "embedding").exists()
    assert report2["embedding"]["carried"] is False
    assert any("embedding" in warning for warning in report2["warnings"])


def test_dry_run_writes_nothing_and_reruns_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry_run leaves the tree byte-identical; two real runs write the same bytes."""
    _patch_providers(monkeypatch)
    adir = _write_assignment(tmp_path)
    snap = _snapshot(
        valid_preprocess=["100"], valid_grading=["100"], embedding_fresh=True
    )
    before = _tree_bytes(adir)

    dry = migrate_assignment_caches(adir, snapshot=snap, dry_run=True)
    assert dry["dry_run"] is True
    assert not (adir / ".cache").exists()
    assert _tree_bytes(adir) == before

    first = migrate_assignment_caches(adir, snapshot=snap)
    after_first = _tree_bytes(adir)
    second = migrate_assignment_caches(adir, snapshot=snap)
    assert second == first  # same report
    assert _tree_bytes(adir) == after_first  # same bytes, .cache included
    assert dry["fetch"] == first["fetch"]  # dry run predicted the real run
    assert dry["preprocess"] == first["preprocess"]
    assert dry["grading"] == first["grading"]
    assert dry["embedding"] == first["embedding"]
    for rel in _LEGACY_FILES:
        assert (adir / rel).is_file()


def test_rerun_after_legacy_cleanup_keeps_folder_items_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: with the legacy files gone, hashes fall back to .cache/fetch.json —
    the re-run is byte-identical and folder submissions stay cached.

    Folder items carry the fetch stamps in their hash; the unconditional {}
    passed before this fix flipped 300 (folder) back to pending on a re-run.
    """
    _patch_providers(monkeypatch)
    adir = _write_assignment(tmp_path)
    config_path = adir / "config.toml"
    snap = _snapshot(valid_preprocess=["100", "200", "300"], valid_grading=["100"])

    first = migrate_assignment_caches(adir, snapshot=snap)
    assert first["preprocess"]["carried"] == 3

    # B5b cleanup: legacy files deleted, .cache/ kept
    for rel in _LEGACY_FILES:
        (adir / rel).unlink()

    before_rerun = _tree_bytes(adir)
    migrate_assignment_caches(adir, snapshot=snap)
    assert _tree_bytes(adir) == before_rerun  # byte-identical re-run

    # production view agrees: nothing pending, stored hashes == current rule
    assert pending_preprocess_items(config_path) == []
    hashes = preprocess_item_hashes(config_path)
    stored = load_cache_file(cache_file(adir, "preprocess"))
    assert stored["300"] == {"hash": hashes["300"][1], "src": hashes["300"][0]}
    assert stored["100"] == {"hash": hashes["100"][1], "src": hashes["100"][0]}


def test_no_legacy_fetch_uses_existing_cache_for_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 fallback level 2: no legacy fetch file -> the hashes read the
    existing .cache/fetch.json (what production reads); nothing rewrites it."""
    _patch_providers(monkeypatch)
    adir = _write_assignment(tmp_path, old_caches=False)
    config_path = adir / "config.toml"
    fetch_path = cache_file(adir, "fetch")
    save_cache_file(fetch_path, {"300.md": "t9"})
    fetch_before = fetch_path.read_bytes()

    snap = _snapshot(valid_preprocess=["100", "300"], valid_grading=[])
    report = migrate_assignment_caches(adir, snapshot=snap)
    assert report["fetch"] == {"old": 0, "carried": 0, "dropped": 0}

    # nothing carried -> the pre-existing fetch cache is not rewritten
    assert fetch_path.read_bytes() == fetch_before

    # the stored folder hash used the stamps from .cache/fetch.json, not {}
    hashes = preprocess_item_hashes(config_path)
    stored = load_cache_file(cache_file(adir, "preprocess"))
    assert stored["300"] == {"hash": hashes["300"][1], "src": hashes["300"][0]}
    assert stored["100"]["hash"] == hashes["100"][1]
    assert (
        stored["300"]["hash"]
        != preprocess_item_hashes(config_path, fetch_cache={})["300"][1]
    )
    assert [p.name for p in pending_preprocess_items(config_path)] == ["200.md"]


def test_no_fetch_cache_anywhere_hashes_match_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 fallback level 3: neither legacy nor .cache/fetch.json -> {} on both
    sides, so the stored hashes still equal the production rule's."""
    _patch_providers(monkeypatch)
    adir = _write_assignment(tmp_path, old_caches=False)
    config_path = adir / "config.toml"
    assert not cache_file(adir, "fetch").exists()

    snap = _snapshot(valid_preprocess=["100", "300"], valid_grading=[])
    migrate_assignment_caches(adir, snapshot=snap)

    hashes = preprocess_item_hashes(config_path)
    stored = load_cache_file(cache_file(adir, "preprocess"))
    assert stored == {
        stem: {"hash": hashes[stem][1], "src": hashes[stem][0]}
        for stem in ("100", "300")
    }
    assert not cache_file(adir, "fetch").exists()
    assert [p.name for p in pending_preprocess_items(config_path)] == ["200.md"]


def test_embedding_invalid_config_warns_and_does_not_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: a config that fails to load skips the embedding stage with a
    warning instead of aborting the whole migration."""
    _patch_providers(monkeypatch)
    adir = _write_assignment(tmp_path)
    # provider missing from [grading] -> load_assignment_file raises ValueError
    (adir / "config.toml").write_text(
        '[grading]\nrubric = "rubrics/r.toml"\nsystem_prompt = ["prompt/system.md"]\n',
        encoding="utf-8",
    )
    snap = _snapshot(valid_preprocess=[], valid_grading=[], embedding_fresh=True)

    report = migrate_assignment_caches(adir, snapshot=snap)  # must not raise

    assert report["embedding"] == {"carried": False, "pairs": 0, "reason": "error"}
    assert any(w.startswith("embedding:") for w in report["warnings"])


def test_no_snapshot_carries_only_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a snapshot only fetch is migrated; the rest waits to recompute."""
    _patch_providers(monkeypatch)
    adir = _write_assignment(tmp_path)

    report = migrate_assignment_caches(adir, snapshot=None)
    assert load_cache_file(cache_file(adir, "fetch")) == {"300.md": "t1"}
    for name in ("preprocess", "grading", "embedding"):
        assert not cache_file(adir, name).exists()
    assert report["preprocess"]["carried"] == 0
    assert report["grading"]["carried"] == 0
    assert report["embedding"]["reason"] == "no snapshot"
    assert any("snapshot" in warning for warning in report["warnings"])

    # a snapshot without an entry for this assignment behaves the same
    other = migrate_assignment_caches(adir, snapshot={"assignments": {"other": {}}})
    assert not cache_file(adir, "preprocess").exists()
    assert any("not in snapshot" in warning for warning in other["warnings"])


def test_missing_files_and_dirs_do_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No legacy files / no raw+processed dirs -> warnings, zero carries, no raise."""
    _patch_providers(monkeypatch)

    # old caches absent, dirs present: nothing to carry, no writes
    clean = _write_assignment(tmp_path / "clean", old_caches=False)
    report = migrate_assignment_caches(clean, snapshot=None)
    assert report["fetch"] == {"old": 0, "carried": 0, "dropped": 0}
    assert not (clean / ".cache").exists()

    # missing legacy files do not block snapshot-valid carries (hashes come
    # from the production rules, not from the legacy file)
    snap = _snapshot(
        valid_preprocess=["100"], valid_grading=["100"], embedding_fresh=True
    )
    report2 = migrate_assignment_caches(clean, snapshot=snap)
    assert report2["preprocess"]["carried"] == 1
    assert report2["grading"]["carried"] == 1
    assert report2["embedding"]["carried"] is False
    assert any("embedding" in warning for warning in report2["warnings"])

    # dirs gone entirely (raw/processed removed): still no crash
    shutil.rmtree(clean / "raw")
    shutil.rmtree(clean / "processed")
    report3 = migrate_assignment_caches(clean, snapshot=snap)
    assert report3["fetch"]["carried"] == 0
    assert any("preprocess" in warning for warning in report3["warnings"])
    assert any("embedding" in warning for warning in report3["warnings"])


def test_cli_assignment_discovery_is_self_evident(tmp_path: Path) -> None:
    """Course traversal picks only dirs whose config loads as an assignment."""
    adir = _write_assignment(tmp_path)
    course = tmp_path / "course"
    junk = course / "not_an_assignment"
    junk.mkdir()
    (junk / "config.toml").write_text("random = 1\n", encoding="utf-8")
    plain = course / "no_config"
    plain.mkdir()

    assert [d.name for d in _assignment_dirs(course)] == ["a1"]
    assert _assignment_dirs(adir) == [adir.resolve()]
    assert _assignment_dirs(tmp_path / "missing") == []

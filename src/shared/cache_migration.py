"""One-time cache migration: legacy per-stage files -> ``.cache/`` envelope.

Legacy layouts (read here; never deleted — B5b backs up, then cleans):

* ``raw/.fetch-cache.json``                flat ``{name: stamp}``
* ``processed/.preprocess.cache.json``     ``{stem: {fmt, hash, src}}``
* ``logs/grading.cache.json``              ``{stem: {fmt, hash}}``
* ``plagiarism/all_pairs.embedding.json``  ``{version, ..., pairs}``

New layout (see :mod:`src.shared.caching`): ``<assignment>/.cache/<name>.json``
with the ``{"fmt": CACHE_FMT, "data": ...}`` envelope. Which entries are
carried is gated by an M0 snapshot (``valid_preprocess`` / ``valid_grading`` /
``embedding_fresh`` per assignment); hashes are always recomputed by the
production rules (``grading_pending``, ``preprocess_item_hashes``,
``embedding_input_hash``) — never reimplemented. Without a snapshot only fetch
entries are carried (existence check); the hash-bearing caches are left to
recompute. ``dry_run=True`` computes and reports only. Re-runs are idempotent:
payloads derive from the legacy files plus the snapshot, so a second run
rewrites identical bytes (a pre-existing ``.cache/`` file is replaced by the
carried subset, never merged).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .assignment_config import load_assignment_file, resolve_assignment_paths
from .caching import cache_file, save_cache_file
from .grading import grading_pending, load_assignment_config
from .pipeline import preprocess_item_hashes
from .plagiarism import embedding_input_hash

DEFAULT_SNAPSHOT = Path("/tmp/tata-cache-snapshot/snapshot.json")

_OLD_CACHE_FILES = {
    "fetch": Path("raw/.fetch-cache.json"),
    "preprocess": Path("processed/.preprocess.cache.json"),
    "grading": Path("logs/grading.cache.json"),
    "embedding": Path("plagiarism/all_pairs.embedding.json"),
}

_CACHE_NAMES = ("fetch", "preprocess", "grading", "embedding")


def _read_old(path: Path) -> dict:
    """Legacy cache JSON as a dict; missing/unreadable/non-object -> ``{}``."""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _live_raw_names(raw_dir: Path) -> set[str]:
    """File names present in ``raw/`` either flat or inside a ``raw/<uid>/``
    folder — the two layouts fetch cache keys refer to. Mirrors the
    canvas_fetch skip rule: a cached name is reusable while the file it names
    exists."""
    if not raw_dir.is_dir():
        return set()
    names = {
        p.name for p in raw_dir.iterdir() if p.is_file() and not p.name.startswith(".")
    }
    for sub in raw_dir.iterdir():
        if sub.is_dir() and not sub.name.startswith("."):
            names.update(
                p.name
                for p in sub.iterdir()
                if p.is_file() and not p.name.startswith(".")
            )
    return names


def _migrate_fetch(assignment_dir: Path, old: dict) -> tuple[dict | None, dict]:
    """Carry fetch entries whose file still exists; drop the rest."""
    live = _live_raw_names(assignment_dir / "raw")
    carried = {name: old[name] for name in sorted(old) if name in live}
    report = {
        "old": len(old),
        "carried": len(carried),
        "dropped": len(old) - len(carried),
    }
    return (carried if old else None), report


def _migrate_preprocess(
    config_path: Path, old: dict, valid: list[str], fetch_payload: dict
) -> tuple[dict | None, dict, list[str]]:
    """Carry snapshot-valid stems with ``{hash, src}`` from the production rule.

    ``fetch_payload`` is the fetch data about to land in ``.cache/fetch.json``
    (folder hashes include fetch stamps; the production file must see the same
    dict after the migration writes it).
    """
    warnings: list[str] = []
    try:
        hashes = preprocess_item_hashes(config_path, fetch_cache=fetch_payload)
    except Exception as exc:  # report, never abort the migration
        warnings.append(f"preprocess: {type(exc).__name__}: {exc}")
        return None, _skip_report(old), warnings
    carried = {
        stem: {"hash": hashes[stem][1], "src": hashes[stem][0]}
        for stem in sorted(set(valid))
        if stem in hashes
    }
    missing = sorted(set(valid) - set(hashes))
    if missing:
        warnings.append(
            f"preprocess: {len(missing)} snapshot-valid stems not in the raw scan: {missing}"
        )
    report = {
        "old": len(old),
        "carried": len(carried),
        "skipped": len(set(old) - set(carried)),
    }
    payload = carried if (old or carried) else None
    return payload, report, warnings


def _migrate_grading(
    config_path: Path, old: dict, valid: list[str]
) -> tuple[dict | None, dict, list[str]]:
    """Carry snapshot-valid stems with the ``grading_pending`` sub-hash."""
    warnings: list[str] = []
    try:
        cfg = load_assignment_config(config_path)
        cfg_model = load_assignment_file(config_path)
        _, sub_hashes = grading_pending(cfg, cfg_model)
    except Exception as exc:  # report, never abort the migration
        warnings.append(f"grading: {type(exc).__name__}: {exc}")
        return None, _skip_report(old), warnings
    carried = {
        stem: {"hash": sub_hashes[stem]}
        for stem in sorted(set(valid))
        if stem in sub_hashes
    }
    missing = sorted(set(valid) - set(sub_hashes))
    if missing:
        warnings.append(
            f"grading: {len(missing)} snapshot-valid stems not in the submission scan: {missing}"
        )
    report = {
        "old": len(old),
        "carried": len(carried),
        "skipped": len(set(old) - set(carried)),
    }
    payload = carried if (old or carried) else None
    return payload, report, warnings


def _migrate_embedding(
    config_path: Path, old: dict, entry: dict
) -> tuple[dict | None, dict, list[str]]:
    """Carry ``pairs`` plus a freshly computed input hash when the snapshot
    says the legacy embedding is fresh; stale/missing is left to recompute."""
    pairs = old.get("pairs")
    if not entry.get("embedding_fresh"):
        reason = "stale" if old else "file missing"
        return None, {"carried": False, "pairs": 0, "reason": reason}, []
    if not isinstance(pairs, list):
        return (
            None,
            {"carried": False, "pairs": 0, "reason": "file missing"},
            [
                "embedding: snapshot says fresh but the legacy file has no pairs; skipped"
            ],
        )
    cfg_model = load_assignment_file(config_path)
    processed_dir = resolve_assignment_paths(
        cfg_model, config_path.parent
    ).processed_dir
    payload = {
        "hash": embedding_input_hash(
            processed_dir, cfg_model.plagiarism.embedding_model
        ),
        "pairs": pairs,
    }
    return payload, {"carried": True, "pairs": len(pairs), "reason": "fresh"}, []


def _skip_report(old: dict) -> dict:
    return {"old": len(old), "carried": 0, "skipped": len(old)}


def migrate_assignment_caches(  # ruff: ignore[too-many-locals]
    assignment_dir: Path,
    *,
    snapshot: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """Migrate one assignment's legacy caches into ``<assignment>/.cache/``.

    ``snapshot`` is the whole M0 document (``{"assignments": {aid: {...}}}``);
    this assignment's entry (``valid_preprocess`` / ``valid_grading`` /
    ``embedding_fresh``) gates which entries are carried. Without the document
    — or without an entry for this assignment — only fetch entries are
    migrated; preprocess/grading/embedding stay untouched and recompute later.
    Returns the per-stage counts; writes nothing (and deletes nothing, ever)
    when ``dry_run=True``.
    """
    assignment_dir = Path(assignment_dir).resolve()
    aid = assignment_dir.name
    config_path = assignment_dir / "config.toml"
    warnings: list[str] = []

    entry: dict | None = None
    if isinstance(snapshot, dict):
        by_aid = snapshot.get("assignments")
        if isinstance(by_aid, dict) and isinstance(by_aid.get(aid), dict):
            entry = by_aid[aid]
    if entry is None:
        note = "no snapshot" if snapshot is None else "assignment not in snapshot"
        warnings.append(
            f"{note}: only fetch entries carried; preprocess/grading/embedding "
            "left to recompute"
        )

    old = {
        name: _read_old(assignment_dir / rel) for name, rel in _OLD_CACHE_FILES.items()
    }

    fetch_payload, fetch_report = _migrate_fetch(assignment_dir, old["fetch"])

    if entry is not None:
        pre_payload, pre_report, stage_warnings = _migrate_preprocess(
            config_path,
            old["preprocess"],
            list(entry.get("valid_preprocess") or []),
            fetch_payload or {},
        )
        grading_payload, grading_report, more_warnings = _migrate_grading(
            config_path, old["grading"], list(entry.get("valid_grading") or [])
        )
        emb_payload, emb_report, final_warnings = _migrate_embedding(
            config_path, old["embedding"], entry
        )
        warnings.extend(stage_warnings + more_warnings + final_warnings)
    else:
        pre_payload = grading_payload = emb_payload = None
        pre_report = _skip_report(old["preprocess"])
        grading_report = _skip_report(old["grading"])
        emb_report = {"carried": False, "pairs": 0, "reason": "no snapshot"}

    payloads = {
        "fetch": fetch_payload,
        "preprocess": pre_payload,
        "grading": grading_payload,
        "embedding": emb_payload,
    }
    if not dry_run:
        for name in _CACHE_NAMES:  # fetch first: preprocess hashes read it
            payload = payloads[name]
            if payload is not None:
                save_cache_file(cache_file(assignment_dir, name), payload)

    return {
        "assignment_dir": str(assignment_dir),
        "aid": aid,
        "dry_run": dry_run,
        "snapshot": entry is not None,
        "fetch": fetch_report,
        "preprocess": pre_report,
        "grading": grading_report,
        "embedding": emb_report,
        "warnings": warnings,
    }


def _assignment_dirs(root: Path) -> list[Path]:
    """``root`` and its direct children that are assignment dirs — self-evidence:
    a ``config.toml`` that :func:`load_assignment_file` accepts (course/global
    container configs fail to load as assignments)."""
    if not root.is_dir():
        return []
    candidates = [root, *(p for p in sorted(root.iterdir()) if p.is_dir())]
    found: list[Path] = []
    for candidate in candidates:
        config_path = candidate / "config.toml"
        if not config_path.is_file():
            continue
        try:
            load_assignment_file(config_path)
        except (OSError, ValueError):
            continue
        found.append(candidate)
    return found


def _format_report(report: dict) -> str:
    fetch = report["fetch"]
    preprocess = report["preprocess"]
    grading = report["grading"]
    embedding = report["embedding"]
    emb = (
        f"{embedding['pairs']} pairs carried"
        if embedding["carried"]
        else f"skipped ({embedding['reason']})"
    )
    line = (
        f"[{report['aid']}] fetch {fetch['carried']}/{fetch['old']} carried"
        f" ({fetch['dropped']} dropped)"
        f" | preprocess {preprocess['carried']}/{preprocess['old']}"
        f" | grading {grading['carried']}/{grading['old']}"
        f" | embedding {emb}"
    )
    if report["warnings"]:
        line += "\n  ! " + "\n  ! ".join(report["warnings"])
    return line


if __name__ == "__main__":  # pragma: no cover - exercised via the real dry run
    parser = argparse.ArgumentParser(
        prog="python -m src.shared.cache_migration",
        description="One-time migration of legacy cache files into "
        "<assignment>/.cache/ (envelope format). Old files are never deleted.",
    )
    parser.add_argument(
        "path",
        help="assignment dir, or a course dir (migrate each assignment below it)",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=DEFAULT_SNAPSHOT,
        help=f"M0 snapshot.json (default: {DEFAULT_SNAPSHOT})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="compute and report only; write nothing"
    )
    opts = parser.parse_args()

    snapshot: dict | None = None
    if opts.snapshot.is_file():
        try:
            parsed = json.loads(opts.snapshot.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            snapshot = parsed
        else:
            print(f"! snapshot unreadable: {opts.snapshot} (fetch-only mode)")
    else:
        print(f"! snapshot not found: {opts.snapshot} (fetch-only mode)")

    dirs = _assignment_dirs(Path(opts.path))
    if not dirs:
        print(f"no assignment dirs at or under {opts.path}")
        sys.exit(1)
    print(
        "dry run — nothing will be written"
        if opts.dry_run
        else "migrating (legacy files are kept)"
    )

    totals = {"fetch": 0, "preprocess": 0, "grading": 0, "embedding": 0}
    for assignment_dir in dirs:
        report = migrate_assignment_caches(
            assignment_dir, snapshot=snapshot, dry_run=opts.dry_run
        )
        print(_format_report(report))
        for name in ("fetch", "preprocess", "grading"):
            totals[name] += report[name]["carried"]
        totals["embedding"] += 1 if report["embedding"]["carried"] else 0
    if len(dirs) > 1:
        print(
            f"TOTAL ({len(dirs)} assignments): fetch {totals['fetch']}"
            f" | preprocess {totals['preprocess']}"
            f" | grading {totals['grading']}"
            f" | embedding {totals['embedding']}"
        )

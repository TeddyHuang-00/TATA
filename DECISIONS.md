# DECISIONS.md

## Layered assignment config

**Date:** 2026-08-28
**Status:** Accepted
**Files:** `src/assignment_config.py`, `data/config.toml`, `main.py`

In the context of six assignments all duplicating `course_id = 271218`, `mode = "attach"`, `out_dir = "raw"` in their own configs, and fetch/plagiarism state living ad hoc in scripts and caches,
facing config drift and repeated state,
we decided for a two-layer config: `data/config.toml` (course-level root: `[fetch]` course_id/mode, `[plagiarism]` weights/alphas) merged under each `data/<name>/config.toml` (per-key assignment wins; all paths resolve against the assignment dir),
and neglected a single global config, per-course config dirs, or keeping the duplicated state,
to achieve single-source course state and config-as-persistent-state,
accepting that assignment configs are no longer self-contained (they need the root for course_id),
because the user explicitly asked for fetch/plagiarism at the assignment root and per-assignment configs in subfolders, and `data/*` is already gitignored so the root config is local state like the rest.

## Plagiarism scripts merged into main.py

**Date:** 2026-08-28
**Status:** Accepted
**Files:** `src/plagiarism.py`, `src/plagiarism_aggregate.py` (from `misc/plagiarism_text.py`, `misc/plagiarism_embedding.py`, `misc/plagiarism_report_aggregate.py`)

In the context of text-submission plagiarism (copydetect + 5% embedding blend) and the cross-assignment aggregate running entirely outside the CLI via three misc scripts with hardcoded constants and flags,
facing a split workflow (embedding script, then text script, then aggregate script with `--pa/--ia` flags),
we decided for folding all three into `main.py plagiarism` (auto-detect code vs text per assignment; `-c data/config.toml` runs all assignments; `--aggregate` produces the cross-assignment z-score report; weights/alphas/floor/cap now config keys in `[plagiarism]`), deleting the misc scripts,
and neglected keeping the scripts with config defaults read from the root,
to achieve one command for the whole plagiarism workflow with tunables in config,
accepting that the embedding model now runs inline (skipped when `all_pairs.embedding.json` is fresher than all `processed/*.md`),
because the user chose "全部折进 main.py plagiarism 子命令 + 根 config 驱动" and the aggregate output (6 files, 54 pairs, 2 students) matches the old script's numbers exactly.

## Deletions of stale scripts and artifacts

**Date:** 2026-08-28
**Status:** Accepted
**Files:** `scripts/run_module1_grading.sh`, `docs/module1/*`, `misc/plagiarism_summary.{json,md}` (kept), `hooks/*` (kept)

In the context of a stale batch script referencing non-existent assignment dirs and an unrelated repo, orphaned Canvas API dumps, and generated artifacts from the deleted aggregate script,
facing dead weight confusing future agents,
we decided for deleting `scripts/run_module1_grading.sh` and `docs/module1/*` (user-confirmed), keeping `misc/plagiarism_summary.{json,md}` and the dormant hooks (user kept them),
and neglected adding a built-in batch grade/score loop ("两条命令的事"),
to achieve a leaner repo,
accepting that the batch loop is now two shell commands per assignment,
because the user's cleanup checklist confirmed exactly these deletions.

## Root config [[fetch.assignments]] list drives fetch and aggregate

**Date:** 2026-08-28
**Status:** Accepted
**Files:** `src/assignment_config.py`, `src/cli_options.py`, `main.py`, `src/plagiarism.py`, `src/plagiarism_aggregate.py`, `data/config.toml`

In the context of `fetch -c data/config.toml` refusing to fetch anything (root config has no assignment_id, so it fell into the interactive picker) and the plagiarism aggregate globbing every dir under `data/` (including `example/`),
facing a root config that could not drive either command and per-assignment [fetch] blocks scattered across six gitignored configs,
we decided for an explicit `[[fetch.assignments]]` list in the root config (each entry: `assignment_id`, optional `mode` falling back to root mode, `out` = fetch output dir relative to the root config; assignment root = `out.parent`), used by `fetch -c data/config.toml` and `fetch --retry` (with the old per-assignment glob kept as fallback for pre-list configs) and by `plagiarism -c data/config.toml --aggregate`, whose per-assignment runs and aggregate pair files are restricted to listed dirs (`BuildConfig.pair_data_files`),
and neglected writing back to per-assignment [fetch] blocks from the list, per-assignment out_dir overrides for listed entries, or listing assignments anywhere but the root,
to achieve one config file that defines the course for both fetch and plagiarism aggregation,
accepting that the list is duplicated state alongside the per-assignment [fetch] blocks (the blocks keep single-assignment fetch working) and that a listed entry without a config.toml is skipped by plagiarism,
because the user explicitly asked for course ID + list of (assignment ID, mode, output path) in the total config usable by both fetch and plagiarism aggregate, and the real fetch run fetched all 6 assignments (318 submissions) with per-entry modes, while the aggregate reported exactly the 6 listed pair files.

## T5 review reuse + T6 split into T6a/T6b/T6c

**Date:** 2026-08-29
**Status:** Accepted
**Files:** `src/score_review.py`, `tests/review_screen_check.py`, `tests/preview_check.py` (T5); `src/tata_plagiarism.py`, `src/tata_settings.py` (T6a/T6b, new)

In the context of T5 verifier rejecting the ScoreReviewScreen extraction (1 MAJOR: escape guard popped the CLI review screen with no way back),
facing a guard that assumed a single-screen CLI stack when the real stack is [default Screen, ScoreReviewScreen],
we decided for an explicit `pop_on_escape` constructor flag (CLI Viewer default False = esc no-op; platform push True = esc pops back), plus a markup hardening fix (Static markup=False + rich.markup.escape on criteria-list data paths) exposed by real student text containing `[https://...](...)` citation markup,
and neglected a centralized markup policy for all Static text paths (json-view/preview-markdown are Markdown widgets, unaffected),
to achieve behavior-equivalent CLI view and a safe platform push,
accepting that T5 landed with the verifier's M1 fix folded in and committed as a single T5 commit with both the extraction and the esc/markup fixes,
because the M1 fix required touching the same file and a split would double review cost; T6 is then split into T6a (PlagiarismScreen, new file) and T6b (SettingsScreen, new file) running in parallel with no shared files, followed by T6c (Dashboard key wiring), because a single T6 subagent would have an oversized context and cross-file write conflicts; cross-course plagiarism Tab is explicitly NOT built (user 2026-08-29 correction, docs 04/01 still carry stale cross-course sections).

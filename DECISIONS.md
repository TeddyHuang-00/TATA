# DECISIONS.md


## TUI module renames + TCSS consolidation

**Date:** 2026-08-31
**Status:** Accepted
**Files:** `src/tui/*.py`, `src/tui/styles/*.tcss`, `pyproject.toml`, `HERMES.md`

In the context of the `tata_` prefix adding noise to every TUI import and CSS living both in files and inline `DEFAULT_CSS` class attributes (four blocks across `library.py`/`settings.py`), and TUI being the primary surface for TATA development,
facing inconsistent styling sources and verbose module names, with no value in the prefix (no other module family named `tata_*` remains),
we decided for renaming the seven `tata_*.py` TUI modules to short names (`src/tui/app.py`, `workspace.py`, `plagiarism.py`, `jobs.py`, `settings.py`, `scan.py`, `library.py`) and moving all Textual CSS under `src/tui/styles/` (`app.tcss`, `score_review.tcss`, `library.tcss`, `settings.tcss`),
and neglected keeping the prefix or per-widget CSS in Python,
to achieve uniform styling location and shorter module paths,
accepting that widget `DEFAULT_CSS` now loads via `Path(__file__).parent / "styles" / ...` at class definition time (same content, byte-identical CSS),
because class names (`TataApp`, screens, panes) and test-file names (`tata_*_check.py`) stay untouched, and the headless test checks are the gate for the behavior contract.

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

## TUI plagiarism 交互改造 + 作业面板精简（Batch T1/T2/T3）

**Date:** 2026-08-30
**Status:** Accepted
**Files:** `src/tata_jobs.py`（新）, `src/tata_app.py`, `src/tata_workspace.py`, `src/tata_plagiarism.py`, `src/tata_scan.py`, `src/assignment_config.py`, `src/plagiarism.py`, `src/score_review.py`, `src/aliases.py`

In the context of the plagiarism tab popping a real browser window (copydetect autoopen=True) conflicting with Textual, the assignment panel still exposing a per-assignment plagiarism stage, the score viewer having no workspace entry, and the job protocol being duplicated ~100 lines across the two job screens,
facing multiple maintainability findings (2 MAJOR + 10 MINOR + 4 COSMETIC, independent review),
we decided to remove plagiarism from the assignment workspace (course panel + S4 tab only), rebuild S4 as course-scoped 4 tabs (Aggregate default / Assignments / Students / Pairs) with an embedded #cmp-pane compare (no push_screen, CompareModal deleted), fix the root cause with `CopyDetector(autoopen=False, silent=True)` plus a `quiet=True` kwarg suppressing the text report (TUI reads JSON), route the course panel [p] through the shared, JSON-writing run_aggregate_job, add a score review button wired through a shared open_score_review helper, unify the display threshold to a single tolerant course-config source, and extract the job protocol into a JobHost mixin in src/tata_jobs.py,
and neglected preserving the per-assignment plagiarism stage, the plain-text report render, the modal-based compare, cross-course plagiarism, per-assignment threshold overrides in the S4 pane, and external-mutation invalidation of the alias lru_cache,
to achieve one consistent interactive plagiarism view with no terminal-side windows, no duplicated job machinery, and a single threshold truth,
accepting that the aggregate pane needs a prior [a]/course-p run to populate (no aggregate.json until then), that the real-data token_overlap int form never triggers red overlap highlighting (fixture list form only), that the JobHost drain timer is widget-bound (no current unmount trigger — documented), and that hand-edited alias.toml changes are visible only after restart or an in-process write,
because the user asked for interactive tab-based plagiarism views without windows, assigned the extra scope decision on tab set (aggregate first) and compare pane retention (Textual-compatible embedded instead of built-in diff widget, which Textual 8.2.8 lacks), and the review's fixes were all low-risk deletions/extractions with byte-identical behavior verification per round. Local dev only (5 commits); remote main untouched per policy.

## Fetch 配置单层化（course config only）+ 0-submission 修复

**Date:** 2026-08-30
**Status:** Accepted
**Files:** `src/assignment_config.py`, `src/canvas_fetch.py`, `src/cli.py`, `src/cli_options.py`, `src/plagiarism.py`, `src/aliases.py`, `src/tata_scan.py`, `src/tata_workspace.py`, `src/tata_app.py`, `src/tata_settings.py`，数据迁移 `data/271218/`（gitignored），docs/README 同步

In the context of re-fetching Module 1-7 printing `text: 0 submissions` for upload-based modules (course `[fetch] mode="text"` forced text mode onto ipynb/docx submissions whose `sub.body` is empty — verified live and via Canvas API; text-entry modules were fine), the `[[fetch.assignments]]` list still carrying `assignment_id`+`out`, fetch settings split across global/course/assignment configs, and the TUI import modal asking for an output dir,
facing a config format that had grown three layers without a single source of truth for fetch, and a silent "0 submissions" failure with no warning,
we decided to make the course config the single fetch source: list entries become `{id, mode?}` with no `out` (output always derived `<course>/<id>/raw`), assignment configs drop `[fetch]` (assignment identity = numeric dir name), `remember_fetch` is replaced by `remember_course_fetch` (writes course config only, never `[fetch].mode` — per-assignment modes live on list entries and `_remember` refuses to bake a course-default mode into them), the legacy per-assignment fallback in `--retry` is deleted, standalone fetches look up per-entry mode from the course list, non-numeric assignment dirs exit with a migrate hint instead of falling into `input()` inside the Textual worker, and `FetchAssignmentEntry.id` tolerates legacy `assignment_id` via `AliasChoices`,
and neglected honoring legacy `out` values on un-migrated list entries (out is dropped silently; covered by one-time `migrate_course_to_ids`, no legacy trees remain) and keeping `out`/`--out` anywhere in the config surface,
to achieve one obvious fetch configuration, per-module modes without cross-contamination, and a re-fetch that reports real counts (attach 56/55/55, text 56/53/54),
accepting that un-migrated legacy course configs must run `python -m src.aliases migrate <course_dir>` once (otherwise entries resolve to ghost id dirs) and that the course `[fetch] mode` remains a default whose stale overwrite risk we removed by never writing it programmatically,
because the user asked for the cleanup ("assignments list no longer accepts out; assignment_id -> id; move fetch settings to course dir") and the 0-submission bug was a direct consequence of the old mode-baking design. Verified: pytest 125, 8/8 headless checks, ruff clean (3 pre-existing errors untouched), live re-fetch non-zero; local dev only (commit 000a4ad7, remote main untouched per policy).

## Fetch 全类型自动收集 + mode 移除 + 多文件学生文件夹化

**Date:** 2026-08-31
**Status:** Accepted
**Files:** `src/canvas_fetch.py`, `src/processing.py`, `src/assignment_config.py`, `src/cli_options.py`, `src/cli.py`, `src/score_review.py`, `src/tata_scan.py`, `src/tata_app.py`, `src/tata_settings.py`, tests/（+10），README/docs/data 同步，plan `plans/2026-08-31-fetch-all-types.md`

In the context of fetch having an exclusive attach|text|auto mode (canvas submissions may mix body text and attachments), per-submission collection dropping one of the two, and mode config leaking course-level defaults into every assignment (the 0-submission bug class),
facing a requirement to auto-collect everything per submission, remove mode entirely, and merge multi-file students into one graded document,
we decided to make fetch layout-syncing: body + all attachments per submission; mode removed from models/CLI/TUI/configs; >1 file per student -> `raw/<uid>/` folder (single files stay flat; `_0` suffix when body+html attachment would collide); each run prunes stale flat duplicates, stale members of produced folders (folder→folder rename), and unproduced folders (2→0 unsubmit, folder→flat), keeping produced dirs, dot-files, and others untouched; preprocess treats raw items as files (unchanged single path) or folders (per-file suffix detection, temp md per member, concat into one `<uid>.md` with `---` + `<!--- file: <name>, submitted: <stamp> -->` headers, body-first ordering); scan counts top-level items; score_review resolves folder members by exact then base-uid stem,
and neglected a `fetch --mode`-free interactive override and any upload-storage dedup beyond name-based cache,
to achieve a mode-less fetch that never drops a student's content, a deterministic per-student processed document with provenance headers, and no silent stale-submission grading,
accepting that the prune is one-directional sync (absent students' stale folders are removed with their cache keys; a re-fetch is always a full declarative state), that folder member ordering is (body-first, then by name) rather than submission-time order, and that folder→folder resubmits with changed files keep only current names,
because the user asked for automatic per-type collection with folder-per-student layout and header-annotated concatenation. Verified: pytest 141 (131→141), 8/8 headless checks, real fetch auto 56/55/54/56/53/54 students, folder 2979482/415019/ -> two-section processed md with cache-stamped headers; local dev only (commit 967683f8 on dev, remote main untouched).

## 导入快速配置 + Settings v2 内置编辑 + 布局修复

**Date:** 2026-09-01
**Status:** Accepted
**Files:** `src/tata_app.py`, `src/tata_settings.py`, `src/tata_rubric.py`（新）, `src/aliases.py`, `tests/tata_dash_check.py`, `tests/tata_modal_check.py`, `tests/tata_rubric_check.py`（新）, `tests/tata_settings_check.py`, `tests/test_aliases.py`, `plans/2026-09-01-import-setup-and-settings-v2.md`

In the context of assignment import ending at an empty config that still needed manual `e` = $EDITOR editing, settings slots `grading.rubric`/`system_prompt` being free-text inputs with no enumeration of the local libraries, rubric content creation requiring a text editor, and SettingsScreen's context Select invisible with uncontrolled row heights,
facing a post-import manual-config gap, a settings form that could silently typo library paths, and a layout blocker whose root cause looked like a large CSS problem,
we decided for a one-shot quick-setup panel (ImportAssignmentModal dismisses `(aid, name)` → AssignmentSetupModal: rubric Select over `data/rubrics/*.toml`, prompt multi-checkbox over `data/prompt/*.md`, provider Select over the registry; defaults = first / all / first; Import disabled until a prompt is checked or any library is empty), which on confirm writes `data/<course>/<aid>/config.toml` (`[grading]` via `edit_config`, header `# schema: ../../config/assignment.schema.json`) and seeds aliases (`seed_course_alias` → `data/alias.toml` `[course]`, `seed_assignment_alias` → `data/<course>/alias.toml` `[assignment]`) before launching the fetch job (course import also seeds `[course]`), plus Settings v2: dynamic `grading.rubric` Select (`rubrics/<file>`) and `system_prompt` `_PromptCheckList` (`prompt/<file>`), `(inherited)` badges on assignment-context keys not set in the local config (local keys computed from raw config, not the merged view), and a push-screen `RubricBuilderScreen` (`src/tata_rubric.py`, pushed like Review, `b` binding + Grading-tab button) editing criteria (name/desc/rating/grading/pts/custom_scale) validated by `RubricDefinition`, writing `data/rubrics/<name>.toml` and refreshing the lists on return; the layout fix turned out to be a root-cause gotcha — Textual 8.2.8 ignores the `CSS` class attribute on non-Screen widgets (SettingsScreen is a `Vertical`, not a pushed Screen), so `DEFAULT_CSS` is required (c9272e81: `CSS` → `DEFAULT_CSS`),
and neglected Canvas write-back, real-time plots, multi-job parallelism (still v2 non-goals), provider-registry editing in Settings (stays read-only display), and keeping `e` = $EDITOR as anything but a fallback path,
to achieve import-to-configured in one panel with file enums instead of typo-prone paths, in-TUI rubric authoring, and a visible, stable Settings layout,
accepting that `(inherited)` badges reflect only the local-config layer (the merged view could still differ at course/global level), that RubricBuilderScreen is a separate Screen rather than inline editing in Settings, and that the quick-setup defaults (first rubric / all prompts / first provider) may need manual adjustment on the first real import,
because the user confirmed the quick-setup panel with defaults, the RubricBuilderScreen as a separate Screen parallel to Settings, the `(inherited)` badge semantics, and the one-shot 3-phase execution (setup → write config + aliases → fetch). Verified: pytest 145 (141→145), 8/8 headless checks (settings / rubric / dash / modal / app / workspace / plagiarism / fetchall), ruff clean; lesson captured: when a flow changes, update ALL related headless checks in the same batch — the dash-check drift was only caught after P1 because the modal check had been updated alone (the batch fixed both: `tata_dash_check.py` + `tata_modal_check.py`, 4c94338c). Local dev only (commits a1642fe6, 87c3a7e1, 4c94338c, c9272e81), remote main untouched per policy.

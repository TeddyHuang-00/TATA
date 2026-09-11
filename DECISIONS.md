# DECISIONS.md

> Old decisions are historical snapshots; the current state is README/HERMES.md and the code. Some early decisions were
> superseded by later batches (e.g. the schema mechanism was removed, fetch mode/out was deleted); when tracing history, read
> in chronological order.

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

In the context of six assignments all duplicating `course_id = 111111`, `mode = "attach"`, `out_dir = "raw"` in their own configs, and fetch/plagiarism state living ad hoc in scripts and caches,
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
because the user chose "fold everything into a main.py plagiarism subcommand + root-config driven" and the aggregate output (6 files, N pairs, N students) matches the old script's numbers exactly.

## Deletions of stale scripts and artifacts

**Date:** 2026-08-28
**Status:** Accepted
**Files:** `scripts/run_module1_grading.sh`, `docs/module1/*`, `misc/plagiarism_summary.{json,md}` (kept), `hooks/*` (kept)

In the context of a stale batch script referencing non-existent assignment dirs and an unrelated repo, orphaned Canvas API dumps, and generated artifacts from the deleted aggregate script,
facing dead weight confusing future agents,
we decided for deleting `scripts/run_module1_grading.sh` and `docs/module1/*` (user-confirmed), keeping `misc/plagiarism_summary.{json,md}` and the dormant hooks (user kept them),
and neglected adding a built-in batch grade/score loop ("a two-command job"),
to achieve a leaner repo,
accepting that the batch loop is now two shell commands per assignment,
because the user's cleanup checklist confirmed exactly these deletions.

## Root config \[[fetch.assignments]\] list drives fetch and aggregate

**Date:** 2026-08-28
**Status:** Accepted
**Files:** `src/assignment_config.py`, `src/cli_options.py`, `main.py`, `src/plagiarism.py`, `src/plagiarism_aggregate.py`, `data/config.toml`

In the context of `fetch -c data/config.toml` refusing to fetch anything (root config has no assignment_id, so it fell into the interactive picker) and the plagiarism aggregate globbing every dir under `data/` (including `example/`),
facing a root config that could not drive either command and per-assignment [fetch] blocks scattered across six gitignored configs,
we decided for an explicit `[[fetch.assignments]]` list in the root config (each entry: `assignment_id`, optional `mode` falling back to root mode, `out` = fetch output dir relative to the root config; assignment root = `out.parent`), used by `fetch -c data/config.toml` and `fetch --retry` (with the old per-assignment glob kept as fallback for pre-list configs) and by `plagiarism -c data/config.toml --aggregate`, whose per-assignment runs and aggregate pair files are restricted to listed dirs (`BuildConfig.pair_data_files`),
and neglected writing back to per-assignment [fetch] blocks from the list, per-assignment out_dir overrides for listed entries, or listing assignments anywhere but the root,
to achieve one config file that defines the course for both fetch and plagiarism aggregation,
accepting that the list is duplicated state alongside the per-assignment [fetch] blocks (the blocks keep single-assignment fetch working) and that a listed entry without a config.toml is skipped by plagiarism,
because the user explicitly asked for course ID + list of (assignment ID, mode, output path) in the total config usable by both fetch and plagiarism aggregate, and the real fetch run fetched all 6 assignments (N submissions) with per-entry modes, while the aggregate reported exactly the 6 listed pair files.

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

## TUI plagiarism interaction rework + assignment panel slimming (Batch T1/T2/T3)

**Date:** 2026-08-30
**Status:** Accepted
**Files:** `src/tata_jobs.py` (new), `src/tata_app.py`, `src/tata_workspace.py`, `src/tata_plagiarism.py`, `src/tata_scan.py`, `src/assignment_config.py`, `src/plagiarism.py`, `src/score_review.py`, `src/aliases.py`

In the context of the plagiarism tab popping a real browser window (copydetect autoopen=True) conflicting with Textual, the assignment panel still exposing a per-assignment plagiarism stage, the score viewer having no workspace entry, and the job protocol being duplicated ~100 lines across the two job screens,
facing multiple maintainability findings (2 MAJOR + 10 MINOR + 4 COSMETIC, independent review),
we decided to remove plagiarism from the assignment workspace (course panel + S4 tab only), rebuild S4 as course-scoped 4 tabs (Aggregate default / Assignments / Students / Pairs) with an embedded #cmp-pane compare (no push_screen, CompareModal deleted), fix the root cause with `CopyDetector(autoopen=False, silent=True)` plus a `quiet=True` kwarg suppressing the text report (TUI reads JSON), route the course panel [p] through the shared, JSON-writing run_aggregate_job, add a score review button wired through a shared open_score_review helper, unify the display threshold to a single tolerant course-config source, and extract the job protocol into a JobHost mixin in src/tata_jobs.py,
and neglected preserving the per-assignment plagiarism stage, the plain-text report render, the modal-based compare, cross-course plagiarism, per-assignment threshold overrides in the S4 pane, and external-mutation invalidation of the alias lru_cache,
to achieve one consistent interactive plagiarism view with no terminal-side windows, no duplicated job machinery, and a single threshold truth,
accepting that the aggregate pane needs a prior [a]/course-p run to populate (no aggregate.json until then), that the real-data token_overlap int form never triggers red overlap highlighting (fixture list form only), that the JobHost drain timer is widget-bound (no current unmount trigger — documented), and that hand-edited alias.toml changes are visible only after restart or an in-process write,
because the user asked for interactive tab-based plagiarism views without windows, assigned the extra scope decision on tab set (aggregate first) and compare pane retention (Textual-compatible embedded instead of built-in diff widget, which Textual 8.2.8 lacks), and the review's fixes were all low-risk deletions/extractions with byte-identical behavior verification per round. Local dev only (5 commits); remote main untouched per policy.

## Fetch config single-layered (course config only) + 0-submission fix

**Date:** 2026-08-30
**Status:** Accepted
**Files:** `src/assignment_config.py`, `src/canvas_fetch.py`, `src/cli.py`, `src/cli_options.py`, `src/plagiarism.py`, `src/aliases.py`, `src/tata_scan.py`, `src/tata_workspace.py`, `src/tata_app.py`, `src/tata_settings.py`, data migration `data/111111/` (gitignored), docs/README sync

In the context of re-fetching Module 1-7 printing `text: 0 submissions` for upload-based modules (course `[fetch] mode="text"` forced text mode onto ipynb/docx submissions whose `sub.body` is empty — verified live and via Canvas API; text-entry modules were fine), the `[[fetch.assignments]]` list still carrying `assignment_id`+`out`, fetch settings split across global/course/assignment configs, and the TUI import modal asking for an output dir,
facing a config format that had grown three layers without a single source of truth for fetch, and a silent "0 submissions" failure with no warning,
we decided to make the course config the single fetch source: list entries become `{id, mode?}` with no `out` (output always derived `<course>/<id>/raw`), assignment configs drop `[fetch]` (assignment identity = numeric dir name), `remember_fetch` is replaced by `remember_course_fetch` (writes course config only, never `[fetch].mode` — per-assignment modes live on list entries and `_remember` refuses to bake a course-default mode into them), the legacy per-assignment fallback in `--retry` is deleted, standalone fetches look up per-entry mode from the course list, non-numeric assignment dirs exit with a migrate hint instead of falling into `input()` inside the Textual worker, and `FetchAssignmentEntry.id` tolerates legacy `assignment_id` via `AliasChoices`,
and neglected honoring legacy `out` values on un-migrated list entries (out is dropped silently; covered by one-time `migrate_course_to_ids`, no legacy trees remain) and keeping `out`/`--out` anywhere in the config surface,
to achieve one obvious fetch configuration, per-module modes without cross-contamination, and a re-fetch that reports real counts (attach N/N/N, text N/N/N),
accepting that un-migrated legacy course configs must run `python -m src.aliases migrate <course_dir>` once (otherwise entries resolve to ghost id dirs) and that the course `[fetch] mode` remains a default whose stale overwrite risk we removed by never writing it programmatically,
because the user asked for the cleanup ("assignments list no longer accepts out; assignment_id -> id; move fetch settings to course dir") and the 0-submission bug was a direct consequence of the old mode-baking design. Verified: pytest 125, 8/8 headless checks, ruff clean (3 pre-existing errors untouched), live re-fetch non-zero; local dev only (commit 000a4ad7, remote main untouched per policy).

## Fetch full-type auto-collection + mode removal + multi-file per-student dirs

**Date:** 2026-08-31
**Status:** Accepted
**Files:** `src/canvas_fetch.py`, `src/processing.py`, `src/assignment_config.py`, `src/cli_options.py`, `src/cli.py`, `src/score_review.py`, `src/tata_scan.py`, `src/tata_app.py`, `src/tata_settings.py`, tests/ (+10), README/docs/data sync, plan `plans/2026-08-31-fetch-all-types.md`

In the context of fetch having an exclusive attach|text|auto mode (canvas submissions may mix body text and attachments), per-submission collection dropping one of the two, and mode config leaking course-level defaults into every assignment (the 0-submission bug class),
facing a requirement to auto-collect everything per submission, remove mode entirely, and merge multi-file students into one graded document,
we decided to make fetch layout-syncing: body + all attachments per submission; mode removed from models/CLI/TUI/configs; >1 file per student -> `raw/<uid>/` folder (single files stay flat; `_0` suffix when body+html attachment would collide); each run prunes stale flat duplicates, stale members of produced folders (folder→folder rename), and unproduced folders (2→0 unsubmit, folder→flat), keeping produced dirs, dot-files, and others untouched; preprocess treats raw items as files (unchanged single path) or folders (per-file suffix detection, temp md per member, concat into one `<uid>.md` with `---` + `<!--- file: <name>, submitted: <stamp> -->` headers, body-first ordering); scan counts top-level items; score_review resolves folder members by exact then base-uid stem,
and neglected a `fetch --mode`-free interactive override and any upload-storage dedup beyond name-based cache,
to achieve a mode-less fetch that never drops a student's content, a deterministic per-student processed document with provenance headers, and no silent stale-submission grading,
accepting that the prune is one-directional sync (absent students' stale folders are removed with their cache keys; a re-fetch is always a full declarative state), that folder member ordering is (body-first, then by name) rather than submission-time order, and that folder→folder resubmits with changed files keep only current names,
because the user asked for automatic per-type collection with folder-per-student layout and header-annotated concatenation. Verified: pytest 141 (131→141), 8/8 headless checks, real fetch auto N students, folder <uid>/ -> two-section processed md with cache-stamped headers; local dev only (commit 967683f8 on dev, remote main untouched).

## Import quick-setup + built-in Settings v2 editing + layout fixes

**Date:** 2026-09-01
**Status:** Accepted
**Files:** `src/tata_app.py`, `src/tata_settings.py`, `src/tata_rubric.py` (new), `src/aliases.py`, `tests/tata_dash_check.py`, `tests/tata_modal_check.py`, `tests/tata_rubric_check.py` (new), `tests/tata_settings_check.py`, `tests/test_aliases.py`, `plans/2026-09-01-import-setup-and-settings-v2.md`

In the context of assignment import ending at an empty config that still needed manual `e` = $EDITOR editing, settings slots `grading.rubric`/`system_prompt` being free-text inputs with no enumeration of the local libraries, rubric content creation requiring a text editor, and SettingsScreen's context Select invisible with uncontrolled row heights,
facing a post-import manual-config gap, a settings form that could silently typo library paths, and a layout blocker whose root cause looked like a large CSS problem,
we decided for a one-shot quick-setup panel (ImportAssignmentModal dismisses `(aid, name)` → AssignmentSetupModal: rubric Select over `data/rubrics/*.toml`, prompt multi-checkbox over `data/prompt/*.md`, provider Select over the registry; defaults = first / all / first; Import disabled until a prompt is checked or any library is empty), which on confirm writes `data/<course>/<aid>/config.toml` (`[grading]` via `edit_config`, header `# schema: ../../config/assignment.schema.json`) and seeds aliases (`seed_course_alias` → `data/alias.toml` `[course]`, `seed_assignment_alias` → `data/<course>/alias.toml` `[assignment]`) before launching the fetch job (course import also seeds `[course]`), plus Settings v2: dynamic `grading.rubric` Select (`rubrics/<file>`) and `system_prompt` `_PromptCheckList` (`prompt/<file>`), `(inherited)` badges on assignment-context keys not set in the local config (local keys computed from raw config, not the merged view), and a push-screen `RubricBuilderScreen` (`src/tata_rubric.py`, pushed like Review, `b` binding + Grading-tab button) editing criteria (name/desc/rating/grading/pts/custom_scale) validated by `RubricDefinition`, writing `data/rubrics/<name>.toml` and refreshing the lists on return; the layout fix turned out to be a root-cause gotcha — Textual 8.2.8 ignores the `CSS` class attribute on non-Screen widgets (SettingsScreen is a `Vertical`, not a pushed Screen), so `DEFAULT_CSS` is required (c9272e81: `CSS` → `DEFAULT_CSS`),
and neglected Canvas write-back, real-time plots, multi-job parallelism (still v2 non-goals), provider-registry editing in Settings (stays read-only display), and keeping `e` = $EDITOR as anything but a fallback path,
to achieve import-to-configured in one panel with file enums instead of typo-prone paths, in-TUI rubric authoring, and a visible, stable Settings layout,
accepting that `(inherited)` badges reflect only the local-config layer (the merged view could still differ at course/global level), that RubricBuilderScreen is a separate Screen rather than inline editing in Settings, and that the quick-setup defaults (first rubric / all prompts / first provider) may need manual adjustment on the first real import,
because the user confirmed the quick-setup panel with defaults, the RubricBuilderScreen as a separate Screen parallel to Settings, the `(inherited)` badge semantics, and the one-shot 3-phase execution (setup → write config + aliases → fetch). Verified: pytest 145 (141→145), 8/8 headless checks (settings / rubric / dash / modal / app / workspace / plagiarism / fetchall), ruff clean; lesson captured: when a flow changes, update ALL related headless checks in the same batch — the dash-check drift was only caught after P1 because the modal check had been updated alone (the batch fixed both: `tata_dash_check.py` + `tata_modal_check.py`, 4c94338c). Local dev only (commits a1642fe6, 87c3a7e1, 4c94338c, c9272e81), remote main untouched per policy.

## assignment.md + CLI rubric generate (feedback v5, 2026-09-04)

**Date:** 2026-09-04
**Status:** Accepted
**Files:** `src/shared/canvas_fetch.py`, `tests/test_canvas_fetch.py`; `src/shared/rubric_gen.py` (new), `src/shared/cli_options.py`, `src/cli/main.py`, `src/shared/grading.py` (`_build_client` → `build_client`), `tests/test_rubric_gen.py`, `tests/test_grading.py`, `README.md`

In the context of rubric authoring being manual (TUI RubricsPane or a text editor), the assignment description existing only inside Canvas (no local copy next to the fetched submissions), and grading's client builder being the private `_build_client` that sibling modules would have to import cross-module under a private name,
facing a need to keep assignment requirements offline and to bootstrap a grading rubric from that description with the same LLM provider used for grading,
we decided for `fetch` converting `assignment.description` (HTML) to markdown via `MarkItDown.convert_stream` (BytesIO + `StreamInfo(extension=".html")`) and saving it as `<assignment_dir>/assignment.md` — that is `out.parent`, beside `raw/`, not inside it; empty/None descriptions write nothing, and a conversion failure degrades to the raw HTML text and never interrupts the fetch. And for a new `rubric generate -c <assignment config.toml> [-o <out>.toml]` CLI subcommand (`RubricCliOptions`/`RubricGenCliOptions`) that reads `<assignment_dir>/assignment.md`, resolves the generator through `[grading].provider` using the publicized `build_client` (was `_build_client`; publicized because a new module needed the cross-module import under a public name, every call site in `grading.py` updated), calls instructor's `client.chat.completions.create(response_model=RubricDefinition, …)` with `RUBRIC_GEN_SYSTEM_PROMPT` (TA role, output schema, content rules: cover every major requirement, 3–10 criteria, pts sum hint), validates structure with pydantic (via instructor) plus content (`_validate_rubric_content`: ≥1 criterion, non-empty name/desc, pts > 0), and writes `[[criterion]]` TOML via tomlkit with no schema comment (same format as the TUI) to `REPO_ROOT/data/rubrics/<assignment dir name>.toml` by default,
and neglected putting assignment.md inside `raw/`, keeping the generator/`_build_client` private, silently overwriting an existing output file, and adding any schema header or extra flags to the generated rubric,
to achieve assignment requirements available offline next to the processed data and one command that bootstraps a rubrics/ file from the description using the grader's own provider and the shared `RubricDefinition` model — format-compatible with manually authored rubrics,
accepting that rubric generation requires a prior fetch (missing assignment.md raises with a "run fetch first" hint; the provider-existence check runs after the assignment.md check so the message points at the real missing input), that the default output name comes from the assignment dir name (-o renames), that a conversion failure leaves HTML in a `.md` file (documented fallback), that CLI-side `except (ValueError, FileNotFoundError)` catches only those classes (anything else propagates), and that the content check catches structural violations only — grading-judgment quality stays with the LLM,
because the user asked for saving the assignment description and automatic rubric generation in the feedback v5 batch; output-refusal, hint-after-success (`config set grading.rubric rubrics/<name>.toml`) and README Quick Start step 10 make the workflow self-servicing. Verified: pytest 197 passed, ruff check/format clean, 10/11 headless checks PASS — `tata_workspace_check.py` FAIL is pre-existing (reproduced identically on parent `0535b079` via /tmp/tata-parent, AssertionError line 52, unrelated to this batch); local dev only (commits c4fc3ff5, b833dc4c), remote main untouched per policy.

## RubricsPane Auto-generate + external-editor suspend (feedback v6, 2026-09-04)

**Date:** 2026-09-04
**Status:** Accepted
**Files:** `src/tui/rubrics_pane.py` (RubricsPane Auto-generate → `AutoGenModal`; the module split out of `library.py` in v6), `src/tui/settings.py` (`action_edit_config` suspend), `src/tui/workspace.py` (`action_edit_config` suspend), `tests/tata_library_check.py` (headless acceptance added)

In the context of the rubric library being usable in the TUI (RubricsPane) while bootstrap-generation lived only in the CLI (`rubric generate`), and both external-editor launchers (`subprocess.run(f"{editor} {shlex.quote(path)}", shell=True, check=False)` in `SettingsScreen.action_edit_config` / `AssignmentScreen.action_edit_config`) panicking the Textual input thread with `BlockingIOError` right as a full-screen editor exits,
facing a RubricsPane that would otherwise duplicate the generation pipeline, the risk of calling through CLI main (a `SystemExit` side effect from `src/cli/main.py`), and a panic whose root cause looked like a driver bug,
we decided for the TUI reusing the single source `src/shared/rubric_gen.generate_rubric` (the exact call path of CLI `rubric generate`, not via CLI main, to avoid its `SystemExit` side effects): a new Auto-generate button in the RubricsPane button row opens `AutoGenModal`, whose Select lists only assignments whose `<assignment_dir>/assignment.md` exists (.is_file()), with label = `assignment_display_name` + `(<course>/<assignment>)`; the output name matches the CLI (`data/rubrics/<assignment dir name>.toml`, cross-course same-dir-name collision kept as-is for CLI parity, so the overwrite-confirmation text names the source assignment); overwrite generates to a temporary `*.toml.tmp` and only a successful generation `os.replace`s it atomically over the target — on failure the old file survives (no pre-emptive unlink); during generation all RubricsPane writable buttons are frozen and `_autogen_running` guards re-entry (a Textual thread worker cannot be preempted, so `exclusive` is bookkeeping only). And for the editor panic we decided to wrap both `subprocess.run` calls in `with self.app.suspend():` (the officially recommended pattern), because the root cause is the editor and the Textual input thread sharing the tty: the editor sets the fd O_NONBLOCK (OFD-level) and on exit the read races with EAGAIN → `linux_driver` panic (`BlockingIOError`); measured 0/6 panics with the fix vs 2/3 on the original,
and neglected routing the TUI through CLI main, adding a second generator, touching `src/shared/rubric_gen.py` (CLI depends on it; overwrite semantics live in the TUI layer after user confirmation), fixing the upstream driver, upgrading textual (8.2.8 is current; `linux_driver` matches `main` — no upstream fix), and touching `src/shared/hooks_runtime.py`'s subprocess (`capture_output`, no tty, unaffected),
to achieve one generation path shared by CLI and TUI with atomic overwrite, and a panic-free editor round-trip,
accepting that the overwrite-confirmation naming, the pure-local `AutoGenModal` list, and the suspend fix rely on documented behavior (0/6 vs 2/3 is this batch's scope, not a full soak; upstream textual has no fix so none is pinned),
because the user asked for the auto-generate entry (①) and the panic fix (②) in the feedback v6 batch; reusing the shared generator keeps CLI/TUI output format-compatible, and `App.suspend` is the documented remedy for exactly this tty race. Verified: per plan acceptance — pytest full-run as defensive regression (no new tests beyond `tests/tata_library_check.py` headless addition, which cannot exercise the real suspend path; noted), ruff clean; local dev only, remote main untouched per policy.

## Publish self-contained provider examples and public prompt addendum (feedback v7, 2026-09-09)

**Date:** 2026-09-09
**Status:** Accepted
**Files:** `data/providers/` (ollama.toml, deepseek-v4-flash.toml), `README.md`, `docs/` (onboarding, config, faq), `src/shared/grading.py`, `data/prompt/` (system.md, lab.md), `.gitignore`

In the context of the repo not being usable from a fresh clone: the README assumed coding-agent fluency (stage-driven flow, starter-asset list missing some shipped examples), the provider examples were not reusable as shipped (the bundled sample used `markdown_json_mode` while grading always builds the client with a `response_model`, i.e. tool-call mode, and the cloud example was still named after its original `deepseek_chat_tool` role), public prompt examples were one generic file while a lab-specific addendum existed only as gitignored local state, and real course/assignment/student identifiers were scattered across public files,

facing a non-technical user who cannot infer provider config or prompt combination from the repo, and a repo that cannot be published as an example without leaking local identities,

we decided for self-contained provider examples (ollama.toml as the recommended no-key default with `mode = "tool_call"`, deepseek-v4-flash.toml renamed from its `deepseek_chat_tool` origin), promoting `data/prompt/lab.md` to a tracked public example (`.gitignore` whitelist plus a README Starter assets bullet), rewriting README for non-technical onboarding including the one-sentence coding-agent prompt chapter, redacting real course/assignment/student identifiers to placeholders (111111/222222/990019, 990001 = "Student A") in public files, and making the grader emit a friendly error for unknown providers,

and neglected expanding the prompt set beyond the single lab addendum, rewording more docs, or keeping a machine-fluent README,

to achieve install-and-run from the README alone with every shipped example valid as-is and a publishable public surface,

accepting that bundled providers must now set `mode = "tool_call"` (local qwen3.8:latest supports tool calls; a missing mode is rejected by provider config validation, while an unknown provider name surfaces via the new friendly error path) and that the cloud example rename breaks configs referencing the old `deepseek_chat_tool` name,

because the user asked for this wrap-up in the 2026-09-09 feedback batch; the rename/README/friendly-error commits are local dev only (1e37e0e, cc0d7c5, 3fcc13b), the lab.md promotion is uncommitted alongside this entry, and remote main is untouched per policy.

## Unified cache mechanism + grading hash completion + deepseek rename references (feedback v8, 2026-09-10)

**Date:** 2026-09-10
**Status:** Accepted
**Files:** `src/shared/caching.py`, `src/shared/cache_migration.py` (new), `src/shared/{canvas_fetch,grading,pipeline,plagiarism,processing}.py`, `src/tui/{scan,workspace}.py`, `tests/` (test_caching, test_cache_migration, test_grading, test_plagiarism, test_canvas_fetch, test_processing, …), `README.md`, `docs/`, `data/providers/deepseek.toml`, `HERMES.md`, data caches under `data/271218` migrated

**Y1 — Unified cache location and envelope.** In the context of TATA's cache fragmentation (4 file caches spread across 4 dirs, 3 formats, 3 read/write implementations), facing the feedback requirement that cache files live in one folder, share one format, and the common mechanism be extracted, we decided for one location `<assignment>/.cache/<stage>.json` + one envelope `{"fmt": CACHE_FMT, "data": <payload>}` + a shared module (atomic write + tolerant read + memoized `file_digest`), and neglected keeping the old locations and only extracting read/write helpers / adding a third-party cache library / folding stage products (processed md, graded json) into the scheme, to achieve single-point maintenance (format or cache changes touch one module), accepting the one-time migration cost and display/scan-layer adaptation to the small directory, because the existing `caching.py` was already a viable seed (preprocess/grading on it) — unifying was lower-risk than a rewrite.

**Y2 — Embedding freshness: mtime → content hash.** In the context of the embedding cache judging freshness by mtime only (model/config changes never triggered recomputation), facing invalidation semantics inconsistent with the other caches, we decided for input hash = sorted processed-md content digests + embedding model name, and neglected keeping mtime / adding only a model-name comparison, to achieve one semantics class (recompute only when content changes) and testability (previously zero tests), accepting one extra read of the processed md per check (cheap locally), because the feedback asked for a "common cache mechanism".

**Y3 — Grading hash completion (screenshots + hooks).** In the context of the grading hash already covering text inputs, facing the two real gaps that screenshots (when visual is on) and hooks did not enter the hash, we decided for folding screenshots (sorted per-stem glob, byte-wise digests, only when visual is on) and hooks (config + script bytes) into the hash, using the memoized `file_digest` to keep TUI polling cost independent of image size, and neglected keeping the status quo, to achieve cache correctness relative to the LLM's actual inputs, accepting that visual-evaluation assignments recompute their hashes under the new rules at migration (no unplanned re-runs), because feedback item 2 required prompt/reference and related content to enter the hash.

**Y4 — Delete `grading.checkpoint.json`.** In the context of the grade checkpoint participating in no decision or display since 09-08 (append-only dedup), facing docs that still told users to delete it (existing in name only), we decided for deleting its read/write paths and the stored files, and neglected keeping it for audit / resume-only, to achieve a single state source, accepting the loss of the "historical done list" (reconstructable from `graded/`), because feedback item 1 asked for easier future maintenance.

**Y5 — Migration strategy: snapshot diff + production-rule recomputation.** In the context of cache location/envelope changes altering the hash input combinations of preprocess (including fetch-entry JSON) and grading (digest computation), where a direct migration would mean full re-runs, facing asymmetric rerun cost (grade = LLM-expensive), we decided for snapshotting the "old-rule valid sets" before migrating and, during migration, recomputing hashes under the new rules (by calling the production functions) only for the valid sets while dropping the rest, and neglected invalidating everything / copying old hashes verbatim, to achieve a post-migration valid set exactly equal to the pre-migration one (verifiably no re-runs), accepting that the migration tool reuses production functions (cross-module private names promoted to public: `grading_pending`, `load_assignment_config`, `preprocess_item_hashes`), because user-data re-run cost is high and the "back up + verify before touching real data" convention applies.

**Y6 — deepseek reference update scope.** In the context of the provider rename (file stem = `deepseek`, model = `deepseek-flash`), facing 16+ stale references (including dead links and stale fixtures), we decided for updating tracked docs/tests wholesale + cleaning up the stale HERMES.md config examples, leaving history records (DECISIONS/plans) untouched, and neglected adding legacy-name compatibility aliases, to achieve single-source references (file name = provider name) and usable docs, accepting that legacy names fail to resolve (already covered by the friendly error), because the user was explicit that the old model name is invalid and that prior config content should be removed.

## TUI v9: settings level scoping + plagiarism fullscreen view + workspace status row (feedback v9, 2026-09-10)

**Date:** 2026-09-10
**Status:** Accepted
**Files:** `src/tui/{app,settings,workspace,plagiarism,icons,rubrics_pane}.py`, `src/tui/styles/{app,settings}.tcss`, `tests/tata_{app,dash,settings,workspace,plagiarism,library,realtime}_check.py`, `plans/designs/*` + `HERMES.md` (docs sync), plan `plans/2026-09-10-feedback-v9.md`

**Z1: Settings push + level scoping.** In the context of the Settings tab keeping a stale course context (the old `_load_context` refreshed its options only when the option value set changed, and a cross-course switch kept the same `{global, course}` set, so labels stayed pinned to the previous course), facing a whole bug class rather than a one-off, we decided for a rework in which `SettingsScreen(Screen)` is constructed with `(state, ctx, pop_on_escape=True)`, composes only that level's tabs (global/course: Canvas + Plagiarism; assignment: Grading + Plagiarism + Paths), and is opened via the new `[⚙ Settings]` button in `#dash-actions` at every dashboard level plus the `,` key, and neglected a context dropdown, a `force` refresh flag, or keeping the tab. The stale-context bug class is gone by construction (an instance can only edit the layer it was built for); the `_ctx_manual` / `available_contexts` / `context_options` / `_writable` / `global_blocked` machinery was deleted with it, to achieve one uniform entry point whose target is explicit in the header and the `Will write:` line, accepting that the screen is single-level by design (switching levels means closing and reopening from the target level), because the user asked for per-level editing with no dropdown and cache invalidation could not fix a comparison that was structurally blind to course identity.

**Z2: Plagiarism fullscreen view.** In the context of the embedded pane permanently costing the course view about 60% of its height (the course table was pinned at 40% with 13 blank rows), facing feedback item 4, we decided for `PlagiarismViewScreen(Screen)` wrapping the existing pane, pushed by the course-level `p` / `[Plagiarism]` button, with `esc` popping back but refused while a job runs (notify tells the user to press `x` first), and neglected keeping the embed or promoting the pane to a shell tab. The course table is `1fr` again and gained a `Flagged` column from the already-computed `flagged_pairs` (0 shows a dim `-`, positive shows a red bold count), to achieve a full-height plagiarism workspace and a course view that uses its screen, accepting that the pane is one push level deeper (details stack inside) and that the refusal guard exists because unmounting mid-job strands the JobHost drain timer and `state.active_job`, because the user asked for the pane out of the course view and the score-review screen established push as the platform pattern.

**Z3: Workspace pending-status row.** In the context of the hidden `#ws-incr` line (`i` toggle) reading as unclear and the `Pipeline · <name>` topbar prefix duplicating the breadcrumb, facing feedback item 3, we decided for an always-visible `#ws-status` row, `✓ fetch · N preprocess pending · N grade pending · N score pending · analyze OK|Not run`, colour-coded (>0 yellow, 0 green, not-fetched and analyze-not-run dim) from the same shared cache rules the stages run, and `height: 4` stage buttons with Nerd Font icons and visible subtitles (`score review` = `view scores`), and neglected keeping an expandable incremental summary. `#ws-topbar` is now `ID · badge · last run`; the `Pipeline` prefix, the `i` binding and `_incremental_line` are deleted, to achieve colour-coded pending counts that cannot disagree with a run, accepting that the row wraps on narrow terminals (height auto), because the user called the old wording unclear and asked exactly for these counts.

**Z4: Rubric alias as filename identity.** In the context of auto-generate always writing `data/rubrics/<assignment dir name>.toml`, facing feedback item 1, we decided for an optional alias input on `AutoGenModal` (`#ag-alias`, placeholder `alias (optional) - default: assignment ID`) whose value becomes the output filename after `validate_name(alias, ".toml")` (invalid values notify and keep the modal open; blank falls back to the assignment directory name, i.e. the assignment ID), and neglected adding a name/alias field to the generated TOML or touching the CLI (its `-o` already covers custom names) and `src/shared/rubric_gen.py`. Filename stays the identity; the modal dismisses `(config_path, alias)` and the overwrite / tmp-file atomic replace / freeze logic is unchanged, to achieve per-assignment output naming from the TUI, accepting that alias uniqueness is only enforced by the overwrite confirmation, because that mirrors the CLI's existing semantics.

**Z5: Nerd Font icons centralised in icons.py.** In the context of no PUA glyphs existing anywhere and feedback item 5 asking for Nerd Font usage, we decided for one module `src/tui/icons.py` holding the Font Awesome range constants (`GEAR`, `FETCH`, `PREPROCESS`, `GRADE`, `SCORE`, `ANALYZE`, `REVIEW`, `PLAGIARISM`, `OK`, plus `PENDING` / `FAIL` which are currently reserved), consumed by the dash-actions buttons, the stage buttons and `#ws-status`, and neglected sprinkling glyphs at call sites. Swapping terminal fonts is a one-file edit, and UI copy stays English, accepting module-level constants with no runtime font detection, because a single swap point was the point of the ask.

**Z6: Topbar de-duplication and panel framing.** In the context of the topbar carrying a level chip plus `Courses: N` / `Filter: X`, and several surfaces having no borders, we decided for `#topbar` showing only `TATA · Canvas: OK` / `TATA · Canvas: ? (.env missing)` (level identity lives in `#breadcrumb`; `1-4` still notify their filter and the Footer lists the keys — course level only since v10 item 6, when footer listings became level-scoped — so the removals lose no information) and for the border contract: panels `round $primary` (including the plagiarism view's topbar, tabs and log), buttons `round $panel`, modals `heavy $primary`, and neglected per-screen border variants. The reclaimed space lands as `1fr` tables/lists (the course table measured 24 rows at 120x40 when this decision landed; 25 since v10 item 1 merged the action row into the breadcrumb line; the plagiarism log is fixed at height 8), to achieve a single visual language and full-height lists.

**Verification (docs-sync re-run, 2026-09-10):** `uv run pytest -q` 274 passed (1 warning, 100.38s); `just test-e2e` 12/12 scripts OK; `uv run ruff check .` clean; `uv run ruff format --check .` 79 files already formatted; wireframe validator ALL CLEAN on `plans/designs`; mdformat clean on the touched markdown. Local dev only; remote untouched per policy.

## TUI v10 (batch 1): inline dashboard action row + dim stage subtitles + level-scoped footer keys (feedback v10, 2026-09-11)

**Date:** 2026-09-11
**Status:** Accepted
**Files:** `src/tui/app.py`, `src/tui/workspace.py`, `src/tui/styles/app.tcss`, `tests/tata_{dash,workspace}_check.py`, docs sync (`plans/designs/{01-dashboard,02-pipeline,99-design-system}.md`, `HERMES.md`)

In the context of the dashboard action row (`[⚙ Settings]` + `[Plagiarism]`) taking a full line below the breadcrumb with the buttons left-aligned, the six stage-button subtitles rendering in the same colour as their main labels, and the Footer (and `?` help panel) advertising keys that do nothing at the current dashboard level (`1-4` state filters plus `F`/`p`/`s` at the global level, `c` at the assignment level),
facing feedback v10 items 1/2/6,
we decided for wrapping `#breadcrumb` and `#dash-actions` in one `Horizontal#dash-head` (`#dash-actions` `width: auto` hugs the right edge of the row; `#breadcrumb` `width: 1fr; height: 3; content-align-vertical: middle` centres its text on the button line), rendering the stage subtitles as label-inline `[dim]` markup (`escape`d; Textual 8.2.x `Button.label` parses Rich markup and the main line keeps the button's normal colour), and a `DashboardScreen.check_action` that returns `False` (disabled + hidden from footer/help) for `filter_*`, `fetch_all`, `open_plagiarism` and `score_review` outside the course level and for `import_item` at the assignment level, with `render_level()` calling `refresh_bindings()` (the Footer only recomputes on focus changes or an explicit refresh, so `check_action` alone left the course-level `1-4` keys missing on first entry); the unreachable `AssignmentScreen.action_rescan` dead method (no binding, no caller) was deleted,
and neglected docking/spacer/`align-vertical` alignment (has no effect on horizontal children and no dock precedent exists in the repo), a `.secondary` child class or custom button rendering for the subtitle colour, and any change to the workspace's `p`/`s`/`a`/`F` shadowing or to the `_set_filter` guard (kept as defence in depth),
to achieve a one-line breadcrumb+actions header with flush-right buttons, a subtitle line that reads as secondary, and a Footer that only lists keys that work where the user is,
accepting that `#dash-head` must keep `height: auto` (Horizontal defaults to 1fr and would clip the row), that the breadcrumb is clamped to 3 rows (a very long alias in a narrow terminal may wrap-clip — the fixed height keeps the buttons aligned), and that the dim subtitle contrast presents differently while a stage button holds focus (Textual's focus style inverts the whole label; behaviour unchanged).

**Verification (batch 1):** `uv run python tests/tata_dash_check.py` / `tests/tata_workspace_check.py` / `tests/tata_app_check.py` all OK; `uv run pytest -q` 274 passed, 1 warning (98.55s); probe-verified at 120x40 and 80x24 (breadcrumb text and button text on the same painted line, actions flush right, footer keys level-scoped). Items 3/4/5 of v10 (command palette, progress layout, cancel propagation) are handled by other batches.

## TUI v10 (batch 2): cooperative cancel propagation + progress row below the log (feedback v10, 2026-09-11)

**Date:** 2026-09-11
**Status:** Accepted
**Files:** `src/tui/{jobs,workspace,plagiarism,app}.py`, `src/shared/{pipeline,grading,scoring,analysis,fetch_pipeline,plagiarism}.py`, `src/tui/styles/app.tcss`, `tests/{tata_workspace_check,tata_plagiarism_check,tata_dash_check,tata_fetchall_check,test_processing,test_grading,test_plagiarism}.py` + new guard `tests/test_job_cancel.py`, docs sync (`plans/designs/02-pipeline.md`, `HERMES.md`, `README.md`)

**AA1 — `cancel_event` becomes part of the stage-callable contract (item 5).** In the context of the job worker never passing the cancel event into the stage function (`run_stage_worker` called `fn(config_path, **kwargs)` and only relabelled the result afterwards, so a cancel let every loop run to completion — grade even submitted the whole pending batch to its thread pool first), facing design 02 §4's promised "current file finishes, no new tasks start" semantics that had never been implemented, we decided for passing `cancel_event=job["cancel_event"]` to every stage function and checking it at each item-loop boundary (preprocess/score/fetch/analyze plus plagiarism per-assignment and per-submission loops; grade additionally short-circuiting before submit and calling `executor.shutdown(cancel_futures=True)` at the next result boundary so queued submissions are dropped), with every callable handed to `_start_job` gaining the keyword-only `cancel_event: threading.Event | None = None` parameter (workspace wrappers, the four shared stage functions, the two plagiarism wrappers, the dashboard's `_fetch_one`/fetch-all closure) and a machine guard (`tests/test_job_cancel.py`, AST-scans all `_start_job` call sites and checks the resolved signatures) so a future stage callable cannot silently miss the parameter, and neglected `worker.cancel()` (cannot kill a thread) / a global flag / per-loop interrupts, to achieve user-visible stopping at item granularity with the honest ceiling that calls already in flight finish (≤`max_parallel_tasks` LLM calls; `detector.run()` and single Canvas fetches are atomic library calls), accepting that results completed after the cancel point are dropped uncollected (the `.cache/` hash rules re-process them on the next run) and that the dashboard fetch-all path accepts but has no producer for the event (no cancel UI at that level; signature conformance only), because the shared hash caches make stopping cheap to resume and the old behaviour — waiting out the entire run after pressing Cancel — was the reported defect.

**AA2 — progress row below the log, stretched `Bar`, elapsed + native ETA (item 4).** In the context of the workspace progress row (text + `ProgressBar` + Cancel) sitting above the RichLog while users watch the log, and Textual's inner `Bar` defaulting to `width: 32` so the bar rendered as a 32-cell stub inside a 1fr track, facing feedback item 4, we decided for moving `#ws-progress` after `#richlog`, adding `#ws-progress > ProgressBar > Bar { width: 1fr }`, `show_eta=True` (Textual's native `ETAStatus`, hidden via `display` while `total` is unknown so fetch/analyze show no `--:--:--` placeholder) and an elapsed `mm:ss` clock in `#ws-progress-text` (job dict gains `started_at`; the 0.1 s drain tick repaints only on integer-second changes), and neglected a hand-rolled ETA formula, `bar.update()` value writes (the reactive setters already feed the ETA sampler; `update()` would double-add samples), and shrinking the Cancel button's 3-row footprint, to achieve a full-width bar with remaining-time feedback and a truthful stopwatch, accepting that at 100x30 mid-job the 3-row bar row costs the 1fr log its single content row (the frame stays; measured in the short-window check — the layout itself never clips off-screen), because the requested layout puts the bar under the log and the repository's 100x30 policy forbids clipping, not row squeezing.

**Verification (batch 2):** `just test-e2e` 12/12 headless check scripts all OK (incl. `tests/tata_workspace_check.py` / `tests/tata_plagiarism_check.py` / `tests/tata_fetchall_check.py` / `tests/tata_dash_check.py`); `uv run pytest -q` 279 passed, 1 warning (95.85s); `uv run ruff check .` all checks passed; `uv run ruff format --check .` 80 files already formatted; `uvx -w mdformat-gfm mdformat --check --number README.md docs/**/*.md` (the `just check` target) clean.

## TUI v10 (batch 3): global ctrl+p command palette dispatching each view's actions (feedback v10, 2026-09-11)

**Date:** 2026-09-11
**Status:** Accepted
**Files:** `src/tui/{commands.py (new),app.py}`, `tests/tata_palette_check.py` (new), `justfile`, docs sync (`plans/designs/{00-ia,01-dashboard,02-pipeline,03-review,04-plagiarism,05-settings,99-design-system}.md`, `plans/2026-09-11-feedback-v10.md`, `HERMES.md`, `README.md`)

In the context of TATA's key map being per-view (dashboard global/course/assignment, workspace, settings, plagiarism view, score review) with no searchable entry point, and Textual 8.2.8 shipping a native palette (`ENABLE_COMMAND_PALETTE=True`, `ctrl+p` priority) whose providers are App-level only and receive the calling screen (`provider.screen = app.screen_stack[-2]`, verified in-app for all eight contexts),
facing feedback v10 item 3 (global command palette, fuzzy search, dispatch the current context's actions),
we decided for one App-level provider `src/tui/commands.py::TataCommands` registered as `TataApp.COMMANDS = {*App.COMMANDS, TataCommands}` (the spread keeps Theme/Quit/Keys/Screenshot), whose `_entries()` returns `(context help, rows)` of `(name, key hint, target, action)` per calling screen — modal→none; Settings/Plagiarism view (`query_one(PlagiarismScreen)` inside the pushed `PlagiarismViewScreen`)/Score review→that view's keys; dashboard shell→`state.dashboard_level` rows (workspace stages f/p/g/s/a + score-review button/x/e/shift+f; global c/`,`/r; course + shift+f/p/s/1-4) — each command running `partial(_invoke, target, action)` → `target.action_<action>()` with zero new logic, plus `Binding("ctrl+p", "command_palette", "Palette", show=False, priority=True)` in `TataApp.BINDINGS` (Textual dedups by action name so it replaces the default injection; `show=False` because Textual always pins its own palette FooterKey — `show=True` rendered the hint twice, measured),
and neglected state-based disabling of gated commands (they behave exactly like their keys, including the notify paths — no over-filtering), Library-tab commands (Library panes are a separate surface; system commands only for now), and any submit-class command (Canvas write-back is a v1 non-goal),
to achieve a global fuzzy-searchable palette whose hits are exactly the actions of the view the user is in, executed through the same entry points the keys use,
accepting that unknown screens (modals, Library tab, plagiarism detail screens, the CLI shell) degrade to the system commands only, that `help` is the context label ("Dashboard · Course", "Assignment workspace", …) rather than a prose sentence, and that a loop-built late-bound lambda would silently run the last entry's action (guarded by `partial` and by the new check's spy).

**Verification (batch 3):** `uv run python tests/tata_palette_check.py` OK — ctrl+p opens/esc closes (also with the search Input focused), per-context discovery rows for global/course/assignment/settings/plagiarism/score-review + modal/Library degrade to system-only, "fet" spy proves the hit runs its own action (`['run_fetch']`), "gra" raises the Grade confirmation modal and esc leaves `ws._job is None`, the Footer's pinned Palette key appears exactly once; two mutations self-proved the check (late-bound lambda → `['toggle_config']`; missing `*App.COMMANDS` → system-command assertion fails); `just test-e2e` **13/13** scripts OK; `uv run pytest -q` **279 passed**, 1 warning (96.17 s); `uv run ruff check .` all checks passed; `uv run ruff format --check .` **82 files already formatted**; wireframe validator on `plans/designs` **ALL CLEAN**; mdformat clean on every touched doc except `plans/designs/02-pipeline.md` (pre-existing dirt, not reformatted).

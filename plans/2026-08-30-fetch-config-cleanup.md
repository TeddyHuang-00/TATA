# 2026-08-30 fetch config cleanup + 0-submission fix

## Root cause (verified, live repro)

`uv run python main.py fetch --retry 271218 2978557` → `text: 0 submissions -> .../2978557/raw/`.

Course config `data/271218/config.toml` sets `[fetch] mode = "text"` (course level), and every
`[[fetch.assignments]]` entry without its own `mode` falls back to it. Modules 2978557 (0.10,
ipynb uploads), 2979480 (1.6, ipynb), 2979482 (1.7, docx) are **file uploads** — text mode reads
`sub.body`, which is empty for uploads → 0 files. Text modules (2979509/2979511/2979512) are
`online_text_entry` and DO carry bodies (verified via Canvas API: 56/56 with body>0).

Fix = config cleanup below (per-entry modes + course default `auto`), NOT new fetch logic.

## New config shape

Course config `data/<course>/config.toml` (single fetch source of truth):

```toml
[fetch]
course_id = 271218
mode = "auto"          # course default; per-entry mode overrides

[[fetch.assignments]]
id = 2978557

[[fetch.assignments]]
id = 2979509
mode = "text"          # only when it differs from the course default
```

- Entry key `assignment_id` → `id`. NO `out` key anywhere: fetch output dir is always
  `<course_dir>/<id>/raw` (derived, not stored). Entry `mode` optional (None → course mode).
- `[fetch]` is REMOVED from assignment configs (`data/<course>/<id>/config.toml`). No
  assignment_id/out_dir keys; assignment identity = the dir name (int).

## Code changes

### src/assignment_config.py
- `FetchAssignmentEntry` → `id: int`, `mode: Literal[...] | None = None`. Delete `out`.
- `FetchSection` → keep `course_id`, `mode` (default "auto"), `assignments`. DELETE
  `assignment_id`, `out_dir`, `resolve_out_dir`.
- Everything else (layered load, container detection) unchanged; `load_assignment_file` still
  strips `assignments` from merged fetch.

### src/canvas_fetch.py
- Replace `remember_fetch(config_path, *, course_id, assignment_id, out_dir, mode)` with a
  course-config-only writer, e.g. `remember_course_fetch(config_path, *, course_id=None,
  entry: tuple[int, str | None] | None = None)`:
  - tomlkit field-level upsert of `[fetch].course_id` (existing keys preserved);
  - `entry=(aid, mode)` appends `{id = aid, mode = "text" if mode and mode != "auto"}` to
    `[[fetch.assignments]]` (AOT), deduped by `id` (existing entry wins, untouched);
  - never writes mode into `[fetch]` (course mode is user-set default; per-assignment modes
    live on entries — this is what prevents the 0-submission regression from recurring).
- `fetch_assignment` unchanged (already writes `<uid>.html`; mkdirs out with parents).

### src/cli.py
- `_fetch_entries`: `out = (cfg_path.parent / str(entry.id) / "raw").resolve()`; mode =
  `entry.mode or cfg.mode`.
- `_run_fetch`:
  - delete `_remember`'s assignment-config path and `out_dir` writes; `_remember` now:
    determine course config (container path itself, else `find_root_config`), then
    `remember_course_fetch(course_cfg, course_id=..., entry=(aid, mode))`.
    No no-course-config branch that writes assignment configs; if no course config exists,
    skip remembering and print a hint (ad-hoc fetch).
  - back-compat: `cfg.assignment_id` no longer exists. Assignment id resolution:
    args.assignment → else `int(cfg_path.parent.name)` if numeric (assignment-config fetch)
    → else interactive pick (tty) / exit with hint (non-tty).
  - `_pick_interactive` (no config at all): default out = `cwd/<aid>/raw`; prints hint about
    course config after fetch (no remember).
  - Delete `_resolve_out` and the `--out`-related branches. FetchCliOptions loses `out` (see
    below), so `--out` no longer exists — remove the root-list warning's `--out` mention
    (keep `--mode ignored with a list` warning).
  - `_retry_fetch`: DELETE the per-assignment-config fallback (assignment configs no longer
    carry [fetch]); everything goes through course config lists. Update the no-target
    sys.exit message.
- `_classify_config` unchanged (merged fetch now course-level only).

### src/cli_options.py
- `FetchCliOptions`: delete `out`; drop the `retry + out` validator clause (keep
  course/assignment together check).

### src/plagiarism.py (detect_plagiarism, root-list branch)
- `listed = [resolved.parent / str(entry.id) for entry in root_fetch.assignments]`
  (was `(resolved.parent / entry.out).parent`).

### src/aliases.py (migrate_course_to_ids — one-time migration, already used)
- `_patch_out_entries` → normalize entries instead of patching `out`: for each
  `[[fetch.assignments]]` entry, `assignment_id` → `id`, drop `out`.
- `_fetch_assignment_id(config_path)` reads assignment-config `[fetch]` — assignment configs
  no longer have it. Rewrite: child dir already numeric → done; else look up the entry `id`
  in the course config list (or skip with a warning when absent).

### TUI
- src/tata_scan.py: `AssignmentInfo.assignment_id` from dir name
  (`int(entry.name)` when `entry.name.isdigit()`, else None) — delete `_fetch_id(cfg,
  "assignment_id")` call; `_fetch_id` stays for `course_id` (course configs).
- src/tata_workspace.py `action_run_fetch`: gate on `cfg.fetch is None or
  cfg.fetch.course_id is None` ("Course not configured for fetch — add a [fetch] course_id
  to the course config"), not assignment_id.
- src/tata_app.py:
  - `ImportAssignmentModal`: drop `#modal-out` Input and `on_select_changed` prefill;
    `_do_import` dismisses `(aid, mode)`; dir-exists check on `course_dir / str(aid)`.
  - `_on_assignment_imported`: unpack `(aid, mode)`; `_fetch_one(course, aid, mode)`:
    `FetchCliOptions(course=course.course_id, assignment=aid, config=course.config_path,
    mode=mode)` (no out) and M3 append `{"id": aid}` (+ `mode` if != "auto") — replace the
    manual tomlkit list append logic to dedupe on `id`.
  - `_fetch_all_section`: docstring; `_on_fetch_all_confirmed`:
    `assignment=entry.id`, `mode=entry.mode or cfg.mode`, NO `out`; label source =
    `str(entry.id)`.
- src/tata_settings.py: `[[fetch.assignments]]` summary line → `entry.get('id')` +
  optional mode (no `out`).

## Tests

- tests/test_fetch_cli.py: new entry shape (`{"id": 11}`, `{"id": 12, "mode": "text"}`), no
  `out`/`--out` assertions; fixtures without assignment-config `[fetch]` (course config
  shape); assert `_run_fetch` derives `<id>/raw` out (stub canvas or smoke via `--retry`
  path is fine as long as the intent is covered).
- tests/test_layered_config.py: `cfg.fetch.assignment_id` assertions → dir-name-based
  semantics; assignments list shape `id`/no `out`; entry strips still asserted.
- tests/test_canvas_fetch.py: `remember_fetch` → `remember_course_fetch` semantics.
- tests/test_aliases.py: migration fixture new shape (no `out`, `id` keys).
- tests/test_plagiarism.py: remove `[fetch] assignment_id` from assignment-config fixture;
  keep root `[fetch]` fixtures.
- tests/e2e_common.py: `entries` → `id`-based (no out); assignment configs WITHOUT `[fetch]`;
  update comments.
- tests/tata_fetchall_check.py: assert no `out` in FetchCliOptions calls; assert
  `assignment == aid`; label assertions may change.
- tests/tata_dash_check.py: `arg.out == "777/raw"` assertions → `arg.assignment == 777`
  (out no longer passed); `[fetch] assignment_id` fixtures → course-config shape.
- tests/tata_plagiarism_check.py: assignment config snippets drop `[fetch] assignment_id`.
- tests/test_tata_scan.py: assignment_id from numeric dir names.
- Add/extend a regression test: fetch of an upload module under course mode "text" must NOT
  silently report success — covered via config semantics (per-entry mode); at minimum update
  affected fixtures. Keep scope tight; NO new fetch logic beyond what's above.

## Data migration (gitignored, local)

1. `data/271218/config.toml` → new shape above:
   - `[fetch]` course_id 271218, `mode = "auto"`;
   - 6 entries with `id` = current aid, NO `out`;
   - per-entry `mode = "text"` on 2979509, 2979511, 2979512 only.
2. Strip the `[fetch]` block (incl. its comment) from all 6
   `data/271218/<aid>/config.toml` (2978557, 2979480, 2979482, 2979509, 2979511, 2979512).
3. `data/config.toml` (global): keep as base layer (course_id/mode defaults) — untouched
   except its comment stays accurate (no code change needed).
4. NOT in git (data/* gitignored) — implement via a small python/tomlkit script in the
   implementer credential-dir task, dry-run first + verify each file parses.

## Docs / schema / example

- README.md fetch section (~L100-140): new config shapes, no `--out`.
- docs/config/assignment.md: [fetch] section example + assignment-config example updated;
  note fetch settings are course-config-only.
- docs/design/01-dashboard.md L209 (out 值不变 → id 派生) + 05-settings.md L53/L74
  (list summary shows id/mode) — keep design docs consistent.
- data/example/config.toml: remove the `[fetch]` comment block; replace with a one-line note
  that fetch settings live in the course config (`data/<course>/config.toml`).
- Regenerate schemas if tracked (`uv run python main.py schema`; check git status — if
  config/assignment.schema.json is tracked, commit the regen).

## Acceptance criteria

- `uv run pytest -q` — 118 baseline → all green (updated fixtures, same or more).
- `just check`-style: `ruff format --check . && ruff check .` clean.
- e2e check scripts run one at a time (`uv run python tests/tata_*_check.py` each).
- Live: `uv run python main.py fetch -c data/271218/config.toml` prints per-module counts
  ≈ 56/55/54/56/53/54 (`attach` for uploads, `text` for text modules) — NO 0.
- `uv run python main.py fetch --retry` still works; `plagiarism -c data/271218/config.toml`
  root-list branch still resolves assignment dirs (smoke: `--aggregate` dry or unit).
- TUI `tata_app_check.py`/`tata_workspace_check.py`/`tata_modal_check.py` still pass (import
  assignment modal, fetch-all, workspace fetch gate).

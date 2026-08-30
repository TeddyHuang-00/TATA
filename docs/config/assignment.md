# Assignment Config Format

This guide explains how to write `data/<assignment-name>/config.toml` without relying on schema validation.

## Layered config

Configs are layered (three levels). The global base config `data/config.toml`
holds defaults shared across courses (`[fetch]` course_id,
`[plagiarism]` settings) but no assignment list; each course config
`data/<course>/config.toml` holds course-level fetch state plus the course's
assignment list; each `data/<course>/<assignment>/config.toml` (this file)
holds assignment-specific settings. Per key, assignment values win, then
course, then global. All paths resolve against the assignment directory
(global/course values are scalars only). The legacy two-level layout
(`data/config.toml` + assignment configs directly under it, assignment list
in the global file) still works as an abbreviation.

- Global base layer (`data/config.toml`, gitignored): `[fetch]` defaults
  (`course_id`) and `[plagiarism]` course-wide knobs (blend
  weights, aggregate alphas). It has no `[grading]` and no assignment list.
- Course layer (`data/<course>/config.toml`, gitignored): course `[fetch]`
  state (`course_id`) plus the course's assignment list
  (`[[fetch.assignments]]`: `id` only — fetch auto-collects every submission
  type per student; the fetch output dir is always
  `<course dir>/<id>/raw` — derived, never stored).
- Assignment layer: everything else (grading, processing, hooks, scoring),
  plus overrides (`template_file`, ...). No `[fetch]` here — the assignment
  identity is the numeric dir name.
- Standalone assignment configs without a global/course config still fetch:
  the assignment resolves via `--course`/`--assignment` or interactively.
  Nothing is remembered — fetch memory is written only into a course config.

The course list is the course's source of truth: `main.py fetch -c data/<course>/config.toml` fetches every listed entry in one shot (fetch collects
the body text and all attachments per student; a multi-file student is
written to `raw/<uid>/`, a single-file student flat at `raw/<file>`),
`fetch --retry` replays it, and `main.py plagiarism -c data/<course>/config.toml --aggregate` runs and aggregates exactly the listed
assignments. `main.py fetch` also writes course-level state to the course
config automatically.
With `-c data/config.toml` (no assignment list) plagiarism falls back to the
discovered course configs (`data/*/config.toml`).

Example course config:

```toml
[fetch]
course_id = 271218

[[fetch.assignments]]
id = 2979511
```

## Where this file sits in the workflow

The assignment config is the runtime contract for every stage:

1. `plagiarism`
2. `preprocess`
3. `grade`
4. `score`
5. `analyze`

`main.py` uses this file to decide where input/output files live, how preprocessing behaves, which rubric/prompt/provider to use for grading, and how plagiarism/scoring outputs are produced.

## Minimal valid config

Only `[grading]` is required.

```toml
[grading]
rubric = "rubrics/example_rubric.toml"
system_prompt = ["prompt/system.md", "prompt/lab.md"]
provider = "deepseek_chat_tool"

# [assignment]
# reference_file = "reference.md"  # optional — omit for rubric-only grading
```

## Full template

```toml
[assignment]
name = "assignment"
raw_dir = "raw"
processed_dir = "processed"
graded_dir = "graded"
logs_dir = "logs"
reference_file = "reference.md"

# [fetch] is course-config-only (data/<course>/config.toml: course_id +
# [[fetch.assignments]] id entries). Assignment configs carry no [fetch];
# the assignment id comes from the numeric dir name.

[processing]
# Optional: one value or a list from: ipynb, html, markdown
# input_format = ["ipynb", "html", "markdown"]
remove_base64_images = true
clean_filenames = true
strip_canvas_suffix = true
strip_html_callouts = true
strip_html_div_tags = true
strip_html_escaped_backslashes = true
strip_html_style_blocks = true
convert_html_tables_to_markdown = true
strip_colab_dataframe_widgets = true
strip_html_script_tags = true
strip_html_button_tags = true
strip_html_svg_tags = true
normalize_dtype_label_html = true
remove_nbconvert_assets = true
# nbconvert_template = "classic"
# nbconvert_template_dir = "templates/mdoutput"

[hooks]
dir = "hooks"

[hooks.mounts]
# Mount values can be one script path or a list of script paths.
# before_preprocess_file = "extract_todo_code_snippets.py"
# after_preprocess = ["cleanup_preprocess_temp.py"]

[grading]
rubric = "rubrics/example_rubric.toml"
system_prompt = ["prompt/system.md", "prompt/lab.md"]
provider = "deepseek_chat_tool"
max_parallel_tasks = 10

[scoring]
report_detail = "full" # full | slim
output_style = "markdown" # markdown | plain | html

[plagiarism]
output_dir = "plagiarism"
template_file = "template.ipynb"
submissions_subdir = "submissions"
template_subdir = "template"
report_file = "report.html"
full_pairs_file = "all_pairs.json"
display_threshold = 0.8
extensions = [".py"]
include_python_files = true
```

## Section-by-section behavior

## `[assignment]` paths and identity

Purpose in workflow:

- Controls where each stage reads inputs and writes outputs.
- These folders are auto-created if missing when stages run (`raw/processed/graded/logs`).

Fields:

- `name` (default `assignment`): logical assignment name used in metadata/logging context.
- `raw_dir` (default `raw`): input submissions location for preprocessing/plagiarism.
- `processed_dir` (default `processed`): preprocessed markdown outputs consumed by grading.
- `graded_dir` (default `graded`): grading JSON output location consumed by scoring/analyze.
- `logs_dir` (default `logs`): stage logs/checkpoint/meta-analysis files.
- `reference_file` (default `reference.md`, optional): reference answer file for comparison grading. Omit to grade against rubric criteria alone.

Accepted value type:

- All fields are strings (relative paths recommended).

## `[processing]` preprocessing behavior

Purpose in workflow:

- Governs how raw notebooks/html/markdown are cleaned and normalized before grading.

Important fields:

- `input_format`: one of `ipynb|html|markdown` or a list; if omitted, preprocessing auto-detects.
- Remaining flags: boolean toggles that enable/disable specific cleanups.
- `nbconvert_template` and `nbconvert_template_dir`: optional strings for nbconvert behavior.

Effect:

- Changes here can materially alter grading input text and therefore model outcomes.

## `[hooks]` and `[hooks.mounts]` lifecycle extensions

Purpose in workflow:

- Lets you inject custom scripts at stage lifecycle points (preprocess/grade/score/analyze/plagiarism).
- Full lifecycle and payload reference: [hooks.md](hooks.md)

Fields:

- `hooks.dir` (default `hooks`): base folder containing hook scripts.
- `hooks.mounts`: map from mount point to script or list of scripts.

Accepted mount names:

- `before_preprocess`
- `before_preprocess_file`
- `after_preprocess_file`
- `after_preprocess`
- `before_grade`
- `before_grade_submission`
- `after_grade_submission`
- `after_grade`
- `before_score`
- `after_score`
- `before_analyze`
- `after_analyze`
- `before_plagiarism`
- `after_plagiarism`

Rules:

- Mount value must be either:
  - one non-empty string, or
  - a non-empty list of non-empty strings.

## `[grading]` model-evaluation settings (required)

Purpose in workflow:

- Drives LLM grading behavior.

Fields:

- `rubric` (required string): rubric TOML path.
- `system_prompt` (required string or list of strings): prompt file path(s) combined during grading.
- `provider` (required string): provider key from `config/provider.toml`.
- `max_parallel_tasks` (optional int, default `10`, range `1..10`): grading concurrency.

## `[scoring]` report generation

Purpose in workflow:

- Controls how `score` stage renders grading JSON into human-readable reports.

Fields:

- `report_detail`: `full` (default) or `slim`.
- `output_style`: `markdown` (default), `plain`, or `html`.

## `[plagiarism]` copydetect outputs and behavior

Purpose in workflow:

- Controls where plagiarism artifacts are written and what file types are included.

Fields and defaults:

- `output_dir = "plagiarism"`
- `template_file = "template.ipynb"`
- `submissions_subdir = "submissions"`
- `template_subdir = "template"`
- `report_file = "report.html"`
- `full_pairs_file = "all_pairs.json"`
- `display_threshold = 0.8` (must be in `[0.0, 1.0]`)
- `extensions = [".py"]` (cannot be empty)
- `include_python_files = true`
- `copydetect_weight = 0.95` / `embedding_weight = 0.05`: text-submission
  blend (copydetect primary, embedding auxiliary)
- `embedding_model = "jinaai/jina-embeddings-v5-omni-small-text-matching"`
- `pairwise_alpha = 0.01` / `individual_alpha = 0.01`: one-sided significance
  thresholds for the cross-assignment aggregate
- `score_floor = 0.001` / `score_cap = 0.999`: logit clamps for the aggregate

Notes:

- `full_pairs_file` is used by the cross-assignment aggregate
  (`main.py plagiarism --aggregate`).
- `extensions` are normalized to lower-case, and a missing leading `.` is auto-added.

## Common mistakes

- Missing `[grading]` section.
- Empty `grading.system_prompt` list.
- `max_parallel_tasks` outside `1..10`.
- Empty `plagiarism.extensions`.
- Invalid hook mount type (non-string/non-list).

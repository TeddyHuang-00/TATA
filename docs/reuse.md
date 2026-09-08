# Reuse and Template Guide

## 1. Reuse strategy

Use this project as a starter by keeping only generic assets:

- [data/example/config.toml](../data/example/config.toml) — template assignment
  config (copy it to `data/<course>/<assignment>/config.toml`; see §3)
- [data/rubrics/example_rubric.toml](../data/rubrics/example_rubric.toml)
- [data/prompt/system.md](../data/prompt/system.md)
- a couple of generic provider files under `data/providers/`

Create one course folder with one assignment folder per assignment (see §2).

## 2. Three-layer config layout

Config is layered **global < course < assignment** (per-key assignment wins);
`rubrics/` and `prompt/` paths in any config resolve against the `data/` root:

```text
data/
├── config.toml              # global base (optional): [plagiarism] defaults, shared [fetch] defaults
├── <course>/                # one dir per course (default name = course_id)
│   ├── config.toml          # course config: course_id + [[fetch.assignments]] list
│   └── <assignment>/        # one dir per assignment (numeric dir name = id)
│       ├── config.toml      # [grading] required; no [fetch]
│       └── ...              # raw/ processed/ graded/ logs/ (auto-created on run)
├── rubrics/                 # rubric library (referenced as rubrics/<file>.toml)
├── prompt/                  # grading prompts (referenced as prompt/<file>.md)
└── providers/               # provider defs (<name>.toml, one provider per file)
```

## 3. Create a new assignment quickly

1. Create the course dir: `data/<course>/config.toml` with
   `[fetch] course_id = <id>` (course dir default name = course_id)
2. Create the assignment dir `data/<course>/<id>` — the numeric dir name is
   the assignment id; assignment configs carry no `[fetch]` section
3. Copy [data/example/config.toml](../data/example/config.toml) into it
4. Point `[grading].rubric` (`rubrics/<file>.toml`) and `[grading].system_prompt`
   (`prompt/<file>.md`) at library files under `data/` — or add your own
   rubric/prompt files there
5. Put student inputs into `raw/`; put reference into `reference.md` (or
   `reference.ipynb`/`reference.html`) at assignment root, or omit
   `[assignment].reference_file` for rubric-only grading; put plagiarism
   boilerplate into `template.ipynb` at assignment root
6. Register the assignment in the course config: `[[fetch.assignments]] id = <id>`

## 4. Config design notes

- Keep [assignment] optional unless you need non-default paths.
  `reference_file` is optional too — omit it for rubric-only grading
  (the default is no reference file).
- Keep [processing.input_format] optional if file extensions are consistent.
- Use [grading.max_parallel_tasks] to control throughput; valid range is 1-10.
- Use [plagiarism] to customize plagiarism output location, template file, and thresholds.

## 5. Recommended repository hygiene

- Do not commit real student submissions by default (`data/*` is gitignored,
  except `data/example/`).
- Keep assignment-specific data in ignored folders.
- Keep only reusable templates and examples in version control.
- Run `cli validate` on each assignment config when config models change.

## 6. Extending for new coursework types

- Add new rubric file under `data/rubrics/`.
- Add new prompt file under `data/prompt/`.
- If preprocessing differs, tune [processing] config first before adding code changes.

## 7. Common migration checklist

- Replace rubric path in config
- Replace prompt path in config
- Add or verify template path for plagiarism (`template.ipynb` by default)
- Ensure provider exists in [data/providers](../data/providers)
- Confirm reference file exists (when reference-based grading is used)
- Run preprocess, plagiarism, grade, score (analyze optional)

## 8. Cross-assignment plagiarism trend check

After individual assignment plagiarism runs, build a single aggregate view:

```bash
uv run main.py plagiarism -c data/<course>/config.toml --aggregate \
	--output misc/plagiarism_summary.md
```

Use this report to prioritize manual review for repeated high-similarity patterns across cohorts.
It uses per-assignment `plagiarism/all_pairs.json` full pair data by default.

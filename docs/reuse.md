# Reuse and Template Guide

## 1. Reuse strategy

Use this project as a starter by keeping only generic assets:

- [assignments/example/config.toml](../assignments/example/config.toml)
- [rubrics/example_rubric.toml](../rubrics/example_rubric.toml)
- [prompt/lab.md](../prompt/lab.md)
- schemas under [config](../config)

Create one folder per assignment under assignments/.

## 2. Create a new assignment quickly

1. Create new folder: `assignments/{name}`
2. Copy [assignments/example/config.toml](../assignments/example/config.toml)
3. Point rubric and prompt to your files
4. Put student inputs into `raw/`
5. Put reference into `processed/reference.md`

## 3. Config design notes

- Keep [assignment] optional unless you need non-default paths.
- Keep [processing.input_format] optional if file extensions are consistent.
- Use [grading.max_parallel_tasks] to control throughput; valid range is 1-10.

## 4. Recommended repository hygiene

- Do not commit real student submissions by default.
- Keep assignment-specific data in ignored folders.
- Keep only reusable templates and examples in version control.
- Regenerate schemas when config models change.

## 5. Extending for new coursework types

- Add new rubric file under `rubrics/`.
- Add new prompt file under `prompt/`.
- If preprocessing differs, tune [processing] config first before adding code changes.

## 6. Common migration checklist

- Replace rubric path in config
- Replace prompt path in config
- Ensure provider exists in [config/provider.toml](../config/provider.toml)
- Confirm reference file exists
- Run preprocess, then grade, then score

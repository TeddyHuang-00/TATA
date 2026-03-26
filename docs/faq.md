# FAQ

## 1. What is the minimum assignment config?

At minimum, your config must include:

```toml
[grading]
rubric = "rubrics/example_rubric.toml"
system_prompt = "prompt/system.md"
provider = "deepseek_chat_tool"
```

Use [assignments/example/config.toml](../assignments/example/config.toml) as the baseline.

## 2. Which paths are optional?

All fields under `[assignment]` are optional.

If omitted, defaults are:

- raw_dir -> `raw`
- processed_dir -> `processed`
- graded_dir -> `graded`
- logs_dir -> `logs`
- reference_file -> `reference.md`

## 3. Do I need to create folders manually?

No. The pipeline now auto-creates assignment folders when running stages.

## 4. How do I generate all schemas?

Run:

```bash
uv run main.py --stage schema
```

This generates:

- [config/assignment.schema.json](../config/assignment.schema.json)
- [config/provider.schema.json](../config/provider.schema.json)
- [config/rubric.schema.json](../config/rubric.schema.json)

## 5. How can I speed up grading?

Set `grading.max_parallel_tasks` in config. Valid range is `1` to `10`.

```toml
[grading]
max_parallel_tasks = 10
```

## 6. What is the recommended stage order?

Use this order:

1. preprocess
2. grade
3. score

Or run all at once:

```bash
uv run main.py --stage all --config assignments/my-assignment/config.toml
```

## 7. Where are outputs written?

- Processed markdown: `processed/`
- Grading JSON: `graded/*.json`
- Score summaries: `graded/*.md`
- Logs and checkpoint: `logs/`

## 8. Why do I get "All submissions already graded (checkpoint hit)"?

The checkpoint file records completed submissions.

If you want to regrade from scratch, remove:

- `logs/grading.checkpoint.json`
- old files in `graded/`

Then run grade again.

## 9. Can preprocessing accept multiple submission formats?

Yes. `processing.input_format` supports both a single value and a list.

Single format:

```toml
[processing]
input_format = "ipynb"
```

Multiple formats:

```toml
[processing]
input_format = ["ipynb", "html", "markdown"]
```

If omitted, preprocessing auto-detects from the first supported file in `raw/`.

## 10. Does reference answer have to be markdown?

No. Grade stage accepts reference files in:

- `.md`
- `.ipynb`
- `.html`

Set `[assignment].reference_file` to any of those formats. Non-markdown references are converted automatically during grading.

Recommended location is assignment root (for example `assignments/my-assignment/reference.ipynb`) so it is separate from student submissions.

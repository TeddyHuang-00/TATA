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
uv run main.py schema
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

1. plagiarism (optional but recommended)
1. preprocess
1. grade
1. score
1. analyze (optional)

Or run all at once:

```bash
uv run main.py preprocess -c assignments/my-assignment/config.toml
uv run main.py plagiarism -c assignments/my-assignment/config.toml
uv run main.py grade -c assignments/my-assignment/config.toml
uv run main.py score -c assignments/my-assignment/config.toml
uv run main.py analyze -c assignments/my-assignment/config.toml
```

## 7. Where are outputs written?

- Processed markdown: `processed/`
- Grading JSON: `graded/*.json`
- Score summaries: `scored/` (format-specific subfolders)
- Logs and checkpoint: `logs/`
- Plagiarism report and extracted files: `plagiarism/report.html`, `plagiarism/submissions/`, `plagiarism/template/`

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

## 11. How does plagiarism detection reduce boilerplate false positives?

Plagiarism stage uses `copydetect` with a template boilerplate source.

By default it expects `template.ipynb` in assignment root and extracts code into:

- `plagiarism/template/template.py`

Student code is extracted into:

- `plagiarism/submissions/*.py`

Then a report is generated at:

- `plagiarism/report.html`

And full pairwise comparison data is exported at:

- `plagiarism/all_pairs.json`

All paths can be customized via `[plagiarism]` config.

## 12. Does a high plagiarism score always mean a student cheated?

No. A high similarity score is a signal for manual review, not automatic proof of misconduct.

Common non-cheating causes include:

- Assignment prompts that are very constrained/straightforward
- Small solution space where many students produce near-identical code
- Shared starter structure or repetitive required steps

Recommended workflow:

1. Treat plagiarism results as triage candidates.
1. Compare highlighted regions for substantive logic overlap, not just scaffolding.
1. Check assignment context (difficulty, template rigidity, expected idioms) before conclusions.
1. Escalate only when evidence is consistent with policy.

## 13. Is there a helper to audit TODO instruction/code mismatches in reference notebooks?

Yes. Use:

```bash
uv run misc/reference_mismatch_audit.py \
	--notebook assignments/my-assignment/reference.ipynb
```

You can also output JSON:

```bash
uv run misc/reference_mismatch_audit.py \
	--notebook assignments/my-assignment/reference.ipynb \
	--format json \
	--output misc/audit_report.json
```

## 14. Can I combine plagiarism results across all assignments into one report?

Yes. Use the aggregate helper script:

```bash
uv run misc/plagiarism_report_aggregate.py \
	--pairwise-alpha 0.01 \
	--individual-alpha 0.01 \
	--output misc/plagiarism_summary.md
```

Useful options:

- `--format json` for machine-readable output
- `--pairwise-alpha 0.005` for stricter pair-level significance
- `--individual-alpha 0.005` for stricter student-level significance
- `--score-floor 0.001` and `--score-cap 0.999` to control logit clipping bounds

The script uses Pydantic/pydantic-settings for CLI option parsing and validation.
It reads `plagiarism/all_pairs.json` (full pair coverage).
It reports significant pairs and significant students as separate sections.

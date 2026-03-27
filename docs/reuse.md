# Reuse and Template Guide

## 1. Reuse strategy

Use this project as a starter by keeping only generic assets:

- [assignments/example/config.toml](../assignments/example/config.toml)
- [rubrics/example_rubric.toml](../rubrics/example_rubric.toml)
- [prompt/system.md](../prompt/system.md)
- schemas under [config](../config)

Create one folder per assignment under assignments/.

## 2. Create a new assignment quickly

1. Create new folder: `assignments/{name}`
2. Copy [assignments/example/config.toml](../assignments/example/config.toml)
3. Point rubric and prompt to your files
4. Put student inputs into `raw/`
5. Put reference into `reference.md` (or `reference.ipynb`/`reference.html`) at assignment root
6. Put plagiarism boilerplate into `template.ipynb` at assignment root

## 3. Config design notes

- Keep [assignment] optional unless you need non-default paths.
- Keep [processing.input_format] optional if file extensions are consistent.
- Use [grading.max_parallel_tasks] to control throughput; valid range is 1-10.
- Use [plagiarism] to customize plagiarism output location, template file, and thresholds.

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
- Add or verify template path for plagiarism (`template.ipynb` by default)
- Ensure provider exists in [config/provider.toml](../config/provider.toml)
- Confirm reference file exists
- Run plagiarism, preprocess, grade, score (analyze optional)

## 7. Reference mismatch quality gate

For notebook-based labs, run TODO/instruction mismatch audit on your reference notebook before grading:

```bash
uv run python misc/reference_mismatch_audit.py \
	--notebook assignments/{name}/reference.ipynb
```

Treat this as a preflight check to reduce rubric ambiguity and downstream grading drift.

## 8. Cross-assignment plagiarism trend check

After individual assignment plagiarism runs, build a single aggregate view:

```bash
uv run python misc/plagiarism_report_aggregate.py \
	--alpha 0.01 \
	--output misc/plagiarism_summary.md
```

Use this report to prioritize manual review for repeated high-similarity patterns across cohorts.
It uses per-assignment `plagiarism/all_pairs.json` full pair data by default.

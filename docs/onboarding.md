# Onboarding Guide

## 1. Prerequisites

- Python 3.13+
- uv installed
- API credentials configured for your provider

## 2. Install dependencies

Run from project root:

```bash
uv sync
```

## 3. Configure provider credentials

Create or update `.env` in project root.

Example for DeepSeek:

```env
DEEPSEEK_API_KEY=your_key_here
```

Provider definitions are in [config/provider.toml](../config/provider.toml).

## 4. Generate schemas (recommended)

Generate all schemas before creating a new assignment config:

```bash
uv run main.py --stage schema
```

This generates:

- [config/assignment.schema.json](../config/assignment.schema.json)
- [config/provider.schema.json](../config/provider.schema.json)
- [config/rubric.schema.json](../config/rubric.schema.json)

## 5. Start from example config

Copy [assignments/example/config.toml](../assignments/example/config.toml) and edit it for your assignment.

Minimal required fields are in `[grading]` only:

- rubric
- system_prompt
- provider

Path-related fields under `[assignment]` are optional and default to:

- raw
- processed
- graded
- logs
- reference.md

Plagiarism settings are optional under `[plagiarism]` and default to:

- output_dir -> `plagiarism`
- template_file -> `template.ipynb`
- submissions_subdir -> `submissions`
- template_subdir -> `template`
- report_file -> `report.html`
- full_pairs_file -> `all_pairs.json`

## 6. Prepare assignment files

For an assignment folder (for example `assignments/my-assignment`):

- put student submissions into `raw/`
- put the reference answer into `reference.md` at assignment root
- put plagiarism boilerplate template into `template.ipynb` at assignment root (recommended)
- reference supports `.md`, `.ipynb`, or `.html`
- ensure your rubric and prompt files exist

## 7. Run pipeline stages

Preprocess only:

```bash
uv run main.py --stage preprocess --config assignments/my-assignment/config.toml
```

Grade only:

```bash
uv run main.py --stage grade --config assignments/my-assignment/config.toml
```

Plagiarism only:

```bash
uv run main.py --stage plagiarism --config assignments/my-assignment/config.toml
```

Score only:

```bash
uv run main.py --stage score --config assignments/my-assignment/config.toml
```

Analyze grading quality (meta analysis):

```bash
uv run main.py --stage analyze --config assignments/my-assignment/config.toml
```

Audit reference notebook TODO/instruction mismatches:

```bash
uv run python misc/reference_mismatch_audit.py \
	--notebook assignments/my-assignment/reference.ipynb
```

Aggregate plagiarism reports across assignments:

```bash
uv run python misc/plagiarism_report_aggregate.py \
	--pairwise-alpha 0.01 \
	--individual-alpha 0.01 \
	--output misc/plagiarism_summary.md
```

Run all stages:

```bash
uv run main.py --stage all --config assignments/my-assignment/config.toml
```

`all` executes stages in this order: plagiarism -> preprocess -> grade -> score -> analyze.

## 8. Outputs

- Processed markdown: `processed/`
- Structured grading JSON: `graded/*.json`
- Score summaries: `scored/` (format-specific subfolders)
- Logs and checkpoint: `logs/`
- Meta analysis reports: `logs/meta_analysis.json`, `logs/meta_analysis.md`
- Plagiarism outputs: `plagiarism/report.html`, `plagiarism/submissions/`, `plagiarism/template/`
- Full pair data for aggregation: `plagiarism/all_pairs.json`

## 9. Need help?

- Hooks lifecycle and IO contract: [hooks.md](hooks.md)
- Frequently asked questions: [faq.md](faq.md)
- Common issues and fixes: [troubleshooting.md](troubleshooting.md)
- Assignment config format (manual reference): [config/assignment.md](config/assignment.md)
- Provider config format (manual reference): [config/provider.md](config/provider.md)
- Rubric config format (manual reference): [config/rubric.md](config/rubric.md)

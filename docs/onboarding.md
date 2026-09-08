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

Provider definitions are one file each in [data/providers](../data/providers)
(`<name>.toml`, flat top-level keys; the file stem is the provider name).

## 4. Validate a config (recommended)

Validate before creating a new assignment config:

```bash
uv run cli validate -c data/<course>/<assignment>/config.toml
```

The bare `data/example/config.toml` copy source validates only at its
destination depth (`data/<course>/<assignment>/`), where rubric/prompt
paths resolve against `data/`.

`validate` checks the config with the same pydantic models used at runtime,
plus the rubric files, prompt files, `[grading].provider` against
`data/providers/`, and the reference file when set. It exits `1` when
anything is wrong.

## 5. Start from example config

Copy [data/example/config.toml](../data/example/config.toml) and edit it for your assignment.

Minimal required fields are in `[grading]` only:

- rubric
- system_prompt
- provider

Path-related fields under `[assignment]` are optional and default to:

- raw
- processed
- graded
- logs
- none (no reference file — set `[assignment].reference_file` to enable
  reference-based grading)

Plagiarism settings are optional under `[plagiarism]` and default to:

- output_dir -> `plagiarism`
- template_file -> `template.ipynb`
- submissions_subdir -> `submissions`
- template_subdir -> `template`
- report_file -> `report.html`
- full_pairs_file -> `all_pairs.json`

## 6. Prepare assignment files

For an assignment folder (for example `data/my-assignment`):

- put student submissions into `raw/`
- put the reference answer into `reference.md` at assignment root
- put plagiarism boilerplate template into `template.ipynb` at assignment root (recommended)
- reference supports `.md`, `.ipynb`, or `.html`
- ensure your rubric and prompt files exist

## 7. Run pipeline stages

Preprocess only:

```bash
uv run main.py preprocess -c data/my-assignment/config.toml
```

Grade only:

```bash
uv run main.py grade -c data/my-assignment/config.toml
```

Plagiarism only:

```bash
uv run main.py plagiarism -c data/my-assignment/config.toml
```

Score only:

```bash
uv run main.py score -c data/my-assignment/config.toml
```

Analyze grading quality (meta analysis):

```bash
uv run main.py analyze -c data/my-assignment/config.toml
```

Aggregate plagiarism reports across assignments:

```bash
uv run main.py plagiarism -c data/config.toml --aggregate \
	--output misc/plagiarism_summary.md
```

Run every stage — in this order (plagiarism is optional but recommended):
`preprocess` → `plagiarism` → `grade` → `score` → `analyze`, run each one
individually:

```bash
uv run main.py preprocess -c data/my-assignment/config.toml
uv run main.py plagiarism -c data/my-assignment/config.toml
uv run main.py grade -c data/my-assignment/config.toml
uv run main.py score -c data/my-assignment/config.toml
uv run main.py analyze -c data/my-assignment/config.toml
```

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

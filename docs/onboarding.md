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
- processed/reference.md

## 6. Prepare assignment files

For an assignment folder (for example `assignments/my-assignment`):

- put student submissions into `raw/`
- put the reference answer into `processed/reference.md`
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

Score only:

```bash
uv run main.py --stage score --config assignments/my-assignment/config.toml
```

Run all stages:

```bash
uv run main.py --stage all --config assignments/my-assignment/config.toml
```

## 8. Outputs

- Processed markdown: `processed/`
- Structured grading JSON: `graded/*.json`
- Score summaries: `graded/*.md`
- Logs and checkpoint: `logs/`

## 9. Need help?

- Frequently asked questions: [faq.md](faq.md)
- Common issues and fixes: [troubleshooting.md](troubleshooting.md)

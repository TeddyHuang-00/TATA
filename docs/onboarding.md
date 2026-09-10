# Onboarding Guide

## 1. Prerequisites

- A computer with Windows, macOS, or Linux
- The [uv](https://docs.astral.sh/uv/) tool (install it once, see below)
- A local LLM server (Ollama, no API key needed) or API credentials for
  your cloud LLM provider (see section 3)

You never install Python yourself: on your first `uv sync`, uv downloads
Python 3.13 and every dependency automatically.

Install uv (one time only):

- Windows (open PowerShell, paste this):
  `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
- macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`

Close and reopen your terminal so the `uv` command is available.

## 2. Install dependencies

Run from project root:

```bash
uv sync
```

## 3. Configure provider credentials

**Ollama (recommended, no API key needed).** Install
[Ollama](https://ollama.com), then:

```bash
ollama serve
ollama pull qwen3.8:latest
```

Provider definitions are one file each under
[data/providers](../data/providers) (`<name>.toml`; flat top-level keys;
the file stem is the provider name). The bundled
[ollama.toml](../data/providers/ollama.toml) already points at
`http://localhost:11434/v1` with model `qwen3.8:latest` (the `latest` tag
of Qwen3.8-27B, 27.3B params), so the shipped example
configs work out of the box.

**Cloud alternative: DeepSeek (needs an API key).** Copy
[.env.sample](../.env.sample) to `.env` in project root and fill in your
key:

```env
DEEPSEEK_API_KEY=your_key_here
```

The bundled example
[deepseek.toml](../data/providers/deepseek.toml)
writes `api_key = "${DEEPSEEK_API_KEY}"`, so it picks the key up from
`.env` automatically.

Either way, you can also manage providers in the TUI: Library → Providers,
create or edit a provider and paste the key directly into the `api_key`
field.

## 4. Validate a config (recommended)

Validate before creating a new assignment config:

```bash
uv run cli validate -c data/<course>/<assignment>/config.toml
```

The shipped `data/example/config.toml` (with its bundled rubric, prompt,
and provider files) validates as-is on a fresh clone.

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

The example config references the bundled provider `ollama`
([ollama.toml](../data/providers/ollama.toml)); swap in a different
provider name (e.g. `deepseek`) if you have API keys instead of
a local Ollama server.

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
uv run cli preprocess -c data/my-assignment/config.toml
```

Grade only:

```bash
uv run cli grade -c data/my-assignment/config.toml
```

Plagiarism only:

```bash
uv run cli plagiarism -c data/my-assignment/config.toml
```

Score only:

```bash
uv run cli score -c data/my-assignment/config.toml
```

Analyze grading quality (meta analysis):

```bash
uv run cli analyze -c data/my-assignment/config.toml
```

Aggregate plagiarism reports across assignments:

```bash
uv run cli plagiarism -c data/config.toml --aggregate \
	--output misc/plagiarism_summary.md
```

Run every stage — in this order (plagiarism is optional but recommended):
`preprocess` → `plagiarism` → `grade` → `score` → `analyze`, run each one
individually:

```bash
uv run cli preprocess -c data/my-assignment/config.toml
uv run cli plagiarism -c data/my-assignment/config.toml
uv run cli grade -c data/my-assignment/config.toml
uv run cli score -c data/my-assignment/config.toml
uv run cli analyze -c data/my-assignment/config.toml
```

## 8. Outputs

- Processed markdown: `processed/`
- Structured grading JSON: `graded/*.json`
- Score summaries: `scored/` (format-specific subfolders)
- Logs: `logs/`
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
